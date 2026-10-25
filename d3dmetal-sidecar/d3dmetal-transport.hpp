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
#include "../include/yaagl_d3dmetal_abi.hpp"

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
};

// Native MPL view descriptor.  Keep this layout exact: D3DMetal passes it in
// the last two integer argument registers to BeginUseTexture/EndUseTexture.
using TextureView = ::yaagl::native::TextureView;
static_assert(sizeof(TextureView) == 16);

// A +1 Objective-C reference to the exact Metal texture view returned by
// D3DMetal's own D3D12 resource bridge.  releaseResource() consumes it.
struct MetalResource {
    void* texture = nullptr;
    void* buffer = nullptr;
    std::size_t bufferOffset = 0;
    TextureView view{};
};

// RAII is explicit because this header is consumed by both C++ and ObjC++.
// qiOwner is the temporary private D3DMetal interface acquired while unwrapping
// the public ID3D12GraphicsCommandList.  releaseCommandList() balances it.
struct NativeCommandList {
    CommandListKind kind = CommandListKind::unsupported;
    void* wrapper = nullptr;
    void* list = nullptr;
    void* context = nullptr;
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

enum class ResourceViewPolicy : std::uint8_t {
    preserve = 0,
    normalize_temporal_motion = 1,
    native_sr = 2,
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

struct RecordRequest {
    PreparedWork prepared;
    const ResourceUse* resources = nullptr;
    std::size_t resourceCount = 0;
};

// Pins every private D3DMetal code/data contract used below.  Passing nullptr
// locates D3DMetal from MPLCreateContext in the current process.
bool initialize(const void* d3dmetalImageBase = nullptr) noexcept;

// Wrap the pinned beta3 GPU-completion handler before it signals fences or
// returns the submitted Metal4 command buffer to the native pool.
std::uintptr_t initializeCompletionHook(std::uintptr_t original) noexcept;

// Public DX12 wrapper -> the current beta3 MPL list and allocator via native
// private-interface RTTI and MDE getters. Non-MPL command lists are rejected.
bool unwrapCommandList(void* d3d12CommandList, NativeCommandList& out) noexcept;
void releaseCommandList(NativeCommandList& commandList) noexcept;

// Use D3DMetal's native D3D12 resource bridge. Required write access is checked
// against the original D3D12 flags before extracting a Metal texture. The
// temporary native owner is balanced; a successful Metal view is retained.
ResourceMapResult mapResource(void* d3d12Resource, MetalResource& out,
                              ResourceAccess required,
                              ResourceViewPolicy policy = ResourceViewPolicy::preserve) noexcept;
bool normalizeTemporalMotionTexture(void*& texture, TextureView& view) noexcept;
bool texturesAlias(const MetalResource& left, const MetalResource& right) noexcept;
void releaseResource(MetalResource& resource) noexcept;

namespace detail {

// Keep the native-boundary operations substitutable without adding runtime
// dispatch to the resource hot path. The owner is acquired exactly once and
// released even if a boundary throws before completing an operation.
template<class Native>
ResourceMapResult mapResourceNative(void* resource, MetalResource& out,
                                    ResourceAccess required, Native& native) noexcept {
    if (out.texture) native.releaseMetal(out.texture);
    if (out.buffer) {
        if constexpr (requires { native.releaseBuffer(out.buffer); })
            native.releaseBuffer(out.buffer);
    }
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
        if (!texture) {
            if constexpr (!requires { native.buffer(owner, std::declval<std::size_t&>()); }) {
                return failure;
            } else {
                std::size_t offset = 0;
                void* buffer = native.buffer(owner, offset);
                if (!buffer) return failure;
                if constexpr (!requires { native.retainBuffer(buffer); }) {
                    return failure;
                } else {
                    if (!native.retainBuffer(buffer)) return failure;
                    out.buffer = buffer;
                    out.bufferOffset = offset;
                    return ResourceMapResult::mapped;
                }
            }
        }
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

} // namespace yaagl::pso::d3dmetal
