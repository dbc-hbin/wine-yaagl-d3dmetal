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
#include <cassert>
#include <cstdint>
#include "d3dmetal-transport.hpp"

int main() {
    @autoreleasepool {
        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        assert(device);
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
