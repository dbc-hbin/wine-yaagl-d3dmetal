#!/usr/bin/env python3
"""Run production Metal4 SR, including nonlinear precision and capped display motion."""

import pathlib
import subprocess
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
NATIVE = ROOT / "d3dmetal-sidecar"

SOURCE = r"""
#include <cassert>
#include <cmath>
#include <cstdio>
#include <dispatch/dispatch.h>
#include <future>
#include <memory>
#include <string>
#include <thread>
#include <vector>
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <MetalFX/MetalFX.h>
// Expose ownership only in this fixture so GPU readbacks cover the actual
// prepared-frame intermediates, not a duplicate implementation of the kernels.
#define private public
#include "metalfx-backend.hpp"
#undef private
#include "metalfx-backend.mm"

using namespace yaagl::pso::metalfx;

static id<MTLTexture> texture(id<MTLDevice> device, MTLPixelFormat format,
                              NSUInteger width, NSUInteger height, MTLTextureUsage usage) {
    auto descriptor = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:format
                                                                          width:width
                                                                         height:height
                                                                      mipmapped:NO];
    descriptor.storageMode = MTLStorageModePrivate;
    descriptor.hazardTrackingMode = MTLHazardTrackingModeTracked;
    descriptor.usage = usage;
    return [device newTextureWithDescriptor:descriptor];
}

static id<MTLTexture> sharedTexture(id<MTLDevice> device, MTLPixelFormat format,
                                   unsigned width, unsigned height) {
    auto descriptor = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:format
        width:width height:height mipmapped:NO];
    descriptor.storageMode = MTLStorageModeShared;
    descriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
    auto result = [device newTextureWithDescriptor:descriptor];
    assert(result);
    return result;
}

template<class T>
static std::vector<T> pixels(id<MTLDevice> device, id<MTLTexture> source,
                             unsigned channels) {
    auto copy = sharedTexture(device, source.pixelFormat, source.width, source.height);
    auto queue = [device newCommandQueue];
    auto command = [queue commandBuffer];
    auto blit = [command blitCommandEncoder];
    [blit copyFromTexture:source sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(0,0,0)
        sourceSize:MTLSizeMake(source.width,source.height,1) toTexture:copy
        destinationSlice:0 destinationLevel:0 destinationOrigin:MTLOriginMake(0,0,0)];
    [blit endEncoding];
    auto completed = dispatch_semaphore_create(0);
    [command addCompletedHandler:^(id<MTLCommandBuffer> finished) {
        assert(finished.status == MTLCommandBufferStatusCompleted);
        dispatch_semaphore_signal(completed);
    }];
    [command commit];
    assert(dispatch_semaphore_wait(completed, dispatch_time(DISPATCH_TIME_NOW,60*NSEC_PER_SEC)) == 0);
    assert(command.status == MTLCommandBufferStatusCompleted);
    dispatch_release(completed);
    std::vector<T> data(source.width * source.height * channels);
    [copy getBytes:data.data() bytesPerRow:source.width * channels * sizeof(T)
        fromRegion:MTLRegionMake2D(0,0,source.width,source.height) mipmapLevel:0];
    [queue release];
    [copy release];
    return data;
}

static std::shared_ptr<const ExecutionLease> execute(id<MTLDevice> device,
                                      const std::shared_ptr<const PreparedFrame>& prepared) {
    auto queue = [device newMTL4CommandQueue];
    auto allocator = [device newCommandAllocator];
    auto command = [device newCommandBuffer];
    auto fence = [device newFence];
    [command beginCommandBufferWithAllocator:allocator];
    auto init = [command computeCommandEncoder];
    [init updateFence:fence afterEncoderStages:MTLStageDispatch];
    [init endEncoding];
    Error error;
    std::shared_ptr<const ExecutionLease> lease;
    assert(prepared->encode(command, fence, lease, &error));
    [command endCommandBuffer];
    auto done = dispatch_semaphore_create(0);
    auto options = [MTL4CommitOptions new];
    [options addFeedbackHandler:^(id<MTL4CommitFeedback> feedback) {
        if (feedback.error) NSLog(@"%@", feedback.error);
        assert(!feedback.error);
        dispatch_semaphore_signal(done);
    }];
    [queue commit:&command count:1 options:options];
    assert(dispatch_semaphore_wait(done, dispatch_time(DISPATCH_TIME_NOW,60*NSEC_PER_SEC)) == 0);
    [options release];
    dispatch_release(done);
    [fence release];
    [command release];
    [allocator release];
    [queue release];
    return lease;
}

static std::shared_ptr<const PreparedFrame> prepare(
    const std::shared_ptr<Feature>& feature, FrameInfo frame, FrameOperations ops) {
    Error error;
    auto result = feature->prepare(frame,
        {frame.color,frame.depth,frame.motionVectors,frame.output,nullptr,nullptr,nullptr},
        &error, ops);
    if (!result) std::fprintf(stderr, "prepare: %s\n", error.message.c_str());
    assert(result);
    return result;
}

static FrameInfo frameInfo(id<MTLTexture> color, id<MTLTexture> depth,
                           id<MTLTexture> motion, id<MTLTexture> output,
                           unsigned width, unsigned height, unsigned outWidth,
                           unsigned outHeight, bool renderMotion) {
    FrameInfo f{};
    f.color=color; f.depth=depth; f.motionVectors=motion; f.output=output;
    f.inputContent={width,height};
    f.colorRect={3,2,width,height}; f.depthRect=f.colorRect;
    f.motionRect={5,7,renderMotion?width:outWidth,renderMotion?height:outHeight};
    f.outputRect={11,13,outWidth,outHeight};
    f.motionVectorScaleX=outWidth; f.motionVectorScaleY=-float(outHeight);
    f.preExposure=1; f.resetHistory=true;
    return f;
}

static std::shared_ptr<Feature> makeFeature(id<MTLDevice> device, id<MTL4Compiler> compiler,
                                           bool renderMotion, bool jittered=false) {
    CreateInfo ci{};
    ci.input={80,64}; ci.output={320,288};
    ci.featureFlags=(renderMotion?FeatureFlagMVLowRes:0) |
                    (jittered?FeatureFlagMVJittered:0);
    ci.dynamicOutput=true; ci.outputSubrects=true;
    Error error;
    auto feature=Feature::create({device,compiler},ci,&error);
    assert(feature);
    return feature;
}

static void cachedExtentRegression(id<MTLDevice> device, id<MTL4Compiler> compiler) {
    auto feature=makeFeature(device,compiler,true);
    auto color=sharedTexture(device,MTLPixelFormatRGBA16Float,88,72);
    auto depth=sharedTexture(device,MTLPixelFormatR32Float,88,72);
    auto motion=sharedTexture(device,MTLPixelFormatRG16Float,88,72);
    auto output=sharedTexture(device,MTLPixelFormatRGBA16Float,192,160);
    auto referenceOutput=sharedTexture(device,MTLPixelFormatRGBA16Float,192,160);
    std::vector<_Float16> colors(88*72*4), vectors(88*72*2,0);
    std::vector<float> depths(88*72,.5f);
    for(unsigned y=0;y<72;++y) for(unsigned x=0;x<88;++x) {
        for(unsigned c=0;c<3;++c) colors[(y*88+x)*4+c]=float((x+y)%16)/16;
        colors[(y*88+x)*4+3]=1;
    }
    [color replaceRegion:MTLRegionMake2D(0,0,88,72) mipmapLevel:0
        withBytes:colors.data() bytesPerRow:88*4*sizeof(_Float16)];
    [depth replaceRegion:MTLRegionMake2D(0,0,88,72) mipmapLevel:0
        withBytes:depths.data() bytesPerRow:88*sizeof(float)];
    [motion replaceRegion:MTLRegionMake2D(0,0,88,72) mipmapLevel:0
        withBytes:vectors.data() bytesPerRow:88*2*sizeof(_Float16)];
    auto small=frameInfo(color,depth,motion,output,64,48,160,128,true);
    small.resetHistory=false;
    auto first=prepare(feature,small,{});
    auto firstLease=execute(device,first);
    auto large=frameInfo(color,depth,motion,output,80,64,160,128,true);
    auto expanded=prepare(feature,large,{});
    auto expandedLease=execute(device,expanded);
    auto resumed=prepare(feature,small,{});
    assert(resumed->impl_->generation==first->impl_->generation);
    assert(resumed->impl_->activation!=first->impl_->activation);
    auto consecutive=prepare(feature,small,{});
    assert(consecutive->impl_->activation==resumed->impl_->activation);
    // An older retained frame must not consume the reactivation reset.
    auto olderLease=execute(device,first);
    assert(first->impl_->generation->encodedActivation!=resumed->impl_->activation);
    auto resumedLease=execute(device,resumed);
    assert(resumedLease->impl_->stagedDepth.width==small.inputContent.width);
    assert(resumedLease->impl_->stagedDepth.height==small.inputContent.height);
    auto reference=makeFeature(device,compiler,true);
    small.output=referenceOutput;
    auto referenceLease=execute(device,prepare(reference,small,{}));
    auto actual=pixels<_Float16>(device,output,4);
    auto expected=pixels<_Float16>(device,referenceOutput,4);
    for(unsigned y=13;y<13+128;++y) for(unsigned x=11;x<11+160;++x)
        for(unsigned c=0;c<4;++c) {
            auto index=(y*192+x)*4+c;
            assert(std::isfinite(float(actual[index])));
            assert(std::fabs(float(actual[index])-float(expected[index]))<.003f);
        }
    [referenceOutput release]; [output release]; [motion release];
    [depth release]; [color release];
}

static void dynamicExtentHistoryRegression(id<MTLDevice> device, id<MTL4Compiler> compiler) {
    constexpr unsigned width = 80, height = 64, outWidth = 160, outHeight = 128;
    CreateInfo creation{};
    creation.input = {width, height};
    creation.output = {outWidth, outHeight};
    creation.featureFlags = FeatureFlagMVLowRes;
    creation.dynamicOutput = true;
    Error error;
    auto feature = Feature::create({device, compiler}, creation, &error);
    assert(feature);
    auto color = sharedTexture(device, MTLPixelFormatRGBA16Float, width, height);
    auto depth = sharedTexture(device, MTLPixelFormatR32Float, width, height);
    auto motion = sharedTexture(device, MTLPixelFormatRG16Float, width, height);
    auto output = texture(device, MTLPixelFormatRGBA16Float, outWidth, outHeight,
                          MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite);
    auto referenceOutput = texture(device, MTLPixelFormatRGBA16Float, outWidth, outHeight,
                                   MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite);
    auto descriptor = [MTLFXTemporalScalerDescriptor new];
    descriptor.inputWidth = width; descriptor.inputHeight = height;
    descriptor.outputWidth = outWidth; descriptor.outputHeight = outHeight;
    descriptor.colorTextureFormat = MTLPixelFormatRGBA16Float;
    descriptor.depthTextureFormat = MTLPixelFormatR32Float;
    descriptor.motionTextureFormat = MTLPixelFormatRG16Float;
    descriptor.outputTextureFormat = MTLPixelFormatRGBA16Float;
    descriptor.inputContentPropertiesEnabled = YES;
    descriptor.requiresSynchronousInitialization = NO;
    descriptor.inputContentMinScale =
        [MTLFXTemporalScalerDescriptor supportedInputContentMinScaleForDevice:device];
    descriptor.inputContentMaxScale =
        [MTLFXTemporalScalerDescriptor supportedInputContentMaxScaleForDevice:device];
    auto reference = [descriptor newTemporalScalerWithDevice:device compiler:compiler];
    [descriptor release];
    assert(reference);
    const Extent extents[] = {{80,64},{60,48},{60,48},{75,60},{75,60},{80,64},
                              {60,48},{75,60},{60,48},{64,48},{64,48},{80,64}};
    for (unsigned n = 0; n < std::size(extents); ++n) {
        unsigned w = extents[n].width, h = extents[n].height;
        std::vector<_Float16> colors(width * height * 4), vectors(width * height * 2);
        std::vector<float> depths(width * height);
        for (unsigned y = 0; y < height; ++y) for (unsigned x = 0; x < width; ++x) {
            unsigned sx = std::min(x, w - 1), sy = std::min(y, h - 1), i = y * width + x;
            for (unsigned c = 0; c < 3; ++c)
                colors[i * 4 + c] = float(((sx + n * 2) / 3 + sy / 5 + c) % 7) / 7.f;
            colors[i * 4 + 3] = 1;
            depths[i] = .2f + .5f * float((sx + n) % w) / w;
            vectors[i * 2] = -2.f;
            vectors[i * 2 + 1] = 0;
        }
        // The independent native scaler consumes CPU-clamped capacity textures,
        // not the sidecar's staging output or reset calculation.
        [color replaceRegion:MTLRegionMake2D(0,0,width,height) mipmapLevel:0
            withBytes:colors.data() bytesPerRow:width * 4 * sizeof(_Float16)];
        [depth replaceRegion:MTLRegionMake2D(0,0,width,height) mipmapLevel:0
            withBytes:depths.data() bytesPerRow:width * sizeof(float)];
        [motion replaceRegion:MTLRegionMake2D(0,0,width,height) mipmapLevel:0
            withBytes:vectors.data() bytesPerRow:width * 2 * sizeof(_Float16)];
        FrameInfo frame{};
        frame.color = color; frame.depth = depth; frame.motionVectors = motion; frame.output = output;
        frame.inputContent = {w,h};
        frame.colorRect = frame.depthRect = frame.motionRect = {0,0,w,h};
        frame.outputRect = {0,0,outWidth,outHeight};
        frame.motionVectorScaleX = frame.motionVectorScaleY = 1;
        frame.preExposure = 1;
        frame.jitterOffsetX = n % 2 ? .25f : -.25f;
        frame.jitterOffsetY = -frame.jitterOffsetX;
        frame.resetHistory = n == 0 || n == 8;
        auto lease = execute(device, prepare(feature, frame, {}));
        auto queue = [device newMTL4CommandQueue];
        auto allocator = [device newCommandAllocator];
        auto command = [device newCommandBuffer];
        auto fence = [device newFence];
        auto rd = [MTLResidencySetDescriptor new];
        rd.initialCapacity = 4;
        NSError* nativeError = nil;
        auto residency = [device newResidencySetWithDescriptor:rd error:&nativeError];
        [rd release];
        assert(residency);
        for (id<MTLAllocation> allocation : {color, depth, motion, referenceOutput})
            [residency addAllocation:allocation];
        [residency commit];
        [command beginCommandBufferWithAllocator:allocator];
        [command useResidencySet:residency];
        auto init = [command computeCommandEncoder];
        [init updateFence:fence afterEncoderStages:MTLStageDispatch];
        [init endEncoding];
        reference.colorTexture = color; reference.depthTexture = depth;
        reference.motionTexture = motion; reference.outputTexture = referenceOutput;
        reference.inputContentWidth = w; reference.inputContentHeight = h;
        reference.preExposure = 1;
        reference.jitterOffsetX = frame.jitterOffsetX;
        reference.jitterOffsetY = frame.jitterOffsetY;
        reference.motionVectorScaleX = reference.motionVectorScaleY = 1;
        reference.reset = frame.resetHistory || n == 9 || n == 11;
        reference.fence = fence;
        [reference encodeToCommandBuffer:command];
        [command endCommandBuffer];
        auto completed = dispatch_semaphore_create(0);
        auto options = [MTL4CommitOptions new];
        [options addFeedbackHandler:^(id<MTL4CommitFeedback> feedback) {
            assert(!feedback.error);
            dispatch_semaphore_signal(completed);
        }];
        [queue commit:&command count:1 options:options];
        assert(dispatch_semaphore_wait(completed,
            dispatch_time(DISPATCH_TIME_NOW, 60 * NSEC_PER_SEC)) == 0);
        auto actual = pixels<_Float16>(device, output, 4);
        auto expected = pixels<_Float16>(device, referenceOutput, 4);
        for (unsigned i = 0; i < actual.size(); ++i) {
            assert(std::isfinite(float(actual[i])));
            assert(std::fabs(float(actual[i]) - float(expected[i])) < .003f);
        }
        reference.fence = nil;
        dispatch_release(completed);
        [options release]; [residency release]; [fence release];
        [command release]; [allocator release]; [queue release];
    }
    feature.reset();
    [reference release]; [referenceOutput release]; [output release];
    [motion release]; [depth release]; [color release];
    puts("SR_DYNAMIC_HISTORY_PASS: native continuity, caller reset and aspect transitions match");
}

static float inverseSrgb(float value) {
    return value <= .04045f ? value/12.92f : std::pow((value+.055f)/1.055f,2.4f);
}

static float inversePq(float value) {
    float p=std::pow(value,1.f/78.84375f);
    return std::pow(std::max(p-.8359375f,0.f)/(18.8515625f-18.6875f*p),
                    1.f/.1593017578125f);
}

static void colorRegression(id<MTLDevice> device, id<MTL4Compiler> compiler) {
    auto feature=makeFeature(device,compiler,true);
    auto depth=sharedTexture(device,MTLPixelFormatR32Float,88,72);
    auto motion=sharedTexture(device,MTLPixelFormatRG16Float,88,80);
    std::vector<float> z(88*72,.5f);
    std::vector<_Float16> mv(88*80*2,0);
    [depth replaceRegion:MTLRegionMake2D(0,0,88,72) mipmapLevel:0 withBytes:z.data() bytesPerRow:88*4];
    [motion replaceRegion:MTLRegionMake2D(0,0,88,80) mipmapLevel:0 withBytes:mv.data() bytesPerRow:88*4];
    std::shared_ptr<ScalerGeneration> firstGeneration;
    // Uniform baseline, near-black ramp, hardware sRGB control, then reuse.
    for (unsigned mode=0;mode<6;++mode) {
        bool ramp=mode==1||mode==2;
        auto format=mode==2?MTLPixelFormatRGBA8Unorm_sRGB:
                    mode==4?MTLPixelFormatBGRA8Unorm:MTLPixelFormatRGBA8Unorm;
        auto color=sharedTexture(device,format,88,72);
        auto output=texture(device,mode==4?MTLPixelFormatBGRA8Unorm:MTLPixelFormatRGBA8Unorm,180,160,
            MTLTextureUsageShaderRead|MTLTextureUsageShaderWrite);
        std::vector<unsigned char> input(88*72*4,255);
        for(unsigned y=0;y<72;++y) for(unsigned x=0;x<88;++x)
            for(unsigned c=0;c<3;++c) input[(y*88+x)*4+c]=
                mode==5?64:!ramp?6:static_cast<unsigned char>(2+std::min(7u,(x>=3?x-3:0)/8)*2);
        [color replaceRegion:MTLRegionMake2D(0,0,88,72) mipmapLevel:0
            withBytes:input.data() bytesPerRow:88*4];
        const unsigned width=mode==0?80:64, height=mode==0?64:48;
        auto f=frameInfo(color,depth,motion,output,width,height,160,128,true);
        FrameOperations ops{};
        ops.colorTransfer=mode==5?ColorTransfer::PQ:ColorTransfer::SRGB;
        auto prepared=prepare(feature,f,ops);
        auto generation=prepared->impl_->generation;
        assert(generation->colorFormat==MTLPixelFormatRGBA16Float);
        assert(generation->outputFormat==MTLPixelFormatRGBA16Float);
        if(mode==0) firstGeneration=generation;
        else assert(generation==firstGeneration); // hardware decode still shares linear descriptor
        auto lease=execute(device,prepared);
        auto linear=pixels<_Float16>(device,lease->impl_->linearColor,4);
        auto temporal=pixels<_Float16>(device,lease->impl_->privateOutput,4);
        auto final=pixels<unsigned char>(device,output,4);
        for(unsigned y=0;y<generation->inputCapacityHeight;++y)
            for(unsigned x=0;x<generation->inputCapacityWidth;++x)
                for(unsigned c=0;c<4;++c)
                    assert(linear[(y*generation->inputCapacityWidth+x)*4+c] ==
                           linear[(std::min(y,height-1)*generation->inputCapacityWidth+
                                   std::min(x,width-1))*4+c]);
        for(unsigned x=0;x<width;++x) {
            const float encoded=input[((2+height/2)*88+3+x)*4]/255.f;
            const float observed=linear[(height/2*generation->inputCapacityWidth+x)*4];
            const float expected=mode==5?inversePq(encoded):inverseSrgb(encoded);
            if (format==MTLPixelFormatRGBA8Unorm_sRGB) {
                // MSL 4.1 section 8.7.7 permits hardware decode error of half
                // an original 8-bit sRGB code. Include the subsequent half
                // store by rounding both ends of that specified interval.
                const float lower=_Float16(inverseSrgb(encoded-.5f/255));
                const float upper=_Float16(inverseSrgb(encoded+.5f/255));
                assert(observed>=lower && observed<=upper);
            } else {
                assert(std::fabs(observed-expected) < 0.000015f);
            }
        }
        unsigned previous=0;
        for(unsigned band=0;band<(ramp?8:1);++band) {
            unsigned x=!ramp?80:(band*8+4)*160/width;
            unsigned value=final[((13+64)*180+11+x)*4];
            const unsigned expected=mode==5?64:!ramp?6:2+band*2;
            assert(std::abs(int(value)-int(expected))<=1);
            if(band) assert(value>previous);
            previous=value;
            assert(float(temporal[(64*160+x)*4])>0);
            std::printf("SR_COLOR mode=%u band=%u linear=%g temporal=%g final=%u\n",
                mode,band,float(linear[(height/2*generation->inputCapacityWidth+width/2)*4]),
                float(temporal[(64*160+x)*4]),value);
        }
        [output release]; [color release];
    }
    // Pure precision policy controls, including unchanged 32F and linear paths.
    assert(workingColorFormat(MTLPixelFormatRGBA32Float,ColorTransfer::SRGB)==MTLPixelFormatRGBA32Float);
    assert(workingColorFormat(MTLPixelFormatRGBA16Float,ColorTransfer::Linear)==MTLPixelFormatRGBA16Float);
    assert(workingColorFormat(MTLPixelFormatRGBA8Unorm,ColorTransfer::Linear)==MTLPixelFormatRGBA8Unorm);
    [motion release]; [depth release];
}

static void motionRegression(id<MTLDevice> device, id<MTL4Compiler> compiler,
                              bool renderMotion, bool capped, bool nonuniform, bool jittered) {
    auto feature=makeFeature(device,compiler,renderMotion,jittered);
    auto reference=makeFeature(device,compiler,renderMotion,jittered);
    auto color=sharedTexture(device,MTLPixelFormatRGBA16Float,88,72);
    auto depth=sharedTexture(device,MTLPixelFormatR32Float,88,72);
    auto motion=sharedTexture(device,MTLPixelFormatRG16Float,330,300);
    auto referenceMotion=sharedTexture(device,MTLPixelFormatRG16Float,330,300);
    auto output=texture(device,MTLPixelFormatRGBA16Float,340,310,
        MTLTextureUsageShaderRead|MTLTextureUsageShaderWrite);
    auto referenceOutput=texture(device,MTLPixelFormatRGBA16Float,340,310,
        MTLTextureUsageShaderRead|MTLTextureUsageShaderWrite);
    std::vector<float> z(88*72,.5f);
    [depth replaceRegion:MTLRegionMake2D(0,0,88,72) mipmapLevel:0 withBytes:z.data() bytesPerRow:88*4];
    float previousJitter=0;
    // Seed larger input capacity, then reset on extent transition and accumulate history.
    for(unsigned n=0;n<7;++n) {
        unsigned w=n?64:80, h=n?48:64;
        unsigned ow=capped&&n!=5?256:n==5&&!capped?128:192;
        unsigned oh=capped&&n!=5?240:n==5&&!capped?96:144;
        auto f=frameInfo(color,depth,motion,output,w,h,ow,oh,renderMotion);
        f.resetHistory=n==0;
        f.jitterOffsetX=jittered?(n%2?.25f:-.25f):0;
        f.jitterOffsetY=-f.jitterOffsetX;
        FrameOperations ops{}; ops.capOutputToTemporalMaxScale=capped;
        auto layout=temporalOutputLayout(*feature->impl_,f,ops);
        unsigned qw=layout.width,qh=layout.height;
        std::vector<_Float16> colors(88*72*4,1), vectors(330*300*2,0), refVectors(330*300*2,0);
        // A moving textured plane; an off-center region has a different velocity.
        for(unsigned y=0;y<72;++y) for(unsigned x=0;x<88;++x) {
            float u=(float(x)-3+.5f-f.jitterOffsetX)/w;
            float v=(float(y)-2+.5f-f.jitterOffsetY)/h;
            float speed=nonuniform&&u>.31f?.035f:.0125f;
            float value=.4f+.12f*std::sin((u-speed*n)*35)+.08f*std::cos(v*29);
            for(unsigned c=0;c<3;++c) colors[(y*88+x)*4+c]=value;
        }
        [color replaceRegion:MTLRegionMake2D(0,0,88,72) mipmapLevel:0
            withBytes:colors.data() bytesPerRow:88*8];
        unsigned mw=renderMotion?w:ow,mh=renderMotion?h:oh;
        for(unsigned y=0;y<mh;++y) for(unsigned x=0;x<mw;++x) {
            float u=(x+.5f)/mw;
            float vx=-(nonuniform&&u>.31f?.035f:.0125f);
            float vy=nonuniform&&u>.31f?.0075f:-.01f;
            // Same render-jittered camera convention, not the disputed F2 formula.
            float dj=f.jitterOffsetX-previousJitter;
            vectors[((y+7)*330+x+5)*2]=(vx-dj/w)*(renderMotion?w:ow)/f.motionVectorScaleX;
            vectors[((y+7)*330+x+5)*2+1]=(vy+dj/h)*(renderMotion?h:oh)/f.motionVectorScaleY;
        }
        [motion replaceRegion:MTLRegionMake2D(0,0,330,300) mipmapLevel:0
            withBytes:vectors.data() bytesPerRow:330*4];
        auto prepared=prepare(feature,f,ops);
        auto lease=execute(device,prepared);
        auto generation=prepared->impl_->generation;
        assert(generation->inputCapacityWidth>=w && generation->inputCapacityHeight>=h);
        if(n && capped) assert(generation->inputCapacityWidth==80);
        bool adapted=capped&&!renderMotion&&layout.capped;
        float sx=f.motionVectorScaleX*(adapted?float(qw)/ow:1);
        float sy=f.motionVectorScaleY*(adapted?float(qh)/oh:1);
        assert(std::fabs([generation->scaler motionVectorScaleX]-sx)<1e-5f);
        assert(std::fabs([generation->scaler motionVectorScaleY]-sy)<1e-5f);
        assert([generation->scaler jitterOffsetX]==f.jitterOffsetX);
        assert([generation->scaler jitterOffsetY]==f.jitterOffsetY);
        unsigned rw=renderMotion?w:qw,rh=renderMotion?h:qh;
        auto staged=pixels<_Float16>(device,lease->impl_->stagedMotion,2);
        for(unsigned y=0;y<rh;++y) for(unsigned x=0;x<rw;++x) {
            unsigned sourceX=adapted?std::min(unsigned((x+.5f)*ow/qw),ow-1):x;
            unsigned sourceY=adapted?std::min(unsigned((y+.5f)*oh/qh),oh-1):y;
            for(unsigned c=0;c<2;++c) {
                _Float16 expected=vectors[((sourceY+7)*330+sourceX+5)*2+c];
                assert(staged[(y*lease->impl_->stagedMotion.width+x)*2+c]==expected);
                refVectors[((y+7)*330+x+5)*2+c]=expected;
            }
        }
        [referenceMotion replaceRegion:MTLRegionMake2D(0,0,330,300) mipmapLevel:0
            withBytes:refVectors.data() bytesPerRow:330*4];
        auto rf=frameInfo(color,depth,referenceMotion,referenceOutput,w,h,qw,qh,renderMotion);
        rf.motionVectorScaleX=sx; rf.motionVectorScaleY=sy;
        rf.jitterOffsetX=f.jitterOffsetX; rf.jitterOffsetY=f.jitterOffsetY;
        rf.resetHistory=f.resetHistory;
        auto refPrepared=prepare(reference,rf,{});
        auto refLease=execute(device,refPrepared);
        auto actual=pixels<_Float16>(device,output,4);
        auto expected=pixels<_Float16>(device,referenceOutput,4);
        float maxError=0;
        for(unsigned y=0;y<oh;++y) for(unsigned x=0;x<ow;++x) {
            bool inside=x>=layout.placementX&&y>=layout.placementY&&
                x<layout.placementX+qw&&y<layout.placementY+qh;
            for(unsigned c=0;c<3;++c) {
                float value=actual[((y+13)*340+x+11)*4+c];
                float want=inside?float(expected[((y-layout.placementY+13)*340+
                    x-layout.placementX+11)*4+c]):0;
                assert(std::isfinite(value));
                maxError=std::max(maxError,std::fabs(value-want));
            }
        }
        assert(maxError<.003f);
        std::printf("SR_MOTION render=%d cap=%d field=%d jitter=%d frame=%u R=%ux%u C=%lux%lu "
                    "O=%ux%u Q=%ux%u scale=%g,%g offcenter=%g,%g maxError=%g\n",
            renderMotion,capped,nonuniform,jittered,n,w,h,
            generation->inputCapacityWidth,generation->inputCapacityHeight,ow,oh,qw,qh,
            sx,sy,float(staged[(rh/3*lease->impl_->stagedMotion.width+rw/4)*2]),
            float(staged[(rh/3*lease->impl_->stagedMotion.width+rw/4)*2+1]),maxError);
        previousJitter=f.jitterOffsetX;
    }
    [referenceOutput release]; [output release]; [referenceMotion release];
    [motion release]; [depth release]; [color release];
}

static void scratchReturnRegression(id<MTLDevice> device, id<MTL4Compiler> compiler) {
    auto feature=makeFeature(device,compiler,true);
    auto& pool=*feature->impl_;
    const auto fill=[&](ExecutionResources& resources) {
        for (auto* slot : {&resources.finishParams,&resources.debugParams,
                &resources.motionResampleParams,&resources.maskParams,&resources.linearizeParams}) {
            *slot=[device newBufferWithLength:256 options:MTLResourceStorageModeShared];
            assert(*slot);
        }
        for (auto* slot : {&resources.stagedReactive,&resources.stagedMotion,
                &resources.stagedDepth,&resources.stagedColor,&resources.combinedMask,
                &resources.linearColor,&resources.convertedExposure,&resources.privateOutput}) {
            *slot=texture(device,MTLPixelFormatRGBA8Unorm,16,16,MTLTextureUsageShaderRead);
            assert(*slot);
        }
    };
    const auto accounting=[&] {
        NSUInteger bytes=0;
        for (auto value : pool.idleTextures) if (value) bytes+=scratchBytes(value);
        assert(bytes==pool.idleTextureBytes && bytes<=pool.kMaxIdleTextureBytes);
    };
    ExecutionResources first, second;
    fill(first); fill(second);
    const std::array firstTextures{first.stagedReactive,first.stagedMotion,
        first.stagedDepth,first.stagedColor,first.combinedMask,first.linearColor,
        first.convertedExposure,first.privateOutput};
    const std::array secondTextures{second.stagedReactive,second.stagedMotion,
        second.stagedDepth,second.stagedColor,second.combinedMask,second.linearColor,
        second.convertedExposure,second.privateOutput};
    const std::array returnedBuffers{first.finishParams,first.debugParams,
        first.motionResampleParams,first.maskParams,first.linearizeParams,second.finishParams,
        second.debugParams,second.motionResampleParams,second.maskParams,second.linearizeParams};
    assert(firstTextures.size()<pool.idleTextures.size() &&
        firstTextures.size()+secondTextures.size()>pool.idleTextures.size());
    assert(pool.idleBuffers.size()<returnedBuffers.size());
    pool.returnResources(first);
    for (unsigned i=0;i<firstTextures.size();++i) assert(pool.idleTextures[i]==firstTextures[i]);
    pool.returnResources(second);
    for (unsigned i=0;i<pool.idleBuffers.size();++i)
        assert(pool.idleBuffers[i]==returnedBuffers[i]);
    const auto appended=pool.idleTextures.size()-firstTextures.size();
    const auto replaced=secondTextures.size()-appended;
    assert(pool.nextTextureSlot==replaced);
    for (unsigned i=0;i<replaced;++i)
        assert(pool.idleTextures[i]==secondTextures[appended+i]);
    for (auto i=replaced;i<firstTextures.size();++i)
        assert(pool.idleTextures[i]==firstTextures[i]);
    for (unsigned i=0;i<appended;++i)
        assert(pool.idleTextures[firstTextures.size()+i]==secondTextures[i]);
    accounting();
    // Removal must deduct the same allocation bytes used during insertion.
    auto taken=pool.takeTexture(MTLPixelFormatRGBA8Unorm,16,16,MTLTextureUsageShaderRead);
    assert(taken);
    accounting();
    [taken release];
    feature->markDormant();
    accounting();

    feature=makeFeature(device,compiler,true);
    auto& capped=*feature->impl_;
    NSUInteger allocationBytes=0;
    for (unsigned i=0;i<capped.idleTextures.size();++i) {
        ExecutionResources resources;
        resources.privateOutput=texture(device,MTLPixelFormatRGBA8Unorm,2048,2048,
            MTLTextureUsageShaderRead);
        assert(resources.privateOutput);
        allocationBytes=scratchBytes(resources.privateOutput);
        assert(allocationBytes && allocationBytes<=capped.kMaxIdleTextureBytes);
        capped.returnResources(resources);
        assert(!resources.privateOutput);
        NSUInteger bytes=0;
        for (auto value : capped.idleTextures) if (value) bytes+=scratchBytes(value);
        assert(bytes==capped.idleTextureBytes && bytes<=capped.kMaxIdleTextureBytes);
    }
    assert(capped.idleTextureBytes==
        std::min(NSUInteger(capped.idleTextures.size()),
                 capped.kMaxIdleTextureBytes/allocationBytes)*allocationBytes);

    ExecutionResources late;
    fill(late);
    std::promise<void> start, returned;
    auto ready=start.get_future();
    auto finished=returned.get_future();
    std::thread completion([&] {
        assert(ready.wait_for(std::chrono::seconds(10))==std::future_status::ready);
        capped.returnResources(late);
        returned.set_value();
    });
    start.set_value();
    feature->markDormant();
    assert(finished.wait_for(std::chrono::seconds(10))==std::future_status::ready);
    completion.join();
    for (auto value : capped.idleTextures) assert(!value);
    for (auto value : capped.idleBuffers) assert(!value);
    assert(!capped.idleTextureBytes && !capped.nextTextureSlot);
    // A completion arriving after dormancy must also leave every pool empty.
    fill(late);
    capped.returnResources(late);
    for (auto value : capped.idleTextures) assert(!value);
    for (auto value : capped.idleBuffers) assert(!value);
    assert(!capped.idleTextureBytes);
    puts("SR_SCRATCH_RETURN_PASS");
}

static void tableOwnershipRegression(id<MTLDevice> device, id<MTL4Compiler> compiler) {
    auto feature=makeFeature(device,compiler,true);
    auto color=sharedTexture(device,MTLPixelFormatRGBA8Unorm,64,64);
    auto depth=sharedTexture(device,MTLPixelFormatR32Float,64,64);
    auto motion=sharedTexture(device,MTLPixelFormatRG16Float,64,64);
    auto exposure=sharedTexture(device,MTLPixelFormatR32Float,1,1);
    auto nextExposure=sharedTexture(device,MTLPixelFormatR32Float,1,1);
    auto output=texture(device,MTLPixelFormatRGBA8Unorm,128,128,
        MTLTextureUsageShaderRead|MTLTextureUsageShaderWrite);
    std::vector<unsigned char> colors(64*64*4,64);
    std::vector<float> depths(64*64,.5f);
    std::vector<_Float16> motions(64*64*2,0);
    float firstValue=1, nextValue=.5f;
    [color replaceRegion:MTLRegionMake2D(0,0,64,64) mipmapLevel:0 withBytes:colors.data() bytesPerRow:64*4];
    [depth replaceRegion:MTLRegionMake2D(0,0,64,64) mipmapLevel:0 withBytes:depths.data() bytesPerRow:64*4];
    [motion replaceRegion:MTLRegionMake2D(0,0,64,64) mipmapLevel:0 withBytes:motions.data() bytesPerRow:64*4];
    [exposure replaceRegion:MTLRegionMake2D(0,0,1,1) mipmapLevel:0 withBytes:&firstValue bytesPerRow:4];
    [nextExposure replaceRegion:MTLRegionMake2D(0,0,1,1) mipmapLevel:0 withBytes:&nextValue bytesPerRow:4];
    FrameInfo frame{};
    frame.color=color; frame.depth=depth; frame.motionVectors=motion; frame.output=output;
    frame.exposureMode=ExposureMode::Texture; frame.exposureTexture=exposure;
    frame.inputContent={64,64}; frame.colorRect={0,0,64,64};
    frame.depthRect=frame.motionRect=frame.colorRect; frame.outputRect={0,0,128,128};
    frame.resetHistory=true;
    FrameOperations operations{};
    operations.colorTransfer=ColorTransfer::SRGB;
    const auto prepareFrame = [&](const std::shared_ptr<Feature>& owner) {
        Error error;
        auto prepared=owner->prepare(frame,
            {color,depth,motion,output,frame.exposureTexture,nullptr,nullptr},&error,operations);
        if (!prepared) std::fprintf(stderr,"table prepare: %s\n",error.message.c_str());
        assert(prepared);
        return prepared;
    };
    auto prepared=prepareFrame(feature);
    auto a=execute(device,prepared);
    id firstFsr=[a->impl_->fsrArgumentTable retain];
    id firstExposure=[a->impl_->argumentTable retain];
    assert(firstFsr && firstExposure);
    // GPU completion does not retire a lease retained by its caller.
    auto b=execute(device,prepared);
    assert(b->impl_->fsrArgumentTable != firstFsr);
    assert(b->impl_->argumentTable != firstExposure);
    auto initial=pixels<unsigned char>(device,output,4);
    assert(std::abs(int(initial[(64*128+64)*4])-64)<=1);
    a.reset();
    auto c=execute(device,prepared);
    assert(c->impl_->fsrArgumentTable == firstFsr);
    assert(c->impl_->argumentTable == firstExposure);
    b.reset(); c.reset(); prepared.reset();

    const auto idleFsr=feature->impl_->idleFsrArgumentTables;
    const auto idleExposure=feature->impl_->idleArgumentTables;
    frame.exposureTexture=nextExposure;
    prepared=prepareFrame(feature);
    auto next=execute(device,prepared);
    assert(std::find(idleFsr.begin(),idleFsr.end(),next->impl_->fsrArgumentTable)!=idleFsr.end());
    assert(std::find(idleExposure.begin(),idleExposure.end(),next->impl_->argumentTable)!=idleExposure.end());
    auto converted=pixels<_Float16>(device,next->impl_->convertedExposure,1);
    assert(float(converted[0])==nextValue);
    auto reusedOutput=pixels<unsigned char>(device,output,4);
    auto reference=makeFeature(device,compiler,true);
    auto referenceFrame=prepareFrame(reference);
    auto referenceLease=execute(device,referenceFrame);
    auto freshOutput=pixels<unsigned char>(device,output,4);
    for (std::size_t i=0;i<freshOutput.size();++i)
        assert(std::abs(int(freshOutput[i])-int(reusedOutput[i]))<=1);

    feature->markDormant();
    auto afterDormant=execute(device,prepared);
    assert(afterDormant->impl_->fsrArgumentTable != next->impl_->fsrArgumentTable);
    assert(afterDormant->impl_->argumentTable != next->impl_->argumentTable);
    next.reset(); afterDormant.reset(); prepared.reset();
    for (id table : feature->impl_->idleFsrArgumentTables) assert(!table);
    for (id table : feature->impl_->idleArgumentTables) assert(!table);
    for (const auto& bundle : feature->impl_->idleBundles)
        assert(!bundle.resources.fsrArgumentTable && !bundle.resources.argumentTable);
    [firstFsr release]; [firstExposure release];
    referenceLease.reset(); referenceFrame.reset(); reference.reset(); feature.reset();
    [output release]; [nextExposure release]; [exposure release];
    [motion release]; [depth release]; [color release];
    puts("SR_TABLE_OWNERSHIP_PASS: retained leases isolated; retired tables reused; exposure rebound; dormant pools empty");
}

static void pipelineTransitionRegression(id<MTLDevice> device, id<MTL4Compiler> compiler) {
    auto feature=makeFeature(device,compiler,true);
    auto color=sharedTexture(device,MTLPixelFormatRGBA16Float,64,64);
    auto depth=sharedTexture(device,MTLPixelFormatR32Float,64,64);
    auto motion=sharedTexture(device,MTLPixelFormatRG16Float,64,64);
    auto mask=sharedTexture(device,MTLPixelFormatR8Unorm,64,64);
    auto output=texture(device,MTLPixelFormatRGBA16Float,128,128,
        MTLTextureUsageShaderRead|MTLTextureUsageShaderWrite);
    std::vector<_Float16> colors(64*64*4), motions(64*64*2,0);
    for (unsigned y=0;y<64;++y) for (unsigned x=0;x<64;++x) {
        const unsigned pixel=(y*64+x)*4;
        for (unsigned c=0;c<3;++c)
            colors[pixel+c]=_Float16(.05f+.8f*float((x*3+y*5+c*11)%257)/256.f);
        colors[pixel+3]=1;
    }
    std::vector<float> depths(64*64,.5f);
    std::vector<unsigned char> masks(64*64,64);
    [color replaceRegion:MTLRegionMake2D(0,0,64,64) mipmapLevel:0 withBytes:colors.data() bytesPerRow:64*8];
    [depth replaceRegion:MTLRegionMake2D(0,0,64,64) mipmapLevel:0 withBytes:depths.data() bytesPerRow:64*4];
    [motion replaceRegion:MTLRegionMake2D(0,0,64,64) mipmapLevel:0 withBytes:motions.data() bytesPerRow:64*4];
    [mask replaceRegion:MTLRegionMake2D(0,0,64,64) mipmapLevel:0 withBytes:masks.data() bytesPerRow:64];
    FrameInfo frame{};
    frame.color=color; frame.depth=depth; frame.motionVectors=motion; frame.output=output;
    frame.inputContent={64,64}; frame.colorRect={0,0,64,64};
    frame.depthRect=frame.motionRect=frame.reactiveRect=frame.compositionRect=frame.colorRect;
    frame.outputRect={0,0,128,128}; frame.resetHistory=true;
    for (unsigned mode : {0u,1u,2u,0u,3u,2u}) {
        FrameOperations ops{};
        ops.sharpening=mode==0;
        ops.sharpness=.5f;
        ops.combineCompositionMask=mode==1 || mode==2;
        ops.colorTransfer=mode==2 ? ColorTransfer::SRGB : ColorTransfer::Linear;
        ops.drawDebugView=mode==3;
        frame.compositionMask=ops.combineCompositionMask ? mask : nullptr;
        TextureSet textures{color,depth,motion,output,nullptr,nullptr,frame.compositionMask};
        Error error;
        auto prepared=feature->prepare(frame,textures,&error,ops);
        if (!prepared) std::fprintf(stderr,"pipeline transition: %s\n",error.message.c_str());
        assert(prepared);
        auto lease=execute(device,prepared);
        auto actual=pixels<_Float16>(device,output,4);
        if (ops.combineCompositionMask) {
            auto combined=pixels<unsigned char>(device,
                lease->impl_->combinedMask ? lease->impl_->combinedMask : mask,1);
            assert(combined==masks);
        }
        auto reference=makeFeature(device,compiler,true);
        auto referenceFrame=reference->prepare(frame,textures,&error,ops);
        assert(referenceFrame);
        auto referenceLease=execute(device,referenceFrame);
        auto expected=pixels<_Float16>(device,output,4);
        for (std::size_t i=0;i<actual.size();++i)
            assert(std::isfinite(float(actual[i])) &&
                   std::fabs(float(actual[i])-float(expected[i]))<.003f);
    }
    feature.reset();
    [output release]; [mask release]; [motion release]; [depth release]; [color release];
    puts("SR_PIPELINE_TRANSITIONS_PASS: finish-only, masks, transfer and debug match fresh features");
}

static void compositionMaskRegression(id<MTLDevice> device, id<MTL4Compiler> compiler) {
    auto feature=makeFeature(device,compiler,true);
    auto reference=makeFeature(device,compiler,true);
    auto color=sharedTexture(device,MTLPixelFormatRGBA16Float,64,48);
    auto depth=sharedTexture(device,MTLPixelFormatR32Float,64,48);
    auto motion=sharedTexture(device,MTLPixelFormatRG16Float,64,48);
    auto mask=sharedTexture(device,MTLPixelFormatR8Unorm,64,48);
    auto zero=sharedTexture(device,MTLPixelFormatR8Unorm,64,48);
    auto reactive=sharedTexture(device,MTLPixelFormatR8Unorm,64,48);
    auto floating=sharedTexture(device,MTLPixelFormatR32Float,64,48);
    auto oversized=sharedTexture(device,MTLPixelFormatR8Unorm,72,52);
    auto output=texture(device,MTLPixelFormatRGBA16Float,128,96,
        MTLTextureUsageShaderRead|MTLTextureUsageShaderWrite);
    auto referenceOutput=texture(device,MTLPixelFormatRGBA16Float,128,96,
        MTLTextureUsageShaderRead|MTLTextureUsageShaderWrite);
    std::vector<unsigned char> masks(64*48), zeros(64*48,0), reactions(64*48), large(72*52);
    std::vector<float> depths(64*48,.5f), floats(64*48);
    std::vector<_Float16> colors(64*48*4), motions(64*48*2,0);
    for(unsigned i=0;i<masks.size();++i) {
        masks[i]=(i*13)%256; reactions[i]=(i*7)%256; floats[i]=float(i%19)/8.f-.5f;
    }
    for(unsigned i=0;i<large.size();++i) large[i]=(i*11)%256;
    [depth replaceRegion:MTLRegionMake2D(0,0,64,48) mipmapLevel:0 withBytes:depths.data() bytesPerRow:64*4];
    [motion replaceRegion:MTLRegionMake2D(0,0,64,48) mipmapLevel:0 withBytes:motions.data() bytesPerRow:64*4];
    [mask replaceRegion:MTLRegionMake2D(0,0,64,48) mipmapLevel:0 withBytes:masks.data() bytesPerRow:64];
    [zero replaceRegion:MTLRegionMake2D(0,0,64,48) mipmapLevel:0 withBytes:zeros.data() bytesPerRow:64];
    [reactive replaceRegion:MTLRegionMake2D(0,0,64,48) mipmapLevel:0 withBytes:reactions.data() bytesPerRow:64];
    [floating replaceRegion:MTLRegionMake2D(0,0,64,48) mipmapLevel:0 withBytes:floats.data() bytesPerRow:64*4];
    [oversized replaceRegion:MTLRegionMake2D(0,0,72,52) mipmapLevel:0 withBytes:large.data() bytesPerRow:72];
    FrameOperations ops{}; ops.combineCompositionMask=true;
    for(unsigned mode=0;mode<7;++mode) {
        for(unsigned i=0;i<64*48;++i) {
            for(unsigned k=0;k<3;++k) colors[i*4+k]=float((i*7+k*37+mode*53)%251)/250.f;
            colors[i*4+3]=1;
        }
        [color replaceRegion:MTLRegionMake2D(0,0,64,48) mipmapLevel:0 withBytes:colors.data() bytesPerRow:64*8];
        unsigned w=mode==5?48:64,h=mode==5?36:48;
        FrameInfo f{};
        f.color=color; f.depth=depth; f.motionVectors=motion; f.output=output;
        f.inputContent={w,h}; f.colorRect=f.depthRect=f.motionRect=f.reactiveRect={0,0,w,h};
        f.compositionRect={mode==4?3u:0u,mode==4?2u:0u,w,h};
        f.outputRect={0,0,128,96}; f.resetHistory=mode==0;
        f.jitterOffsetX=float(mode%4)/4.f-.375f; f.jitterOffsetY=-f.jitterOffsetX;
        f.motionVectorScaleX=w; f.motionVectorScaleY=-float(h);
        f.compositionMask=mode==3?floating:mode==4?oversized:mask;
        f.reactiveMask=mode==2?reactive:nullptr;
        Error error;
        auto prepared=feature->prepare(f,{color,depth,motion,output,nullptr,
            f.reactiveMask,f.compositionMask},&error,ops);
        assert(prepared);
        auto lease=execute(device,prepared);
        const bool direct=mode<2 || mode==6;
        assert((lease->impl_->combinedMask==nil)==direct);
        auto actual=pixels<_Float16>(device,output,4);
        // A zero reactive input forces the original max/clamp pass without
        // changing its result. Keep independent, identically advanced history.
        f.output=referenceOutput;
        if (!f.reactiveMask) f.reactiveMask=zero;
        auto expectedFrame=reference->prepare(f,{color,depth,motion,referenceOutput,nullptr,
            f.reactiveMask,f.compositionMask},&error,ops);
        assert(expectedFrame);
        auto expectedLease=execute(device,expectedFrame);
        auto expected=pixels<_Float16>(device,referenceOutput,4);
        for(unsigned i=0;i<actual.size();++i)
            assert(std::isfinite(float(actual[i])) &&
                   std::fabs(float(actual[i])-float(expected[i]))<.003f);
        auto effectiveMask=direct?pixels<unsigned char>(device,mask,1):
            pixels<unsigned char>(device,lease->impl_->combinedMask,1);
        assert(effectiveMask==pixels<unsigned char>(device,expectedLease->impl_->combinedMask,1));
    }
    [referenceOutput release]; [output release]; [oversized release]; [floating release];
    [reactive release]; [zero release]; [mask release]; [motion release]; [depth release]; [color release];
    puts("SR_COMPOSITION_MASK_PASS: direct, both masks, float clamp, offsets and capacity transitions match");
}

static void copiedInputPaddingRegression(id<MTLDevice> device, id<MTL4Compiler> compiler) {
    constexpr unsigned width=1920,height=1080,sourceWidth=width+8,sourceHeight=height+8;
    CreateInfo ci{}; ci.input={width,height}; ci.output={width*2,height*2};
    ci.dynamicOutput=true; ci.featureFlags=FeatureFlagMVLowRes;
    Error error;
    auto feature=Feature::create({device,compiler},ci,&error);
    assert(feature);
    auto color=sharedTexture(device,MTLPixelFormatRGBA16Float,sourceWidth,sourceHeight);
    auto depth=sharedTexture(device,MTLPixelFormatR32Float,sourceWidth,sourceHeight);
    auto motion=sharedTexture(device,MTLPixelFormatRG16Float,sourceWidth,sourceHeight);
    auto reactive=sharedTexture(device,MTLPixelFormatR8Unorm,sourceWidth,sourceHeight);
    auto output=texture(device,MTLPixelFormatRGBA16Float,width*2,height*2,
        MTLTextureUsageShaderRead|MTLTextureUsageShaderWrite);
    std::vector<_Float16> colors(sourceWidth*sourceHeight*4),motions(sourceWidth*sourceHeight*2);
    std::vector<float> depths(sourceWidth*sourceHeight);
    std::vector<unsigned char> masks(sourceWidth*sourceHeight);
    for(unsigned y=0;y<sourceHeight;++y) for(unsigned x=0;x<sourceWidth;++x) {
        auto i=y*sourceWidth+x;
        for(unsigned k=0;k<3;++k) colors[i*4+k]=float((x*13+y*7+k*53)%251)/250.f;
        colors[i*4+3]=1;
        depths[i]=.2f+float((x*13+y*7)%251)/500.f;
        masks[i]=(x*7+y*11)%256;
        for(unsigned k=0;k<2;++k) motions[i*2+k]=float(int((x*11+y*3+k)%31)-15)/1024.f;
    }
    [color replaceRegion:MTLRegionMake2D(0,0,sourceWidth,sourceHeight) mipmapLevel:0
        withBytes:colors.data() bytesPerRow:sourceWidth*8];
    [depth replaceRegion:MTLRegionMake2D(0,0,sourceWidth,sourceHeight) mipmapLevel:0
        withBytes:depths.data() bytesPerRow:sourceWidth*4];
    [motion replaceRegion:MTLRegionMake2D(0,0,sourceWidth,sourceHeight) mipmapLevel:0
        withBytes:motions.data() bytesPerRow:sourceWidth*4];
    [reactive replaceRegion:MTLRegionMake2D(0,0,sourceWidth,sourceHeight) mipmapLevel:0
        withBytes:masks.data() bytesPerRow:sourceWidth];
    // Reuse capacity through repeated full, 90%, and 75% input extents.
    // The 810-row active extent leaves a partial final doubling-copy block.
    const Extent extents[]={{width,height},{width*9/10,height*9/10},
        {width*3/4,height*3/4},{width*9/10,height*9/10},
        {width,height},{width*9/10,height*9/10},
        {width*3/4,height*3/4},{width*9/10,height*9/10},
        {width*3/4,height},{width,height*3/4},
        {width*3/4+1,height*3/4+1},{width,height}};
    for(const auto& extent:extents) {
        unsigned w=extent.width,h=extent.height;
        FrameInfo f{};
        f.color=color; f.depth=depth; f.motionVectors=motion; f.output=output;
        f.inputContent={w,h}; f.colorRect={3,2,w,h}; f.depthRect={3,5,w,h};
        f.motionRect={7,2,w,h}; f.outputRect={0,0,width*2,height*2};
        f.reactiveMask=reactive; f.reactiveRect={5,3,w,h};
        f.motionVectorScaleX=w; f.motionVectorScaleY=-float(h);
        auto prepared=feature->prepare(f,{color,depth,motion,output,nullptr,reactive,nullptr},&error,{});
        assert(prepared);
        auto lease=execute(device,prepared);
        auto actualColor=pixels<_Float16>(device,lease->impl_->stagedColor,4);
        auto actualDepth=pixels<float>(device,lease->impl_->stagedDepth,1);
        auto actualMotion=pixels<_Float16>(device,lease->impl_->stagedMotion,2);
        auto actualMask=pixels<unsigned char>(device,lease->impl_->stagedReactive,1);
        for(unsigned y=0;y<height;++y) for(unsigned x=0;x<width;++x) {
            unsigned sx=std::min(x,w-1),sy=std::min(y,h-1),i=y*width+x;
            for(unsigned k=0;k<4;++k)
                assert(actualColor[i*4+k]==colors[((sy+2)*sourceWidth+sx+3)*4+k]);
            assert(actualDepth[i]==depths[(sy+5)*sourceWidth+sx+3]);
            for(unsigned k=0;k<2;++k)
                assert(actualMotion[i*2+k]==motions[((sy+2)*sourceWidth+sx+7)*2+k]);
            assert(actualMask[i]==masks[(sy+3)*sourceWidth+sx+5]);
        }
    }
    feature.reset();
    [output release]; [reactive release]; [motion release]; [depth release]; [color release];
    puts("SR_COPIED_INPUT_PADDING_PASS: full input capacity survives extent changes, odd sizes and independent edges");
}

static void generatedPaddingRegression(id<MTLDevice> device, id<MTL4Compiler> compiler) {
    auto feature=makeFeature(device,compiler,true);
    auto color=sharedTexture(device,MTLPixelFormatRGBA32Float,88,72);
    auto depth=sharedTexture(device,MTLPixelFormatR32Float,88,72);
    auto motion=sharedTexture(device,MTLPixelFormatRG16Float,88,80);
    auto reactive=sharedTexture(device,MTLPixelFormatR32Float,96,80);
    auto composition=sharedTexture(device,MTLPixelFormatR32Float,96,80);
    auto output=texture(device,MTLPixelFormatRGBA32Float,180,160,
        MTLTextureUsageShaderRead|MTLTextureUsageShaderWrite);
    std::vector<float> colors(88*72*4,1), depths(88*72,.5f), r(96*80), c(96*80);
    std::vector<_Float16> mv(88*80*2,0);
    for(unsigned y=0;y<72;++y) for(unsigned x=0;x<88;++x)
        for(unsigned k=0;k<3;++k) colors[(y*88+x)*4+k]=float((x*3+y*5+k)%239)/255;
    for(unsigned y=0;y<80;++y) for(unsigned x=0;x<96;++x) {
        r[y*96+x]=(int((x*7+y*11)%41)-8)/25.f;
        c[y*96+x]=(int((x*13+y*3)%37)-5)/25.f;
    }
    [color replaceRegion:MTLRegionMake2D(0,0,88,72) mipmapLevel:0 withBytes:colors.data() bytesPerRow:88*16];
    [depth replaceRegion:MTLRegionMake2D(0,0,88,72) mipmapLevel:0 withBytes:depths.data() bytesPerRow:88*4];
    [motion replaceRegion:MTLRegionMake2D(0,0,88,80) mipmapLevel:0 withBytes:mv.data() bytesPerRow:88*4];
    [reactive replaceRegion:MTLRegionMake2D(0,0,96,80) mipmapLevel:0 withBytes:r.data() bytesPerRow:96*4];
    [composition replaceRegion:MTLRegionMake2D(0,0,96,80) mipmapLevel:0 withBytes:c.data() bytesPerRow:96*4];
    std::shared_ptr<const ExecutionLease> retained;
    std::vector<float> retainedPixels;
    for(unsigned mode=0;mode<7;++mode) {
        unsigned w=mode==0||mode==4?80:mode==6?79:64;
        unsigned h=mode==0||mode==5?64:mode==6?63:48;
        auto f=frameInfo(color,depth,motion,output,w,h,160,128,true);
        f.reactiveMask=mode==2?nullptr:reactive;
        f.compositionMask=mode==3?nullptr:composition;
        f.reactiveRect={7,5,w,h}; f.compositionRect={11,9,w,h};
        FrameOperations ops{};
        ops.colorTransfer=mode%2?ColorTransfer::PQ:ColorTransfer::SRGB;
        ops.combineCompositionMask=true;
        Error error;
        auto prepared=feature->prepare(f,{color,depth,motion,output,nullptr,
            f.reactiveMask,f.compositionMask},&error,ops);
        assert(prepared);
        auto lease=execute(device,prepared);
        auto linear=pixels<float>(device,lease->impl_->linearColor,4);
        auto mask=pixels<unsigned char>(device,lease->impl_->combinedMask,1);
        unsigned cw=lease->impl_->linearColor.width,ch=lease->impl_->linearColor.height;
        for(unsigned y=0;y<ch;++y) for(unsigned x=0;x<cw;++x) {
            unsigned sx=std::min(x,w-1),sy=std::min(y,h-1);
            for(unsigned k=0;k<4;++k) {
                float value=linear[(y*cw+x)*4+k];
                assert(value==linear[(sy*cw+sx)*4+k]);
                float encoded=colors[((sy+2)*88+sx+3)*4+k];
                float expected=k==3?encoded:mode%2?inversePq(encoded):inverseSrgb(encoded);
                assert(std::fabs(value-expected)<.0001f);
            }
            float rv=f.reactiveMask?r[(sy+5)*96+sx+7]:0;
            float cv=f.compositionMask?c[(sy+9)*96+sx+11]:0;
            float expected=std::max(std::clamp(rv,0.f,1.f),std::clamp(cv,0.f,1.f));
            assert(std::abs(int(mask[y*cw+x])-int(std::round(expected*255)))<=1);
            assert(mask[y*cw+x]==mask[sy*cw+sx]);
        }
        if (!mode) { retained=lease; retainedPixels=linear; }
        else {
            assert(retained->impl_->linearColor!=lease->impl_->linearColor);
            assert(retained->impl_->fsrArgumentTable!=lease->impl_->fsrArgumentTable);
            assert(pixels<float>(device,retained->impl_->linearColor,4)==retainedPixels);
        }
    }
    [output release]; [composition release]; [reactive release];
    [motion release]; [depth release]; [color release];
    puts("SR_GENERATED_PADDING_PASS");
}

static void finishCropRegression(id<MTLDevice> device) {
    struct alignas(8) Params {
        uint32_t sourceOrigin[2], compositionOrigin[2], destinationOrigin[2], extent[2];
        uint32_t dispatchOrigin[2], dispatchExtent[2], transfer, flags;
        float sharpness, exposure;
    };
    static_assert(sizeof(Params) == 64);
    constexpr unsigned width = 9, height = 7, sourceX = 2, sourceY = 3;
    constexpr unsigned paddedWidth = 17, paddedHeight = 13;
    constexpr unsigned outputWidth = 15, outputHeight = 12, outputX = 3, outputY = 2;
    std::vector<_Float16> active(width * height * 4);
    std::vector<_Float16> padded(paddedWidth * paddedHeight * 4, _Float16(8));
    for (unsigned y = 0; y < height; ++y)
        for (unsigned x = 0; x < width; ++x) {
            const unsigned pixel = (y * width + x) * 4;
            for (unsigned c = 0; c < 3; ++c)
                active[pixel + c] = _Float16(.02f + float((x * 3 + y * 5 + c * 7) % 23) / 24);
            active[pixel + 3] = _Float16(.25f + float((x + y) % 5) / 8);
            std::memcpy(&padded[((y + sourceY) * paddedWidth + x + sourceX) * 4],
                        &active[pixel], 4 * sizeof(_Float16));
        }
    auto crop = sharedTexture(device, MTLPixelFormatRGBA16Float, width, height);
    auto source = sharedTexture(device, MTLPixelFormatRGBA16Float, paddedWidth, paddedHeight);
    auto exposure = sharedTexture(device, MTLPixelFormatR32Float, 1, 1);
    const float exposureValue = 1.25f;
    [crop replaceRegion:MTLRegionMake2D(0,0,width,height) mipmapLevel:0
              withBytes:active.data() bytesPerRow:width * 8];
    [source replaceRegion:MTLRegionMake2D(0,0,paddedWidth,paddedHeight) mipmapLevel:0
                withBytes:padded.data() bytesPerRow:paddedWidth * 8];
    [exposure replaceRegion:MTLRegionMake2D(0,0,1,1) mipmapLevel:0
                  withBytes:&exposureValue bytesPerRow:sizeof(exposureValue)];
    NSError* error = nil;
    dispatch_data_t data = dispatch_data_create(kFsrKernelsMetallib, sizeof(kFsrKernelsMetallib),
                                                nullptr, DISPATCH_DATA_DESTRUCTOR_DEFAULT);
    auto library = [device newLibraryWithData:data error:&error];
    dispatch_release(data);
    assert(library && !error);
    std::array<id<MTLComputePipelineState>, 2> pipelines{};
    for (bool sharpen : {false, true}) {
        auto constants = [MTLFunctionConstantValues new];
        [constants setConstantValue:&sharpen type:MTLDataTypeBool atIndex:0];
        pipelines[sharpen] = makePipeline(device, library, @"yaagl_fsr_finish", constants);
        [constants release];
        assert(pipelines[sharpen]);
    }
    auto queue = [device newCommandQueue];
    for (bool sharpen : {false, true})
    for (unsigned transfer : {0u, 1u, 2u})
        for (float sharpness : {0.0f, .5f, 1.0f}) {
            std::vector<float> results[2];
            for (unsigned centered = 0; centered != 2; ++centered) {
                const unsigned ow = centered ? outputWidth : width;
                const unsigned oh = centered ? outputHeight : height;
                auto output = sharedTexture(device, MTLPixelFormatRGBA32Float, ow, oh);
                Params params{};
                params.sourceOrigin[0] = centered ? sourceX : 0;
                params.sourceOrigin[1] = centered ? sourceY : 0;
                params.destinationOrigin[0] = centered ? outputX : 0;
                params.destinationOrigin[1] = centered ? outputY : 0;
                params.extent[0] = width; params.extent[1] = height;
                params.dispatchExtent[0] = ow; params.dispatchExtent[1] = oh;
                params.transfer = transfer;
                params.flags = (1u << 3) | (centered ? 1u << 4 : 0);
                params.sharpness = sharpness; params.exposure = 1;
                auto constants = [device newBufferWithBytes:&params length:sizeof(params)
                                                   options:MTLResourceStorageModeShared];
                auto command = [queue commandBuffer];
                auto encoder = [command computeCommandEncoder];
                [encoder setComputePipelineState:pipelines[sharpen]];
                [encoder setBuffer:constants offset:0 atIndex:0];
                [encoder setTexture:centered ? source : crop atIndex:0];
                [encoder setTexture:output atIndex:1];
                [encoder setTexture:exposure atIndex:2];
                [encoder dispatchThreads:MTLSizeMake(ow,oh,1)
                     threadsPerThreadgroup:MTLSizeMake(8,8,1)];
                [encoder endEncoding];
                auto done = dispatch_semaphore_create(0);
                [command addCompletedHandler:^(id<MTLCommandBuffer> completed) {
                    assert(completed.status == MTLCommandBufferStatusCompleted);
                    dispatch_semaphore_signal(done);
                }];
                [command commit];
                assert(dispatch_semaphore_wait(done,dispatch_time(DISPATCH_TIME_NOW,30*NSEC_PER_SEC))==0);
                results[centered].resize(ow * oh * 4);
                [output getBytes:results[centered].data() bytesPerRow:ow * 4 * sizeof(float)
                     fromRegion:MTLRegionMake2D(0,0,ow,oh) mipmapLevel:0];
                dispatch_release(done); [constants release]; [output release];
            }
            for (unsigned y = 0; y < outputHeight; ++y)
                for (unsigned x = 0; x < outputWidth; ++x) {
                    auto pixel = &results[1][(y * outputWidth + x) * 4];
                    if (x < outputX || y < outputY || x >= outputX + width || y >= outputY + height) {
                        assert(pixel[0] == 0 && pixel[1] == 0 && pixel[2] == 0 && pixel[3] == 1);
                    } else {
                        const unsigned offset = ((y - outputY) * width + x - outputX) * 4;
                        assert(!std::memcmp(pixel, &results[0][offset], 4 * sizeof(float)));
                        if (!sharpen && transfer == 0)
                            for (unsigned c = 0; c < 3; ++c)
                                assert(pixel[c] == float(active[offset + c]));
                        assert(pixel[3] == float(active[offset + 3]));
                    }
                }
        }
    [queue release];
    for (auto pipeline : pipelines) [pipeline release];
    [library release];
    [exposure release]; [source release]; [crop release];
    puts("SR_FINISH_CROP_PASS: RCAS active edges, centered padding, exposure and alpha preserved");
}

int main() {
    @autoreleasepool {
        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        assert(device);
        auto compilerDescriptor = [MTL4CompilerDescriptor new];
        NSError* error = nil;
        id<MTL4Compiler> compiler = [device newCompilerWithDescriptor:compilerDescriptor
                                                                  error:&error];
        [compilerDescriptor release];
        assert(compiler);
        colorRegression(device,compiler);
        cachedExtentRegression(device,compiler);
        dynamicExtentHistoryRegression(device,compiler);
        generatedPaddingRegression(device,compiler);
        copiedInputPaddingRegression(device,compiler);
        motionRegression(device,compiler,false,true,false,false);
        motionRegression(device,compiler,false,true,true,false);
        motionRegression(device,compiler,false,true,true,true);
        motionRegression(device,compiler,true,true,true,false);
        motionRegression(device,compiler,false,false,true,false);
        tableOwnershipRegression(device,compiler);
        scratchReturnRegression(device,compiler);
        pipelineTransitionRegression(device,compiler);
        compositionMaskRegression(device,compiler);
        finishCropRegression(device);

        CreateContext context{device, compiler};
        CreateInfo creation{};
        creation.input = {64, 64};
        creation.output = {128, 128};
        creation.featureFlags = FeatureFlagMVLowRes;
        creation.dynamicOutput = true;
        Error backendError;
        auto feature = Feature::create(context, creation, &backendError);
        assert(feature && !backendError);

        auto color = texture(device, MTLPixelFormatRGBA16Float, 64, 64,
                             MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite);
        auto depth = texture(device, MTLPixelFormatR32Float, 64, 64,
                             MTLTextureUsageShaderRead);
        auto motion = texture(device, MTLPixelFormatRG16Float, 64, 64,
                              MTLTextureUsageShaderRead);
        auto output = texture(device, MTLPixelFormatRGBA16Float, 128, 128,
                              MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite);
        assert(color && depth && motion && output);

        FrameInfo frame{};
        frame.color = color;
        frame.depth = depth;
        frame.motionVectors = motion;
        frame.output = output;
        frame.inputContent = {64, 64};
        frame.colorRect = {0, 0, 64, 64};
        frame.depthRect = frame.colorRect;
        frame.motionRect = frame.colorRect;
        frame.outputRect = {0, 0, 128, 128};
        frame.motionVectorScaleX = frame.motionVectorScaleY = 1.0f;
        frame.preExposure = 1.0f;
        frame.resetHistory = true;
        TextureSet textures{color, depth, motion, output, nullptr, nullptr, nullptr};
        auto prepared = feature->prepare(frame, textures, &backendError);
        assert(prepared && !backendError);

        id<MTL4CommandQueue> queue = [device newMTL4CommandQueue];
        id<MTL4CommandAllocator> allocator = [device newCommandAllocator];
        id<MTL4CommandBuffer> command = [device newCommandBuffer];
        id<MTLFence> fence = [device newFence];
        assert(queue && allocator && command && fence);
        [command beginCommandBufferWithAllocator:allocator];
        auto initialization = [command computeCommandEncoder];
        [initialization updateFence:fence afterEncoderStages:MTLStageDispatch];
        [initialization endEncoding];
        std::shared_ptr<const ExecutionLease> lease;
        assert(prepared->encode(command, fence, lease, &backendError) && lease);
        [command endCommandBuffer];

        auto options = [MTL4CommitOptions new];
        auto completed = dispatch_semaphore_create(0);
        [options addFeedbackHandler:^(id<MTL4CommitFeedback> feedback) {
            assert(!feedback.error);
            dispatch_semaphore_signal(completed);
        }];
        [queue commit:&command count:1 options:options];
        assert(dispatch_semaphore_wait(completed,
                                       dispatch_time(DISPATCH_TIME_NOW, 30 * NSEC_PER_SEC)) == 0);

        lease.reset();
        prepared.reset();
        feature.reset();
        [options release];
        dispatch_release(completed);
        [fence release];
        [command release];
        [allocator release];
        [queue release];
        [output release];
        [motion release];
        [depth release];
        [color release];
        [compiler release];
        [device release];
    }
    puts("FSR_NATIVE_SR_PASS");
}
"""


def main():
    with tempfile.TemporaryDirectory(prefix="yaagl-fsr-native-") as directory:
        directory = pathlib.Path(directory)
        source = directory / "fsr-native.mm"
        binary = directory / "fsr-native"
        source.write_text(SOURCE)
        subprocess.run(("node", str(NATIVE.parent / "scripts/build-fsr-kernels.mjs"),
                        str(directory)), check=True)
        subprocess.run(
            (
                "xcrun",
                "clang++",
                "-arch",
                "x86_64",
                "-std=c++20",
                "-fno-objc-arc",
                "-Wl,-adhoc_codesign",
                "-I",
                str(NATIVE),
                "-I",
                str(directory),
                str(source),
                "-framework",
                "Foundation",
                "-framework",
                "Metal",
                "-framework",
                "MetalFX",
                "-o",
                str(binary),
            ),
            check=True,
        )
        subprocess.run((str(binary),), check=True)


if __name__ == "__main__":
    main()
