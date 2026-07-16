THEOS ?= /opt/theos

TARGET := iphone:clang:latest:15.0
ARCHS := arm64
THEOS_PACKAGE_SCHEME :=

include $(THEOS)/makefiles/common.mk

TWEAK_NAME := PCMacProbe

PCMacProbe_FILES := PCMacProbe.mm
PCMacProbe_CFLAGS := -fobjc-arc -fno-omit-frame-pointer -Wno-deprecated-declarations \
	-Wno-unused-function -I$(CURDIR)/vendor/dobby
PCMacProbe_CCFLAGS := -std=c++17
PCMacProbe_FRAMEWORKS := Foundation UIKit
PCMacProbe_LDFLAGS += $(CURDIR)/vendor/dobby/libdobby.a -Wl,-dead_strip

include $(THEOS_MAKE_PATH)/tweak.mk

.PHONY: embedded-mac-dylib clean-mac

embedded-mac-dylib:
	$(MAKE) clean
	$(MAKE)
	mkdir -p build/mac
	cp .theos/obj/debug/PCMacProbe.dylib build/mac/PCMacProbe.dylib
	install_name_tool -id @executable_path/Frameworks/PCMacProbe.dylib build/mac/PCMacProbe.dylib
	vtool -set-build-version maccatalyst 11.0 14.0 -replace \
		-output build/mac/PCMacProbe.dylib build/mac/PCMacProbe.dylib
	codesign --force --sign - build/mac/PCMacProbe.dylib

clean-mac:
	rm -rf build/mac
