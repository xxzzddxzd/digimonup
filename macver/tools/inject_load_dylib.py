#!/usr/bin/env python3
import argparse
import os
import stat
import struct

MH_MAGIC_64 = 0xFEEDFACF
LC_LOAD_DYLIB = 0xC
LC_SEGMENT_64 = 0x19

HEADER_64 = struct.Struct("<IiiIIIII")
LOAD_COMMAND = struct.Struct("<II")
DYLIB_COMMAND = struct.Struct("<IIIIII")


def align(value, alignment):
    return (value + alignment - 1) & ~(alignment - 1)


def read_c_string(data, offset, limit):
    end = data.find(b"\0", offset, limit)
    if end < 0:
        end = limit
    return data[offset:end].decode("utf-8", "replace")


def min_section_offset(data, ncmds):
    offset = HEADER_64.size
    minimum = None
    for _ in range(ncmds):
        cmd, cmdsize = LOAD_COMMAND.unpack_from(data, offset)
        if cmd == LC_SEGMENT_64:
            nsects = struct.unpack_from("<I", data, offset + 64)[0]
            section_offset = offset + 72
            for i in range(nsects):
                current = section_offset + i * 80
                file_offset = struct.unpack_from("<I", data, current + 48)[0]
                if file_offset:
                    minimum = file_offset if minimum is None else min(minimum, file_offset)
        offset += cmdsize
    return minimum or len(data)


def has_dylib(data, ncmds, dylib_path):
    offset = HEADER_64.size
    for _ in range(ncmds):
        cmd, cmdsize = LOAD_COMMAND.unpack_from(data, offset)
        if cmd == LC_LOAD_DYLIB:
            name_offset = struct.unpack_from("<I", data, offset + 8)[0]
            name = read_c_string(data, offset + name_offset, offset + cmdsize)
            if name == dylib_path:
                return True
        offset += cmdsize
    return False


def inject(binary_path, dylib_path):
    with open(binary_path, "rb") as f:
        data = bytearray(f.read())

    if len(data) < HEADER_64.size:
        raise SystemExit(f"{binary_path}: too small")

    magic, cputype, cpusubtype, filetype, ncmds, sizeofcmds, flags, reserved = HEADER_64.unpack_from(data, 0)
    if magic != MH_MAGIC_64:
        raise SystemExit(f"{binary_path}: only thin little-endian Mach-O 64 is supported")

    if has_dylib(data, ncmds, dylib_path):
        print(f"{binary_path}: already has {dylib_path}")
        return

    encoded = dylib_path.encode("utf-8") + b"\0"
    cmdsize = align(DYLIB_COMMAND.size + len(encoded), 8)
    command = bytearray(DYLIB_COMMAND.pack(LC_LOAD_DYLIB, cmdsize, DYLIB_COMMAND.size, 2, 0, 0))
    command += encoded
    command += b"\0" * (cmdsize - len(command))

    old_end = HEADER_64.size + sizeofcmds
    new_end = old_end + cmdsize
    first_section = min_section_offset(data, ncmds)
    if new_end > first_section:
        raise SystemExit(
            f"{binary_path}: not enough load-command padding "
            f"(need end {new_end}, first section {first_section})"
        )

    padding = data[old_end:new_end]
    if any(padding):
        raise SystemExit(f"{binary_path}: load-command padding is not empty")

    data[old_end:new_end] = command
    HEADER_64.pack_into(data, 0, magic, cputype, cpusubtype, filetype, ncmds + 1, sizeofcmds + cmdsize, flags, reserved)

    mode = stat.S_IMODE(os.stat(binary_path).st_mode)
    tmp = f"{binary_path}.injecting"
    with open(tmp, "wb") as f:
        f.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, binary_path)
    print(f"{binary_path}: injected {dylib_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binary")
    parser.add_argument("dylib")
    args = parser.parse_args()
    inject(args.binary, args.dylib)


if __name__ == "__main__":
    main()
