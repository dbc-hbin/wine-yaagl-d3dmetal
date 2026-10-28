#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <bcrypt.h>
#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <new>
#include <string>
#include <vector>

#include "../../include/yaagl_fsr3_lifetime.h"
#include "../../d3dmetal-sidecar/third-party/fidelityfx/Kits/FidelityFX/framegeneration/fsr3/include/ffx_provider_fsr3framegeneration.h"
#include "../../d3dmetal-sidecar/third-party/fidelityfx/Kits/FidelityFX/framegeneration/fsr3/internal/ffx_opticalflow_shaderblobs.h"
#include "../../d3dmetal-sidecar/third-party/fidelityfx/Kits/FidelityFX/framegeneration/fsr3/internal/ffx_frameinterpolation_shaderblobs.h"
#include "../../d3dmetal-sidecar/third-party/fidelityfx/Kits/FidelityFX/backend/dx12/ffx_dx12.h"
#include "../../d3dmetal-sidecar/third-party/fidelityfx/Kits/FidelityFX/api/internal/ffx_message.h"

#ifndef YAAGL_FSR3_NATIVE_SHA256
#error Build with scripts/build-fsr3-lifetime.sh and the canonical native image identity.
#endif

namespace {

// Pinned FSR3 3.1.6 native boundary, recovered against the complete file digest.
// Native selection remains authoritative; only its selected Create slot changes.
constexpr std::uint32_t kProviderRva = 0x2472438;
constexpr std::uint32_t kVtableRva = 0x2467498;
constexpr std::uint32_t kCreateRva = 0xf25c0;
constexpr std::uint32_t kOpticalflowBlobRva = 0x1039a0;
constexpr std::uint32_t kInterpolationBlobRva = 0x1012f0;
constexpr std::uint32_t kSwapchainConfigRva = 0x7850;
constexpr std::uint32_t kSwapchainAbiRva = 0x7700;
constexpr std::uint32_t kPrintMessageRva = 0x120a0;
constexpr char kNativeDigest[] = YAAGL_FSR3_NATIVE_SHA256;
static_assert(sizeof(kNativeDigest) == 65);
static_assert(sizeof(void*) == 8);
static_assert(offsetof(FfxFrameGenerationConfig, swapChain) == 0x10);
static_assert(sizeof(FfxShaderBlob) == 0x108);
static_assert(offsetof(FfxShaderBlob, data) == 0);
static_assert(offsetof(FfxShaderBlob, size) == 8);
static_assert(offsetof(FfxShaderBlob, boundConstantBufferNames) == 0x28);
static_assert(offsetof(FfxShaderBlob, boundSRVTextureNames) == 0x48);
static_assert(offsetof(FfxShaderBlob, boundUAVTextureNames) == 0x68);
static_assert(offsetof(FfxShaderBlob, boundSRVBufferNames) == 0x88);
static_assert(offsetof(FfxShaderBlob, boundUAVBufferNames) == 0xa8);
static_assert(offsetof(FfxShaderBlob, boundSamplerNames) == 0xc8);
static_assert(offsetof(FfxShaderBlob, boundRTAccelerationStructureNames) == 0xe8);
static_assert(offsetof(FfxShaderBlob, boundRTAccelerationStructureSpaces) == 0x100);

using NativeCreate = ffxReturnCode_t (*)(void*, ffxContext*, ffxCreateContextDescHeader*, const ffxAllocationCallbacks**);
struct ProviderVtable {
    void* (*destroyProvider)(void*, unsigned);
    bool (*canProvide)(const void*, std::uint64_t);
    bool (*isSupported)(const void*, void*);
    NativeCreate create;
    ffxReturnCode_t (*destroy)(void*, ffxContext*, const ffxAllocationCallbacks**);
    ffxReturnCode_t (*configure)(const void*, ffxContext*, const ffxConfigureDescHeader*);
    ffxReturnCode_t (*query)(const void*, ffxContext*, ffxQueryDescHeader*);
    ffxReturnCode_t (*dispatch)(const void*, ffxContext*, const ffxDispatchDescHeader*);
};
struct Provider {
    const ProviderVtable* table;
    std::size_t references;
    std::uint64_t id;
    std::uint64_t effect;
    const char* version;
};
static_assert(offsetof(ProviderVtable, create) == 0x18);
static_assert(offsetof(ProviderVtable, dispatch) == 0x38);
static_assert(offsetof(Provider, references) == 8);
static_assert(offsetof(Provider, id) == 0x10);
static_assert(offsetof(Provider, effect) == 0x18);
static_assert(offsetof(Provider, version) == 0x20);
static_assert(sizeof(Provider) == 0x28);

std::mutex installationMutex;
ffxProvider_Fsr3FrameGeneration sourceProvider;
Provider proxy{};
const unsigned char* nativeBase;
NativeCreate originalCreate;
PfnFfxConfigure nativeConfigure;

template<class T> T nativeFunction(std::uint32_t rva) {
    return reinterpret_cast<T>(const_cast<unsigned char*>(nativeBase) + rva);
}

template<class Operation> ffxReturnCode_t invoke(Operation&& operation) noexcept {
    try { return operation(); }
    catch (const std::bad_alloc&) { return FFX_API_RETURN_ERROR_MEMORY; }
    catch (...) { return FFX_API_RETURN_ERROR_RUNTIME_ERROR; }
}

// This provider is permanently pinned and has RefCount=1. Native APIs never
// own its destruction; accepting deletion could invalidate live contexts.
void* destroyProvider(void*, unsigned) { std::abort(); }
bool canProvide(const void*, std::uint64_t type) { return sourceProvider.CanProvide(type); }
bool isSupported(const void*, void* device) { return sourceProvider.IsSupported(device); }

ffxReturnCode_t create(void* provider, ffxContext* context, ffxCreateContextDescHeader* desc,
                       const ffxAllocationCallbacks** allocation) {
    const auto* selected = static_cast<const Provider*>(provider);
    if (provider != nativeBase + kProviderRva || selected->id != proxy.id || selected->effect != proxy.effect)
        return originalCreate(provider, context, desc, allocation);
    return invoke([&] {
        Allocator allocator{*allocation};
        const auto result = sourceProvider.CreateContext(context, desc, allocator);
        if (result == FFX_API_RETURN_OK) {
            // Only native entrypoints read this header. Source methods are
            // invoked directly below, never through a cross-compiler vtable.
            static_cast<InternalContextHeader*>(*context)->provider = reinterpret_cast<ffxProvider*>(&proxy);
        }
        return result;
    });
}
ffxReturnCode_t destroy(void*, ffxContext* context, const ffxAllocationCallbacks** allocation) {
    return invoke([&] { Allocator allocator{*allocation}; return sourceProvider.DestroyContext(context, allocator); });
}
ffxReturnCode_t configure(const void*, ffxContext* context, const ffxConfigureDescHeader* desc) {
    return invoke([&] { return sourceProvider.Configure(context, desc); });
}
ffxReturnCode_t query(const void*, ffxContext* context, ffxQueryDescHeader* desc) {
    return invoke([&] { return sourceProvider.Query(context, desc); });
}
ffxReturnCode_t dispatch(const void*, ffxContext* context, const ffxDispatchDescHeader* desc) {
    return invoke([&] { return sourceProvider.Dispatch(context, desc); });
}
const ProviderVtable providerTable{destroyProvider, canProvide, isSupported, create, destroy, configure, query, dispatch};

int authenticate(HMODULE module) {
    std::wstring path(MAX_PATH, L'\0');
    for (;;) {
        const auto length = GetModuleFileNameW(module, path.data(), static_cast<DWORD>(path.size()));
        if (!length) return YAAGL_FSR3_INSTALL_UNSUPPORTED;
        if (length < path.size()) { path.resize(length); break; }
        path.resize(path.size() * 2);
    }
    struct File {
        HANDLE file = INVALID_HANDLE_VALUE;
        HANDLE mapping = nullptr;
        unsigned char* bytes = nullptr;
        BCRYPT_ALG_HANDLE hash = nullptr;
        BCRYPT_HASH_HANDLE operation = nullptr;
        ~File() {
            if (operation) BCryptDestroyHash(operation);
            if (hash) BCryptCloseAlgorithmProvider(hash, 0);
            if (bytes) UnmapViewOfFile(bytes);
            if (mapping) CloseHandle(mapping);
            if (file != INVALID_HANDLE_VALUE) CloseHandle(file);
        }
    } file;
    file.file = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    LARGE_INTEGER size{};
    if (file.file == INVALID_HANDLE_VALUE || !GetFileSizeEx(file.file, &size)) return YAAGL_FSR3_INSTALL_ERROR;
    if (size.QuadPart < static_cast<LONGLONG>(sizeof(IMAGE_DOS_HEADER)) || size.QuadPart > MAXDWORD)
        return YAAGL_FSR3_INSTALL_UNSUPPORTED;
    file.mapping = CreateFileMappingW(file.file, nullptr, PAGE_READONLY, 0, 0, nullptr);
    if (!file.mapping) return YAAGL_FSR3_INSTALL_ERROR;
    file.bytes = static_cast<unsigned char*>(MapViewOfFile(file.mapping, FILE_MAP_READ, 0, 0, 0));
    if (!file.bytes || BCryptOpenAlgorithmProvider(&file.hash, BCRYPT_SHA256_ALGORITHM, nullptr, 0) < 0)
        return YAAGL_FSR3_INSTALL_ERROR;
    std::array<unsigned char, (sizeof(kNativeDigest) - 1) / 2> digest{};
    if (BCryptCreateHash(file.hash, &file.operation, nullptr, 0, nullptr, 0, 0) < 0 ||
        BCryptHashData(file.operation, file.bytes, static_cast<ULONG>(size.QuadPart), 0) < 0 ||
        BCryptFinishHash(file.operation, digest.data(), digest.size(), 0) < 0)
        return YAAGL_FSR3_INSTALL_ERROR;
    char hex[sizeof(kNativeDigest)];
    for (std::size_t index = 0; index < digest.size(); ++index)
        std::snprintf(hex + index * 2, sizeof(hex) - index * 2, "%02x", digest[index]);
    if (std::memcmp(hex, kNativeDigest, sizeof(hex))) return YAAGL_FSR3_INSTALL_UNSUPPORTED;

    // The entire source file is authenticated before its PE offsets are used.
    const auto* diskDos = reinterpret_cast<const IMAGE_DOS_HEADER*>(file.bytes);
    const auto* disk = reinterpret_cast<const IMAGE_NT_HEADERS64*>(file.bytes + diskDos->e_lfanew);
    const auto* memory = reinterpret_cast<const unsigned char*>(module);
    const auto* liveDos = reinterpret_cast<const IMAGE_DOS_HEADER*>(memory);
    if (liveDos->e_magic != diskDos->e_magic || liveDos->e_lfanew != diskDos->e_lfanew)
        return YAAGL_FSR3_INSTALL_UNSUPPORTED;
    const auto* live = reinterpret_cast<const IMAGE_NT_HEADERS64*>(memory + diskDos->e_lfanew);
    if (live->Signature != disk->Signature || live->FileHeader.Machine != IMAGE_FILE_MACHINE_AMD64 ||
        live->OptionalHeader.Magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC ||
        live->OptionalHeader.SizeOfImage != disk->OptionalHeader.SizeOfImage ||
        live->FileHeader.TimeDateStamp != disk->FileHeader.TimeDateStamp)
        return YAAGL_FSR3_INSTALL_UNSUPPORTED;
    const auto* sections = IMAGE_FIRST_SECTION(disk);
    const auto atRva = [&](DWORD rva) -> const unsigned char* {
        if (rva < disk->OptionalHeader.SizeOfHeaders) return file.bytes + rva;
        for (unsigned index = 0; index < disk->FileHeader.NumberOfSections; ++index) {
            const auto& section = sections[index];
            if (rva >= section.VirtualAddress && rva - section.VirtualAddress < section.SizeOfRawData)
                return file.bytes + section.PointerToRawData + rva - section.VirtualAddress;
        }
        return nullptr;
    };
    const auto& relocation = disk->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_BASERELOC];
    const auto* relocations = atRva(relocation.VirtualAddress);
    const auto delta = reinterpret_cast<std::uintptr_t>(module) - disk->OptionalHeader.ImageBase;
    for (unsigned index = 0; index < disk->FileHeader.NumberOfSections; ++index) {
        const auto& section = sections[index];
        if (!(section.Characteristics & IMAGE_SCN_MEM_EXECUTE)) continue;
        const auto count = std::min(section.SizeOfRawData, section.Misc.VirtualSize);
        std::vector<unsigned char> expected(file.bytes + section.PointerToRawData,
                                            file.bytes + section.PointerToRawData + count);
        for (DWORD position = 0; position < relocation.Size;) {
            const auto* block = reinterpret_cast<const IMAGE_BASE_RELOCATION*>(relocations + position);
            const auto* entries = reinterpret_cast<const WORD*>(block + 1);
            for (DWORD entry = 0; entry < (block->SizeOfBlock - sizeof(*block)) / sizeof(*entries); ++entry) {
                const auto type = entries[entry] >> 12;
                const auto rva = block->VirtualAddress + (entries[entry] & 0xfff);
                if (type == IMAGE_REL_BASED_ABSOLUTE || rva < section.VirtualAddress || rva >= section.VirtualAddress + count)
                    continue;
                if (type != IMAGE_REL_BASED_DIR64 || rva - section.VirtualAddress + sizeof(std::uint64_t) > count)
                    return YAAGL_FSR3_INSTALL_UNSUPPORTED;
                std::uint64_t value;
                std::memcpy(&value, expected.data() + rva - section.VirtualAddress, sizeof(value));
                value += delta;
                std::memcpy(expected.data() + rva - section.VirtualAddress, &value, sizeof(value));
            }
            position += block->SizeOfBlock;
        }
        if (std::memcmp(memory + section.VirtualAddress, expected.data(), count))
            return YAAGL_FSR3_INSTALL_UNSUPPORTED;
    }
    const auto* table = reinterpret_cast<const ProviderVtable*>(memory + kVtableRva);
    if (reinterpret_cast<const void*>(table->create) != memory + kCreateRva)
        return YAAGL_FSR3_INSTALL_UNSUPPORTED;
    return YAAGL_FSR3_INSTALL_OK;
}

int install(HMODULE module) {
    std::lock_guard lock(installationMutex);
    if (nativeBase) {
        if (nativeBase != reinterpret_cast<const unsigned char*>(module)) return YAAGL_FSR3_INSTALL_UNSUPPORTED;
        const auto* table = reinterpret_cast<const ProviderVtable*>(nativeBase + kVtableRva);
        if (table->create == create) return YAAGL_FSR3_INSTALL_OK;
    }
    const auto verified = authenticate(module);
    if (verified != YAAGL_FSR3_INSTALL_OK) return verified;
    HMODULE pinned;
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
                           reinterpret_cast<LPCWSTR>(module), &pinned) ||
        !GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
                           reinterpret_cast<LPCWSTR>(&install), &pinned))
        return YAAGL_FSR3_INSTALL_ERROR;
    if (!nativeBase) {
        nativeBase = reinterpret_cast<const unsigned char*>(module);
        originalCreate = nativeFunction<NativeCreate>(kCreateRva);
        nativeConfigure = reinterpret_cast<PfnFfxConfigure>(GetProcAddress(module, "ffxConfigure"));
        proxy = {&providerTable, 1, sourceProvider.GetId(), FFX_API_EFFECT_ID_FRAMEGENERATION, sourceProvider.GetVersionName()};
    }
    auto* slot = reinterpret_cast<void**>(const_cast<unsigned char*>(nativeBase) + kVtableRva + offsetof(ProviderVtable, create));
    DWORD protection, unused;
    if (!VirtualProtect(slot, sizeof(*slot), PAGE_READWRITE, &protection)) return YAAGL_FSR3_INSTALL_ERROR;
    const auto previous = InterlockedCompareExchangePointer(slot, reinterpret_cast<void*>(create), reinterpret_cast<void*>(originalCreate));
    if (!VirtualProtect(slot, sizeof(*slot), protection, &unused)) {
        InterlockedCompareExchangePointer(slot, reinterpret_cast<void*>(originalCreate), reinterpret_cast<void*>(create));
        return YAAGL_FSR3_INSTALL_ERROR;
    }
    return previous == reinterpret_cast<void*>(originalCreate) ? YAAGL_FSR3_INSTALL_OK : YAAGL_FSR3_INSTALL_UNSUPPORTED;
}

} // namespace

extern "C" __declspec(dllexport) int yaagl_fsr3_install(void* module) {
    try { return install(static_cast<HMODULE>(module)); }
    catch (...) { return YAAGL_FSR3_INSTALL_ERROR; }
}

FfxErrorCode opticalflowGetPermutationBlobByIndex(FfxOpticalflowPass pass, std::uint32_t options, FfxShaderBlob* output) {
    return nativeFunction<FfxErrorCode (*)(FfxOpticalflowPass, std::uint32_t, FfxShaderBlob*)>(kOpticalflowBlobRva)(pass, options, output);
}
FfxErrorCode frameInterpolationGetPermutationBlobByIndex(FfxFrameInterpolationPass pass, std::uint32_t options, FfxShaderBlob* output) {
    return nativeFunction<FfxErrorCode (*)(FfxFrameInterpolationPass, std::uint32_t, FfxShaderBlob*)>(kInterpolationBlobRva)(pass, options, output);
}
FfxErrorCode ffxSetFrameGenerationConfigToSwapchainDX12(const FfxFrameGenerationConfig* config) {
    return nativeFunction<FfxErrorCode (*)(const FfxFrameGenerationConfig*)>(kSwapchainConfigRva)(config);
}
FfxABIVersion ffxGetSwapchainABIDX12(FfxSwapchain swapchain) {
    return nativeFunction<FfxABIVersion (*)(FfxSwapchain)>(kSwapchainAbiRva)(swapchain);
}
void ffxPrintMessage(std::uint32_t type, const wchar_t* message) {
    nativeFunction<void (*)(std::uint32_t, const wchar_t*)>(kPrintMessageRva)(type, message);
}
void ffxSetPrintMessageCallback(ffxMessageCallback callback, std::uint32_t level) {
    ffxConfigureDescGlobalDebug1 desc{};
    desc.header.type = FFX_API_CONFIGURE_DESC_TYPE_GLOBALDEBUG1;
    desc.fpMessage = callback;
    desc.debugLevel = level;
    nativeConfigure(nullptr, &desc.header);
}
