#!/usr/bin/env python3
"""Exercise the production Metal texture-view normalization for SR motion vectors."""

import pathlib
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
NATIVE = ROOT / "d3dmetal-sidecar"

SOURCE = r'''
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <objc/runtime.h>
#include <cassert>
#include <cstdint>
#include <cstring>
#include "d3dmetal-transport.hpp"

static void publicationFailures(id<MTLDevice> device) {
    auto descriptor = [MTLTextureDescriptor
        texture2DDescriptorWithPixelFormat:MTLPixelFormatRG16Uint width:64 height:64 mipmapped:NO];
    descriptor.storageMode = MTLStorageModePrivate;
    descriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsagePixelFormatView;
    auto sample = [device newTextureWithDescriptor:descriptor];
    const SEL viewSelector = @selector(newTextureViewWithPixelFormat:textureType:levels:slices:swizzle:);
    const Method viewMethod = class_getInstanceMethod(object_getClass(sample), viewSelector);
    auto sampleMap = [[NSMapTable alloc]
        initWithKeyOptions:NSPointerFunctionsWeakMemory | NSPointerFunctionsObjectPointerPersonality
              valueOptions:NSPointerFunctionsWeakMemory capacity:1];
    const SEL publishSelector = @selector(setObject:forKey:);
    const Method publishMethod = class_getInstanceMethod(object_getClass(sampleMap), publishSelector);
    assert(viewMethod && publishMethod);
    const IMP originalView = method_getImplementation(viewMethod);
    const IMP originalPublish = method_getImplementation(publishMethod);
    [sampleMap release]; [sample release];
    auto observed = [[NSMapTable alloc]
        initWithKeyOptions:NSPointerFunctionsStrongMemory
              valueOptions:NSPointerFunctionsWeakMemory | NSPointerFunctionsObjectPointerPersonality
                  capacity:3];
    __block id currentSource = nil;
    __block bool nilView = false;
    __block unsigned creations = 0, failures = 0;
    const IMP replacementView = imp_implementationWithBlock(
        ^id(id receiver, MTLPixelFormat format, MTLTextureType type,
            NSRange levels, NSRange slices, MTLTextureSwizzleChannels swizzle) {
            if (receiver == currentSource && nilView) {
                ++failures;
                return nil;
            }
            id view = reinterpret_cast<id (*)(id, SEL, MTLPixelFormat, MTLTextureType,
                NSRange, NSRange, MTLTextureSwizzleChannels)>(originalView)(
                    receiver, viewSelector, format, type, levels, slices, swizzle);
            if (receiver == currentSource) {
                assert(view);
                ++creations;
                [observed setObject:view forKey:@"view"];
            }
            return view;
        });
    const IMP replacementPublish = imp_implementationWithBlock(^(id receiver, id value, id key) {
        if (key == currentSource) {
            ++failures;
            @throw [NSException exceptionWithName:@"MotionPublicationFixture"
                reason:@"injected weak cache publication failure" userInfo:nil];
        }
        reinterpret_cast<void (*)(id, SEL, id, id)>(originalPublish)(
            receiver, publishSelector, value, key);
    });
    @try {
        method_setImplementation(viewMethod, replacementView);
        method_setImplementation(publishMethod, replacementPublish);
        for (bool unavailable : {true, false}) {
            nilView = unavailable;
            for (unsigned attempt = 0; attempt != 16; ++attempt) {
                @autoreleasepool {
                    auto heapDescriptor = [MTLHeapDescriptor new];
                    heapDescriptor.type = MTLHeapTypePlacement;
                    heapDescriptor.storageMode = MTLStorageModePrivate;
                    heapDescriptor.size = 64 * 1024 * 1024;
                    auto heap = [device newHeapWithDescriptor:heapDescriptor];
                    [heapDescriptor release];
                    auto source = [heap newTextureWithDescriptor:descriptor offset:0];
                    assert(heap && source);
                    currentSource = source;
                    [observed setObject:heap forKey:@"heap"];
                    [observed setObject:source forKey:@"source"];
                    [source retain];
                    const auto sourceOwners = source.retainCount;
                    void* normalized = source;
                    yaagl::pso::d3dmetal::TextureView range{};
                    const auto originalRange = range;
                    @autoreleasepool {
                        assert(!yaagl::pso::d3dmetal::normalizeTemporalMotionTexture(normalized, range));
                    }
                    assert(normalized == source && source.retainCount == sourceOwners);
                    assert(!std::memcmp(&range, &originalRange, sizeof(range)));
                    // Failure must preserve both caller-owned source references.
                    [reinterpret_cast<id>(normalized) release];
                    [source release]; [heap release];
                    currentSource = nil;
                }
                assert(![observed objectForKey:@"view"]);
                assert(![observed objectForKey:@"source"]);
                assert(![observed objectForKey:@"heap"]);
            }
        }
        assert(creations == 16 && failures == 32);
    } @finally {
        method_setImplementation(publishMethod, originalPublish);
        method_setImplementation(viewMethod, originalView);
        imp_removeBlock(replacementPublish); imp_removeBlock(replacementView);
        [observed release];
    }
}

int main() {
    @autoreleasepool {
        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        assert(device);
        publicationFailures(device);
        const MTLPixelFormat formats[] = {
            MTLPixelFormatRG16Unorm, MTLPixelFormatRG16Snorm,
            MTLPixelFormatRG16Uint, MTLPixelFormatRG16Sint,
            MTLPixelFormatRG32Uint, MTLPixelFormatRG32Sint,
        };
        for (const auto format : formats) {
            auto descriptor = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:format
                                                                                  width:64
                                                                                 height:64
                                                                              mipmapped:NO];
            descriptor.storageMode = MTLStorageModePrivate;
            descriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsagePixelFormatView;
            id<MTLTexture> source = [device newTextureWithDescriptor:descriptor];
            assert(source);
            [source retain];
            void* normalized = source;
            yaagl::pso::d3dmetal::TextureView range{};
            assert(yaagl::pso::d3dmetal::normalizeTemporalMotionTexture(normalized, range));
            auto view = reinterpret_cast<id<MTLTexture>>(normalized);
            const auto expected = format == MTLPixelFormatRG32Uint || format == MTLPixelFormatRG32Sint
                ? MTLPixelFormatRG32Float : MTLPixelFormatRG16Float;
            assert(view && view.pixelFormat == expected);
            yaagl::pso::d3dmetal::MetalResource normalizedResource{};
            normalizedResource.texture = normalized;
            yaagl::pso::d3dmetal::MetalResource sourceResource{};
            sourceResource.texture = source;
            assert(yaagl::pso::d3dmetal::texturesAlias(normalizedResource, sourceResource));
            [view release];
            [source retain];
            void* cached = source;
            assert(yaagl::pso::d3dmetal::normalizeTemporalMotionTexture(cached, range));
            assert(reinterpret_cast<id<MTLTexture>>(cached).pixelFormat == expected);
            [reinterpret_cast<id<MTLTexture>>(cached) release];
            [source release];
        }
        auto arrayDescriptor = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRG16Float
                                                                                   width:64
                                                                                  height:64
                                                                               mipmapped:NO];
        arrayDescriptor.textureType = MTLTextureType2DArray;
        arrayDescriptor.arrayLength = 2;
        arrayDescriptor.storageMode = MTLStorageModePrivate;
        arrayDescriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsagePixelFormatView;
        id<MTLTexture> arraySource = [device newTextureWithDescriptor:arrayDescriptor];
        assert(arraySource);
        [arraySource retain];
        void* arrayTexture = arraySource;
        yaagl::pso::d3dmetal::TextureView arrayRange{};
        assert(yaagl::pso::d3dmetal::normalizeTemporalMotionTexture(arrayTexture, arrayRange));
        auto arrayView = reinterpret_cast<id<MTLTexture>>(arrayTexture);
        assert(arrayView.textureType == MTLTextureType2D && arrayView.arrayLength == 1 &&
               arrayRange.sliceCount == 1);
        [arrayView release];
        [arraySource release];

        auto mipDescriptor = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRG16Float
                                                                                 width:64
                                                                                height:64
                                                                             mipmapped:YES];
        mipDescriptor.storageMode = MTLStorageModePrivate;
        mipDescriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsagePixelFormatView;
        id<MTLTexture> mipSource = [device newTextureWithDescriptor:mipDescriptor];
        assert(mipSource);
        yaagl::pso::d3dmetal::MetalResource firstMip{};
        firstMip.texture = mipSource;
        firstMip.view.mipCount = 1;
        firstMip.view.sliceCount = 1;
        yaagl::pso::d3dmetal::MetalResource secondMip = firstMip;
        secondMip.view.firstMip = 1;
        assert(!yaagl::pso::d3dmetal::texturesAlias(firstMip, secondMip));
        secondMip.view.firstMip = 0;
        assert(yaagl::pso::d3dmetal::texturesAlias(firstMip, secondMip));
        [mipSource release];

        id<MTLTexture> unsupported = nil;
        auto descriptor = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm
                                                                              width:64 height:64 mipmapped:NO];
        descriptor.storageMode = MTLStorageModePrivate;
        descriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsagePixelFormatView;
        unsupported = [device newTextureWithDescriptor:descriptor];
        void* texture = unsupported;
        yaagl::pso::d3dmetal::TextureView unsupportedRange{};
        assert(!yaagl::pso::d3dmetal::normalizeTemporalMotionTexture(texture, unsupportedRange));
        assert(texture == unsupported);
        [unsupported release];

        // A cached view must not keep an obsolete game's entire heap alive.
        NSMapTable* lifetime = [[NSMapTable alloc]
            initWithKeyOptions:NSPointerFunctionsStrongMemory
                  valueOptions:NSPointerFunctionsWeakMemory capacity:3];
        const auto beforeHeap = device.currentAllocatedSize;
        @autoreleasepool {
            auto heapDescriptor = [MTLHeapDescriptor new];
            heapDescriptor.type = MTLHeapTypePlacement;
            heapDescriptor.storageMode = MTLStorageModePrivate;
            heapDescriptor.size = 64 * 1024 * 1024;
            id<MTLHeap> heap = [device newHeapWithDescriptor:heapDescriptor];
            [heapDescriptor release];
            assert(heap);
            [lifetime setObject:heap forKey:@"heap"];
            auto heapTextureDescriptor = [MTLTextureDescriptor
                texture2DDescriptorWithPixelFormat:MTLPixelFormatRG16Uint
                                             width:64 height:64 mipmapped:NO];
            heapTextureDescriptor.storageMode = MTLStorageModePrivate;
            heapTextureDescriptor.usage =
                MTLTextureUsageShaderRead | MTLTextureUsagePixelFormatView;
            id<MTLTexture> heapSource = [heap newTextureWithDescriptor:heapTextureDescriptor offset:0];
            assert(heapSource);
            [lifetime setObject:heapSource forKey:@"source"];
            [heapSource retain];
            void* first = heapSource;
            yaagl::pso::d3dmetal::TextureView heapRange{};
            assert(yaagl::pso::d3dmetal::normalizeTemporalMotionTexture(first, heapRange));
            [lifetime setObject:reinterpret_cast<id>(first) forKey:@"view"];
            [heapSource retain];
            void* second = heapSource;
            assert(yaagl::pso::d3dmetal::normalizeTemporalMotionTexture(second, heapRange));
            assert(first == second);
            [heapSource release];
            [heap release];
            assert([lifetime objectForKey:@"source"]);
            assert([lifetime objectForKey:@"heap"]);
            [reinterpret_cast<id>(first) release];
            assert(reinterpret_cast<id<MTLTexture>>(second).pixelFormat == MTLPixelFormatRG16Float);
            [reinterpret_cast<id>(second) release];
        }
        printf("MOTION_VIEW_HEAP before=%llu after=%llu retained=%d\n",
               static_cast<unsigned long long>(beforeHeap),
               static_cast<unsigned long long>(device.currentAllocatedSize),
               [lifetime objectForKey:@"heap"] != nil);
        fflush(stdout);
        assert(![lifetime objectForKey:@"view"]);
        assert(![lifetime objectForKey:@"source"]);
        assert(![lifetime objectForKey:@"heap"]);
        [lifetime release];
        [device release];
    }
    puts("MOTION_VIEW_PASS");
}
'''


def main():
    with tempfile.TemporaryDirectory(prefix="yaagl-motion-view-") as directory:
        directory = pathlib.Path(directory)
        source = directory / "motion-view.mm"
        binary = directory / "motion-view"
        source.write_text(SOURCE)
        subprocess.run(("node", str(NATIVE.parent / "scripts/build-fsr-kernels.mjs"),
                        str(directory)), check=True)
        subprocess.run((
            "xcrun", "clang++", "-arch", "x86_64", "-std=c++20", "-fno-objc-arc",
            "-I", str(NATIVE), "-I", str(directory), str(source),
            str(NATIVE / "metalfx-backend.mm"),
            str(NATIVE / "fsr-framegeneration.mm"),
            str(NATIVE / "d3dmetal-transport.mm"),
            "-framework", "Foundation", "-framework", "Metal", "-framework", "MetalFX",
            "-o", str(binary)), check=True)
        subprocess.run((str(binary),), check=True)


if __name__ == "__main__":
    main()
