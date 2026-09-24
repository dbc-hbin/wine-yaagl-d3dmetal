#include "display-routing.hpp"

#include "../include/yaagl_d3dmetal_display.h"

#include <dlfcn.h>
#include <os/lock.h>

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstring>

namespace yaagl::pso::display {
namespace {

using QueryDisplay = int (*)(std::uintptr_t, std::uintptr_t, yaagl_d3dmetal_display*);
using GetContainingOutput = std::int32_t (*)(void*, void**);
// The pinned direct vtable thunk and unixcall unpacker must both forward the
// IDXGIOutput* here; otherwise RDX can still contain the fullscreen BOOL.
using SetFullscreenState = std::int32_t (*)(void*, int, void*);
using EnumOutputs = std::int32_t (*)(void*, std::uint32_t, void**);
using AddSwapChain = void (*)(void*, void*);
using AsInterface = void* (*)(void*, const void*);
using NoArgMethod = void (*)(void*);

constexpr std::int32_t kInvalidCall = static_cast<std::int32_t>(0x887a0001u);
constexpr std::uint64_t kNativeReference = 0x100000001ull;
constexpr std::size_t kHwnd = 0x18;
constexpr std::size_t kQueue = 0x28;
constexpr std::size_t kFullscreen = 0xb8;
constexpr std::size_t kOutput = 0xd0;
constexpr std::size_t kSwapchainLock = 0x128;
constexpr std::size_t kOutputAdapter = 0x18;
constexpr std::size_t kOutputMonitor = 0x20;
constexpr std::size_t kOutputRefresh = 0x38;
constexpr std::size_t kOutputLock = 0x3c;
constexpr std::size_t kOutputSwapchains = 0x40;
constexpr std::size_t kOutputSwapchainsEnd = 0x48;
constexpr std::size_t kOutputInterface = 0x188;
constexpr std::size_t kOutputReferences = 0x198;
constexpr std::size_t kExternalOwner = sizeof(void*);

struct Guid {
    std::uint32_t first;
    std::uint16_t second;
    std::uint16_t third;
    std::uint8_t last[8];
};
constexpr Guid kNativeOutput = {0x0c0d70fc, 0x8008, 0x48c6,
                                {0x87, 0x23, 0x4a, 0x8d, 0x01, 0x9a, 0x1d, 0x60}};
constexpr Guid kReleaseInterface = {0x6ddd0b82, 0x550f, 0x487e,
                           {0x94, 0xfe, 0x44, 0x73, 0xfe, 0x0c, 0xa9, 0x5d}};

struct Runtime {
    QueryDisplay query = nullptr;
    GetContainingOutput originalGet = nullptr;
    SetFullscreenState originalSet = nullptr;
    EnumOutputs enumerate = nullptr;
    AddSwapChain addSwapChain = nullptr;
};
Runtime runtime;

template <typename T>
T load(const void* object, std::size_t offset) noexcept {
    T value;
    std::memcpy(&value, static_cast<const char*>(object) + offset, sizeof(value));
    return value;
}

template <typename T>
void store(void* object, std::size_t offset, T value) noexcept {
    std::memcpy(static_cast<char*>(object) + offset, &value, sizeof(value));
}

os_unfair_lock* lockAt(void* object, std::size_t offset) noexcept {
    return reinterpret_cast<os_unfair_lock*>(static_cast<char*>(object) + offset);
}

struct Locked {
    explicit Locked(os_unfair_lock* lock) noexcept : lock(lock) { os_unfair_lock_lock(lock); }
    ~Locked() { os_unfair_lock_unlock(lock); }
    Locked(const Locked&) = delete;
    Locked& operator=(const Locked&) = delete;
    os_unfair_lock* lock;
};

// Native external DXGI interfaces store the controlling object one pointer
// before the returned interface. This is the same conversion used by the
// pinned CreateSwapChainForHwnd and ResizeTarget implementations.
void* nativeOutput(void* external) noexcept {
    if (!external) return nullptr;
    void* owner = load<void*>(static_cast<char*>(external) - kExternalOwner, 0);
    auto* methods = load<void**>(owner, 0);
    return reinterpret_cast<AsInterface>(methods[3])(owner, &kNativeOutput);
}

void releaseExternal(void* external) noexcept {
    if (!external) return;
    void* owner = load<void*>(static_cast<char*>(external) - kExternalOwner, 0);
    auto* methods = load<void**>(owner, 0);
    void* unknown = reinterpret_cast<AsInterface>(methods[3])(owner, &kReleaseInterface);
    if (!unknown) return;
    auto* unknownMethods = load<void**>(unknown, 0);
    reinterpret_cast<NoArgMethod>(unknownMethods[5])(unknown);
}

void addReference(void* output) noexcept {
    auto* count = reinterpret_cast<std::uint64_t*>(static_cast<char*>(output) + kOutputReferences);
    std::atomic_ref<std::uint64_t>(*count).fetch_add(kNativeReference, std::memory_order_relaxed);
}

void releaseReference(void* output) noexcept {
    auto* count = reinterpret_cast<std::uint64_t*>(static_cast<char*>(output) + kOutputReferences);
    if (std::atomic_ref<std::uint64_t>(*count).fetch_sub(kNativeReference,
            std::memory_order_acq_rel) != kNativeReference) return;
    void* interface = static_cast<char*>(output) + kOutputInterface;
    auto* methods = load<void**>(interface, 0);
    reinterpret_cast<NoArgMethod>(methods[1])(interface);
}

bool hasSwapchain(void* output, void* swapchain) noexcept {
    Locked guard(lockAt(output, kOutputLock));
    auto* first = load<void**>(output, kOutputSwapchains);
    auto* last = load<void**>(output, kOutputSwapchainsEnd);
    for (auto* cursor = first; cursor != last; ++cursor) {
        if (*cursor == swapchain) return true;
    }
    return false;
}

bool removeSwapchain(void* output, void* swapchain) noexcept {
    Locked guard(lockAt(output, kOutputLock));
    auto* first = load<void**>(output, kOutputSwapchains);
    auto* last = load<void**>(output, kOutputSwapchainsEnd);
    for (auto* cursor = first; cursor != last; ++cursor) {
        if (*cursor != swapchain) continue;
        std::memmove(cursor, cursor + 1, reinterpret_cast<char*>(last) -
                                             reinterpret_cast<char*>(cursor + 1));
        store(output, kOutputSwapchainsEnd, last - 1);
        return true;
    }
    return false;
}

struct Candidate {
    void* native = nullptr;
    void* external = nullptr;
    ~Candidate() { releaseExternal(external); }
    Candidate(const Candidate&) = delete;
    Candidate& operator=(const Candidate&) = delete;
    Candidate() = default;
};

void* adapterFor(void* swapchain, void* fallbackOutput) noexcept {
    void* output = load<void*>(swapchain, kOutput);
    if (!output) output = fallbackOutput;
    return output ? load<void*>(output, kOutputAdapter) : nullptr;
}

bool findOutput(void* adapter, std::uintptr_t monitor, Candidate& found) noexcept {
    if (!adapter || !monitor) return false;
    for (std::uint32_t index = 0; index < 256; ++index) {
        void* external = nullptr;
        const std::int32_t result = runtime.enumerate(adapter, index, &external);
        if (result < 0 || !external) return false;
        void* output = nativeOutput(external);
        if (output && load<std::uintptr_t>(output, kOutputMonitor) == monitor) {
            found.native = output;
            found.external = external;
            return true;
        }
        releaseExternal(external);
    }
    return false;
}

// Caller owns swapchain +0x128. AddSwapChain is allowed to grow its vector
// only when the selected monitor changes; steady-state Present does not allocate.
bool bindOutput(void* swapchain, void* next) {
    void* previous = load<void*>(swapchain, kOutput);
    if (!next || !previous) return false;
    if (next == previous) return true;
    if (!hasSwapchain(previous, swapchain)) return false;
    addReference(next);
    try {
        runtime.addSwapChain(next, swapchain);
    } catch (...) {
        releaseReference(next);
        return false;
    }
    if (!removeSwapchain(previous, swapchain)) {
        removeSwapchain(next, swapchain);
        releaseReference(next);
        return false;
    }
    store(swapchain, kOutput, next);
    releaseReference(previous);
    return true;
}

std::uintptr_t hwndOf(void* swapchain) noexcept {
    return reinterpret_cast<std::uintptr_t>(load<void*>(swapchain, kHwnd));
}

bool query(void* swapchain, std::uintptr_t overrideMonitor,
           yaagl_d3dmetal_display& display) noexcept {
    return runtime.query && runtime.query(hwndOf(swapchain), overrideMonitor, &display) == 1 &&
           display.monitor && display.refresh_rate;
}

void route(void* swapchain, const yaagl_d3dmetal_display& display) {
    void* current = load<void*>(swapchain, kOutput);
    if (!current) return;
    if (load<std::uintptr_t>(current, kOutputMonitor) != display.monitor) {
        Candidate candidate;
        if (!findOutput(adapterFor(swapchain, nullptr), display.monitor, candidate) ||
            !bindOutput(swapchain, candidate.native)) return;
        current = candidate.native;
    }
    store(current, kOutputRefresh, display.refresh_rate);
}

std::int32_t getContainingOutput(void* swapchain, void** result) {
    if (!runtime.query || !result) return runtime.originalGet(swapchain, result);
    if (load<std::uint8_t>(swapchain, kFullscreen)) {
        Locked guard(lockAt(swapchain, kSwapchainLock));
        return runtime.originalGet(swapchain, result);
    }
    yaagl_d3dmetal_display display{};
    if (!query(swapchain, 0, display)) return runtime.originalGet(swapchain, result);
    void* current = load<void*>(swapchain, kOutput);
    if (!current) {
        void* first = nullptr;
        const std::int32_t status = runtime.originalGet(swapchain, &first);
        if (status < 0 || !first) {
            *result = first;
            return status;
        }
        current = nativeOutput(first);
        if (current && load<std::uintptr_t>(current, kOutputMonitor) == display.monitor) {
            store(current, kOutputRefresh, display.refresh_rate);
            *result = first;
            return status;
        }
        Candidate candidate;
        if (current && findOutput(adapterFor(swapchain, current), display.monitor, candidate)) {
            store(candidate.native, kOutputRefresh, display.refresh_rate);
            *result = candidate.external;
            candidate.external = nullptr;
            releaseExternal(first);
            return 0;
        }
        *result = first;
        return status;
    }
    Locked guard(lockAt(swapchain, kSwapchainLock));
    if (!load<std::uint8_t>(swapchain, kFullscreen)) route(swapchain, display);
    return runtime.originalGet(swapchain, result);
}

std::int32_t setFullscreenState(void* swapchain, int fullscreen, void* target) {
    if (!runtime.query) return runtime.originalSet(swapchain, fullscreen, target);
    if (!fullscreen) {
        const std::int32_t status = runtime.originalSet(swapchain, fullscreen, target);
        if (status < 0) return status;
        yaagl_d3dmetal_display display{};
        if (query(swapchain, 0, display)) {
            Locked guard(lockAt(swapchain, kSwapchainLock));
            route(swapchain, display);
        }
        return status;
    }
    std::uintptr_t overrideMonitor = 0;
    if (target) {
        void* output = nativeOutput(target);
        if (!output || !(overrideMonitor = load<std::uintptr_t>(output, kOutputMonitor)))
            return kInvalidCall;
    }
    yaagl_d3dmetal_display display{};
    if (query(swapchain, overrideMonitor, display)) {
        Locked guard(lockAt(swapchain, kSwapchainLock));
        route(swapchain, display);
        if (target && load<std::uintptr_t>(load<void*>(swapchain, kOutput), kOutputMonitor) !=
                          overrideMonitor) return kInvalidCall;
    } else if (target) {
        return kInvalidCall;
    }
    return runtime.originalSet(swapchain, fullscreen, target);
}

void presentFlush(void* swapchain) {
    // This hook is reached after native Present1 has acquired +0x128, before
    // the original queue flush. Never acquire +0x128 again here.
    if (runtime.query) {
        void* current = load<void*>(swapchain, kOutput);
        const std::uintptr_t overrideMonitor =
            current && load<std::uint8_t>(swapchain, kFullscreen)
                ? load<std::uintptr_t>(current, kOutputMonitor) : 0;
        yaagl_d3dmetal_display display{};
        if (query(swapchain, overrideMonitor, display)) route(swapchain, display);
    }
    void* queue = load<void*>(swapchain, kQueue);
    auto* methods = load<void**>(queue, 0);
    reinterpret_cast<NoArgMethod>(methods[2])(queue);
}

} // namespace

Hooks initialize(std::uintptr_t imageBase, std::uintptr_t originalGetContainingOutput,
                 std::uintptr_t originalSetFullscreenState) noexcept {
    runtime.originalGet = reinterpret_cast<GetContainingOutput>(originalGetContainingOutput);
    runtime.originalSet = reinterpret_cast<SetFullscreenState>(originalSetFullscreenState);
    runtime.enumerate = reinterpret_cast<EnumOutputs>(imageBase + 0x1509be);
    runtime.addSwapChain = reinterpret_cast<AddSwapChain>(imageBase + 0x118dc4);
    runtime.query = reinterpret_cast<QueryDisplay>(dlsym(RTLD_DEFAULT,
                                                         "macdrv_query_d3dmetal_display"));
    return {reinterpret_cast<std::uintptr_t>(&getContainingOutput),
            reinterpret_cast<std::uintptr_t>(&setFullscreenState),
            reinterpret_cast<std::uintptr_t>(&presentFlush)};
}

} // namespace yaagl::pso::display
