#import "function-hooks.hpp"

#include <cstring>

namespace yaagl::pso {
namespace {

constexpr std::uint32_t kFunctionKeyDomain = 0x464e4b31; // "FNK1"

enum class Helpers : std::uint8_t { None, Tessellation, FirstFunction };
enum class Constants : std::uint8_t { None, Tessellation, StreamOutput };

template<typename T>
T load(const void* base, const std::size_t offset) {
    T value;
    std::memcpy(&value, static_cast<const std::uint8_t*>(base) + offset, sizeof(value));
    return value;
}

} // namespace

bool makeFunctionExtractionKey(
    id library, const std::uintptr_t rawFlag, const void* reflection,
    MTLFunctionConstantValues* constants, const std::uintptr_t callerOffset,
    const FunctionContext& context, KeyBytes& key, const void*& device) {
    key.clear();
    device = nullptr;
    if (library == nil || context.owner == nullptr) return false;

    // Checking the native caller as well as TLS prevents an unrelated nested
    // extraction (notably RT) from inheriting graphics/compute eligibility.
    if (context.kind == ContextKind::Graphics) {
        switch (callerOffset) {
            case 0x1107d5: case 0x110987: case 0x110a5b:
            case 0x110d9b: case 0x110e56: case 0x110f1f:
            case 0x111523: case 0x1115e6: case 0x1116d8:
                break;
            default: return false;
        }
        if (context.stages == nullptr ||
            load<const void*>(context.owner, 0x30) != context.stages) return false;
    } else if (context.kind == ContextKind::Compute) {
        if (callerOffset != 0x10f0ed || constants != nil) return false;
    } else {
        return false;
    }

    const void* ownerDevice = load<const void*>(context.owner, 0x8);
    if (ownerDevice == nullptr) return false;

    Helpers helpers = Helpers::None;
    if (reflection != nullptr) {
        const std::uint8_t kind = load<std::uint8_t>(reflection, 0xf8);
        if (kind == 3) {
            helpers = Helpers::Tessellation;
        } else if (kind == 8 || kind == 9) {
            // Leave invalid native variants on the original throwing path.
            if (load<std::uint32_t>(reflection, 0x148) != 11) return false;
            if (load<std::uint8_t>(reflection, 0x101) != 0 ||
                load<std::uint8_t>(reflection, 0x100) == 1) {
                helpers = Helpers::FirstFunction;
            }
        }
    }

    Constants constantKind = Constants::None;
    std::uint32_t vertexOutputSize = 0;
    std::uint32_t tessellationFactorBits = 0;
    if (constants != nil) {
        if (context.kind != ContextKind::Graphics) return false;
        const void* vertex = load<const void*>(context.stages, 0x28);
        if (vertex == nullptr || load<std::uint32_t>(vertex, 0x168) != 1) return false;
        const void* specializedStage = nullptr;
        if (callerOffset == 0x110987) {
            specializedStage = load<const void*>(context.stages, 0x10);
            if (load<const void*>(context.stages, 0x18) == nullptr) return false;
            constantKind = Constants::Tessellation;
            // MTLDataTypeFloat reads four bytes at this unaligned owner field.
            // Preserve the exact representation, including NaN payloads.
            tessellationFactorBits = load<std::uint32_t>(context.owner, 0x2b9);
        } else if (callerOffset == 0x111523) {
            specializedStage = load<const void*>(context.stages, 0x20);
            if (specializedStage == nullptr ||
                load<std::uint32_t>(specializedStage, 0x168) != 6 ||
                load<std::uint8_t>(specializedStage, 0x139) != 0 ||
                load<std::uint8_t>(specializedStage, 0x70) != 1) return false;
            constantKind = Constants::StreamOutput;
        } else {
            return false;
        }
        if (specializedStage == nullptr ||
            reflection != static_cast<const std::uint8_t*>(specializedStage) + 0x20) return false;
        const auto* librarySlot = reinterpret_cast<const std::uintptr_t*>(
            static_cast<const std::uint8_t*>(specializedStage) + 0x178);
        if (__atomic_load_n(librarySlot, __ATOMIC_ACQUIRE) !=
            reinterpret_cast<std::uintptr_t>(library)) return false;
        vertexOutputSize = load<std::uint32_t>(vertex, 0x128);
    }

    // Library identity fixes the immutable function-name enumeration/order.
    // The cache retains that library for as long as this key is retained.
    const auto libraryIdentity = reinterpret_cast<std::uintptr_t>(library);
    const bool compileFlag = (rawFlag & 0xff) != 0;
    key.append(&kFunctionKeyDomain, sizeof(kFunctionKeyDomain));
    key.append(&libraryIdentity, sizeof(libraryIdentity));
    key.append(&compileFlag, sizeof(compileFlag));
    key.append(&helpers, sizeof(helpers));
    key.append(&constantKind, sizeof(constantKind));
    if (constantKind != Constants::None) {
        // The pinned call sites fix names and MTLDataTypes: UInt output size,
        // followed only for tessellation by Float max tessellation factor.
        key.append(&vertexOutputSize, sizeof(vertexOutputSize));
        if (constantKind == Constants::Tessellation) {
            key.append(&tessellationFactorBits, sizeof(tessellationFactorBits));
        }
    }
    device = ownerDevice;
    return true;
}

} // namespace yaagl::pso
