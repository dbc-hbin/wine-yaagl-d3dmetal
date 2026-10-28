#include "fsr-framegeneration.hpp"
#include "d3dmetal-transport.hpp"
#include "async-pipeline-state.hpp"
#include "../include/yaagl_fsr_fg_bridge.h"
#include "third-party/fidelityfx/Kits/FidelityFX/api/include/ffx_api_types.h"
#include "fsr-kernels.inc"

#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <MetalFX/MetalFX.h>
#import <objc/message.h>

#include <array>
#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <memory>
#include <mutex>
#include <new>
#include <unordered_map>
#include <utility>
#include <vector>

namespace yaagl::pso::fsr::framegeneration {
namespace {

namespace transport = d3dmetal;

constexpr std::uint32_t Ok = 0;
constexpr std::uint32_t Runtime = 3;
constexpr std::uint32_t Unsupported = 4;
constexpr std::uint32_t Memory = 5;
constexpr std::uint32_t Parameter = 6;

std::atomic<bool> ready{false};
std::atomic<std::uint64_t> nextContext{1};
std::atomic<unsigned> errorCount{0};
std::mutex registryMutex;

void logFailure(std::uint32_t operation, const char* stage, std::uint32_t result,
                std::uint32_t flags = 0, std::uint32_t width = 0,
                std::uint32_t height = 0, float cameraNear = 0,
                float cameraFar = 0, float fov = 0, float scale = 0) noexcept {
    if (errorCount.fetch_add(1, std::memory_order_relaxed) >= 120) return;
    std::fprintf(stderr,
        "yaagl-fsr-framegeneration: operation=%u stage=%s result=%u flags=0x%x "
        "input=%ux%u camera_near=%.9g camera_far=%.9g fov=%.9g scale=%.9g\n",
        operation, stage, result, flags, width, height, cameraNear, cameraFar, fov, scale);
}

template<class R, class... A>
R send(id object, SEL selector, A... arguments) {
    return reinterpret_cast<R (*)(id, SEL, A...)>(objc_msgSend)(
        object, selector, arguments...);
}

/*
 * Metal HUD adds its "Frame Interpolator: Enabled" row on the first MetalFX
 * interpolation encode and never removes it, although it has a removal method.
 * Call that method when this process stops interpolating so the row reflects
 * the current state; the next encode adds it back. The class exists only while
 * MTL_HUD_ENABLED loads libMTLHud, so this is a no-op without the HUD.
 */
void hideHudFrameInterpolator() noexcept {
    @try {
        Class service = NSClassFromString(@"MTLHUDService");
        SEL instance = NSSelectorFromString(@"instance");
        SEL disable = NSSelectorFromString(@"metalFXFrameInterpolatorDisable");
        if (!service || ![service respondsToSelector:instance]) return;
        id hud = send<id>((id)service, instance);
        if ([hud respondsToSelector:disable]) send<void>(hud, disable);
    } @catch (NSException*) {
    }
}

/*
 * The row is process-wide. `interpolating` counts contexts that have
 * interpolated since their last OFF; only a native FSR fallback never counts.
 * MetalFX reports each encode to the HUD asynchronously, when the frame reaches
 * its layer (measured at most one frame, 19 ms, later). A removal therefore
 * waits 500 ms and runs only if nothing counts and no encode outside a counted
 * context happened after it was scheduled; such an encode schedules its own.
 */
struct HudRow {
    std::mutex mutex;
    unsigned interpolating = 0;
    std::uint64_t activity = 0;
} hudRow;

// Call with hudRow.mutex held.
void scheduleHudRowRemoval() noexcept {
    const std::uint64_t activity = ++hudRow.activity;
    if (!NSClassFromString(@"MTLHUDService")) return;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 500 * NSEC_PER_MSEC),
                   dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
        @autoreleasepool {
            std::lock_guard lock(hudRow.mutex);
            if (!hudRow.interpolating && hudRow.activity == activity)
                hideHudFrameInterpolator();
        }
    });
}

// Call with the context's executionMutex held; `counted` is State::hudCounted.
void releaseHudRow(bool& counted) noexcept {
    std::lock_guard lock(hudRow.mutex);
    if (counted) {
        counted = false;
        if (hudRow.interpolating) --hudRow.interpolating;
    }
    if (!hudRow.interpolating) {
        // Remove a stale row immediately when OFF/DESTROY is explicit. A
        // queued MetalFX encode can re-add it, so keep the delayed retry too.
        hideHudFrameInterpolator();
        scheduleHudRowRemoval();
    }
}

class Object {
public:
    Object() noexcept = default;
    explicit Object(id value) noexcept : value_([value retain]) {}
    Object(const Object& other) noexcept : value_([other.value_ retain]) {}
    Object(Object&& other) noexcept : value_(other.value_) { other.value_ = nil; }
    ~Object() { [value_ release]; }
    Object& operator=(Object other) noexcept {
        std::swap(value_, other.value_);
        return *this;
    }
    id get() const noexcept { return value_; }
    explicit operator bool() const noexcept { return value_ != nil; }
private:
    id value_ = nil;
};

struct Guid {
    std::uint32_t a;
    std::uint16_t b, c;
    std::uint8_t d[8];
};
constexpr Guid deviceGuid{0x189819f1, 0x1db6, 0x4b57,
                         {0xbe, 0x54, 0x18, 0x21, 0x33, 0x9b, 0x85, 0xf7}};
struct Luid { std::uint32_t low; std::int32_t high; };

void* pointer(std::uint64_t value) noexcept {
    return reinterpret_cast<void*>(static_cast<std::uintptr_t>(value));
}

void releaseCom(void* object) noexcept {
    if (!object) return;
    using Function = std::uint32_t (__attribute__((ms_abi)) *)(void*);
    reinterpret_cast<Function>((*static_cast<void***>(object))[2])(object);
}

std::unique_ptr<void, decltype(&releaseCom)> owner(void* object, bool child) {
    if (!object) return {nullptr, releaseCom};
    using Function = std::int32_t (__attribute__((ms_abi)) *)(
        void*, const Guid*, void**);
    void* result = nullptr;
    auto table = *static_cast<void***>(object);
    if (reinterpret_cast<Function>(table[child ? 7 : 0])(
            object, &deviceGuid, &result) < 0 || !result)
        return {nullptr, releaseCom};
    return {result, releaseCom};
}

bool getLuid(void* device, Luid& result) noexcept {
    if (!device) return false;
    using Function = Luid* (__attribute__((ms_abi)) *)(void*, Luid*);
    return reinterpret_cast<Function>((*static_cast<void***>(device))[43])(
               device, &result) == &result;
}

bool matches(void* object, bool child, const Luid& expected) {
    auto device = owner(object, child);
    Luid value{};
    return device && getLuid(device.get(), value) &&
           value.low == expected.low && value.high == expected.high;
}

struct Command {
    transport::NativeCommandList value{};
    ~Command() { transport::releaseCommandList(value); }
    bool open(void* list) {
        return transport::unwrapCommandList(list, value) &&
               value.device && value.kind == transport::CommandListKind::mpl;
    }
};

struct Resources {
    std::array<transport::MetalResource, 8> values{};
    ~Resources() {
        for (auto& value : values) transport::releaseResource(value);
    }
};

MTLPixelFormat colorFormat(std::uint32_t format) noexcept {
    switch (format) {
    case FFX_API_SURFACE_FORMAT_R32G32B32A32_FLOAT: return MTLPixelFormatRGBA32Float;
    case FFX_API_SURFACE_FORMAT_R16G16B16A16_FLOAT: return MTLPixelFormatRGBA16Float;
    case FFX_API_SURFACE_FORMAT_R10G10B10A2_UNORM: return MTLPixelFormatRGB10A2Unorm;
    case FFX_API_SURFACE_FORMAT_R8G8B8A8_UNORM: return MTLPixelFormatRGBA8Unorm;
    case FFX_API_SURFACE_FORMAT_R8G8B8A8_SRGB: return MTLPixelFormatRGBA8Unorm_sRGB;
    case FFX_API_SURFACE_FORMAT_B8G8R8A8_UNORM: return MTLPixelFormatBGRA8Unorm;
    case FFX_API_SURFACE_FORMAT_B8G8R8A8_SRGB: return MTLPixelFormatBGRA8Unorm_sRGB;
    default: return MTLPixelFormatInvalid;
    }
}

bool textureValid(id<MTLTexture> texture, id<MTLDevice> device,
                  NSUInteger width, NSUInteger height) {
    return texture && texture.device == device &&
           texture.textureType == MTLTextureType2D &&
           texture.sampleCount == 1 && texture.width >= width &&
           texture.height >= height &&
           texture.storageMode != MTLStorageModeMemoryless;
}

Object privateTexture(id<MTLDevice> device, MTLPixelFormat format,
                      NSUInteger width, NSUInteger height, MTLTextureUsage usage) {
    MTLTextureDescriptor* descriptor =
        [[MTLTextureDescriptor alloc] init];
    id<MTLTexture> texture = nil;
    @try {
        descriptor.textureType = MTLTextureType2D;
        descriptor.pixelFormat = format;
        descriptor.width = width;
        descriptor.height = height;
        descriptor.depth = 1;
        descriptor.mipmapLevelCount = 1;
        descriptor.arrayLength = 1;
        descriptor.sampleCount = 1;
        descriptor.storageMode = MTLStorageModePrivate;
        descriptor.usage = usage;
        texture = [device newTextureWithDescriptor:descriptor];
        return Object(texture);
    } @finally {
        [texture release];
        [descriptor release];
    }
}

// MetalFX retains the per-frame resources assigned to the cached interpolator.
// The execution lease owns them for GPU lifetime, so drop the interpolator's
// references on every exit from encode, including failures and exceptions.
class API_AVAILABLE(macos(26.0)) InterpolatorBindings final {
public:
    explicit InterpolatorBindings(id<MTLFXFrameInterpolatorBase> effect) noexcept
        : effect_(effect) {}
    InterpolatorBindings(const InterpolatorBindings&) = delete;
    InterpolatorBindings& operator=(const InterpolatorBindings&) = delete;
    ~InterpolatorBindings() {
        @try {
            effect_.colorTexture = nil;
            effect_.prevColorTexture = nil;
            effect_.depthTexture = nil;
            effect_.motionTexture = nil;
            effect_.uiTexture = nil;
            if (@available(macOS 27.0, *)) effect_.distortionTexture = nil;
            effect_.outputTexture = nil;
            effect_.fence = nil;
        } @catch (id) {
        }
    }
private:
    id<MTLFXFrameInterpolatorBase> effect_;
};

Object makePipeline(id<MTLDevice> device, id<MTLLibrary> library, NSString* name) {
    NSError* error = nil;
    id<MTLFunction> function = [library newFunctionWithName:name];
    if (!function) return {};
    id<MTLComputePipelineState> pipeline = nil;
    @try {
        pipeline = [device newComputePipelineStateWithFunction:function error:&error];
        return Object(pipeline);
    } @finally {
        [pipeline release];
        [function release];
    }
}

struct Kernels {
    Object library;
    Object decode;
    Object decodeSceneUi;
    Object depth;
    Object motion;
    Object depthMotion;
    Object encode;
    Object distortion;
};

struct Configuration {
    Object device;
    Object compiler;
    Object descriptor;
    Object factory;
    MTLPixelFormat color = MTLPixelFormatInvalid;
    MTLPixelFormat ui = MTLPixelFormatInvalid;
    MTLPixelFormat sourceColor = MTLPixelFormatInvalid;
    MTLPixelFormat sourceUi = MTLPixelFormatInvalid;
    std::uint32_t transfer = 0;
    bool cameraPresent = false;
    MTLPixelFormat depth = MTLPixelFormatInvalid;
    MTLPixelFormat motion = MTLPixelFormatRG16Float;
    NSUInteger width = 0, height = 0, outputWidth = 0, outputHeight = 0;
    bool distortionEnabled = false;
    Object decodePipeline;
    Object decodeSceneUiPipeline;
    Object depthPipeline;
    Object motionPipeline;
    Object depthMotionPipeline;
    Object encodePipeline;
    Object distortionPipeline;

    NSUInteger threadgroupWidth(const yaagl_fsr_fg_prepare_packet& parameters) const {
        // Only this complete render/output profile has measured Metal4 evidence.
        return parameters.render_width == 1920 && parameters.render_height == 1080 &&
            outputWidth == 2560 && outputHeight == 1440 ? 16 : 8;
    }

    Object makeInterpolator() const {
        if (@available(macOS 26.0, *)) {
            auto d = (MTLFXFrameInterpolatorDescriptor*)descriptor.get();
            id instance = [d newFrameInterpolatorWithDevice:(id<MTLDevice>)device.get()
                                                  compiler:(id<MTL4Compiler>)compiler.get()];
            Object result(instance);
            [instance release];
            return result;
        }
        return {};
    }
};

// Slots contain only retired allocations, never owners. Owners keep the slot
// alive without keeping State alive; retirement never takes a State lock.
struct TextureSlot {
    std::mutex mutex;
    std::uint64_t epoch = 0;
    Object idle;
    void clear() {
        std::lock_guard lock(mutex);
        ++epoch;
        idle = {};
    }
};

struct TextureOwner {
    Object texture;
    std::shared_ptr<TextureSlot> slot;
    std::uint64_t epoch = 0;
    ~TextureOwner() {
        if (!slot || !texture) return;
        std::lock_guard lock(slot->mutex);
        if (epoch == slot->epoch && !slot->idle)
            slot->idle = std::move(texture);
    }
};

std::shared_ptr<TextureOwner> acquireTexture(
    const std::shared_ptr<TextureSlot>& slot, bool recycle,
    id<MTLDevice> device, MTLPixelFormat format, NSUInteger width,
    NSUInteger height, MTLTextureUsage usage) {
    auto result = std::make_shared<TextureOwner>();
    if (recycle) {
        result->slot = slot;
        std::lock_guard lock(slot->mutex);
        result->epoch = slot->epoch;
        auto cached = (id<MTLTexture>)slot->idle.get();
        if (cached && cached.device == device && cached.pixelFormat == format &&
            cached.width == width && cached.height == height && cached.usage == usage)
            result->texture = std::move(slot->idle);
        slot->idle = {};
    }
    if (!result->texture)
        result->texture = privateTexture(device, format, width, height, usage);
    return result;
}

struct Snapshot {
    yaagl_fsr_fg_prepare_packet parameters{};
    // The shared Snapshot is retained by both recorded commands and every
    // execution lease, including a partially encoded producer or consumer.
    std::shared_ptr<TextureOwner> depth, motion;
    bool produced = false;
    NSUInteger depthWidth = 0;
    NSUInteger depthHeight = 0;
    NSUInteger motionWidth = 0;
    NSUInteger motionHeight = 0;
    bool hasPreviousJitter = false;
    float previousJitterUvX = 0;
    float previousJitterUvY = 0;
};

// Scratch never contains input snapshots or current/previous scene history.
struct Scratch {
    std::shared_ptr<Configuration> configuration;
    bool inferUi = false;
    Object ui, depth, motion, output, distortion, parameters, argumentTable, residency;
};

struct ScratchSlot {
    std::mutex mutex;
    std::uint64_t epoch = 0;
    std::unique_ptr<Scratch> idle;
    void clear() {
        std::lock_guard lock(mutex);
        ++epoch;
        idle.reset();
    }
};

struct State {
    std::shared_ptr<ScratchSlot> scratch = std::make_shared<ScratchSlot>();
    std::shared_ptr<TextureSlot> snapshotDepth = std::make_shared<TextureSlot>();
    std::shared_ptr<TextureSlot> snapshotMotion = std::make_shared<TextureSlot>();
    std::shared_ptr<TextureSlot> scene = std::make_shared<TextureSlot>();
    std::vector<std::shared_ptr<Configuration>> configurations;
    Kernels kernels;
    std::mutex mutex;
    std::mutex executionMutex;
    std::shared_ptr<void> device;
    Luid luid{};
    yaagl_fsr_fg_create_packet creation{};
    bool retired = false;
    // Pending provider selection records its first Prepare before PE replays Configure.
    bool generationEnabled = true;
    // Protected by executionMutex while encoding; configuration also holds mutex.
    std::uint64_t generationEpoch = 0;
    // Protected by executionMutex; set while this context holds a HudRow count.
    bool hudCounted = false;
    // Set only after the first eligible Prepare has selected the MetalFX provider.
    bool providerSelected = false;
    Object metalDevice;
    // Dense pending metadata only; recorded commands retain their own snapshots.
    // Full IDs avoid modulo collisions, and swap removal never moves a Snapshot.
    struct PendingFrame {
        std::uint64_t id = 0;
        std::shared_ptr<Snapshot> snapshot;
    };
    std::array<PendingFrame, YAAGL_FSR_FG_MAX_PENDING_FRAMES> frames{};
    std::size_t frameCount = 0;

    std::shared_ptr<Snapshot>* findFrame(std::uint64_t id) {
        for (std::size_t i = 0; i < frameCount; ++i)
            if (frames[i].id == id) return &frames[i].snapshot;
        return nullptr;
    }
    void eraseFrame(std::uint64_t id) {
        for (std::size_t i = 0; i < frameCount; ++i) {
            if (frames[i].id != id) continue;
            --frameCount;
            if (i != frameCount) frames[i] = std::move(frames[frameCount]);
            frames[frameCount].snapshot.reset();
            return;
        }
    }
    void clearFrames() {
        while (frameCount) frames[--frameCount].snapshot.reset();
    }
    std::shared_ptr<TextureOwner> history;
    std::shared_ptr<Configuration> historyConfiguration;
    yaagl_fsr_fg_dispatch_packet historyDispatch{};
    std::uint64_t historyFrame = 0;
    bool hasPreparedJitter = false;
    std::uint64_t preparedFrame = 0;
    float preparedJitterUvX = 0;
    float preparedJitterUvY = 0;

    void capturePreparedJitter(Snapshot& snapshot, std::uint64_t frame) const {
        const auto& parameters = snapshot.parameters;
        snapshot.hasPreviousJitter = hasPreparedJitter &&
            preparedFrame + 1 == frame && !parameters.reset;
        snapshot.previousJitterUvX = preparedJitterUvX;
        snapshot.previousJitterUvY = preparedJitterUvY;
    }

    void commitPreparedJitter(const Snapshot& snapshot, std::uint64_t frame) {
        const auto& parameters = snapshot.parameters;
        hasPreparedJitter = true;
        preparedFrame = frame;
        // Preserve each frame's render domain when output history survives a resize.
        preparedJitterUvX = parameters.jitter_x / float(parameters.render_width);
        preparedJitterUvY = parameters.jitter_y / float(parameters.render_height);
    }
};

std::unordered_map<std::uint64_t, std::shared_ptr<State>> contexts;

std::shared_ptr<State> lookup(std::uint64_t context) {
    std::lock_guard lock(registryMutex);
    auto found = contexts.find(context);
    return found == contexts.end() ? nullptr : found->second;
}

bool supported(id<MTLDevice> device, void* compiler) {
    if (@available(macOS 26.0, *)) {
        return device && compiler &&
            [MTLFXFrameInterpolatorDescriptor supportsMetal4FX:device];
    }
    return false;
}

std::shared_ptr<Configuration> configure(
    State& state, const Command& command,
    const yaagl_fsr_fg_prepare_packet& packet,
    id<MTLTexture> depth, MTLPixelFormat sourceColor, MTLPixelFormat sourceUi,
    NSUInteger outputWidth, NSUInteger outputHeight, std::uint32_t transfer,
    bool distortionEnabled) {
    if (@available(macOS 26.0, *)) {
        auto device = (id<MTLDevice>)command.value.device;
        // api() already validated Metal4 support for this same retained command.
        const MTLPixelFormat color = MTLPixelFormatRGBA16Float;
        const MTLPixelFormat ui = sourceUi == MTLPixelFormatInvalid
                                ? MTLPixelFormatInvalid : MTLPixelFormatRGBA16Float;
        const MTLPixelFormat depthFormat = depth.pixelFormat;
        const MTLPixelFormat motion = MTLPixelFormatRG16Float;
        const bool cameraPresent = packet.camera_info_present != 0;
        // MetalFX consumes color, depth, and motion in one origin-zero active domain.
        for (const auto& cached : state.configurations) {
            if (cached->device.get() == device &&
                cached->color == color && cached->ui == ui &&
                cached->sourceColor == sourceColor && cached->sourceUi == sourceUi &&
                cached->transfer == transfer && cached->cameraPresent == cameraPresent &&
                cached->depth == depthFormat && cached->motion == motion &&
                cached->width == outputWidth && cached->height == outputHeight &&
                cached->outputWidth == outputWidth && cached->outputHeight == outputHeight &&
                cached->distortionEnabled == distortionEnabled)
                return cached;
        }

        auto result = std::make_shared<Configuration>();
        result->device = Object(device);
        result->compiler = Object((id)command.value.compiler);
        result->color = color;
        result->ui = ui;
        result->sourceColor = sourceColor;
        result->sourceUi = sourceUi;
        result->transfer = transfer;
        result->cameraPresent = cameraPresent;
        result->depth = depthFormat;
        result->motion = motion;
        result->width = outputWidth;
        result->height = outputHeight;
        result->outputWidth = outputWidth;
        result->outputHeight = outputHeight;
        result->distortionEnabled = distortionEnabled;

        MTLFXFrameInterpolatorDescriptor* descriptor =
            [[MTLFXFrameInterpolatorDescriptor alloc] init];
        result->descriptor = Object(descriptor);
        [descriptor release];
        descriptor.colorTextureFormat = result->color;
        descriptor.outputTextureFormat = result->color;
        descriptor.depthTextureFormat = result->depth;
        descriptor.motionTextureFormat = result->motion;
        descriptor.uiTextureFormat = result->ui;
        if (@available(macOS 27.0, *))
            descriptor.distortionTextureEnabled = distortionEnabled;
        descriptor.inputWidth = result->width;
        descriptor.inputHeight = result->height;
        descriptor.outputWidth = result->outputWidth;
        descriptor.outputHeight = result->outputHeight;
        if (@available(macOS 27.0, *)) {
            descriptor.requiresPrevColorTexture = NO;
            descriptor.distortionTextureEnabled = distortionEnabled;
        } else if (distortionEnabled) {
            return {};
        }

        result->factory = result->makeInterpolator();
        if (!result->factory) return {};
        auto& kernels = state.kernels;
        if (kernels.library && ((id<MTLLibrary>)kernels.library.get()).device != device)
            kernels = {};
        if (!kernels.library) {
            NSError* libraryError = nil;
            dispatch_data_t data = dispatch_data_create(kFsrKernelsMetallib, sizeof(kFsrKernelsMetallib),
                                                        nullptr, DISPATCH_DATA_DESTRUCTOR_DEFAULT);
            id<MTLLibrary> library = nil;
            @try {
                library = [device newLibraryWithData:data error:&libraryError];
            } @finally {
                dispatch_release(data);
            }
            kernels.library = Object(library);
            [library release];
            if (!kernels.library) return {};
        }
        auto library = (id<MTLLibrary>)kernels.library.get();
        if (!kernels.decode)
            kernels.decode = makePipeline(device, library, @"yaagl_fg_decode_crop");
        if (ui != MTLPixelFormatInvalid && !kernels.decodeSceneUi)
            kernels.decodeSceneUi = makePipeline(device, library, @"yaagl_fg_decode_scene_ui");
        if (depthFormat == MTLPixelFormatR32Float) {
            if (!kernels.depthMotion)
                kernels.depthMotion = makePipeline(device, library, @"yaagl_fg_normalize_depth_motion_r32");
            result->depthMotionPipeline = kernels.depthMotion;
        } else {
            if (!kernels.depth)
                kernels.depth = makePipeline(device, library, @"yaagl_fg_resample_depth");
            if (!kernels.motion)
                kernels.motion = makePipeline(device, library, @"yaagl_fg_normalize_motion");
            result->depthPipeline = kernels.depth;
            result->motionPipeline = kernels.motion;
        }
        if (!kernels.encode)
            kernels.encode = makePipeline(device, library, @"yaagl_fg_encode_scatter");
        if (distortionEnabled && !kernels.distortion)
            kernels.distortion = makePipeline(device, library, @"yaagl_fg_decode_distortion");
        result->decodePipeline = kernels.decode;
        result->decodeSceneUiPipeline = kernels.decodeSceneUi;
        result->encodePipeline = kernels.encode;
        if (distortionEnabled) result->distortionPipeline = kernels.distortion;
        if (!result->decodePipeline ||
            (depthFormat == MTLPixelFormatR32Float ? !result->depthMotionPipeline
                : (!result->depthPipeline || !result->motionPipeline)) ||
            (ui != MTLPixelFormatInvalid && !result->decodeSceneUiPipeline) ||
            !result->encodePipeline || (distortionEnabled && !result->distortionPipeline))
            return {};
        constexpr std::size_t configurationCacheLimit = 8;
        if (state.configurations.size() == configurationCacheLimit)
            state.configurations.erase(state.configurations.begin());
        state.configurations.push_back(result);
        return result;
    }
    return {};
}

bool mapOne(Resources& resources, unsigned index, const State& state,
            const Command& command, std::uint64_t address, bool output,
            NSUInteger width, NSUInteger height) {
    if (!address || index >= resources.values.size()) return false;
    void* resource = pointer(address);
    if (!matches(resource, true, state.luid)) return false;
    const auto access = output ? transport::ResourceAccess::write : transport::ResourceAccess::read;
    if (transport::mapResource(resource, resources.values[index], access) !=
        transport::ResourceMapResult::mapped)
        return false;
    const auto& view = resources.values[index].view;
    return view.mipCount == 1 && view.sliceCount == 1 && view.planes == 1 &&
           textureValid((id<MTLTexture>)resources.values[index].texture,
                        (id<MTLDevice>)command.value.device, width, height);
}

bool mapBuffer(Resources& resources, unsigned index, const State& state,
               const Command& command, std::uint64_t address,
               std::uint64_t offset) {
    if (!address || index >= resources.values.size()) return false;
    void* resource = pointer(address);
    if (!matches(resource, true, state.luid)) return false;
    if (transport::mapResource(resource, resources.values[index],
                               transport::ResourceAccess::write) !=
        transport::ResourceMapResult::mapped)
        return false;
    const auto& mapped = resources.values[index];
    auto buffer = (id<MTLBuffer>)mapped.buffer;
    if (!buffer || buffer.device != (id<MTLDevice>)command.value.device ||
        offset > SIZE_MAX - mapped.bufferOffset)
        return false;
    const std::size_t effective = mapped.bufferOffset + offset;
    return effective <= buffer.length &&
           sizeof(std::uint32_t) <= buffer.length - effective;
}

id<MTLTexture> rootTexture(id<MTLTexture> texture) {
    while (auto parent = texture.parentTexture) texture = parent;
    return texture;
}

/* D3D12_RESOURCE_BARRIER, including the eight-byte aligned transition union. */
struct Barrier {
    std::uint32_t type = 0;
    std::uint32_t flags = 0;
    void* resource = nullptr;
    std::uint32_t subresource = 0xffffffffu;
    std::uint32_t before = 0;
    std::uint32_t after = 0;
    std::uint32_t padding = 0;
};
static_assert(sizeof(Barrier) == 32);

bool record(void* list, Command& command, const Resources& resources,
            const std::shared_ptr<const PreparedFrame>& prepared,
            const std::array<std::uint64_t, 8>& addresses,
            const std::array<std::uint32_t, 8>& states, unsigned resourceCount,
            bool generate) {
    std::array<transport::ResourceUse, 8> uses{};
    std::array<Barrier, 8> barriers{};
    unsigned count = 0, useCount = 0;
    for (unsigned i = 0; i != resourceCount; ++i) {
        if (!addresses[i]) continue;
        const bool output = generate && (i == 1 || i == 3 || i == 7);
        unsigned first = 0;
        while (first != i && addresses[first] != addresses[i]) ++first;
        if (first != i) {
            // One resource has one initial state and one access role. Validate
            // every alias before any command-list mutation, then record it once.
            if (states[first] != states[i] ||
                output != (generate && (first == 1 || first == 3 || first == 7)))
                return false;
            continue;
        }
        uses[useCount++] = {&resources.values[i], output
            ? transport::ResourceAccess::write : transport::ResourceAccess::read};
        const std::uint32_t target = output ? 0x8u : 0x40u;
        if (states[i] == target) continue;
        auto& barrier = barriers[count++];
        barrier.resource = pointer(addresses[i]);
        barrier.before = states[i];
        barrier.after = target;
    }
    using ResourceBarrier = void (__attribute__((ms_abi)) *)(
        void*, std::uint32_t, const Barrier*);
    auto transition = reinterpret_cast<ResourceBarrier>(
        (*static_cast<void***>(list))[26]);
    if (count) transition(list, count, barriers.data());
    transport::RecordRequest request{prepared, uses.data(), useCount};
    bool result = transport::record(command.value, request);
    for (unsigned i = 0; i != count; ++i)
        std::swap(barriers[i].before, barriers[i].after);
    if (count) transition(list, count, barriers.data());
    return result;
}

struct CameraPlanes {
    float nearPlane;
    float farPlane;
};

bool normalizeCameraPlanes(const yaagl_fsr_fg_prepare_packet& p,
                           std::uint32_t createFlags, CameraPlanes& planes) noexcept {
    if (!std::isfinite(p.camera_near) || p.camera_near <= 0) return false;
    if (createFlags & YAAGL_FSR_FG_DEPTH_INFINITE) {
        planes = {p.camera_near, INFINITY};
        return true;
    }
    if (std::isnan(p.camera_far) ||
        (std::isinf(p.camera_far) && p.camera_far < 0)) return false;
    if (std::isinf(p.camera_far)) {
        planes = {p.camera_near, INFINITY};
        return true;
    }
    if (!std::isfinite(p.camera_far) || p.camera_far <= 0 ||
        p.camera_far == p.camera_near)
        return false;
    planes = {std::min(p.camera_near, p.camera_far),
              std::max(p.camera_near, p.camera_far)};
    return true;
}

bool validParameters(const yaagl_fsr_fg_prepare_packet& p, std::uint32_t createFlags) {
    CameraPlanes planes{};
    if (p.reconstruct_camera_motion > 1 ||
        p.motion_invalid_value_present > 1 ||
        (p.motion_invalid_value_present && !std::isfinite(p.motion_invalid_value)) ||
        p.projection_matrix_present > 1 ||
        (p.projection_matrix_present && !p.camera_info_present) ||
        (p.flags & ~31u) ||
        (p.camera_aspect_ratio < 0 || !std::isfinite(p.camera_aspect_ratio)) ||
        !p.render_width || !p.render_height ||
        !std::isfinite(p.jitter_x) || !std::isfinite(p.jitter_y) ||
        !std::isfinite(p.motion_scale_x) || !std::isfinite(p.motion_scale_y) ||
        !std::isfinite(p.frame_time_delta_ms) || p.frame_time_delta_ms < 0 ||
        !std::isfinite(p.view_space_to_meters) ||
        !normalizeCameraPlanes(p, createFlags, planes) ||
        !std::isfinite(p.camera_fov_vertical_radians) ||
        p.camera_fov_vertical_radians <= 0 ||
        p.camera_fov_vertical_radians >= 3.14159265358979323846f)
        return false;
    if (p.projection_matrix_present) {
        simd_float4x4 matrix{};
        std::memcpy(&matrix, p.view_to_clip, sizeof(matrix));
        const float determinant = simd_determinant(matrix);
        if (!std::isfinite(determinant) || determinant == 0) return false;
    }
    if (p.reconstruct_camera_motion) {
        simd_float4x4 matrix{};
        std::memcpy(&matrix, p.clip_to_previous_clip, sizeof(matrix));
        const float determinant = simd_determinant(matrix);
        if (!std::isfinite(determinant) || determinant == 0) return false;
    }
    if (!p.camera_info_present) return true;
    simd_float3 basis[3]{};
    for (unsigned i = 0; i != 3; ++i) {
        if (!std::isfinite(p.camera_position[i]) || !std::isfinite(p.camera_up[i]) ||
            !std::isfinite(p.camera_right[i]) || !std::isfinite(p.camera_forward[i]))
            return false;
        basis[0][i] = p.camera_up[i];
        basis[1][i] = p.camera_right[i];
        basis[2][i] = p.camera_forward[i];
    }
    constexpr float tolerance = 1.0e-3f;
    for (const auto& axis : basis)
        if (std::abs(simd_length(axis) - 1.0f) > tolerance) return false;
    return std::abs(simd_dot(basis[0], basis[1])) <= tolerance &&
           std::abs(simd_dot(basis[0], basis[2])) <= tolerance &&
           std::abs(simd_dot(basis[1], basis[2])) <= tolerance;
}

bool validTransfer(const yaagl_fsr_fg_dispatch_packet& p) {
    if (p.backbuffer_transfer_function > FFX_API_BACKBUFFER_TRANSFER_FUNCTION_SCRGB ||
        !std::isfinite(p.min_luminance) || !std::isfinite(p.max_luminance))
        return false;
    if (p.backbuffer_transfer_function == FFX_API_BACKBUFFER_TRANSFER_FUNCTION_PQ)
        return p.max_luminance > 0;
    if (p.backbuffer_transfer_function == FFX_API_BACKBUFFER_TRANSFER_FUNCTION_SCRGB)
        return p.max_luminance > p.min_luminance;
    return true;
}

bool resolveRect(const yaagl_fsr_fg_dispatch_packet& p, NSUInteger displayWidth,
                 NSUInteger displayHeight, std::int32_t& left, std::int32_t& top,
                 NSUInteger& width, NSUInteger& height) {
    const bool defaultRect = p.generation_rect_width == 0 &&
                             p.generation_rect_height == 0;
    if (defaultRect) {
        if (p.generation_rect_left || p.generation_rect_top) return false;
        left = top = 0; width = displayWidth; height = displayHeight;
        return true;
    }
    if (p.generation_rect_left < 0 || p.generation_rect_top < 0 ||
        p.generation_rect_width <= 0 || p.generation_rect_height <= 0)
        return false;
    const std::uint64_t right = std::uint64_t(p.generation_rect_left) +
                                std::uint64_t(p.generation_rect_width);
    const std::uint64_t bottom = std::uint64_t(p.generation_rect_top) +
                                 std::uint64_t(p.generation_rect_height);
    if (right > displayWidth || bottom > displayHeight) return false;
    left = p.generation_rect_left; top = p.generation_rect_top;
    width = p.generation_rect_width; height = p.generation_rect_height;
    return true;
}

struct Copy {
    id<MTLTexture> source;
    id<MTLTexture> destination;
    NSUInteger width, height;
    NSUInteger sourceX = 0, sourceY = 0;
    NSUInteger destinationX = 0, destinationY = 0;
};

// A generation prepass owns one encoder, including on partial encoding failure.
struct API_AVAILABLE(macos(26.0)) Prepass {
    id<MTL4ComputeCommandEncoder> encoder = nil;
    id<MTLFence> fence = nil;
    ~Prepass() {
        @try { finish(); } @catch (id) {}
    }
    void finish() {
        if (!encoder) return;
        auto active = encoder;
        encoder = nil;
        @try {
            [active updateFence:fence afterEncoderStages:MTLStageBlit | MTLStageDispatch];
        } @finally {
            [active endEncoding];
        }
    }
};

API_AVAILABLE(macos(26.0))
bool copyTextures(id<MTL4CommandBuffer> buffer, id<MTLFence> fence, const Copy* copies,
                  std::size_t count, std::vector<Object>& retained,
                  id<MTL4ComputeCommandEncoder> sharedEncoder = nil) {
    if (@available(macOS 26.0, *)) {
        id<MTL4ComputeCommandEncoder> encoder =
            sharedEncoder ? sharedEncoder : [buffer computeCommandEncoder];
        if (!encoder) return false;
        @try {
            if (!sharedEncoder) retained.emplace_back(encoder);
            if (fence && !sharedEncoder)
                [encoder waitForFence:fence beforeEncoderStages:MTLStageBlit];
            for (std::size_t i = 0; i != count; ++i) {
                const auto& copy = copies[i];
                [encoder copyFromTexture:copy.source sourceSlice:0 sourceLevel:0
                            sourceOrigin:MTLOriginMake(copy.sourceX, copy.sourceY, 0)
                              sourceSize:MTLSizeMake(copy.width, copy.height, 1)
                               toTexture:copy.destination destinationSlice:0 destinationLevel:0
                       destinationOrigin:MTLOriginMake(copy.destinationX, copy.destinationY, 0)];
            }
            if (sharedEncoder) {
                [encoder barrierAfterEncoderStages:MTLStageBlit
                               beforeEncoderStages:MTLStageDispatch
                                 visibilityOptions:MTL4VisibilityOptionDevice];
            }
        } @finally {
            if (!sharedEncoder) {
                @try {
                    if (fence) [encoder updateFence:fence afterEncoderStages:MTLStageBlit];
                } @finally {
                    [encoder endEncoding];
                }
            }
        }
        return true;
    }
    return false;
}

struct alignas(16) FgParams {
    std::int32_t sourceOrigin[2];
    std::uint32_t extent[2];
    std::uint32_t sourceExtent[2];
    std::uint32_t motionTargetExtent[2];
    std::uint32_t outputExtent[2];
    float motionScale[2];
    float jitterCancellation[2];
    float minLuminance;
    float maxLuminance;
    std::uint32_t transfer;
    std::uint32_t debugFlags;
    std::uint32_t reset;
    std::uint32_t disableHint;
    std::uint32_t uiOrigin[2], sceneOrigin[2], presentOrigin[2];
    std::uint32_t uiMode;
    std::uint32_t reconstructCameraMotion;
    float clipToPreviousClip[16];
    float motionVectorsInvalidValue;
    std::uint32_t motionVectorsInvalidValuePresent;
    float sourceJitterUv[2];
    float sourceUvScale[2] = {1.0f, 1.0f};
    float sourceUvOffset[2];
};
static_assert(sizeof(FgParams) == 208);

struct PassTextures {
    id<MTLTexture> source = nil;
    id<MTLTexture> destination = nil;
    // Slots 2/3 carry depth/motion for scatter and extra inputs/outputs for fused passes.
    id<MTLTexture> auxiliary0 = nil;
    id<MTLTexture> auxiliary1 = nil;
    id<MTLTexture> ui = nil;
    id<MTLTexture> scene = nil;
    id<MTLTexture> present = nil;
};

API_AVAILABLE(macos(26.0))
bool computePass(id<MTL4CommandBuffer> buffer, id<MTLFence> fence, id<MTL4ArgumentTable> table,
                 NSUInteger threadgroupWidth,
                 id<MTLComputePipelineState> pipeline, const PassTextures& textures,
                 id<MTLBuffer> parameterBuffer,
                 NSUInteger parameterOffset, const FgParams& params,
                 std::vector<Object>& retained, id<MTL4ComputeCommandEncoder> sharedEncoder = nil,
                 bool depthDependency = false) {
    if (!pipeline || !textures.source || !textures.destination || !parameterBuffer) return false;
    if (@available(macOS 26.0, *)) {
        if (!table) return false;
        [table setAddress:parameterBuffer.gpuAddress + parameterOffset atIndex:0];
        const id<MTLTexture> bindings[]{
            textures.source, textures.destination, textures.auxiliary0,
            textures.auxiliary0 ? textures.auxiliary1 : nil,
            textures.auxiliary0 ? (textures.ui ? textures.ui : textures.source) : nil,
            textures.auxiliary0 ? (textures.scene ? textures.scene : textures.source) : nil,
            textures.auxiliary0 ? (textures.present ? textures.present : textures.source) : nil};
        for (NSUInteger i = 0; i < std::size(bindings); ++i)
            [table setTexture:(bindings[i] ? bindings[i].gpuResourceID : MTLResourceID{})
                      atIndex:i];
        id<MTL4ComputeCommandEncoder> encoder =
            sharedEncoder ? sharedEncoder : [buffer computeCommandEncoder];
        if (!encoder) return false;
        @try {
            if (!sharedEncoder) {
                retained.emplace_back(encoder);
                [encoder waitForFence:fence beforeEncoderStages:MTLStageDispatch];
            } else if (depthDependency) {
                // Motion reconstruction consumes the stored normalized depth.
                [encoder barrierAfterEncoderStages:MTLStageDispatch
                               beforeEncoderStages:MTLStageDispatch
                                 visibilityOptions:MTL4VisibilityOptionDevice];
            }
            [encoder setComputePipelineState:pipeline];
            [encoder setArgumentTable:table];
            [encoder dispatchThreads:MTLSizeMake(params.extent[0], params.extent[1], 1)
                 threadsPerThreadgroup:MTLSizeMake(threadgroupWidth, 8, 1)];
        } @finally {
            if (!sharedEncoder) {
                @try {
                    [encoder updateFence:fence afterEncoderStages:MTLStageDispatch];
                } @finally {
                    [encoder endEncoding];
                }
            }
        }
        return true;
    }
    return false;
}

bool sameConfiguration(const Configuration& a, const Configuration& b) {
    return a.factory.get() == b.factory.get() &&
           a.device.get() == b.device.get() &&
           a.color == b.color && a.ui == b.ui &&
           a.sourceColor == b.sourceColor && a.sourceUi == b.sourceUi &&
           a.transfer == b.transfer && a.cameraPresent == b.cameraPresent &&
           a.depth == b.depth && a.motion == b.motion &&
           a.width == b.width && a.height == b.height &&
           a.outputWidth == b.outputWidth && a.outputHeight == b.outputHeight &&
           a.distortionEnabled == b.distortionEnabled;
}

bool sameHistoryValues(const yaagl_fsr_fg_dispatch_packet& a,
                       const yaagl_fsr_fg_dispatch_packet& b) {
    return a.generation_rect_left == b.generation_rect_left &&
           a.generation_rect_top == b.generation_rect_top &&
           a.generation_rect_width == b.generation_rect_width &&
           a.generation_rect_height == b.generation_rect_height &&
           a.present_x == b.present_x &&
           a.present_y == b.present_y &&
           a.hudless_x == b.hudless_x &&
           a.hudless_y == b.hudless_y &&
           a.backbuffer_transfer_function == b.backbuffer_transfer_function &&
           (a.backbuffer_transfer_function == FFX_API_BACKBUFFER_TRANSFER_FUNCTION_SRGB ||
            (a.min_luminance == b.min_luminance &&
             a.max_luminance == b.max_luminance));
}

} // namespace

struct PreparedFrame::Impl {
    enum class Kind { Snapshot, Generate };
    Kind kind = Kind::Snapshot;
    std::shared_ptr<State> state;
    std::shared_ptr<Snapshot> snapshot;
    std::shared_ptr<Configuration> configuration;
    Object first;
    Object second;
    Object third;
    Object fourth;
    Object ui;
    Object uiAlpha;
    Object distortion;
    Object disableInterpolation;
    std::uint64_t disableInterpolationOffset = 0;
    yaagl_fsr_fg_dispatch_packet dispatch{};
    bool reset = false;
    std::uint64_t generationEpoch = 0;
};

struct ExecutionLease::Impl {
    std::shared_ptr<PreparedFrame::Impl> prepared;
    std::shared_ptr<TextureOwner> currentScene, previousScene;
    std::vector<Object> objects;
    Object residency;
    Object argumentTable;
    std::shared_ptr<ScratchSlot> scratchSlot;
    std::unique_ptr<Scratch> scratch;
    std::uint64_t scratchEpoch = 0;
    bool recycleScratch = false;

    ~Impl() {
        if (residency) {
            if (@available(macOS 15.0, *)) {
                auto set = (id<MTLResidencySet>)residency.get();
                [set endResidency];
                [set removeAllAllocations];
                [set commit];
            }
        }
        if (scratch) {
            scratch->residency = residency;
            scratch->argumentTable = argumentTable;
            std::lock_guard lock(scratchSlot->mutex);
            if (recycleScratch && scratchEpoch == scratchSlot->epoch && !scratchSlot->idle)
                scratchSlot->idle = std::move(scratch);
        }
    }

    bool makeResident(id buffer) {
        if (@available(macOS 26.0, *)) {
            auto device = (id<MTLDevice>)prepared->configuration->device.get();
            id<MTLResidencySet> set = (id<MTLResidencySet>)residency.get();
            if (!set) {
                MTLResidencySetDescriptor* descriptor =
                    [[MTLResidencySetDescriptor alloc] init];
                @try {
                    descriptor.initialCapacity = objects.size();
                    NSError* error = nil;
                    set = [device newResidencySetWithDescriptor:descriptor error:&error];
                } @finally {
                    [descriptor release];
                }
                if (!set) return false;
                residency = Object(set);
                [set release];
            }
            for (const auto& object : objects)
                if ([object.get() conformsToProtocol:@protocol(MTLAllocation)])
                    [set addAllocation:(id<MTLAllocation>)object.get()];
            [set commit];
            [set requestResidency];
            [(id<MTL4CommandBuffer>)buffer useResidencySet:set];
            return true;
        }
        return false;
    }
};

PreparedFrame::PreparedFrame(std::shared_ptr<Impl> impl) noexcept
    : impl_(std::move(impl)) {}
PreparedFrame::~PreparedFrame() = default;
ExecutionLease::ExecutionLease(std::shared_ptr<Impl> impl) noexcept
    : impl_(std::move(impl)) {}
ExecutionLease::~ExecutionLease() = default;

bool PreparedFrame::encode(
    void* commandBuffer, void* fence,
    std::shared_ptr<const ExecutionLease>& lease) const noexcept {
    lease.reset();
    auto fail = [&](const char* stage) noexcept {
        if (impl_ && impl_->snapshot && impl_->state) {
            const auto& p = impl_->snapshot->parameters;
            logFailure(impl_->kind == Impl::Kind::Snapshot
                           ? YAAGL_FSR_FG_PREPARE : YAAGL_FSR_FG_DISPATCH,
                       stage, Runtime, impl_->state->creation.flags,
                       p.render_width, p.render_height, p.camera_near, p.camera_far,
                       p.camera_fov_vertical_radians, p.view_space_to_meters);
        } else {
            logFailure(YAAGL_FSR_FG_DISPATCH, stage, Runtime);
        }
        return false;
    };
    try {
        @try {
            if (@available(macOS 26.0, *)) {
                if (!commandBuffer || !impl_ || !impl_->snapshot) return fail("encode_arguments");
                auto buffer = (id<MTL4CommandBuffer>)commandBuffer;
                auto& configuration = *impl_->configuration;
                const NSUInteger threadgroupWidth =
                    configuration.threadgroupWidth(impl_->snapshot->parameters);
                if (![buffer conformsToProtocol:@protocol(MTL4CommandBuffer)])
                    return fail("encode_command_buffer_type");
                auto execution = std::make_shared<ExecutionLease::Impl>();
                execution->prepared = impl_;
                const id inputs[]{
                    (id)fence, impl_->first.get(), impl_->second.get(), impl_->third.get(),
                    impl_->fourth.get(), impl_->ui.get(), impl_->uiAlpha.get(),
                    impl_->distortion.get(), impl_->disableInterpolation.get(),
                    impl_->snapshot->depth->texture.get(), impl_->snapshot->motion->texture.get()};

                auto retainedLease = std::shared_ptr<const ExecutionLease>(
                    new ExecutionLease(execution));
                /* Publish ownership before the first command is encoded. */
                lease = retainedLease;

                std::lock_guard executionLock(impl_->state->executionMutex);
                if (impl_->kind == Impl::Kind::Snapshot) {
                    // Input ownership plus the single snapshot copy encoder.
                    execution->objects.reserve(std::size(inputs) + 1);
                    for (id object : inputs) execution->objects.emplace_back(object);
                    if (!execution->makeResident(buffer))
                        return fail("snapshot_residency");
                    const std::array<Copy, 2> copies{{
                        {(id<MTLTexture>)impl_->first.get(),
                         (id<MTLTexture>)impl_->snapshot->depth->texture.get(),
                         impl_->snapshot->depthWidth, impl_->snapshot->depthHeight,
                         impl_->snapshot->parameters.depth_subrect_x,
                         impl_->snapshot->parameters.depth_subrect_y},
                        {(id<MTLTexture>)impl_->second.get(),
                         (id<MTLTexture>)impl_->snapshot->motion->texture.get(),
                         impl_->snapshot->motionWidth, impl_->snapshot->motionHeight,
                         impl_->snapshot->parameters.motion_subrect_x,
                         impl_->snapshot->parameters.motion_subrect_y}
                    }};
                    if (!copyTextures(buffer, (id<MTLFence>)fence,
                                      copies.data(), copies.size(), execution->objects))
                        return fail("snapshot_copy");
                    impl_->snapshot->produced = true;
                    return true;
                }

                if (!impl_->snapshot->produced)
                    return fail("generate_before_prepare_execution");

                Object interpolator = configuration.factory;
                if (!interpolator) return fail("encode_interpolator");
                auto effect = (id<MTLFXFrameInterpolatorBase>)interpolator.get();
                auto device = (id<MTLDevice>)configuration.device.get();
                execution->currentScene = acquireTexture(
                    impl_->state->scene,
                    impl_->generationEpoch == impl_->state->generationEpoch &&
                        impl_->state->generationEnabled,
                    device, configuration.color,
                    configuration.outputWidth, configuration.outputHeight,
                    effect.colorTextureUsage | MTLTextureUsageShaderRead |
                    MTLTextureUsageShaderWrite);
                Object current = execution->currentScene->texture;
                // MetalFX infers UI only from the precomposited present image.
                // Explicit premultiplied UI is composited once by encode_scatter
                // in the supplied color domain, after MetalFX generates the scene.
                const bool inferUi = configuration.ui != MTLPixelFormatInvalid && !impl_->ui;
                execution->scratchSlot = impl_->state->scratch;
                execution->recycleScratch =
                    impl_->generationEpoch == impl_->state->generationEpoch &&
                    impl_->state->generationEnabled;
                {
                    std::lock_guard lock(execution->scratchSlot->mutex);
                    execution->scratchEpoch = execution->scratchSlot->epoch;
                    auto& idle = execution->scratchSlot->idle;
                    if (execution->recycleScratch && idle &&
                        idle->configuration == impl_->configuration &&
                        idle->inferUi == inferUi)
                        execution->scratch = std::move(idle);
                    if (execution->recycleScratch) idle.reset();
                }
                if (!execution->scratch) {
                    execution->scratch = std::make_unique<Scratch>();
                    execution->scratch->configuration = impl_->configuration;
                    execution->scratch->inferUi = inferUi;
                }
                auto& scratch = *execution->scratch;
                execution->residency = scratch.residency;
                execution->argumentTable = scratch.argumentTable;
                Object ui = scratch.ui;
                if (!ui && inferUi)
                    ui = privateTexture(device, configuration.ui, configuration.outputWidth,
                                        configuration.outputHeight, effect.uiTextureUsage |
                                        MTLTextureUsageShaderWrite);
                Object normalizedDepth = scratch.depth ? scratch.depth : privateTexture(device, configuration.depth,
                    configuration.width, configuration.height,
                    effect.depthTextureUsage | MTLTextureUsageShaderRead |
                    MTLTextureUsageShaderWrite);
                Object normalizedMotion = scratch.motion ? scratch.motion : privateTexture(device, configuration.motion,
                    configuration.width, configuration.height,
                    effect.motionTextureUsage | MTLTextureUsageShaderRead |
                    MTLTextureUsageShaderWrite);
                Object output = scratch.output ? scratch.output : privateTexture(device, configuration.color,
                    configuration.outputWidth, configuration.outputHeight,
                    effect.outputTextureUsage | MTLTextureUsageShaderRead |
                    MTLTextureUsageShaderWrite);
                auto distortionSource = (id<MTLTexture>)impl_->distortion.get();
                const bool decodeDistortion = distortionSource &&
                    impl_->dispatch.distortion_low_precision;
                const bool cropDistortion = distortionSource &&
                    (impl_->dispatch.distortion_x || impl_->dispatch.distortion_y ||
                     distortionSource.width != impl_->dispatch.distortion_width ||
                     distortionSource.height != impl_->dispatch.distortion_height);
                Object distortion = impl_->distortion;
                if ((cropDistortion || decodeDistortion) && distortionSource) {
                    const auto format = decodeDistortion ? MTLPixelFormatRGBA32Float :
                                                          distortionSource.pixelFormat;
                    const auto usage = MTLTextureUsageShaderRead |
                        (decodeDistortion ? MTLTextureUsageShaderWrite : 0);
                    auto cached = (id<MTLTexture>)scratch.distortion.get();
                    if (!cached || cached.device != device || cached.pixelFormat != format ||
                        cached.width != impl_->dispatch.distortion_width ||
                        cached.height != impl_->dispatch.distortion_height || cached.usage != usage)
                        scratch.distortion = privateTexture(device, format,
                            impl_->dispatch.distortion_width, impl_->dispatch.distortion_height,
                            usage);
                    distortion = scratch.distortion;
                } else {
                    scratch.distortion = {};
                }
                if (!current || !normalizedDepth || !normalizedMotion || !output ||
                    (inferUi && !ui))
                    return fail("encode_intermediate_textures");
                scratch.ui = ui;
                scratch.depth = normalizedDepth;
                scratch.motion = normalizedMotion;
                scratch.output = output;

                auto& state = *impl_->state;
                const std::uint64_t frame = impl_->dispatch.frame_id;
                bool reset = impl_->reset || impl_->snapshot->parameters.reset ||
                    !state.history || !state.historyConfiguration ||
                    !sameConfiguration(configuration, *state.historyConfiguration) ||
                    !sameHistoryValues(impl_->dispatch, state.historyDispatch) ||
                    state.historyFrame + 1 != frame;
                execution->previousScene = reset ? execution->currentScene : state.history;
                Object previous = execution->previousScene->texture;

                id<MTLTexture> present = (id<MTLTexture>)impl_->first.get();
                id<MTLTexture> scene = impl_->third
                    ? (id<MTLTexture>)impl_->third.get() : present;
                auto rawView = [&](id<MTLTexture> texture) -> Object {
                    MTLPixelFormat raw = texture.pixelFormat;
                    if (raw == MTLPixelFormatRGBA8Unorm_sRGB) raw = MTLPixelFormatRGBA8Unorm;
                    else if (raw == MTLPixelFormatBGRA8Unorm_sRGB) raw = MTLPixelFormatBGRA8Unorm;
                    if (raw == texture.pixelFormat) return Object(texture);
                    id<MTLTexture> view = [texture newTextureViewWithPixelFormat:raw];
                    Object result(view);
                    [view release];
                    return result;
                };
                Object sceneView = rawView(scene);
                Object presentView = scene == present ? sceneView : rawView(present);
                Object outputView = rawView((id<MTLTexture>)impl_->second.get());
                Object uiSourceView = impl_->ui
                    ? rawView((id<MTLTexture>)impl_->ui.get())
                    : impl_->uiAlpha ? rawView((id<MTLTexture>)impl_->uiAlpha.get())
                                     : presentView;
                Object distortionView = distortion
                    ? rawView((id<MTLTexture>)distortion.get()) : Object{};
                if (!sceneView || !presentView || !outputView ||
                    (configuration.ui != MTLPixelFormatInvalid && !uiSourceView) ||
                    (configuration.distortionEnabled && !distortionView))
                    return fail("encode_texture_views");
                const bool currentGeneration =
                    impl_->generationEpoch == state.generationEpoch &&
                    state.generationEnabled;
                const auto& p = impl_->snapshot->parameters;
                const auto& d = impl_->dispatch;
                FgParams colorParams{};
                colorParams.sourceOrigin[0] = d.output_x;
                colorParams.sourceOrigin[1] = d.output_y;
                colorParams.extent[0] = configuration.outputWidth;
                colorParams.extent[1] = configuration.outputHeight;
                colorParams.sourceExtent[0] = state.creation.display_width;
                colorParams.sourceExtent[1] = state.creation.display_height;
                colorParams.motionTargetExtent[0] = state.creation.display_width;
                colorParams.motionTargetExtent[1] = state.creation.display_height;
                colorParams.outputExtent[0] = configuration.outputWidth;
                colorParams.outputExtent[1] = configuration.outputHeight;
                colorParams.minLuminance = d.min_luminance;
                colorParams.maxLuminance = d.max_luminance;
                colorParams.transfer = d.backbuffer_transfer_function;
                colorParams.debugFlags = d.flags | (p.flags & 7u);
                colorParams.reset = reset;
                colorParams.disableHint = reset || !currentGeneration;
                colorParams.uiMode = impl_->ui ? 1 : impl_->uiAlpha ? 2 : 0;
                colorParams.uiOrigin[0] = impl_->ui ? d.ui_x : d.ui_alpha_x;
                colorParams.uiOrigin[1] = impl_->ui ? d.ui_y : d.ui_alpha_y;
                colorParams.sceneOrigin[0] = d.hudless_x;
                colorParams.sceneOrigin[1] = d.hudless_y;
                colorParams.presentOrigin[0] = d.present_x;
                colorParams.presentOrigin[1] = d.present_y;
                colorParams.reconstructCameraMotion = p.reconstruct_camera_motion;
                colorParams.motionVectorsInvalidValue = p.motion_invalid_value;
                colorParams.motionVectorsInvalidValuePresent = p.motion_invalid_value_present;
                std::memcpy(colorParams.clipToPreviousClip, p.clip_to_previous_clip,
                            sizeof(p.clip_to_previous_clip));
                FgParams sceneParams = colorParams;
                sceneParams.sourceOrigin[0] = impl_->third ? d.hudless_x : d.present_x;
                sceneParams.sourceOrigin[1] = impl_->third ? d.hudless_y : d.present_y;
                FgParams uiParams = colorParams;
                uiParams.sourceOrigin[0] = d.present_x;
                uiParams.sourceOrigin[1] = d.present_y;

                FgParams depthParams{};
                depthParams.extent[0] = configuration.width;
                depthParams.extent[1] = configuration.height;
                depthParams.sourceExtent[0] = impl_->snapshot->depthWidth;
                depthParams.sourceExtent[1] = impl_->snapshot->depthHeight;
                depthParams.sourceJitterUv[0] = p.jitter_x / float(p.render_width);
                depthParams.sourceJitterUv[1] = p.jitter_y / float(p.render_height);
                if (!d.independent_subrects) {
                    // Public generation rectangles crop the full display, not
                    // the render snapshot. Apply the crop before render jitter.
                    depthParams.sourceUvScale[0] =
                        float(configuration.width) / state.creation.display_width;
                    depthParams.sourceUvScale[1] =
                        float(configuration.height) / state.creation.display_height;
                    depthParams.sourceUvOffset[0] =
                        float(sceneParams.sourceOrigin[0]) / state.creation.display_width;
                    depthParams.sourceUvOffset[1] =
                        float(sceneParams.sourceOrigin[1]) / state.creation.display_height;
                }

                FgParams motionParams{};
                motionParams.extent[0] = configuration.width;
                motionParams.extent[1] = configuration.height;
                const bool displayMotion = (state.creation.flags &
                    YAAGL_FSR_FG_DISPLAY_RESOLUTION_MOTION_VECTORS) != 0;
                // Snapshot copies have already consumed the resource subrect origins.
                motionParams.sourceExtent[0] = impl_->snapshot->motionWidth;
                motionParams.sourceExtent[1] = impl_->snapshot->motionHeight;
                // Render-grid samples sit at (texel + 0.5 - J) / renderExtent.
                // Pull them into the stabilized color grid independently of
                // whether the vector values themselves contain jitter.
                // Display-resolution motion already uses the stabilized grid.
                if (!displayMotion) {
                    motionParams.sourceJitterUv[0] = depthParams.sourceJitterUv[0];
                    motionParams.sourceJitterUv[1] = depthParams.sourceJitterUv[1];
                }
                motionParams.motionTargetExtent[0] = displayMotion
                    ? state.creation.display_width
                    : (p.motion_width ? p.motion_width : p.render_width);
                motionParams.motionTargetExtent[1] = displayMotion
                    ? state.creation.display_height
                    : (p.motion_height ? p.motion_height : p.render_height);
                motionParams.outputExtent[0] = configuration.outputWidth;
                motionParams.outputExtent[1] = configuration.outputHeight;
                if (!displayMotion && !d.independent_subrects) {
                    for (unsigned axis = 0; axis < 2; ++axis) {
                        motionParams.sourceUvScale[axis] = depthParams.sourceUvScale[axis];
                        motionParams.sourceUvOffset[axis] = depthParams.sourceUvOffset[axis];
                    }
                    // Cropping does not shrink full-render pixel displacement.
                    motionParams.outputExtent[0] = state.creation.display_width;
                    motionParams.outputExtent[1] = state.creation.display_height;
                }
                if (displayMotion && !d.independent_subrects &&
                    impl_->snapshot->motionWidth == state.creation.display_width &&
                    impl_->snapshot->motionHeight == state.creation.display_height) {
                    // Full-display motion shares the public color crop. Cropping
                    // does not scale pixel displacement; packed domains still resample.
                    motionParams.sourceOrigin[0] = sceneParams.sourceOrigin[0];
                    motionParams.sourceOrigin[1] = sceneParams.sourceOrigin[1];
                    motionParams.sourceExtent[0] = configuration.width;
                    motionParams.sourceExtent[1] = configuration.height;
                    motionParams.outputExtent[0] = state.creation.display_width;
                    motionParams.outputExtent[1] = state.creation.display_height;
                }
                motionParams.motionScale[0] = p.motion_scale_x;
                motionParams.motionScale[1] = p.motion_scale_y;
                motionParams.reconstructCameraMotion = p.reconstruct_camera_motion;
                motionParams.motionVectorsInvalidValue = p.motion_invalid_value;
                motionParams.motionVectorsInvalidValuePresent = p.motion_invalid_value_present;
                std::memcpy(motionParams.clipToPreviousClip, p.clip_to_previous_clip,
                            sizeof(p.clip_to_previous_clip));
                if ((state.creation.flags & YAAGL_FSR_FG_MOTION_VECTORS_JITTERED) &&
                    impl_->snapshot->hasPreviousJitter && !reset) {
                    // The kernel subtracts this in the motion-value target domain,
                    // then converts to output pixels. Packet motion extents may be zero.
                    motionParams.jitterCancellation[0] =
                        (impl_->snapshot->previousJitterUvX -
                         p.jitter_x / float(p.render_width)) *
                        float(motionParams.motionTargetExtent[0]);
                    motionParams.jitterCancellation[1] =
                        (impl_->snapshot->previousJitterUvY -
                         p.jitter_y / float(p.render_height)) *
                        float(motionParams.motionTargetExtent[1]);
                }

                FgParams distortionParams{};
                distortionParams.sourceOrigin[0] = d.distortion_x;
                distortionParams.sourceOrigin[1] = d.distortion_y;
                distortionParams.extent[0] = d.distortion_width;
                distortionParams.extent[1] = d.distortion_height;
                distortionParams.motionScale[0] = d.distortion_scale;
                distortionParams.motionScale[1] = d.distortion_bias;
                // Each execution owns immutable, naturally aligned constant slots.
                struct ParameterBlock {
                    FgParams color, scene, ui, distortion, depth, motion;
                };
                static_assert(offsetof(ParameterBlock, ui) ==
                              offsetof(ParameterBlock, scene) + sizeof(FgParams));
                static_assert(offsetof(ParameterBlock, motion) ==
                              offsetof(ParameterBlock, depth) + sizeof(FgParams));
                const ParameterBlock parameters{
                    colorParams, sceneParams, uiParams, distortionParams, depthParams, motionParams};
                if (!scratch.parameters) {
                    id<MTLBuffer> allocated =
                    [device newBufferWithLength:sizeof(parameters)
                                       options:MTLResourceStorageModeShared];
                    scratch.parameters = Object(allocated);
                    [allocated release];
                }
                Object parameterOwner = scratch.parameters;
                auto parameterBuffer = (id<MTLBuffer>)parameterOwner.get();
                if (!parameterOwner)
                    return fail("encode_parameter_buffers");
                std::memcpy(parameterBuffer.contents, &parameters, sizeof(parameters));
                const id generated[]{
                    interpolator.get(), current.get(), ui.get(), normalizedDepth.get(),
                    normalizedMotion.get(), output.get(), previous.get(), distortion.get(),
                    sceneView.get(), presentView.get(), outputView.get()};
                const id passOutputs[]{
                    current.get(), ui.get(), decodeDistortion ? distortion.get() : nil,
                    normalizedDepth.get(), normalizedMotion.get(), output.get()};
                const auto dispatchEncoderUpperBound =
                    std::count_if(std::begin(passOutputs), std::end(passOutputs),
                                  [](id object) { return object != nil; });
                const bool retainUiView = uiSourceView && uiSourceView.get() != presentView.get();
                id<MTL4ArgumentTable> argumentTable =
                    (id<MTL4ArgumentTable>)execution->argumentTable.get();
                if (!argumentTable) {
                    MTL4ArgumentTableDescriptor* descriptor = [MTL4ArgumentTableDescriptor new];
                    @try {
                        descriptor.maxBufferBindCount = 1;
                        descriptor.maxTextureBindCount = 7;
                        descriptor.initializeBindings = YES;
                        NSError* error = nil;
                        argumentTable = [device newArgumentTableWithDescriptor:descriptor error:&error];
                    } @finally {
                        [descriptor release];
                    }
                    execution->argumentTable = Object(argumentTable);
                    [argumentTable release];
                    if (!argumentTable) return fail("encode_argument_table");
                }
                // Metal snapshots the execution-local table at each dispatch.
                // Reserve the per-output encoder upper bound before any GPU work;
                // Metal4 batches these passes. Include constants, the baseline copy
                // encoder and the optional disable-hint encoder.
                execution->objects.reserve(std::size(inputs) + std::size(generated) +
                    retainUiView + bool(distortionView) + 1 + dispatchEncoderUpperBound +
                    1 + bool(impl_->disableInterpolation));
                for (id object : inputs) execution->objects.emplace_back(object);
                for (id object : generated) execution->objects.emplace_back(object);
                if (retainUiView) execution->objects.push_back(uiSourceView);
                if (distortionView) execution->objects.push_back(distortionView);
                execution->objects.push_back(parameterOwner);
                // Metal4 GPU addresses must be admitted before the residency set commits.
                if (!execution->makeResident(buffer))
                    return fail("encode_residency");

                const NSUInteger copyX = d.output_subrect_only ? d.present_x : 0;
                const NSUInteger copyY = d.output_subrect_only ? d.present_y : 0;
                const NSUInteger copyWidth = d.output_subrect_only
                    ? configuration.outputWidth : present.width;
                const NSUInteger copyHeight = d.output_subrect_only
                    ? configuration.outputHeight : present.height;
                std::array<Copy, 4> baselines{};
                std::size_t baselineCount = 0;
                baselines[baselineCount++] = {
                    present, (id<MTLTexture>)impl_->second.get(),
                    copyWidth, copyHeight, copyX, copyY,
                    d.output_subrect_only ? d.output_x : 0,
                    d.output_subrect_only ? d.output_y : 0};
                if (impl_->fourth)
                    baselines[baselineCount++] = {
                        present, (id<MTLTexture>)impl_->fourth.get(),
                        copyWidth, copyHeight, copyX, copyY,
                        d.output_subrect_only ? d.real_x : 0,
                        d.output_subrect_only ? d.real_y : 0};
                if (impl_->fourth && !d.output_subrect_only &&
                    (d.real_x != d.present_x || d.real_y != d.present_y))
                    baselines[baselineCount++] = {
                        present, (id<MTLTexture>)impl_->fourth.get(),
                        configuration.outputWidth, configuration.outputHeight,
                        d.present_x, d.present_y, d.real_x, d.real_y};
                if (cropDistortion && !decodeDistortion)
                    baselines[baselineCount++] = {
                        distortionSource, (id<MTLTexture>)distortion.get(),
                        d.distortion_width, d.distortion_height,
                        d.distortion_x, d.distortion_y, 0, 0};
                Prepass prepass;
                prepass.encoder = [(id<MTL4CommandBuffer>)buffer computeCommandEncoder];
                if (!prepass.encoder) return fail("encode_prepass");
                prepass.fence = (id<MTLFence>)fence;
                execution->objects.emplace_back(prepass.encoder);
                [prepass.encoder waitForFence:prepass.fence
                    beforeEncoderStages:MTLStageBlit | MTLStageDispatch];
                if (!copyTextures(buffer, (id<MTLFence>)fence,
                                  baselines.data(), baselineCount, execution->objects,
                                  prepass.encoder))
                    return fail("encode_baseline_copy");
                if (!computePass(buffer, (id<MTLFence>)fence, argumentTable, threadgroupWidth,
                                 (id<MTLComputePipelineState>)(ui
                                     ? configuration.decodeSceneUiPipeline.get()
                                     : configuration.decodePipeline.get()),
                                 {.source = (id<MTLTexture>)sceneView.get(),
                                  .destination = (id<MTLTexture>)current.get(),
                                  .auxiliary0 = ui ? (id<MTLTexture>)uiSourceView.get() : nil,
                                  .auxiliary1 = (id<MTLTexture>)ui.get()},
                                 parameterBuffer, offsetof(ParameterBlock, scene), sceneParams,
                                 execution->objects, prepass.encoder))
                    return fail("encode_scene_decode");
                if (decodeDistortion && !computePass(
                        buffer, (id<MTLFence>)fence, argumentTable, threadgroupWidth,
                        (id<MTLComputePipelineState>)configuration.distortionPipeline.get(),
                        {.source = distortionSource,
                         .destination = (id<MTLTexture>)distortion.get()},
                        parameterBuffer, offsetof(ParameterBlock, distortion), distortionParams,
                        execution->objects, prepass.encoder))
                    return fail("encode_distortion_decode");
                if (configuration.depthMotionPipeline) {
                    if (!computePass(buffer, (id<MTLFence>)fence, argumentTable, threadgroupWidth,
                            (id<MTLComputePipelineState>)configuration.depthMotionPipeline.get(),
                            {.source = (id<MTLTexture>)impl_->snapshot->motion->texture.get(),
                             .destination = (id<MTLTexture>)normalizedMotion.get(),
                             .auxiliary0 = (id<MTLTexture>)impl_->snapshot->depth->texture.get(),
                             .auxiliary1 = (id<MTLTexture>)normalizedDepth.get()},
                            parameterBuffer, offsetof(ParameterBlock, depth), depthParams,
                            execution->objects, prepass.encoder))
                        return fail("encode_depth_motion_normalize");
                } else {
                    if (!computePass(buffer, (id<MTLFence>)fence, argumentTable, threadgroupWidth,
                                     (id<MTLComputePipelineState>)configuration.depthPipeline.get(),
                                     {.source = (id<MTLTexture>)impl_->snapshot->depth->texture.get(),
                                      .destination = (id<MTLTexture>)normalizedDepth.get()},
                                     parameterBuffer, offsetof(ParameterBlock, depth), depthParams,
                                     execution->objects, prepass.encoder))
                        return fail("encode_depth_normalize");
                    if (!computePass(buffer, (id<MTLFence>)fence, argumentTable, threadgroupWidth,
                                     (id<MTLComputePipelineState>)configuration.motionPipeline.get(),
                                     {.source = (id<MTLTexture>)impl_->snapshot->motion->texture.get(),
                                      .destination = (id<MTLTexture>)normalizedMotion.get(),
                                      .auxiliary0 = (id<MTLTexture>)normalizedDepth.get()},
                                     parameterBuffer, offsetof(ParameterBlock, motion), motionParams,
                                     execution->objects, prepass.encoder, true))
                        return fail("encode_motion_normalize");
                }
                prepass.finish();

                const InterpolatorBindings bindings(effect);
                effect.colorTexture = (id<MTLTexture>)current.get();
                effect.prevColorTexture = (id<MTLTexture>)previous.get();
                effect.depthTexture = (id<MTLTexture>)normalizedDepth.get();
                effect.motionTexture = (id<MTLTexture>)normalizedMotion.get();
                effect.uiTexture = (id<MTLTexture>)ui.get();
                effect.uiTextureComposited = bool(ui);
                effect.outputTexture = (id<MTLTexture>)output.get();
                if (@available(macOS 27.0, *)) {
                    effect.distortionTexture = (id<MTLTexture>)distortionView.get();
                    if (distortionView) {
                        effect.distortionOffsetX = 0;
                        effect.distortionOffsetY = 0;
                        effect.distortionWidth = d.distortion_width;
                        effect.distortionHeight = d.distortion_height;
                    }
                }
                effect.fence = (id<MTLFence>)fence;
                effect.shouldResetHistory = reset;
                effect.depthReversed =
                    (state.creation.flags & YAAGL_FSR_FG_DEPTH_INVERTED) != 0;
                effect.motionVectorScaleX = 1.0f;
                effect.motionVectorScaleY = 1.0f;
                // Color is already stabilized, and the prepass aligns depth
                // and motion to it. Native sampling must not reapply render jitter.
                effect.jitterOffsetX = 0.0f;
                effect.jitterOffsetY = 0.0f;
                effect.deltaTime = p.frame_time_delta_ms * 0.001f;
                effect.aspectRatio = p.camera_aspect_ratio > 0
                    ? p.camera_aspect_ratio
                    : float(p.render_width) / float(p.render_height);

                if (@available(macOS 27.0, *)) {
                    // All interpolator inputs are normalized to the origin-zero
                    // generation-rectangle domain before MetalFX consumes them.
                    effect.contentWidth = configuration.outputWidth;
                    effect.contentHeight = configuration.outputHeight;
                    effect.depthContentOffsetX = 0;
                    effect.depthContentOffsetY = 0;
                    effect.motionContentOffsetX = 0;
                    effect.motionContentOffsetY = 0;
                    effect.outputOffsetX = 0;
                    effect.outputOffsetY = 0;
                }
                CameraPlanes planes{};
                if (!normalizeCameraPlanes(p, state.creation.flags, planes)) {
                    logFailure(YAAGL_FSR_FG_DISPATCH, "encode_camera_planes", Parameter,
                               state.creation.flags, p.render_width, p.render_height,
                               p.camera_near, p.camera_far,
                               p.camera_fov_vertical_radians, p.view_space_to_meters);
                    return false;
                }
                const float worldScale = p.view_space_to_meters > 0
                    ? p.view_space_to_meters : 1.0f;
                effect.nearPlane = planes.nearPlane * worldScale;
                effect.farPlane = planes.farPlane * worldScale;
                effect.fieldOfView =
                    p.camera_fov_vertical_radians * 57.295779513082320876f;
                if (p.camera_info_present) {
                    if (@available(macOS 27.0, *)) {
                        simd_float3 position = {p.camera_position[0] * worldScale,
                            p.camera_position[1] * worldScale, p.camera_position[2] * worldScale};
                        simd_float3 right = {p.camera_right[0], p.camera_right[1], p.camera_right[2]};
                        simd_float3 up = {p.camera_up[0], p.camera_up[1], p.camera_up[2]};
                        simd_float3 forward = {p.camera_forward[0], p.camera_forward[1],
                                               p.camera_forward[2]};
                        simd_float4x4 view{};
                        view.columns[0] = {right.x, up.x, forward.x, 0};
                        view.columns[1] = {right.y, up.y, forward.y, 0};
                        view.columns[2] = {right.z, up.z, forward.z, 0};
                        view.columns[3] = {-simd_dot(right, position), -simd_dot(up, position),
                                           -simd_dot(forward, position), 1};
                        float n = effect.nearPlane, f = effect.farPlane;
                        float y = 1.0f / std::tan(p.camera_fov_vertical_radians * 0.5f);
                        // MetalFX complements normal raw depth before camera
                        // reprojection, so synthesized matrices always use its
                        // reversed-depth working domain. Keep depthReversed tied
                        // to the input texture, and private overrides unchanged.
                        float a = std::isinf(f) ? 0.0f : n / (n - f);
                        float b = std::isinf(f) ? n : n * f / (f - n);
                        simd_float4x4 projection{};
                        projection.columns[0] = {y / effect.aspectRatio, 0, 0, 0};
                        projection.columns[1] = {0, y, 0, 0};
                        projection.columns[2] = {0, 0, a, 1};
                        projection.columns[3] = {0, 0, b, 0};
                        if (p.projection_matrix_present)
                            std::memcpy(&projection, p.view_to_clip, sizeof(projection));
                        effect.worldToViewMatrix = view;
                        effect.viewToClipMatrix = projection;
                    }
                }

                // Record the encode before MetalFX can report it to the HUD.
                if (!currentGeneration || !state.hudCounted) {
                    std::lock_guard lock(hudRow.mutex);
                    if (currentGeneration) {
                        state.hudCounted = true;
                        ++hudRow.interpolating;
                    } else {
                        // A command recorded before OFF or DESTROY re-adds the row.
                        scheduleHudRowRemoval();
                    }
                }
                // A stale replay can still advance the reused native interpolator.
                // Invalidate continuity before encoding, including partial failure.
                if (!currentGeneration && state.historyConfiguration == impl_->configuration)
                    state.historyConfiguration.reset();
                [(id<MTL4FXFrameInterpolator>)interpolator.get()
                    encodeToCommandBuffer:(id<MTL4CommandBuffer>)buffer];

                if (!computePass(buffer, (id<MTLFence>)fence, argumentTable, threadgroupWidth,
                                 (id<MTLComputePipelineState>)configuration.encodePipeline.get(),
                                 {.source = (id<MTLTexture>)output.get(),
                                  .destination = (id<MTLTexture>)outputView.get(),
                                  .auxiliary0 = (id<MTLTexture>)normalizedDepth.get(),
                                  .auxiliary1 = (id<MTLTexture>)normalizedMotion.get(),
                                  .ui = (id<MTLTexture>)uiSourceView.get(),
                                  .scene = (id<MTLTexture>)sceneView.get(),
                                  .present = (id<MTLTexture>)presentView.get()},
                                 parameterBuffer, offsetof(ParameterBlock, color), colorParams,
                                 execution->objects))
                    return fail("encode_output_scatter");
                if (impl_->disableInterpolation) {
                    id<MTL4ComputeCommandEncoder> encoder =
                        [(id<MTL4CommandBuffer>)buffer computeCommandEncoder];
                    if (!encoder) return fail("encode_disable_hint");
                    @try {
                        execution->objects.emplace_back(encoder);
                        [encoder waitForFence:(id<MTLFence>)fence
                             beforeEncoderStages:MTLStageBlit];
                        [encoder copyFromBuffer:parameterBuffer
                                   sourceOffset:offsetof(ParameterBlock, color) +
                                                offsetof(FgParams, disableHint)
                                      toBuffer:(id<MTLBuffer>)impl_->disableInterpolation.get()
                             destinationOffset:impl_->disableInterpolationOffset
                                          size:sizeof(std::uint32_t)];
                    } @finally {
                        @try {
                            [encoder updateFence:(id<MTLFence>)fence
                                afterEncoderStages:MTLStageBlit];
                        } @finally {
                            [encoder endEncoding];
                        }
                    }
                }
                // A queued command may encode after OFF (or after a subsequent ON).
                // Its output remains owned by the command, but must not seed new history.
                if (currentGeneration) {
                    state.history = execution->currentScene;
                    state.historyConfiguration = impl_->configuration;
                    state.historyDispatch = impl_->dispatch;
                    state.historyFrame = frame;
                }
                return true;
            }
            return fail("encode_os_unavailable");
        } @catch (NSException* exception) {
            const char* reason = [[exception reason] UTF8String];
            return fail(reason && *reason ? reason : "encode_objc_exception");
        }
    } catch (...) {
        return fail("encode_cpp_exception");
    }
}

bool initialize(const std::uint8_t* imageBase) noexcept {
    try {
        bool result = transport::initialize(imageBase);
        if (@available(macOS 26.0, *)) {
            result = result && NSClassFromString(@"MTLFXFrameInterpolatorDescriptor");
        } else {
            result = false;
        }
        ready.store(result, std::memory_order_release);
        return result;
    } catch (...) {
        ready.store(false, std::memory_order_release);
        return false;
    }
}

bool available() noexcept {
    return ready.load(std::memory_order_acquire);
}

std::uint32_t api(std::uint32_t operation, void* arguments) noexcept {
    if (!arguments) return Parameter;
    auto& header = *static_cast<yaagl_fsr_fg_packet_header*>(arguments);
    if (header.size < sizeof(header)) return Parameter;
    auto finish = [&](std::uint32_t result, const char* stage = "request",
                      std::uint32_t flags = 0) {
        header.result = result;
        if (result != Ok) {
            if (operation == YAAGL_FSR_FG_PREPARE &&
                header.size == sizeof(yaagl_fsr_fg_prepare_packet)) {
                const auto& p = *static_cast<const yaagl_fsr_fg_prepare_packet*>(arguments);
                logFailure(operation, stage, result, flags, p.render_width, p.render_height,
                           p.camera_near, p.camera_far, p.camera_fov_vertical_radians,
                           p.view_space_to_meters);
            } else {
                logFailure(operation, stage, result, flags);
            }
        }
        return result;
    };
    if (header.operation != operation ||
        header.version != YAAGL_FSR_FG_BRIDGE_VERSION)
        return finish(Parameter);
    try {
        @try {
            if (operation == YAAGL_FSR_FG_PRESENT) {
                if (header.size != sizeof(yaagl_fsr_fg_present_packet))
                    return finish(Parameter);
                const auto& p = *static_cast<const yaagl_fsr_fg_present_packet*>(arguments);
                if (!p.swapchain || !p.source || p.generated > 1 || p.only_generated > 1)
                    return finish(Parameter);
                async_pipeline::presentationHistory().mark(
                    {reinterpret_cast<void*>(p.swapchain), p.source, p.frame_id,
                     p.generated != 0, p.only_generated != 0});
                return finish(Ok);
            }
            if (operation == YAAGL_FSR_FG_DESTROY) {
                if (header.size != sizeof(yaagl_fsr_fg_destroy_packet))
                    return finish(Parameter);
                std::shared_ptr<State> state;
                {
                    std::lock_guard lock(registryMutex);
                    auto found = contexts.find(header.context);
                    if (found == contexts.end()) return finish(Parameter);
                    state = found->second;
                    contexts.erase(found);
                }
                std::lock_guard lock(state->mutex);
                state->retired = true;
                state->clearFrames();
                // Commands recorded before DESTROY may still encode; mark them stale.
                std::lock_guard executionLock(state->executionMutex);
                ++state->generationEpoch;
                state->scratch->clear();
                state->snapshotDepth->clear();
                state->snapshotMotion->clear();
                state->scene->clear();
                // Snapshots own queued configurations; retired caches/history are no longer needed.
                state->configurations.clear();
                state->kernels = {};
                state->history = {};
                state->historyConfiguration.reset();
                releaseHudRow(state->hudCounted);
                return finish(Ok);
            }
            if (operation == YAAGL_FSR_FG_RETIRE_FRAME) {
                if (header.size != sizeof(yaagl_fsr_fg_retire_frame_packet))
                    return finish(Parameter);
                auto state = lookup(header.context);
                if (!state) return finish(Parameter);
                std::lock_guard lock(state->mutex);
                if (state->retired) return finish(Parameter);
                auto& retire =
                    *static_cast<yaagl_fsr_fg_retire_frame_packet*>(arguments);
                state->eraseFrame(retire.frame_id);
                retire.pending_frame_count = state->frameCount;
                for (std::size_t i = 0; i < state->frameCount; ++i)
                    retire.pending_frame_ids[i] = state->frames[i].id;
                return finish(Ok);
            }
            if (operation == YAAGL_FSR_FG_CONFIGURE) {
                if (header.size != sizeof(yaagl_fsr_fg_configure_packet))
                    return finish(Parameter);
                auto state = lookup(header.context);
                if (!state) return finish(Parameter);
                std::lock_guard lock(state->mutex);
                if (state->retired) return finish(Parameter);
                auto& config = *static_cast<yaagl_fsr_fg_configure_packet*>(arguments);
                if (config.enabled > 1) return finish(Parameter);
                if (config.enabled && state->generationEnabled) return finish(Ok);
                std::lock_guard executionLock(state->executionMutex);
                state->generationEnabled = config.enabled != 0;
                if (!state->generationEnabled) {
                    releaseHudRow(state->hudCounted);
                    ++state->generationEpoch;
                    state->scratch->clear();
                    state->snapshotDepth->clear();
                    state->snapshotMotion->clear();
                    state->scene->clear();
                    state->clearFrames();
                    // OFF can follow every Prepare without a Generate. Keep the bounded
                    // interpolator cache; only temporal state and pending work are invalid.
                    state->hasPreparedJitter = false;
                    state->history = {};
                    state->historyConfiguration.reset();
                }
                return finish(Ok);
            }
            if (!available()) return finish(Unsupported);

            if (operation == YAAGL_FSR_FG_CREATE) {
                if (header.size != sizeof(yaagl_fsr_fg_create_packet))
                    return finish(Parameter);
                auto& p = *static_cast<yaagl_fsr_fg_create_packet*>(arguments);
                if (!p.display_width || !p.display_height ||
                    !p.max_render_width || !p.max_render_height ||
                    p.max_render_width > p.display_width ||
                    p.max_render_height > p.display_height ||
                    (p.flags & ~(YAAGL_FSR_FG_DEPTH_INVERTED |
                                 YAAGL_FSR_FG_DEPTH_INFINITE |
                                 YAAGL_FSR_FG_DISPLAY_RESOLUTION_MOTION_VECTORS |
                                 YAAGL_FSR_FG_MOTION_VECTORS_JITTERED)) ||
                    p.user_interface_recomposition > 1 ||
                    p.orthographic_projection > 1)
                    return finish(Parameter);
                if (colorFormat(p.backbuffer_format) == MTLPixelFormatInvalid)
                    return finish(Unsupported);
                auto state = std::make_shared<State>();
                state->device = owner(pointer(p.device), false);
                if (!state->device || !getLuid(state->device.get(), state->luid))
                    return finish(Parameter);
                state->creation = p;
                auto context = nextContext.fetch_add(1, std::memory_order_relaxed);
                {
                    std::lock_guard lock(registryMutex);
                    contexts.emplace(context, state);
                }
                header.context = context;
                return finish(Ok);
            }

            if (operation != YAAGL_FSR_FG_PREPARE &&
                operation != YAAGL_FSR_FG_DISPATCH)
                return finish(Unsupported);

            const bool generate = operation == YAAGL_FSR_FG_DISPATCH;
            if (header.size != (generate ? sizeof(yaagl_fsr_fg_dispatch_packet)
                                         : sizeof(yaagl_fsr_fg_prepare_packet)))
                return finish(Parameter);
            auto state = lookup(header.context);
            if (!state) return finish(Parameter);
            std::lock_guard lock(state->mutex);
            if (state->retired) return finish(Parameter);

            auto* p = generate ? nullptr
                : static_cast<yaagl_fsr_fg_prepare_packet*>(arguments);
            auto* d = generate
                ? static_cast<yaagl_fsr_fg_dispatch_packet*>(arguments) : nullptr;
            void* list = pointer(generate ? d->command_list : p->command_list);
            if (!matches(list, true, state->luid)) return finish(Parameter);
            Command command;
            if (!command.open(list)) return finish(Unsupported);
            if (!supported((id<MTLDevice>)command.value.device,
                           command.value.compiler))
                return finish(Unsupported);
            if (state->providerSelected &&
                state->metalDevice.get() != (id)command.value.device)
                return finish(Parameter);

            Resources resources;
            std::shared_ptr<PreparedFrame::Impl> implementation;
            std::array<std::uint64_t, 8> addresses{};
            std::array<std::uint32_t, 8> states{};
            unsigned resourceCount = 2;
            std::uint64_t frame = generate ? d->frame_id : p->frame_id;

            if (!generate) {
                CameraPlanes planes{};
                if (!normalizeCameraPlanes(*p, state->creation.flags, planes))
                    return finish(Parameter, "prepare_camera_planes", state->creation.flags);
                if (!validParameters(*p, state->creation.flags) ||
                    p->render_width > state->creation.max_render_width ||
                    p->render_height > state->creation.max_render_height)
                    return finish(Parameter, "prepare_parameters", state->creation.flags);
                if (state->findFrame(frame)) return finish(Parameter);
                // Prepare may continue while FG is off. Recorded work owns its snapshot,
                // but disabled presentation cannot consume frame-ID metadata.
                if (state->generationEnabled && state->frameCount >= state->frames.size())
                    return finish(Runtime, "prepare_backpressure");
                addresses = {p->depth, p->motion_vectors, 0};
                states = {p->depth_state, p->motion_vectors_state, 0};
                const bool displayMotion = (state->creation.flags &
                    YAAGL_FSR_FG_DISPLAY_RESOLUTION_MOTION_VECTORS) != 0;
                const NSUInteger motionWidth = p->motion_width
                    ? p->motion_width
                    : (displayMotion ? state->creation.display_width : p->render_width);
                const NSUInteger motionHeight = p->motion_height
                    ? p->motion_height
                    : (displayMotion ? state->creation.display_height : p->render_height);
                if (!addresses[0] || !addresses[1] || addresses[0] == addresses[1] ||
                    !mapOne(resources, 0, *state, command, addresses[0], false,
                            NSUInteger(p->depth_subrect_x) + p->render_width,
                            NSUInteger(p->depth_subrect_y) + p->render_height) ||
                    !mapOne(resources, 1, *state, command, addresses[1], false,
                            NSUInteger(p->motion_subrect_x) + motionWidth,
                            NSUInteger(p->motion_subrect_y) + motionHeight) ||
                    rootTexture((id<MTLTexture>)resources.values[0].texture) ==
                    rootTexture((id<MTLTexture>)resources.values[1].texture))
                    return finish(Unsupported);
                auto depth = (id<MTLTexture>)resources.values[0].texture;
                auto motion = (id<MTLTexture>)resources.values[1].texture;
                // The first Prepare selects the provider even before the pending
                // configuration is replayed. Validate its MetalFX eligibility,
                // but do not create or record snapshots while generation is off.
                if (!state->generationEnabled && state->providerSelected) return finish(Ok);
                // Prepare only copies snapshots. Reuse validated input requirements
                // instead of constructing another interpolator for each render size.
                const auto cached = std::find_if(state->configurations.begin(),
                    state->configurations.end(), [&](const auto& value) {
                        return value->device.get() == (id)command.value.device &&
                            value->depth == depth.pixelFormat;
                    });
                auto configuration = cached != state->configurations.end() ? *cached :
                    configure(*state, command, *p, depth,
                        MTLPixelFormatRGBA16Float, MTLPixelFormatInvalid,
                        p->render_width, p->render_height,
                        FFX_API_BACKBUFFER_TRANSFER_FUNCTION_SRGB, false);
                if (!configuration) return finish(Unsupported);
                if (!state->generationEnabled) {
                    // Retain the validated configuration for the first enabled Prepare.
                    state->providerSelected = true;
                    state->metalDevice = Object((id)command.value.device);
                    return finish(Ok);
                }
                implementation = std::make_shared<PreparedFrame::Impl>();
                auto snapshot = std::make_shared<Snapshot>();
                implementation->configuration = configuration;
                snapshot->parameters = *p;
                snapshot->depthWidth = p->render_width;
                snapshot->depthHeight = p->render_height;
                snapshot->motionWidth = motionWidth;
                snapshot->motionHeight = motionHeight;
                auto nativeInputs =
                    (id<MTLFXFrameInterpolatorBase>)configuration->factory.get();
                snapshot->depth = acquireTexture(
                    state->snapshotDepth, state->generationEnabled,
                    (id<MTLDevice>)configuration->device.get(), configuration->depth,
                    p->render_width, p->render_height,
                    nativeInputs.depthTextureUsage | MTLTextureUsageShaderRead);
                snapshot->motion = acquireTexture(
                    state->snapshotMotion, state->generationEnabled,
                    (id<MTLDevice>)configuration->device.get(), motion.pixelFormat,
                    motionWidth, motionHeight,
                    nativeInputs.motionTextureUsage | MTLTextureUsageShaderRead);
                if (!snapshot->depth->texture || !snapshot->motion->texture)
                    return finish(Unsupported);
                state->capturePreparedJitter(*snapshot, frame);
                implementation->snapshot = std::move(snapshot);
            } else {
                if ((d->flags & ~7u) || d->output_subrect_only > 1 ||
                    d->independent_subrects > 1 || d->distortion_low_precision > 1 ||
                    (d->distortion_low_precision &&
                     (!std::isfinite(d->distortion_scale) ||
                      !std::isfinite(d->distortion_bias))))
                    return finish(Parameter);
                if (d->num_generated_frames != 1) return finish(Unsupported);
                std::int32_t rectLeft = 0, rectTop = 0;
                NSUInteger rectWidth = 0, rectHeight = 0;
                if (!validTransfer(*d) ||
                    !resolveRect(*d, state->creation.display_width,
                                 state->creation.display_height, rectLeft, rectTop,
                                 rectWidth, rectHeight))
                    return finish(Parameter);
                auto found = state->findFrame(frame);
                if (!found) return finish(Parameter);
                implementation = std::make_shared<PreparedFrame::Impl>();
                implementation->kind = PreparedFrame::Impl::Kind::Generate;
                implementation->generationEpoch = state->generationEpoch;
                implementation->reset = d->reset != 0;
                implementation->dispatch = *d;
                auto& dispatch = implementation->dispatch;
                addresses[0] = d->present_color;
                addresses[1] = d->output;
                addresses[2] = d->hudless_color;
                addresses[3] = d->real_output;
                addresses[4] = d->ui;
                addresses[5] = d->ui_alpha;
                addresses[6] = d->distortion;
                addresses[7] = d->disable_interpolation;
                states[0] = d->present_color_state;
                states[1] = d->output_state;
                states[2] = d->hudless_color_state;
                states[3] = d->real_output_state;
                states[4] = d->ui_state;
                states[5] = d->ui_alpha_state;
                states[6] = d->distortion_state;
                states[7] = d->disable_interpolation_state;
                if (!d->independent_subrects) {
                    dispatch.present_x = dispatch.hudless_x = dispatch.output_x = dispatch.real_x = rectLeft;
                    dispatch.present_y = dispatch.hudless_y = dispatch.output_y = dispatch.real_y = rectTop;
                }
                if (d->ui && d->ui_alpha) return finish(Parameter);
                if ((state->creation.user_interface_recomposition ||
                     d->ui || d->ui_alpha) &&
                    (!d->hudless_color || (!d->ui && !d->ui_alpha)))
                    return finish(Parameter);
                if (std::uint64_t(dispatch.present_x) + rectWidth > state->creation.display_width ||
                    std::uint64_t(dispatch.present_y) + rectHeight > state->creation.display_height ||
                    std::uint64_t(dispatch.output_x) + rectWidth > state->creation.display_width ||
                    std::uint64_t(dispatch.output_y) + rectHeight > state->creation.display_height)
                    return finish(Parameter);
                if (d->disable_interpolation &&
                    !mapBuffer(resources, 7, *state, command,
                                d->disable_interpolation,
                                d->disable_interpolation_offset))
                    return finish(Parameter);
                if (d->disable_interpolation)
                    resourceCount = 8;
                if (!addresses[0] || !addresses[1] || addresses[0] == addresses[1] ||
                    !mapOne(resources, 0, *state, command, addresses[0], false,
                            state->creation.display_width, state->creation.display_height) ||
                    !mapOne(resources, 1, *state, command, addresses[1], true,
                            state->creation.display_width, state->creation.display_height))
                    return finish(Unsupported);
                auto present = (id<MTLTexture>)resources.values[0].texture;
                auto output = (id<MTLTexture>)resources.values[1].texture;
                const auto presentRoot = rootTexture(present);
                const auto outputRoot = rootTexture(output);
                if (presentRoot == outputRoot) return finish(Unsupported);
                if ((!d->output_subrect_only &&
                     (present.width != output.width || present.height != output.height)) ||
                    present.pixelFormat != output.pixelFormat ||
                    (d->output_subrect_only &&
                     (std::uint64_t(rectLeft) + rectWidth > present.width ||
                      std::uint64_t(rectTop) + rectHeight > present.height ||
                      std::uint64_t(dispatch.output_x) + rectWidth > output.width ||
                      std::uint64_t(dispatch.output_y) + rectHeight > output.height)))
                    return finish(Unsupported);
                id<MTLTexture> scene = present;
                id<MTLTexture> sceneRoot = presentRoot;
                id<MTLTexture> realOutputRoot = nil;
                MTLPixelFormat uiFormat = MTLPixelFormatInvalid;
                if (d->hudless_color) {
                    resourceCount = std::max(resourceCount, 3u);
                    if (!mapOne(resources, 2, *state, command, d->hudless_color, false,
                                std::uint64_t(dispatch.hudless_x) + rectWidth,
                                std::uint64_t(dispatch.hudless_y) + rectHeight))
                        return finish(Unsupported);
                    scene = (id<MTLTexture>)resources.values[2].texture;
                    sceneRoot = rootTexture(scene);
                    if (sceneRoot == outputRoot) return finish(Parameter);
                    if (!d->ui && !d->ui_alpha)
                        uiFormat = present.pixelFormat;
                }
                if (d->real_output) {
                    resourceCount = std::max(resourceCount, 4u);
                    if (!mapOne(resources, 3, *state, command, d->real_output, true,
                                d->output_subrect_only
                                    ? std::uint64_t(dispatch.real_x) + rectWidth
                                    : state->creation.display_width,
                                d->output_subrect_only
                                    ? std::uint64_t(dispatch.real_y) + rectHeight
                                    : state->creation.display_height))
                        return finish(Unsupported);
                    realOutputRoot = rootTexture((id<MTLTexture>)resources.values[3].texture);
                    if (realOutputRoot == outputRoot ||
                        realOutputRoot == presentRoot ||
                        realOutputRoot == sceneRoot ||
                        ((id<MTLTexture>)resources.values[3].texture).pixelFormat !=
                            present.pixelFormat ||
                        (!d->output_subrect_only &&
                         (((id<MTLTexture>)resources.values[3].texture).width != present.width ||
                          ((id<MTLTexture>)resources.values[3].texture).height != present.height)))
                        return finish(Parameter);
                }
                if (d->ui) {
                    resourceCount = std::max(resourceCount, 5u);
                    if (!mapOne(resources, 4, *state, command, d->ui, false,
                                std::uint64_t(d->ui_x) + rectWidth,
                                std::uint64_t(d->ui_y) + rectHeight))
                        return finish(Unsupported);
                    uiFormat = ((id<MTLTexture>)resources.values[4].texture).pixelFormat;
                    if (uiFormat != MTLPixelFormatRGBA8Unorm &&
                        uiFormat != MTLPixelFormatBGRA8Unorm &&
                        uiFormat != MTLPixelFormatRGBA16Float &&
                        uiFormat != MTLPixelFormatRGBA32Float)
                        return finish(Unsupported);
                }
                if (d->ui_alpha) {
                    resourceCount = std::max(resourceCount, 6u);
                    if (!mapOne(resources, 5, *state, command, d->ui_alpha, false,
                                std::uint64_t(d->ui_alpha_x) + rectWidth,
                                std::uint64_t(d->ui_alpha_y) + rectHeight))
                        return finish(Unsupported);
                    auto alphaFormat =
                        ((id<MTLTexture>)resources.values[5].texture).pixelFormat;
                    if (alphaFormat != MTLPixelFormatR8Unorm &&
                        alphaFormat != MTLPixelFormatR16Unorm &&
                        alphaFormat != MTLPixelFormatR16Float &&
                        alphaFormat != MTLPixelFormatR32Float)
                        return finish(Unsupported);
                }
                if (d->distortion) {
                    if (!d->distortion_width || !d->distortion_height)
                        return finish(Parameter);
                    resourceCount = std::max(resourceCount, 7u);
                    if (!mapOne(resources, 6, *state, command, d->distortion, false,
                                std::uint64_t(d->distortion_x) + d->distortion_width,
                                std::uint64_t(d->distortion_y) + d->distortion_height))
                        return finish(Unsupported);
                    auto distortion = (id<MTLTexture>)resources.values[6].texture;
                    const auto distortionRoot = rootTexture(distortion);
                    if (distortionRoot == outputRoot ||
                        (d->real_output && distortionRoot == realOutputRoot))
                        return finish(Parameter);
                    if (distortion.pixelFormat != MTLPixelFormatRGBA16Float &&
                        distortion.pixelFormat != MTLPixelFormatRGBA32Float &&
                        !(d->distortion_low_precision &&
                          (distortion.pixelFormat == MTLPixelFormatRGBA8Unorm ||
                           distortion.pixelFormat == MTLPixelFormatRGBA16Unorm ||
                           distortion.pixelFormat == MTLPixelFormatRGBA16Snorm)))
                        return finish(Unsupported);
                }
                for (unsigned i = 4; i != 6; ++i) {
                    const auto address = i == 4 ? d->ui : d->ui_alpha;
                    if (!address) continue;
                    auto texture = (id<MTLTexture>)resources.values[i].texture;
                    const auto textureRoot = rootTexture(texture);
                    if (textureRoot == outputRoot ||
                        (d->real_output && textureRoot == realOutputRoot))
                        return finish(Parameter);
                }
                implementation->configuration = configure(*state, command,
                    (*found)->parameters,
                    (id<MTLTexture>)(*found)->depth->texture.get(), scene.pixelFormat,
                    uiFormat, rectWidth, rectHeight, d->backbuffer_transfer_function,
                    d->distortion != 0);
                if (!implementation->configuration) return finish(Unsupported);
                implementation->snapshot = *found;
            }
            implementation->state = state;
            implementation->first = Object((id)resources.values[0].texture);
            implementation->second = Object((id)resources.values[1].texture);
            if (addresses[2])
                implementation->third = Object((id)resources.values[2].texture);
            if (addresses[3])
                implementation->fourth = Object((id)resources.values[3].texture);
            if (addresses[4])
                implementation->ui = Object((id)resources.values[4].texture);
            if (addresses[5])
                implementation->uiAlpha = Object((id)resources.values[5].texture);
            if (addresses[6])
                implementation->distortion = Object((id)resources.values[6].texture);
            if (addresses[7])
                implementation->disableInterpolation = Object(
                    (id)resources.values[7].buffer);
            if (addresses[7])
                implementation->disableInterpolationOffset =
                    resources.values[7].bufferOffset + d->disable_interpolation_offset;
            auto prepared = std::shared_ptr<const PreparedFrame>(
                new PreparedFrame(implementation));

            if (!record(list, command, resources, prepared, addresses, states,
                        resourceCount, generate)) {
                return finish(Runtime);
            }
            // Recording retains the snapshot itself. Publish its lookup metadata
            // only after every recording step has succeeded.
            if (!generate && state->generationEnabled)
                state->frames[state->frameCount++] = {frame, implementation->snapshot};
            if (!generate)
                state->commitPreparedJitter(*implementation->snapshot, frame);
            if (generate) state->eraseFrame(frame);
            state->providerSelected = true;
            state->metalDevice = Object((id)command.value.device);
            return finish(Ok);
        } @catch (NSException*) {
            return finish(Runtime);
        }
    } catch (const std::bad_alloc&) {
        return finish(Memory);
    } catch (...) {
        return finish(Runtime);
    }
}

} // namespace yaagl::pso::fsr::framegeneration

extern "C" __attribute__((visibility("default"))) std::uint32_t
yaagl_fsr_fg_api(std::uint32_t operation, void* arguments) noexcept {
    // Wine game threads have no autorelease pool; api() catches every
    // exception, so this pool is always drained on return.
    @autoreleasepool {
        return yaagl::pso::fsr::framegeneration::api(operation, arguments);
    }
}