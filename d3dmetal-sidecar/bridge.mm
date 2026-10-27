#import <objc/message.h>
#import <objc/runtime.h>

#include <mach-o/dyld.h>
#include <mach-o/loader.h>

#include <array>
#include <atomic>
#include <cstdint>
#include <cstring>

#include "async-pipeline.hpp"
#include "d3dmetal-transport.hpp"
#include "fsr-translator.hpp"
#include "fsr-framegeneration.hpp"
#include "display-routing.hpp"
#include "layout.hpp"
#include "low-latency.hpp"
#include "ngx-hooks.hpp"
#include "video-memory.hpp"

namespace yaagl::pso {
namespace {

constexpr std::size_t kHookCount = static_cast<std::size_t>(layout::Hook::Count);
std::array<std::uintptr_t, kHookCount> originalFunctions{};
std::array<std::uintptr_t, kHookCount> dispatchTable{};

char presentResidencyKey;

void addPresentResidency(id queue, SEL selector, id set) {
    // Only DoPresent's layer-set registration reaches this hook. Its worker
    // serializes Present on this queue, including the synchronous path.
    id previous = objc_getAssociatedObject(queue, &presentResidencyKey);
    if (previous == set) return;
    using Message = void (*)(id, SEL, id);
    if (previous != nil) {
        reinterpret_cast<Message>(objc_msgSend)(
            queue, @selector(removeResidencySet:), previous);
    }
    reinterpret_cast<Message>(objc_msgSend)(queue, selector, set);
    // The queue owns this bookkeeping. Replacement releases the old set;
    // queue destruction releases the final association without a global map.
    objc_setAssociatedObject(queue, &presentResidencyKey, set, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
}

std::int32_t __attribute__((ms_abi)) queryVideoMemoryInfo(
    void* adapter, std::uint32_t node, std::uint32_t group,
    video_memory::QueryVideoMemoryInfo* info) noexcept {
    video_memory::updateAdapterDescription(adapter);
    return video_memory::query(adapter, node, group, info);
}

bool matchesImage(const mach_header* untyped) noexcept {
    if (untyped->magic != MH_MAGIC_64 || untyped->cputype != CPU_TYPE_X86_64 ||
        untyped->filetype != MH_DYLIB) return false;
    const auto* header = reinterpret_cast<const mach_header_64*>(untyped);
    if (header->ncmds != layout::kCommandCount || header->sizeofcmds != layout::kCommandsSize ||
        sizeof(*header) + header->sizeofcmds > layout::kFirstTextOffset) return false;
    const auto* base = reinterpret_cast<const std::uint8_t*>(header);
    std::size_t position = sizeof(*header);
    bool uuidMatches = false;
    bool textMatches = false;
    bool slotWritable = false;
    for (std::uint32_t index = 0; index < header->ncmds; ++index) {
        if (position + sizeof(load_command) > sizeof(*header) + header->sizeofcmds) return false;
        const auto* command = reinterpret_cast<const load_command*>(base + position);
        if (command->cmdsize < sizeof(load_command) || (command->cmdsize & 7) != 0 ||
            command->cmdsize > sizeof(*header) + header->sizeofcmds - position) return false;
        if (command->cmd == LC_UUID && command->cmdsize == sizeof(uuid_command)) {
            const auto* uuid = reinterpret_cast<const uuid_command*>(command);
            if (uuidMatches || std::memcmp(uuid->uuid, layout::kUuid, sizeof(layout::kUuid)) != 0) return false;
            uuidMatches = true;
        }
        if (command->cmd == LC_SEGMENT_64 && command->cmdsize >= sizeof(segment_command_64)) {
            const auto* segment = reinterpret_cast<const segment_command_64*>(command);
            if (std::strncmp(segment->segname, "__TEXT", sizeof(segment->segname)) == 0) {
                textMatches = segment->vmaddr == 0 && segment->vmsize == layout::kTextSize &&
                    segment->fileoff == 0 && segment->filesize == layout::kTextSize && segment->initprot == 5;
            } else if (std::strncmp(segment->segname, "__DATA", sizeof(segment->segname)) == 0) {
                slotWritable = segment->vmaddr <= layout::kDataSlot &&
                    layout::kDataSlot + sizeof(std::uintptr_t) <= segment->vmaddr + segment->vmsize &&
                    segment->initprot == 3;
            }
        }
        position += command->cmdsize;
    }
    if (!uuidMatches || !textMatches || !slotWritable || position != sizeof(*header) + header->sizeofcmds) return false;
    const auto* common = reinterpret_cast<const section_64*>(base + layout::kCommonSectionOffset);
    if (std::strncmp(common->sectname, "__common", sizeof(common->sectname)) != 0 ||
        std::strncmp(common->segname, "__DATA", sizeof(common->segname)) != 0 ||
        common->addr != layout::kCommonAddress || common->size != layout::kCommonSize ||
        (common->flags & SECTION_TYPE) != S_ZEROFILL) return false;
    for (const auto& span : layout::kVerificationSpans) {
        if (std::memcmp(base + span.offset, span.bytes, span.size) != 0) return false;
    }
    auto& slot = *reinterpret_cast<std::uintptr_t*>(const_cast<std::uint8_t*>(base) + layout::kDataSlot);
    return std::atomic_ref<std::uintptr_t>(slot).load(std::memory_order_acquire) == 0;
}

__attribute__((constructor)) void initialize() noexcept {
    try {
        const std::uint8_t* base = nullptr;
        for (std::uint32_t index = 0; index < _dyld_image_count(); ++index) {
            const mach_header* header = _dyld_get_image_header(index);
            if (!matchesImage(header)) continue;
            if (base != nullptr) return;
            base = reinterpret_cast<const std::uint8_t*>(header);
        }
        if (base == nullptr) return;
        static_cast<void>(fsr::initialize(base));
        static_cast<void>(fsr::framegeneration::initialize(base));
        for (std::size_t index = 0; index < kHookCount; ++index) {
            originalFunctions[index] = reinterpret_cast<std::uintptr_t>(base + layout::kTrampolines[index]);
        }
        const auto original = [](layout::Hook hook) {
            return originalFunctions[static_cast<std::size_t>(hook)];
        };
        const std::array<std::uintptr_t, ngx::kHookCount> replayOriginals = {
            original(layout::Hook::ReplayTemporalScale),
        };
        const auto replayHooks = ngx::initializeHooks(replayOriginals);
        for (const auto hook : replayHooks) {
            if (hook == 0) return;
        }
        const display::Hooks displayHooks = display::initialize(reinterpret_cast<std::uintptr_t>(base),
            original(layout::Hook::GetContainingOutput), original(layout::Hook::SetFullscreenState));
        const auto completionHook = d3dmetal::initializeCompletionHook(
            original(layout::Hook::CompleteExecute));
        if (!completionHook) return;
        const auto asyncHooks = async_pipeline::initialize(base, {
            .graphicsCtor = original(layout::Hook::GraphicsCtor),
            .getRenderPipelineState = original(layout::Hook::GetRenderPipelineState),
            .graphicsDestroy = original(layout::Hook::GraphicsDestroy),
            .draw = original(layout::Hook::MPLDraw),
            .drawIndexed = original(layout::Hook::MPLDrawIndexed),
            .rootCtor = original(layout::Hook::RootCtor),
            .rootDestroy = original(layout::Hook::RootDestroy),
            .reset = original(layout::Hook::MPLReset),
            .beginQuery = original(layout::Hook::MPLBeginQuery),
            .endQuery = original(layout::Hook::MPLEndQuery),
            .listDestroy = original(layout::Hook::MPLDestroy),
            .present = original(layout::Hook::Present1),
            .dx11Draw = original(layout::Hook::DX11Draw),
            .dx11DrawIndexed = original(layout::Hook::DX11DrawIndexed),
            .dx11Begin = original(layout::Hook::DX11Begin),
            .dx11End = original(layout::Hook::DX11End),
            .dx11Destroy = original(layout::Hook::DX11Destroy),
        });
        const auto latencyHooks = latency::initialize(base, {
            .present = asyncHooks.present,
            .queryInterface = original(layout::Hook::NvapiQueryInterface),
            .completePresent = original(layout::Hook::CompletePresent),
            .destroySwapchain = original(layout::Hook::LatencySwapchainDestroy),
            .destroyDevice = original(layout::Hook::LatencyDeviceDestroy),
        });
        dispatchTable = {
            displayHooks.getContainingOutput,
            displayHooks.setFullscreenState,
            reinterpret_cast<std::uintptr_t>(&queryVideoMemoryInfo),
            replayHooks[0],
            asyncHooks.graphicsCtor, asyncHooks.getRenderPipelineState, asyncHooks.graphicsDestroy,
            asyncHooks.draw, asyncHooks.drawIndexed, asyncHooks.rootCtor, asyncHooks.rootDestroy,
            asyncHooks.reset, asyncHooks.beginQuery, asyncHooks.endQuery, asyncHooks.listDestroy,
            latencyHooks.present, asyncHooks.dx11Draw, asyncHooks.dx11DrawIndexed,
            asyncHooks.dx11Begin, asyncHooks.dx11End, asyncHooks.dx11Destroy,
            completionHook,
            reinterpret_cast<std::uintptr_t>(&addPresentResidency),
            latencyHooks.queryInterface, latencyHooks.completePresent,
            latencyHooks.destroySwapchain, latencyHooks.destroyDevice,
        };
        auto& slot = *reinterpret_cast<std::uintptr_t*>(const_cast<std::uint8_t*>(base) + layout::kDataSlot);
        std::atomic_ref<std::uintptr_t>(slot).store(
            reinterpret_cast<std::uintptr_t>(dispatchTable.data()), std::memory_order_release);
    } catch (...) {
        // Unpublished gates retain the byte-for-byte original helper path.
    }
}

} // namespace
} // namespace yaagl::pso
