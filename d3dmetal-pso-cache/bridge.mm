#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <objc/message.h>

#include <mach-o/dyld.h>
#include <mach-o/loader.h>

#include <array>
#include <atomic>
#include <cstdint>
#include <cstring>
#include <new>

#include "cache.hpp"
#include "fsr-translator.hpp"
#include "fsr-framegeneration.hpp"
#include "function-cache.hpp"
#include "function-hooks.hpp"
#include "key.hpp"
#include "ngx-hooks.hpp"
#include "d3dmetal-transport.hpp"
#include "display-routing.hpp"
#include "layout.hpp"
#include "rt-key.hpp"
#include "video-memory.hpp"

namespace yaagl::pso {
namespace {

using CreateNative = id (*)(const void*, id, std::uint64_t, id*, NSError**);
using GetRender = id (*)(const void*, std::uint32_t, std::uint64_t);
using CompileCompute = void (*)(const void*, bool);
using DestroyDevice = void (*)(const void*, const void*);
constexpr std::size_t kHookCount = static_cast<std::size_t>(layout::Hook::Count);
std::array<std::uintptr_t, kHookCount> originalFunctions{};
static_assert(layout::kPresentDispatchIndex == kHookCount);
static_assert(layout::kCommitDispatchIndex == kHookCount + 1);
static_assert(layout::kPresentResidencyAddDispatchIndex == kHookCount + 2);
static_assert(layout::kPresentResidencyFinishDispatchIndex == kHookCount + 3);
static_assert(layout::kVideoMemoryDispatchIndex == kHookCount + 4);
std::array<std::uintptr_t, kHookCount + 5> dispatchTable{};
thread_local Context currentContext{};
thread_local FunctionContext currentFunctionContext{};
std::uintptr_t functionImageBase = 0;

struct PresentResidency final {
    id queue = nil;
    id set = nil;
};
thread_local PresentResidency presentResidency;

// This is only the DoPresent call site, not the queue's long-lived residency
// registrations. MTL4CommandQueue attaches its current sets to committed work;
// removing this set after DoPresent's final present/commit only changes later
// command buffers, including when already-submitted work is still in flight.
void addPresentResidency(id queue, SEL selector, id set) {
    using Message = void (*)(id, SEL, id);
    reinterpret_cast<Message>(objc_msgSend)(queue, selector, set);
    if (queue != nil && set != nil) presentResidency = {queue, set};
}

void finishPresentResidency() {
    const PresentResidency submitted = presentResidency;
    presentResidency = {};
    if (submitted.queue == nil) return;
    using Message = void (*)(id, SEL, id);
    reinterpret_cast<Message>(objc_msgSend)(submitted.queue,
        @selector(removeResidencySet:), submitted.set);
}

struct ContextScope final {
    explicit ContextScope(const Context& next) noexcept : previous(currentContext) {
        currentContext = next;
    }
    ~ContextScope() { currentContext = previous; }
    Context previous;
};

struct FunctionContextScope final {
    explicit FunctionContextScope(const FunctionContext& next) noexcept
        : previous(currentFunctionContext) {
        currentFunctionContext = next;
    }
    ~FunctionContextScope() { currentFunctionContext = previous; }
    FunctionContext previous;
};

struct Runtime final {
    Cache cache;
    FunctionCache functions;
};

Runtime& runtime() {
    static Runtime value;
    return value;
}

struct Invocation final {
    CreateNative original;
    const void* device;
    id descriptor;
    std::uint64_t options;
    bool reflectionRequested;

    NativeResult create() {
        id reflection = nil;
        NSError* error = nil;
        id state = original(device, descriptor, options,
            reflectionRequested ? &reflection : nullptr, &error);
        return NativeResult(state, reflection, error);
    }
};

id createPipeline(Api api, const void* device, id descriptor, std::uint64_t options,
                  id* reflection, NSError** error) {
    const auto original = reinterpret_cast<CreateNative>(originalFunctions[static_cast<std::size_t>(api)]);
    if (api == Api::Metal4Render) {
        if (@available(macOS 26.0, *)) {
            MTL4PipelineOptions* effective = [(MTL4RenderPipelineDescriptor*)descriptor options];
            effective.shaderReflection = static_cast<MTL4ShaderReflection>(effective.shaderReflection | 1);
        }
    }

    Key key;
    bool recognized = false;
    bool keyAllocationFailed = false;
    try {
        @try {
            recognized = makeKey(api, device, descriptor, options, reflection != nullptr, currentContext, key);
        } @catch (NSException* exception) {
            if (![exception.name isEqualToString:NSMallocException]) @throw;
            keyAllocationFailed = true;
        }
    } catch (const std::bad_alloc&) {
        keyAllocationFailed = true;
    }
    if (keyAllocationFailed || !recognized) {
        return original(device, descriptor, options, reflection, error);
    }

    Runtime& state = runtime();
    Invocation invocation{original, device, descriptor, options, reflection != nullptr};
    NativeResult result = state.cache.getOrCreate(
        device, key.bytes, key.resources, [&invocation] { return invocation.create(); });
    if (reflection != nullptr) *reflection = [result.takeReflection() autorelease];
    if (error != nullptr) *error = [result.takeError() autorelease];
    return result.takeState();
}

id createMetal4Render(const void* device, id descriptor, std::uint64_t options, id* reflection, NSError** error) {
    return createPipeline(Api::Metal4Render, device, descriptor, options, reflection, error);
}
id createRender(const void* device, id descriptor, std::uint64_t options, id* reflection, NSError** error) {
    return createPipeline(Api::Render, device, descriptor, options, reflection, error);
}
id createMesh(const void* device, id descriptor, std::uint64_t options, id* reflection, NSError** error) {
    return createPipeline(Api::Mesh, device, descriptor, options, reflection, error);
}
id createCompute(const void* device, id descriptor, std::uint64_t options, id* reflection, NSError** error) {
    return createPipeline(Api::Compute, device, descriptor, options, reflection, error);
}

struct ExtractionInvocation final {
    ExtractFunctionsEntry original;
    std::uintptr_t ignoredDevice;
    id library;
    std::uintptr_t rawFlag;
    const void* reflection;
    std::uintptr_t ignoredNames;
    MTLFunctionConstantValues* constants;

    static FunctionResult create(void* opaque) {
        auto& call = *static_cast<ExtractionInvocation*>(opaque);
        NSMutableArray* functions = nil;
        call.original(&functions, call.ignoredDevice, call.library, call.rawFlag,
            call.reflection, call.ignoredNames, call.constants);
        // The pinned native helper discards the whole array on NSError. Other
        // exceptions propagate; a nonnil return is a complete extraction.
        return FunctionResult(functions, functions != nil);
    }
};

__attribute__((noinline)) void* extractFunctions(
    void* output, std::uintptr_t ignoredDevice, id library,
    std::uintptr_t rawFlag, const void* reflection,
    std::uintptr_t ignoredNames, MTLFunctionConstantValues* constants) {
    const auto caller = reinterpret_cast<std::uintptr_t>(
        __builtin_extract_return_addr(__builtin_return_address(0)));
    const auto original = reinterpret_cast<ExtractFunctionsEntry>(
        originalFunctions[static_cast<std::size_t>(layout::Hook::ExtractFunctions)]);
    Runtime& state = runtime();
    ExtractionInvocation invocation {original, ignoredDevice, library, rawFlag,
        reflection, ignoredNames, constants};
    KeyBytes key;
    const void* device = nullptr;
    bool recognized = false;
    if (functionImageBase != 0 && caller >= functionImageBase) {
        try {
            recognized = makeFunctionExtractionKey(library, rawFlag, reflection, constants,
                caller - functionImageBase, currentFunctionContext, key, device);
        } catch (const std::bad_alloc&) {
            // A cache-key allocation must not replace the native operation.
        }
    }
    FunctionResult result = recognized ? state.functions.getOrCreate(
        device, key, library, &ExtractionInvocation::create, &invocation) :
        ExtractionInvocation::create(&invocation);
    NSMutableArray* functions = result.takeFunctions();
    std::memcpy(output, &functions, sizeof(functions));
    return output;
}

void loadGraphicsFunctions(const void* owner, const void* stages) {
    const FunctionContextScope scope({ContextKind::Graphics, owner, stages});
    const auto original = reinterpret_cast<LoadGraphicsFunctionsEntry>(
        originalFunctions[static_cast<std::size_t>(layout::Hook::LoadGraphicsFunctions)]);
    original(owner, stages);
}

id getRender(const void* owner, std::uint32_t dynamicFlags, std::uint64_t formats) {
    const ContextScope scope({ContextKind::Graphics, owner, dynamicFlags, formats});
    const auto original = reinterpret_cast<GetRender>(originalFunctions[static_cast<std::size_t>(layout::Hook::GetRender)]);
    return original(owner, dynamicFlags, formats);
}

void compileCompute(const void* owner, bool indirect) {
    const ContextScope scope({ContextKind::Compute, owner, static_cast<std::uint32_t>(indirect), 0});
    const FunctionContextScope functionScope({ContextKind::Compute, owner, nullptr});
    const auto original = reinterpret_cast<CompileCompute>(originalFunctions[static_cast<std::size_t>(layout::Hook::CompileCompute)]);
    original(owner, indirect);
}

void destroyWithFunctionCacheRetired(const void* device, const void* vtt) {
    Runtime& state = runtime();
    const auto original = reinterpret_cast<DestroyDevice>(originalFunctions[static_cast<std::size_t>(layout::Hook::DestroyDevice)]);
    state.cache.withDeviceRetired(device, original, vtt);
}

void destroyDevice(const void* device, const void* vtt) {
    Runtime& state = runtime();
    state.functions.withDeviceRetired(device, &destroyWithFunctionCacheRetired, vtt);
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
                textMatches = segment->vmaddr == 0 && segment->vmsize == 0x4ae000 &&
                    segment->fileoff == 0 && segment->filesize == 0x4ae000 && segment->initprot == 5;
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

void constructAdapter(void* adapter, void* factory, void* device) {
    using Constructor = void (*)(void*, void*, void*);
    const auto original = reinterpret_cast<Constructor>(
        originalFunctions[static_cast<std::size_t>(layout::Hook::ConstructAdapter)]);
    original(adapter, factory, device);
    video_memory::updateAdapterDescription(adapter);
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
        static_cast<void>(runtime());
        for (std::size_t index = 0; index < kHookCount; ++index) {
            originalFunctions[index] = reinterpret_cast<std::uintptr_t>(base + layout::kTrampolines[index]);
        }
        const void* rtOriginals[] = {
            reinterpret_cast<const void*>(originalFunctions[7]),
            reinterpret_cast<const void*>(originalFunctions[8]),
            reinterpret_cast<const void*>(originalFunctions[9]),
            reinterpret_cast<const void*>(originalFunctions[10]),
        };
        const RtHookEntryPoints rt = initializeRtHooks(base, rtOriginals);
        if (rt.createFunction == nullptr || rt.createCombinedAnyHitIntersectionFunction == nullptr ||
            rt.createIntersectionWrapperFunction == nullptr || rt.getAndRetainLibrary == nullptr) return;
        const std::array<std::uintptr_t, ngx::kHookCount> ngxOriginals = {
            originalFunctions[static_cast<std::size_t>(layout::Hook::ReplayTemporalScaleMPL)],
            originalFunctions[static_cast<std::size_t>(layout::Hook::EncodeTemporalScaleMTL)],
        };
        const auto ngxHooks = ngx::initializeHooks(ngxOriginals);
        for (const auto hook : ngxHooks) {
            if (hook == 0) return;
        }
        functionImageBase = reinterpret_cast<std::uintptr_t>(base);
        const display::Hooks displayHooks = display::initialize(reinterpret_cast<std::uintptr_t>(base),
            originalFunctions[static_cast<std::size_t>(layout::Hook::GetContainingOutput)],
            originalFunctions[static_cast<std::size_t>(layout::Hook::SetFullscreenState)]);
        dispatchTable = {
            reinterpret_cast<std::uintptr_t>(&createMetal4Render),
            reinterpret_cast<std::uintptr_t>(&createRender),
            reinterpret_cast<std::uintptr_t>(&createMesh),
            reinterpret_cast<std::uintptr_t>(&createCompute),
            reinterpret_cast<std::uintptr_t>(&getRender),
            reinterpret_cast<std::uintptr_t>(&compileCompute),
            reinterpret_cast<std::uintptr_t>(&destroyDevice),
            reinterpret_cast<std::uintptr_t>(rt.createFunction),
            reinterpret_cast<std::uintptr_t>(rt.createCombinedAnyHitIntersectionFunction),
            reinterpret_cast<std::uintptr_t>(rt.createIntersectionWrapperFunction),
            reinterpret_cast<std::uintptr_t>(rt.getAndRetainLibrary),
            ngxHooks[0],
            ngxHooks[1],
            displayHooks.getContainingOutput,
            displayHooks.setFullscreenState,
            reinterpret_cast<std::uintptr_t>(&constructAdapter),
            reinterpret_cast<std::uintptr_t>(&extractFunctions),
            reinterpret_cast<std::uintptr_t>(&loadGraphicsFunctions),
            displayHooks.presentFlush,
            reinterpret_cast<std::uintptr_t>(&d3dmetal::commitRecordedBatch),
            reinterpret_cast<std::uintptr_t>(&addPresentResidency),
            reinterpret_cast<std::uintptr_t>(&finishPresentResidency),
            reinterpret_cast<std::uintptr_t>(&video_memory::query),
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
