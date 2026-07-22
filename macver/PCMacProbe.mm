#import <Foundation/Foundation.h>
#import <UIKit/UIKit.h>
#import <objc/runtime.h>
#import <mach/mach.h>
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
#import <execinfo.h>
#include <atomic>
#include <math.h>
#include <typeinfo>
#import "dobby.h"

static __thread bool gInLog;
static __thread bool gEffectManagerClearing;
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

static uint64_t PCMemoryFootprintMB(void) {
    task_vm_info_data_t info = {};
    mach_msg_type_number_t count = TASK_VM_INFO_COUNT;
    kern_return_t result = task_info(mach_task_self_, TASK_VM_INFO,
                                     (task_info_t)&info, &count);
    if (result != KERN_SUCCESS) return 0;
    return (uint64_t)(info.phys_footprint / (1024ULL * 1024ULL));
}

static uint64_t gBundleLoadRequests;
static uint64_t gBundleLoads;
static uint64_t gBundleUnloads;
static uint64_t gAssetLoadRequests;
static uint64_t gAssetLoadCompletes;
static uint64_t gBundleProviderReleases;
static uint64_t gAddressablePoolInstancesReleased;
static uint64_t gAddressablePoolInstancesNotTracked;
static uint64_t gEffectPoolInstancesReleased;

// GameObjectPool<T> keeps an Addressables-created prefab instance in its
// _original field.  The game destroys that object when the pool is cleared,
// but does not release the tracked Addressables instance handle.  Keep only
// diagnostic path/count data here; managed Unity objects are never retained by
// Objective-C collections.
static NSMutableDictionary<NSValue *, NSString *> *gProjectilePoolPaths;
static NSMutableDictionary<NSString *, NSNumber *> *gProjectilePoolInitCounts;
static NSMutableDictionary<NSString *, NSNumber *> *gProjectileAssetRequestCounts;

static uint64_t PCIncrement(uint64_t *value) {
    return __atomic_add_fetch(value, 1, __ATOMIC_RELAXED);
}

static long long PCBundleLiveEstimate(void) {
    uint64_t loads = __atomic_load_n(&gBundleLoads, __ATOMIC_RELAXED);
    uint64_t unloads = __atomic_load_n(&gBundleUnloads, __ATOMIC_RELAXED);
    return (long long)loads - (long long)unloads;
}

static bool PCIsProjectilePrefabPath(NSString *path) {
    if (!path.length) return false;
    return [path rangeOfString:@"Projectile.prefab"
                       options:NSCaseInsensitiveSearch].location != NSNotFound;
}

static NSString *PCProjectilePoolPath(void *pool) {
    if (!pool || !gProjectilePoolPaths) return nil;
    @synchronized (gProjectilePoolPaths) {
        return gProjectilePoolPaths[[NSValue valueWithPointer:pool]];
    }
}

static uint64_t PCTrackProjectilePool(void *pool, NSString *path) {
    if (!pool || !PCIsProjectilePrefabPath(path)) return 0;
    @synchronized (gProjectilePoolPaths) {
        gProjectilePoolPaths[[NSValue valueWithPointer:pool]] = [path copy];
        uint64_t count = [gProjectilePoolInitCounts[path] unsignedLongLongValue] + 1;
        gProjectilePoolInitCounts[path] = @(count);
        return count;
    }
}

static uint64_t PCTrackProjectileAssetRequest(NSString *path) {
    if (!PCIsProjectilePrefabPath(path)) return 0;
    @synchronized (gProjectileAssetRequestCounts) {
        uint64_t count =
            [gProjectileAssetRequestCounts[path] unsignedLongLongValue] + 1;
        gProjectileAssetRequestCounts[path] = @(count);
        return count;
    }
}

static void PCUntrackProjectilePool(void *pool) {
    if (!pool || !gProjectilePoolPaths) return;
    @synchronized (gProjectilePoolPaths) {
        [gProjectilePoolPaths removeObjectForKey:[NSValue valueWithPointer:pool]];
    }
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
            stringByAppendingPathComponent:@"Library/Caches/PCMacProbe"];
        NSFileManager *manager = NSFileManager.defaultManager;
        [manager createDirectoryAtPath:directory
           withIntermediateDirectories:YES
                            attributes:nil
                                 error:nil];

        gPersistentLogPath = [directory stringByAppendingPathComponent:@"PCMacProbe-current.log"];
        NSString *previous = [directory stringByAppendingPathComponent:@"PCMacProbe-previous.log"];
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

// MARK: - Game speed and in-game overlay

static NSString *const PCSpeedDefaultsKey = @"PCMacProbe.speedScale";
static NSString *const PCFloatingEdgeLeftKey = @"PCMacProbe.floatingEdgeLeft";
static NSString *const PCFloatingYRatioKey = @"PCMacProbe.floatingYRatio";
static const NSTimeInterval kPCFloatingCollapseDelay = 2.5;
static const CGFloat kPCFloatingButtonSize = 54.0;
static const CGFloat kPCFloatingCollapsedVisible = 0.40;
static const CGFloat kPCFloatingCollapsedAlpha = 0.45;
static std::atomic<float> gSpeedScale { 1.0f };
static std::atomic<bool> gLoggedFirstSpeedIntercept { false };
static void (*orig_setTimeScale)(float);

static float PCNormalizedSpeed(float value) {
    float rounded = roundf(value);
    if (rounded < 1.0f) return 1.0f;
    if (rounded > 10.0f) return 10.0f;
    return rounded;
}

static float PCCurrentSpeed(void) {
    return gSpeedScale.load(std::memory_order_relaxed);
}

static void PCSetSpeed(float value, bool persist, bool applyNow) {
    float speed = PCNormalizedSpeed(value);
    float previous = gSpeedScale.exchange(speed, std::memory_order_relaxed);
    if (persist) {
        [NSUserDefaults.standardUserDefaults setInteger:(NSInteger)speed
                                                 forKey:PCSpeedDefaultsKey];
    }
    if (applyNow && orig_setTimeScale) {
        orig_setTimeScale(speed);
    }
    if (previous != speed || applyNow) {
        NSLog(@"#pc  game speed set value=%.0fx persisted=%d applied=%d",
              speed, persist, applyNow && orig_setTimeScale != nullptr);
    }
}

static void pc_setTimeScale(float requestedScale) {
    float speed = PCCurrentSpeed();
    if (!gLoggedFirstSpeedIntercept.exchange(true, std::memory_order_relaxed)) {
        NSLog(@"#pc  Time.set_timeScale first intercept requested=%g forced=%g",
              requestedScale, speed);
    }
    orig_setTimeScale(speed);
}

@interface PCSpeedOverlay : NSObject
@property(nonatomic, strong) UIButton *floatingButton;
@property(nonatomic, strong) UIView *panel;
@property(nonatomic, strong) UILabel *speedLabel;
@property(nonatomic, strong) UISlider *speedSlider;
@property(nonatomic, weak) UIWindow *hostWindow;
@property(nonatomic, assign) BOOL floatingButtonWasDragged;
@property(nonatomic, assign) BOOL floatingCollapsed;
@property(nonatomic, assign) BOOL floatingPreferLeft;
@property(nonatomic, assign) NSUInteger floatingDockGeneration;
+ (instancetype)sharedOverlay;
- (void)installWhenReady;
@end

@implementation PCSpeedOverlay

+ (instancetype)sharedOverlay {
    static PCSpeedOverlay *overlay;
    static dispatch_once_t onceToken;
    dispatch_once(&onceToken, ^{
        overlay = [PCSpeedOverlay new];
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
                scene.activationState != UISceneActivationStateForegroundInactive) continue;
            for (UIWindow *window in ((UIWindowScene *)scene).windows) {
                if (window.hidden || window.alpha <= 0.0) continue;
                if (window.isKeyWindow) return window;
                if (!fallback) fallback = window;
            }
        }
    }
    if (!fallback) {
        for (UIWindow *window in application.windows) {
            if (window.hidden || window.alpha <= 0.0) continue;
            if (window.isKeyWindow) return window;
            if (!fallback) fallback = window;
        }
    }
    return fallback;
}

- (void)installWhenReady {
    NSAssert(NSThread.isMainThread, @"PCSpeedOverlay must be installed on main thread");
    UIWindow *window = [self activeGameWindow];
    if (!window) {
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 500 * NSEC_PER_MSEC),
                       dispatch_get_main_queue(), ^{
            [self installWhenReady];
        });
        return;
    }

    if (self.hostWindow == window && self.floatingButton.superview == window) {
        [window bringSubviewToFront:self.panel];
        [window bringSubviewToFront:self.floatingButton];
        if (self.panel.hidden) {
            [self scheduleFloatingCollapse];
        }
        return;
    }

    [self.panel removeFromSuperview];
    [self.floatingButton removeFromSuperview];
    self.hostWindow = window;

    UIView *panel = [[UIView alloc] initWithFrame:CGRectMake(0, 0, 280, 170)];
    panel.backgroundColor = [UIColor colorWithWhite:0.08 alpha:0.94];
    panel.layer.cornerRadius = 16.0;
    panel.layer.borderWidth = 1.0;
    panel.layer.borderColor = [UIColor colorWithWhite:1.0 alpha:0.18].CGColor;
    panel.clipsToBounds = YES;
    panel.hidden = YES;
    panel.accessibilityIdentifier = @"PCMacProbe.speedPanel";

    UILabel *title = [[UILabel alloc] initWithFrame:CGRectMake(18, 14, 200, 25)];
    title.text = @"插件面板";
    title.textColor = UIColor.whiteColor;
    title.font = [UIFont boldSystemFontOfSize:18.0];
    [panel addSubview:title];

    UIButton *close = [UIButton buttonWithType:UIButtonTypeSystem];
    close.frame = CGRectMake(232, 8, 40, 40);
    [close setTitle:@"×" forState:UIControlStateNormal];
    [close setTitleColor:[UIColor colorWithWhite:0.85 alpha:1.0]
                forState:UIControlStateNormal];
    close.titleLabel.font = [UIFont systemFontOfSize:28.0 weight:UIFontWeightLight];
    close.accessibilityLabel = @"关闭插件面板";
    [close addTarget:self action:@selector(togglePanel) forControlEvents:UIControlEventTouchUpInside];
    [panel addSubview:close];

    UILabel *speedLabel = [[UILabel alloc] initWithFrame:CGRectMake(18, 52, 244, 28)];
    speedLabel.textColor = UIColor.whiteColor;
    speedLabel.font = [UIFont monospacedDigitSystemFontOfSize:17.0
                                                      weight:UIFontWeightSemibold];
    [panel addSubview:speedLabel];
    self.speedLabel = speedLabel;

    UISlider *slider = [[UISlider alloc] initWithFrame:CGRectMake(18, 89, 244, 30)];
    slider.minimumValue = 1.0f;
    slider.maximumValue = 10.0f;
    slider.continuous = YES;
    slider.minimumTrackTintColor = [UIColor colorWithRed:0.19 green:0.68 blue:1.0 alpha:1.0];
    slider.maximumTrackTintColor = [UIColor colorWithWhite:1.0 alpha:0.24];
    slider.accessibilityLabel = @"游戏加速度";
    [slider addTarget:self action:@selector(speedSliderChanged:)
       forControlEvents:UIControlEventValueChanged];
    [panel addSubview:slider];
    self.speedSlider = slider;

    UILabel *minimum = [[UILabel alloc] initWithFrame:CGRectMake(18, 123, 50, 22)];
    minimum.text = @"1×";
    minimum.textColor = [UIColor colorWithWhite:0.7 alpha:1.0];
    minimum.font = [UIFont systemFontOfSize:13.0];
    [panel addSubview:minimum];

    UILabel *maximum = [[UILabel alloc] initWithFrame:CGRectMake(212, 123, 50, 22)];
    maximum.text = @"10×";
    maximum.textAlignment = NSTextAlignmentRight;
    maximum.textColor = [UIColor colorWithWhite:0.7 alpha:1.0];
    maximum.font = [UIFont systemFontOfSize:13.0];
    [panel addSubview:maximum];

    UIButton *button = [UIButton buttonWithType:UIButtonTypeCustom];
    button.frame = CGRectMake(0, 0, 54, 54);
    button.backgroundColor = [UIColor colorWithWhite:0.06 alpha:0.88];
    button.layer.cornerRadius = 27.0;
    button.layer.borderWidth = 1.5;
    button.layer.borderColor = [UIColor colorWithRed:0.19 green:0.68 blue:1.0 alpha:0.9].CGColor;
    [button setTitle:@"加速" forState:UIControlStateNormal];
    [button setTitleColor:UIColor.whiteColor forState:UIControlStateNormal];
    button.titleLabel.font = [UIFont boldSystemFontOfSize:14.0];
    button.accessibilityLabel = @"打开插件面板";
    button.accessibilityIdentifier = @"PCMacProbe.floatingButton";
    [button addTarget:self action:@selector(floatingButtonTouchDown:)
       forControlEvents:UIControlEventTouchDown];
    [button addTarget:self action:@selector(floatingButtonTapped:)
       forControlEvents:UIControlEventTouchUpInside];

    UIPanGestureRecognizer *pan = [[UIPanGestureRecognizer alloc]
        initWithTarget:self action:@selector(dragFloatingButton:)];
    pan.cancelsTouchesInView = YES;
    [button addGestureRecognizer:pan];

    self.panel = panel;
    self.floatingButton = button;
    [window addSubview:panel];
    [window addSubview:button];

    button.frame = CGRectMake(0, 0, kPCFloatingButtonSize, kPCFloatingButtonSize);
    self.floatingCollapsed = NO;
    [self restoreFloatingButtonPositionAnimated:NO collapsed:NO];
    [self positionPanel];
    [self refreshSpeedUI];
    [self scheduleFloatingCollapse];

    // Applying here is safe: a foreground game window means Unity has passed
    // its early initialization phase. It also restores the persisted speed.
    PCSetSpeed(PCCurrentSpeed(), false, true);
    NSLog(@"#pc  speed overlay installed window=%p bounds=%@ edgeLeft=%d",
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
        [UIView animateWithDuration:0.22 delay:0 options:UIViewAnimationOptionCurveEaseInOut animations:apply completion:nil];
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
        dispatch_time(DISPATCH_TIME_NOW, (int64_t)(kPCFloatingCollapseDelay * NSEC_PER_SEC)),
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
    BOOL preferLeft = self.floatingPreferLeft;
    if (!collapsed || !self.floatingCollapsed) {
        preferLeft = button.center.x < CGRectGetMidX(window.bounds);
        self.floatingPreferLeft = preferLeft;
    }
    CGPoint center = [self edgeCenterPreferLeft:preferLeft y:button.center.y collapsed:collapsed];
    self.floatingCollapsed = collapsed;
    void (^apply)(void) = ^{
        button.center = center;
        button.alpha = collapsed ? kPCFloatingCollapsedAlpha : 1.0;
    };
    if (animated) {
        [UIView animateWithDuration:0.22 delay:0 options:UIViewAnimationOptionCurveEaseInOut animations:apply completion:nil];
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
    CGPoint center = [self edgeCenterPreferLeft:preferLeft y:button.center.y collapsed:NO];
    [UIView animateWithDuration:0.18 delay:0 options:UIViewAnimationOptionCurveEaseOut animations:^{
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
    CGFloat panelWidth = MIN(280.0, MAX(220.0, availableWidth - 24.0));
    CGFloat x = safe.left + (availableWidth - panelWidth) * 0.5;
    CGFloat y = safe.top + 74.0;
    self.panel.frame = CGRectMake(x, y, panelWidth, 170.0);

    CGFloat contentWidth = panelWidth - 36.0;
    self.speedLabel.frame = CGRectMake(18, 52, contentWidth, 28);
    self.speedSlider.frame = CGRectMake(18, 89, contentWidth, 30);
}

- (void)refreshSpeedUI {
    float speed = PCCurrentSpeed();
    self.speedSlider.value = speed;
    self.speedLabel.text = [NSString stringWithFormat:@"游戏加速度：%.0f×", speed];
    self.floatingButton.accessibilityValue = [NSString stringWithFormat:@"%.0f倍", speed];
}

- (void)togglePanel {
    BOOL willShow = self.panel.hidden;
    self.panel.hidden = !willShow;
    if (willShow) {
        [self cancelFloatingCollapse];
        [self setFloatingCollapsed:NO animated:YES];
        [self positionPanel];
        [self refreshSpeedUI];
        [self.hostWindow bringSubviewToFront:self.panel];
        [self.hostWindow bringSubviewToFront:self.floatingButton];
    } else {
        [self scheduleFloatingCollapse];
    }
}

- (void)speedSliderChanged:(UISlider *)slider {
    float speed = PCNormalizedSpeed(slider.value);
    slider.value = speed;
    if (speed != PCCurrentSpeed()) {
        PCSetSpeed(speed, true, true);
    }
    [self refreshSpeedUI];
}

- (void)floatingButtonTouchDown:(UIButton *)sender {
    self.floatingButtonWasDragged = NO;
    [self cancelFloatingCollapse];
    if (self.floatingCollapsed) {
        [self setFloatingCollapsed:NO animated:YES];
    } else {
        sender.alpha = 1.0;
    }
}

- (void)floatingButtonTapped:(UIButton *)sender {
    if (self.floatingButtonWasDragged) {
        self.floatingButtonWasDragged = NO;
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
        [self snapFloatingButtonToEdgeAndSave];
    }
}

@end

static void PCStartSpeedOverlay(void) {
    dispatch_async(dispatch_get_main_queue(), ^{
        PCSpeedOverlay *overlay = PCSpeedOverlay.sharedOverlay;
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
    if (!target || !replacement) {
        NSLog(@"#pc  hook failed name=%s target=%p replacement=%p",
              name, target, replacement);
        return;
    }
    if (original) *original = nullptr;
    int result = DobbyHook(target, replacement, original);
    NSLog(@"#pc  hook %@ name=%s target=%p original=%p dobby_result=%d",
          result == 0 ? @"installed" : @"failed", name, target,
          original ? *original : nullptr, result);
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
    int result = DobbyCodePatch(target, (uint8_t *)&replacement,
                                (uint32_t)sizeof(replacement));
    if (result != 0) {
        NSLog(@"#pc  patch failed name=%s target=%p dobby_result=%d",
              name, target, result);
        return;
    }
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
        PCAppendRawCrashLine("UnityCrash.LastManagedThrow available=0", true);
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

// MARK: - Unity / IL2CPP probes (UnityFramework offsets for 1.0.2)

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
    if (PCCurrentSpeed() > 1.0f) {
        PCLogStack(@"ACTk OnSpeedCheaterDetected suppressed while accelerated");
        return;
    }
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

// MainScene.OpenAwakeUI runs these four display-only coroutines in order after
// purchase recovery and main-scene initialization. Completing only these
// iterators suppresses the automatic startup popups while leaving the rest of
// C_StartSequence intact. Their normal/manual feature entry points are not
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
// and the UI has changed to its final "tap to close" state. Reuse OnBack two
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

    // UIPopupReward.Event_Click reads _goSkip at +0x60 before handling BtnGet.
    // The component can outlive this child briefly during a scene transition.
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
            // IL2CPP managed exceptions use C++ Il2CppExceptionWrapper values.
            // Do not let one escape a libdispatch block and terminate the app.
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
            NSLog(@"#pc  UIPopupReward.AutoClose managed exception contained "
                   "instance=%p",
                  instance);
        }
    });
}

// C_Result is the hologram equipment-result pipeline. It opens UIItemSelect
// only when DataUtil.CompareStatEquipedItem reports that the new item is better.
// Mark that synchronous call path so UIItemSelect instances opened elsewhere
// keep their normal manual behaviour. Auto-equip reuses PS_ItemEquip.Request;
// its response already resumes SpawnItem when item-spawner auto mode is active.
static __thread int gItemSpawnerResultDepth;
static bool (*orig_itemSpawnerResultMoveNext)(void *, const void *);
static void (*orig_itemSelectSetData)(void *, void *, int32_t, void *, const void *);
static int32_t (*gItemInfoGetType)(void *, const void *);
static void *(*gItemInfoGetStringUID)(void *, const void *);
static void (*gItemEquipRequest)(int32_t, void *, bool, const void *);
static void (*gItemSelectClose)(void *, const void *);

static bool pc_itemSpawnerResultMoveNext(void *iterator, const void *method) {
    gItemSpawnerResultDepth++;
    bool result = orig_itemSpawnerResultMoveNext(iterator, method);
    gItemSpawnerResultDepth--;
    return result;
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

    NSLog(@"#pc  ItemSpawner.AutoEquip request info=%p type=%d uid=%@",
          info, itemType, PCStringFromIl2Cpp(uid));
    gItemEquipRequest(itemType, uid, true, nullptr);
    gItemSelectClose(instance, nullptr);
}

// The Firewall dungeon's StartDungeon coroutine normally preloads every
// ranking page and ranked-player details before calling PlayDungeon. Preserve
// Event_Click's content/ticket validation, but shorten the coroutine after
// those checks to the ordinary dungeon transition.
static void *gGameInfoInstance;
static void (*orig_gameInfoUpdateBattleStart)(void *, const void *);
static void (*gGameInfoPlayDungeon)(void *, int32_t, int32_t, int32_t,
                                    const void *);
static bool (*orig_firewallStartDungeonMoveNext)(void *, const void *);

static void pc_gameInfoUpdateBattleStart(void *instance, const void *method) {
    if (instance && instance != gGameInfoInstance) {
        gGameInfoInstance = instance;
        NSLog(@"#pc  GameInfo captured instance=%p", instance);
    }
    orig_gameInfoUpdateBattleStart(instance, method);
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
// icon active. Reuse TouchRelationEmoji after creation; it performs the same
// object/cooldown checks as a physical tap and then sends
// PS_PartnerRelationExp.Request. The short guard protects against duplicate
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

// Mine cells are normally clickable only when SetState finds them immediately
// adjacent to the current position. Keep the request-in-flight and stamina
// checks, but allow every rendered cell to reuse RequestMoveCell.
static void (*gMineRowItemEnableMove)(void *, bool, const void *);
static bool (*gMineRowItemRequestMoveCell)(void *, const void *);
static int32_t (*gMineCellInfoGetCol)(void *, const void *);
static int32_t (*gMineCellInfoGetRow)(void *, const void *);
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
    if (!instance || !gMineRowItemRequestMoveCell) {
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
           "distance=%d far=%d moveType=Cell(0)",
          PCStringFromIl2Cpp(name), currentCol, currentRow, col, row,
          deltaCol + deltaRow, farMove ? 1 : 0);
    bool requested = gMineRowItemRequestMoveCell(instance, nullptr);
    gMineFarMovePending = requested && farMove;
    NSLog(@"#pc  Mine.DirectMove request col=%d row=%d acceptedLocal=%d "
           "refreshAfterResponse=%d",
          col, row, requested ? 1 : 0, gMineFarMovePending ? 1 : 0);
}

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

// Only the BattleProcessor.State_Defeat call path is marked. If that first
// defeat opens UIGrowthGuide, close it through the scene's normal method after
// one second without affecting growth guides opened elsewhere.
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
            NSLog(@"#pc  BattleFailure.GrowthGuide auto-close cancelled scene=%p "
                   "destroyed=1",
                  instance);
            return;
        }

        gPendingFailureGrowthScene = nullptr;
        gFailureGrowthGeneration++;
        NSLog(@"#pc  BattleFailure.GrowthGuide auto-close firing scene=%p", instance);
        gUIContentsSceneCloseGrowthGuide(instance, nullptr);
    });
}

// MARK: - AssetBundle / Addressables memory probes

// tp.GameObjectPool<T>.Init(parent, prefabPath, size) creates _original with
// tp.Addressables.LoadPrefab -> Addressables.InstantiateAsync(trackHandle=true).
// Its Clear/Cleanup methods only Object.Destroy(_original), so the tracked
// Addressables handle is otherwise leaked every time a pool is rebuilt.
static bool (*gAddressablesReleaseInstance)(void *instance);

static void (*orig_gameObjectPoolInitPath)(void *, void *, void *, int32_t,
                                           const void *);
static void pc_gameObjectPoolInitPath(void *pool, void *parent, void *prefabPath,
                                      int32_t size, const void *method) {
    @autoreleasepool {
        NSString *path = PCStringFromIl2Cpp(prefabPath);
        uint64_t initCount = PCTrackProjectilePool(pool, path);
        if (initCount) {
            NSLog(@"#pc  ProjectilePool.Init begin path=%@ pool=%p size=%d init_count=%llu "
                   "footprint=%lluMB",
                  path, pool, size, (unsigned long long)initCount,
                  (unsigned long long)PCMemoryFootprintMB());
        }
        orig_gameObjectPoolInitPath(pool, parent, prefabPath, size, method);
        if (initCount) {
            void *original = pool ? *(void **)((uint8_t *)pool + 16) : nullptr;
            NSLog(@"#pc  ProjectilePool.Init end path=%@ pool=%p original=%p size=%d "
                   "footprint=%lluMB",
                  path, pool, original, size,
                  (unsigned long long)PCMemoryFootprintMB());
        }
    }
}

static void (*orig_gameObjectPoolClear)(void *, bool, const void *);
static void pc_gameObjectPoolClear(void *pool, bool isOriginal, const void *method) {
    void *original = pool ? *(void **)((uint8_t *)pool + 16) : nullptr;
    NSString *path = PCProjectilePoolPath(pool);
    orig_gameObjectPoolClear(pool, isOriginal, method);

    bool released = false;
    if (path.length && isOriginal && original && gAddressablesReleaseInstance) {
        // Clear already destroyed all clones and scheduled Destroy(_original).
        // ReleaseInstance removes the tracked operation and decrements the
        // Addressables dependency/bundle reference count.
        released = gAddressablesReleaseInstance(original);
        PCIncrement(released ? &gAddressablePoolInstancesReleased
                             : &gAddressablePoolInstancesNotTracked);
    }
    if (path.length) {
        NSLog(@"#pc  ProjectilePool.Clear path=%@ pool=%p original=%p release=%d "
               "released_total=%llu not_tracked_total=%llu footprint=%lluMB",
              path, pool, original, released,
              (unsigned long long)__atomic_load_n(&gAddressablePoolInstancesReleased,
                                                   __ATOMIC_RELAXED),
              (unsigned long long)__atomic_load_n(&gAddressablePoolInstancesNotTracked,
                                                   __ATOMIC_RELAXED),
              (unsigned long long)PCMemoryFootprintMB());
    }
    if (isOriginal) PCUntrackProjectilePool(pool);
}

static void (*orig_gameObjectPoolCleanup)(void *, const void *);
static void pc_gameObjectPoolCleanup(void *pool, const void *method) {
    void *original = pool ? *(void **)((uint8_t *)pool + 16) : nullptr;
    NSString *path = PCProjectilePoolPath(pool);
    orig_gameObjectPoolCleanup(pool, method);
    void *currentOriginal = pool ? *(void **)((uint8_t *)pool + 16) : nullptr;

    bool released = false;
    bool releaseForEffectManager = gEffectManagerClearing && original;
    bool releaseTrackedProjectile = path.length && original && !currentOriginal;
    if ((releaseForEffectManager || releaseTrackedProjectile) &&
        gAddressablesReleaseInstance) {
        released = gAddressablesReleaseInstance(original);
        PCIncrement(released ? &gAddressablePoolInstancesReleased
                             : &gAddressablePoolInstancesNotTracked);
        if (releaseForEffectManager && released) {
            PCIncrement(&gEffectPoolInstancesReleased);
        }
        PCUntrackProjectilePool(pool);
    }
    if (releaseForEffectManager) {
        NSLog(@"#pc  EffectPool.Release pool=%p original=%p release=%d retained=%d "
               "effect_released_total=%llu footprint=%lluMB",
              pool, original, released, currentOriginal != nullptr,
              (unsigned long long)__atomic_load_n(&gEffectPoolInstancesReleased,
                                                   __ATOMIC_RELAXED),
              (unsigned long long)PCMemoryFootprintMB());
    }
    if (path.length) {
        NSLog(@"#pc  ProjectilePool.Cleanup path=%@ pool=%p original=%p retained=%d "
               "release=%d footprint=%lluMB",
              path, pool, original, currentOriginal != nullptr, released,
              (unsigned long long)PCMemoryFootprintMB());
    }
}

// EffectManager.Clear destroys Root_EffectObject and then clears its pool
// dictionary without calling GameObjectPool.Clear.  Every pool original came
// from Addressables.InstantiateAsync(trackHandle=true), so those tracked
// operations survive the destroyed hierarchy and leak their bundle refs.
static void (*gEffectManagerCleanup)(void *, const void *);
static void (*orig_effectManagerClear)(void *, const void *);
static void pc_effectManagerClear(void *manager, const void *method) {
    uint64_t before =
        __atomic_load_n(&gEffectPoolInstancesReleased, __ATOMIC_RELAXED);
    if (manager && gEffectManagerCleanup) {
        gEffectManagerClearing = true;
        gEffectManagerCleanup(manager, nullptr);
        gEffectManagerClearing = false;
    }
    uint64_t after =
        __atomic_load_n(&gEffectPoolInstancesReleased, __ATOMIC_RELAXED);
    NSLog(@"#pc  EffectManager.Clear manager=%p released_now=%llu released_total=%llu "
           "footprint=%lluMB",
          manager, (unsigned long long)(after - before),
          (unsigned long long)after, (unsigned long long)PCMemoryFootprintMB());
    orig_effectManagerClear(manager, method);
}

static void *(*orig_assetBundleLoadFromFileAsyncInternal)(void *, uint32_t, uint64_t,
                                                          const void *);
static void *pc_assetBundleLoadFromFileAsyncInternal(void *path, uint32_t crc, uint64_t offset,
                                                     const void *method) {
    void *request = orig_assetBundleLoadFromFileAsyncInternal(path, crc, offset, method);
    uint64_t count = PCIncrement(&gBundleLoadRequests);
    @autoreleasepool {
        NSLog(@"#pc  AB LoadFromFileAsync request=%llu path=%@ crc=%u offset=%llu op=%p "
               "footprint=%lluMB",
              (unsigned long long)count, PCStringFromIl2Cpp(path), crc,
              (unsigned long long)offset, request,
              (unsigned long long)PCMemoryFootprintMB());
    }
    return request;
}

static void *(*orig_assetBundleLoadFromStream)(void *, const void *);
static void *pc_assetBundleLoadFromStream(void *stream, const void *method) {
    void *bundle = orig_assetBundleLoadFromStream(stream, method);
    uint64_t loads = bundle ? PCIncrement(&gBundleLoads)
                            : __atomic_load_n(&gBundleLoads, __ATOMIC_RELAXED);
    @autoreleasepool {
        NSLog(@"#pc  AB LoadFromStream stream=%p bundle=%p loads=%llu unloads=%llu live=%lld "
               "footprint=%lluMB",
              stream, bundle, (unsigned long long)loads,
              (unsigned long long)__atomic_load_n(&gBundleUnloads, __ATOMIC_RELAXED),
              PCBundleLiveEstimate(), (unsigned long long)PCMemoryFootprintMB());
    }
    return bundle;
}

static void (*orig_encryptedBundleLoadLocal)(void *, void *, void *, bool, const void *);
static void pc_encryptedBundleLoadLocal(void *resource, void *provideHandle, void *path,
                                        bool encrypted, const void *method) {
    @autoreleasepool {
        NSLog(@"#pc  AB EncryptedProvider.LoadLocal begin resource=%p path=%@ encrypted=%d "
               "footprint=%lluMB",
              resource, PCStringFromIl2Cpp(path), encrypted,
              (unsigned long long)PCMemoryFootprintMB());
    }
    orig_encryptedBundleLoadLocal(resource, provideHandle, path, encrypted, method);
    @autoreleasepool {
        NSLog(@"#pc  AB EncryptedProvider.LoadLocal end resource=%p path=%@ "
               "footprint=%lluMB",
              resource, PCStringFromIl2Cpp(path),
              (unsigned long long)PCMemoryFootprintMB());
    }
}

static void (*orig_standardBundleComplete)(void *, void *, const void *);
static void pc_standardBundleComplete(void *resource, void *bundle, const void *method) {
    orig_standardBundleComplete(resource, bundle, method);
    uint64_t loads = bundle ? PCIncrement(&gBundleLoads)
                            : __atomic_load_n(&gBundleLoads, __ATOMIC_RELAXED);
    void *path = resource ? *(void **)((uint8_t *)resource + 120) : nullptr;
    @autoreleasepool {
        NSLog(@"#pc  AB AssetBundleResource.Complete bundle=%p path=%@ loads=%llu "
               "unloads=%llu live=%lld footprint=%lluMB",
              bundle, PCStringFromIl2Cpp(path), (unsigned long long)loads,
              (unsigned long long)__atomic_load_n(&gBundleUnloads, __ATOMIC_RELAXED),
              PCBundleLiveEstimate(), (unsigned long long)PCMemoryFootprintMB());
    }
}

static void (*orig_encryptedBundleProviderRelease)(void *, void *, void *, const void *);
static void pc_encryptedBundleProviderRelease(void *provider, void *location, void *asset,
                                               const void *method) {
    uint64_t releases = PCIncrement(&gBundleProviderReleases);
    @autoreleasepool {
        NSLog(@"#pc  AB EncryptedProvider.Release releases=%llu provider=%p location=%p "
               "resource=%p footprint=%lluMB",
              (unsigned long long)releases, provider, location, asset,
              (unsigned long long)PCMemoryFootprintMB());
    }
    orig_encryptedBundleProviderRelease(provider, location, asset, method);
}

static void (*orig_encryptedBundleUnload)(void *, const void *);
static void pc_encryptedBundleUnload(void *resource, const void *method) {
    @autoreleasepool {
        NSLog(@"#pc  AB EncryptedResource.Unload begin resource=%p footprint=%lluMB",
              resource, (unsigned long long)PCMemoryFootprintMB());
    }
    orig_encryptedBundleUnload(resource, method);
    @autoreleasepool {
        NSLog(@"#pc  AB EncryptedResource.Unload end resource=%p footprint=%lluMB",
              resource, (unsigned long long)PCMemoryFootprintMB());
    }
}

static void (*orig_encryptedBundleDispose)(void *, const void *);
static void pc_encryptedBundleDispose(void *resource, const void *method) {
    @autoreleasepool {
        NSLog(@"#pc  AB EncryptedResource.Dispose resource=%p footprint=%lluMB",
              resource, (unsigned long long)PCMemoryFootprintMB());
    }
    orig_encryptedBundleDispose(resource, method);
}

static bool (*orig_standardBundleResourceUnload)(void *, void **, const void *);
static bool pc_standardBundleResourceUnload(void *resource, void **unloadOperation,
                                            const void *method) {
    bool result = orig_standardBundleResourceUnload(resource, unloadOperation, method);
    @autoreleasepool {
        NSLog(@"#pc  AB AssetBundleResource.Unload resource=%p scheduled=%d op=%p "
               "footprint=%lluMB",
              resource, result, unloadOperation ? *unloadOperation : nullptr,
              (unsigned long long)PCMemoryFootprintMB());
    }
    return result;
}

static void (*orig_assetBundleUnload)(void *, bool, const void *);
static void pc_assetBundleUnload(void *bundle, bool unloadAllLoadedObjects,
                                 const void *method) {
    orig_assetBundleUnload(bundle, unloadAllLoadedObjects, method);
    uint64_t unloads = PCIncrement(&gBundleUnloads);
    @autoreleasepool {
        NSLog(@"#pc  AB Unload bundle=%p all=%d loads=%llu unloads=%llu live=%lld "
               "footprint=%lluMB",
              bundle, unloadAllLoadedObjects,
              (unsigned long long)__atomic_load_n(&gBundleLoads, __ATOMIC_RELAXED),
              (unsigned long long)unloads, PCBundleLiveEstimate(),
              (unsigned long long)PCMemoryFootprintMB());
    }
}

static void *(*orig_assetBundleUnloadAsync)(void *, bool, const void *);
static void *pc_assetBundleUnloadAsync(void *bundle, bool unloadAllLoadedObjects,
                                       const void *method) {
    void *operation = orig_assetBundleUnloadAsync(bundle, unloadAllLoadedObjects, method);
    uint64_t unloads = PCIncrement(&gBundleUnloads);
    @autoreleasepool {
        NSLog(@"#pc  AB UnloadAsync bundle=%p all=%d op=%p loads=%llu unloads=%llu "
               "live=%lld footprint=%lluMB",
              bundle, unloadAllLoadedObjects, operation,
              (unsigned long long)__atomic_load_n(&gBundleLoads, __ATOMIC_RELAXED),
              (unsigned long long)unloads, PCBundleLiveEstimate(),
              (unsigned long long)PCMemoryFootprintMB());
    }
    return operation;
}

static void *(*orig_assetBundleLoadAssetAsync)(void *, void *, void *, const void *);
static void *pc_assetBundleLoadAssetAsync(void *bundle, void *name, void *type,
                                          const void *method) {
    void *request = orig_assetBundleLoadAssetAsync(bundle, name, type, method);
    uint64_t count = PCIncrement(&gAssetLoadRequests);
    @autoreleasepool {
        NSString *assetName = PCStringFromIl2Cpp(name);
        uint64_t projectileCount = PCTrackProjectileAssetRequest(assetName);
        NSLog(@"#pc  AB LoadAssetAsync request=%llu bundle=%p name=%@ type=%p op=%p",
              (unsigned long long)count, bundle, assetName, type, request);
        // Two samples per path are enough to distinguish first-stage preload
        // from the repeated attack-time caller without flooding the log.
        if (projectileCount > 0 && projectileCount <= 2) {
            PCLogStack([NSString stringWithFormat:
                @"ProjectileAsset.Request path=%@ path_count=%llu bundle=%p",
                assetName, (unsigned long long)projectileCount, bundle]);
        }
    }
    return request;
}

static void *(*orig_assetBundleLoadSubAssetsAsync)(void *, void *, void *, const void *);
static void *pc_assetBundleLoadSubAssetsAsync(void *bundle, void *name, void *type,
                                              const void *method) {
    void *request = orig_assetBundleLoadSubAssetsAsync(bundle, name, type, method);
    uint64_t count = PCIncrement(&gAssetLoadRequests);
    @autoreleasepool {
        NSLog(@"#pc  AB LoadSubAssetsAsync request=%llu bundle=%p name=%@ type=%p op=%p",
              (unsigned long long)count, bundle, PCStringFromIl2Cpp(name), type, request);
    }
    return request;
}

static void *(*orig_assetBundleLoadAllAssetsAsync)(void *, void *, const void *);
static void *pc_assetBundleLoadAllAssetsAsync(void *bundle, void *type, const void *method) {
    void *request = orig_assetBundleLoadAllAssetsAsync(bundle, type, method);
    uint64_t count = PCIncrement(&gAssetLoadRequests);
    @autoreleasepool {
        NSLog(@"#pc  AB LoadAllAssetsAsync request=%llu bundle=%p type=%p op=%p",
              (unsigned long long)count, bundle, type, request);
    }
    return request;
}

static void (*orig_bundledAssetActionComplete)(void *, void *, const void *);
static void pc_bundledAssetActionComplete(void *operation, void *asyncOperation,
                                          const void *method) {
    orig_bundledAssetActionComplete(operation, asyncOperation, method);
    uint64_t completes = PCIncrement(&gAssetLoadCompletes);
    @autoreleasepool {
        NSLog(@"#pc  AB AssetLoadComplete completes=%llu requests=%llu operation=%p "
               "async=%p footprint=%lluMB",
              (unsigned long long)completes,
              (unsigned long long)__atomic_load_n(&gAssetLoadRequests, __ATOMIC_RELAXED),
              operation, asyncOperation, (unsigned long long)PCMemoryFootprintMB());
    }
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
              "message=%s stack=%s footprint=%lluMB",
              exception, context, exceptionClass, message, stack,
              (unsigned long long)PCMemoryFootprintMB());
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
    PCPatchInstruction(slide, 0x2FB5B50, 0x1E20CC20, 0x1E2C1000,
                       "UIItemSpawnerInfo.auto_open_delay_0.5s");

    gQuestInfoIsComplete =
        (bool (*)(void *, const void *))(slide + 0x2DE9468);
    gQuestInfoIsGetReward =
        (bool (*)(void *, const void *))(slide + 0x2DE9560);
    gQuestInfoGetKey =
        (int32_t (*)(void *, const void *))(slide + 0x2DEA18C);
    gQuestCompleteRequest =
        (void (*)(void *, const void *))(slide + 0x2EEC074);
    gGameObjectSetActive =
        (void (*)(void *, bool, const void *))(slide + 0x6A3A5F0);
    gUnityObjectImplicit =
        (bool (*)(void *, const void *))(slide + 0x6A42928);
    gComponentGetGameObject =
        (void *(*)(void *, const void *))(slide + 0x6A33E0C);
    gGameObjectGetActiveInHierarchy =
        (bool (*)(void *, const void *))(slide + 0x6A3A7F4);
    gItemInfoGetType =
        (int32_t (*)(void *, const void *))(slide + 0x2DB16E8);
    gItemInfoGetStringUID =
        (void *(*)(void *, const void *))(slide + 0x2DB1694);
    gItemEquipRequest =
        (void (*)(int32_t, void *, bool, const void *))(slide + 0x2EC6884);
    gItemSelectClose =
        (void (*)(void *, const void *))(slide + 0x2FAE02C);
    gMainSceneTouchRelationEmoji =
        (bool (*)(void *, const void *))(slide + 0x319E020);
    gGameInfoPlayDungeon =
        (void (*)(void *, int32_t, int32_t, int32_t, const void *))
            (slide + 0x2DA535C);
    gMineRowItemEnableMove =
        (void (*)(void *, bool, const void *))(slide + 0x3091548);
    gMineRowItemRequestMoveCell =
        (bool (*)(void *, const void *))(slide + 0x3091754);
    gMineCellInfoGetCol =
        (int32_t (*)(void *, const void *))(slide + 0x2DBBD6C);
    gMineCellInfoGetRow =
        (int32_t (*)(void *, const void *))(slide + 0x2DBBDB4);
    gMineScrollViewGetCellItem =
        (void *(*)(void *, int32_t, int32_t, const void *))
            (slide + 0x308DDAC);
    gMineInfosRequest =
        (void (*)(const void *))(slide + 0x2ED9FE0);
    gUIGardenMineSetData =
        (void (*)(void *, bool, const void *))(slide + 0x308BABC);
    gMineInfoGetCol =
        (int32_t (*)(void *, const void *))(slide + 0x2DBC260);
    gMineInfoGetRow =
        (int32_t (*)(void *, const void *))(slide + 0x2DBC2A0);
    gMineInfoGetDistance =
        (int32_t (*)(void *, const void *))(slide + 0x2DBC2E0);
    gUIContentsSceneCloseGrowthGuide =
        (void (*)(void *, const void *))(slide + 0x3199FCC);

    gAddressablesReleaseInstance =
        (bool (*)(void *))(slide + 0x644DDFC);
    gEffectManagerCleanup =
        (void (*)(void *, const void *))(slide + 0x327A790);
    NSLog(@"#pc  Addressables.ReleaseInstance resolver=%p",
          gAddressablesReleaseInstance);

    // UnityEngine.Time.set_timeScale(float), confirmed from the 1.0.2
    // UnityEngine.CoreModule IL2CPP disassembly. Hooking its wrapper keeps the
    // ABI identical to the reference unity2025 implementation while avoiding
    // a runtime signature scan for this fixed, hash-verified game build.
    PCHook((void *)(slide + 0x06A4C1E0), (void *)pc_setTimeScale,
           (void **)&orig_setTimeScale,
           "UnityEngine.Time.set_timeScale_0x6A4C1E0");

    PCHook((void *)(slide + 0x0E51644), (void *)pc_jailbreakCheck,
           (void **)&orig_jailbreakCheck, "native_jailbreak_check_0xE51644");
    PCHook((void *)(slide + 0x32977C8), (void *)pc_globalQuit,
           (void **)&orig_globalQuit, "GlobalObject.Quit_0x32977C8");
    PCHook((void *)(slide + 0x69A3840), (void *)pc_applicationQuit,
           (void **)&orig_applicationQuit, "Application.Quit_0x69A3840");
    PCHook((void *)(slide + 0x329A88C), (void *)pc_obscuredCheater,
           (void **)&orig_obscuredCheater, "OnObscuredCheaterDetected_0x329A88C");
    PCHook((void *)(slide + 0x329AC40), (void *)pc_speedCheater,
           (void **)&orig_speedCheater, "OnSpeedCheaterDetected_0x329AC40");
    PCHook((void *)(slide + 0x329AFF4), (void *)pc_timeCheater,
           (void **)&orig_timeCheater, "OnTimeCheaterDetected_0x329AFF4");
    PCHook((void *)(slide + 0x326167C), (void *)pc_banProcess,
           (void **)&orig_banProcess, "LoginScene.BanProcess_0x326167C");
    PCHook((void *)(slide + 0x3261868), (void *)pc_banPopupProcess,
           (void **)&orig_banPopupProcess, "LoginScene.BanPopupProcess_0x3261868");
    PCHook((void *)(slide + 0x2E5FC34), (void *)pc_banInfoRequest,
           (void **)&orig_banInfoRequest, "PS_BanInfo.Request_0x2E5FC34");
    PCHook((void *)(slide + 0x2E605CC), (void *)pc_integrityRequest,
           (void **)&orig_integrityRequest, "PS_Integrity.Request_0x2E605CC");
    PCHook((void *)(slide + 0x2E6077C), (void *)pc_integrityError,
           (void **)&orig_integrityError, "PS_Integrity.OnErrorCallback_0x2E6077C");
    PCHook((void *)(slide + 0x2DBA910), (void *)pc_timeRewardGetRemainTime,
           (void **)&orig_timeRewardGetRemainTime,
           "TimeRewardListParam.GetRemainTime_AdRemove_0x2DBA910");
    PCHook((void *)(slide + 0x2F19F64), (void *)pc_openNoticeMoveNext,
           (void **)&orig_openNoticeMoveNext,
           "MainScene.OpenNotice.MoveNext_skip_0x2F19F64");
    PCHook((void *)(slide + 0x2F19B24), (void *)pc_openLoginBonusMoveNext,
           (void **)&orig_openLoginBonusMoveNext,
           "MainScene.OpenLoginBonus.MoveNext_skip_0x2F19B24");
    PCHook((void *)(slide + 0x2F18C00), (void *)pc_openAFKMoveNext,
           (void **)&orig_openAFKMoveNext,
           "MainScene.OpenAFK.MoveNext_skip_0x2F18C00");
    PCHook((void *)(slide + 0x2F1A8E8), (void *)pc_openTimeDealMoveNext,
           (void **)&orig_openTimeDealMoveNext,
           "MainScene.OpenTimeDeal.MoveNext_skip_0x2F1A8E8");
    PCHook((void *)(slide + 0x320C998), (void *)pc_popupRewardOnBack,
           (void **)&orig_popupRewardOnBack,
           "UIPopupReward.OnBack_auto_close_0x320C998");
    PCHook((void *)(slide + 0x320CDC8), (void *)pc_popupRewardShowComplete,
           (void **)&orig_popupRewardShowComplete,
           "UIPopupReward.ShowCompete_auto_close_0x320CDC8");
    PCHook((void *)(slide + 0x2FB5ECC), (void *)pc_itemSpawnerResultMoveNext,
           (void **)&orig_itemSpawnerResultMoveNext,
           "UIItemSpawnerInfo.C_Result.MoveNext_auto_equip_0x2FB5ECC");
    PCHook((void *)(slide + 0x2FACE08), (void *)pc_itemSelectSetData,
           (void **)&orig_itemSelectSetData,
           "UIItemSelect.SetData_item_spawner_auto_equip_0x2FACE08");
    PCHook((void *)(slide + 0x2DAADFC),
           (void *)pc_gameInfoUpdateBattleStart,
           (void **)&orig_gameInfoUpdateBattleStart,
           "GameInfo.UpdateBattleStart_capture_0x2DAADFC");
    PCHook((void *)(slide + 0x30391D0),
           (void *)pc_firewallStartDungeonMoveNext,
           (void **)&orig_firewallStartDungeonMoveNext,
           "UIDungeonReady_Firewall.StartDungeon.MoveNext_direct_0x30391D0");
    PCHook((void *)(slide + 0x319DB10),
           (void *)pc_mainSceneCreateRelationEmoji,
           (void **)&orig_mainSceneCreateRelationEmoji,
           "UIMainScene.CreateRelationEmoji_auto_care_0x319DB10");
    PCHook((void *)(slide + 0x320ED20), (void *)pc_guideQuestSetData,
           (void **)&orig_guideQuestSetData,
           "UIGuideQuestInfo.SetData_auto_claim_0x320ED20");
    PCHook((void *)(slide + 0x309117C), (void *)pc_mineRowItemSetState,
           (void **)&orig_mineRowItemSetState,
           "UIGardenMineRowItem.SetState_enable_all_0x309117C");
    PCHook((void *)(slide + 0x3091F58), (void *)pc_mineRowItemEventClick,
           (void **)&orig_mineRowItemEventClick,
           "UIGardenMineRowItem.Event_Click_direct_move_0x3091F58");
    PCHook((void *)(slide + 0x308D210), (void *)pc_uiGardenMineMove,
           (void **)&orig_uiGardenMineMove,
           "UIGardenMine.Move_refresh_far_target_0x308D210");
    PCHook((void *)(slide + 0x2EDA160), (void *)pc_mineInfosResponse,
           (void **)&orig_mineInfosResponse,
           "PS_MineInfos.Response_refresh_far_target_0x2EDA160");
    PCHook((void *)(slide + 0x31432F4), (void *)pc_battleProcessorStateDefeat,
           (void **)&orig_battleProcessorStateDefeat,
           "BattleProcessor.State_Defeat_mark_0x31432F4");
    PCHook((void *)(slide + 0x31998F0),
           (void *)pc_uiContentsSceneShowGrowthGuide,
           (void **)&orig_uiContentsSceneShowGrowthGuide,
           "UIContentsScene.ShowGrowthGuide_auto_close_failure_0x31998F0");

    PCHook((void *)(slide + 0x57FAAEC), (void *)pc_gameObjectPoolInitPath,
           (void **)&orig_gameObjectPoolInitPath,
           "GameObjectPool.Init_path_0x57FAAEC");
    PCHook((void *)(slide + 0x57FBBE4), (void *)pc_gameObjectPoolClear,
           (void **)&orig_gameObjectPoolClear,
           "GameObjectPool.Clear_0x57FBBE4");
    PCHook((void *)(slide + 0x57FBE50), (void *)pc_gameObjectPoolCleanup,
           (void **)&orig_gameObjectPoolCleanup,
           "GameObjectPool.Cleanup_0x57FBE50");
    PCHook((void *)(slide + 0x327A694), (void *)pc_effectManagerClear,
           (void **)&orig_effectManagerClear,
           "EffectManager.Clear_release_pools_0x327A694");

    PCHook((void *)(slide + 0x698A578), (void *)pc_assetBundleLoadFromFileAsyncInternal,
           (void **)&orig_assetBundleLoadFromFileAsyncInternal,
           "AssetBundle.LoadFromFileAsync_Internal_0x698A578");
    PCHook((void *)(slide + 0x698AB9C), (void *)pc_assetBundleLoadFromStream,
           (void **)&orig_assetBundleLoadFromStream,
           "AssetBundle.LoadFromStream_0x698AB9C");
    PCHook((void *)(slide + 0x2CFA3E0), (void *)pc_encryptedBundleLoadLocal,
           (void **)&orig_encryptedBundleLoadLocal,
           "EncryptedAssetBundleResource.LoadFromLocalStream_0x2CFA3E0");
    PCHook((void *)(slide + 0x685CEE0), (void *)pc_standardBundleComplete,
           (void **)&orig_standardBundleComplete,
           "AssetBundleResource.CompleteBundleLoad_0x685CEE0");
    PCHook((void *)(slide + 0x2CFA0C4), (void *)pc_encryptedBundleProviderRelease,
           (void **)&orig_encryptedBundleProviderRelease,
           "EncryptedAssetBundleProvider.Release_0x2CFA0C4");
    PCHook((void *)(slide + 0x2CFA148), (void *)pc_encryptedBundleUnload,
           (void **)&orig_encryptedBundleUnload,
           "EncryptedAssetBundleResource.Unload_0x2CFA148");
    PCHook((void *)(slide + 0x2CFADFC), (void *)pc_encryptedBundleDispose,
           (void **)&orig_encryptedBundleDispose,
           "EncryptedAssetBundleResource.Dispose_0x2CFADFC");
    PCHook((void *)(slide + 0x685D138), (void *)pc_standardBundleResourceUnload,
           (void **)&orig_standardBundleResourceUnload,
           "AssetBundleResource.Unload_0x685D138");
    PCHook((void *)(slide + 0x698B664), (void *)pc_assetBundleUnload,
           (void **)&orig_assetBundleUnload, "AssetBundle.Unload_0x698B664");
    PCHook((void *)(slide + 0x698B77C), (void *)pc_assetBundleUnloadAsync,
           (void **)&orig_assetBundleUnloadAsync, "AssetBundle.UnloadAsync_0x698B77C");
    PCHook((void *)(slide + 0x698AD1C), (void *)pc_assetBundleLoadAssetAsync,
           (void **)&orig_assetBundleLoadAssetAsync,
           "AssetBundle.LoadAssetAsync_0x698AD1C");
    PCHook((void *)(slide + 0x698B0D0), (void *)pc_assetBundleLoadSubAssetsAsync,
           (void **)&orig_assetBundleLoadSubAssetsAsync,
           "AssetBundle.LoadAssetWithSubAssetsAsync_0x698B0D0");
    PCHook((void *)(slide + 0x698B500), (void *)pc_assetBundleLoadAllAssetsAsync,
           (void **)&orig_assetBundleLoadAllAssetsAsync,
           "AssetBundle.LoadAllAssetsAsync_0x698B500");
    PCHook((void *)(slide + 0x6861734), (void *)pc_bundledAssetActionComplete,
           (void **)&orig_bundledAssetActionComplete,
           "BundledAssetProvider.InternalOp.ActionComplete_0x6861734");
    PCHook((void *)(slide + 0x69B7B20), (void *)pc_unityInternalLog,
           (void **)&orig_unityInternalLog, "DebugLogHandler.Internal_Log_0x69B7B20");
    PCHook((void *)(slide + 0x69B7D70), (void *)pc_unityInternalLogException,
           (void **)&orig_unityInternalLogException,
           "DebugLogHandler.Internal_LogException_0x69B7D70");
    PCHook((void *)(slide + 0x3280374),
           (void *)pc_exceptionManagerUnhandled,
           (void **)&orig_exceptionManagerUnhandled,
           "ExceptionManager.HandleUnhandledException_0x3280374");
    PCHook((void *)(slide + 0x6A41DE8),
           (void *)pc_unityUnhandledException,
           (void **)&orig_unityUnhandledException,
           "Unity.UnhandledExceptionHandler.Handle_0x6A41DE8");
    PCHook((void *)(slide + 0x6A42018),
           (void *)pc_unityIOSNativeUnhandledException,
           (void **)&orig_unityIOSNativeUnhandledException,
           "Unity.iOSNativeUnhandledExceptionHandler_0x6A42018");
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
    NSLog(@"#pc  hook backend=Dobby hook=%p patch=%p", DobbyHook, DobbyCodePatch);

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

__attribute__((constructor)) static void PCMacProbeInitialize(void) {
    @autoreleasepool {
        NSString *bundleID = NSBundle.mainBundle.bundleIdentifier;
        if (![bundleID isEqualToString:@"jp.co.bandainamcoent.BNEI0442"]) return;
        PCInitializePersistentLogs();
        gProjectilePoolPaths = [NSMutableDictionary dictionary];
        gProjectilePoolInitCounts = [NSMutableDictionary dictionary];
        gProjectileAssetRequestCounts = [NSMutableDictionary dictionary];
        NSLog(@"#pc  PCMacProbe loaded bundle=%@ pid=%d", bundleID, getpid());
        NSLog(@"#pc  persistent logs pc=%@ unity_native=%@",
              gPersistentLogPath, gUnityNativeLogPath);
        NSLog(@"#pc  crash history=%@", gUnityCrashHistoryPath);

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
        NSInteger savedSpeed = [NSUserDefaults.standardUserDefaults
            integerForKey:PCSpeedDefaultsKey];
        PCSetSpeed(savedSpeed >= 1 && savedSpeed <= 10 ? (float)savedSpeed : 1.0f,
                   false, false);
        PCStartSpeedOverlay();
        NSLog(@"#pc  PCMacProbe initialization complete");
    }
}
