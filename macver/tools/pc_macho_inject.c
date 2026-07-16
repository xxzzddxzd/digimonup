#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <mach/machine.h>
#include <mach-o/loader.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static size_t align_up(size_t value, size_t alignment) {
    return (value + alignment - 1) & ~(alignment - 1);
}

static void fail(const char *message, const char *path) {
    if (path) {
        fprintf(stderr, "error: %s: %s\n", message, path);
    } else {
        fprintf(stderr, "error: %s\n", message);
    }
    exit(2);
}

static void fail_errno(const char *operation, const char *path) {
    fprintf(stderr, "error: %s %s: %s\n", operation, path, strerror(errno));
    exit(2);
}

static void read_all(int fd, uint8_t *data, size_t size, const char *path) {
    size_t offset = 0;
    while (offset < size) {
        ssize_t count = read(fd, data + offset, size - offset);
        if (count < 0) {
            if (errno == EINTR) continue;
            fail_errno("read", path);
        }
        if (count == 0) fail("unexpected end of file", path);
        offset += (size_t)count;
    }
}

static void write_all(int fd, const uint8_t *data, size_t size, const char *path) {
    size_t offset = 0;
    while (offset < size) {
        ssize_t count = write(fd, data + offset, size - offset);
        if (count < 0) {
            if (errno == EINTR) continue;
            fail_errno("write", path);
        }
        offset += (size_t)count;
    }
}

typedef struct {
    struct mach_header_64 *header;
    size_t commands_end;
    size_t first_section;
    bool already_loaded;
} macho_info;

static macho_info inspect_macho(uint8_t *data, size_t size, const char *dylib_path) {
    if (size < sizeof(struct mach_header_64)) fail("file is too small", NULL);

    struct mach_header_64 *header = (struct mach_header_64 *)data;
    if (header->magic != MH_MAGIC_64) {
        fail("only a thin little-endian 64-bit Mach-O is supported", NULL);
    }
    if (header->cputype != CPU_TYPE_ARM64) {
        fail("only an arm64 Mach-O is supported", NULL);
    }

    size_t commands_end = sizeof(*header) + (size_t)header->sizeofcmds;
    if (commands_end > size) fail("invalid Mach-O load-command size", NULL);

    size_t cursor = sizeof(*header);
    size_t first_section = size;
    bool already_loaded = false;

    for (uint32_t index = 0; index < header->ncmds; index++) {
        if (cursor + sizeof(struct load_command) > commands_end) {
            fail("truncated Mach-O load command", NULL);
        }
        struct load_command *command = (struct load_command *)(data + cursor);
        if (command->cmdsize < sizeof(*command) || cursor + command->cmdsize > commands_end) {
            fail("invalid Mach-O load command", NULL);
        }

        if (command->cmd == LC_LOAD_DYLIB) {
            if (command->cmdsize < sizeof(struct dylib_command)) {
                fail("invalid LC_LOAD_DYLIB command", NULL);
            }
            struct dylib_command *dylib = (struct dylib_command *)command;
            uint32_t name_offset = dylib->dylib.name.offset;
            if (name_offset >= command->cmdsize) fail("invalid dylib name offset", NULL);
            const char *name = (const char *)command + name_offset;
            size_t maximum = command->cmdsize - name_offset;
            if (memchr(name, '\0', maximum) && strcmp(name, dylib_path) == 0) {
                already_loaded = true;
            }
        } else if (command->cmd == LC_SEGMENT_64) {
            if (command->cmdsize < sizeof(struct segment_command_64)) {
                fail("invalid LC_SEGMENT_64 command", NULL);
            }
            struct segment_command_64 *segment = (struct segment_command_64 *)command;
            size_t sections_size = (size_t)segment->nsects * sizeof(struct section_64);
            if (sizeof(*segment) + sections_size > command->cmdsize) {
                fail("invalid Mach-O section table", NULL);
            }
            struct section_64 *sections = (struct section_64 *)(segment + 1);
            for (uint32_t section_index = 0; section_index < segment->nsects; section_index++) {
                uint32_t file_offset = sections[section_index].offset;
                if (file_offset != 0 && file_offset < first_section) first_section = file_offset;
            }
        }
        cursor += command->cmdsize;
    }

    macho_info result = { header, commands_end, first_section, already_loaded };
    return result;
}

static void replace_file(const char *path, const uint8_t *data, size_t size, mode_t mode) {
    char temporary[PATH_MAX];
    int length = snprintf(temporary, sizeof(temporary), "%s.pcinject.XXXXXX", path);
    if (length < 0 || (size_t)length >= sizeof(temporary)) fail("path is too long", path);

    int fd = mkstemp(temporary);
    if (fd < 0) fail_errno("create temporary file for", path);
    if (fchmod(fd, mode & 07777) != 0) {
        unlink(temporary);
        fail_errno("chmod temporary file for", path);
    }
    write_all(fd, data, size, temporary);
    if (fsync(fd) != 0) {
        close(fd);
        unlink(temporary);
        fail_errno("fsync temporary file for", path);
    }
    if (close(fd) != 0) {
        unlink(temporary);
        fail_errno("close temporary file for", path);
    }
    if (rename(temporary, path) != 0) {
        unlink(temporary);
        fail_errno("replace", path);
    }
}

int main(int argc, char **argv) {
    bool check_only = false;
    int first_argument = 1;
    if (argc > 1 && strcmp(argv[1], "--check") == 0) {
        check_only = true;
        first_argument++;
    }
    if (argc - first_argument != 2) {
        fprintf(stderr, "usage: %s [--check] <Mach-O executable> <dylib load path>\n", argv[0]);
        return 2;
    }

    const char *binary_path = argv[first_argument];
    const char *dylib_path = argv[first_argument + 1];
    struct stat attributes;
    if (stat(binary_path, &attributes) != 0) fail_errno("stat", binary_path);
    if (attributes.st_size <= 0) fail("empty Mach-O file", binary_path);

    int fd = open(binary_path, O_RDONLY);
    if (fd < 0) fail_errno("open", binary_path);
    size_t size = (size_t)attributes.st_size;
    uint8_t *data = (uint8_t *)malloc(size);
    if (!data) fail("out of memory", NULL);
    read_all(fd, data, size, binary_path);
    close(fd);

    macho_info info = inspect_macho(data, size, dylib_path);
    if (check_only) {
        free(data);
        if (info.already_loaded) {
            printf("ok: %s loads %s\n", binary_path, dylib_path);
            return 0;
        }
        fprintf(stderr, "missing: %s does not load %s\n", binary_path, dylib_path);
        return 1;
    }
    if (info.already_loaded) {
        printf("already installed: %s\n", dylib_path);
        free(data);
        return 0;
    }

    size_t path_size = strlen(dylib_path) + 1;
    size_t command_size = align_up(sizeof(struct dylib_command) + path_size, 8);
    size_t new_commands_end = info.commands_end + command_size;
    if (new_commands_end > info.first_section || new_commands_end > size) {
        fail("not enough empty Mach-O load-command space", binary_path);
    }
    for (size_t index = info.commands_end; index < new_commands_end; index++) {
        if (data[index] != 0) fail("Mach-O load-command padding is not empty", binary_path);
    }

    struct dylib_command *new_command =
        (struct dylib_command *)(data + info.commands_end);
    memset(new_command, 0, command_size);
    new_command->cmd = LC_LOAD_DYLIB;
    new_command->cmdsize = (uint32_t)command_size;
    new_command->dylib.name.offset = (uint32_t)sizeof(*new_command);
    new_command->dylib.timestamp = 2;
    memcpy((uint8_t *)new_command + sizeof(*new_command), dylib_path, path_size);
    info.header->ncmds += 1;
    info.header->sizeofcmds += (uint32_t)command_size;

    replace_file(binary_path, data, size, attributes.st_mode);
    free(data);
    printf("installed: %s\n", dylib_path);
    return 0;
}
