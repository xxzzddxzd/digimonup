#import <Foundation/Foundation.h>
#import <dlfcn.h>
#import <mach-o/dyld.h>
#import <mach-o/loader.h>
#import <pthread.h>
#import <stdarg.h>
#import <stdio.h>
#import <stdlib.h>
#import <string.h>
#import <unistd.h>
#import <fcntl.h>
#import <errno.h>
#import <sys/stat.h>

typedef void *(*il2cpp_domain_get_t)(void);
typedef void **(*il2cpp_domain_get_assemblies_t)(const void *domain, size_t *size);
typedef void *(*il2cpp_assembly_get_image_t)(const void *assembly);
typedef const char *(*il2cpp_image_get_name_t)(const void *image);
typedef size_t (*il2cpp_image_get_class_count_t)(const void *image);
typedef void *(*il2cpp_image_get_class_t)(const void *image, size_t index);
typedef const char *(*il2cpp_class_get_name_t)(void *klass);
typedef const char *(*il2cpp_class_get_namespace_t)(void *klass);
typedef void *(*il2cpp_class_get_methods_t)(void *klass, void **iter);
typedef void *(*il2cpp_class_get_fields_t)(void *klass, void **iter);
typedef const char *(*il2cpp_method_get_name_t)(const void *method);
typedef uint32_t (*il2cpp_method_get_param_count_t)(const void *method);
typedef const char *(*il2cpp_field_get_name_t)(void *field);
typedef size_t (*il2cpp_field_get_offset_t)(void *field);
typedef void *(*il2cpp_thread_attach_t)(void *domain);
typedef void (*il2cpp_thread_detach_t)(void *thread);
typedef void *(*il2cpp_class_get_parent_t)(void *klass);

static il2cpp_domain_get_t p_domain_get;
static il2cpp_domain_get_assemblies_t p_domain_get_assemblies;
static il2cpp_assembly_get_image_t p_assembly_get_image;
static il2cpp_image_get_name_t p_image_get_name;
static il2cpp_image_get_class_count_t p_image_get_class_count;
static il2cpp_image_get_class_t p_image_get_class;
static il2cpp_class_get_name_t p_class_get_name;
static il2cpp_class_get_namespace_t p_class_get_namespace;
static il2cpp_class_get_methods_t p_class_get_methods;
static il2cpp_class_get_fields_t p_class_get_fields;
static il2cpp_method_get_name_t p_method_get_name;
static il2cpp_method_get_param_count_t p_method_get_param_count;
static il2cpp_field_get_name_t p_field_get_name;
static il2cpp_field_get_offset_t p_field_get_offset;
static il2cpp_thread_attach_t p_thread_attach;
static il2cpp_thread_detach_t p_thread_detach;
static il2cpp_class_get_parent_t p_class_get_parent;

static uintptr_t gUFBase;
static uintptr_t gUFEnd;
static intptr_t gUFSlide;
static FILE *gOut;
static int gStatusFD = -1;
static pthread_mutex_t gLogLock = PTHREAD_MUTEX_INITIALIZER;

static const char *kKeywords[] = {
    "PacketManager", "PacketSender", "PS_Auth", "LoginResponseData",
    "UILoginMessageBox", "UILogin", "MainScene", "UIPopupReward",
    "NavMeshSurface", "UIItemSpawnerInfo", "UIItemSelect",
    "PS_ItemEquip", "PS_ItemSell", "GameInfo",
    "UIDungeonReady_Firewall", "UIMainScene", "UIGuideQuestInfo",
    "UIGardenMineRowItem", "UIGardenMine", "PS_MineInfos",
    "BattleProcessor", "UIContentsScene", "DebugLogHandler",
    "ExceptionManager", "UnhandledException", "iOSNativeUnhandled",
    "DataUtil", "SelectiveCacheCleaner", "CacheCleaner",
    "GlobalObject", "LoginScene", "PS_BanInfo", "PS_Integrity",
    "TimeRewardListParam", "QuestInfo", "PS_Quest",
    "ItemInfo", "MineCellInfo", "MineInfo", "BattleInfoParam",
    "ObscuredCheater", "SpeedCheater", "TimeCheater",
    "ClientCacheClear", "OpenNotice", "OpenLoginBonus", "OpenAFK",
    "OpenTimeDeal", "StartDungeon", "OnResponseSpawn",
    "SpawnAndSell", "RequestData", "ResponseData",
    "CodeStage", "AntiCheat", "Caching",
    "DataInfo", "StageData", "RegionData",
    "C_Result", "wRequest", "wResponse",
    NULL
};

static void StatusWrite(const char *fmt, ...) {
    char buf[1024];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    if (n <= 0) return;
    pthread_mutex_lock(&gLogLock);
    if (gStatusFD >= 0) {
        write(gStatusFD, buf, (size_t)n);
        write(gStatusFD, "\n", 1);
        fsync(gStatusFD);
    }
    if (gOut) {
        fprintf(gOut, "# %s\n", buf);
        fflush(gOut);
    }
    pthread_mutex_unlock(&gLogLock);
}

static void InitOutputs(void) {
    const char *home = getenv("HOME");
    char dumpPath[512] = "/tmp/il2cpp-150.dump";
    char statusPath[512] = "/tmp/il2cpp-150.status";
    if (home && home[0]) {
        char dir[512];
        snprintf(dir, sizeof(dir), "%s/Library/Caches/PCJBProbe", home);
        mkdir(dir, 0755);
        snprintf(dumpPath, sizeof(dumpPath), "%s/il2cpp-150.dump", dir);
        snprintf(statusPath, sizeof(statusPath), "%s/il2cpp-150.status", dir);
    }
    gStatusFD = open(statusPath, O_CREAT | O_WRONLY | O_APPEND, 0644);
    gOut = fopen(dumpPath, "w");
    StatusWrite("outputs dump=%s status=%s pid=%d", dumpPath, statusPath, getpid());
}

static bool InUnityFramework(const void *ptr) {
    uintptr_t addr = (uintptr_t)ptr;
    return gUFBase && addr >= gUFBase && addr < gUFEnd;
}

static uintptr_t RVA(const void *ptr) {
    if (!ptr) return 0;
    return (uintptr_t)ptr - (uintptr_t)gUFSlide;
}

static void *MethodPointer(const void *method) {
    if (!method) return NULL;
    void *p0 = ((void **)method)[0];
    void *p1 = ((void **)method)[1];
    if (InUnityFramework(p0)) return p0;
    if (InUnityFramework(p1)) return p1;
    return p0;
}

static bool NameMatches(const char *ns, const char *name) {
    if (!name) return false;
    if (ns && ns[0]) {
        for (int i = 0; kKeywords[i]; i++) {
            if (strstr(ns, kKeywords[i])) return true;
        }
    }
    for (int i = 0; kKeywords[i]; i++) {
        if (strstr(name, kKeywords[i])) return true;
    }
    if (ns && strncmp(ns, "UnityEngine", 11) == 0) {
        if (!strcmp(name, "Application") || !strcmp(name, "Object") ||
            !strcmp(name, "GameObject") || !strcmp(name, "Component") ||
            !strcmp(name, "Caching") || !strcmp(name, "Time") ||
            !strcmp(name, "DebugLogHandler") ||
            !strcmp(name, "UnhandledExceptionHandler")) {
            return true;
        }
    }
    return false;
}

static void DumpHex(const void *ptr, int words) {
    if (!InUnityFramework(ptr) || !gOut) return;
    const uint32_t *ins = (const uint32_t *)ptr;
    fprintf(gOut, "  HEX");
    for (int i = 0; i < words; i++) fprintf(gOut, " %08x", ins[i]);
    fprintf(gOut, "\n");
}

static void DumpClass(void *klass, const char *imageName) {
    if (!klass || !gOut) return;
    const char *name = p_class_get_name ? p_class_get_name(klass) : NULL;
    const char *ns = p_class_get_namespace ? p_class_get_namespace(klass) : NULL;
    if (!NameMatches(ns, name)) return;

    const char *parentName = "";
    if (p_class_get_parent) {
        void *parent = p_class_get_parent(klass);
        if (parent && p_class_get_name) parentName = p_class_get_name(parent) ?: "";
    }
    fprintf(gOut, "CLASS image=%s ns=%s name=%s parent=%s\n",
            imageName ?: "", ns ?: "", name ?: "", parentName);

    if (p_class_get_fields && p_field_get_name && p_field_get_offset) {
        void *iter = NULL;
        void *field = NULL;
        while ((field = p_class_get_fields(klass, &iter))) {
            const char *fname = p_field_get_name(field) ?: "";
            size_t off = p_field_get_offset(field);
            fprintf(gOut, "  FIELD offset=0x%lx name=%s\n", (unsigned long)off, fname);
        }
    }

    if (p_class_get_methods && p_method_get_name) {
        void *iter = NULL;
        void *method = NULL;
        while ((method = p_class_get_methods(klass, &iter))) {
            const char *mname = p_method_get_name(method) ?: "";
            uint32_t argc = p_method_get_param_count ? p_method_get_param_count(method) : 0;
            void *fn = MethodPointer(method);
            uintptr_t rva = InUnityFramework(fn) ? RVA(fn) : 0;
            fprintf(gOut, "  METHOD rva=0x%lx args=%u name=%s ptr=%p\n",
                    (unsigned long)rva, argc, mname, fn);
            if (strstr(name, "OnResponseSpawn") || strstr(mname, "OnResponseSpawn") ||
                (strstr(name, "UIItemSpawnerInfo") && strstr(mname, "b__0")) ||
                strstr(name, "C_Result") ||
                (strstr(name, "OpenNotice") && !strcmp(mname, "MoveNext")) ||
                (strstr(name, "OpenAFK") && !strcmp(mname, "MoveNext")) ||
                (strstr(name, "OpenLoginBonus") && !strcmp(mname, "MoveNext")) ||
                (strstr(name, "OpenTimeDeal") && !strcmp(mname, "MoveNext")) ||
                (strstr(name, "StartDungeon") && !strcmp(mname, "MoveNext"))) {
                DumpHex(fn, 48);
            }
        }
    }
}

static bool ResolveFromDefault(void) {
#define SYM(name, var) var = (typeof(var))dlsym(RTLD_DEFAULT, name)
    SYM("il2cpp_domain_get", p_domain_get);
    SYM("il2cpp_domain_get_assemblies", p_domain_get_assemblies);
    SYM("il2cpp_assembly_get_image", p_assembly_get_image);
    SYM("il2cpp_image_get_name", p_image_get_name);
    SYM("il2cpp_image_get_class_count", p_image_get_class_count);
    SYM("il2cpp_image_get_class", p_image_get_class);
    SYM("il2cpp_class_get_name", p_class_get_name);
    SYM("il2cpp_class_get_namespace", p_class_get_namespace);
    SYM("il2cpp_class_get_methods", p_class_get_methods);
    SYM("il2cpp_class_get_fields", p_class_get_fields);
    SYM("il2cpp_method_get_name", p_method_get_name);
    SYM("il2cpp_method_get_param_count", p_method_get_param_count);
    SYM("il2cpp_field_get_name", p_field_get_name);
    SYM("il2cpp_field_get_offset", p_field_get_offset);
    SYM("il2cpp_thread_attach", p_thread_attach);
    SYM("il2cpp_thread_detach", p_thread_detach);
    SYM("il2cpp_class_get_parent", p_class_get_parent);
#undef SYM
    return p_domain_get != NULL;
}

static bool LocateUnityFramework(void) {
    uint32_t count = _dyld_image_count();
    for (uint32_t i = 0; i < count; i++) {
        const char *name = _dyld_get_image_name(i);
        if (!name || !strstr(name, "/UnityFramework.framework/UnityFramework")) continue;
        const struct mach_header *header = _dyld_get_image_header(i);
        intptr_t slide = _dyld_get_image_vmaddr_slide(i);
        gUFBase = (uintptr_t)header;
        gUFSlide = slide;
        uintptr_t maxEnd = gUFBase;
        const struct mach_header_64 *mh = (const struct mach_header_64 *)header;
        const uint8_t *cmd = (const uint8_t *)header + sizeof(struct mach_header_64);
        for (uint32_t c = 0; c < mh->ncmds; c++) {
            const struct load_command *lc = (const struct load_command *)cmd;
            if (lc->cmd == LC_SEGMENT_64) {
                const struct segment_command_64 *seg = (const struct segment_command_64 *)cmd;
                uintptr_t runEnd = (uintptr_t)seg->vmaddr + (uintptr_t)slide + (uintptr_t)seg->vmsize;
                if (runEnd > maxEnd) maxEnd = runEnd;
            }
            cmd += lc->cmdsize;
        }
        gUFEnd = maxEnd ? maxEnd : (gUFBase + 0x0c000000);
        StatusWrite("UnityFramework name=%s slide=0x%lx base=0x%lx end=0x%lx",
                    name, (long)slide, (unsigned long)gUFBase, (unsigned long)gUFEnd);
        return true;
    }
    return false;
}

static void DumpAll(void) {
    if (!p_domain_get) {
        StatusWrite("missing il2cpp_domain_get");
        return;
    }
    void *domain = p_domain_get();
    if (!domain) {
        StatusWrite("domain is null");
        return;
    }
    void *thread = NULL;
    if (p_thread_attach) {
        thread = p_thread_attach(domain);
        StatusWrite("thread attached=%p", thread);
    }
    size_t asmCount = 0;
    void **assemblies = p_domain_get_assemblies ? p_domain_get_assemblies(domain, &asmCount) : NULL;
    StatusWrite("assemblies=%zu", asmCount);
    if (gOut) {
        fprintf(gOut, "# UnityFramework base=0x%lx slide=0x%lx end=0x%lx\n",
                (unsigned long)gUFBase, (unsigned long)gUFSlide, (unsigned long)gUFEnd);
        fprintf(gOut, "# assemblies=%zu\n", asmCount);
    }
    int classIndex = 0;
    int dumped = 0;
    for (size_t i = 0; i < asmCount; i++) {
        void *image = p_assembly_get_image(assemblies[i]);
        if (!image) continue;
        const char *imageName = p_image_get_name ? p_image_get_name(image) : "";
        size_t classCount = p_image_get_class_count ? p_image_get_class_count(image) : 0;
        if (gOut) fprintf(gOut, "IMAGE name=%s classes=%zu\n", imageName ?: "", classCount);
        for (size_t c = 0; c < classCount; c++) {
            void *klass = p_image_get_class(image, c);
            classIndex++;
            if (!klass) continue;
            const char *name = p_class_get_name ? p_class_get_name(klass) : NULL;
            const char *ns = p_class_get_namespace ? p_class_get_namespace(klass) : NULL;
            if (NameMatches(ns, name)) {
                DumpClass(klass, imageName);
                dumped++;
            }
            if ((classIndex % 2000) == 0) {
                StatusWrite("progress classes=%d dumped=%d image=%s",
                            classIndex, dumped, imageName ?: "");
                if (gOut) fflush(gOut);
            }
        }
    }
    StatusWrite("done classes=%d dumped=%d", classIndex, dumped);
    if (gOut) {
        fprintf(gOut, "# DONE classes=%d dumped=%d\n", classIndex, dumped);
        fflush(gOut);
    }
    if (thread && p_thread_detach) p_thread_detach(thread);
}

static void *DumpThread(void *arg) {
    (void)arg;
    // Stay out of process startup / AppGuard init.
    for (int i = 0; i < 12; i++) {
        sleep(1);
        StatusWrite("delay %ds", i + 1);
    }
    if (!LocateUnityFramework()) {
        StatusWrite("UnityFramework not loaded after delay");
    }
    bool resolved = ResolveFromDefault();
    StatusWrite("il2cpp resolved=%d domain_get=%p", resolved ? 1 : 0, p_domain_get);

    for (int i = 0; i < 180; i++) {
        void *domain = p_domain_get ? p_domain_get() : NULL;
        size_t asmCount = 0;
        bool ready = false;
        if (domain && p_domain_get_assemblies && p_assembly_get_image) {
            void **assemblies = p_domain_get_assemblies(domain, &asmCount);
            for (size_t n = 0; n < asmCount; n++) {
                void *image = p_assembly_get_image(assemblies[n]);
                const char *imageName = p_image_get_name ? p_image_get_name(image) : "";
                if (imageName && strstr(imageName, "Assembly-CSharp")) {
                    ready = true;
                    break;
                }
            }
        }
        if ((i % 4) == 0) {
            StatusWrite("poll i=%d domain=%p assemblies=%zu csharp=%d",
                        i, domain, asmCount, ready ? 1 : 0);
        }
        if (ready && asmCount > 8) {
            sleep(1);
            DumpAll();
            return NULL;
        }
        usleep(500000);
    }
    StatusWrite("timeout; dump anyway");
    DumpAll();
    return NULL;
}

__attribute__((constructor)) static void PCJBDumpInit(void) {
    @autoreleasepool {
        NSString *bundleID = NSBundle.mainBundle.bundleIdentifier;
        if (![bundleID isEqualToString:@"jp.co.bandainamcoent.BNEI0442"]) return;
        InitOutputs();
        StatusWrite("loaded pid=%d bundle=%s", getpid(), bundleID.UTF8String ?: "");
        pthread_t thread;
        pthread_attr_t attr;
        pthread_attr_init(&attr);
        pthread_attr_setdetachstate(&attr, PTHREAD_CREATE_DETACHED);
        pthread_create(&thread, &attr, DumpThread, NULL);
        pthread_attr_destroy(&attr);
    }
}
