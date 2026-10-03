#pragma once

#import <Metal/Metal.h>

#include <cstdint>

#include "key.hpp"

namespace yaagl::pso {

// ExtractDXILFunctions returns a nontrivial one-pointer owning wrapper through
// RDI and also returns that storage address in RAX. RSI and R9 are optimized
// away in the pinned image: preserve their raw registers, not C++ references.
// The flag is read through CL; constants is the first stack argument.
using ExtractFunctionsEntry = void* (*)(
    void* result, std::uintptr_t ignoredDevice, id library,
    std::uintptr_t rawFlag, const void* reflection,
    std::uintptr_t ignoredNames, MTLFunctionConstantValues* constants);
using LoadGraphicsFunctionsEntry = void (*)(const void* owner, const void* stages);

struct FunctionContext final {
    ContextKind kind = ContextKind::None;
    const void* owner = nullptr;
    const void* stages = nullptr;
};

// Only the verified graphics/compute call sites are eligible. In particular,
// RT extraction must remain private to its per-function provenance capture.
// Successful keys contain effective extraction inputs, never unused registers
// or an opaque constant-values object's address. device is the outer owner's
// D3DMDevice, not the unused extraction argument or its MTLDevice.
[[nodiscard]] bool makeFunctionExtractionKey(
    id library, std::uintptr_t rawFlag, const void* reflection,
    MTLFunctionConstantValues* constants, std::uintptr_t callerOffset,
    const FunctionContext& context, KeyBytes& key, const void*& device);

} // namespace yaagl::pso
