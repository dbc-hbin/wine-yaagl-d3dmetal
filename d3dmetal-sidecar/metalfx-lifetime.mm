#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <MetalFX/MetalFX.h>
#import <objc/runtime.h>

#include <CommonCrypto/CommonDigest.h>
#include <dlfcn.h>
#include <mach-o/loader.h>

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <mutex>

#include "metalfx-lifetime.hpp"

namespace yaagl::pso::metalfx_lifetime {
namespace {

#if defined(__x86_64__)

// Complete original-function digests from apple-lifetime-audit/metalfx/native-evidence.json.
// The checks cover the ownership/retirement contracts as well as the replaced IMPs.
struct Span {
    std::size_t offset;
    CC_LONG size;
    const char* sha256;
};
constexpr Span spans[] = {
    {0x52d84, 38, "6be5aeb71bcb44436b16f2a482611e506126b77d26760a26c560dd8675040872"},
    {0x5589c, 318, "a6490b4a3bd6471fffcead58a41af20f77b7fda512c8899fb42c5414e968abd5"},
    {0x5893a, 630, "c03ae1f511555ed1ed019ab6505c01d4d28738f09be163634ce25760b6b2894c"},
    {0x58f4a, 126, "5235259e027e2dfd195130699603901d887a2474ad086b2ce638d1bb057b0cce"},
    {0x58fc8, 188, "c50691a7ed829c49d4cbff335f611e94facf46b14fdd857c83e959b9fba424f6"},
    {0x59084, 9516, "72504671205f3f39a14f06949d38a542d01b7419e0f93c1d12b9cf27f4c15311"},
    {0x5b704, 788, "bcfc5e0ff6c420275bdebaa5b348269dcfceb552ee3dd4fd4dc042128cec305c"},
    {0x5ba34, 1672, "9ac922aca14b9bcbbdfe45dec78c3e120fff3f155fac8f716831cf104d6bb1dd"},
    {0x5e852, 418, "47028196d400bcf161d1025004ce51bcda08b0597e1aeeea2dc937f7f47baf86"},
    {0x5e9f4, 70, "a9c7a11e61a938daeaff1a8f0c7821acea414499412eae01054691bcd64dbbc4"},
    {0x5f1d6, 1302, "f9ed5ed5af44f85b560d947ebe225bb6427190ed4e194166568c8ac1a6f036db"},
    {0x65852, 486, "8823a5834b569c3a827aa1d4bbcd80e73ef9cb21c54929cc3bf2c9a31cb0de37"},
    {0x7fe7f, 514, "13c63aa0c18b358b68d5a631394e17988d59e7fdbb3aee25adf5897d0538d20a"},
    {0x80081, 161, "bcd2f85bdf7713d2137bebdcaeb2ff8262e3aa6282560fad3589ef7d0c997883"},
    {0x8014f, 47, "c06e94cb62eff9fda615dc89abfd168c8fd70f765a220949ef2c48ff66d2d0eb"},
    {0x80196, 56, "019655f74a5a44abb1da0aeac1c87215158c423e303e2e562c7cff5d1789019a"},
    {0xa128a, 3278, "d4f1c865fa8abbe34dc8ee5b118476b677aad5f0a6cee5a27c9a2689499f3caa"},
    {0xa1f58, 107, "ba77fcbd47bbdd2e198e35ce0d9876d64c3f11ba3d0c0ee022aec126d35b782a"},
    {0xa5294, 253, "27094b96f0d722f9f7aef902ae4c6e4fbdaa2e51abb5297856bee275f85e4462"},
    {0xa5ade, 5044, "d14d5db759e6d1e47238fce08604120e8b78debaee183dc8eceabba93073b1be"},
};
constexpr std::uint8_t metalfxUuid[] = {
    0x90, 0xe6, 0x5f, 0x5a, 0x11, 0x16, 0x3e, 0xba,
    0x98, 0x40, 0x38, 0x9f, 0xd0, 0x5f, 0x47, 0xfb,
};
constexpr std::uint8_t libcxxabiUuid[] = {
    0x6b, 0x6b, 0x49, 0x5c, 0xeb, 0xf6, 0x33, 0xf6,
    0xa3, 0x6d, 0xf0, 0xf0, 0xab, 0xf4, 0x9b, 0x48,
};

struct Image {
    const std::uint8_t* base = nullptr;
    const std::uint8_t* text = nullptr;
    std::uint64_t textSize = 0;
};

bool imageAt(const void* address, const std::uint8_t (&uuid)[16], Image& image) noexcept {
    Dl_info info{};
    if (!dladdr(address, &info) || !info.dli_fbase) return false;
    const auto* header = static_cast<const mach_header_64*>(info.dli_fbase);
    if (header->magic != MH_MAGIC_64 || header->cputype != CPU_TYPE_X86_64 ||
        header->filetype != MH_DYLIB || header->sizeofcmds > 0x10000) return false;
    const auto* base = reinterpret_cast<const std::uint8_t*>(header);
    std::size_t offset = sizeof(*header);
    const std::size_t end = offset + header->sizeofcmds;
    bool matchedUuid = false;
    const segment_command_64* text = nullptr;
    for (std::uint32_t n = 0; n != header->ncmds; ++n) {
        if (end - offset < sizeof(load_command)) return false;
        const auto* command = reinterpret_cast<const load_command*>(base + offset);
        if (command->cmdsize < sizeof(*command) || command->cmdsize > end - offset) return false;
        if (command->cmd == LC_UUID) {
            if (matchedUuid || command->cmdsize != sizeof(uuid_command) ||
                std::memcmp(reinterpret_cast<const uuid_command*>(command)->uuid, uuid, 16))
                return false;
            matchedUuid = true;
        }
        if (command->cmd == LC_SEGMENT_64 && command->cmdsize >= sizeof(segment_command_64)) {
            const auto* segment = reinterpret_cast<const segment_command_64*>(command);
            if (!std::strncmp(segment->segname, "__TEXT", sizeof(segment->segname))) {
                if (text || !(segment->initprot & VM_PROT_EXECUTE)) return false;
                text = segment;
            }
        }
        offset += command->cmdsize;
    }
    // dyld's shared-cache images retain cache-relative file offsets. All
    // authenticated spans here are loaded-image offsets, not file offsets.
    if (offset != end || !matchedUuid || !text) return false;
    image = {base, base, text->vmsize};
    return true;
}

bool matches(const Image& image, const Span& span) noexcept {
    if (span.offset > image.textSize || span.size > image.textSize - span.offset) return false;
    unsigned char digest[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256(image.text + span.offset, span.size, digest);
    constexpr char digits[] = "0123456789abcdef";
    for (std::size_t n = 0; n != sizeof(digest); ++n) {
        if (span.sha256[n * 2] != digits[digest[n] >> 4] ||
            span.sha256[n * 2 + 1] != digits[digest[n] & 15]) return false;
    }
    return true;
}

template<class T> T field(const void* object, std::size_t offset) noexcept {
    T result;
    std::memcpy(&result, static_cast<const std::uint8_t*>(object) + offset, sizeof(result));
    return result;
}

template<class T> void setField(void* object, std::size_t offset, T value) noexcept {
    std::memcpy(static_cast<std::uint8_t*>(object) + offset, &value, sizeof(value));
}

Method exactMethod(Class cls, const char* name, const char* encoding,
                   const Image& image, std::size_t offset) noexcept {
    Method method = class_getInstanceMethod(cls, sel_registerName(name));
    if (!method || std::strcmp(method_getTypeEncoding(method), encoding) ||
        reinterpret_cast<const void*>(method_getImplementation(method)) != image.base + offset)
        return nullptr;
    return method;
}

bool exactIvar(Class cls, const char* name, const char* type, std::ptrdiff_t offset) noexcept {
    Ivar ivar = class_getInstanceVariable(cls, name);
    return ivar && ivar_getOffset(ivar) == offset && !std::strcmp(ivar_getTypeEncoding(ivar), type);
}

using QueueGetter = dispatch_queue_t (*)(id, SEL);
using QueueSetter = void (*)(id, SEL, dispatch_queue_t);
QueueGetter originalQueueGetter = nullptr;
QueueSetter originalQueueSetter = nullptr;

void setFeedbackQueue(id self, SEL selector, dispatch_queue_t queue) {
    auto previous = originalQueueGetter(self, @selector(feedbackQueue));
    if (previous == queue) return;
    if (queue) dispatch_retain(queue);
    originalQueueSetter(self, selector, queue);
    if (previous) dispatch_release(previous);
}

bool installQueueDescriptor() noexcept {
    constexpr std::uint8_t uuid[] = {
        0x48, 0xf1, 0x71, 0xcd, 0xfa, 0xce, 0x36, 0xbc,
        0xb4, 0x93, 0x35, 0xd0, 0x97, 0x29, 0x95, 0x2e,
    };
    // The original setter only stores the pointer; copy and dealloc already
    // retain/release it. Authenticate their complete ownership contracts.
    constexpr Span queueSpans[] = {
        {0x1f3af3, 102, "b6f5b027085092c91f61fffa23e2f448bf43af2475d6583e888f1fa66e1133e0"},
        {0x1f3b59, 73, "ea2afe9beaf951cefae431d2e88dd437237d8af1404c96df9337a6365a25b3fb"},
        {0x1f3ba2, 99, "3c398322016dfbff88d73e300c68b1a97525ef0887c308ff38398ae0b20aad38"},
        {0x1f3cec, 10, "18f8ca8f045e7e5bfd8001a5097aeffe0de32b78982eb6d0d25bd36db2963bf5"},
        {0x1f3cf6, 10, "de46328af24146cdc8a470edd9871fa4539c92063d2ffb120a41984dcf3b76c1"},
    };
    Class cls = objc_lookUpClass("MTL4CommandQueueDescriptor");
    if (!cls || class_getInstanceSize(cls) != 40 ||
        class_getSuperclass(cls) != objc_lookUpClass("NSObject")) return false;
    Image image;
    if (!imageAt(reinterpret_cast<void*>(class_getMethodImplementation(
            cls, @selector(setFeedbackQueue:))), uuid, image)) return false;
    for (const auto& span : queueSpans) if (!matches(image, span)) return false;
    Method setter = exactMethod(cls, "setFeedbackQueue:", "v24@0:8@16", image, 0x1f3cf6);
    Method getter = exactMethod(cls, "feedbackQueue", "@16@0:8", image, 0x1f3cec);
    if (!setter || !getter ||
        !exactMethod(cls, "init", "@16@0:8", image, 0x1f3af3) ||
        !exactMethod(cls, "dealloc", "v16@0:8", image, 0x1f3b59) ||
        !exactMethod(cls, "copyWithZone:", "@24@0:8^{_NSZone=}16", image, 0x1f3ba2))
        return false;
    originalQueueGetter = reinterpret_cast<QueueGetter>(method_getImplementation(getter));
    originalQueueSetter = reinterpret_cast<QueueSetter>(method_getImplementation(setter));
    method_setImplementation(setter, reinterpret_cast<IMP>(&setFeedbackQueue));
    return true;
}

bool typedStub(const Image& image, std::size_t offset, const char (&bytes)[7],
               const void* target) noexcept {
    if (offset > image.textSize || 6 > image.textSize - offset ||
        std::memcmp(image.base + offset, bytes, 6)) return false;
    const auto displacement = field<std::int32_t>(image.base, offset + 2);
    const auto* slot = image.base + offset + 6 + displacement;
    return field<const void*>(slot, 0) == target;
}

struct Hooks {
    Image image;
    Class fgClass = Nil;
    Class bbrClass = Nil;
    Method fgDestruct = nullptr;
    Method bbrInit = nullptr;
    Method bbrDestruct = nullptr;
};

bool authenticate(Hooks& hooks) noexcept {
    hooks.fgClass = objc_lookUpClass("_M4FXFrameInterpolatorEffect");
    hooks.bbrClass = objc_lookUpClass("_M4FXTemporalScalingEffectBBR");
    if (!hooks.fgClass || !hooks.bbrClass || class_getInstanceSize(hooks.fgClass) != 592 ||
        class_getInstanceSize(hooks.bbrClass) != 640) return false;
    const auto fgImp = class_getMethodImplementation(hooks.fgClass, sel_registerName(".cxx_destruct"));
    if (!imageAt(reinterpret_cast<void*>(fgImp), metalfxUuid, hooks.image)) return false;
    for (const auto& span : spans) if (!matches(hooks.image, span)) return false;
    if (!exactIvar(hooks.fgClass, "_pDevice",
            "^{MFXDevice4=@@@@{MFXComputeEncoder4=@@^{MFXDevice4}}{MFXRenderEncoder4=@@I@}QI@}", 0x40) ||
        !exactIvar(hooks.bbrClass, "_filter",
            "{unique_ptr<BBRNet_Filter<MFXDevice4>, std::default_delete<BBRNet_Filter<MFXDevice4>>>=\"\"{?=\"__ptr_\"^v}}", 0x270))
        return false;
    hooks.fgDestruct = exactMethod(hooks.fgClass, ".cxx_destruct", "v16@0:8", hooks.image, 0xa5294);
    hooks.bbrInit = exactMethod(hooks.bbrClass, "initWithDevice:descriptor:compiler:history:",
                                "@48@0:8@16@24@32@40", hooks.image, 0x7fe7f);
    hooks.bbrDestruct = exactMethod(hooks.bbrClass, ".cxx_destruct", "v16@0:8", hooks.image, 0x8014f);
    Class bbrBase = objc_lookUpClass("_MFXTemporalScalingEffectBase");
    if (!hooks.fgDestruct || !hooks.bbrInit || !hooks.bbrDestruct ||
        !bbrBase || class_getSuperclass(hooks.bbrClass) != bbrBase ||
        !exactMethod(bbrBase, "initWithDevice:descriptor:", "@32@0:8@16@24",
                     hooks.image, 0x5f1d6) ||
        !exactMethod(hooks.fgClass, "initWithDevice:compiler:descriptor:", "@40@0:8@16@24@32",
                     hooks.image, 0xa128a) ||
        !exactMethod(hooks.fgClass, "dealloc", "v16@0:8", hooks.image, 0xa1f58) ||
        !exactMethod(hooks.bbrClass, "encodeToCommandBuffer:", "v24@0:8@16",
                     hooks.image, 0x80081)) return false;

    const void* typedNew = dlsym(RTLD_DEFAULT, "_ZnwmSt19__type_descriptor_t");
    const void* typedDelete = dlsym(RTLD_DEFAULT, "_ZdlPvSt19__type_descriptor_t");
    Image abi;
    if (!typedNew || !typedDelete || !imageAt(typedNew, libcxxabiUuid, abi) ||
        typedNew != abi.base + 0x1255a || typedDelete != abi.base + 0x125e2 ||
        !matches(abi, {0x1255a, 14, "1e17d0b141948687b295aeac62cb92b2f338d6ac7ee9a29291f2296add0ca51b"}) ||
        !matches(abi, {0x125e2, 10, "c4846950bee14bff4ae1ce6320f1f7f29e041c89ac4499e886b284596ae41b63"}) ||
        !typedStub(hooks.image, 0xc313a, "\xff\x25\xa0\xca\xb5\x23", typedDelete) ||
        !typedStub(hooks.image, 0xc3140, "\xff\x25\xa2\xca\xb5\x23", typedNew) ||
        !typedStub(hooks.image, 0xc3032, "\xff\x25\x58\xca\xb5\x23", hooks.image.base + 0x58fc8) ||
        !typedStub(hooks.image, 0xc301a, "\xff\x25\x50\xca\xb5\x23", hooks.image.base + 0x59084) ||
        !typedStub(hooks.image, 0xc3038, "\xff\x25\x5a\xca\xb5\x23", hooks.image.base + 0x5b704))
        return false;
    return true;
}

using Destruct = void (*)(id);
using Init = id (*)(id, SEL, id, id, id, id);
using DestroyOwner = void (*)(void*);
using ResetFilter = void (*)(void**, void*);
Class fgClass = Nil;
Class bbrClass = Nil;
Destruct originalFgDestruct = nullptr;
Destruct originalBbrDestruct = nullptr;
Init originalBbrInit = nullptr;
DestroyOwner destroyOwner = nullptr;
ResetFilter resetFilter = nullptr;

void fgDestruct(id self) {
    const bool repaired = object_getClass(self) == fgClass;
    // libobjc's special destructor caller supplies only self; do not inspect _cmd.
    originalFgDestruct(self);
    if (repaired) destroyOwner(reinterpret_cast<std::uint8_t*>(self) + 0x40);
}

void destroyFilter(void* filter) {
    // +0x80196 takes an owning-slot address and its replacement value. It invokes
    // the original filter destructor and the matching typed delete exactly once.
    resetFilter(&filter, nullptr);
}

void retireGpu(void* filter) {
    const auto value = field<std::uint64_t>(filter, 0x1b0);
    id<MTLSharedEvent> event = field<id<MTLSharedEvent>>(filter, 0x198);
    if (value == 0 || event.signaledValue >= value) {
        destroyFilter(filter);
        return;
    }
    // The public Metal4 buffer signals the preceding odd value. The internal
    // graph waits for it and signals this even value at execution completion.
    // Keep the detached filter and its event alive without blocking submission.
    [event retain];
    auto listener = [MTLSharedEventListener new];
    [event notifyListener:listener atValue:value
        block:^(id<MTLSharedEvent>, std::uint64_t) {
            destroyFilter(filter);
            [event release];
            [listener release];
        }];
}

void retireFilter(void* filter) {
    void* network = field<void*>(filter, 0x160);
    dispatch_group_t group = network ? field<dispatch_group_t>(network, 0x10) : nullptr;
    if (!group || dispatch_group_wait(group, DISPATCH_TIME_NOW) == 0) {
        retireGpu(filter);
        return;
    }
    // The finished byte precedes the callback's final network access. Actual
    // group completion is the boundary; the filter owns the group throughout.
    dispatch_group_notify(group, dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
        retireGpu(filter);
    });
}

void bbrDestruct(id self) {
    if (object_getClass(self) != bbrClass) {
        originalBbrDestruct(self);
        return;
    }
    void* filter = field<void*>(self, 0x270);
    if (filter) {
        // Init changed this backlink to borrowed ownership, not a nil pointer.
        // No legal CPU encode/replay remains when the effect reaches destruction.
        setField<id>(filter, 8, nil);
        setField<void*>(self, 0x270, nullptr);
    }
    originalBbrDestruct(self);
    // Deferred callbacks touch only the detached native filter/network/event.
    // They must never capture self or read its already-destructed base ivars.
    if (filter) retireFilter(filter);
}

id bbrInit(id self, SEL selector, id device, id descriptor, id compiler, id history) {
    if (object_getClass(self) != bbrClass)
        return originalBbrInit(self, selector, device, descriptor, compiler, history);
    struct InitOwnership {
        id effect;
        explicit InitOwnership(id object) : effect([object retain]) {}
        ~InitOwnership() {
            // Original init consumes its initial self even on nil/exception.
            // This temporary owner keeps the storage alive through conversion.
            // Every public factory constructs once; an attached native filter
            // has exactly the one parent retain authenticated in its C1.
            if (void* filter = field<void*>(effect, 0x270))
                [field<id>(filter, 8) release];
            [effect release];
        }
    } ownership(self);
    return originalBbrInit(self, selector, device, descriptor, compiler, history);
}

#endif

} // namespace

bool initializeQueueDescriptor() noexcept {
#if defined(__x86_64__)
    static std::once_flag once;
    static bool installed = false;
    try {
        std::call_once(once, [] { installed = installQueueDescriptor(); });
    } catch (...) {
        return false;
    }
    return installed;
#else
    return false;
#endif
}

bool initialize() noexcept {
#if defined(__x86_64__)
    static std::once_flag once;
    static bool installed = false;
    try {
        std::call_once(once, [] {
            Hooks hooks;
            if (!authenticate(hooks)) return;
            fgClass = hooks.fgClass;
            bbrClass = hooks.bbrClass;
            originalFgDestruct = reinterpret_cast<Destruct>(method_getImplementation(hooks.fgDestruct));
            originalBbrDestruct = reinterpret_cast<Destruct>(method_getImplementation(hooks.bbrDestruct));
            originalBbrInit = reinterpret_cast<Init>(method_getImplementation(hooks.bbrInit));
            destroyOwner = reinterpret_cast<DestroyOwner>(
                const_cast<std::uint8_t*>(hooks.image.base) + 0x58f4a);
            resetFilter = reinterpret_cast<ResetFilter>(
                const_cast<std::uint8_t*>(hooks.image.base) + 0x80196);
            // Authenticate every entry before changing any IMP. Install the
            // matching teardown before exposing borrowed-backlink construction.
            method_setImplementation(hooks.bbrDestruct, reinterpret_cast<IMP>(&bbrDestruct));
            method_setImplementation(hooks.fgDestruct, reinterpret_cast<IMP>(&fgDestruct));
            method_setImplementation(hooks.bbrInit, reinterpret_cast<IMP>(&bbrInit));
            installed = true;
        });
    } catch (...) {
        return false;
    }
    return installed;
#else
    return false;
#endif
}

} // namespace yaagl::pso::metalfx_lifetime
