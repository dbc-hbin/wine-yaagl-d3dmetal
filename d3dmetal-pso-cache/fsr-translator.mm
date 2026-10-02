#include "fsr-translator.hpp"
#include "fsr-contract.hpp"
#include "d3dmetal-transport.hpp"
#include "d3dmetal-transport-legacy.hpp"
#include "metalfx-backend.hpp"
#include "../include/yaagl_fsr_bridge.h"

#import <Foundation/Foundation.h>

#include <array>
#include <atomic>
#include <cstring>
#include <memory>
#include <mutex>
#include <new>
#include <unordered_map>
#include <utility>

namespace yaagl::pso::fsr {
namespace {
constexpr std::uint32_t kOk = 0;
constexpr std::uint32_t kRuntime = 3;
constexpr std::uint32_t kNoProvider = 4;
constexpr std::uint32_t kMemory = 5;
constexpr std::uint32_t kParameter = 6;
constexpr std::uint64_t kProvider = 0x4d46580000000001ull;

std::atomic<bool> gAvailable{false};
std::atomic<std::uint64_t> gNextContext{1};
std::mutex gRegistryMutex;

struct ComGuid { std::uint32_t a; std::uint16_t b, c; std::uint8_t d[8]; };
constexpr ComGuid kD3D12Device{0x189819f1, 0x1db6, 0x4b57,
    {0xbe, 0x54, 0x18, 0x21, 0x33, 0x9b, 0x85, 0xf7}};

void releaseCom(void* object) noexcept {
    if (!object) return;
    using Release = std::uint32_t (__attribute__((ms_abi)) *)(void*);
    reinterpret_cast<Release>((*static_cast<void***>(object))[2])(object);
}


struct AdapterLuid { std::uint32_t low; std::int32_t high; };

bool adapterLuid(void* device, AdapterLuid& output) noexcept {
    if (!device) return false;
    using GetAdapterLuid = AdapterLuid* (__attribute__((ms_abi)) *)(void*, AdapterLuid*);
    AdapterLuid value{};
    const auto table = *static_cast<void***>(device);
    if (reinterpret_cast<GetAdapterLuid>(table[43])(device, &value) != &value) return false;
    output = value;
    return true;
}

bool sameAdapter(const AdapterLuid& a, const AdapterLuid& b) noexcept {
    return a.low == b.low && a.high == b.high;
}

std::shared_ptr<void> deviceOwner(void* object, bool child) {
    if (!object) return {};
    using Query = std::int32_t (__attribute__((ms_abi)) *)(void*, const ComGuid*, void**);
    void* identity = nullptr;
    const auto table = *static_cast<void***>(object);
    const auto status = reinterpret_cast<Query>(table[child ? 7 : 0])(object, &kD3D12Device, &identity);
    if (status < 0 || !identity) return {};
    return std::shared_ptr<void>(identity, &releaseCom);
}

struct State {
    std::mutex mutex;
    std::uint64_t id = 0;
    CreateContract create;
    std::shared_ptr<void> device;
    AdapterLuid adapter{};
    std::shared_ptr<void> executionDevice;
    std::shared_ptr<metalfx::Feature> backend;
    metalfx::Extent generationOutput{};
    void* generationDevice = nullptr;
    void* generationCompiler = nullptr;
    bool retired = false;
};
std::unordered_map<std::uint64_t, std::shared_ptr<State>> gContexts;

std::shared_ptr<State> find(std::uint64_t id) {
    std::lock_guard lock(gRegistryMutex);
    const auto it = gContexts.find(id);
    return it == gContexts.end() ? nullptr : it->second;
}

std::uint32_t backendResult(metalfx::ErrorCode code) noexcept {
    switch (code) {
    case metalfx::ErrorCode::InvalidContext:
    case metalfx::ErrorCode::InvalidFrame: return kParameter;
    case metalfx::ErrorCode::UnsupportedFeature:
    case metalfx::ErrorCode::IncompatibleTexture: return kNoProvider;
    case metalfx::ErrorCode::ResourceCreationFailed: return kMemory;
    default: return kRuntime;
    }
}

struct CommandScope {
    d3dmetal::NativeCommandList native{};
    ~CommandScope() { d3dmetal::releaseCommandList(native); }
    bool open(void* list) noexcept {
        return d3dmetal::unwrapCommandList(list, native) &&
            (native.kind != d3dmetal::CommandListKind::legacy ||
             d3dmetal::legacy::resolveCommandList(native));
    }
};

struct ResourceScope {
    std::array<d3dmetal::MetalResource, 7> values{};
    ~ResourceScope() { for (auto& value : values) d3dmetal::releaseResource(value); }
};

std::uint32_t create(yaagl_fsr_create_packet& packet) {
    CreateContract contract;
    if (packet.header.size != sizeof(packet) || packet.provider_version != kProvider)
        return kParameter;
    const auto validated = validateCreate(packet, contract);
    if (validated != ContractStatus::Ok) return kParameter;
    auto owner = deviceOwner(reinterpret_cast<void*>(static_cast<std::uintptr_t>(packet.device)), false);
    if (!owner) return kParameter;
    auto state = std::make_shared<State>();
    state->id = gNextContext.fetch_add(1, std::memory_order_relaxed);
    state->create = contract;
    if (!adapterLuid(owner.get(), state->adapter)) return kParameter;
    state->device = std::move(owner);
    {
        std::lock_guard lock(gRegistryMutex);
        gContexts.emplace(state->id, state);
    }
    packet.header.context = state->id;
    return kOk;
}

std::uint32_t destroy(yaagl_fsr_packet_header& packet) {
    std::shared_ptr<State> state;
    {
        std::lock_guard lock(gRegistryMutex);
        const auto it = gContexts.find(packet.context);
        if (it == gContexts.end()) return kParameter;
        state = it->second; gContexts.erase(it);
    }
    {
        std::lock_guard lock(state->mutex);
        state->retired = true;
        // Recorded frames can outlive the context; they must not refill pools.
        if (state->backend) state->backend->markDormant();
        state->backend.reset();
        state->executionDevice.reset();
        state->device.reset();
    }
    return kOk;
}

std::uint32_t configure(yaagl_fsr_configure_packet& packet) {
    if (packet.header.size != sizeof(packet)) return kParameter;
    if (!packet.header.context) return kOk;
    auto state = find(packet.header.context);
    if (!state) return kParameter;
    return kOk;
}

std::uint32_t dispatch(yaagl_fsr_dispatch_packet& packet) {
    if (packet.header.size != sizeof(packet)) return kParameter;
    auto state = find(packet.header.context);
    if (!state) return kParameter;
    FrameContract frame;
    const auto validation = validateFrame(state->create, packet, frame);
    if (validation != ContractStatus::Ok) {
        return validation == ContractStatus::Unsupported ? kNoProvider : kParameter;
    }

    std::lock_guard stateLock(state->mutex);
    if (state->retired) return kParameter;
    CommandScope command;
    auto commandList = reinterpret_cast<void*>(static_cast<std::uintptr_t>(packet.command_list));
    if (!command.open(commandList)) return kParameter;
    auto commandDevice = deviceOwner(commandList, true);
    AdapterLuid commandAdapter{};
    if (!commandDevice || !adapterLuid(commandDevice.get(), commandAdapter) ||
        !sameAdapter(commandAdapter, state->adapter)) {
        return kParameter;
    }
    if (!state->executionDevice) state->executionDevice = commandDevice;
    const auto mode = command.native.kind == d3dmetal::CommandListKind::mpl
        ? metalfx::CommandMode::Metal4 : metalfx::CommandMode::Legacy;
    const metalfx::Extent output{frame.backend.outputRect.width, frame.backend.outputRect.height};
    const bool newGeneration = !state->backend || state->generationOutput.width != output.width ||
                               state->generationOutput.height != output.height ||
                               state->generationDevice != command.native.device ||
                               state->generationCompiler != command.native.compiler;
    if (state->backend && state->backend->mode() != mode) {
        return kParameter;
    }
    if (newGeneration) {
        auto createInfo = state->create.backend;
        createInfo.output = output;
        metalfx::CreateContext context{command.native.device, command.native.compiler, mode};
        metalfx::Error backendError;
        std::shared_ptr<metalfx::Feature> backend =
            metalfx::Feature::create(context, createInfo, &backendError);
        if (!backend) {
            return backendResult(backendError.code);
        }
        // Recorded frames can outlive the replaced feature; they must not refill pools.
        if (state->backend) state->backend->markDormant();
        // A fresh feature resets temporal history on its first executed frame.
        state->backend = std::move(backend);
        state->generationOutput = output;
        state->generationDevice = command.native.device;
        state->generationCompiler = command.native.compiler;
    }

    ResourceScope mapped;
    const std::array<void*, 7> resources{
        frame.backend.color, frame.backend.depth, frame.backend.motionVectors, frame.backend.output,
        frame.backend.exposureMode == metalfx::ExposureMode::Texture ? frame.backend.exposureTexture : nullptr,
        frame.backend.reactiveMask, frame.backend.compositionMask};
    std::array<d3dmetal::ResourceUse, 7> uses{};
    std::size_t useCount = 0;
    for (std::size_t i = 0; i < resources.size(); ++i) {
        if (!resources[i]) continue;
        auto resourceDevice = deviceOwner(resources[i], true);
        AdapterLuid resourceAdapter{};
        if (!resourceDevice || !adapterLuid(resourceDevice.get(), resourceAdapter) ||
            !sameAdapter(resourceAdapter, state->adapter)) {
            return kParameter;
        }
        const auto access = i == 3 ? d3dmetal::ResourceAccess::write : d3dmetal::ResourceAccess::read;
        switch (d3dmetal::mapResource(resources[i], mapped.values[i], access)) {
        case d3dmetal::ResourceMapResult::mapped: break;
        case d3dmetal::ResourceMapResult::metadataUnavailable:
            return kNoProvider;
        case d3dmetal::ResourceMapResult::notUnorderedAccess:
            return kParameter;
        case d3dmetal::ResourceMapResult::mapFailed:
            return kNoProvider;
        }
        uses[useCount++] = {&mapped.values[i], access};
    }
    for (std::size_t i = 0; i < mapped.values.size(); ++i) {
        if (i == 3 || !mapped.values[i].texture) continue;
        if (mapped.values[i].texture == mapped.values[3].texture &&
            std::memcmp(&mapped.values[i].view, &mapped.values[3].view,
                        sizeof(d3dmetal::TextureView)) == 0) return kParameter;
    }
    metalfx::TextureSet textures{
        mapped.values[0].texture, mapped.values[1].texture, mapped.values[2].texture,
        mapped.values[3].texture, mapped.values[4].texture, mapped.values[5].texture,
        mapped.values[6].texture};
    metalfx::Error backendError;
    std::shared_ptr<const metalfx::PreparedFrame> prepared =
        state->backend->prepare(frame.backend, textures, &backendError, frame.operations);
    if (!prepared) {
        return backendResult(backendError.code);
    }
    const d3dmetal::RecordRequest request{prepared, uses.data(), useCount};
    const bool recorded = command.native.kind == d3dmetal::CommandListKind::legacy
        ? d3dmetal::legacy::record(command.native, request)
        : d3dmetal::record(command.native, request);
    if (!recorded) return kRuntime;
    return kOk;
}
} // namespace

bool initialize(const std::uint8_t* imageBase) noexcept {
    try {
        const bool ready = d3dmetal::initialize(imageBase) &&
                           d3dmetal::legacy::initialize(imageBase);
        gAvailable.store(ready, std::memory_order_release);
        return ready;
    } catch (...) { return false; }
}

bool available() noexcept { return gAvailable.load(std::memory_order_acquire); }

} // namespace yaagl::pso::fsr

extern "C" __attribute__((visibility("default"))) std::uint32_t
yaagl_fsr_api(std::uint32_t operation, void* arguments) noexcept {
    using namespace yaagl::pso::fsr;
    if (!arguments) return 6;
    auto& header = *static_cast<yaagl_fsr_packet_header*>(arguments);
    if (header.operation != operation || header.size < sizeof(header)) return 6;
    if (!available() && operation != YAAGL_FSR_CONFIGURE) return 3;
    // Wine game threads have no autorelease pool; without this one, objects
    // autoreleased while recording accumulate until the thread exits.
    @autoreleasepool {
        try {
            @try {
                switch (operation) {
                case YAAGL_FSR_CREATE: return create(*static_cast<yaagl_fsr_create_packet*>(arguments));
                case YAAGL_FSR_DESTROY: return destroy(header);
                case YAAGL_FSR_CONFIGURE: return configure(*static_cast<yaagl_fsr_configure_packet*>(arguments));
                case YAAGL_FSR_DISPATCH: return dispatch(*static_cast<yaagl_fsr_dispatch_packet*>(arguments));
                default: return 2;
                }
            } @catch (NSException*) { return 3; }
        } catch (const std::bad_alloc&) { return 5; }
        catch (...) { return 3; }
    }
}
