#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <mutex>
#include <variant>
#include <type_traits>
#include <utility>
#include <vector>

namespace yaagl::pso::fsr::framegeneration {
class PreparedFrame;
class ExecutionLease;
}

namespace yaagl::pso::metalfx {
class PreparedFrame;
class ExecutionLease;
}

namespace yaagl::pso::d3dmetal {

enum class ReplayResult : std::uint8_t {
    NotRecorded,
    Succeeded,
    Failed,
};

enum class CommandListKind : std::uint8_t {
    unsupported = 0,
    mpl = 1,
    legacy = 2,
};

// Native MPL view descriptor.  Keep this layout exact: D3DMetal passes it in
// the last two integer argument registers to BeginUseTexture/EndUseTexture.
struct TextureView {
    std::uint16_t firstMip = 0;
    std::uint16_t mipCount = 1;
    std::uint16_t firstSlice = 0;
    std::uint16_t sliceCount = 1;
    std::uint8_t planes = 1;
    std::uint8_t padding[7]{};
};
static_assert(sizeof(TextureView) == 16);

// A +1 Objective-C reference to the exact Metal texture view returned by
// D3DMetal's own D3D12 resource bridge.  releaseResource() consumes it.
struct MetalResource {
    void* texture = nullptr;
    TextureView view{};
};

// RAII is explicit because this header is consumed by both C++ and ObjC++.
// qiOwner is the temporary private D3DMetal interface acquired while unwrapping
// the public ID3D12GraphicsCommandList.  releaseCommandList() balances it.
struct NativeCommandList {
    CommandListKind kind = CommandListKind::unsupported;
    void* wrapper = nullptr;
    void* list = nullptr;
    void* allocator = nullptr;
    // Borrowed from the active MPLContext. The independent backend retains
    // these when it creates a Feature; they are valid while this command list
    // transport is held.
    void* device = nullptr;
    void* compiler = nullptr;
    void* qiOwner = nullptr;
};

enum class ResourceAccess : std::uint8_t {
    read = 0,
    write = 1,
};

enum class ResourceMapResult : std::uint8_t {
    mapped,
    metadataUnavailable,
    notUnorderedAccess,
    mapFailed,
};

struct ResourceUse {
    const MetalResource* resource = nullptr;
    ResourceAccess access = ResourceAccess::read;
};

struct PreparedWork {
    using Variant = std::variant<
        std::monostate,
        std::shared_ptr<const metalfx::PreparedFrame>,
        std::shared_ptr<const fsr::framegeneration::PreparedFrame>>;

    Variant value{};

    PreparedWork() = default;
    PreparedWork(std::shared_ptr<const metalfx::PreparedFrame> frame) noexcept
        : value(std::move(frame)) {}
    PreparedWork(std::shared_ptr<const fsr::framegeneration::PreparedFrame> frame) noexcept
        : value(std::move(frame)) {}

    explicit operator bool() const noexcept {
        return std::visit([](const auto& frame) noexcept {
            using T = std::decay_t<decltype(frame)>;
            if constexpr (std::is_same_v<T, std::monostate>) return false;
            else return static_cast<bool>(frame);
        }, value);
    }
};

struct ExecutionSlot;

struct ExecutionLeaseStore {
    std::mutex mutex;
    std::vector<std::shared_ptr<ExecutionSlot>> slots;

    void retain(const std::shared_ptr<ExecutionSlot>& slot) {
        std::lock_guard<std::mutex> lock(mutex);
        slots.emplace_back(slot);
    }
    void retire(const ExecutionSlot* slot) noexcept {
        std::lock_guard<std::mutex> lock(mutex);
        std::erase_if(slots, [slot](const auto& value) { return value.get() == slot; });
    }
};

struct ExecutionSlot {
    using Lease = std::variant<
        std::shared_ptr<const metalfx::ExecutionLease>,
        std::shared_ptr<const fsr::framegeneration::ExecutionLease>>;
    Lease lease;
    std::mutex mutex;
    std::weak_ptr<ExecutionLeaseStore> owner;
    void* commandBuffer = nullptr;

    explicit ExecutionSlot(Lease initial, void* buffer,
                            const std::shared_ptr<ExecutionLeaseStore>& retention = {}) noexcept
        : lease(std::move(initial)), owner(retention), commandBuffer(buffer) {}
    void retire() noexcept {
        {
            std::lock_guard<std::mutex> lock(mutex);
            std::visit([](auto& value) { value.reset(); }, lease);
        }
        if (auto retention = owner.lock()) retention->retire(this);
    }
};

struct RecordRequest {
    PreparedWork prepared;
    const ResourceUse* resources = nullptr;
    std::size_t resourceCount = 0;
};

// Pins every private D3DMetal code/data contract used below.  Passing nullptr
// locates D3DMetal from MPLCreateContext in the current process.
bool initialize(const void* d3dmetalImageBase = nullptr) noexcept;

// Public DX12 command-list -> private MPL/legacy transport.  MPL returns the
// current IMPLCommandList and IMPLCommandAllocator.  Legacy is identified but
// intentionally has no custom replay transport in this implementation.
bool unwrapCommandList(void* d3d12CommandList, NativeCommandList& out) noexcept;
void releaseCommandList(NativeCommandList& commandList) noexcept;

// Use D3DMetal's native D3D12 resource bridge. Required write access is checked
// against the original D3D12 flags before extracting a Metal texture. The
// temporary native owner is balanced; a successful Metal view is retained.
ResourceMapResult mapResource(void* d3d12Resource, MetalResource& out,
                              ResourceAccess required) noexcept;
void releaseResource(MetalResource& resource) noexcept;

namespace detail {

// Keep the native-boundary operations substitutable without adding runtime
// dispatch to the resource hot path. The owner is acquired exactly once and
// released even if a boundary throws before completing an operation.
template<class Native>
ResourceMapResult mapResourceNative(void* resource, MetalResource& out,
                                    ResourceAccess required, Native& native) noexcept {
    if (out.texture) native.releaseMetal(out.texture);
    out = {};
    void* owner = nullptr;
    struct OwnerGuard {
        Native& native;
        void*& owner;
        ~OwnerGuard() { if (owner) native.releaseInternal(owner); }
    } guard{native, owner};
    ResourceMapResult failure = ResourceMapResult::metadataUnavailable;
    try {
        if (!resource || !native.acquire(resource, owner) || !owner) return failure;
        if (required == ResourceAccess::write && !(native.flags(owner) & 0x4u))
            return ResourceMapResult::notUnorderedAccess;
        failure = ResourceMapResult::mapFailed;
        void* texture = native.texture(owner);
        TextureView view{};
        if (!texture || !native.view(texture, view) || !native.retain(texture)) return failure;
        out.texture = texture;
        out.view = view;
        return ResourceMapResult::mapped;
    } catch (...) {
        return failure;
    }
}

} // namespace detail

// Records one immutable custom temporal command through MPL's normal compute
// scheduler.  The supplied prepared object is retained once and is released
// when the D3D12 command allocator can legally reset.
bool record(NativeCommandList& commandList, const RecordRequest& request) noexcept;

// Called first from the existing ReplayTemporalScaleMPL hook. Only NotRecorded
// may enter the native TemporalScale parser; recognized failures remain ours.
ReplayResult replay(void* mplReplayer, const void* command) noexcept;

// Called only by the pinned native Metal4 queue-commit callsite. Preserves the
// original Objective-C commit while retiring submitted execution slots at feedback.
void commitRecordedBatch(void* queue, void* selector, const void* const* buffers,
                         std::size_t count, void* options);

} // namespace yaagl::pso::d3dmetal
