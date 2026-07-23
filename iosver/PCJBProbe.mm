#import <Foundation/Foundation.h>
#import <UIKit/UIKit.h>
#import <objc/runtime.h>
#import <mach-o/dyld.h>
#import <dlfcn.h>
#import <sys/stat.h>
#import <sys/sysctl.h>
#import <sys/types.h>
#import <fcntl.h>
#import <unistd.h>
#import <signal.h>
#import <stdarg.h>
#import <string.h>
#import <errno.h>
#import <pthread.h>
#import <sys/time.h>
#import <dispatch/dispatch.h>
#import <execinfo.h>
#import <mach/mach.h>
#include <typeinfo>

typedef void (*MSHookFunctionType)(void *symbol, void *replace, void **result);
typedef void (*MSHookMemoryType)(void *target, const void *data, size_t size);
static MSHookFunctionType gHookFunction;
static MSHookMemoryType gHookMemory;
static __thread bool gInLog;
static bool gUnityHooksInstalled;

typedef struct {
    void *klass;
    void *monitor;
    int32_t length;
    unichar chars[0];
} PCIl2CppString;

static NSString *PCStringFromIl2Cpp(const void *value) {
    if (!value) return @"(null)";
    const PCIl2CppString *string = (const PCIl2CppString *)value;
    if (string->length < 0 || string->length > 32768) {
        return [NSString stringWithFormat:@"(invalid:%p len=%d)", value, string->length];
    }
    return [NSString stringWithCharacters:string->chars length:(NSUInteger)string->length];
}

static pthread_mutex_t gPersistentLogLock = PTHREAD_MUTEX_INITIALIZER;
static int gPersistentLogFD = -1;
static int gUnityNativeLogFD = -1;
static int gUnityCrashHistoryFD = -1;
static NSString *gPersistentLogPath;
static NSString *gUnityNativeLogPath;
static NSString *gUnityCrashHistoryPath;

typedef struct {
    bool valid;
    struct timeval capturedAt;
    void *wrapper;
    void *managedException;
    char exceptionClass[192];
    char message[1024];
    char managedStack[3072];
    void *nativeFrames[48];
    int nativeFrameCount;
} PCManagedThrowSnapshot;

static __thread PCManagedThrowSnapshot gLastManagedThrow;
static __thread bool gInCxaThrowProbe;

static bool PCReadableRange(const void *pointer, size_t length) {
    if (!pointer || length == 0) return false;
    uintptr_t start = (uintptr_t)pointer;
    uintptr_t end = start + length - 1;
    if (end < start) return false;
    uint8_t probe = 0;
    vm_size_t bytesRead = 0;
    kern_return_t result = vm_read_overwrite(
        mach_task_self(), (vm_address_t)start, 1, (vm_address_t)&probe,
        &bytesRead);
    if (result != KERN_SUCCESS || bytesRead != 1) return false;
    if (end == start) return true;
    bytesRead = 0;
    result = vm_read_overwrite(
        mach_task_self(), (vm_address_t)end, 1, (vm_address_t)&probe,
        &bytesRead);
    return result == KERN_SUCCESS && bytesRead == 1;
}

static NSString *PCSafeStringFromIl2Cpp(const void *value) {
    if (!PCReadableRange(value, offsetof(PCIl2CppString, chars))) return @"";
    const PCIl2CppString *string = (const PCIl2CppString *)value;
    int32_t length = string->length;
    if (length < 0 || length > 32768) return @"";
    if (length == 0) return @"";
    if (!PCReadableRange(string->chars, (size_t)length * sizeof(unichar))) {
        return @"";
    }
    return [NSString stringWithCharacters:string->chars
                                    length:(NSUInteger)length] ?: @"";
}

static size_t PCUTF8FromIl2CppString(const void *value, char *output,
                                    size_t outputSize) {
    if (!output || outputSize == 0) return 0;
    output[0] = '\0';
    if (!PCReadableRange(value, offsetof(PCIl2CppString, chars))) return 0;
    const PCIl2CppString *string = (const PCIl2CppString *)value;
    int32_t length = string->length;
    if (length < 0 || length > 32768 ||
        !PCReadableRange(string->chars, (size_t)length * sizeof(unichar))) {
        return 0;
    }

    size_t written = 0;
    for (int32_t index = 0; index < length && written + 1 < outputSize; index++) {
        uint32_t codepoint = string->chars[index];
        if (codepoint >= 0xD800 && codepoint <= 0xDBFF && index + 1 < length) {
            uint32_t low = string->chars[index + 1];
            if (low >= 0xDC00 && low <= 0xDFFF) {
                codepoint = 0x10000 + ((codepoint - 0xD800) << 10) +
                            (low - 0xDC00);
                index++;
            }
        }

        char encoded[4] = {};
        size_t encodedLength = 0;
        if (codepoint <= 0x7F) {
            encoded[0] = (char)codepoint;
            encodedLength = 1;
        } else if (codepoint <= 0x7FF) {
            encoded[0] = (char)(0xC0 | (codepoint >> 6));
            encoded[1] = (char)(0x80 | (codepoint & 0x3F));
            encodedLength = 2;
        } else if (codepoint <= 0xFFFF) {
            encoded[0] = (char)(0xE0 | (codepoint >> 12));
            encoded[1] = (char)(0x80 | ((codepoint >> 6) & 0x3F));
            encoded[2] = (char)(0x80 | (codepoint & 0x3F));
            encodedLength = 3;
        } else {
            encoded[0] = (char)(0xF0 | (codepoint >> 18));
            encoded[1] = (char)(0x80 | ((codepoint >> 12) & 0x3F));
            encoded[2] = (char)(0x80 | ((codepoint >> 6) & 0x3F));
            encoded[3] = (char)(0x80 | (codepoint & 0x3F));
            encodedLength = 4;
        }
        if (written + encodedLength >= outputSize) break;
        memcpy(output + written, encoded, encodedLength);
        written += encodedLength;
    }
    output[written] = '\0';
    return written;
}

static void PCCopyReadableCString(const char *value, char *output,
                                 size_t outputSize) {
    if (!output || outputSize == 0) return;
    output[0] = '\0';
    if (!value) return;
    size_t index = 0;
    while (index + 1 < outputSize && PCReadableRange(value + index, 1)) {
        char character = value[index];
        if (!character) break;
        output[index++] = character;
    }
    output[index] = '\0';
}

static void PCDescribeManagedException(void *exception, char *exceptionClass,
                                      size_t classSize, char *message,
                                      size_t messageSize, char *stack,
                                      size_t stackSize) {
    if (classSize) exceptionClass[0] = '\0';
    if (messageSize) message[0] = '\0';
    if (stackSize) stack[0] = '\0';
    if (!PCReadableRange(exception, 0x48)) return;

    uint8_t *object = (uint8_t *)exception;
    void *klass = *(void **)object;
    void *classNameString = *(void **)(object + 0x10);
    void *messageString = *(void **)(object + 0x18);
    void *stackString = *(void **)(object + 0x40);

    PCUTF8FromIl2CppString(classNameString, exceptionClass, classSize);
    PCUTF8FromIl2CppString(messageString, message, messageSize);
    PCUTF8FromIl2CppString(stackString, stack, stackSize);

    if (!exceptionClass[0] && PCReadableRange(klass, 0x20)) {
        const char *className = *(const char **)((uint8_t *)klass + 0x10);
        const char *nameSpace = *(const char **)((uint8_t *)klass + 0x18);
        char rawClass[128] = {};
        char rawNamespace[64] = {};
        PCCopyReadableCString(className, rawClass, sizeof(rawClass));
        PCCopyReadableCString(nameSpace, rawNamespace, sizeof(rawNamespace));
        if (rawNamespace[0]) {
            snprintf(exceptionClass, classSize, "%s.%s", rawNamespace, rawClass);
        } else if (rawClass[0]) {
            snprintf(exceptionClass, classSize, "%s", rawClass);
        }
    }
    if (!exceptionClass[0]) snprintf(exceptionClass, classSize, "(unknown)");
    if (!message[0]) snprintf(message, messageSize, "(no message)");
    if (!stack[0]) snprintf(stack, stackSize, "(managed stack unavailable)");
}

static void PCWriteAll(int fileDescriptor, const char *bytes, size_t length) {
    if (fileDescriptor < 0 || !bytes || length == 0) return;
    while (length > 0) {
        ssize_t written = write(fileDescriptor, bytes, length);
        if (written <= 0) return;
        bytes += written;
        length -= (size_t)written;
    }
}

static void PCAppendRawCrashLine(const char *message, bool includePersistent) {
    if (!message) return;
    struct timeval value = {};
    gettimeofday(&value, nullptr);
    struct tm localTime = {};
    localtime_r(&value.tv_sec, &localTime);
    char line[8192] = {};
    int length = snprintf(
        line, sizeof(line),
        "%04d-%02d-%02d %02d:%02d:%02d.%03d pid=%d #pc  %s\n",
        localTime.tm_year + 1900, localTime.tm_mon + 1, localTime.tm_mday,
        localTime.tm_hour, localTime.tm_min, localTime.tm_sec,
        (int)(value.tv_usec / 1000), getpid(), message);
    if (length <= 0) return;
    size_t safeLength = MIN((size_t)length, sizeof(line) - 1);
    pthread_mutex_lock(&gPersistentLogLock);
    if (includePersistent) {
        PCWriteAll(gPersistentLogFD, line, safeLength);
    }
    PCWriteAll(gUnityCrashHistoryFD, line, safeLength);
    pthread_mutex_unlock(&gPersistentLogLock);
}

static void PCAppendManagedExceptionReport(const char *source, void *exception,
                                           bool terminating) {
    char exceptionClass[192] = {};
    char message[1024] = {};
    char stack[3072] = {};
    PCDescribeManagedException(exception, exceptionClass, sizeof(exceptionClass),
                               message, sizeof(message), stack, sizeof(stack));
    char report[6144] = {};
    snprintf(report, sizeof(report),
             "UnityCrash.Managed source=%s terminating=%d exception=%p "
             "class=%s message=%s stack=%s",
             source ?: "(unknown)", terminating ? 1 : 0, exception,
             exceptionClass, message, stack);
    PCAppendRawCrashLine(report, true);
}

static NSString *PCTimestamp(void) {
    struct timeval value = {};
    gettimeofday(&value, nullptr);
    struct tm localTime = {};
    localtime_r(&value.tv_sec, &localTime);
    char buffer[40] = {};
    snprintf(buffer, sizeof(buffer), "%04d-%02d-%02d %02d:%02d:%02d.%03d",
             localTime.tm_year + 1900, localTime.tm_mon + 1, localTime.tm_mday,
             localTime.tm_hour, localTime.tm_min, localTime.tm_sec,
             (int)(value.tv_usec / 1000));
    return [NSString stringWithUTF8String:buffer];
}

static void PCAppendPersistentLine(NSString *message) {
    if (gPersistentLogFD < 0 || !message) return;
    @autoreleasepool {
        NSString *line = [NSString stringWithFormat:@"%@ pid=%d %@\n",
                          PCTimestamp(), getpid(), message];
        NSData *data = [line dataUsingEncoding:NSUTF8StringEncoding];
        if (!data.length) return;
        pthread_mutex_lock(&gPersistentLogLock);
        const uint8_t *bytes = (const uint8_t *)data.bytes;
        size_t remaining = data.length;
        while (remaining > 0) {
            ssize_t written = write(gPersistentLogFD, bytes, remaining);
            if (written <= 0) break;
            bytes += written;
            remaining -= (size_t)written;
        }
        pthread_mutex_unlock(&gPersistentLogLock);
    }
}

static void PCPersistentNSLog(NSString *format, ...) NS_FORMAT_FUNCTION(1, 2);
static void PCPersistentNSLog(NSString *format, ...) {
    @autoreleasepool {
        va_list arguments;
        va_start(arguments, format);
        NSString *message = [[NSString alloc] initWithFormat:format arguments:arguments];
        va_end(arguments);
        PCAppendPersistentLine(message);
        // This is the real Foundation NSLog.  The macro below is intentionally
        // declared after this function so the live unified log is preserved.
        NSLog(@"%@", message);
    }
}

static void PCInitializePersistentLogs(void) {
    @autoreleasepool {
        NSString *directory = [NSHomeDirectory()
            stringByAppendingPathComponent:@"Library/Caches/PCJBProbe"];
        NSFileManager *manager = NSFileManager.defaultManager;
        [manager createDirectoryAtPath:directory
           withIntermediateDirectories:YES
                            attributes:nil
                                 error:nil];

        gPersistentLogPath = [directory stringByAppendingPathComponent:@"PCJBProbe-current.log"];
        NSString *previous = [directory stringByAppendingPathComponent:@"PCJBProbe-previous.log"];
        [manager removeItemAtPath:previous error:nil];
        if ([manager fileExistsAtPath:gPersistentLogPath]) {
            [manager moveItemAtPath:gPersistentLogPath toPath:previous error:nil];
        }

        gUnityNativeLogPath = [directory stringByAppendingPathComponent:@"UnityNative-current.log"];
        NSString *nativePrevious =
            [directory stringByAppendingPathComponent:@"UnityNative-previous.log"];
        [manager removeItemAtPath:nativePrevious error:nil];
        if ([manager fileExistsAtPath:gUnityNativeLogPath]) {
            [manager moveItemAtPath:gUnityNativeLogPath toPath:nativePrevious error:nil];
        }

        gUnityCrashHistoryPath =
            [directory stringByAppendingPathComponent:@"UnityCrash-history.log"];
        NSDictionary<NSFileAttributeKey, id> *crashAttributes =
            [manager attributesOfItemAtPath:gUnityCrashHistoryPath error:nil];
        if ([crashAttributes fileSize] > 4 * 1024 * 1024) {
            NSString *crashPrevious =
                [directory stringByAppendingPathComponent:@"UnityCrash-previous.log"];
            [manager removeItemAtPath:crashPrevious error:nil];
            [manager moveItemAtPath:gUnityCrashHistoryPath
                             toPath:crashPrevious
                              error:nil];
        }

        gPersistentLogFD = open(gPersistentLogPath.fileSystemRepresentation,
                                O_CREAT | O_WRONLY | O_APPEND, 0644);
        gUnityNativeLogFD = open(gUnityNativeLogPath.fileSystemRepresentation,
                                O_CREAT | O_WRONLY | O_APPEND, 0644);
        gUnityCrashHistoryFD =
            open(gUnityCrashHistoryPath.fileSystemRepresentation,
                 O_CREAT | O_WRONLY | O_APPEND, 0644);
        if (gUnityNativeLogFD >= 0) {
            dup2(gUnityNativeLogFD, STDOUT_FILENO);
            dup2(gUnityNativeLogFD, STDERR_FILENO);
            setvbuf(stdout, nullptr, _IONBF, 0);
            setvbuf(stderr, nullptr, _IONBF, 0);
        }
    }
}

#define NSLog(...) PCPersistentNSLog(__VA_ARGS__)

// MARK: - In-game plugin panel

static NSString *const PCSkipTouchToStartDefaultsKey =
    @"PCJBProbe.skipTouchToStart";
static NSString *const PCBattleRollbackDefaultsKey =
    @"PCJBProbe.battleRollbackOnDefeat";
static NSString *const PCFloatingEdgeLeftKey = @"PCJBProbe.floatingEdgeLeft";
static NSString *const PCFloatingYRatioKey = @"PCJBProbe.floatingYRatio";
static const NSTimeInterval kPCFloatingCollapseDelay = 2.0;
static const CGFloat kPCFloatingButtonSize = 40.0;
static const CGFloat kPCFloatingCollapsedVisible = 0.32;  // thin AssistiveTouch-like strip
static const CGFloat kPCFloatingCollapsedAlpha = 0.42;
static bool gSkipTouchToStartEnabled = true;
static bool gBattleRollbackEnabled;
static int32_t gBattleRollbackBaseStage = -1;
static int32_t gBattleRollbackBaseSector = -1;
static int32_t gBattleRollbackTargetStage = -1;
static int32_t gBattleRollbackTargetSector = -1;
static int32_t gBattleRollbackLastRequestedStage = -1;
static int32_t gBattleRollbackLastRequestedSector = -1;
static bool gBattleRollbackRestorePending;

// Values copied from the game's real PS_Auth request.  These keys match
// chlz/client/config.py so the copied JSON can be transferred directly to the
// script's AccountConfig.  Never write the credential values to the log.
static pthread_mutex_t gLoginInfoLock = PTHREAD_MUTEX_INITIALIZER;
static NSString *gLoginVersion = @"";
static NSString *gLoginDataNo = @"";
static NSString *gLoginClientID = @"";
static NSString *gLoginDeviceID = @"";
static NSString *gLoginPlatformUserID = @"";
static NSString *gLoginDeviceModel = @"";
static NSString *gLoginOperatingSystem = @"";
static NSString *gLoginAdID = @"";
static NSString *gLoginPushToken = @"";
static int32_t gLoginRegionType;
static int32_t gLoginCountry;
static int32_t gLoginStoreRegionCode;
static int32_t gLoginServerNum;
static bool gLoginIsGuest;
static bool gLoginAuthRequestCaptured;

static NSDictionary<NSString *, id> *PCLoginInfoDictionary(bool *readyOut) {
    pthread_mutex_lock(&gLoginInfoLock);
    NSString *version = [gLoginVersion copy];
    NSString *dataNo = [gLoginDataNo copy];
    NSString *clientID = [gLoginClientID copy];
    NSString *deviceID = [gLoginDeviceID copy];
    NSString *platformUserID = [gLoginPlatformUserID copy];
    NSString *deviceModel = [gLoginDeviceModel copy];
    NSString *operatingSystem = [gLoginOperatingSystem copy];
    NSString *adID = [gLoginAdID copy];
    NSString *pushToken = [gLoginPushToken copy];
    int32_t regionType = gLoginRegionType;
    int32_t country = gLoginCountry;
    int32_t storeRegionCode = gLoginStoreRegionCode;
    int32_t serverNum = gLoginServerNum;
    bool isGuest = gLoginIsGuest;
    bool ready = gLoginAuthRequestCaptured && clientID.length > 0 &&
                 deviceID.length > 0 && platformUserID.length > 0;
    pthread_mutex_unlock(&gLoginInfoLock);

    if (readyOut) *readyOut = ready;
    return @{
        @"client_id" : clientID ?: @"",
        @"device_id" : deviceID ?: @"",
        @"platform_user_id" : platformUserID ?: @"",
        @"device_model" : deviceModel ?: @"",
        @"operating_system" : operatingSystem ?: @"",
        @"ad_id" : adID ?: @"",
        @"push_token" : pushToken ?: @"",
        @"is_guest" : @(isGuest),
        @"country" : @(country),
        @"store_region_code" : @(storeRegionCode),
        @"region_type" : @(regionType),
        @"data_no" : dataNo ?: @"",
        @"preferred_server_num" : @(serverNum),
        @"version" : version ?: @""
    };
}

static NSString *PCLoginInfoJSON(bool *readyOut) {
    bool ready = false;
    NSDictionary<NSString *, id> *dictionary = PCLoginInfoDictionary(&ready);
    NSError *error = nil;
    NSJSONWritingOptions options = NSJSONWritingPrettyPrinted;
    if (@available(iOS 11.0, *)) options |= NSJSONWritingSortedKeys;
    NSData *data = [NSJSONSerialization dataWithJSONObject:dictionary
                                                   options:options
                                                     error:&error];
    if (readyOut) *readyOut = ready;
    if (!data || error) return @"无法生成登录信息";
    return [[NSString alloc] initWithData:data
                                 encoding:NSUTF8StringEncoding] ?: @"";
}

static void PCApplySkipTouchToStartIfReady(void);

static bool PCSkipTouchToStartEnabled(void) {
    return gSkipTouchToStartEnabled;
}

static void PCSetSkipTouchToStartEnabled(bool enabled, bool persist) {
    bool previous = gSkipTouchToStartEnabled;
    gSkipTouchToStartEnabled = enabled;
    if (persist) {
        [NSUserDefaults.standardUserDefaults setBool:enabled
                                              forKey:PCSkipTouchToStartDefaultsKey];
    }
    if (previous != enabled || persist) {
        NSLog(@"#pc  PluginSetting.SkipTouchToStart enabled=%d persisted=%d",
              enabled ? 1 : 0, persist ? 1 : 0);
    }
    if (enabled) PCApplySkipTouchToStartIfReady();
}

static bool PCBattleRollbackEnabled(void) {
    return gBattleRollbackEnabled;
}

static void PCSetBattleRollbackEnabled(bool enabled, bool persist) {
    bool previous = gBattleRollbackEnabled;
    gBattleRollbackEnabled = enabled;

    if (previous != enabled) {
        if (!enabled && gBattleRollbackBaseStage > 0 &&
            gBattleRollbackBaseSector > 0 &&
            gBattleRollbackTargetStage > 0 &&
            gBattleRollbackTargetSector > 0 &&
            (gBattleRollbackTargetStage != gBattleRollbackBaseStage ||
             gBattleRollbackTargetSector != gBattleRollbackBaseSector)) {
            // Do not mutate the active battle.  Restore the recorded latest
            // stage immediately before the next ordinary battle request.
            gBattleRollbackRestorePending = true;
        } else if (!enabled) {
            gBattleRollbackBaseStage = -1;
            gBattleRollbackBaseSector = -1;
            gBattleRollbackTargetStage = -1;
            gBattleRollbackTargetSector = -1;
            gBattleRollbackRestorePending = false;
        } else {
            // Re-enabling before a pending restore resumes the current
            // rollback target.  A fresh session has no target until a defeat.
            gBattleRollbackRestorePending = false;
        }
    }

    if (persist) {
        [NSUserDefaults.standardUserDefaults setBool:enabled
                                              forKey:PCBattleRollbackDefaultsKey];
    }
    if (previous != enabled || persist) {
        NSLog(@"#pc  PluginSetting.BattleRollback enabled=%d persisted=%d "
               "base=%d/%d target=%d/%d restorePending=%d",
              enabled ? 1 : 0, persist ? 1 : 0,
              gBattleRollbackBaseStage, gBattleRollbackBaseSector,
              gBattleRollbackTargetStage, gBattleRollbackTargetSector,
              gBattleRollbackRestorePending ? 1 : 0);
    }
}

@interface PCPluginOverlay : NSObject
@property(nonatomic, strong) UIButton *floatingButton;
@property(nonatomic, strong) UIView *panel;
@property(nonatomic, strong) UISwitch *skipTouchSwitch;
@property(nonatomic, strong) UISwitch *battleRollbackSwitch;
@property(nonatomic, strong) UILabel *loginInfoHeader;
@property(nonatomic, strong) UITextView *loginInfoView;
@property(nonatomic, weak) UIWindow *hostWindow;
@property(nonatomic, assign) BOOL floatingButtonWasDragged;
@property(nonatomic, assign) BOOL floatingExpandedOnTouch;
@property(nonatomic, assign) BOOL floatingCollapsed;
@property(nonatomic, assign) BOOL floatingPreferLeft;
@property(nonatomic, assign) NSUInteger floatingDockGeneration;
+ (instancetype)sharedOverlay;
- (void)installWhenReady;
- (void)refreshUI;
@end

@implementation PCPluginOverlay

+ (instancetype)sharedOverlay {
    static PCPluginOverlay *overlay;
    static dispatch_once_t onceToken;
    dispatch_once(&onceToken, ^{
        overlay = [PCPluginOverlay new];
    });
    return overlay;
}

- (UIWindow *)activeGameWindow {
    UIApplication *application = UIApplication.sharedApplication;
    UIWindow *fallback = nil;
    if (@available(iOS 13.0, *)) {
        for (UIScene *scene in application.connectedScenes) {
            if (![scene isKindOfClass:UIWindowScene.class]) continue;
            if (scene.activationState != UISceneActivationStateForegroundActive &&
                scene.activationState != UISceneActivationStateForegroundInactive) {
                continue;
            }
            for (UIWindow *window in ((UIWindowScene *)scene).windows) {
                if (window.hidden || window.alpha <= 0.0) continue;
                if (window.isKeyWindow) return window;
                if (!fallback) fallback = window;
            }
        }
    }
    return fallback;
}

- (void)installWhenReady {
    NSAssert(NSThread.isMainThread, @"PCPluginOverlay must be installed on main thread");
    UIWindow *window = [self activeGameWindow];
    if (!window) {
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 500 * NSEC_PER_MSEC),
                       dispatch_get_main_queue(), ^{
            [self installWhenReady];
        });
        return;
    }

    if (self.hostWindow == window && self.floatingButton.superview == window) {
        [self positionPanel];
        [self refreshUI];
        // Re-clamp against the latest bounds (rotation / safe-area changes)
        // while preserving the remembered edge + Y ratio.
        [self restoreFloatingButtonPositionAnimated:NO
                                          collapsed:self.floatingCollapsed];
        [window bringSubviewToFront:self.panel];
        [window bringSubviewToFront:self.floatingButton];
        if (self.panel.hidden && !self.floatingCollapsed) {
            [self scheduleFloatingCollapse];
        }
        return;
    }

    [self.panel removeFromSuperview];
    [self.floatingButton removeFromSuperview];
    self.hostWindow = window;

    UIView *panel = [[UIView alloc] initWithFrame:CGRectMake(0, 0, 300, 330)];
    panel.backgroundColor = [UIColor colorWithWhite:0.08 alpha:0.94];
    panel.layer.cornerRadius = 16.0;
    panel.layer.borderWidth = 1.0;
    panel.layer.borderColor = [UIColor colorWithWhite:1.0 alpha:0.18].CGColor;
    panel.clipsToBounds = YES;
    panel.hidden = YES;
    panel.accessibilityIdentifier = @"PCJBProbe.pluginPanel";

    UILabel *title = [[UILabel alloc] initWithFrame:CGRectMake(18, 14, 210, 25)];
    title.text = @"插件面板";
    title.textColor = UIColor.whiteColor;
    title.font = [UIFont boldSystemFontOfSize:18.0];
    [panel addSubview:title];

    UIButton *close = [UIButton buttonWithType:UIButtonTypeSystem];
    close.frame = CGRectMake(252, 8, 40, 40);
    [close setTitle:@"×" forState:UIControlStateNormal];
    [close setTitleColor:[UIColor colorWithWhite:0.85 alpha:1.0]
                forState:UIControlStateNormal];
    close.titleLabel.font = [UIFont systemFontOfSize:28.0
                                             weight:UIFontWeightLight];
    close.accessibilityLabel = @"关闭插件面板";
    [close addTarget:self action:@selector(togglePanel)
       forControlEvents:UIControlEventTouchUpInside];
    [panel addSubview:close];

    UILabel *skipLabel = [[UILabel alloc] initWithFrame:CGRectMake(18, 56, 210, 28)];
    skipLabel.text = @"跳过 Touch to Start";
    skipLabel.textColor = UIColor.whiteColor;
    skipLabel.font = [UIFont systemFontOfSize:16.0 weight:UIFontWeightSemibold];
    [panel addSubview:skipLabel];

    UILabel *skipDetail = [[UILabel alloc] initWithFrame:CGRectMake(18, 86, 230, 22)];
    skipDetail.text = @"启动后自动进入游戏";
    skipDetail.textColor = [UIColor colorWithWhite:0.68 alpha:1.0];
    skipDetail.font = [UIFont systemFontOfSize:13.0];
    [panel addSubview:skipDetail];

    UISwitch *skipSwitch = [[UISwitch alloc] initWithFrame:CGRectZero];
    skipSwitch.onTintColor = [UIColor colorWithRed:0.19 green:0.68 blue:1.0 alpha:1.0];
    skipSwitch.accessibilityLabel = @"跳过 Touch to Start";
    skipSwitch.accessibilityIdentifier = @"PCJBProbe.skipTouchToStartSwitch";
    [skipSwitch addTarget:self action:@selector(skipTouchSwitchChanged:)
         forControlEvents:UIControlEventValueChanged];
    [panel addSubview:skipSwitch];
    self.skipTouchSwitch = skipSwitch;

    UILabel *loginInfoHeader =
        [[UILabel alloc] initWithFrame:CGRectMake(18, 118, 264, 22)];
    loginInfoHeader.text = @"登录信息（点击下方内容复制）";
    loginInfoHeader.textColor = UIColor.whiteColor;
    loginInfoHeader.font =
        [UIFont systemFontOfSize:14.0 weight:UIFontWeightSemibold];
    loginInfoHeader.accessibilityIdentifier = @"PCJBProbe.loginInfoHeader";
    [panel addSubview:loginInfoHeader];
    self.loginInfoHeader = loginInfoHeader;

    UITextView *loginInfoView =
        [[UITextView alloc] initWithFrame:CGRectMake(14, 144, 272, 170)];
    loginInfoView.backgroundColor = [UIColor colorWithWhite:0.02 alpha:0.75];
    loginInfoView.textColor = [UIColor colorWithWhite:0.82 alpha:1.0];
    if (@available(iOS 13.0, *)) {
        loginInfoView.font = [UIFont monospacedSystemFontOfSize:10.0
                                                       weight:UIFontWeightRegular];
    } else {
        loginInfoView.font = [UIFont fontWithName:@"Menlo" size:10.0];
    }
    loginInfoView.editable = NO;
    loginInfoView.selectable = NO;
    loginInfoView.scrollEnabled = YES;
    loginInfoView.alwaysBounceVertical = YES;
    loginInfoView.layer.cornerRadius = 9.0;
    loginInfoView.layer.borderWidth = 1.0;
    loginInfoView.layer.borderColor =
        [UIColor colorWithWhite:1.0 alpha:0.12].CGColor;
    loginInfoView.textContainerInset = UIEdgeInsetsMake(8, 7, 8, 7);
    loginInfoView.accessibilityLabel = @"登录信息，点击复制";
    loginInfoView.accessibilityIdentifier = @"PCJBProbe.loginInfo";
    UITapGestureRecognizer *copyLoginTap = [[UITapGestureRecognizer alloc]
        initWithTarget:self action:@selector(copyLoginInfo:)];
    copyLoginTap.cancelsTouchesInView = NO;
    [loginInfoView addGestureRecognizer:copyLoginTap];
    [panel addSubview:loginInfoView];
    self.loginInfoView = loginInfoView;

    // Battle rollback / stage override is temporarily disabled.  Keep the
    // panel code here for a later re-enable, but do not expose an inactive
    // switch to the user.
#if 0
    UILabel *rollbackLabel =
        [[UILabel alloc] initWithFrame:CGRectMake(18, 116, 210, 28)];
    rollbackLabel.text = @"失败时回退";
    rollbackLabel.textColor = UIColor.whiteColor;
    rollbackLabel.font =
        [UIFont systemFontOfSize:16.0 weight:UIFontWeightSemibold];
    [panel addSubview:rollbackLabel];

    UILabel *rollbackDetail =
        [[UILabel alloc] initWithFrame:CGRectMake(18, 146, 230, 22)];
    rollbackDetail.text = @"失败逐关回退，胜利停留当前关";
    rollbackDetail.textColor = [UIColor colorWithWhite:0.68 alpha:1.0];
    rollbackDetail.font = [UIFont systemFontOfSize:13.0];
    [panel addSubview:rollbackDetail];

    UISwitch *rollbackSwitch = [[UISwitch alloc] initWithFrame:CGRectZero];
    rollbackSwitch.onTintColor =
        [UIColor colorWithRed:0.19 green:0.68 blue:1.0 alpha:1.0];
    rollbackSwitch.accessibilityLabel = @"失败时回退";
    rollbackSwitch.accessibilityIdentifier =
        @"PCJBProbe.battleRollbackSwitch";
    [rollbackSwitch addTarget:self
                       action:@selector(battleRollbackSwitchChanged:)
             forControlEvents:UIControlEventValueChanged];
    [panel addSubview:rollbackSwitch];
    self.battleRollbackSwitch = rollbackSwitch;
#endif

    UIButton *button = [UIButton buttonWithType:UIButtonTypeCustom];
    button.frame = CGRectMake(0, 0, kPCFloatingButtonSize, kPCFloatingButtonSize);
    button.backgroundColor = [UIColor colorWithWhite:0.06 alpha:0.88];
    button.layer.cornerRadius = kPCFloatingButtonSize * 0.5;
    button.layer.borderWidth = 1.0;
    button.layer.borderColor =
        [UIColor colorWithRed:0.19 green:0.68 blue:1.0 alpha:0.9].CGColor;
    button.layer.shadowColor = UIColor.blackColor.CGColor;
    button.layer.shadowOpacity = 0.28;
    button.layer.shadowRadius = 4.0;
    button.layer.shadowOffset = CGSizeMake(0.0, 1.5);
    [button setTitle:@"插件" forState:UIControlStateNormal];
    [button setTitleColor:UIColor.whiteColor forState:UIControlStateNormal];
    button.titleLabel.font = [UIFont boldSystemFontOfSize:11.0];
    button.accessibilityLabel = @"打开插件面板";
    button.accessibilityIdentifier = @"PCJBProbe.floatingButton";
    [button addTarget:self action:@selector(floatingButtonTouchDown:)
       forControlEvents:UIControlEventTouchDown];
    [button addTarget:self action:@selector(floatingButtonTapped:)
       forControlEvents:UIControlEventTouchUpInside];

    UIPanGestureRecognizer *pan = [[UIPanGestureRecognizer alloc]
        initWithTarget:self action:@selector(dragFloatingButton:)];
    // Once UIKit recognizes a drag, cancel UIButton's pending TouchUpInside.
    // The explicit dragged flag below is a second guard for recognizer/control
    // delivery-order differences across iOS versions.
    pan.cancelsTouchesInView = YES;
    [button addGestureRecognizer:pan];

    self.panel = panel;
    self.floatingButton = button;
    [window addSubview:panel];
    [window addSubview:button];

    self.floatingCollapsed = NO;
    self.floatingExpandedOnTouch = NO;
    [self restoreFloatingButtonPositionAnimated:NO collapsed:NO];
    [self positionPanel];
    [self refreshUI];
    [self scheduleFloatingCollapse];
    NSLog(@"#pc  PluginOverlay installed window=%p bounds=%@ edgeLeft=%d",
          window, NSStringFromCGRect(window.bounds), self.floatingPreferLeft ? 1 : 0);
}

- (BOOL)isPluginPanelVisible {
    return self.panel && !self.panel.hidden;
}

- (CGRect)floatingDragBoundsCollapsed:(BOOL)collapsed {
    UIWindow *window = self.hostWindow;
    if (!window) return CGRectZero;
    UIEdgeInsets safe = window.safeAreaInsets;
    CGFloat half = kPCFloatingButtonSize * 0.5;
    CGFloat minY = safe.top + half + 6.0;
    CGFloat maxY = CGRectGetHeight(window.bounds) - safe.bottom - half - 6.0;
    if (maxY < minY) {
        minY = half + 6.0;
        maxY = CGRectGetHeight(window.bounds) - half - 6.0;
    }
    CGFloat minX;
    CGFloat maxX;
    if (collapsed) {
        // Leave a thin strip on-screen so the control stays tappable.
        CGFloat visible = kPCFloatingButtonSize * kPCFloatingCollapsedVisible;
        minX = visible - half;
        maxX = CGRectGetWidth(window.bounds) - (visible - half);
    } else {
        minX = safe.left + half + 6.0;
        maxX = CGRectGetWidth(window.bounds) - safe.right - half - 6.0;
    }
    if (maxX < minX) {
        minX = half;
        maxX = CGRectGetWidth(window.bounds) - half;
    }
    return CGRectMake(minX, minY, maxX - minX, maxY - minY);
}

- (CGPoint)clampFloatingCenter:(CGPoint)center collapsed:(BOOL)collapsed {
    CGRect box = [self floatingDragBoundsCollapsed:collapsed];
    center.x = MIN(MAX(center.x, CGRectGetMinX(box)), CGRectGetMaxX(box));
    center.y = MIN(MAX(center.y, CGRectGetMinY(box)), CGRectGetMaxY(box));
    return center;
}

- (CGPoint)edgeCenterPreferLeft:(BOOL)preferLeft
                              y:(CGFloat)y
                      collapsed:(BOOL)collapsed {
    CGRect box = [self floatingDragBoundsCollapsed:collapsed];
    CGPoint center;
    center.x = preferLeft ? CGRectGetMinX(box) : CGRectGetMaxX(box);
    center.y = y;
    return [self clampFloatingCenter:center collapsed:collapsed];
}

- (void)saveFloatingButtonPosition {
    UIButton *button = self.floatingButton;
    UIWindow *window = self.hostWindow;
    if (!button || !window) return;
    CGRect box = [self floatingDragBoundsCollapsed:NO];
    CGFloat span = MAX(1.0, CGRectGetHeight(box));
    CGFloat yRatio = (button.center.y - CGRectGetMinY(box)) / span;
    yRatio = MIN(MAX(yRatio, 0.0), 1.0);
    BOOL preferLeft = button.center.x < CGRectGetMidX(window.bounds);
    self.floatingPreferLeft = preferLeft;
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    [defaults setBool:preferLeft forKey:PCFloatingEdgeLeftKey];
    [defaults setDouble:yRatio forKey:PCFloatingYRatioKey];
    [defaults synchronize];
}

- (void)restoreFloatingButtonPositionAnimated:(BOOL)animated
                                    collapsed:(BOOL)collapsed {
    UIButton *button = self.floatingButton;
    UIWindow *window = self.hostWindow;
    if (!button || !window) return;

    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    BOOL hasSaved = [defaults objectForKey:PCFloatingYRatioKey] != nil;
    BOOL preferLeft = hasSaved ? [defaults boolForKey:PCFloatingEdgeLeftKey] : NO;
    CGFloat yRatio = hasSaved ? [defaults doubleForKey:PCFloatingYRatioKey] : 0.18;
    yRatio = MIN(MAX(yRatio, 0.0), 1.0);
    self.floatingPreferLeft = preferLeft;

    CGRect box = [self floatingDragBoundsCollapsed:NO];
    CGFloat y = CGRectGetMinY(box) + yRatio * MAX(1.0, CGRectGetHeight(box));
    CGPoint center = [self edgeCenterPreferLeft:preferLeft y:y collapsed:collapsed];
    self.floatingCollapsed = collapsed;
    void (^apply)(void) = ^{
        button.center = center;
        button.alpha = collapsed ? kPCFloatingCollapsedAlpha : 1.0;
    };
    if (animated) {
        [UIView animateWithDuration:0.22
                              delay:0
                            options:UIViewAnimationOptionCurveEaseInOut
                         animations:apply
                         completion:nil];
    } else {
        apply();
    }
}

- (void)cancelFloatingCollapse {
    self.floatingDockGeneration += 1;
}

- (void)scheduleFloatingCollapse {
    if (!self.floatingButton || [self isPluginPanelVisible]) {
        [self cancelFloatingCollapse];
        return;
    }
    NSUInteger generation = ++self.floatingDockGeneration;
    __weak typeof(self) weakSelf = self;
    dispatch_after(
        dispatch_time(DISPATCH_TIME_NOW,
                      (int64_t)(kPCFloatingCollapseDelay * NSEC_PER_SEC)),
        dispatch_get_main_queue(), ^{
            __strong typeof(weakSelf) self = weakSelf;
            if (!self) return;
            if (generation != self.floatingDockGeneration) return;
            if ([self isPluginPanelVisible]) return;
            [self setFloatingCollapsed:YES animated:YES];
        });
}

- (void)setFloatingCollapsed:(BOOL)collapsed animated:(BOOL)animated {
    UIButton *button = self.floatingButton;
    UIWindow *window = self.hostWindow;
    if (!button || !window) return;
    if (self.floatingCollapsed == collapsed && !animated) {
        // still refresh edge position after rotation / reattach
    }
    BOOL preferLeft = self.floatingPreferLeft;
    // If currently free-floating mid-screen, recompute preferred side.
    if (!collapsed || !self.floatingCollapsed) {
        preferLeft = button.center.x < CGRectGetMidX(window.bounds);
        self.floatingPreferLeft = preferLeft;
    }
    CGPoint center = [self edgeCenterPreferLeft:preferLeft
                                              y:button.center.y
                                      collapsed:collapsed];
    self.floatingCollapsed = collapsed;
    void (^apply)(void) = ^{
        button.center = center;
        button.alpha = collapsed ? kPCFloatingCollapsedAlpha : 1.0;
    };
    if (animated) {
        [UIView animateWithDuration:0.22
                              delay:0
                            options:UIViewAnimationOptionCurveEaseInOut
                         animations:apply
                         completion:nil];
    } else {
        apply();
    }
    if (!collapsed) {
        [self saveFloatingButtonPosition];
    }
}

- (void)snapFloatingButtonToEdgeAndSave {
    UIButton *button = self.floatingButton;
    UIWindow *window = self.hostWindow;
    if (!button || !window) return;
    BOOL preferLeft = button.center.x < CGRectGetMidX(window.bounds);
    self.floatingPreferLeft = preferLeft;
    self.floatingCollapsed = NO;
    CGPoint center = [self edgeCenterPreferLeft:preferLeft
                                              y:button.center.y
                                      collapsed:NO];
    [UIView animateWithDuration:0.18
                          delay:0
                        options:UIViewAnimationOptionCurveEaseOut
                     animations:^{
        button.center = center;
        button.alpha = 1.0;
    } completion:^(__unused BOOL finished) {
        [self saveFloatingButtonPosition];
        [self scheduleFloatingCollapse];
    }];
}

- (void)positionPanel {
    UIWindow *window = self.hostWindow;
    if (!window || !self.panel) return;
    UIEdgeInsets safe = window.safeAreaInsets;
    CGFloat availableWidth = CGRectGetWidth(window.bounds) - safe.left - safe.right;
    CGFloat panelWidth = MIN(300.0, MAX(240.0, availableWidth - 24.0));
    CGFloat x = safe.left + (availableWidth - panelWidth) * 0.5;
    CGFloat y = safe.top + 74.0;
    CGFloat availableHeight = CGRectGetHeight(window.bounds) - safe.bottom - y - 12.0;
    CGFloat panelHeight = MIN(330.0, MAX(238.0, availableHeight));
    self.panel.frame = CGRectMake(x, y, panelWidth, panelHeight);

    self.skipTouchSwitch.center =
        CGPointMake(panelWidth - 46.0, 72.0);
    self.loginInfoHeader.frame = CGRectMake(18.0, 118.0,
                                            panelWidth - 36.0, 22.0);
    self.loginInfoView.frame = CGRectMake(14.0, 144.0,
                                          panelWidth - 28.0,
                                          MAX(78.0, panelHeight - 160.0));
#if 0
    self.battleRollbackSwitch.center =
        CGPointMake(panelWidth - 46.0, 132.0);
#endif
}

- (void)refreshUI {
    self.skipTouchSwitch.on = PCSkipTouchToStartEnabled();
    bool loginInfoReady = false;
    NSString *loginJSON = PCLoginInfoJSON(&loginInfoReady);
    self.loginInfoView.text = loginInfoReady
        ? loginJSON
        : @"等待游戏完成登录认证…\n\n认证完成后会显示 chlz/main.py 所需的账号配置。";
    self.loginInfoView.accessibilityValue =
        loginInfoReady ? @"已获取，点击复制" : @"尚未获取";
#if 0
    self.battleRollbackSwitch.on = PCBattleRollbackEnabled();
    self.floatingButton.accessibilityValue = [NSString
        stringWithFormat:@"跳过启动%@，失败回退%@",
                         PCSkipTouchToStartEnabled() ? @"已开启" : @"已关闭",
                         PCBattleRollbackEnabled() ? @"已开启" : @"已关闭"];
#else
    self.floatingButton.accessibilityValue = [NSString
        stringWithFormat:@"跳过启动%@",
                         PCSkipTouchToStartEnabled() ? @"已开启" : @"已关闭"];
#endif
}

- (void)copyLoginInfo:(UITapGestureRecognizer *)recognizer {
    if (recognizer.state != UIGestureRecognizerStateEnded) return;
    bool ready = false;
    NSString *loginJSON = PCLoginInfoJSON(&ready);
    if (!ready) {
        self.loginInfoHeader.text = @"登录信息尚未获取";
    } else {
        UIPasteboard.generalPasteboard.string = loginJSON;
        self.loginInfoHeader.text = @"登录信息已复制";
        NSLog(@"#pc  PluginOverlay login info copied bytes=%lu",
              (unsigned long)[loginJSON lengthOfBytesUsingEncoding:NSUTF8StringEncoding]);
    }
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 1200 * NSEC_PER_MSEC),
                   dispatch_get_main_queue(), ^{
        self.loginInfoHeader.text = @"登录信息（点击下方内容复制）";
    });
}

- (void)togglePanel {
    BOOL willShow = self.panel.hidden;
    self.panel.hidden = !willShow;
    if (willShow) {
        [self cancelFloatingCollapse];
        [self setFloatingCollapsed:NO animated:YES];
        [self positionPanel];
        [self refreshUI];
        [self.hostWindow bringSubviewToFront:self.panel];
        [self.hostWindow bringSubviewToFront:self.floatingButton];
    } else {
        [self scheduleFloatingCollapse];
    }
}

- (void)skipTouchSwitchChanged:(UISwitch *)sender {
    PCSetSkipTouchToStartEnabled(sender.isOn, true);
    [self refreshUI];
}

#if 0
- (void)battleRollbackSwitchChanged:(UISwitch *)sender {
    PCSetBattleRollbackEnabled(sender.isOn, true);
    [self refreshUI];
}
#endif

- (void)floatingButtonTouchDown:(UIButton *)sender {
    self.floatingButtonWasDragged = NO;
    [self cancelFloatingCollapse];
    if (self.floatingCollapsed) {
        // AssistiveTouch-style: first interaction only peeks the button out.
        self.floatingExpandedOnTouch = YES;
        [self setFloatingCollapsed:NO animated:YES];
    } else {
        self.floatingExpandedOnTouch = NO;
        sender.alpha = 1.0;
    }
}

- (void)floatingButtonTapped:(UIButton *)sender {
    if (self.floatingButtonWasDragged) {
        self.floatingButtonWasDragged = NO;
        self.floatingExpandedOnTouch = NO;
        NSLog(@"#pc  PluginOverlay tap suppressed reason=drag");
        [self scheduleFloatingCollapse];
        return;
    }
    if (self.floatingExpandedOnTouch) {
        self.floatingExpandedOnTouch = NO;
        NSLog(@"#pc  PluginOverlay tap suppressed reason=expand-from-collapse");
        [self scheduleFloatingCollapse];
        return;
    }
    [self togglePanel];
}

- (void)dragFloatingButton:(UIPanGestureRecognizer *)recognizer {
    UIView *button = recognizer.view;
    UIWindow *window = self.hostWindow;
    if (!button || !window) return;

    if (recognizer.state == UIGestureRecognizerStateBegan) {
        self.floatingButtonWasDragged = YES;
        self.floatingExpandedOnTouch = NO;
        [self cancelFloatingCollapse];
        if (self.floatingCollapsed) {
            [self setFloatingCollapsed:NO animated:NO];
        }
        button.alpha = 1.0;
    } else if (recognizer.state == UIGestureRecognizerStateChanged) {
        self.floatingButtonWasDragged = YES;
    }

    CGPoint translation = [recognizer translationInView:window];
    CGPoint center = button.center;
    center.x += translation.x;
    center.y += translation.y;
    [recognizer setTranslation:CGPointZero inView:window];
    button.center = [self clampFloatingCenter:center collapsed:NO];
    button.alpha = 1.0;
    self.floatingCollapsed = NO;

    if (recognizer.state == UIGestureRecognizerStateEnded ||
        recognizer.state == UIGestureRecognizerStateCancelled) {
        NSLog(@"#pc  PluginOverlay drag ended center=%@",
              NSStringFromCGPoint(button.center));
        [self snapFloatingButtonToEdgeAndSave];
    }
}

@end

static void PCRefreshLoginInfoUI(void) {
    dispatch_async(dispatch_get_main_queue(), ^{
        [[PCPluginOverlay sharedOverlay] refreshUI];
    });
}

// tp.PS_Auth.SetPaketData(PS_Auth packet, string uid)
// PSBase.reqData is +0x10; the RequestData field offsets below come from the
// matching 1.0.3 IL2CPP dump in 103/Cpp2IL/DiffableCs.
static void (*orig_authSetPaketData)(void *, void *, const void *);
static void pc_authSetPaketData(void *packet, void *uid, const void *method) {
    orig_authSetPaketData(packet, uid, method);
    if (!PCReadableRange(packet, 0x18)) return;
    void *request = *(void **)((uint8_t *)packet + 0x10);
    if (!PCReadableRange(request, 0x78)) return;

    NSString *version = PCSafeStringFromIl2Cpp(
        *(void **)((uint8_t *)request + 0x10));
    NSString *dataNo = PCSafeStringFromIl2Cpp(
        *(void **)((uint8_t *)request + 0x18));
    NSString *clientID = PCSafeStringFromIl2Cpp(
        *(void **)((uint8_t *)request + 0x30));
    NSString *deviceModel = PCSafeStringFromIl2Cpp(
        *(void **)((uint8_t *)request + 0x40));
    NSString *deviceID = PCSafeStringFromIl2Cpp(
        *(void **)((uint8_t *)request + 0x48));
    NSString *platformUserID = PCSafeStringFromIl2Cpp(
        *(void **)((uint8_t *)request + 0x50));
    NSString *pushToken = PCSafeStringFromIl2Cpp(
        *(void **)((uint8_t *)request + 0x58));
    NSString *operatingSystem = PCSafeStringFromIl2Cpp(
        *(void **)((uint8_t *)request + 0x68));
    NSString *adID = PCSafeStringFromIl2Cpp(
        *(void **)((uint8_t *)request + 0x70));

    pthread_mutex_lock(&gLoginInfoLock);
    gLoginVersion = [version copy];
    gLoginDataNo = [dataNo copy];
    gLoginRegionType = *(int32_t *)((uint8_t *)request + 0x28);
    gLoginClientID = [clientID copy];
    gLoginIsGuest = *(bool *)((uint8_t *)request + 0x38);
    gLoginCountry = *(int32_t *)((uint8_t *)request + 0x3C);
    gLoginDeviceModel = [deviceModel copy];
    gLoginDeviceID = [deviceID copy];
    gLoginPlatformUserID = [platformUserID copy];
    gLoginPushToken = [pushToken copy];
    gLoginStoreRegionCode = *(int32_t *)((uint8_t *)request + 0x60);
    gLoginOperatingSystem = [operatingSystem copy];
    gLoginAdID = [adID copy];
    gLoginAuthRequestCaptured = true;
    bool ready = gLoginClientID.length > 0 && gLoginDeviceID.length > 0 &&
                 gLoginPlatformUserID.length > 0;
    pthread_mutex_unlock(&gLoginInfoLock);

    NSLog(@"#pc  LoginInfo auth request captured ready=%d", ready ? 1 : 0);
    PCRefreshLoginInfoUI();
}

// tp.PS_Auth.ResponseData.Response(): the server may normalize/replace the
// client id, so prefer the response value when it is present.
static void (*orig_authResponse)(void *, const void *);
static void pc_authResponse(void *response, const void *method) {
    if (PCReadableRange(response, 0x50)) {
        NSString *clientID = PCSafeStringFromIl2Cpp(
            *(void **)((uint8_t *)response + 0x48));
        if (clientID.length > 0) {
            pthread_mutex_lock(&gLoginInfoLock);
            gLoginClientID = [clientID copy];
            pthread_mutex_unlock(&gLoginInfoLock);
            NSLog(@"#pc  LoginInfo auth response captured client_id=updated");
            PCRefreshLoginInfoUI();
        }
    }
    orig_authResponse(response, method);
}

// tp.LoginResponseData.Response(): selected character server used by
// chlz GameSession.login().
static void (*orig_loginResponse)(void *, const void *);
static void pc_loginResponse(void *response, const void *method) {
    if (PCReadableRange(response, 0x4C)) {
        int32_t serverNum = *(int32_t *)((uint8_t *)response + 0x48);
        if (serverNum > 0 && serverNum < 10000) {
            pthread_mutex_lock(&gLoginInfoLock);
            gLoginServerNum = serverNum;
            pthread_mutex_unlock(&gLoginInfoLock);
            NSLog(@"#pc  LoginInfo login response captured server_num=%d",
                  serverNum);
            PCRefreshLoginInfoUI();
        }
    }
    orig_loginResponse(response, method);
}

static void PCStartPluginOverlay(void) {
    dispatch_async(dispatch_get_main_queue(), ^{
        PCPluginOverlay *overlay = PCPluginOverlay.sharedOverlay;
        [NSNotificationCenter.defaultCenter
            addObserverForName:UIApplicationDidBecomeActiveNotification
                        object:nil
                         queue:NSOperationQueue.mainQueue
                    usingBlock:^(__unused NSNotification *notification) {
            [overlay installWhenReady];
        }];
        [overlay installWhenReady];
    });
}

static bool PCContainsInsensitive(const char *value, const char *needle) {
    if (!value || !needle) return false;
    size_t valueLength = strlen(value);
    size_t needleLength = strlen(needle);
    if (needleLength == 0 || valueLength < needleLength) return false;
    for (size_t i = 0; i + needleLength <= valueLength; i++) {
        if (strncasecmp(value + i, needle, needleLength) == 0) return true;
    }
    return false;
}

static bool PCSuspiciousPath(const char *path) {
    if (!path) return false;
    static const char *needles[] = {
        "cydia", "substrate", "substitute", "ellekit", "libhooker",
        "tweakinject", "systemhook", "frida", "jailbreak", "sileo",
        "zebra", "/bin/bash", "/usr/sbin/sshd", "/etc/apt", "/var/jb",
        "/private/preboot", "shadow", "choicy"
    };
    for (size_t i = 0; i < sizeof(needles) / sizeof(needles[0]); i++) {
        if (PCContainsInsensitive(path, needles[i])) return true;
    }
    return false;
}

static void PCLogStack(NSString *reason) {
    if (gInLog) return;
    gInLog = true;
    NSLog(@"#pc  %@ stack=%@", reason, [NSThread callStackSymbols]);
    gInLog = false;
}

static void PCHook(void *target, void *replacement, void **original, const char *name) {
    if (!target || !gHookFunction) {
        NSLog(@"#pc  hook failed name=%s target=%p hooker=%p", name, target, gHookFunction);
        return;
    }
    gHookFunction(target, replacement, original);
    NSLog(@"#pc  hook installed name=%s target=%p original=%p", name, target,
          original ? *original : nullptr);
}

static void PCPatchInstruction(intptr_t slide, uintptr_t offset, uint32_t expected,
                               uint32_t replacement, const char *name) {
    uint32_t *target = (uint32_t *)(slide + offset);
    uint32_t before = 0;
    memcpy(&before, target, sizeof(before));
    if (before != expected) {
        NSLog(@"#pc  patch mismatch name=%s target=%p expected=0x%08x actual=0x%08x",
              name, target, expected, before);
        return;
    }
    if (!gHookMemory) {
        NSLog(@"#pc  patch failed name=%s target=%p MSHookMemory missing", name, target);
        return;
    }
    gHookMemory(target, &replacement, sizeof(replacement));
    uint32_t after = 0;
    memcpy(&after, target, sizeof(after));
    NSLog(@"#pc  patch installed name=%s target=%p before=0x%08x after=0x%08x",
          name, target, before, after);
}

// MARK: - libc / anti-debug / path probes

static int (*orig_access)(const char *, int);
static int pc_access(const char *path, int mode) {
    int result = orig_access(path, mode);
    if (!gInLog && PCSuspiciousPath(path)) {
        gInLog = true;
        NSLog(@"#pc  access path=%s mode=%d result=%d errno=%d", path, mode, result, errno);
        gInLog = false;
    }
    return result;
}

static int (*orig_stat)(const char *, struct stat *);
static int pc_stat(const char *path, struct stat *buffer) {
    int result = orig_stat(path, buffer);
    if (!gInLog && PCSuspiciousPath(path)) {
        gInLog = true;
        NSLog(@"#pc  stat path=%s result=%d errno=%d", path, result, errno);
        gInLog = false;
    }
    return result;
}

static int (*orig_lstat)(const char *, struct stat *);
static int pc_lstat(const char *path, struct stat *buffer) {
    int result = orig_lstat(path, buffer);
    if (!gInLog && PCSuspiciousPath(path)) {
        gInLog = true;
        NSLog(@"#pc  lstat path=%s result=%d errno=%d", path, result, errno);
        gInLog = false;
    }
    return result;
}

static FILE *(*orig_fopen)(const char *, const char *);
static FILE *pc_fopen(const char *path, const char *mode) {
    FILE *result = orig_fopen(path, mode);
    if (!gInLog && PCSuspiciousPath(path)) {
        gInLog = true;
        NSLog(@"#pc  fopen path=%s mode=%s result=%p errno=%d", path,
              mode ? mode : "(null)", result, errno);
        gInLog = false;
    }
    return result;
}

static int (*orig_open)(const char *, int, ...);
static int pc_open(const char *path, int flags, ...) {
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list args;
        va_start(args, flags);
        mode = (mode_t)va_arg(args, int);
        va_end(args);
    }
    int result = (flags & O_CREAT) ? orig_open(path, flags, mode) : orig_open(path, flags);
    if (!gInLog && PCSuspiciousPath(path)) {
        gInLog = true;
        NSLog(@"#pc  open path=%s flags=0x%x result=%d errno=%d", path, flags, result, errno);
        gInLog = false;
    }
    return result;
}

static void *(*orig_dlopen)(const char *, int);
static void *pc_dlopen(const char *path, int mode) {
    void *result = orig_dlopen(path, mode);
    if (!gInLog && path && PCSuspiciousPath(path)) {
        gInLog = true;
        NSLog(@"#pc  dlopen path=%s mode=0x%x result=%p", path, mode, result);
        gInLog = false;
    }
    return result;
}

static void *(*orig_dlsym)(void *, const char *);
static void *pc_dlsym(void *handle, const char *symbol) {
    void *result = orig_dlsym(handle, symbol);
    if (!gInLog && symbol &&
        (PCContainsInsensitive(symbol, "ptrace") ||
         PCContainsInsensitive(symbol, "sysctl") ||
         PCContainsInsensitive(symbol, "jail") ||
         PCContainsInsensitive(symbol, "fork") ||
         PCContainsInsensitive(symbol, "dyld"))) {
        gInLog = true;
        NSLog(@"#pc  dlsym symbol=%s handle=%p result=%p", symbol, handle, result);
        gInLog = false;
    }
    return result;
}

static char *(*orig_getenv)(const char *);
static char *pc_getenv(const char *name) {
    char *result = orig_getenv(name);
    if (!gInLog && name &&
        (PCContainsInsensitive(name, "dyld") ||
         PCContainsInsensitive(name, "inject") ||
         PCContainsInsensitive(name, "jail") ||
         PCContainsInsensitive(name, "frida"))) {
        gInLog = true;
        NSLog(@"#pc  getenv name=%s value=%s", name, result ? result : "(null)");
        gInLog = false;
    }
    return result;
}

static int (*orig_sysctl)(int *, u_int, void *, size_t *, void *, size_t);
static int pc_sysctl(int *name, u_int count, void *oldp, size_t *oldlenp,
                     void *newp, size_t newlen) {
    int result = orig_sysctl(name, count, oldp, oldlenp, newp, newlen);
    if (!gInLog && name && count >= 2 && name[0] == CTL_KERN && name[1] == KERN_PROC) {
        gInLog = true;
        int selector = count >= 3 ? name[2] : -1;
        int pid = count >= 4 ? name[3] : -1;
        NSLog(@"#pc  sysctl KERN_PROC selector=%d pid=%d result=%d errno=%d", selector, pid, result, errno);
        gInLog = false;
    }
    return result;
}

static int (*orig_sysctlbyname)(const char *, void *, size_t *, void *, size_t);
static int pc_sysctlbyname(const char *name, void *oldp, size_t *oldlenp,
                           void *newp, size_t newlen) {
    int result = orig_sysctlbyname(name, oldp, oldlenp, newp, newlen);
    if (!gInLog && name &&
        (PCContainsInsensitive(name, "proc") || PCContainsInsensitive(name, "debug") ||
         PCContainsInsensitive(name, "native"))) {
        gInLog = true;
        NSLog(@"#pc  sysctlbyname name=%s result=%d errno=%d", name, result, errno);
        gInLog = false;
    }
    return result;
}

static int (*orig_ptrace)(int, pid_t, caddr_t, int);
static int pc_ptrace(int request, pid_t pid, caddr_t address, int data) {
    int result = orig_ptrace(request, pid, address, data);
    if (!gInLog) {
        gInLog = true;
        NSLog(@"#pc  ptrace request=%d pid=%d address=%p data=%d result=%d errno=%d",
              request, pid, address, data, result, errno);
        gInLog = false;
    }
    return result;
}

// MARK: - termination probes

typedef void (*PCCxaThrowFunction)(void *, std::type_info *, void (*)(void *));
static PCCxaThrowFunction orig_cxa_throw;

static void pc_cxa_throw(void *exceptionObject, std::type_info *typeInfo,
                         void (*destructor)(void *)) __attribute__((noreturn));
static void pc_cxa_throw(void *exceptionObject, std::type_info *typeInfo,
                         void (*destructor)(void *)) {
    if (!gInCxaThrowProbe) {
        gInCxaThrowProbe = true;
        const char *typeName = typeInfo ? typeInfo->name() : nullptr;
        if (typeName && strstr(typeName, "Il2CppExceptionWrapper")) {
            memset(&gLastManagedThrow, 0, sizeof(gLastManagedThrow));
            gLastManagedThrow.valid = true;
            gettimeofday(&gLastManagedThrow.capturedAt, nullptr);
            gLastManagedThrow.wrapper = exceptionObject;
            if (PCReadableRange(exceptionObject, sizeof(void *))) {
                gLastManagedThrow.managedException = *(void **)exceptionObject;
            }
            PCDescribeManagedException(
                gLastManagedThrow.managedException,
                gLastManagedThrow.exceptionClass,
                sizeof(gLastManagedThrow.exceptionClass),
                gLastManagedThrow.message, sizeof(gLastManagedThrow.message),
                gLastManagedThrow.managedStack,
                sizeof(gLastManagedThrow.managedStack));
            gLastManagedThrow.nativeFrameCount =
                backtrace(gLastManagedThrow.nativeFrames,
                          (int)(sizeof(gLastManagedThrow.nativeFrames) /
                                sizeof(gLastManagedThrow.nativeFrames[0])));
        }
        gInCxaThrowProbe = false;
    }
    orig_cxa_throw(exceptionObject, typeInfo, destructor);
    __builtin_unreachable();
}

static void PCAppendNativeFrames(const char *label, void *const *frames,
                                 int frameCount) {
    for (int index = 0; index < frameCount; index++) {
        Dl_info info = {};
        uintptr_t address = (uintptr_t)frames[index];
        const char *image = "(unknown)";
        const char *symbol = "(unknown)";
        uintptr_t offset = address;
        uintptr_t symbolOffset = 0;
        if (dladdr(frames[index], &info)) {
            if (info.dli_fname) {
                const char *slash = strrchr(info.dli_fname, '/');
                image = slash ? slash + 1 : info.dli_fname;
            }
            if (info.dli_sname) symbol = info.dli_sname;
            if (info.dli_fbase) offset = address - (uintptr_t)info.dli_fbase;
            if (info.dli_saddr) {
                symbolOffset = address - (uintptr_t)info.dli_saddr;
            }
        }
        char line[1024] = {};
        snprintf(line, sizeof(line),
                 "UnityCrash.%s_frame index=%d address=%p image=%s "
                 "offset=0x%lx symbol=%s symbol_offset=0x%lx",
                 label, index, frames[index], image, (unsigned long)offset,
                 symbol, (unsigned long)symbolOffset);
        PCAppendRawCrashLine(line, true);
    }
}

static void PCAppendLastManagedThrow(void) {
    if (!gLastManagedThrow.valid) {
        PCAppendRawCrashLine(
            "UnityCrash.LastManagedThrow available=0", true);
        return;
    }
    struct timeval now = {};
    gettimeofday(&now, nullptr);
    double age = (double)(now.tv_sec - gLastManagedThrow.capturedAt.tv_sec) +
                 (double)(now.tv_usec -
                          gLastManagedThrow.capturedAt.tv_usec) /
                     1000000.0;
    char report[6144] = {};
    snprintf(report, sizeof(report),
             "UnityCrash.LastManagedThrow available=1 age=%.3fs wrapper=%p "
             "exception=%p class=%s message=%s stack=%s",
             age, gLastManagedThrow.wrapper,
             gLastManagedThrow.managedException,
             gLastManagedThrow.exceptionClass,
             gLastManagedThrow.message,
             gLastManagedThrow.managedStack);
    PCAppendRawCrashLine(report, true);
    PCAppendNativeFrames("throw", gLastManagedThrow.nativeFrames,
                         gLastManagedThrow.nativeFrameCount);
}

static void PCAppendTerminationReport(const char *reason) {
    char header[512] = {};
    snprintf(header, sizeof(header),
             "UnityCrash.Termination reason=%s thread=%p",
             reason ?: "(unknown)", (void *)pthread_self());
    PCAppendRawCrashLine(header, true);
    PCAppendLastManagedThrow();
    void *frames[48] = {};
    int frameCount =
        backtrace(frames, (int)(sizeof(frames) / sizeof(frames[0])));
    PCAppendNativeFrames("termination", frames, frameCount);
    pthread_mutex_lock(&gPersistentLogLock);
    if (gPersistentLogFD >= 0) fsync(gPersistentLogFD);
    if (gUnityCrashHistoryFD >= 0) fsync(gUnityCrashHistoryFD);
    if (gUnityNativeLogFD >= 0) fsync(gUnityNativeLogFD);
    pthread_mutex_unlock(&gPersistentLogLock);
}

static void (*orig_exit)(int);
static void pc_exit(int status) {
    char reason[64] = {};
    snprintf(reason, sizeof(reason), "exit status=%d", status);
    PCAppendTerminationReport(reason);
    PCLogStack([NSString stringWithFormat:@"exit status=%d", status]);
    orig_exit(status);
    __builtin_unreachable();
}

static void (*orig__exit)(int);
static void pc__exit(int status) {
    char reason[64] = {};
    snprintf(reason, sizeof(reason), "_exit status=%d", status);
    PCAppendTerminationReport(reason);
    PCLogStack([NSString stringWithFormat:@"_exit status=%d", status]);
    orig__exit(status);
    __builtin_unreachable();
}

static void (*orig_abort)(void);
static void pc_abort(void) {
    PCAppendTerminationReport("abort");
    PCLogStack(@"abort");
    orig_abort();
    __builtin_unreachable();
}

static int (*orig_raise)(int);
static int pc_raise(int signalNumber) {
    if (signalNumber == SIGABRT || signalNumber == SIGBUS ||
        signalNumber == SIGFPE || signalNumber == SIGILL ||
        signalNumber == SIGSEGV || signalNumber == SIGTRAP) {
        char reason[64] = {};
        snprintf(reason, sizeof(reason), "raise signal=%d", signalNumber);
        PCAppendTerminationReport(reason);
    }
    PCLogStack([NSString stringWithFormat:@"raise signal=%d", signalNumber]);
    return orig_raise(signalNumber);
}

static int (*orig_kill)(pid_t, int);
static int pc_kill(pid_t pid, int signalNumber) {
    if (pid == getpid() &&
        (signalNumber == SIGABRT || signalNumber == SIGBUS ||
         signalNumber == SIGFPE || signalNumber == SIGILL ||
         signalNumber == SIGSEGV || signalNumber == SIGTRAP ||
         signalNumber == SIGKILL)) {
        char reason[80] = {};
        snprintf(reason, sizeof(reason), "kill pid=%d signal=%d", pid,
                 signalNumber);
        PCAppendTerminationReport(reason);
    }
    PCLogStack([NSString stringWithFormat:@"kill pid=%d signal=%d", pid, signalNumber]);
    return orig_kill(pid, signalNumber);
}

// MARK: - Objective-C probes

static BOOL (*orig_fileExistsAtPath)(id, SEL, NSString *);
static BOOL pc_fileExistsAtPath(id self, SEL command, NSString *path) {
    BOOL result = orig_fileExistsAtPath(self, command, path);
    if (!gInLog && PCSuspiciousPath(path.UTF8String)) {
        gInLog = true;
        NSLog(@"#pc  NSFileManager fileExistsAtPath=%@ result=%d", path, result);
        gInLog = false;
    }
    return result;
}

static BOOL (*orig_fileExistsAtPathIsDirectory)(id, SEL, NSString *, BOOL *);
static BOOL pc_fileExistsAtPathIsDirectory(id self, SEL command, NSString *path, BOOL *isDirectory) {
    BOOL result = orig_fileExistsAtPathIsDirectory(self, command, path, isDirectory);
    if (!gInLog && PCSuspiciousPath(path.UTF8String)) {
        gInLog = true;
        NSLog(@"#pc  NSFileManager fileExistsAtPath:isDirectory path=%@ result=%d dir=%d",
              path, result, isDirectory ? *isDirectory : -1);
        gInLog = false;
    }
    return result;
}

static BOOL (*orig_canOpenURL)(id, SEL, NSURL *);
static BOOL pc_canOpenURL(id self, SEL command, NSURL *url) {
    BOOL result = orig_canOpenURL(self, command, url);
    const char *value = url.absoluteString.UTF8String;
    if (!gInLog && value &&
        (PCContainsInsensitive(value, "cydia") || PCContainsInsensitive(value, "sileo") ||
         PCContainsInsensitive(value, "zbra") || PCContainsInsensitive(value, "filza"))) {
        gInLog = true;
        NSLog(@"#pc  UIApplication canOpenURL=%@ result=%d", url, result);
        gInLog = false;
    }
    return result;
}

static void PCHookMessage(Class cls, SEL selector, IMP replacement, IMP *original, const char *name) {
    Method method = class_getInstanceMethod(cls, selector);
    if (!method) {
        NSLog(@"#pc  objc hook missing name=%s", name);
        return;
    }
    *original = method_getImplementation(method);
    method_setImplementation(method, replacement);
    NSLog(@"#pc  objc hook installed name=%s original=%p", name, *original);
}

// MARK: - Unity / IL2CPP probes (UnityFramework offsets for 1.0.4)

static bool (*orig_jailbreakCheck)(void);
static bool pc_jailbreakCheck(void) {
    bool result = orig_jailbreakCheck();
    PCLogStack([NSString stringWithFormat:@"native jailbreak check result=%d", result]);
    return result;
}

static void (*orig_globalQuit)(void *, const void *);
static void pc_globalQuit(void *instance, const void *method) {
    PCLogStack([NSString stringWithFormat:@"GlobalObject.Quit instance=%p method=%p", instance, method]);
    orig_globalQuit(instance, method);
}

static void (*orig_applicationQuit)(int, const void *);
static void pc_applicationQuit(int exitCode, const void *method) {
    PCLogStack([NSString stringWithFormat:@"Application.Quit exitCode=%d", exitCode]);
    orig_applicationQuit(exitCode, method);
}

static void (*orig_obscuredCheater)(void *, const void *);
static void pc_obscuredCheater(void *instance, const void *method) {
    PCLogStack(@"ACTk OnObscuredCheaterDetected");
    orig_obscuredCheater(instance, method);
}

static void (*orig_speedCheater)(void *, const void *);
static void pc_speedCheater(void *instance, const void *method) {
    PCLogStack(@"ACTk OnSpeedCheaterDetected");
    orig_speedCheater(instance, method);
}

static void (*orig_timeCheater)(void *, int, int, const void *);
static void pc_timeCheater(void *instance, int result, int error, const void *method) {
    PCLogStack([NSString stringWithFormat:@"ACTk OnTimeCheaterDetected result=%d error=%d", result, error]);
    orig_timeCheater(instance, result, error, method);
}

static void (*orig_banProcess)(void *, const void *);
static void pc_banProcess(void *instance, const void *method) {
    PCLogStack(@"LoginScene.BanProcess");
    orig_banProcess(instance, method);
}

static void *(*orig_banPopupProcess)(void *, void *, const void *);
static void *pc_banPopupProcess(void *instance, void *list, const void *method) {
    PCLogStack([NSString stringWithFormat:@"LoginScene.BanPopupProcess list=%p", list]);
    return orig_banPopupProcess(instance, list, method);
}

static void (*orig_banInfoRequest)(void *, const void *);
static void pc_banInfoRequest(void *callback, const void *method) {
    PCLogStack([NSString stringWithFormat:@"PS_BanInfo.Request callback=%p", callback]);
    orig_banInfoRequest(callback, method);
}

static void (*orig_integrityRequest)(void *, void *, const void *);
static void pc_integrityRequest(void *requestHash, void *token, const void *method) {
    PCLogStack([NSString stringWithFormat:@"PS_Integrity.Request hash=%p token=%p", requestHash, token]);
    orig_integrityRequest(requestHash, token, method);
}

static void (*orig_integrityError)(void *, const void *);
static void pc_integrityError(void *parameter, const void *method) {
    PCLogStack([NSString stringWithFormat:@"PS_Integrity.OnErrorCallback param=%p", parameter]);
    orig_integrityError(parameter, method);
}

// E_TIME_REWARD.AdRemove == 4.  PS_ADView.Request checks this remaining time
// before showing its confirmation dialog or entering the rewarded-ad SDK.
static float (*orig_timeRewardGetRemainTime)(void *, int32_t, const void *);
static float pc_timeRewardGetRemainTime(void *instance, int32_t type, const void *method) {
    float original = orig_timeRewardGetRemainTime(instance, type, method);
    if (type == 4) {
        const float forced = 86400.0f;
        NSLog(@"#pc  TimeRewardListParam.GetRemainTime AdRemove original=%.3f forced=%.3f",
              original, forced);
        return forced;
    }
    return original;
}

// UILogin only exposes the title-screen entry controls after all asset/version
// checks have completed.  Start the same stored-account login used by BtnStart
// at that exact point, so readiness checks are preserved without requiring a
// physical "Touch to Start" tap.  E_LOGIN_TYPE.Guest == 2.
static void (*gUILoginStartLoginRequest)(void *, int32_t, const void *);
static void (*orig_uiLoginShowStartButton)(void *, bool, const void *);
static void *gLastAutoLoginInstance;
static void *gReadyUILoginInstance;
static bool gUILoginStartButtonVisible;

static void PCApplySkipTouchToStartIfReady(void) {
    if (!PCSkipTouchToStartEnabled() || !gUILoginStartButtonVisible ||
        !gReadyUILoginInstance || !gUILoginStartLoginRequest ||
        gReadyUILoginInstance == gLastAutoLoginInstance) {
        return;
    }

    gLastAutoLoginInstance = gReadyUILoginInstance;
    NSLog(@"#pc  UILogin.AutoStart instance=%p loginType=Guest",
          gReadyUILoginInstance);
    gUILoginStartLoginRequest(gReadyUILoginInstance, 2, nullptr);
}

static void pc_uiLoginShowStartButton(void *instance, bool show, const void *method) {
    orig_uiLoginShowStartButton(instance, show, method);
    if (!instance) return;

    if (show) {
        gReadyUILoginInstance = instance;
        gUILoginStartButtonVisible = true;
    } else if (instance == gReadyUILoginInstance) {
        gUILoginStartButtonVisible = false;
    }

    if (!PCSkipTouchToStartEnabled()) {
        if (show) {
            NSLog(@"#pc  UILogin.AutoStart skipped instance=%p setting=disabled",
                  instance);
        }
        return;
    }
    PCApplySkipTouchToStartIfReady();
}

// MainScene.OpenAwakeUI runs these four display-only coroutines in order after
// purchase recovery and main-scene initialization.  Completing only these
// iterators suppresses the automatic startup popups while leaving the rest of
// C_StartSequence intact.  Their normal/manual feature entry points are not
// hooked, and the rewarded-ad card logic above remains active.
static bool PCFinishStartupPopup(void *iterator, const char *name) {
    if (iterator) {
        int32_t *state = (int32_t *)((uint8_t *)iterator + 0x10);
        if (*state == 0) {
            NSLog(@"#pc  MainScene.StartupPopup skipped=%s iterator=%p", name, iterator);
        }
        *state = -1;
    }
    return false;
}

static bool (*orig_openNoticeMoveNext)(void *, const void *);
static bool pc_openNoticeMoveNext(void *iterator, const void *method) {
    return PCFinishStartupPopup(iterator, "OpenNotice");
}

static bool (*orig_openLoginBonusMoveNext)(void *, const void *);
static bool pc_openLoginBonusMoveNext(void *iterator, const void *method) {
    return PCFinishStartupPopup(iterator, "OpenLoginBonus");
}

static bool (*orig_openAFKMoveNext)(void *, const void *);
static bool pc_openAFKMoveNext(void *iterator, const void *method) {
    return PCFinishStartupPopup(iterator, "OpenAFK");
}

static bool (*orig_openTimeDealMoveNext)(void *, const void *);
static bool pc_openTimeDealMoveNext(void *iterator, const void *method) {
    return PCFinishStartupPopup(iterator, "OpenTimeDeal");
}

// UIPopupReward.ShowCompete is called when the reward animation has finished
// and the UI has changed to its final "tap to close" state.  Reuse OnBack two
// seconds later because it performs the same close callback as a real tap.
static void (*orig_popupRewardShowComplete)(void *, const void *);
static bool (*orig_popupRewardOnBack)(void *, const void *);
static bool (*gUnityObjectImplicit)(void *, const void *);
static void *(*gComponentGetGameObject)(void *, const void *);
static bool (*gGameObjectGetActiveInHierarchy)(void *, const void *);
static void *gPendingRewardPopup;
static uint64_t gRewardPopupGeneration;

static bool PCRewardPopupCanAutoClose(void *instance) {
    if (!instance || !gUnityObjectImplicit ||
        !gUnityObjectImplicit(instance, nullptr)) {
        return false;
    }

    // UIPopupReward.Event_Click unconditionally reads _goSkip at +0x60 before
    // it handles BtnGet.  The popup component can remain alive briefly after
    // its child objects have been destroyed, so validate the child as well.
    void *skipObject = *(void **)((uint8_t *)instance + 0x60);
    if (!skipObject || !gUnityObjectImplicit(skipObject, nullptr)) {
        return false;
    }

    if (gComponentGetGameObject && gGameObjectGetActiveInHierarchy) {
        void *gameObject = gComponentGetGameObject(instance, nullptr);
        if (!gameObject || !gUnityObjectImplicit(gameObject, nullptr) ||
            !gGameObjectGetActiveInHierarchy(gameObject, nullptr)) {
            return false;
        }
    }
    return true;
}

static bool pc_popupRewardOnBack(void *instance, const void *method) {
    if (instance && instance == gPendingRewardPopup) {
        gPendingRewardPopup = nullptr;
        gRewardPopupGeneration++;
    }
    return orig_popupRewardOnBack(instance, method);
}

static void pc_popupRewardShowComplete(void *instance, const void *method) {
    orig_popupRewardShowComplete(instance, method);
    if (!instance || !orig_popupRewardOnBack) return;

    gPendingRewardPopup = instance;
    uint64_t generation = ++gRewardPopupGeneration;
    NSLog(@"#pc  UIPopupReward.AutoClose scheduled instance=%p delay=2.0s", instance);

    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(2.0 * NSEC_PER_SEC)),
                   dispatch_get_main_queue(), ^{
        if (gPendingRewardPopup != instance ||
            gRewardPopupGeneration != generation) {
            return;
        }

        bool canClose = false;
        try {
            canClose = PCRewardPopupCanAutoClose(instance);
        } catch (...) {
            // IL2CPP raises managed exceptions as C++ Il2CppExceptionWrapper
            // objects.  Never allow one to escape a libdispatch block.
            NSLog(@"#pc  UIPopupReward.AutoClose validation exception contained "
                   "instance=%p",
                  instance);
        }
        if (!canClose) {
            gPendingRewardPopup = nullptr;
            gRewardPopupGeneration++;
            NSLog(@"#pc  UIPopupReward.AutoClose cancelled instance=%p invalid=1",
                  instance);
            return;
        }

        gPendingRewardPopup = nullptr;
        gRewardPopupGeneration++;
        NSLog(@"#pc  UIPopupReward.AutoClose firing instance=%p", instance);
        try {
            orig_popupRewardOnBack(instance, nullptr);
            NSLog(@"#pc  UIPopupReward.AutoClose completed instance=%p", instance);
        } catch (...) {
            // Event_Click closes the popup before invoking its close callback.
            // A stale callback may still throw during a scene transition; keep
            // that managed exception from reaching std::terminate/abort.
            NSLog(@"#pc  UIPopupReward.AutoClose managed exception contained "
                   "instance=%p",
                  instance);
        }
    });
}

// Mine cells are normally clickable only when SetState finds them immediately
// adjacent to the current position.  Keep the game's request-in-flight lock,
// but make every rendered cell selectable after each state refresh.  A click
// keeps the original cell-type split: rock cells use RequestDrill (and its
// Mine_Drill check), while ordinary/reward cells use RequestMoveCell (and its
// Mine_Stamina check).  Only the UI adjacency restriction is bypassed here.
static void (*gMineRowItemEnableMove)(void *, bool, const void *);
static bool (*gMineRowItemRequestMoveCell)(void *, const void *);
static bool (*gMineRowItemRequestDrill)(void *, const void *);
static int32_t (*gMineCellInfoGetCol)(void *, const void *);
static int32_t (*gMineCellInfoGetRow)(void *, const void *);
static int32_t (*gMineCellInfoGetType)(void *, const void *);
static void *(*gMineScrollViewGetCellItem)(void *, int32_t, int32_t,
                                           const void *);
static void (*gMineInfosRequest)(const void *);
static void (*gUIGardenMineSetData)(void *, bool, const void *);
static int32_t (*gMineInfoGetCol)(void *, const void *);
static int32_t (*gMineInfoGetRow)(void *, const void *);
static int32_t (*gMineInfoGetDistance)(void *, const void *);

static bool gMineFarMovePending;
static void *gMineRefreshPopup;
static bool gMineRefreshAwaitingResponse;
static uint64_t gMineRefreshGeneration;

static void (*orig_mineRowItemSetState)(void *, const void *);
static void pc_mineRowItemSetState(void *instance, const void *method) {
    orig_mineRowItemSetState(instance, method);
    if (!instance || !gMineRowItemEnableMove) return;
    gMineRowItemEnableMove(instance, true, nullptr);
}

static void (*orig_mineRowItemEventClick)(void *, void *, const void *);
static void pc_mineRowItemEventClick(void *instance, void *name,
                                     const void *method) {
    if (!instance || !gMineRowItemRequestMoveCell ||
        !gMineRowItemRequestDrill || !gMineCellInfoGetType) {
        orig_mineRowItemEventClick(instance, name, method);
        return;
    }

    void *info = *(void **)((uint8_t *)instance + 0x30);
    if (!info || !gMineCellInfoGetCol || !gMineCellInfoGetRow) {
        orig_mineRowItemEventClick(instance, name, method);
        return;
    }

    int32_t col = gMineCellInfoGetCol(info, nullptr);
    int32_t row = gMineCellInfoGetRow(info, nullptr);
    int32_t cellType = gMineCellInfoGetType(info, nullptr);
    bool isRock = cellType == 1;
    int32_t currentCol = col;
    int32_t currentRow = row;
    bool hasCurrentCell = false;
    void *scrollView = *(void **)((uint8_t *)instance + 0x40);
    if (scrollView && gMineScrollViewGetCellItem) {
        void *currentItem =
            gMineScrollViewGetCellItem(scrollView, -1, -1, nullptr);
        if (currentItem) {
            void *currentInfo = *(void **)((uint8_t *)currentItem + 0x30);
            if (currentInfo) {
                currentCol = gMineCellInfoGetCol(currentInfo, nullptr);
                currentRow = gMineCellInfoGetRow(currentInfo, nullptr);
                hasCurrentCell = true;
            }
        }
    }

    int32_t deltaCol = col - currentCol;
    int32_t deltaRow = row - currentRow;
    if (deltaCol < 0) deltaCol = -deltaCol;
    if (deltaRow < 0) deltaRow = -deltaRow;
    bool farMove = hasCurrentCell && (deltaCol + deltaRow > 1);
    gMineFarMovePending = false;
    NSLog(@"#pc  Mine.DirectMove click name=%@ from=(%d,%d) to=(%d,%d) "
           "distance=%d far=%d cellType=%d action=%s",
          PCStringFromIl2Cpp(name), currentCol, currentRow, col, row,
          deltaCol + deltaRow, farMove ? 1 : 0, cellType,
          isRock ? "Drill" : "CellMove");
    bool requested = isRock
        ? gMineRowItemRequestDrill(instance, nullptr)
        : gMineRowItemRequestMoveCell(instance, nullptr);
    gMineFarMovePending = !isRock && requested && farMove;
    NSLog(@"#pc  Mine.DirectMove request col=%d row=%d action=%s "
           "acceptedLocal=%d refreshAfterResponse=%d",
          col, row, isRock ? "Drill" : "CellMove",
          requested ? 1 : 0, gMineFarMovePending ? 1 : 0);
}

// A normal move response advances the rolling 5x5 mine window by one row.
// Sending a farther target in one RequestMoveCell still advances that window
// only once, leaving the character at its edge.  Keep the ordinary move and,
// only for a non-adjacent target, reload PS_MineInfos after the move finishes.
// Its response is the authoritative window around the server-side position.
static void (*orig_uiGardenMineMove)(void *, void *, void *, const void *);
static void pc_uiGardenMineMove(void *instance, void *moveList,
                                void *cellList, const void *method) {
    bool refresh = gMineFarMovePending;
    gMineFarMovePending = false;
    orig_uiGardenMineMove(instance, moveList, cellList, method);
    if (!refresh || !instance || !gMineInfosRequest) return;

    gMineRefreshPopup = instance;
    uint64_t generation = ++gMineRefreshGeneration;
    NSLog(@"#pc  Mine.ViewRefresh scheduled popup=%p delay=0.75s", instance);
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW,
                                 (int64_t)(0.75 * NSEC_PER_SEC)),
                   dispatch_get_main_queue(), ^{
        if (gMineRefreshPopup != instance ||
            gMineRefreshGeneration != generation) {
            return;
        }
        if (gUnityObjectImplicit && !gUnityObjectImplicit(instance, nullptr)) {
            gMineRefreshPopup = nullptr;
            gMineRefreshGeneration++;
            NSLog(@"#pc  Mine.ViewRefresh cancelled popup=%p destroyed=1",
                  instance);
            return;
        }

        gMineRefreshAwaitingResponse = true;
        NSLog(@"#pc  Mine.ViewRefresh request PS_MineInfos popup=%p", instance);
        gMineInfosRequest(nullptr);
    });
}

static void (*orig_mineInfosResponse)(void *, const void *);
static void pc_mineInfosResponse(void *response, const void *method) {
    bool refresh = gMineRefreshAwaitingResponse;
    void *mineInfo = response
        ? *(void **)((uint8_t *)response + 0x48)
        : nullptr;
    int32_t col = mineInfo && gMineInfoGetCol
        ? gMineInfoGetCol(mineInfo, nullptr) : -1;
    int32_t row = mineInfo && gMineInfoGetRow
        ? gMineInfoGetRow(mineInfo, nullptr) : -1;
    int32_t distance = mineInfo && gMineInfoGetDistance
        ? gMineInfoGetDistance(mineInfo, nullptr) : -1;
    int32_t rowCount = -1;
    if (mineInfo) {
        void *rows = *(void **)((uint8_t *)mineInfo + 0x10);
        if (rows) {
            int32_t count = *(int32_t *)((uint8_t *)rows + 0x18);
            if (count >= 0 && count <= 1024) rowCount = count;
        }
    }

    orig_mineInfosResponse(response, method);
    NSLog(@"#pc  Mine.ViewRefresh response requestedByTweak=%d "
           "center=(%d,%d) distance=%d rows=%d",
          refresh ? 1 : 0, col, row, distance, rowCount);
    if (!refresh) return;

    gMineRefreshAwaitingResponse = false;
    void *popup = gMineRefreshPopup;
    gMineRefreshPopup = nullptr;
    gMineRefreshGeneration++;
    if (!popup || !gUIGardenMineSetData) return;
    if (gUnityObjectImplicit && !gUnityObjectImplicit(popup, nullptr)) {
        NSLog(@"#pc  Mine.ViewRefresh redraw cancelled popup=%p destroyed=1",
              popup);
        return;
    }

    NSLog(@"#pc  Mine.ViewRefresh redraw popup=%p update=0", popup);
    gUIGardenMineSetData(popup, false, nullptr);
}

// The first regular battle defeat after launch can open UIGrowthGuide.  Its
// own cooldown explains why later defeats in the same run do not show it.
// Mark only the BattleProcessor.State_Defeat call path, then use the scene's
// normal CloseGrowthGuide entry point one second after the guide is shown.
static __thread int gBattleDefeatDepth;
static void (*gUIContentsSceneCloseGrowthGuide)(void *, const void *);
static void *gPendingFailureGrowthScene;
static uint64_t gFailureGrowthGeneration;

static void (*orig_battleProcessorStateDefeat)(void *, const void *);
static void pc_battleProcessorStateDefeat(void *instance, const void *method) {
    gBattleDefeatDepth++;
    orig_battleProcessorStateDefeat(instance, method);
    gBattleDefeatDepth--;
}

static void (*orig_uiContentsSceneShowGrowthGuide)(void *, bool, bool,
                                                    const void *);
static void pc_uiContentsSceneShowGrowthGuide(void *instance,
                                              bool isReservationMoveState,
                                              bool isAddPopupNode,
                                              const void *method) {
    bool isBattleFailure = gBattleDefeatDepth > 0;
    orig_uiContentsSceneShowGrowthGuide(instance, isReservationMoveState,
                                        isAddPopupNode, method);
    if (!isBattleFailure || !instance || !gUIContentsSceneCloseGrowthGuide) {
        return;
    }

    gPendingFailureGrowthScene = instance;
    uint64_t generation = ++gFailureGrowthGeneration;
    NSLog(@"#pc  BattleFailure.GrowthGuide auto-close scheduled scene=%p delay=1.0s",
          instance);

    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(1.0 * NSEC_PER_SEC)),
                   dispatch_get_main_queue(), ^{
        if (gPendingFailureGrowthScene != instance ||
            gFailureGrowthGeneration != generation) {
            return;
        }
        if (gUnityObjectImplicit && !gUnityObjectImplicit(instance, nullptr)) {
            gPendingFailureGrowthScene = nullptr;
            gFailureGrowthGeneration++;
            NSLog(@"#pc  BattleFailure.GrowthGuide auto-close cancelled scene=%p destroyed=1",
                  instance);
            return;
        }

        gPendingFailureGrowthScene = nullptr;
        gFailureGrowthGeneration++;
        NSLog(@"#pc  BattleFailure.GrowthGuide auto-close firing scene=%p", instance);
        gUIContentsSceneCloseGrowthGuide(instance, nullptr);
    });
}

// StageBase.C_InitNaviMesh rebuilds the same runtime NavMeshData before every
// battle.  NavMeshSurface.BuildNavMesh replaces m_NavMeshData (+0x60), but the
// package implementation never destroys the old Unity native object.  Those
// abandoned NavMeshData allocations accumulated until iOS killed the process
// at its 3 GiB per-process limit.  Preserve every rebuild (different stages
// may have different geometry), then explicitly destroy the replaced data.
static void (*orig_navMeshSurfaceBuildNavMesh)(void *, const void *);
static void (*gUnityObjectDestroy)(void *, const void *);
static uint64_t gNavMeshBuildCount;
static uint64_t gNavMeshCleanupCount;
static void *gPreviousNavMeshSurface;
static void *gPreviousNavMeshData;

static bool PCNavMeshDestroyData(void *data, uint64_t buildCount,
                                 const char *source) {
    if (!data || !gUnityObjectDestroy ||
        !PCReadableRange(data, sizeof(void *))) {
        return false;
    }

    bool valid = true;
    try {
        if (gUnityObjectImplicit) {
            valid = gUnityObjectImplicit(data, nullptr);
        }
    } catch (...) {
        valid = false;
        NSLog(@"#pc  NavMesh.NativeCleanup validation exception contained "
              "build=%llu source=%s data=%p",
              (unsigned long long)buildCount, source ?: "(unknown)", data);
    }
    if (!valid) return false;

    try {
        gUnityObjectDestroy(data, nullptr);
        gNavMeshCleanupCount++;
        NSLog(@"#pc  NavMesh.NativeCleanup destroy scheduled build=%llu "
              "source=%s data=%p totalCleaned=%llu",
              (unsigned long long)buildCount, source ?: "(unknown)", data,
              (unsigned long long)gNavMeshCleanupCount);
        return true;
    } catch (...) {
        // This call is introduced by the tweak.  Do not allow an unexpected
        // Unity object-lifetime exception to terminate the managed caller.
        NSLog(@"#pc  NavMesh.NativeCleanup destroy exception contained "
              "build=%llu source=%s data=%p",
              (unsigned long long)buildCount, source ?: "(unknown)", data);
        return false;
    }
}

static void pc_navMeshSurfaceBuildNavMesh(void *surface, const void *method) {
    void *oldData = nullptr;
    if (surface &&
        PCReadableRange((uint8_t *)surface + 0x60, sizeof(void *))) {
        oldData = *(void **)((uint8_t *)surface + 0x60);
    }

    orig_navMeshSurfaceBuildNavMesh(surface, method);

    uint64_t buildCount = ++gNavMeshBuildCount;
    void *previousSurface = gPreviousNavMeshSurface;
    void *previousData = gPreviousNavMeshData;
    void *newData = nullptr;
    if (surface &&
        PCReadableRange((uint8_t *)surface + 0x60, sizeof(void *))) {
        newData = *(void **)((uint8_t *)surface + 0x60);
    }

    // Hold only the latest runtime-built NavMeshData.  The game creates a new
    // StageBase/NavMeshSurface for many consecutive battles, so the previous
    // surface's data is the common leak; oldData covers the less common case
    // where the same surface itself is rebuilt.
    gPreviousNavMeshSurface = surface;
    gPreviousNavMeshData = newData;

    bool cleanedCurrentOld = false;
    if (oldData && oldData != newData) {
        cleanedCurrentOld =
            PCNavMeshDestroyData(oldData, buildCount, "current_surface_old");
    }

    bool cleanedPrevious = false;
    if (previousData && previousData != newData &&
        previousData != oldData) {
        cleanedPrevious =
            PCNavMeshDestroyData(previousData, buildCount,
                                 "previous_surface_data");
    }

    NSLog(@"#pc  NavMesh.NativeCleanup build=%llu surface=%p old=%p new=%p "
          "previousSurface=%p previousData=%p cleanedCurrent=%d "
          "cleanedPrevious=%d totalCleaned=%llu",
          (unsigned long long)buildCount, surface, oldData, newData,
          previousSurface, previousData, cleanedCurrentOld ? 1 : 0,
          cleanedPrevious ? 1 : 0,
          (unsigned long long)gNavMeshCleanupCount);
}

// C_Result is the hologram equipment-result pipeline.  It opens UIItemSelect
// only when DataUtil.CompareStatEquipedItem reports that the new item is better.
// Mark that synchronous call path so UIItemSelect instances opened elsewhere
// keep their normal manual behaviour.
//
// PS_ItemEquip.Response normally resumes SpawnItem immediately.  For tweak
// replacements, hold that resume until the response's authoritative
// _unEquipUID has been sold through PS_ItemSell.Request.  This preserves the
// game's own equip/sell data refresh while preventing auto-spawn from getting
// stuck behind the unequipped item.
static __thread int gItemSpawnerResultDepth;
static __thread void *gItemSpawnerResultInstance;
static __thread int gItemEquipResponseDepth;
static bool (*orig_itemSpawnerResultMoveNext)(void *, const void *);
static void (*orig_itemSelectSetData)(void *, void *, int32_t, void *, const void *);
static void (*orig_itemEquipResponse)(void *, const void *);
static void (*orig_itemSellResponse)(void *, const void *);
static void (*orig_itemSpawnerSpawnItem)(void *, const void *);
static int32_t (*gItemInfoGetType)(void *, const void *);
static void *(*gItemInfoGetStringUID)(void *, const void *);
static void (*gItemEquipRequest)(int32_t, void *, bool, const void *);
static void (*gItemSellRequest)(void *, const void *);
static void (*gItemSelectClose)(void *, const void *);
static void *gItemSpawnerReplacementInstance;
static bool gItemSpawnerEquipPending;
static bool gItemSpawnerSellPending;
static char gItemSpawnerOldUID[128];

static void PCItemSpawnerResume(void *spawner, const char *reason) {
    if (!spawner || !orig_itemSpawnerSpawnItem) {
        NSLog(@"#pc  ItemSpawner.AutoReplace resume skipped spawner=%p "
              "reason=%s missing_target=1",
              spawner, reason ?: "(unknown)");
        return;
    }

    bool valid = true;
    try {
        if (gUnityObjectImplicit) {
            valid = gUnityObjectImplicit(spawner, nullptr);
        }
    } catch (...) {
        valid = false;
        NSLog(@"#pc  ItemSpawner.AutoReplace resume validation exception "
              "contained spawner=%p reason=%s",
              spawner, reason ?: "(unknown)");
    }
    if (!valid) {
        NSLog(@"#pc  ItemSpawner.AutoReplace resume cancelled spawner=%p "
              "reason=%s destroyed=1",
              spawner, reason ?: "(unknown)");
        return;
    }

    NSLog(@"#pc  ItemSpawner.AutoReplace resume spawner=%p reason=%s",
          spawner, reason ?: "(unknown)");
    try {
        orig_itemSpawnerSpawnItem(spawner, nullptr);
    } catch (...) {
        // This call is initiated by the tweak rather than a managed caller, so
        // contain any stale-scene exception instead of letting it terminate.
        NSLog(@"#pc  ItemSpawner.AutoReplace resume exception contained "
              "spawner=%p reason=%s",
              spawner, reason ?: "(unknown)");
    }
}

static void PCItemSpawnerFinishReplacement(const char *reason) {
    void *spawner = gItemSpawnerReplacementInstance;
    gItemSpawnerReplacementInstance = nullptr;
    gItemSpawnerEquipPending = false;
    gItemSpawnerSellPending = false;
    gItemSpawnerOldUID[0] = '\0';
    PCItemSpawnerResume(spawner, reason);
}

static bool pc_itemSpawnerResultMoveNext(void *iterator, const void *method) {
    void *previousInstance = gItemSpawnerResultInstance;
    gItemSpawnerResultInstance =
        iterator ? *(void **)((uint8_t *)iterator + 0x20) : nullptr;
    gItemSpawnerResultDepth++;
    try {
        bool result = orig_itemSpawnerResultMoveNext(iterator, method);
        gItemSpawnerResultDepth--;
        gItemSpawnerResultInstance = previousInstance;
        return result;
    } catch (...) {
        gItemSpawnerResultDepth--;
        gItemSpawnerResultInstance = previousInstance;
        throw;
    }
}

static void pc_itemSelectSetData(void *instance, void *info, int32_t openType,
                                 void *userName, const void *method) {
    bool fromItemSpawner = gItemSpawnerResultDepth > 0 && openType == 0;
    orig_itemSelectSetData(instance, info, openType, userName, method);
    if (!fromItemSpawner || !instance || !info || !gItemInfoGetType ||
        !gItemInfoGetStringUID || !gItemEquipRequest || !gItemSelectClose) {
        return;
    }

    int32_t itemType = gItemInfoGetType(info, nullptr);
    void *uid = gItemInfoGetStringUID(info, nullptr);
    if (!uid) {
        NSLog(@"#pc  ItemSpawner.AutoEquip skipped info=%p reason=no_uid", info);
        return;
    }

    if (gItemSpawnerEquipPending || gItemSpawnerSellPending) {
        NSLog(@"#pc  ItemSpawner.AutoEquip skipped info=%p type=%d uid=%@ "
              "reason=replacement_in_flight equipPending=%d sellPending=%d",
              info, itemType, PCStringFromIl2Cpp(uid),
              gItemSpawnerEquipPending ? 1 : 0,
              gItemSpawnerSellPending ? 1 : 0);
        return;
    }

    gItemSpawnerReplacementInstance = gItemSpawnerResultInstance;
    gItemSpawnerEquipPending = true;
    gItemSpawnerSellPending = false;
    gItemSpawnerOldUID[0] = '\0';
    NSLog(@"#pc  ItemSpawner.AutoEquip request info=%p spawner=%p type=%d "
          "uid=%@",
          info, gItemSpawnerReplacementInstance, itemType,
          PCStringFromIl2Cpp(uid));
    try {
        gItemEquipRequest(itemType, uid, true, nullptr);
    } catch (...) {
        gItemSpawnerReplacementInstance = nullptr;
        gItemSpawnerEquipPending = false;
        NSLog(@"#pc  ItemSpawner.AutoEquip request exception contained "
              "info=%p type=%d uid=%@",
              info, itemType, PCStringFromIl2Cpp(uid));
        return;
    }
    gItemSelectClose(instance, nullptr);
}

static void pc_itemSpawnerSpawnItem(void *instance, const void *method) {
    if (gItemSpawnerEquipPending && gItemEquipResponseDepth > 0) {
        if (!gItemSpawnerReplacementInstance) {
            gItemSpawnerReplacementInstance = instance;
        }
        NSLog(@"#pc  ItemSpawner.AutoReplace hold SpawnItem instance=%p "
              "until=old_item_sell",
              instance);
        return;
    }
    orig_itemSpawnerSpawnItem(instance, method);
}

static void pc_itemEquipResponse(void *response, const void *method) {
    bool autoReplace = gItemSpawnerEquipPending;
    void *equipUID =
        autoReplace && response
            ? *(void **)((uint8_t *)response + 0x50)
            : nullptr;
    void *oldUID =
        autoReplace && response
            ? *(void **)((uint8_t *)response + 0x58)
            : nullptr;

    gItemEquipResponseDepth++;
    try {
        orig_itemEquipResponse(response, method);
    } catch (...) {
        gItemEquipResponseDepth--;
        if (autoReplace) {
            gItemSpawnerReplacementInstance = nullptr;
            gItemSpawnerEquipPending = false;
            gItemSpawnerSellPending = false;
            gItemSpawnerOldUID[0] = '\0';
            NSLog(@"#pc  ItemSpawner.AutoEquip response exception "
                  "response=%p equipUID=%@ oldUID=%@",
                  response, PCStringFromIl2Cpp(equipUID),
                  PCStringFromIl2Cpp(oldUID));
        }
        throw;
    }
    gItemEquipResponseDepth--;

    if (!autoReplace || !gItemSpawnerEquipPending) return;

    gItemSpawnerEquipPending = false;
    if (!oldUID || !gItemSellRequest ||
        PCUTF8FromIl2CppString(oldUID, gItemSpawnerOldUID,
                              sizeof(gItemSpawnerOldUID)) == 0) {
        NSLog(@"#pc  ItemSpawner.AutoEquip response response=%p equipUID=%@ "
              "oldUID=%@ sell=skipped",
              response, PCStringFromIl2Cpp(equipUID),
              PCStringFromIl2Cpp(oldUID));
        PCItemSpawnerFinishReplacement("no_old_item_to_sell");
        return;
    }

    gItemSpawnerSellPending = true;
    NSLog(@"#pc  ItemSpawner.AutoSell request response=%p equipUID=%@ "
          "oldUID=%s",
          response, PCStringFromIl2Cpp(equipUID), gItemSpawnerOldUID);
    try {
        gItemSellRequest(oldUID, nullptr);
    } catch (...) {
        NSLog(@"#pc  ItemSpawner.AutoSell request exception contained "
              "oldUID=%s",
              gItemSpawnerOldUID);
        PCItemSpawnerFinishReplacement("sell_request_exception");
    }
}

static void pc_itemSellResponse(void *response, const void *method) {
    bool autoReplace = gItemSpawnerSellPending;
    char oldUID[sizeof(gItemSpawnerOldUID)] = {};
    if (autoReplace) {
        strlcpy(oldUID, gItemSpawnerOldUID, sizeof(oldUID));
    }

    orig_itemSellResponse(response, method);
    if (!autoReplace || !gItemSpawnerSellPending) return;

    NSLog(@"#pc  ItemSpawner.AutoSell response response=%p oldUID=%s",
          response, oldUID[0] ? oldUID : "(unknown)");
    PCItemSpawnerFinishReplacement("old_item_sold");
}

// The Firewall dungeon's StartDungeon coroutine normally downloads every
// ranking page and several ranked-player details before it calls PlayDungeon.
// If any ranking request stalls, UIDungeonReady_Firewall._isClicked remains
// set and the challenge button silently ignores every later tap.  The original
// Event_Click is left intact so its content/ticket checks still run; only the
// coroutine reached after those checks is shortened to the same PlayDungeon
// transition used by ordinary dungeons.
static void *gGameInfoInstance;
static void (*orig_gameInfoUpdateBattleStart)(void *, const void *);
static void (*orig_gameInfoPlayBattle)(void *, int32_t, int32_t, int32_t,
                                       int32_t, bool, const void *);
static void (*gGameInfoPlayDungeon)(void *, int32_t, int32_t, int32_t,
                                    const void *);
static int32_t (*gBattleInfoParamClientGetStage)(void *, const void *);
static int32_t (*gBattleInfoParamClientGetSector)(void *, const void *);
static int32_t (*gBattleInfoParamClientGetRepeat)(void *, const void *);
static int32_t (*gBattleInfoParamClientGetBattleState)(void *, const void *);
static void (*gBattleInfoParamClientSetBattleState)(void *, int32_t,
                                                    const void *);
static int32_t (*gBattleInfoParamClientGetReason)(void *, const void *);
static void (*gBattleInfoParamClientSetReason)(void *, int32_t, const void *);
static int32_t (*gBattleInfoParamClientGetWave)(void *, const void *);
static void (*gBattleInfoParamClientSetWave)(void *, int32_t, const void *);
static void *(*gBattleInfoParamClientGetRegion)(void *, const void *);
static void *(*gDataInfoRegionGetStage)(void *, int32_t, const void *);
static int32_t (*gDataInfoStageGetSectorCount)(void *, const void *);
static void (*orig_battleStartStageRequest)(int32_t, bool, const void *);
static void (*orig_battleEndStageRequest)(int32_t, int32_t, int64_t, int64_t,
                                          int64_t, void *, const void *);
static bool (*orig_firewallStartDungeonMoveNext)(void *, const void *);

static void pc_gameInfoUpdateBattleStart(void *instance, const void *method) {
    if (instance && instance != gGameInfoInstance) {
        gGameInfoInstance = instance;
        NSLog(@"#pc  GameInfo captured instance=%p", instance);
    }
    orig_gameInfoUpdateBattleStart(instance, method);
}

static void *PCBattleInfoClient(void) {
    if (!gGameInfoInstance ||
        !PCReadableRange((uint8_t *)gGameInfoInstance + 0x20,
                         sizeof(void *))) {
        return nullptr;
    }
    return *(void **)((uint8_t *)gGameInfoInstance + 0x20);
}

static int32_t PCBattleCurrentStage(void) {
    void *client = PCBattleInfoClient();
    if (!client || !gBattleInfoParamClientGetStage) return -1;
    try {
        return gBattleInfoParamClientGetStage(client, nullptr);
    } catch (...) {
        NSLog(@"#pc  BattleRollback.GetStage exception contained client=%p",
              client);
        return -1;
    }
}

static int32_t PCBattleCurrentSector(void) {
    void *client = PCBattleInfoClient();
    if (!client || !gBattleInfoParamClientGetSector) return -1;
    try {
        return gBattleInfoParamClientGetSector(client, nullptr);
    } catch (...) {
        NSLog(@"#pc  BattleRollback.GetSector exception contained client=%p",
              client);
        return -1;
    }
}

static int32_t PCBattleCurrentRepeat(void) {
    void *client = PCBattleInfoClient();
    if (!client || !gBattleInfoParamClientGetRepeat) return -1;
    try {
        return gBattleInfoParamClientGetRepeat(client, nullptr);
    } catch (...) {
        NSLog(@"#pc  BattleRollback.GetRepeat exception contained client=%p",
              client);
        return -1;
    }
}

static int32_t PCBattleCurrentState(void) {
    void *client = PCBattleInfoClient();
    if (!client || !gBattleInfoParamClientGetBattleState) return -1;
    try {
        return gBattleInfoParamClientGetBattleState(client, nullptr);
    } catch (...) {
        NSLog(@"#pc  BattleRollback.GetBattleState exception contained "
               "client=%p",
              client);
        return -1;
    }
}

static int32_t PCBattleCurrentReason(void) {
    void *client = PCBattleInfoClient();
    if (!client || !gBattleInfoParamClientGetReason) return -1;
    try {
        return gBattleInfoParamClientGetReason(client, nullptr);
    } catch (...) {
        NSLog(@"#pc  BattleRollback.GetReason exception contained client=%p",
              client);
        return -1;
    }
}

static int32_t PCBattleCurrentWave(void) {
    void *client = PCBattleInfoClient();
    if (!client || !gBattleInfoParamClientGetWave) return -1;
    try {
        return gBattleInfoParamClientGetWave(client, nullptr);
    } catch (...) {
        NSLog(@"#pc  BattleRollback.GetWave exception contained client=%p",
              client);
        return -1;
    }
}

static void PCBattleResetRedirectedStartState(void) {
    void *client = PCBattleInfoClient();
    if (!client || !gBattleInfoParamClientGetBattleState ||
        !gBattleInfoParamClientSetBattleState ||
        !gBattleInfoParamClientGetReason ||
        !gBattleInfoParamClientSetReason ||
        !gBattleInfoParamClientGetWave ||
        !gBattleInfoParamClientSetWave) {
        NSLog(@"#pc  BattleRollback.ResetStartState unavailable client=%p",
              client);
        return;
    }

    try {
        int32_t oldState =
            gBattleInfoParamClientGetBattleState(client, nullptr);
        int32_t oldReason =
            gBattleInfoParamClientGetReason(client, nullptr);
        int32_t oldWave = gBattleInfoParamClientGetWave(client, nullptr);

        // SetBattleInfo changes stage/sector but carries these values over
        // from the battle that just failed. A redirected start must look like
        // a fresh forward battle to the server.
        gBattleInfoParamClientSetBattleState(client, 0, nullptr);
        gBattleInfoParamClientSetReason(client, 0, nullptr);
        gBattleInfoParamClientSetWave(client, 0, nullptr);

        NSLog(@"#pc  BattleRollback.ResetStartState state=%d->%d "
               "reason=%d->%d wave=%d->%d",
              oldState,
              gBattleInfoParamClientGetBattleState(client, nullptr),
              oldReason, gBattleInfoParamClientGetReason(client, nullptr),
              oldWave, gBattleInfoParamClientGetWave(client, nullptr));
    } catch (...) {
        NSLog(@"#pc  BattleRollback.ResetStartState exception contained "
               "client=%p",
              client);
    }
}

static int32_t PCBattleSectorCountForStage(int32_t stage) {
    void *client = PCBattleInfoClient();
    if (!client || stage <= 0 || !gBattleInfoParamClientGetRegion ||
        !gDataInfoRegionGetStage || !gDataInfoStageGetSectorCount) {
        return -1;
    }

    try {
        void *region = gBattleInfoParamClientGetRegion(client, nullptr);
        void *stageData =
            region ? gDataInfoRegionGetStage(region, stage, nullptr) : nullptr;
        int32_t count = stageData
            ? gDataInfoStageGetSectorCount(stageData, nullptr) : -1;
        NSLog(@"#pc  BattleRollback.SectorCount stage=%d count=%d "
               "region=%p stageData=%p",
              stage, count, region, stageData);
        return count;
    } catch (...) {
        NSLog(@"#pc  BattleRollback.SectorCount exception contained "
               "client=%p stage=%d",
              client, stage);
        return -1;
    }
}

static void PCBattleRecordDefeat(int32_t reason) {
    if (!PCBattleRollbackEnabled()) return;

    int32_t failedStage = gBattleRollbackLastRequestedStage;
    int32_t failedSector = gBattleRollbackLastRequestedSector;
    if (failedStage <= 0) failedStage = PCBattleCurrentStage();
    if (failedSector <= 0) failedSector = PCBattleCurrentSector();
    if (failedStage <= 0 || failedSector <= 0) {
        NSLog(@"#pc  BattleRollback.Defeat skipped reason=%d "
               "stage=%d sector=%d",
              reason, failedStage, failedSector);
        return;
    }

    if (gBattleRollbackBaseStage <= 0) {
        gBattleRollbackBaseStage = failedStage;
        gBattleRollbackBaseSector = failedSector;
    }

    if (failedSector > 1) {
        gBattleRollbackTargetStage = failedStage;
        gBattleRollbackTargetSector = failedSector - 1;
    } else if (failedStage > 1) {
        gBattleRollbackTargetStage = failedStage - 1;
        gBattleRollbackTargetSector =
            PCBattleSectorCountForStage(gBattleRollbackTargetStage);
        if (gBattleRollbackTargetSector <= 0) {
            // The normal stage data currently uses ten sectors per displayed
            // stage. Keep the rollback usable if the lookup is unavailable.
            gBattleRollbackTargetSector = 10;
        }
    } else {
        gBattleRollbackTargetStage = 1;
        gBattleRollbackTargetSector = 1;
    }

    gBattleRollbackRestorePending = false;
    NSLog(@"#pc  BattleRollback.Defeat reason=%d failed=%d/%d "
           "base=%d/%d next=%d/%d",
          reason, failedStage, failedSector,
          gBattleRollbackBaseStage, gBattleRollbackBaseSector,
          gBattleRollbackTargetStage, gBattleRollbackTargetSector);
}

static void pc_gameInfoPlayBattle(void *instance, int32_t type,
                                  int32_t stageKey, int32_t sector,
                                  int32_t attribute, bool changeStage,
                                  const void *method) {
    if (instance && instance != gGameInfoInstance) {
        gGameInfoInstance = instance;
        NSLog(@"#pc  GameInfo captured from PlayBattle instance=%p", instance);
    }

    int32_t currentStage = PCBattleCurrentStage();
    int32_t currentSector = PCBattleCurrentSector();
    int32_t sendStage = stageKey;
    int32_t sendSector = sector;
    bool sendChangeStage = changeStage;
    bool autoContinue = type == 1 && stageKey < 0 && sector < 0;
    bool redirected = false;
    bool restoring = false;

    if (autoContinue && PCBattleRollbackEnabled() &&
        gBattleRollbackTargetStage > 0 &&
        gBattleRollbackTargetSector > 0 &&
        (currentStage != gBattleRollbackTargetStage ||
         currentSector != gBattleRollbackTargetSector)) {
        // Keep PlayBattle on its negative stage-key path while explicitly
        // selecting the rollback sector.  -2 is intentionally distinct from
        // the game's ordinary auto-continue default (-1).
        sendStage = -2;
        sendSector = gBattleRollbackTargetSector;
        sendChangeStage = true;
        redirected = true;
    } else if (autoContinue && !PCBattleRollbackEnabled() &&
               gBattleRollbackRestorePending &&
               gBattleRollbackBaseStage > 0 &&
               gBattleRollbackBaseSector > 0) {
        sendStage = gBattleRollbackBaseStage;
        sendSector = gBattleRollbackBaseSector;
        sendChangeStage = true;
        redirected = true;
        restoring = true;
    }

    NSLog(@"#pc  BattleRollback.PlayBattle enabled=%d type=%d "
           "input=%d/%d output=%d/%d current=%d/%d base=%d/%d "
           "target=%d/%d auto=%d redirected=%d attr=%d "
           "changeStage=%d->%d",
          PCBattleRollbackEnabled() ? 1 : 0, type, stageKey, sector,
          sendStage, sendSector, currentStage, currentSector,
          gBattleRollbackBaseStage, gBattleRollbackBaseSector,
          gBattleRollbackTargetStage, gBattleRollbackTargetSector,
          autoContinue ? 1 : 0, redirected ? 1 : 0, attribute,
          changeStage ? 1 : 0, sendChangeStage ? 1 : 0);

    orig_gameInfoPlayBattle(instance, type, sendStage, sendSector, attribute,
                            sendChangeStage, method);

    if (redirected) {
        PCBattleResetRedirectedStartState();
    }

    int32_t actualStage = PCBattleCurrentStage();
    int32_t actualSector = PCBattleCurrentSector();
    NSLog(@"#pc  BattleRollback.PlayBattleResult actual=%d/%d "
           "redirected=%d restoring=%d",
          actualStage, actualSector, redirected ? 1 : 0, restoring ? 1 : 0);

    if (restoring && actualStage == sendStage && actualSector == sendSector) {
        NSLog(@"#pc  BattleRollback.Restored stage=%d/%d",
              actualStage, actualSector);
        gBattleRollbackBaseStage = -1;
        gBattleRollbackBaseSector = -1;
        gBattleRollbackTargetStage = -1;
        gBattleRollbackTargetSector = -1;
        gBattleRollbackRestorePending = false;
    }

}

static void pc_battleStartStageRequest(int32_t attribute, bool changeStage,
                                       const void *method) {
    int32_t stage = PCBattleCurrentStage();
    int32_t sector = PCBattleCurrentSector();
    gBattleRollbackLastRequestedStage = stage;
    gBattleRollbackLastRequestedSector = sector;
    NSLog(@"#pc  BattleRollback.Request observe=%d/%d attr=%d "
           "changeStage=%d repeat=%d state=%d reason=%d wave=%d",
          stage, sector, attribute, changeStage ? 1 : 0,
          PCBattleCurrentRepeat(), PCBattleCurrentState(),
          PCBattleCurrentReason(), PCBattleCurrentWave());
    orig_battleStartStageRequest(attribute, changeStage, method);
}

static void pc_battleEndStageRequest(int32_t reason, int32_t battleState,
                                     int64_t damage, int64_t sendDamage,
                                     int64_t receiveDamage, void *callback,
                                     const void *method) {
    // Clear == 1.  A clear intentionally leaves the current rollback target
    // untouched, so every later request keeps farming the same stage.
    if (reason == 2 || reason == 3 || reason == 4) {
        PCBattleRecordDefeat(reason);
    } else if (reason == 1 && PCBattleRollbackEnabled() &&
               gBattleRollbackTargetStage > 0 &&
               gBattleRollbackTargetSector > 0) {
        NSLog(@"#pc  BattleRollback.Clear keep=%d/%d base=%d/%d",
              gBattleRollbackTargetStage, gBattleRollbackTargetSector,
              gBattleRollbackBaseStage, gBattleRollbackBaseSector);
    }
    orig_battleEndStageRequest(reason, battleState, damage, sendDamage,
                               receiveDamage, callback, method);
}

static bool pc_firewallStartDungeonMoveNext(void *iterator,
                                            const void *method) {
    if (!iterator || *(int32_t *)((uint8_t *)iterator + 0x10) != 0) {
        return orig_firewallStartDungeonMoveNext(iterator, method);
    }

    void *ready = *(void **)((uint8_t *)iterator + 0x20);
    void *data = ready ? *(void **)((uint8_t *)ready + 0x98) : nullptr;
    void *stage = data ? *(void **)((uint8_t *)data + 0x58) : nullptr;
    if (!ready || !stage || !gGameInfoInstance || !gGameInfoPlayDungeon) {
        NSLog(@"#pc  FirewallDungeon.DirectStart fallback iterator=%p ready=%p "
              "stage=%p gameInfo=%p",
              iterator, ready, stage, gGameInfoInstance);
        return orig_firewallStartDungeonMoveNext(iterator, method);
    }

    int32_t stageIndex = *(int32_t *)((uint8_t *)stage + 0x2C);
    int32_t level = *(int32_t *)((uint8_t *)ready + 0xA0);
    *(int32_t *)((uint8_t *)iterator + 0x10) = -1;
    NSLog(@"#pc  FirewallDungeon.DirectStart stage=%d sector=1 level=%d",
          stageIndex, level);
    gGameInfoPlayDungeon(gGameInfoInstance, stageIndex, 1, level, nullptr);
    return false;
}

// UIMainScene.CreateRelationEmoji is reached only after the partner-relation
// cooldown has expired and the main-battle HUD has made the random "care"
// icon active.  Reuse TouchRelationEmoji after creation; it performs the same
// object/cooldown checks as a physical tap and then sends
// PS_PartnerRelationExp.Request.  The short guard protects against duplicate
// requests if the same UI instance is refreshed more than once in a frame.
static void (*orig_mainSceneCreateRelationEmoji)(void *, const void *);
static bool (*gMainSceneTouchRelationEmoji)(void *, const void *);
static void *gLastAutoCareEmoji;
static NSTimeInterval gLastAutoCareRequestTime;

static void pc_mainSceneCreateRelationEmoji(void *instance, const void *method) {
    orig_mainSceneCreateRelationEmoji(instance, method);
    if (!instance || !gMainSceneTouchRelationEmoji) return;

    void *emoji = *(void **)((uint8_t *)instance + 0xE8);
    if (!emoji) return;

    NSTimeInterval now = NSDate.date.timeIntervalSince1970;
    if (emoji == gLastAutoCareEmoji && now - gLastAutoCareRequestTime < 2.0) {
        return;
    }

    if (gMainSceneTouchRelationEmoji(instance, nullptr)) {
        gLastAutoCareEmoji = emoji;
        gLastAutoCareRequestTime = now;
        NSLog(@"#pc  PartnerRelation.AutoCare requested scene=%p emoji=%p",
              instance, emoji);
    }
}

// The main-scene task box is tp.UIGuideQuestInfo. SetData stores its current
// QuestInfo at +0x28 and toggles the completed prompt GameObject at +0x40.
// Its click handler only calls PS_QuestComplete.Request when the quest is
// complete, so perform that same request directly and keep the prompt hidden.
static bool (*gQuestInfoIsComplete)(void *, const void *);
static bool (*gQuestInfoIsGetReward)(void *, const void *);
static int32_t (*gQuestInfoGetKey)(void *, const void *);
static void (*gQuestCompleteRequest)(void *, const void *);
static void (*gGameObjectSetActive)(void *, bool, const void *);
static void (*orig_guideQuestSetData)(void *, const void *);
static int32_t gLastAutoClaimQuestKey = -1;
static NSTimeInterval gLastAutoClaimRequestTime;

static void pc_guideQuestSetData(void *instance, const void *method) {
    orig_guideQuestSetData(instance, method);
    if (!instance) return;

    void *completePrompt = *(void **)((uint8_t *)instance + 0x40);
    if (completePrompt && gGameObjectSetActive) {
        gGameObjectSetActive(completePrompt, false, nullptr);
    }

    void *info = *(void **)((uint8_t *)instance + 0x28);
    if (!info || !gQuestInfoIsComplete || !gQuestInfoIsGetReward ||
        !gQuestInfoGetKey || !gQuestCompleteRequest) {
        return;
    }
    if (!gQuestInfoIsComplete(info, nullptr) ||
        gQuestInfoIsGetReward(info, nullptr)) {
        return;
    }

    int32_t key = gQuestInfoGetKey(info, nullptr);
    NSTimeInterval now = NSDate.date.timeIntervalSince1970;
    if (key == gLastAutoClaimQuestKey && now - gLastAutoClaimRequestTime < 10.0) {
        return;
    }
    gLastAutoClaimQuestKey = key;
    gLastAutoClaimRequestTime = now;
    NSLog(@"#pc  GuideQuest.AutoClaim key=%d info=%p", key, info);
    gQuestCompleteRequest(info, nullptr);
}

static const char *PCUnityLogLevelName(int32_t level) {
    switch (level) {
        case 0: return "Error";
        case 1: return "Assert";
        case 2: return "Warning";
        case 3: return "Log";
        case 4: return "Exception";
        default: return "Unknown";
    }
}

static void (*orig_unityInternalLog)(int32_t, int32_t, void *, void *, const void *);
static void pc_unityInternalLog(int32_t level, int32_t options, void *message,
                                void *context, const void *method) {
    @autoreleasepool {
        NSLog(@"#pc  UNITY level=%s(%d) options=%d context=%p message=%@",
              PCUnityLogLevelName(level), level, options, context,
              PCStringFromIl2Cpp(message));
    }
    orig_unityInternalLog(level, options, message, context, method);
}

static void (*orig_unityInternalLogException)(void *, void *, const void *);
static void pc_unityInternalLogException(void *exception, void *context,
                                         const void *method) {
    char exceptionClass[192] = {};
    char message[1024] = {};
    char stack[3072] = {};
    PCDescribeManagedException(exception, exceptionClass, sizeof(exceptionClass),
                               message, sizeof(message), stack, sizeof(stack));
    @autoreleasepool {
        NSLog(@"#pc  UNITY level=Exception exception=%p context=%p class=%s "
              "message=%s stack=%s",
              exception, context, exceptionClass, message, stack);
    }
    orig_unityInternalLogException(exception, context, method);
}

static void (*orig_exceptionManagerUnhandled)(void *, void *, void *,
                                              const void *);
static void pc_exceptionManagerUnhandled(void *instance, void *sender,
                                         void *eventArguments,
                                         const void *method) {
    void *exception = nullptr;
    bool terminating = false;
    if (PCReadableRange(eventArguments, 0x19)) {
        exception = *(void **)((uint8_t *)eventArguments + 0x10);
        terminating = *(bool *)((uint8_t *)eventArguments + 0x18);
    }
    PCAppendManagedExceptionReport("tp.ExceptionManager", exception,
                                   terminating);
    orig_exceptionManagerUnhandled(instance, sender, eventArguments, method);
}

static void (*orig_unityUnhandledException)(void *, void *, const void *);
static void pc_unityUnhandledException(void *sender, void *eventArguments,
                                       const void *method) {
    void *exception = nullptr;
    bool terminating = false;
    if (PCReadableRange(eventArguments, 0x19)) {
        exception = *(void **)((uint8_t *)eventArguments + 0x10);
        terminating = *(bool *)((uint8_t *)eventArguments + 0x18);
    }
    PCAppendManagedExceptionReport("UnityEngine.UnhandledExceptionHandler",
                                   exception, terminating);
    orig_unityUnhandledException(sender, eventArguments, method);
}

static void (*orig_unityIOSNativeUnhandledException)(void *, void *, void *,
                                                      const void *);
static void pc_unityIOSNativeUnhandledException(void *managedExceptionType,
                                                void *managedExceptionMessage,
                                                void *managedExceptionStack,
                                                const void *method) {
    char exceptionType[512] = {};
    char exceptionMessage[2048] = {};
    char exceptionStack[4096] = {};
    PCUTF8FromIl2CppString(managedExceptionType, exceptionType,
                          sizeof(exceptionType));
    PCUTF8FromIl2CppString(managedExceptionMessage, exceptionMessage,
                          sizeof(exceptionMessage));
    PCUTF8FromIl2CppString(managedExceptionStack, exceptionStack,
                          sizeof(exceptionStack));
    char report[7168] = {};
    snprintf(report, sizeof(report),
             "UnityCrash.iOSNativeUnhandled type=%s message=%s stack=%s",
             exceptionType[0] ? exceptionType : "(unknown)",
             exceptionMessage[0] ? exceptionMessage : "(no message)",
             exceptionStack[0] ? exceptionStack : "(no managed stack)");
    PCAppendRawCrashLine(report, true);
    pthread_mutex_lock(&gPersistentLogLock);
    if (gPersistentLogFD >= 0) fsync(gPersistentLogFD);
    if (gUnityCrashHistoryFD >= 0) fsync(gUnityCrashHistoryFD);
    pthread_mutex_unlock(&gPersistentLogLock);
    orig_unityIOSNativeUnhandledException(
        managedExceptionType, managedExceptionMessage, managedExceptionStack,
        method);
}

static void PCInstallUnityHooks(intptr_t slide) {
    if (gUnityHooksInstalled) return;
    gUnityHooksInstalled = true;
    NSLog(@"#pc  UnityFramework slide=0x%lx", (long)slide);

    // UIItemSpawnerInfo.<OnResponseSpawn>b__0: force its 1.7s/0.9s
    // auto-open delay selection to a single 0.5s value.
    PCPatchInstruction(slide, 0x2FB7EDC, 0x1E20CC20, 0x1E2C1000,
                       "UIItemSpawnerInfo.auto_open_delay_0.5s");

    gQuestInfoIsComplete =
        (bool (*)(void *, const void *))(slide + 0x2DEB528);
    gQuestInfoIsGetReward =
        (bool (*)(void *, const void *))(slide + 0x2DEB620);
    gQuestInfoGetKey =
        (int32_t (*)(void *, const void *))(slide + 0x2DEC24C);
    gQuestCompleteRequest =
        (void (*)(void *, const void *))(slide + 0x2EEE1E0);
    gGameObjectSetActive =
        (void (*)(void *, bool, const void *))(slide + 0x6A3CBFC);
    gUILoginStartLoginRequest =
        (void (*)(void *, int32_t, const void *))(slide + 0x326D084);
    gUnityObjectImplicit =
        (bool (*)(void *, const void *))(slide + 0x6A44F34);
    gUnityObjectDestroy =
        (void (*)(void *, const void *))(slide + 0x6A468A4);
    gComponentGetGameObject =
        (void *(*)(void *, const void *))(slide + 0x6A36418);
    gGameObjectGetActiveInHierarchy =
        (bool (*)(void *, const void *))(slide + 0x6A3CE00);
    gItemInfoGetType =
        (int32_t (*)(void *, const void *))(slide + 0x2DB37A8);
    gItemInfoGetStringUID =
        (void *(*)(void *, const void *))(slide + 0x2DB3754);
    gItemEquipRequest =
        (void (*)(int32_t, void *, bool, const void *))(slide + 0x2EC89F0);
    gItemSellRequest =
        (void (*)(void *, const void *))(slide + 0x2EC95F8);
    gItemSelectClose =
        (void (*)(void *, const void *))(slide + 0x2FB03B8);
    gMainSceneTouchRelationEmoji =
        (bool (*)(void *, const void *))(slide + 0x31A084C);
    gGameInfoPlayDungeon =
        (void (*)(void *, int32_t, int32_t, int32_t, const void *))
            (slide + 0x2DA741C);
    gMineRowItemEnableMove =
        (void (*)(void *, bool, const void *))(slide + 0x30939C4);
    gMineRowItemRequestMoveCell =
        (bool (*)(void *, const void *))(slide + 0x3093BD0);
    gMineRowItemRequestDrill =
        (bool (*)(void *, const void *))(slide + 0x3094090);
    gMineCellInfoGetCol =
        (int32_t (*)(void *, const void *))(slide + 0x2DBDE2C);
    gMineCellInfoGetRow =
        (int32_t (*)(void *, const void *))(slide + 0x2DBDE74);
    gMineCellInfoGetType =
        (int32_t (*)(void *, const void *))(slide + 0x2DBDEBC);
    gMineScrollViewGetCellItem =
        (void *(*)(void *, int32_t, int32_t, const void *))
            (slide + 0x3090228);
    gMineInfosRequest =
        (void (*)(const void *))(slide + 0x2EDC14C);
    gUIGardenMineSetData =
        (void (*)(void *, bool, const void *))(slide + 0x308DF38);
    gMineInfoGetCol =
        (int32_t (*)(void *, const void *))(slide + 0x2DBE320);
    gMineInfoGetRow =
        (int32_t (*)(void *, const void *))(slide + 0x2DBE360);
    gMineInfoGetDistance =
        (int32_t (*)(void *, const void *))(slide + 0x2DBE3A0);
    gUIContentsSceneCloseGrowthGuide =
        (void (*)(void *, const void *))(slide + 0x319C7F8);
    gBattleInfoParamClientGetStage =
        (int32_t (*)(void *, const void *))(slide + 0x2D7A13C);
    gBattleInfoParamClientGetSector =
        (int32_t (*)(void *, const void *))(slide + 0x2D7A158);
    gBattleInfoParamClientGetRepeat =
        (int32_t (*)(void *, const void *))(slide + 0x2D7A104);
    gBattleInfoParamClientGetBattleState =
        (int32_t (*)(void *, const void *))(slide + 0x2D7A078);
    gBattleInfoParamClientSetBattleState =
        (void (*)(void *, int32_t, const void *))(slide + 0x2D7A094);
    gBattleInfoParamClientGetReason =
        (int32_t (*)(void *, const void *))(slide + 0x2D7A0B0);
    gBattleInfoParamClientSetReason =
        (void (*)(void *, int32_t, const void *))(slide + 0x2D7A0CC);
    gBattleInfoParamClientGetWave =
        (int32_t (*)(void *, const void *))(slide + 0x2D7A174);
    gBattleInfoParamClientSetWave =
        (void (*)(void *, int32_t, const void *))(slide + 0x2D7A190);
    gBattleInfoParamClientGetRegion =
        (void *(*)(void *, const void *))(slide + 0x2D7B074);
    gDataInfoRegionGetStage =
        (void *(*)(void *, int32_t, const void *))(slide + 0x2E09F64);
    gDataInfoStageGetSectorCount =
        (int32_t (*)(void *, const void *))(slide + 0x2E09E50);

    PCHook((void *)(slide + 0xE51644), (void *)pc_jailbreakCheck,
           (void **)&orig_jailbreakCheck, "native_jailbreak_check_0xE51644");
    PCHook((void *)(slide + 0x329A278), (void *)pc_globalQuit,
           (void **)&orig_globalQuit, "GlobalObject.Quit_0x329A278");
    PCHook((void *)(slide + 0x69A5E4C), (void *)pc_applicationQuit,
           (void **)&orig_applicationQuit, "Application.Quit_0x69A5E4C");
    PCHook((void *)(slide + 0x329D33C), (void *)pc_obscuredCheater,
           (void **)&orig_obscuredCheater, "OnObscuredCheaterDetected_0x329D33C");
    PCHook((void *)(slide + 0x329D66C), (void *)pc_speedCheater,
           (void **)&orig_speedCheater, "OnSpeedCheaterDetected_0x329D66C");
    PCHook((void *)(slide + 0x329DA08), (void *)pc_timeCheater,
           (void **)&orig_timeCheater, "OnTimeCheaterDetected_0x329DA08");
    PCHook((void *)(slide + 0x326412C), (void *)pc_banProcess,
           (void **)&orig_banProcess, "LoginScene.BanProcess_0x326412C");
    PCHook((void *)(slide + 0x3264318), (void *)pc_banPopupProcess,
           (void **)&orig_banPopupProcess, "LoginScene.BanPopupProcess_0x3264318");
    PCHook((void *)(slide + 0x2E61D4C), (void *)pc_banInfoRequest,
           (void **)&orig_banInfoRequest, "PS_BanInfo.Request_0x2E61D4C");
    PCHook((void *)(slide + 0x2E626E4), (void *)pc_integrityRequest,
           (void **)&orig_integrityRequest, "PS_Integrity.Request_0x2E626E4");
    PCHook((void *)(slide + 0x2E62894), (void *)pc_integrityError,
           (void **)&orig_integrityError, "PS_Integrity.OnErrorCallback_0x2E62894");
    PCHook((void *)(slide + 0x2DBC9D0), (void *)pc_timeRewardGetRemainTime,
           (void **)&orig_timeRewardGetRemainTime,
           "TimeRewardListParam.GetRemainTime_AdRemove_0x2DBC9D0");
    PCHook((void *)(slide + 0x2ED0D94), (void *)pc_authSetPaketData,
           (void **)&orig_authSetPaketData,
           "PS_Auth.SetPaketData_capture_login_0x2ED0D94");
    PCHook((void *)(slide + 0x2ED1990), (void *)pc_authResponse,
           (void **)&orig_authResponse,
           "PS_Auth.ResponseData.Response_capture_client_0x2ED1990");
    PCHook((void *)(slide + 0x2ED410C), (void *)pc_loginResponse,
           (void **)&orig_loginResponse,
           "LoginResponseData.Response_capture_server_0x2ED410C");
    PCHook((void *)(slide + 0x326CB94), (void *)pc_uiLoginShowStartButton,
           (void **)&orig_uiLoginShowStartButton,
           "UILogin.ShowStartButton_auto_start_0x326CB94");
    PCHook((void *)(slide + 0x2F1C26C), (void *)pc_openNoticeMoveNext,
           (void **)&orig_openNoticeMoveNext,
           "MainScene.OpenNotice.MoveNext_skip_0x2F1C26C");
    PCHook((void *)(slide + 0x2F1BE2C), (void *)pc_openLoginBonusMoveNext,
           (void **)&orig_openLoginBonusMoveNext,
           "MainScene.OpenLoginBonus.MoveNext_skip_0x2F1BE2C");
    PCHook((void *)(slide + 0x2F1AF08), (void *)pc_openAFKMoveNext,
           (void **)&orig_openAFKMoveNext,
           "MainScene.OpenAFK.MoveNext_skip_0x2F1AF08");
    PCHook((void *)(slide + 0x2F1CBF0), (void *)pc_openTimeDealMoveNext,
           (void **)&orig_openTimeDealMoveNext,
           "MainScene.OpenTimeDeal.MoveNext_skip_0x2F1CBF0");
    PCHook((void *)(slide + 0x320F448), (void *)pc_popupRewardOnBack,
           (void **)&orig_popupRewardOnBack,
           "UIPopupReward.OnBack_auto_close_0x320F448");
    PCHook((void *)(slide + 0x320F878), (void *)pc_popupRewardShowComplete,
           (void **)&orig_popupRewardShowComplete,
           "UIPopupReward.ShowCompete_auto_close_0x320F878");
    PCHook((void *)(slide + 0x5E35454),
           (void *)pc_navMeshSurfaceBuildNavMesh,
           (void **)&orig_navMeshSurfaceBuildNavMesh,
           "NavMeshSurface.BuildNavMesh_destroy_replaced_data_0x5E35454");
    PCHook((void *)(slide + 0x2FB8258), (void *)pc_itemSpawnerResultMoveNext,
           (void **)&orig_itemSpawnerResultMoveNext,
           "UIItemSpawnerInfo.C_Result.MoveNext_auto_equip_0x2FB8258");
    PCHook((void *)(slide + 0x2FAF194), (void *)pc_itemSelectSetData,
           (void **)&orig_itemSelectSetData,
           "UIItemSelect.SetData_item_spawner_auto_equip_0x2FAF194");
    PCHook((void *)(slide + 0x2EC8D70), (void *)pc_itemEquipResponse,
           (void **)&orig_itemEquipResponse,
           "PS_ItemEquip.ResponseData.Response_sell_old_0x2EC8D70");
    PCHook((void *)(slide + 0x2EC9C70), (void *)pc_itemSellResponse,
           (void **)&orig_itemSellResponse,
           "PS_ItemSell.ResponseData.Response_resume_spawn_0x2EC9C70");
    PCHook((void *)(slide + 0x2FB3A74), (void *)pc_itemSpawnerSpawnItem,
           (void **)&orig_itemSpawnerSpawnItem,
           "UIItemSpawnerInfo.SpawnItem_wait_old_sell_0x2FB3A74");
    PCHook((void *)(slide + 0x2DACEBC),
           (void *)pc_gameInfoUpdateBattleStart,
           (void **)&orig_gameInfoUpdateBattleStart,
           "GameInfo.UpdateBattleStart_capture_0x2DACEBC");
    // Battle rollback / stage override is temporarily disabled.  The hooks
    // remain in source so the experiment can be restored without reconstructing
    // the call chain.
#if 0
    PCHook((void *)(slide + 0x2DA7BD4),
           (void *)pc_gameInfoPlayBattle,
           (void **)&orig_gameInfoPlayBattle,
           "GameInfo.PlayBattle_rollback_root_0x2DA7BD4");
    PCHook((void *)(slide + 0x2E70C08),
           (void *)pc_battleStartStageRequest,
           (void **)&orig_battleStartStageRequest,
           "PS_BattleStart_Stage.Request_rollback_0x2E70C08");
    PCHook((void *)(slide + 0x2E738BC),
           (void *)pc_battleEndStageRequest,
           (void **)&orig_battleEndStageRequest,
           "PS_BattleEnd_Stage.Request_rollback_0x2E738BC");
#endif
    PCHook((void *)(slide + 0x303B55C),
           (void *)pc_firewallStartDungeonMoveNext,
           (void **)&orig_firewallStartDungeonMoveNext,
           "UIDungeonReady_Firewall.StartDungeon.MoveNext_direct_0x303B55C");
    PCHook((void *)(slide + 0x31A033C),
           (void *)pc_mainSceneCreateRelationEmoji,
           (void **)&orig_mainSceneCreateRelationEmoji,
           "UIMainScene.CreateRelationEmoji_auto_care_0x31A033C");
    PCHook((void *)(slide + 0x32117D0), (void *)pc_guideQuestSetData,
           (void **)&orig_guideQuestSetData,
           "UIGuideQuestInfo.SetData_auto_claim_0x32117D0");
    PCHook((void *)(slide + 0x30935F8), (void *)pc_mineRowItemSetState,
           (void **)&orig_mineRowItemSetState,
           "UIGardenMineRowItem.SetState_enable_all_0x30935F8");
    PCHook((void *)(slide + 0x30943D4), (void *)pc_mineRowItemEventClick,
           (void **)&orig_mineRowItemEventClick,
           "UIGardenMineRowItem.Event_Click_direct_move_0x30943D4");
    PCHook((void *)(slide + 0x308F68C), (void *)pc_uiGardenMineMove,
           (void **)&orig_uiGardenMineMove,
           "UIGardenMine.Move_refresh_far_target_0x308F68C");
    PCHook((void *)(slide + 0x2EDC2CC), (void *)pc_mineInfosResponse,
           (void **)&orig_mineInfosResponse,
           "PS_MineInfos.Response_refresh_far_target_0x2EDC2CC");
    PCHook((void *)(slide + 0x3145AE8), (void *)pc_battleProcessorStateDefeat,
           (void **)&orig_battleProcessorStateDefeat,
           "BattleProcessor.State_Defeat_mark_0x3145AE8");
    PCHook((void *)(slide + 0x319C11C),
           (void *)pc_uiContentsSceneShowGrowthGuide,
           (void **)&orig_uiContentsSceneShowGrowthGuide,
           "UIContentsScene.ShowGrowthGuide_auto_close_failure_0x319C11C");

    PCHook((void *)(slide + 0x69BA12C), (void *)pc_unityInternalLog,
           (void **)&orig_unityInternalLog, "DebugLogHandler.Internal_Log_0x69BA12C");
    PCHook((void *)(slide + 0x69BA37C), (void *)pc_unityInternalLogException,
           (void **)&orig_unityInternalLogException,
           "DebugLogHandler.Internal_LogException_0x69BA37C");
    PCHook((void *)(slide + 0x3282E24),
           (void *)pc_exceptionManagerUnhandled,
           (void **)&orig_exceptionManagerUnhandled,
           "ExceptionManager.HandleUnhandledException_0x3282E24");
    PCHook((void *)(slide + 0x6A443F4),
           (void *)pc_unityUnhandledException,
           (void **)&orig_unityUnhandledException,
           "Unity.UnhandledExceptionHandler.Handle_0x6A443F4");
    PCHook((void *)(slide + 0x6A44624),
           (void *)pc_unityIOSNativeUnhandledException,
           (void **)&orig_unityIOSNativeUnhandledException,
           "Unity.iOSNativeUnhandledExceptionHandler_0x6A44624");
}

static void PCImageAdded(const struct mach_header *header, intptr_t slide) {
    Dl_info info = {};
    if (!dladdr(header, &info) || !info.dli_fname) return;
    const char *name = info.dli_fname;
    if (strstr(name, "/UnityFramework.framework/UnityFramework")) {
        PCInstallUnityHooks(slide);
    }
}

static void PCInstallSymbolHooks(void) {
    void *(*lookup)(void *, const char *) = dlsym;
    gHookFunction = (MSHookFunctionType)lookup(RTLD_DEFAULT, "MSHookFunction");
    gHookMemory = (MSHookMemoryType)lookup(RTLD_DEFAULT, "MSHookMemory");
    NSLog(@"#pc  symbol hooker=%p memory=%p", gHookFunction, gHookMemory);
    if (!gHookFunction) return;

#define PC_HOOK_SYMBOL(symbolName, replacement, original) \
    PCHook(lookup(RTLD_DEFAULT, symbolName), (void *)replacement, (void **)&original, symbolName)
    PC_HOOK_SYMBOL("access", pc_access, orig_access);
    PC_HOOK_SYMBOL("stat", pc_stat, orig_stat);
    PC_HOOK_SYMBOL("lstat", pc_lstat, orig_lstat);
    PC_HOOK_SYMBOL("fopen", pc_fopen, orig_fopen);
    PC_HOOK_SYMBOL("open", pc_open, orig_open);
    PC_HOOK_SYMBOL("dlopen", pc_dlopen, orig_dlopen);
    PC_HOOK_SYMBOL("getenv", pc_getenv, orig_getenv);
    PC_HOOK_SYMBOL("sysctl", pc_sysctl, orig_sysctl);
    PC_HOOK_SYMBOL("sysctlbyname", pc_sysctlbyname, orig_sysctlbyname);
    PC_HOOK_SYMBOL("ptrace", pc_ptrace, orig_ptrace);
    PC_HOOK_SYMBOL("__cxa_throw", pc_cxa_throw, orig_cxa_throw);
    PC_HOOK_SYMBOL("exit", pc_exit, orig_exit);
    PC_HOOK_SYMBOL("_exit", pc__exit, orig__exit);
    PC_HOOK_SYMBOL("abort", pc_abort, orig_abort);
    PC_HOOK_SYMBOL("raise", pc_raise, orig_raise);
    PC_HOOK_SYMBOL("kill", pc_kill, orig_kill);
    // Install dlsym last because lookup itself uses dlsym.
    void *dlsymTarget = lookup(RTLD_DEFAULT, "dlsym");
    PCHook(dlsymTarget, (void *)pc_dlsym, (void **)&orig_dlsym, "dlsym");
#undef PC_HOOK_SYMBOL
}

__attribute__((constructor)) static void PCJBProbeInitialize(void) {
    @autoreleasepool {
        NSString *bundleID = NSBundle.mainBundle.bundleIdentifier;
        if (![bundleID isEqualToString:@"jp.co.bandainamcoent.BNEI0442"]) return;
        PCInitializePersistentLogs();
        NSLog(@"#pc  PCJBProbe loaded bundle=%@ pid=%d", bundleID, getpid());
        NSLog(@"#pc  persistent logs pc=%@ unity_native=%@",
              gPersistentLogPath, gUnityNativeLogPath);
        NSLog(@"#pc  crash history=%@", gUnityCrashHistoryPath);

        NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
        id savedSkipTouch = [defaults objectForKey:PCSkipTouchToStartDefaultsKey];
        bool skipTouchEnabled =
            savedSkipTouch ? [defaults boolForKey:PCSkipTouchToStartDefaultsKey] : true;
        PCSetSkipTouchToStartEnabled(skipTouchEnabled, false);
        NSLog(@"#pc  PluginSetting.SkipTouchToStart loaded=%d source=%s",
              skipTouchEnabled ? 1 : 0, savedSkipTouch ? "saved" : "default");
        // Battle rollback / stage override is temporarily disabled.
#if 0
        id savedBattleRollback =
            [defaults objectForKey:PCBattleRollbackDefaultsKey];
        bool battleRollbackEnabled = savedBattleRollback
            ? [defaults boolForKey:PCBattleRollbackDefaultsKey] : false;
        PCSetBattleRollbackEnabled(battleRollbackEnabled, false);
        NSLog(@"#pc  PluginSetting.BattleRollback loaded=%d source=%s",
              battleRollbackEnabled ? 1 : 0,
              savedBattleRollback ? "saved" : "default");
#endif

        PCInstallSymbolHooks();
        PCHookMessage(NSFileManager.class, @selector(fileExistsAtPath:),
                      (IMP)pc_fileExistsAtPath, (IMP *)&orig_fileExistsAtPath,
                      "-[NSFileManager fileExistsAtPath:]");
        PCHookMessage(NSFileManager.class, @selector(fileExistsAtPath:isDirectory:),
                      (IMP)pc_fileExistsAtPathIsDirectory,
                      (IMP *)&orig_fileExistsAtPathIsDirectory,
                      "-[NSFileManager fileExistsAtPath:isDirectory:]");
        PCHookMessage(UIApplication.class, @selector(canOpenURL:),
                      (IMP)pc_canOpenURL, (IMP *)&orig_canOpenURL,
                      "-[UIApplication canOpenURL:]");

        _dyld_register_func_for_add_image(PCImageAdded);
        PCStartPluginOverlay();
        NSLog(@"#pc  PCJBProbe initialization complete");
    }
}
