#import "d3dmetal-transport.hpp"
#include "../include/yaagl_d3dmetal_native.hpp"
#import "metalfx-backend.hpp"
#import "fsr-framegeneration.hpp"

#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <objc/message.h>

#include <dlfcn.h>

#include <array>
#include <atomic>
#include <cstring>
#include <limits>
#include <mutex>
#include <optional>
#include <unordered_map>
#include <vector>

namespace yaagl::pso::d3dmetal {
namespace {

constexpr std::size_t kMaxResources = 8;
constexpr std::uint32_t kDispatchHeader = 0x0c000020u;
constexpr std::uint32_t kRecordedHeader = 0x3f000020u;
constexpr std::uint32_t kRecordedMagic = 0x32534459u; // "YDS2"
constexpr std::uint64_t kRecordedCookie = 0xd1557a9e46c20b31ULL;
// GPTK4 beta3 private fields, checked by image UUID before use. Native getters
// own all Objective-C wrapper layouts; these are only C++ command-stream fields.
constexpr native::CommandListLayout kCommandListLayout{0x78, 0x58, 0x18, 0x80};
constexpr std::size_t kD3DMOrderedEncode = 0x50;
constexpr std::size_t kMplComputeCursor = 0x2c38;
constexpr std::size_t kMplIndirectDispatchState = 0x3480;
// ReplayTemporalScale 0x417e0, REA ev_6a8e53680e95203420c7085e0b6b3a9c7eda6bb202677987daa87a1199195a08.
constexpr std::size_t kReplayerCommandBufferOwner = 0x498;
constexpr std::size_t kCommandBufferOwnerCommandBuffer = 0x58;
constexpr std::size_t kReplayerComputeEncoder = 0x4a0;
constexpr std::size_t kReplayerArgumentTable = 0x4a8;
constexpr std::size_t kReplayerFence = 0xb18;

struct RecordedCommand {
    std::uint32_t header;
    std::uint32_t magic;
    void* owner;
    std::uint64_t cookie;
    std::uint64_t reserved;
};
static_assert(sizeof(RecordedCommand) == 0x20);

struct Runtime {
    const std::uint8_t* image = nullptr;
    bool ready = false;
    native::D3DMetalApi api{};
};

Runtime gRuntime;
std::mutex gRuntimeLock;
std::atomic<bool> gReady{false};

template<class T>
T loadAt(const void* pointer, std::size_t offset = 0) noexcept {
    T value{};
    if (pointer)
        std::memcpy(&value, static_cast<const std::uint8_t*>(pointer) + offset, sizeof(value));
    return value;
}

template<class T>
void storeAt(void* pointer, std::size_t offset, const T& value) noexcept {
    if (pointer)
        std::memcpy(static_cast<std::uint8_t*>(pointer) + offset, &value, sizeof(value));
}

const std::uint8_t* locateImage() noexcept {
    void* symbol = dlsym(RTLD_DEFAULT, "MPLCreateContext");
    Dl_info info{};
    if (!symbol || !dladdr(symbol, &info) || !info.dli_fbase) return nullptr;
    return static_cast<const std::uint8_t*>(info.dli_fbase);
}

bool ensureInitialized() noexcept {
    if (gReady.load(std::memory_order_acquire)) return true;
    return initialize(nullptr);
}

void releasePrivateOwner(void* owner) noexcept {
    if (owner && gRuntime.api.releaseCommandList)
        gRuntime.api.releaseCommandList(&owner);
}

void releaseInternalTexture(void*& texture) noexcept {
    if (texture && gRuntime.api.releaseTexture)
        gRuntime.api.releaseTexture(&texture);
}

bool supportedPlaneView(id<MTLTexture> texture, TextureView& view) noexcept {
    if (!texture) return false;
    const NSUInteger format = texture.pixelFormat;
    // These are the exact depth/depth-stencil format cases already used by
    // D3DMetal's MPL resource tracker for temporal resources.
    if (format == 253 || format == 261 || format == 262) return false;
    view = {};
    if (!texture.mipmapLevelCount || texture.mipmapLevelCount > std::numeric_limits<std::uint16_t>::max() ||
        !texture.arrayLength || texture.arrayLength > std::numeric_limits<std::uint16_t>::max())
        return false;
    view.mipCount = static_cast<std::uint16_t>(texture.mipmapLevelCount);
    view.sliceCount = static_cast<std::uint16_t>(texture.arrayLength);
    if (format == 250 || format == 252 || format == 255 || format == 260)
        view.planes = 2;
    return true;
}

MTLPixelFormat temporalMotionFormat(MTLPixelFormat format) noexcept {
    switch (format) {
    case MTLPixelFormatRG16Float:
    case MTLPixelFormatRG32Float:
        return format;
    case MTLPixelFormatRG16Uint:
    case MTLPixelFormatRG16Sint:
    case MTLPixelFormatRG16Unorm:
    case MTLPixelFormatRG16Snorm:
        return MTLPixelFormatRG16Float;
    case MTLPixelFormatRG32Uint:
    case MTLPixelFormatRG32Sint:
        return MTLPixelFormatRG32Float;
    default:
        return MTLPixelFormatInvalid;
    }
}

struct MotionViewCacheEntry {
    id<MTLTexture> source = nil;
    id<MTLTexture> view = nil;
    MTLPixelFormat sourceFormat = MTLPixelFormatInvalid;
    MTLPixelFormat viewFormat = MTLPixelFormatInvalid;
    MTLTextureType textureType = MTLTextureType2D;
    TextureView range{};
};

std::mutex gMotionViewCacheMutex;
std::array<MotionViewCacheEntry, 32> gMotionViewCache{};
std::size_t gMotionViewCacheNext = 0;

bool sameMotionViewKey(const MotionViewCacheEntry& entry,
                       id<MTLTexture> source, MTLPixelFormat sourceFormat,
                       MTLPixelFormat viewFormat, MTLTextureType textureType,
                       const TextureView& range) noexcept {
    return entry.source == source && entry.sourceFormat == sourceFormat &&
           entry.viewFormat == viewFormat && entry.textureType == textureType &&
           std::memcmp(&entry.range, &range, sizeof(range)) == 0;
}

bool createNativeSrTextureView(void*& texture, TextureView& range,
                               MTLPixelFormat requestedFormat) noexcept {
    id<MTLTexture> source = reinterpret_cast<id<MTLTexture>>(texture);
    if (!source) return false;
    @try {
        if (source.textureType != MTLTextureType2D && source.textureType != MTLTextureType2DArray)
            return false;
        const MTLPixelFormat sourceFormat = source.pixelFormat;
        const MTLPixelFormat viewFormat = requestedFormat == MTLPixelFormatInvalid
            ? sourceFormat : requestedFormat;
        if (viewFormat == MTLPixelFormatInvalid) return false;
        if (!source.mipmapLevelCount || source.mipmapLevelCount > std::numeric_limits<std::uint16_t>::max())
            return false;
        TextureView viewRange = range;
        viewRange.firstMip = 0;
        viewRange.mipCount = static_cast<std::uint16_t>(source.mipmapLevelCount);
        viewRange.firstSlice = 0;
        viewRange.sliceCount = 1;
        if (source.textureType == MTLTextureType2D &&
            viewFormat == sourceFormat) {
            range = viewRange;
            return true;
        }

        std::lock_guard<std::mutex> lock(gMotionViewCacheMutex);
        for (const auto& entry : gMotionViewCache) {
            if (!entry.view || !sameMotionViewKey(entry, source, sourceFormat, viewFormat,
                                                  MTLTextureType2D, viewRange))
                continue;
            [entry.view retain];
            [source release];
            texture = entry.view;
            range = viewRange;
            return true;
        }

        id<MTLTexture> view = [source newTextureViewWithPixelFormat:viewFormat
                                                         textureType:MTLTextureType2D
                                                              levels:NSMakeRange(viewRange.firstMip, viewRange.mipCount)
                                                              slices:NSMakeRange(viewRange.firstSlice, viewRange.sliceCount)
                                                             swizzle:MTLTextureSwizzleChannelsDefault];
        if (!view) return false;

        auto& entry = gMotionViewCache[gMotionViewCacheNext];
        gMotionViewCacheNext = (gMotionViewCacheNext + 1) % gMotionViewCache.size();
        [entry.source release];
        [entry.view release];
        entry.source = [source retain];
        entry.view = [view retain];
        entry.sourceFormat = sourceFormat;
        entry.viewFormat = viewFormat;
        entry.textureType = MTLTextureType2D;
        entry.range = viewRange;
        texture = view;
        range = viewRange;
        [source release];
        return true;
    } @catch (id) {
        return false;
    }
}

bool normalizeTemporalMotionView(void*& texture, TextureView& range) noexcept {
    if (range.planes != 1) return false;
    const auto source = reinterpret_cast<id<MTLTexture>>(texture);
    if (!source) return false;
    const MTLPixelFormat viewFormat = temporalMotionFormat(source.pixelFormat);
    if (viewFormat == MTLPixelFormatInvalid) return false;
    return createNativeSrTextureView(texture, range, viewFormat);
}

void releaseMetalTexture(void* texture) noexcept {
    if (!texture) return;
    @try {
        [reinterpret_cast<id>(texture) release];
    } @catch (id) {
    }
}

// ID3D12Resource::GetDesc is a public Microsoft-ABI method. Do not read
// resource flags from a private object offset or mistake a buffer for a texture.
struct ResourceDesc {
    std::uint32_t dimension;
    std::uint64_t alignment, width;
    std::uint32_t height;
    std::uint16_t depthOrArraySize, mipLevels;
    std::uint32_t format, sampleCount, sampleQuality, layout, flags;
};
static_assert(sizeof(ResourceDesc) == 56);
static_assert(offsetof(ResourceDesc, flags) == 48);

struct NativeResourceBridge {
    ResourceDesc desc{};
    bool isBuffer = false;

    bool acquire(void* resource, void*& owner) noexcept {
        @try {
            using GetDesc = void (__attribute__((ms_abi)) *)(void*, ResourceDesc*);
            auto table = *static_cast<void***>(resource);
            reinterpret_cast<GetDesc>(table[10])(resource, &desc);
            isBuffer = desc.dimension == 1;
            if (isBuffer)
                owner = gRuntime.api.acquirePrivateInterface(resource, gRuntime.api.bufferIid);
            else
                gRuntime.api.getInternalResource(&owner, resource);
            return owner != nullptr;
        } @catch (id) { return false; }
    }
    std::uint32_t flags(void*) const noexcept { return desc.flags; }
    void* texture(void* owner) const noexcept {
        if (isBuffer) return nullptr;
        @try { return gRuntime.api.getMetalTexture(owner); }
        @catch (id) { return nullptr; }
    }
    void* buffer(void* owner, std::size_t& offset) const noexcept {
        if (!isBuffer || !owner) return nullptr;
        @try {
            // Native GetCurrentEntry supplies the current MTLBuffer, including
            // renamed resources.
            auto entry = gRuntime.api.getBufferEntry(owner);
            auto buffer = reinterpret_cast<id<MTLBuffer>>(loadAt<void*>(entry));
            offset = 0;
            return buffer && buffer.length >= sizeof(std::uint32_t) ? buffer : nullptr;
        } @catch (id) { return nullptr; }
    }
    bool view(void* texture, TextureView& out) const noexcept {
        @try { return supportedPlaneView(reinterpret_cast<id<MTLTexture>>(texture), out); }
        @catch (id) { return false; }
    }
    bool retain(void* object) const noexcept {
        @try { [reinterpret_cast<id>(object) retain]; return true; }
        @catch (id) { return false; }
    }
    bool retainBuffer(void* object) const noexcept { return retain(object); }
    void releaseInternal(void* owner) const noexcept {
        if (isBuffer) native::D3DMetalApi::releasePrivateInterface(owner);
        else releaseInternalTexture(owner);
    }
    void releaseMetal(void* object) const noexcept { releaseMetalTexture(object); }
    void releaseBuffer(void* object) const noexcept { releaseMetalTexture(object); }
};

struct UseEntry {
    id<MTLTexture> texture = nil;
    id<MTLBuffer> buffer = nil;
    std::size_t bufferOffset = 0;
    TextureView view{};
    ResourceAccess access = ResourceAccess::read;
};

bool sameView(const TextureView& a, const TextureView& b) noexcept {
    return std::memcmp(&a, &b, sizeof(a)) == 0;
}

bool prepareUses(const RecordRequest& request,
                 std::array<UseEntry, kMaxResources>& uses,
                 std::size_t& count) noexcept {
    count = 0;
    if (request.resourceCount > kMaxResources) return false;
    if (request.resourceCount && !request.resources) return false;
    for (std::size_t i = 0; i < request.resourceCount; ++i) {
        const ResourceUse& source = request.resources[i];
        if (!source.resource || (!source.resource->texture && !source.resource->buffer)) return false;
        if (source.resource->buffer) {
            id<MTLBuffer> buffer = reinterpret_cast<id<MTLBuffer>>(source.resource->buffer);
            bool duplicate = false;
            for (std::size_t j = 0; j < count; ++j) {
                if (uses[j].buffer != buffer || uses[j].bufferOffset != source.resource->bufferOffset) continue;
                if (uses[j].access != source.access) return false;
                duplicate = true;
                break;
            }
            if (!duplicate)
                uses[count++] = {nil, buffer, source.resource->bufferOffset, {}, source.access};
            continue;
        }
        id<MTLTexture> texture = reinterpret_cast<id<MTLTexture>>(source.resource->texture);
        bool duplicate = false;
        for (std::size_t j = 0; j < count; ++j) {
            if (uses[j].texture != texture || !sameView(uses[j].view, source.resource->view)) continue;
            if (uses[j].access != source.access) return false;
            duplicate = true;
            break;
        }
        if (duplicate) continue;
        uses[count++] = {texture, nil, 0, source.resource->view, source.access};
    }
    return true;
}

template<class PreparedType>
using ExecutionLeaseFor = std::conditional_t<
    std::is_same_v<PreparedType, std::shared_ptr<const metalfx::PreparedFrame>>,
    std::shared_ptr<const metalfx::ExecutionLease>,
    std::shared_ptr<const fsr::framegeneration::ExecutionLease>>;

struct ExecutionSlot;

struct ExecutionLeaseStore {
    std::mutex mutex;
    std::vector<std::shared_ptr<ExecutionSlot>> slots;

    void retain(const std::shared_ptr<ExecutionSlot>& slot) {
        std::lock_guard lock(mutex);
        slots.push_back(slot);
    }
    void retire(const ExecutionSlot* slot) noexcept {
        std::lock_guard lock(mutex);
        std::erase_if(slots, [slot](const auto& value) { return value.get() == slot; });
    }
};

struct ExecutionSlot {
    using Lease = std::variant<std::shared_ptr<const metalfx::ExecutionLease>,
                               std::shared_ptr<const fsr::framegeneration::ExecutionLease>>;
    Lease lease;
    std::mutex mutex;
    std::weak_ptr<ExecutionLeaseStore> owner;
    void* commandBuffer;

    ExecutionSlot(Lease initial, const std::shared_ptr<ExecutionLeaseStore>& store,
                  void* buffer) noexcept
        : lease(std::move(initial)), owner(store), commandBuffer(buffer) {}

    void retire() noexcept {
        {
            std::lock_guard lock(mutex);
            std::visit([](auto& value) { value.reset(); }, lease);
        }
        if (auto store = owner.lock()) store->retire(this);
    }
};

std::mutex gPendingLock;
std::unordered_map<void*, std::vector<std::weak_ptr<ExecutionSlot>>> gPending;
std::atomic<bool> gPendingNonempty{false};

void unregisterSlot(const ExecutionSlot* slot) noexcept {
    std::lock_guard lock(gPendingLock);
    const auto found = gPending.find(slot->commandBuffer);
    if (found == gPending.end()) return;
    std::erase_if(found->second, [slot](const auto& weak) {
        const auto value = weak.lock();
        return !value || value.get() == slot;
    });
    if (found->second.empty()) gPending.erase(found);
    gPendingNonempty.store(!gPending.empty(), std::memory_order_release);
}

std::shared_ptr<ExecutionSlot> reserveExecutionSlot(
    const std::shared_ptr<ExecutionLeaseStore>& owner, void* buffer, ExecutionSlot::Lease initial) {
    auto slot = std::make_shared<ExecutionSlot>(std::move(initial), owner, buffer);
    owner->retain(slot);
    try {
        std::lock_guard lock(gPendingLock);
        gPending[buffer].emplace_back(slot);
        gPendingNonempty.store(true, std::memory_order_release);
    } catch (...) {
        unregisterSlot(slot.get());
        owner->retire(slot.get());
        throw;
    }
    return slot;
}

void retireCompletedBuffer(void* buffer) noexcept {
    std::vector<std::weak_ptr<ExecutionSlot>> completed;
    {
        std::lock_guard lock(gPendingLock);
        const auto found = gPending.find(buffer);
        if (found == gPending.end()) return;
        completed = std::move(found->second);
        gPending.erase(found);
        gPendingNonempty.store(!gPending.empty(), std::memory_order_release);
    }
    // Destructors return Metal resources to their pools. Never run them while
    // holding the registry lock, or retain a raw recorded-owner pointer.
    for (const auto& weak : completed)
        if (auto slot = weak.lock()) slot->retire();
}

using CompleteExecute = void (*)(void*);
std::atomic<CompleteExecute> gCompleteExecute;

void completeExecute(void* completion) {
    if (gPendingNonempty.load(std::memory_order_acquire)) {
        // beta3 DoExecute::$_2::operator() at 0x16f0d0 is invoked by GPU
        // feedback. Its capture contains worker +0 and ExecuteCommand +0x10.
        // EncodeExecute begins/re-encodes each submission's buffer; the
        // original completion returns it to the pool only after this hook.
        const auto worker = loadAt<void*>(completion);
        if (loadAt<std::uint8_t>(worker, 0x1a) == 1) {
            const auto command = loadAt<void*>(completion, 0x10);
            @autoreleasepool {
                retireCompletedBuffer(loadAt<void*>(command, 0xa8));
            }
        }
    }
    gCompleteExecute.load(std::memory_order_acquire)(completion);
}

struct OwnerState {
    PreparedWork prepared;
    // Recorded inputs remain allocator-owned. Completed executions retire
    // independently; unsubmitted or partially encoded work keeps this fallback.
    std::shared_ptr<ExecutionLeaseStore> leases = std::make_shared<ExecutionLeaseStore>();
    std::mutex leaseLock;
    std::array<id, kMaxResources> textures{};
    std::array<id, kMaxResources> buffers{};
    std::size_t textureCount = 0;

    OwnerState(PreparedWork frame,
               const std::array<UseEntry, kMaxResources>& uses,
               std::size_t count)
        : prepared(std::move(frame)) {
        @try {
            for (std::size_t i = 0; i < count; ++i) {
                if (uses[i].texture) {
                    textures[textureCount++] = [uses[i].texture retain];
                } else {
                    buffers[textureCount] = [uses[i].buffer retain];
                    ++textureCount;
                }
            }
        } @catch (id exception) {
            for (std::size_t i = 0; i < textureCount; ++i)
                textures[i] ? [textures[i] release] : [buffers[i] release];
            @throw exception;
        }
    }

    ~OwnerState() {
        {
            std::lock_guard lock(leases->mutex);
            for (const auto& slot : leases->slots) unregisterSlot(slot.get());
        }
        for (std::size_t i = 0; i < textureCount; ++i)
            (textures[i] ? [textures[i] release] : [buffers[i] release]);
    }

    bool encode(void* commandBuffer, void* fence) noexcept {
        if (!prepared) return false;
        metalfx::Error error{};
        bool encoded = false;
        try {
            std::lock_guard<std::mutex> lock(leaseLock);
            // Reserve and register the slot before the backend can append GPU work.
            // PreparedFrame publishes its ExecutionLease before any command;
            // keeping it through a false return preserves partially encoded
            // resource ownership as well.

            encoded = std::visit([&](const auto& frame) -> bool {
                using T = std::decay_t<decltype(frame)>;
                if constexpr (std::is_same_v<T, std::monostate>) return false;
                else {
                    using FrameLease = ExecutionLeaseFor<T>;
                    auto slot = reserveExecutionSlot(
                        leases, commandBuffer, ExecutionSlot::Lease{FrameLease{}});
                    std::lock_guard slotLock(slot->mutex);
                    FrameLease& lease = std::get<FrameLease>(slot->lease);
                    bool result;
                    if constexpr (std::is_same_v<T, std::shared_ptr<const metalfx::PreparedFrame>>)
                        result = frame && frame->encode(commandBuffer, fence, lease, &error);
                    else
                        result = frame && frame->encode(commandBuffer, fence, lease);
                    return result && static_cast<bool>(lease);
                }
            }, prepared.value);
        } catch (...) {
            return false;
        }
        return encoded;
    }
};

} // namespace
} // namespace yaagl::pso::d3dmetal

@interface YAAGLMetalFXRecordedOwner : NSObject {
@public
    std::optional<yaagl::pso::d3dmetal::OwnerState> _state;
}
@end

@implementation YAAGLMetalFXRecordedOwner
@end

namespace yaagl::pso::d3dmetal {

std::uintptr_t initializeCompletionHook(std::uintptr_t original) noexcept {
    if (!original) return 0;
    gCompleteExecute.store(reinterpret_cast<CompleteExecute>(original), std::memory_order_release);
    return reinterpret_cast<std::uintptr_t>(&completeExecute);
}

bool initialize(const void* d3dmetalImageBase) noexcept {
    std::lock_guard<std::mutex> lock(gRuntimeLock);
    const auto* image = d3dmetalImageBase
        ? static_cast<const std::uint8_t*>(d3dmetalImageBase) : locateImage();
    if (gRuntime.ready) return image == gRuntime.image;
    if (!image) return false;
    try {
        native::D3DMetalApi api;
        if (!api.load(reinterpret_cast<const mach_header_64*>(image))) return false;
        gRuntime.image = image;
        gRuntime.api = api;
        gRuntime.ready = true;
        gReady.store(true, std::memory_order_release);
        return true;
    } catch (...) {
        return false;
    }
}

bool unwrapCommandList(void* d3d12CommandList, NativeCommandList& out) noexcept {
    releaseCommandList(out);
    if (!d3d12CommandList || !ensureInitialized()) return false;
    void* owner = nullptr;
    @try {
        owner = gRuntime.api.acquireCommandList(d3d12CommandList);
        if (!owner) return false;
        void* mpl = gRuntime.api.castCommandList(owner);
        if (!mpl) { releasePrivateOwner(owner); return false; }
        native::MplState state;
        void* active = nullptr;
        const char* reason = nullptr;
        if (!native::describeWrappedMpl(mpl, kCommandListLayout, state, active, &reason)) {
            releasePrivateOwner(owner);
            return false;
        }
        out.kind = CommandListKind::mpl;
        out.wrapper = mpl;
        out.list = state.list;
        out.context = state.context;
        out.allocator = state.allocator;
        out.device = state.device;
        out.compiler = state.compiler;
        out.qiOwner = owner;
        return true;
    } @catch (id) {
        releasePrivateOwner(owner);
        return false;
    }
}

void releaseCommandList(NativeCommandList& commandList) noexcept {
    if (commandList.qiOwner)
        releasePrivateOwner(commandList.qiOwner);
    commandList = {};
}

bool normalizeTemporalMotionTexture(void*& texture, TextureView& view) noexcept {
    return normalizeTemporalMotionView(texture, view);
}

bool texturesAlias(const MetalResource& left, const MetalResource& right) noexcept {
    if (!left.texture || !right.texture) return false;
    @try {
        id<MTLTexture> leftTexture = reinterpret_cast<id<MTLTexture>>(left.texture);
        id<MTLTexture> rightTexture = reinterpret_cast<id<MTLTexture>>(right.texture);
        for (unsigned i = 0; i != 32 && leftTexture.parentTexture; ++i)
            leftTexture = leftTexture.parentTexture;
        for (unsigned i = 0; i != 32 && rightTexture.parentTexture; ++i)
            rightTexture = rightTexture.parentTexture;
        if (leftTexture != rightTexture) return false;
        const auto overlaps = [](std::uint16_t first, std::uint16_t count,
                                 std::uint16_t otherFirst, std::uint16_t otherCount) noexcept {
            const auto end = static_cast<std::uint32_t>(first) + count;
            const auto otherEnd = static_cast<std::uint32_t>(otherFirst) + otherCount;
            return count && otherCount && first < otherEnd && otherFirst < end;
        };
        return overlaps(left.view.firstMip, left.view.mipCount,
                        right.view.firstMip, right.view.mipCount) &&
               overlaps(left.view.firstSlice, left.view.sliceCount,
                        right.view.firstSlice, right.view.sliceCount);
    } @catch (id) {
        return left.texture == right.texture;
    }
}

ResourceMapResult mapResource(void* d3d12Resource, MetalResource& out,
                              ResourceAccess required, ResourceViewPolicy policy) noexcept {
    if (!d3d12Resource || !ensureInitialized()) {
        releaseResource(out);
        return ResourceMapResult::metadataUnavailable;
    }
    NativeResourceBridge native;
    const auto result = detail::mapResourceNative(d3d12Resource, out, required, native);
    if (result != ResourceMapResult::mapped || !out.texture ||
        policy == ResourceViewPolicy::preserve)
        return result;
    if (policy == ResourceViewPolicy::native_sr &&
        createNativeSrTextureView(out.texture, out.view, MTLPixelFormatInvalid))
        return ResourceMapResult::mapped;
    if (policy == ResourceViewPolicy::normalize_temporal_motion &&
        normalizeTemporalMotionView(out.texture, out.view))
        return ResourceMapResult::mapped;
    releaseResource(out);
    return ResourceMapResult::mapFailed;
}

void releaseResource(MetalResource& resource) noexcept {
    releaseMetalTexture(resource.texture);
    releaseMetalTexture(resource.buffer);
    resource = {};
}

bool record(NativeCommandList& commandList, const RecordRequest& request) noexcept {
    if (!ensureInitialized() || commandList.kind != CommandListKind::mpl ||
        !commandList.wrapper || !commandList.list || !commandList.allocator || !request.prepared)
        return false;

    void* active = nullptr;
    native::MplState state;
    if (!native::describeWrappedMpl(commandList.wrapper, kCommandListLayout, state, active) ||
        state.list != commandList.list || state.allocator != commandList.allocator ||
        state.context != commandList.context || state.device != commandList.device ||
        state.compiler != commandList.compiler ||
        loadAt<void*>(commandList.list, kMplIndirectDispatchState) != nullptr)
        return false;

    std::array<UseEntry, kMaxResources> uses{};
    std::size_t useCount = 0;
    if (!prepareUses(request, uses, useCount)) return false;

    const auto beginUse = gRuntime.api.beginUseTexture;
    const auto endUse = gRuntime.api.endUseTexture;
    const auto dispatch = gRuntime.api.dispatch;
    const auto endEncoder = gRuntime.api.endEncoder;
    const auto extendLifetime = gRuntime.api.extendResourceLifetime;

    YAAGLMetalFXRecordedOwner* owner = nil;
    std::size_t begun = 0;
    bool transferred = false;
    bool success = false;

    try {
        @try {
            owner = [[YAAGLMetalFXRecordedOwner alloc] init];
            if (!owner) return false;
            owner->_state.emplace(request.prepared, uses, useCount);

            // Transfer the +1 from alloc/init to the native command allocator.
            // ExtendResourceLifetime itself does not retain; allocator Reset is
            // the matching objc_release, proven by beta3 Reset at 0x43cfe.
            extendLifetime(commandList.allocator, owner);
            transferred = true;

            for (; begun < useCount; ++begun) {
                if (uses[begun].buffer) {
                    gRuntime.api.beginUseBuffer(
                        commandList.list, reinterpret_cast<void*>(uses[begun].buffer),
                        4u, uses[begun].access == ResourceAccess::write ? 1u : 0u);
                } else {
                    beginUse(commandList.list, reinterpret_cast<void*>(uses[begun].texture), 4u,
                             uses[begun].access == ResourceAccess::write ? 1u : 0u,
                             uses[begun].view);
                }
            }

            dispatch(commandList.list, MTLSizeMake(1, 1, 1));
            std::uint8_t* after = loadAt<std::uint8_t*>(commandList.list, kMplComputeCursor);
            if (!after)
                @throw [NSException exceptionWithName:@"YAAGLMetalFXRecordContract"
                                               reason:@"MPL dispatch produced no compute cursor"
                                             userInfo:nil];
            auto* slot = reinterpret_cast<RecordedCommand*>(after - sizeof(RecordedCommand));
            if (loadAt<std::uint32_t>(slot) != kDispatchHeader)
                @throw [NSException exceptionWithName:@"YAAGLMetalFXRecordContract"
                                               reason:@"MPL dispatch tail contract changed"
                                             userInfo:nil];

            const auto ownerBits = static_cast<std::uint64_t>(reinterpret_cast<std::uintptr_t>(owner));
            const RecordedCommand custom{
                kRecordedHeader,
                kRecordedMagic,
                owner,
                kRecordedCookie ^ ownerBits,
                0,
            };
            std::memcpy(slot, &custom, sizeof(custom));
            // Type 1 is the compute stream.  This emits the exact range end
            // marker used by native TemporalScale and dirties state for the
            // following D3D12 compute work, preserving root-state semantics.
            endEncoder(commandList.list, 1u);
            for (std::size_t i = useCount; i != 0; --i) {
                const UseEntry& use = uses[i - 1];
                if (use.buffer) {
                    gRuntime.api.endUseBuffer(
                        commandList.list, reinterpret_cast<void*>(use.buffer), 4u,
                        use.access == ResourceAccess::write ? 1u : 0u);
                } else {
                    endUse(commandList.list, reinterpret_cast<void*>(use.texture), 4u,
                           use.access == ResourceAccess::write ? 1u : 0u, use.view);
                }
            }
            begun = 0;
            // Mirror the native temporal path's list-local submission policy. This does not
            // wait on the GPU, disable parallel recording globally, or order by
            // Evaluate ID: D3DMetal's native queue keeps actual submission order.
            storeAt<std::uint8_t>(active, kD3DMOrderedEncode, 1u);
            success = true;
        } @catch (id) {
            success = false;
        }
    } catch (...) {
        success = false;
    }
    if (!success && begun) {
        @try {
            while (begun) {
                const UseEntry& use = uses[--begun];
                if (use.buffer) {
                    gRuntime.api.endUseBuffer(
                        commandList.list, reinterpret_cast<void*>(use.buffer), 4u,
                        use.access == ResourceAccess::write ? 1u : 0u);
                } else {
                    endUse(commandList.list, reinterpret_cast<void*>(use.texture), 4u,
                           use.access == ResourceAccess::write ? 1u : 0u, use.view);
                }
            }
        } @catch (id) {
        }
    }
    if (owner && !transferred)
        [owner release];
    return success;
}

ReplayResult replay(void* mplReplayer, const void* command) noexcept {
    if (!command || loadAt<std::uint32_t>(command) != kRecordedHeader)
        return ReplayResult::NotRecorded;
    const RecordedCommand value = loadAt<RecordedCommand>(command);
    if (value.magic != kRecordedMagic || !value.owner) {
        return ReplayResult::NotRecorded;
    }
    const auto ownerBits = static_cast<std::uint64_t>(reinterpret_cast<std::uintptr_t>(value.owner));
    if (value.cookie != (kRecordedCookie ^ ownerBits) || value.reserved != 0) {
        return ReplayResult::NotRecorded;
    }
    if (!mplReplayer || !ensureInitialized()) {
        return ReplayResult::Failed;
    }
    auto* owner = reinterpret_cast<YAAGLMetalFXRecordedOwner*>(value.owner);
    auto* state = owner->_state ? &*owner->_state : nullptr;
    void* commandBufferOwner = loadAt<void*>(mplReplayer, kReplayerCommandBufferOwner);
    void* commandBuffer = loadAt<void*>(
        commandBufferOwner, kCommandBufferOwnerCommandBuffer);
    void* encoder = loadAt<void*>(mplReplayer, kReplayerComputeEncoder);
    void* argumentTable = loadAt<void*>(mplReplayer, kReplayerArgumentTable);
    void* fence = loadAt<void*>(mplReplayer, kReplayerFence);
    if (!commandBuffer || !encoder || !argumentTable || !fence || !state) {
        return ReplayResult::Failed;
    }

    void* updateSelector = reinterpret_cast<void*>(
        @selector(updateFence:afterEncoderStages:));
    void* waitSelector = reinterpret_cast<void*>(
        @selector(waitForFence:beforeEncoderStages:));
    void* endSelector = reinterpret_cast<void*>(@selector(endEncoding));
    void* computeSelector = reinterpret_cast<void*>(@selector(computeCommandEncoder));
    void* tableSelector = reinterpret_cast<void*>(@selector(setArgumentTable:));

    std::uint64_t computeStage;
    if (@available(macOS 26.0, *)) computeStage = MTLStageDispatch;
    else return ReplayResult::Failed;
    using FenceBoundary = void (*)(void*, void*, void*, std::uint64_t);
    using MsgVoid = void (*)(void*, void*);
    using MsgObject = void* (*)(void*, void*);
    using MsgSetObject = void (*)(void*, void*, void*);
    const auto updateFence = reinterpret_cast<FenceBoundary>(objc_msgSend);
    const auto waitFence = reinterpret_cast<FenceBoundary>(objc_msgSend);
    const auto sendVoid = reinterpret_cast<MsgVoid>(objc_msgSend);
    const auto sendObject = reinterpret_cast<MsgObject>(objc_msgSend);
    const auto sendSetObject = reinterpret_cast<MsgSetObject>(objc_msgSend);

    bool encoded = false;
    void* replacement = nullptr;
    @try {
        // The native compute stream is live when opcode 0x3f is replayed. Hand
        // its writes to the exact fence, end that encoder, then let the backend
        // append MetalFX work directly to the same MTL4CommandBuffer.
        updateFence(encoder, updateSelector, fence, computeStage);
        sendVoid(encoder, endSelector);
        storeAt<void*>(mplReplayer, kReplayerComputeEncoder, nullptr);

        encoded = state->encode(commandBuffer, fence);

        // Re-enter the ordinary MPL compute stream exactly as ComputeEncoder::
        // Begin does: create a fresh encoder, restore its shared argument table,
        // then wait on the fence MetalFX publishes before subsequent D3D work.
        replacement = sendObject(commandBuffer, computeSelector);
        if (!replacement) {
            return ReplayResult::Failed;
        }
        storeAt<void*>(mplReplayer, kReplayerComputeEncoder, replacement);
        sendSetObject(replacement, tableSelector, argumentTable);
        waitFence(replacement, waitSelector, fence, computeStage);
        return encoded ? ReplayResult::Succeeded : ReplayResult::Failed;
    } @catch (id) {
        // If the backend or an Objective-C boundary raised after the old encoder
        // was ended, make one best-effort attempt to restore an MPL compute
        // encoder so ReplayEncoders can continue to its range-end command.
        if (!replacement) {
            @try {
                replacement = sendObject(commandBuffer, computeSelector);
                if (replacement) {
                    storeAt<void*>(mplReplayer, kReplayerComputeEncoder, replacement);
                    sendSetObject(replacement, tableSelector, argumentTable);
                    waitFence(replacement, waitSelector, fence, computeStage);
                }
            } @catch (id) {
            }
        }
        return ReplayResult::Failed;
    }
}

} // namespace yaagl::pso::d3dmetal
