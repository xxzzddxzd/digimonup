SDK := $(shell xcrun --sdk iphoneos --show-sdk-path)
CLANG := $(shell xcrun -f clang++)
TARGET := arm64-apple-ios15.0

.PHONY: all clean

all: PCJBProbe.dylib

PCJBProbe.dylib: PCJBProbe.mm
	$(CLANG) -target $(TARGET) -isysroot $(SDK) -fobjc-arc -fno-omit-frame-pointer \
		-O1 -Wall -Wextra -Wno-unused-parameter -dynamiclib \
		-framework Foundation -framework UIKit \
		-Wl,-install_name,/var/jb/usr/lib/TweakInject/PCJBProbe.dylib \
		-o $@ $<
	codesign --force --sign - $@

clean:
	rm -f PCJBProbe.dylib
