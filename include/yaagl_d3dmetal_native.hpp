#pragma once

#include "yaagl_d3dmetal_abi.hpp"
#include "yaagl_macho_symbols.hpp"

#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <objc/message.h>
#import <objc/runtime.h>

#include <dlfcn.h>
#include <cstdio>

namespace yaagl::native {

// The getter owns its layout. Do not read the unique_ptr ivar at self + 8.
inline void* pointerGetter(void* wrapper, SEL selector, const char** reason = nullptr) noexcept {
    if (reason) *reason = nullptr;
    const auto fail = [reason](const char* message) -> void* {
        if (reason) *reason = message;
        return nullptr;
    };
    if (!wrapper) return fail("missing-mde-wrapper");
    if (!selector) return fail("missing-mde-selector");
    id object = reinterpret_cast<id>(wrapper);
    Method method = class_getInstanceMethod(object_getClass(object), selector);
    if (!method) return fail("missing-mde-getter");
    if (method_getNumberOfArguments(method) != 2) return fail("mde-getter-argument-mismatch");
    const char* returnType = method_getTypeEncoding(method);
    if (!returnType) return fail("missing-mde-getter-type");
    returnType += std::strspn(returnType, "rnNoORV"); // Objective-C type qualifiers
    if (*returnType != '^') return fail("mde-getter-return-mismatch");
    using Getter = void* (*)(id, SEL);
    void* value = reinterpret_cast<Getter>(objc_msgSend)(object, selector);
    return value ? value : fail("empty-mde-getter-result");
}

inline void* fieldPointer(void* object, std::size_t offset) noexcept {
    return object ? readObject<void*>(static_cast<std::uint8_t*>(object) + offset) : nullptr;
}

inline bool describeWrappedMpl(void* wrapper, const CommandListLayout& layout,
                               MplState& state, void*& active,
                               const char** reason = nullptr) noexcept {
    state = {};
    if (reason) *reason = nullptr;
    const auto fail = [reason](const char* message) {
        if (reason) *reason = message;
        return false;
    };
    active = fieldPointer(wrapper, layout.active);
    if (!active) return fail("missing-active-command-list");
    void* mdeList = fieldPointer(active, layout.mdeList);
    void* allocatorOwner = fieldPointer(active, layout.allocator);
    if (!allocatorOwner) return fail("missing-command-allocator-owner");
    void* mdeAllocator = fieldPointer(allocatorOwner, layout.mdeAllocator);
    void* list = pointerGetter(mdeList, @selector(commandList), reason);
    if (!list) return false;
    void* allocator = pointerGetter(mdeAllocator, @selector(commandAllocator), reason);
    if (!allocator || !describeMpl(list, state, reason)) return false;
    if (state.allocator != allocator) {
        state = {};
        return fail("mde-allocator-identity-mismatch");
    }
    return true;
}

struct D3DMetalApi {
    using GetInternal = void (*)(void**, void*);
    using GetMetal = void* (*)(void*);
    using Release = void (*)(void**);
    using Dispatch = void (*)(void*, MTLSize);
    using EndEncoder = void (*)(void*, unsigned);
    using UseTexture = void (*)(void*, void*, unsigned, unsigned, TextureView);
    using ExtendLifetime = void (*)(void*, void*);
    using DynamicCast = void* (*)(void*, const void*, const void*, std::ptrdiff_t);
    using GetBufferEntry = void* (*)(void*);
    using UseBuffer = void (*)(void*, void*, unsigned, unsigned);

    GetInternal getInternalResource = nullptr;
    GetMetal getMetalTexture = nullptr;
    Release releaseTexture = nullptr;
    Release releaseCommandList = nullptr;
    Dispatch dispatch = nullptr;
    EndEncoder endEncoder = nullptr;
    UseTexture beginUseTexture = nullptr;
    UseTexture endUseTexture = nullptr;
    ExtendLifetime extendResourceLifetime = nullptr;
    DynamicCast dynamicCast = nullptr;
    GetBufferEntry getBufferEntry = nullptr;
    UseBuffer beginUseBuffer = nullptr;
    UseBuffer endUseBuffer = nullptr;
    const void* bufferIid = nullptr;
    const void* commandListIid = nullptr;
    const void* commandListType = nullptr;
    const void* mplType = nullptr;
    void* const* gfxTosIUnknown = nullptr;

    bool load(const mach_header_64* image) {
        // Pristine GPTK4 beta3. The patcher preserves LC_UUID and LC_SYMTAB.
        constexpr std::uint8_t expectedUuid[16] = {
            0x44,0x18,0xec,0x47,0xc3,0xf0,0x32,0x85,
            0xa0,0x56,0x15,0xd8,0x7d,0x78,0x5f,0xfe};
        *this = {};
        const macho::Symbols symbols(image);
        if (!symbols.valid() || image->cputype != CPU_TYPE_X86_64 ||
            std::memcmp(symbols.uuid().data(), expectedUuid, 16)) {
            std::fprintf(stderr, "[yaagl-native] image-contract: %s\n",
                symbols.valid() ? "unsupported-architecture-or-uuid" : symbols.error());
            return false;
        }
        bool complete = true;
        const auto find = [&](const char* name, bool code) -> void* {
            const char* error = nullptr;
            void* value = symbols.find(name, code, &error);
            if (!value) {
                complete = false;
                std::fprintf(stderr, "[yaagl-native] symbol=%s reason=%s\n", name, error);
            }
            // GUID/RTTI definitions may be weak-coalesced by dyld. Native NGX
            // loads these through bound GOT entries, so prefer the loader's
            // canonical definition after proving the name exists in this image.
            if (value && !code && name[0] == '_') {
                if (void* bound = dlsym(RTLD_DEFAULT, name + 1)) value = bound;
            }
            return value;
        };
        getInternalResource = reinterpret_cast<GetInternal>(find(
            "__ZN3ngx19GetInternalResourceI14ID3D12Resource12D3D12TextureEEN9Microsoft3WRL6ComPtrIT0_EEPT_", true));
        getMetalTexture = reinterpret_cast<GetMetal>(find(
            "__ZN3ngx15GetMetalTextureI12D3D12TextureEEPU21objcproto10MTLTexture11objc_objectPT_", true));
        releaseTexture = reinterpret_cast<Release>(find(
            "__ZN9Microsoft3WRL6ComPtrI12D3D12TextureE15InternalReleaseEv", true));
        releaseCommandList = reinterpret_cast<Release>(find(
            "__ZN9Microsoft3WRL6ComPtrI24D3D12GraphicsCommandListE15InternalReleaseEv", true));
        dispatch = reinterpret_cast<Dispatch>(find("__ZN14MPLCommandList8DispatchE7MTLSize", true));
        endEncoder = reinterpret_cast<EndEncoder>(find(
            "__ZN14MPLCommandList10EndEncoderE14MPLEncoderType", true));
        beginUseTexture = reinterpret_cast<UseTexture>(find(
            "__ZN14MPLCommandList15BeginUseTextureEPU21objcproto10MTLTexture11objc_object8MPLStage9MPLAccess14MPLTextureView", true));
        endUseTexture = reinterpret_cast<UseTexture>(find(
            "__ZN14MPLCommandList13EndUseTextureEPU21objcproto10MTLTexture11objc_object8MPLStage9MPLAccess14MPLTextureView", true));
        extendResourceLifetime = reinterpret_cast<ExtendLifetime>(find(
            "__ZN19MPLCommandAllocator22ExtendResourceLifetimeEPU22objcproto11MTLResource11objc_object", true));
        beginUseBuffer = reinterpret_cast<UseBuffer>(find(
            "__ZN14MPLCommandList14BeginUseBufferEPU19objcproto9MTLBuffer11objc_object8MPLStage9MPLAccess", true));
        endUseBuffer = reinterpret_cast<UseBuffer>(find(
            "__ZN14MPLCommandList12EndUseBufferEPU19objcproto9MTLBuffer11objc_object8MPLStage9MPLAccess", true));
        getBufferEntry = reinterpret_cast<GetBufferEntry>(find(
            "__ZN11D3D12Buffer15GetCurrentEntryEv", true));
        bufferIid = find("__GUID_c33e341a_1fd8_4680_9b9a_738cb0303cdf", false);
        commandListIid = find("__GUID_96878c49_f9cd_481d_a487_04ea64a393d1", false);
        commandListType = find("__ZTI24D3D12GraphicsCommandList", false);
        mplType = find("__ZTI27D3D12GraphicsCommandListMPL", false);
        gfxTosIUnknown = reinterpret_cast<void* const*>(find(
            "__ZN15GFXTOSInterface13IUnknownIfaceE", false));
        // This is the platform C++ ABI function, not a private D3DMetal stub.
        dynamicCast = reinterpret_cast<DynamicCast>(dlsym(RTLD_DEFAULT, "__dynamic_cast"));
        if (!dynamicCast) {
            complete = false;
            std::fprintf(stderr, "[yaagl-native] symbol=__dynamic_cast reason=unavailable\n");
        }
        if (!complete) *this = {};
        return complete;
    }

    void* acquirePrivateInterface(void* publicList, const void* iid) const noexcept {
        if (!publicList || !gfxTosIUnknown) return nullptr;
        void* owner = nullptr;
        if (*gfxTosIUnknown) {
            // GFXTOS is a Windows-facing wrapper with its native bridge in the
            // preceding pointer. Native Evaluate uses this same interface call.
            void* bridge = readObject<void*>(static_cast<std::uint8_t*>(publicList) - sizeof(void*));
            void** vtable = readObject<void**>(bridge);
            if (!vtable || !vtable[3]) return nullptr;
            using GetInterface = void* (*)(void*, const void*);
            owner = reinterpret_cast<GetInterface>(vtable[3])(bridge, iid);
            if (!owner) return nullptr;
            // The returned pointer is borrowed. Native AddRef is Microsoft ABI,
            // whereas the GetInterface bridge above is System V ABI.
            void** ownerTable = readObject<void**>(owner);
            if (!ownerTable) return nullptr;
            const auto adjustment = readObject<std::ptrdiff_t>(ownerTable - 3);
            void* unknown = static_cast<std::uint8_t*>(owner) + adjustment;
            void** unknownTable = readObject<void**>(unknown);
            if (!unknownTable || !unknownTable[4]) return nullptr;
            using AddRef = std::uint32_t (__attribute__((ms_abi)) *)(void*);
            reinterpret_cast<AddRef>(unknownTable[4])(unknown);
        } else {
            void** vtable = readObject<void**>(publicList);
            if (!vtable || !vtable[0]) return nullptr;
            using QueryInterface = std::int32_t (__attribute__((ms_abi)) *)(void*, const void*, void**);
            if (reinterpret_cast<QueryInterface>(vtable[0])(publicList, iid, &owner) < 0) {
                if (owner) releasePrivateInterface(owner);
                return nullptr;
            }
        }
        return owner;
    }

    void* acquireCommandList(void* publicList) const noexcept {
        return acquirePrivateInterface(publicList, commandListIid);
    }

    static void releasePrivateInterface(void* owner) noexcept {
        if (!owner) return;
        void** table = readObject<void**>(owner);
        const auto adjustment = readObject<std::ptrdiff_t>(table - 3);
        void* unknown = static_cast<std::uint8_t*>(owner) + adjustment;
        void** unknownTable = readObject<void**>(unknown);
        using Release = std::uint32_t (__attribute__((ms_abi)) *)(void*);
        reinterpret_cast<Release>(unknownTable[5])(unknown);
    }

    void* castCommandList(void* owner) const noexcept {
        // -1 is the standard no-hint mode. The C++ runtime derives the base
        // displacement from RTTI.
        return owner ? dynamicCast(owner, commandListType, mplType, -1) : nullptr;
    }
};

} // namespace yaagl::native
