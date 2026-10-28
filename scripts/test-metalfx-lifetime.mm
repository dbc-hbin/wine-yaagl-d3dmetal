// Isolated native fixture. Including the production module makes its exact
// authentication gate available without adding test controls to the sidecar API.
#include "../d3dmetal-sidecar/metalfx-lifetime.mm"

#include <array>
#include <atomic>
#include <cassert>
#include <cmath>
#include <cstdio>
#include <simd/simd.h>
#include <string>
#include <vector>

namespace lifetime = yaagl::pso::metalfx_lifetime;

struct Deaths {
    std::atomic<unsigned> made{0}, died{0};
    dispatch_group_t group = dispatch_group_create();
};
static Deaths effects, residencies;
static char effectKey, residencyKey;

@interface YAAGLMetalFxLifetimeNotice : NSObject {
@public
    Deaths* deaths;
}
@end
@implementation YAAGLMetalFxLifetimeNotice
- (void)dealloc {
    ++deaths->died;
    dispatch_group_leave(deaths->group);
    [super dealloc];
}
@end

static void track(id object, char* key, Deaths& deaths) {
    assert(object);
    if (objc_getAssociatedObject(object, key)) return;
    auto notice = [YAAGLMetalFxLifetimeNotice new];
    notice->deaths = &deaths;
    ++deaths.made;
    dispatch_group_enter(deaths.group);
    objc_setAssociatedObject(object, key, notice, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
    [notice release];
}

static const std::uint8_t* metalfxBase;
static thread_local bool factoryActive;
static std::string scenario;
static bool injected;
static CC_SHA256_CTX pixelsHash;
static id (*nativeFgInit)(id, SEL, id, id, id);
static lifetime::Init nativeBbrInit;
static id (*nativeResidency)(id, SEL, id, NSError**);
static id (*nativeArguments)(id, SEL, id, NSError**);
static id (*nativeLibrary)(id, SEL, id, NSError**);
static void (*nativeConstant)(id, SEL, const void*, MTLDataType, NSUInteger);

static id observeFgInit(id self, SEL selector, id device, id compiler, id descriptor) {
    [self retain];
    track(self, &effectKey, effects);
    @try {
        return nativeFgInit(self, selector, device, compiler, descriptor);
    } @finally {
        [self release];
    }
}

static id observeBbrInit(id self, SEL selector, id device, id descriptor, id compiler, id history) {
    [self retain];
    track(self, &effectKey, effects);
    @try {
        return nativeBbrInit(self, selector, device, descriptor, compiler, history);
    } @finally {
        [self release];
    }
}

static id observeResidency(id self, SEL selector, id descriptor, NSError** error) {
    id result = nativeResidency(self, selector, descriptor, error);
    if (factoryActive && result) track(result, &residencyKey, residencies);
    return result;
}

static id failArguments(id self, SEL selector, id descriptor, NSError** error) {
    // MFXDevice4 C2's verified LSDA covers this call before effect+0x40 assignment.
    if (factoryActive && scenario == "fail-owner" &&
        __builtin_return_address(0) == metalfxBase + 0x6597f) {
        injected = true;
        @throw [NSException exceptionWithName:@"YAAGLMetalFxLifetimeFixture"
            reason:@"native owner construction boundary" userInfo:nil];
    }
    return nativeArguments(self, selector, descriptor, error);
}

static void failConstant(id self, SEL selector, const void* value, MTLDataType type, NSUInteger index) {
    // FrameGenImpl C2 +0xa5c27 is after raw owner assignment. Its authenticated
    // LSDA unwinds to +0xa6c91, then outer init destroys the partial implementation.
    if (factoryActive && scenario == "fail-implementation" &&
        __builtin_return_address(0) == metalfxBase + 0xa5c2d) {
        injected = true;
        @throw [NSException exceptionWithName:@"YAAGLMetalFxLifetimeFixture"
            reason:@"post-owner implementation construction boundary" userInfo:nil];
    }
    nativeConstant(self, selector, value, type, index);
}

static id failLibrary(id self, SEL selector, id url, NSError** error) {
    // BBR4 init +0x5915d is inside the attached-filter nil and exception paths.
    if (factoryActive && __builtin_return_address(0) == metalfxBase + 0x59163 &&
        (scenario == "fail-bbr-nil" || scenario == "fail-bbr-throw")) {
        injected = true;
        if (scenario == "fail-bbr-nil") return nil;
        @throw [NSException exceptionWithName:@"YAAGLMetalFxLifetimeFixture"
            reason:@"attached BBR filter initialization boundary" userInfo:nil];
    }
    return nativeLibrary(self, selector, url, error);
}

static IMP replace(Class cls, const char* selector, IMP replacement) {
    auto method = class_getInstanceMethod(cls, sel_registerName(selector));
    assert(method);
    return method_setImplementation(method, replacement);
}

static void observe(id<MTLDevice> device, const lifetime::Hooks& hooks) {
    nativeFgInit = reinterpret_cast<decltype(nativeFgInit)>(replace(hooks.fgClass,
        "initWithDevice:compiler:descriptor:", reinterpret_cast<IMP>(&observeFgInit)));
    nativeBbrInit = reinterpret_cast<lifetime::Init>(replace(hooks.bbrClass,
        "initWithDevice:descriptor:compiler:history:", reinterpret_cast<IMP>(&observeBbrInit)));
    Class deviceClass = object_getClass(device);
    nativeResidency = reinterpret_cast<decltype(nativeResidency)>(replace(deviceClass,
        "newResidencySetWithDescriptor:error:", reinterpret_cast<IMP>(&observeResidency)));
    nativeArguments = reinterpret_cast<decltype(nativeArguments)>(replace(deviceClass,
        "newArgumentTableWithDescriptor:error:", reinterpret_cast<IMP>(&failArguments)));
    nativeLibrary = reinterpret_cast<decltype(nativeLibrary)>(replace(deviceClass,
        "newLibraryWithURL:error:", reinterpret_cast<IMP>(&failLibrary)));
    auto constants = [MTLFunctionConstantValues new];
    nativeConstant = reinterpret_cast<decltype(nativeConstant)>(replace(object_getClass(constants),
        "setConstantValue:type:atIndex:", reinterpret_cast<IMP>(&failConstant)));
    [constants release];
}

static id makeEffect(id<MTLDevice> device, id<MTL4Compiler> compiler, bool fg, bool synchronous) {
    factoryActive = true;
    @try {
        if (fg) {
            auto descriptor = [MTLFXFrameInterpolatorDescriptor new];
            descriptor.inputWidth = descriptor.outputWidth = 640;
            descriptor.inputHeight = descriptor.outputHeight = 360;
            descriptor.colorTextureFormat = descriptor.outputTextureFormat = MTLPixelFormatRGBA16Float;
            descriptor.depthTextureFormat = MTLPixelFormatR32Float;
            descriptor.motionTextureFormat = MTLPixelFormatRG16Float;
            descriptor.requiresPrevColorTexture = scenario == "external-history";
            @try {
                return [descriptor newFrameInterpolatorWithDevice:device compiler:compiler];
            } @finally {
                [descriptor release];
            }
        }
        auto descriptor = [MTLFXTemporalScalerDescriptor new];
        descriptor.inputWidth = 640; descriptor.inputHeight = 360;
        descriptor.outputWidth = 1280; descriptor.outputHeight = 720;
        descriptor.colorTextureFormat = descriptor.outputTextureFormat = MTLPixelFormatRGBA16Float;
        descriptor.depthTextureFormat = MTLPixelFormatR32Float;
        descriptor.motionTextureFormat = MTLPixelFormatRG16Float;
        descriptor.autoExposureEnabled = YES;
        descriptor.requiresSynchronousInitialization = synchronous;
        @try {
            return [descriptor newTemporalScalerWithDevice:device compiler:compiler];
        } @finally {
            [descriptor release];
        }
    } @finally {
        factoryActive = false;
    }
}

template<class Pixel> static id<MTLTexture> input(id<MTLDevice> device, MTLPixelFormat format,
                                               MTLTextureUsage usage, Pixel value) {
    auto descriptor = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:format
        width:640 height:360 mipmapped:NO];
    descriptor.storageMode = MTLStorageModeShared;
    descriptor.usage = usage | MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
    auto texture = [device newTextureWithDescriptor:descriptor];
    assert(texture);
    std::vector<Pixel> pixels(640 * 360, value);
    [texture replaceRegion:MTLRegionMake2D(0, 0, 640, 360) mipmapLevel:0
        withBytes:pixels.data() bytesPerRow:640 * sizeof(Pixel)];
    return texture;
}

struct Frame {
    id<MTL4CommandBuffer> buffer;
    id<MTL4CommandAllocator> allocator;
    id<MTLResidencySet> residency;
    id<MTLTexture> color, depth, motion, output;
    id<MTLBuffer> readback;
    id<MTLFence> fence;
    id<MTLSharedEvent> startGate;
    MTL4CommitOptions* options;
    dispatch_semaphore_t completed = dispatch_semaphore_create(0);
    NSUInteger width, height, rowBytes;

    Frame(id<MTLDevice> device, id effect, bool fg, id<MTLSharedEvent> gate = nil) {
        startGate = [gate retain];
        width = fg ? 640 : 1280; height = fg ? 360 : 720;
        id<MTLFXTemporalScalerBase> usages = effect;
        color = input(device, MTLPixelFormatRGBA16Float, usages.colorTextureUsage,
            std::array<__fp16, 4>{.25f, .5f, .75f, 1});
        depth = input(device, MTLPixelFormatR32Float, usages.depthTextureUsage, .5f);
        motion = input(device, MTLPixelFormatRG16Float, usages.motionTextureUsage,
            std::array<__fp16, 2>{0, 0});
        auto descriptor = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA16Float
            width:width height:height mipmapped:NO];
        descriptor.storageMode = MTLStorageModePrivate;
        descriptor.usage = usages.outputTextureUsage | MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
        output = [device newTextureWithDescriptor:descriptor];
        const auto alignment = [device minimumTextureBufferAlignmentForPixelFormat:MTLPixelFormatRGBA16Float];
        rowBytes = (width * 8 + alignment - 1) / alignment * alignment;
        readback = [device newBufferWithLength:rowBytes * height options:MTLResourceStorageModeShared];
        assert(output && readback);
        auto setDescriptor = [MTLResidencySetDescriptor new];
        NSError* error = nil;
        residency = [device newResidencySetWithDescriptor:setDescriptor error:&error];
        [setDescriptor release];
        assert(residency && !error);
        id<MTLAllocation> allocations[] = {color, depth, motion, output, readback};
        [residency addAllocations:allocations count:5]; [residency commit];
        allocator = [device newCommandAllocator]; buffer = [device newCommandBuffer];
        fence = [device newFence];
        assert(allocator && buffer && fence);
        [buffer beginCommandBufferWithAllocator:allocator];
        [buffer useResidencySet:residency];
        auto begin = [buffer computeCommandEncoder];
        [begin updateFence:fence afterEncoderStages:MTLStageDispatch]; [begin endEncoding];
        if (fg) {
            id<MTL4FXFrameInterpolator> e = effect;
            e.colorTexture = color; e.prevColorTexture = color;
            e.depthTexture = depth; e.motionTexture = motion; e.outputTexture = output;
            e.fence = fence; e.shouldResetHistory = YES; e.motionVectorScaleX = e.motionVectorScaleY = 1;
            e.deltaTime = .016f; e.aspectRatio = 640.f / 360; e.nearPlane = .25f; e.farPlane = 250;
            e.fieldOfView = 60; e.contentWidth = 640; e.contentHeight = 360;
            e.worldToViewMatrix = matrix_identity_float4x4;
            e.viewToClipMatrix = matrix_identity_float4x4;
            [e encodeToCommandBuffer:buffer];
        } else {
            id<MTL4FXTemporalScaler> e = effect;
            e.colorTexture = color; e.depthTexture = depth; e.motionTexture = motion; e.outputTexture = output;
            e.inputContentWidth = 640; e.inputContentHeight = 360;
            e.fence = fence; e.reset = YES; e.preExposure = 1;
            e.motionVectorScaleX = e.motionVectorScaleY = 1;
            [e encodeToCommandBuffer:buffer];
        }
        auto copy = [buffer computeCommandEncoder];
        [copy waitForFence:fence beforeEncoderStages:MTLStageBlit];
        [copy copyFromTexture:output sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(0, 0, 0)
            sourceSize:MTLSizeMake(width, height, 1) toBuffer:readback destinationOffset:0
            destinationBytesPerRow:rowBytes destinationBytesPerImage:rowBytes * height];
        [copy endEncoding]; [buffer endCommandBuffer];
        options = [MTL4CommitOptions new];
        auto done = completed;
        [options addFeedbackHandler:^(id<MTL4CommitFeedback> feedback) {
            assert(!feedback.error && feedback.GPUEndTime > 0);
            dispatch_semaphore_signal(done);
        }];
    }

    void submit(id<MTL4CommandQueue> queue) {
        if (startGate) [queue waitForEvent:startGate value:1];
        [queue commit:&buffer count:1 options:options];
    }
    void wait() {
        assert(dispatch_semaphore_wait(completed, dispatch_time(DISPATCH_TIME_NOW, 60 * NSEC_PER_SEC)) == 0);
        for (NSUInteger y = 0; y != height; ++y) {
            auto* row = reinterpret_cast<const __fp16*>(static_cast<const char*>(readback.contents) + y * rowBytes);
            CC_SHA256_Update(&pixelsHash, row, static_cast<CC_LONG>(width * 8));
            for (NSUInteger x = 0; x != width; ++x) {
                assert(std::isfinite(float(row[x * 4])) && float(row[x * 4]) >= -.01f && float(row[x * 4]) < 2);
                assert(std::isfinite(float(row[x * 4 + 1])) && std::isfinite(float(row[x * 4 + 2])));
            }
        }
        const auto* center = reinterpret_cast<const __fp16*>(
            static_cast<const char*>(readback.contents) + (height / 2) * rowBytes) + (width / 2) * 4;
        assert(float(center[0]) > .05f && float(center[1]) > .1f && float(center[2]) > .2f);
    }

    ~Frame() {
        [buffer release]; [allocator release]; [options release]; dispatch_release(completed);
        [fence release]; [residency release]; [readback release];
        [startGate release];
        [output release]; [motion release]; [depth release]; [color release];
    }
};

static void waitPrewarm(id effect) {
    void* filter = lifetime::field<void*>(effect, 0x270);
    assert(filter);
    void* network = lifetime::field<void*>(filter, 0x160);
    if (network) {
        auto group = lifetime::field<dispatch_group_t>(network, 0x10);
        if (group) assert(dispatch_group_wait(group, dispatch_time(DISPATCH_TIME_NOW, 60 * NSEC_PER_SEC)) == 0);
    }
}

static void unexpectedDestruct(id) { __builtin_trap(); }

int main(int argc, char** argv) {
    if (argc == 2 && !std::strcmp(argv[1], "reject")) {
        @autoreleasepool {
            lifetime::Hooks hooks;
            assert(lifetime::authenticate(hooks));
            const auto fg = method_getImplementation(hooks.fgDestruct);
            const auto init = method_getImplementation(hooks.bbrInit);
            const auto bbr = method_getImplementation(hooks.bbrDestruct);
            method_setImplementation(hooks.fgDestruct, reinterpret_cast<IMP>(&unexpectedDestruct));
            assert(!lifetime::initialize());
            assert(method_getImplementation(hooks.fgDestruct) == reinterpret_cast<IMP>(&unexpectedDestruct));
            assert(method_getImplementation(hooks.bbrInit) == init);
            assert(method_getImplementation(hooks.bbrDestruct) == bbr);
            method_setImplementation(hooks.fgDestruct, fg);
            std::puts("{\"gateRejected\":true,\"unmodifiedOtherMethods\":true}");
            return 0;
        }
    }
    assert(argc == 4);
    const bool repaired = !std::strcmp(argv[1], "repair");
    assert(repaired || !std::strcmp(argv[1], "baseline"));
    const bool fg = !std::strcmp(argv[2], "fg");
    assert(fg || !std::strcmp(argv[2], "bbr"));
    scenario = argv[3];
    @autoreleasepool {
        auto device = MTLCreateSystemDefaultDevice(); assert(device);
        lifetime::Hooks hooks;
        if (!lifetime::authenticate(hooks)) {
            std::fprintf(stderr, "MetalFX x86_64 exact-image authentication failed\n"); return 3;
        }
        metalfxBase = hooks.image.base;
        if (repaired) assert(lifetime::initialize());
        observe(device, hooks);
        auto compilerDescriptor = [MTL4CompilerDescriptor new];
        NSError* error = nil;
        auto compiler = [device newCompilerWithDescriptor:compilerDescriptor error:&error];
        [compilerDescriptor release]; assert(compiler && !error);
        auto first = [device newMTL4CommandQueue]; auto second = [device newMTL4CommandQueue];
        assert(first && second);
        const bool failing = scenario.starts_with("fail-");
        const bool async = scenario == "async-drop";
        CC_SHA256_Init(&pixelsHash);
        const auto before = device.currentAllocatedSize;
        for (unsigned n = 0; n != 4; ++n) {
            @autoreleasepool {
                id effect = nil;
                bool caught = false;
                injected = false;
                @try {
                    effect = makeEffect(device, compiler, fg, !async);
                } @catch (NSException* exception) {
                    assert([exception.name isEqualToString:@"YAAGLMetalFxLifetimeFixture"]);
                    caught = true;
                }
                if (failing) {
                    assert(injected && !effect);
                    assert(caught == (scenario != "fail-bbr-nil"));
                    continue;
                }
                assert(effect && object_getClass(effect) == (fg ? hooks.fgClass : hooks.bbrClass));
                if (scenario == "factory" || scenario == "async-drop") {
                    if (!fg && !async) waitPrewarm(effect);
                    [effect release];
                } else if (scenario == "prewarm-group") {
                    assert(!fg);
                    waitPrewarm(effect);
                    void* filter = lifetime::field<void*>(effect, 0x270);
                    void* network = lifetime::field<void*>(filter, 0x160);
                    assert(network && lifetime::field<bool>(network, 0x18));
                    auto group = lifetime::field<dispatch_group_t>(network, 0x10);
                    assert(group);
                    // Hold an extra, explicitly owned group entry after native
                    // prewarm completes. This deterministically presents finished
                    // == 1 with group work still outstanding, without racing or
                    // altering a callback's memory. Only actual group completion
                    // permits the repair to reclaim the detached network.
                    dispatch_retain(group);
                    dispatch_group_enter(group);
                    [effect release];
                    assert(residencies.died.load() < residencies.made.load());
                    dispatch_group_leave(group);
                    dispatch_release(group);
                } else if (scenario == "release-before-submit") {
                    assert(!fg);
                    Frame frame(device, effect, false);
                    [effect release];
                    // No public producer has run. The graph's final even event
                    // cannot complete, so native resources must still be owned.
                    assert(residencies.died.load() < residencies.made.load());
                    frame.submit(first); frame.wait();
                } else if (scenario == "multi-queue-replay") {
                    // This is the recorded feature's transferred +1 owner. Replay
                    // means fresh native encoding, as in D3DMetal list replay.
                    id recording = [effect retain]; [effect release];
                    auto gate = [device newSharedEvent];
                    {
                        Frame a(device, recording, fg, gate), b(device, recording, fg);
                        a.submit(first); b.submit(second);
                        assert(effects.died.load() < effects.made.load());
                        gate.signaledValue = 1;
                        a.wait(); b.wait();
                    }
                    [gate release];
                    {
                        Frame replay(device, recording, fg);
                        replay.submit(second); replay.wait();
                    }
                    [recording release];
                } else {
                    assert(scenario == "encode" || scenario == "external-history");
                    { Frame frame(device, effect, fg); frame.submit(first); frame.wait(); }
                    [effect release];
                }
            }
        }
        [compiler release];
        const bool balancedControl = scenario == "fail-owner";
        assert(effects.made.load() > 0 && residencies.made.load() > 0);
        if (repaired || balancedControl) {
            assert(dispatch_group_wait(effects.group, dispatch_time(DISPATCH_TIME_NOW, 60 * NSEC_PER_SEC)) == 0);
            assert(dispatch_group_wait(residencies.group, dispatch_time(DISPATCH_TIME_NOW, 60 * NSEC_PER_SEC)) == 0);
            assert(effects.died.load() == effects.made.load());
            assert(residencies.died.load() == residencies.made.load());
        } else {
            if (fg) assert(effects.died.load() == effects.made.load());
            else assert(effects.died.load() < effects.made.load());
            assert(residencies.died.load() < residencies.made.load());
        }
        unsigned char digest[CC_SHA256_DIGEST_LENGTH];
        CC_SHA256_Final(digest, &pixelsHash);
        char hash[CC_SHA256_DIGEST_LENGTH * 2 + 1];
        for (unsigned n = 0; n != sizeof(digest); ++n)
            std::snprintf(hash + n * 2, 3, "%02x", digest[n]);
        std::printf("{\"mode\":\"%s\",\"effect\":\"%s\",\"scenario\":\"%s\","
            "\"effectsCreated\":%u,\"effectsDestroyed\":%u,\"residenciesCreated\":%u,"
            "\"residenciesDestroyed\":%u,\"allocatedBefore\":%llu,\"allocatedAfter\":%llu,"
            "\"outputSha256\":\"%s\"}\n",
            argv[1], argv[2], argv[3], effects.made.load(), effects.died.load(),
            residencies.made.load(), residencies.died.load(),
            static_cast<unsigned long long>(before), static_cast<unsigned long long>(device.currentAllocatedSize), hash);
        [second release]; [first release]; [device release];
    }
}
