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
static NSString *gPersistentLogPath;
static NSString *gUnityNativeLogPath;

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

        gPersistentLogFD = open(gPersistentLogPath.fileSystemRepresentation,
                                O_CREAT | O_WRONLY | O_APPEND, 0644);
        gUnityNativeLogFD = open(gUnityNativeLogPath.fileSystemRepresentation,
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

static void (*orig_exit)(int);
static void pc_exit(int status) {
    PCLogStack([NSString stringWithFormat:@"exit status=%d", status]);
    orig_exit(status);
    __builtin_unreachable();
}

static void (*orig__exit)(int);
static void pc__exit(int status) {
    PCLogStack([NSString stringWithFormat:@"_exit status=%d", status]);
    orig__exit(status);
    __builtin_unreachable();
}

static void (*orig_abort)(void);
static void pc_abort(void) {
    PCLogStack(@"abort");
    orig_abort();
    __builtin_unreachable();
}

static int (*orig_raise)(int);
static int pc_raise(int signalNumber) {
    PCLogStack([NSString stringWithFormat:@"raise signal=%d", signalNumber]);
    return orig_raise(signalNumber);
}

static int (*orig_kill)(pid_t, int);
static int pc_kill(pid_t pid, int signalNumber) {
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
    @autoreleasepool {
        NSLog(@"#pc  UNITY level=Exception exception=%p context=%p", exception, context);
    }
    orig_unityInternalLogException(exception, context, method);
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
    PCHook((void *)(slide + 0x320ED20), (void *)pc_guideQuestSetData,
           (void **)&orig_guideQuestSetData,
           "UIGuideQuestInfo.SetData_auto_claim_0x320ED20");

    PCHook((void *)(slide + 0x69B7B20), (void *)pc_unityInternalLog,
           (void **)&orig_unityInternalLog, "DebugLogHandler.Internal_Log_0x69B7B20");
    PCHook((void *)(slide + 0x69B7D70), (void *)pc_unityInternalLogException,
           (void **)&orig_unityInternalLogException,
           "DebugLogHandler.Internal_LogException_0x69B7D70");
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
        NSLog(@"#pc  PCJBProbe initialization complete");
    }
}
