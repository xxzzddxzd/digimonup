ifneq ($(wildcard /var/jb/usr/bin/sh),)
SHELL := /var/jb/usr/bin/sh
endif
SDK := $(shell xcrun --sdk iphoneos --show-sdk-path 2>/dev/null)
ifeq ($(SDK),)
SDK := /var/jb/usr/share/SDKs/iPhoneOS.sdk
endif
CLANG := $(shell xcrun -f clang++ 2>/dev/null)
ifeq ($(CLANG),)
CLANG := clang++
endif
TARGET := arm64-apple-ios15.0

.PHONY: all clean dump

all: PCJBProbe.dylib

PCJBProbe.dylib: PCJBProbe.mm
	$(CLANG) -target $(TARGET) -isysroot $(SDK) -fobjc-arc -fexceptions -fno-omit-frame-pointer \
		-O1 -Wall -Wextra -Wno-unused-parameter -dynamiclib \
		-framework Foundation -framework UIKit -framework CoreGraphics \
		-Wl,-install_name,/var/jb/usr/lib/TweakInject/PCJBProbe.dylib \
		-o $@ $<
	(codesign --force --sign - $@ 2>/dev/null) || ldid2 -S $@

dump: PCJBDump.dylib

PCJBDump.dylib: PCJBDump.m
	$(CLANG) -target $(TARGET) -isysroot $(SDK) -fobjc-arc -fexceptions -fno-omit-frame-pointer \
		-O1 -Wall -Wextra -Wno-unused-parameter -dynamiclib \
		-framework Foundation \
		-Wl,-install_name,/var/jb/usr/lib/TweakInject/PCJBDump.dylib \
		-o $@ $<
	(codesign --force --sign - $@ 2>/dev/null) || ldid2 -S $@

clean:
	rm -f PCJBProbe.dylib PCJBDump.dylib
