#include "low-latency.hpp"
#include "low-latency-state.hpp"
#include "async-pipeline-state.hpp"
#include "../include/yaagl_d3dmetal_abi.hpp"
#include "../include/yaagl_macho_symbols.hpp"
#include "../include/yaagl_nvapi_low_latency.hpp"

#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <dlfcn.h>

namespace yaagl::latency {
namespace {

using Present = std::int32_t (__attribute__((ms_abi)) *)(void*, unsigned, unsigned, const void*);
using Complete = void (*)(void*);
// The base destructor has a hidden construction-vtable argument in RSI.
using DestroyDevice = void (*)(void*, const void*);
using DynamicCast = void* (*)(void*, const void*, const void*, std::ptrdiff_t);

Hooks original{};
std::atomic<nvapi::Sleep> nativeSleep{};
std::atomic<nvapi::SetSleepMode> nativeSetMode{};
std::atomic<nvapi::GetSleepStatus> nativeGetStatus{};
std::atomic<nvapi::SetLatencyMarker> nativeMarker{};
void* const* gfxUnknown = nullptr;
const void* unknownIid = nullptr;
const void* unknownType = nullptr;
const void* deviceType = nullptr;
DynamicCast dynamicCast = nullptr;
bool forceMode = false;

Limiter& limiter() {
    // Native completion threads can still call us during process teardown.
    static auto* value = new Limiter(forceMode);
    return *value;
}

template<class T> T field(void* object, std::ptrdiff_t offset) {
    return native::readObject<T>(static_cast<std::uint8_t*>(object) + offset);
}

void* deviceKey(void* publicDevice) {
    if (!publicDevice) return nullptr;
    void* unknown = nullptr;
    using Ref = std::uint32_t (__attribute__((ms_abi)) *)(void*);
    if (*gfxUnknown) {
        // Same private D3DMUnknown conversion as native SetLatencyMarker.
        // Its GetInterface is System V; its AddRef/Release are Microsoft ABI.
        void* bridge = field<void*>(publicDevice, -static_cast<std::ptrdiff_t>(sizeof(void*)));
        void** table = native::readObject<void**>(bridge);
        using GetInterface = void* (*)(void*, const void*);
        unknown = reinterpret_cast<GetInterface>(table[3])(bridge, unknownIid);
        if (!unknown) return nullptr;
        reinterpret_cast<Ref>(native::readObject<void**>(unknown)[4])(unknown);
    } else {
        using Query = std::int32_t (__attribute__((ms_abi)) *)(void*, const void*, void**);
        void** table = native::readObject<void**>(publicDevice);
        if (reinterpret_cast<Query>(table[0])(publicDevice, unknownIid, &unknown) < 0) return nullptr;
    }
    // RTTI supplies the base displacement; no D3D11/12 device-field offsets.
    void* device = dynamicCast(unknown, unknownType, deviceType, -1);
    reinterpret_cast<Ref>(native::readObject<void**>(unknown)[5])(unknown);
    return device;
}

std::int32_t __attribute__((ms_abi)) setMode(void* device, nvapi::SleepMode* mode) {
    if (!device || !mode) return nvapi::InvalidArgument;
    const auto result = nativeSetMode.load(std::memory_order_acquire)(device, mode);
    if (result != nvapi::Ok) return result;
    void* key = deviceKey(device);
    if (!key) return nvapi::NotSupported;
    // Fixed queue depth is not an FPS limiter. Do not silently accept a cap.
    if (mode->minimumIntervalUs) return nvapi::NotSupported;
    limiter().configure(key, mode->lowLatencyMode != 0);
    return result;
}

std::int32_t __attribute__((ms_abi)) sleep(void* device) {
    if (!device) return nvapi::InvalidArgument;
    const auto result = nativeSleep.load(std::memory_order_acquire)(device);
    if (result == nvapi::Ok) {
        if (void* key = deviceKey(device)) limiter().sleep(key);
    }
    return result;
}

std::int32_t __attribute__((ms_abi)) getStatus(void* device, nvapi::SleepStatus* status) {
    if (!device || !status) return nvapi::InvalidArgument;
    const auto result = nativeGetStatus.load(std::memory_order_acquire)(device, status);
    if (result == nvapi::Ok) {
        const auto state = limiter().snapshot(deviceKey(device));
        status->lowLatencyMode = state.active;
        status->useGameSleep = state.sleepCalled;
        status->sleepIntervalUs = state.sleepIntervalUs;
    }
    return result;
}

std::int32_t __attribute__((ms_abi)) marker(void* device, nvapi::LatencyMarker* params) {
    if (!device || !params) return nvapi::InvalidArgument;
    const auto result = nativeMarker.load(std::memory_order_acquire)(device, params);
    if (result == nvapi::Ok && params->markerType >= nvapi::OutOfBandRenderSubmitStart &&
        params->markerType <= nvapi::OutOfBandPresentEnd) {
        if (void* key = deviceKey(device)) limiter().exclude(key);
    }
    return result;
}

void* __attribute__((ms_abi)) queryInterface(std::uint32_t id) {
    void* resolved = reinterpret_cast<nvapi::QueryInterface>(original.queryInterface)(id);
    if (!resolved) return nullptr;
    // Resolve only on the real API call, after D3DMetal's own constructors ran.
    // The PE DLL may cache the returned wrapper; originals are published first.
    switch (id) {
    case nvapi::SleepId:
        nativeSleep.store(reinterpret_cast<nvapi::Sleep>(resolved), std::memory_order_release);
        return reinterpret_cast<void*>(&sleep);
    case nvapi::SetSleepModeId:
        nativeSetMode.store(reinterpret_cast<nvapi::SetSleepMode>(resolved), std::memory_order_release);
        return reinterpret_cast<void*>(&setMode);
    case nvapi::GetSleepStatusId:
        nativeGetStatus.store(reinterpret_cast<nvapi::GetSleepStatus>(resolved), std::memory_order_release);
        return reinterpret_cast<void*>(&getStatus);
    case nvapi::SetLatencyMarkerId:
        nativeMarker.store(reinterpret_cast<nvapi::SetLatencyMarker>(resolved), std::memory_order_release);
        return reinterpret_cast<void*>(&marker);
    default:
        return resolved;
    }
}

std::int32_t __attribute__((ms_abi)) present(void* swapchain, unsigned interval,
                                          unsigned flags, const void* params) {
    const auto call = reinterpret_cast<Present>(original.present);
    if (flags & 1u) { // DXGI_PRESENT_TEST never schedules a native completion.
        limiter().testPresent(field<void*>(swapchain, 0x20));
        return call(swapchain, interval, flags, params);
    }
    // Pinned native swapchain owns a D3DMDevice at +0x20. Public COM is +0x10.
    const bool eligible = !pso::async_pipeline::presentationHistory().hasFrameGeneration(
        static_cast<std::uint8_t*>(swapchain) + 0x10);
    auto token = limiter().begin(field<void*>(swapchain, 0x20), swapchain, eligible);
    const auto before = field<std::uint64_t>(swapchain, 0x120);
    const auto result = call(swapchain, interval, flags, params);
    limiter().finish(token, result >= 0 && field<std::uint64_t>(swapchain, 0x120) != before,
        (flags & 8u) == 0); // DXGI_PRESENT_DO_NOT_WAIT must not gain a blocking wait.
    return result;
}

void completePresent(void* block) {
    // Observe before native completion releases its reserved swapchain ref.
    limiter().complete(field<void*>(block, 0x20));
    reinterpret_cast<Complete>(original.completePresent)(block);
}

void destroySwapchain(void* swapchain) {
    limiter().retire(swapchain);
    reinterpret_cast<Complete>(original.destroySwapchain)(swapchain);
}

void destroyDevice(void* device, const void* constructionVtable) {
    limiter().destroy(device);
    reinterpret_cast<DestroyDevice>(original.destroyDevice)(device, constructionVtable);
}

} // namespace

Hooks initialize(const std::uint8_t* image, Hooks originals) {
    original = originals;
    const char* mode = std::getenv("YAAGL_D3DMETAL_LOW_LATENCY");
    forceMode = mode && !std::strcmp(mode, "force");
    if (!forceMode && (!mode || std::strcmp(mode, "1"))) return originals;
    const macho::Symbols symbols(reinterpret_cast<const mach_header_64*>(image));
    const auto data = [&](const char* name) -> void* {
        void* value = symbols.find(name, false);
        if (value) {
            if (void* bound = dlsym(RTLD_DEFAULT, name + 1)) value = bound;
        }
        return value;
    };
    gfxUnknown = reinterpret_cast<void* const*>(data("__ZN15GFXTOSInterface13IUnknownIfaceE"));
    unknownIid = data("__GUID_6ddd0b82_550f_487e_94fe_4473fe0ca95d");
    unknownType = data("__ZTI11D3DMUnknown");
    deviceType = data("__ZTI10D3DMDevice");
    dynamicCast = reinterpret_cast<DynamicCast>(dlsym(RTLD_DEFAULT, "__dynamic_cast"));
    if (!gfxUnknown || !unknownIid || !unknownType || !deviceType || !dynamicCast) {
        std::fprintf(stderr, "[yaagl-latency] unavailable native device contract\n");
        return originals;
    }
    return {reinterpret_cast<std::uintptr_t>(&present),
        reinterpret_cast<std::uintptr_t>(&queryInterface),
        reinterpret_cast<std::uintptr_t>(&completePresent),
        reinterpret_cast<std::uintptr_t>(&destroySwapchain),
        reinterpret_cast<std::uintptr_t>(&destroyDevice)};
}

} // namespace yaagl::latency
