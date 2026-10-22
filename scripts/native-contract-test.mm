#include "../include/yaagl_d3dmetal_native.hpp"

#include <mach-o/stab.h>
#include <cxxabi.h>
#include <cassert>
#include <cstdlib>
#include <limits.h>
#include <fstream>
#include <iterator>
#include <string>
#include <typeinfo>

namespace {
template<class T> void put(std::vector<std::uint8_t>& bytes, std::size_t at, const T& value) {
    assert(at <= bytes.size() && sizeof(value) <= bytes.size() - at);
    std::memcpy(bytes.data() + at, &value, sizeof(value));
}

struct SyntheticImage {
    std::vector<std::uint8_t> bytes = std::vector<std::uint8_t>(0x5000);
    static constexpr std::uint64_t vm = 0x100000000ULL;
    std::size_t symtabCommand = 0;

    explicit SyntheticImage(std::uint64_t functionOffset = 0x800) {
        mach_header_64 header{};
        header.magic = MH_MAGIC_64;
        header.cputype = CPU_TYPE_X86_64;
        header.filetype = MH_DYLIB;
        header.ncmds = 4;
        header.sizeofcmds = 2 * sizeof(segment_command_64) + sizeof(section_64) +
            sizeof(symtab_command) + sizeof(uuid_command);
        put(bytes, 0, header);
        std::size_t cursor = sizeof(header);
        segment_command_64 text{};
        text.cmd = LC_SEGMENT_64;
        text.cmdsize = sizeof(text) + sizeof(section_64);
        std::strcpy(text.segname, SEG_TEXT);
        text.vmaddr = vm;
        text.vmsize = text.filesize = 0x1000;
        text.initprot = VM_PROT_READ | VM_PROT_EXECUTE;
        text.nsects = 1;
        put(bytes, cursor, text);
        section_64 code{};
        std::strcpy(code.sectname, SECT_TEXT);
        std::strcpy(code.segname, SEG_TEXT);
        code.addr = vm + 0x800;
        code.size = 0x100;
        code.flags = S_ATTR_PURE_INSTRUCTIONS;
        put(bytes, cursor + sizeof(text), code);
        cursor += text.cmdsize;
        segment_command_64 linkedit{};
        linkedit.cmd = LC_SEGMENT_64;
        linkedit.cmdsize = sizeof(linkedit);
        std::strcpy(linkedit.segname, SEG_LINKEDIT);
        linkedit.vmaddr = vm + 0x3000;
        linkedit.vmsize = 0x1000;
        linkedit.fileoff = 0x1000;
        linkedit.filesize = 0x400;
        linkedit.initprot = VM_PROT_READ;
        put(bytes, cursor, linkedit);
        cursor += sizeof(linkedit);
        symtabCommand = cursor;
        symtab_command symbols{};
        symbols.cmd = LC_SYMTAB;
        symbols.cmdsize = sizeof(symbols);
        symbols.symoff = 0x1000; // maps to bytes + 0x3000, NOT bytes + 0x1000
        symbols.nsyms = 3;
        symbols.stroff = 0x1100;
        symbols.strsize = 128;
        put(bytes, cursor, symbols);
        cursor += sizeof(symbols);
        uuid_command uuid{};
        uuid.cmd = LC_UUID;
        uuid.cmdsize = sizeof(uuid);
        std::memset(uuid.uuid, 0xab, sizeof(uuid.uuid));
        put(bytes, cursor, uuid);
        nlist_64 function{};
        function.n_un.n_strx = 1;
        function.n_type = N_SECT;
        function.n_sect = 1;
        function.n_value = vm + functionOffset;
        put(bytes, 0x3000, function);
        nlist_64 debug = function;
        debug.n_type = N_FUN;
        debug.n_value += 1;
        put(bytes, 0x3000 + sizeof(nlist_64), debug);
        std::strcpy(reinterpret_cast<char*>(bytes.data() + 0x3101), "_fixture");
    }
    const mach_header_64* header() const {
        return reinterpret_cast<const mach_header_64*>(bytes.data());
    }
};

void testSymbols() {
    SyntheticImage first;
    SyntheticImage moved(0x880);
    const yaagl::macho::Symbols a(first.header()), b(moved.header());
    assert(a.valid() && b.valid());
    assert(a.find("_fixture", true) == first.bytes.data() + 0x800);
    assert(b.find("_fixture", true) == moved.bytes.data() + 0x880);
    const char* reason = nullptr;
    assert(!a.find("_missing", true, &reason) && std::string(reason) == "symbol-not-found");
    auto corrupt = first;
    auto table = yaagl::native::readObject<symtab_command>(corrupt.bytes.data() + corrupt.symtabCommand);
    table.stroff = 0x2000;
    put(corrupt.bytes, corrupt.symtabCommand, table);
    assert(!yaagl::macho::Symbols(corrupt.header()).valid());
    corrupt = first;
    auto entry = yaagl::native::readObject<nlist_64>(corrupt.bytes.data() + 0x3000);
    entry.n_value += 1;
    put(corrupt.bytes, 0x3000 + 2 * sizeof(entry), entry);
    assert(!yaagl::macho::Symbols(corrupt.header()).find("_fixture", true, &reason));
    assert(std::string(reason) == "ambiguous-symbol");
    corrupt = first;
    std::memset(corrupt.bytes.data() + 0x3100, 'x', 128);
    assert(!yaagl::macho::Symbols(corrupt.header()).find("_fixture", true, &reason));
    assert(std::string(reason) == "unterminated-symbol-name");
    corrupt = first;
    entry.n_value = SyntheticImage::vm + 0x999;
    put(corrupt.bytes, 0x3000, entry);
    assert(!yaagl::macho::Symbols(corrupt.header()).find("_fixture", true, &reason));
    assert(std::string(reason) == "symbol-value-out-of-section");
    std::puts("symbol relocation, linkedit mapping, missing/duplicate/malformed symbols: PASS");
}
}

@interface YaaglGetterFixture : NSObject {
@public
    std::uint64_t padding[9];
    void* raw;
}
- (void*)commandList;
- (void*)commandAllocator;
- (NSUInteger)notAPointer;
@end
@implementation YaaglGetterFixture
- (void*)commandList { return raw; }
- (void*)commandAllocator { return raw; }
- (NSUInteger)notAPointer { return 1; }
@end

namespace {
void testOwnership() {
    int device = 1, compiler = 2;
    yaagl::native::MplContextPrefix context{&device, nullptr, &compiler};
    // Only the context prefix exists. Reading old +0x60/+0x66 magic would be invalid.
    yaagl::native::MplAllocatorPrefix allocator{&context};
    yaagl::native::MplListPrefix list{&context, &allocator};
    auto* listWrapper = [YaaglGetterFixture new];
    auto* allocatorWrapper = [YaaglGetterFixture new];
    listWrapper->raw = &list;
    allocatorWrapper->raw = &allocator;
    assert(ivar_getOffset(class_getInstanceVariable([YaaglGetterFixture class], "raw")) > 8);
    struct AllocatorOwner { void* padding[3]; void* mde; } owner{{}, allocatorWrapper};
    struct Active { void* padding[2]; void* mde; void* allocator; } active{{}, listWrapper, &owner};
    struct Wrapper { void* padding[5]; void* active; } wrapper{{}, &active};
    const yaagl::native::CommandListLayout offsets{
        offsetof(Wrapper, active), offsetof(Active, mde),
        offsetof(Active, allocator), offsetof(AllocatorOwner, mde)};
    yaagl::native::MplState state;
    void* observed = nullptr;
    assert(yaagl::native::describeWrappedMpl(&wrapper, offsets, state, observed));
    assert(state.list == &list && state.allocator == &allocator && observed == &active);
    assert(state.device == &device && state.compiler == &compiler);
    allocator.context = nullptr;
    const char* reason = nullptr;
    assert(!yaagl::native::describeWrappedMpl(&wrapper, offsets, state, observed, &reason));
    assert(std::string(reason) == "allocator-context-mismatch" && !state.list);
    allocator.context = &context;
    assert(!yaagl::native::pointerGetter(listWrapper, @selector(notAPointer), &reason));
    assert(std::string(reason) == "mde-getter-return-mismatch");
    assert(!yaagl::native::pointerGetter(listWrapper, sel_registerName("absentGetter"), &reason));
    assert(std::string(reason) == "missing-mde-getter");
    allocatorWrapper->raw = &list;
    assert(!yaagl::native::describeWrappedMpl(&wrapper, offsets, state, observed, &reason));
    assert(std::string(reason) == "mde-allocator-identity-mismatch" && !state.list);
    allocatorWrapper->raw = &allocator;
    context.compiler = nullptr;
    assert(!yaagl::native::describeWrappedMpl(&wrapper, offsets, state, observed, &reason));
    assert(std::string(reason) == "missing-metal-compiler" && !state.list);
    context.compiler = &compiler;
    assert(yaagl::native::describeWrappedMpl(&wrapper, offsets, state, observed, &reason));
    assert(reason == nullptr && state.list == &list);
    [listWrapper release];
    [allocatorWrapper release];
    std::puts("moved MDE ivar, native getter types, allocator ownership graph: PASS");
}

struct CastBase { virtual ~CastBase() = default; int base = 1; };
struct CastOther { virtual ~CastOther() = default; int other = 2; };
struct CastDerived : CastOther, CastBase {};
void testCast() {
    yaagl::native::D3DMetalApi api;
    api.dynamicCast = reinterpret_cast<decltype(api.dynamicCast)>(dlsym(RTLD_DEFAULT, "__dynamic_cast"));
    assert(api.dynamicCast);
    api.commandListType = &typeid(CastBase);
    api.mplType = &typeid(CastDerived);
    CastDerived object;
    assert(api.castCommandList(static_cast<CastBase*>(&object)) == &object);
    CastBase unsupported;
    assert(!api.castCommandList(&unsupported));
    std::puts("RTTI-derived base displacement and unsupported-list rejection: PASS");
}

struct FakeOwner { void** vtable; std::uint32_t references; };
struct FakeBridge { void** vtable; FakeOwner* owner; };
FakeOwner* expectedOwner = nullptr;
const void* expectedGuid = nullptr;
std::uint32_t __attribute__((ms_abi)) fakeAddRef(void* pointer) {
    assert(pointer == expectedOwner);
    return ++expectedOwner->references;
}
std::uint32_t __attribute__((ms_abi)) fakeReleaseInterface(void* pointer) {
    assert(pointer == expectedOwner && expectedOwner->references > 1);
    return --expectedOwner->references;
}
void* fakeGetInterface(void* pointer, const void* guid) {
    assert(guid == expectedGuid);
    return static_cast<FakeBridge*>(pointer)->owner;
}
std::int32_t __attribute__((ms_abi)) fakeQuery(void*, const void* guid, void** result) {
    assert(guid == expectedGuid);
    ++expectedOwner->references;
    *result = expectedOwner;
    return 0;
}
void fakeRelease(void** pointer) {
    assert(*pointer == expectedOwner && expectedOwner->references > 1);
    --expectedOwner->references;
    *pointer = nullptr;
}
void testMixedAbi() {
    std::array<void*, 9> ownerTable{};
    FakeOwner owner{ownerTable.data() + 3, 1};
    ownerTable[3 + 4] = reinterpret_cast<void*>(&fakeAddRef);
    ownerTable[3 + 5] = reinterpret_cast<void*>(&fakeReleaseInterface);
    // vtable[-3] is the Itanium virtual-base adjustment, zero for this fixture.
    std::array<void*, 4> bridgeTable{};
    bridgeTable[3] = reinterpret_cast<void*>(&fakeGetInterface);
    FakeBridge bridge{bridgeTable.data(), &owner};
    std::array<void*, 1> publicTable{reinterpret_cast<void*>(&fakeQuery)};
    std::array<void*, 2> publicWrapper{&bridge, publicTable.data()};
    expectedOwner = &owner;
    expectedGuid = &ownerTable;
    void* gfxTos = &bridge;
    yaagl::native::D3DMetalApi api;
    api.gfxTosIUnknown = &gfxTos;
    api.commandListIid = expectedGuid;
    api.releaseCommandList = &fakeRelease;
    void* acquired = api.acquireCommandList(&publicWrapper[1]);
    assert(acquired == &owner && owner.references == 2);
    api.releaseCommandList(&acquired);
    assert(acquired == nullptr && owner.references == 1);
    gfxTos = nullptr;
    acquired = api.acquireCommandList(&publicWrapper[1]);
    assert(acquired == &owner && owner.references == 2);
    api.releaseCommandList(&acquired);
    assert(acquired == nullptr && owner.references == 1);
    acquired = api.acquirePrivateInterface(&publicWrapper[1], expectedGuid);
    assert(acquired == &owner && owner.references == 2);
    api.releasePrivateInterface(acquired);
    assert(owner.references == 1);
    std::puts("System V bridge / Microsoft AddRef+QI / native release ownership: PASS");
}

void testRealImage(const char* path) {
    std::ifstream stream(path, std::ios::binary);
    assert(stream);
    std::vector<std::uint8_t> file{std::istreambuf_iterator<char>(stream), {}};
    assert(file.size() >= sizeof(mach_header_64));
    const auto header = yaagl::native::readObject<mach_header_64>(file.data());
    assert(header.magic == MH_MAGIC_64 && header.sizeofcmds <= file.size() - sizeof(header));
    std::vector<segment_command_64> segments;
    std::uint64_t textBase = 0, imageEnd = 0;
    std::size_t cursor = sizeof(header);
    for (std::uint32_t i = 0; i != header.ncmds; ++i) {
        assert(cursor + sizeof(load_command) <= file.size());
        const auto command = yaagl::native::readObject<load_command>(file.data() + cursor);
        assert(command.cmdsize >= sizeof(command) && command.cmdsize <= file.size() - cursor);
        if (command.cmd == LC_SEGMENT_64) {
            const auto segment = yaagl::native::readObject<segment_command_64>(file.data() + cursor);
            assert(segment.fileoff <= file.size() && segment.filesize <= file.size() - segment.fileoff);
            segments.push_back(segment);
            if (std::strncmp(segment.segname, SEG_TEXT, sizeof(segment.segname)) == 0) textBase = segment.vmaddr;
            imageEnd = std::max(imageEnd, segment.vmaddr + segment.vmsize);
        }
        cursor += command.cmdsize;
    }
    std::vector<std::uint8_t> mapped(imageEnd - textBase);
    for (const auto& segment : segments) {
        assert(segment.vmaddr >= textBase && segment.filesize <= imageEnd - segment.vmaddr);
        std::memcpy(mapped.data() + segment.vmaddr - textBase, file.data() + segment.fileoff, segment.filesize);
    }
    yaagl::native::D3DMetalApi api;
    assert(api.load(reinterpret_cast<const mach_header_64*>(mapped.data())));
    assert(api.getInternalResource && api.releaseTexture && api.releaseCommandList && api.gfxTosIUnknown);
    std::size_t uuidOffset = sizeof(header);
    while (yaagl::native::readObject<load_command>(mapped.data() + uuidOffset).cmd != LC_UUID)
        uuidOffset += yaagl::native::readObject<load_command>(mapped.data() + uuidOffset).cmdsize;
    mapped[uuidOffset + offsetof(uuid_command, uuid)] ^= 1;
    assert(!api.load(reinterpret_cast<const mach_header_64*>(mapped.data())));
    assert(!api.getInternalResource && !api.releaseTexture && !api.releaseCommandList && !api.gfxTosIUnknown);
    mapped[uuidOffset + offsetof(uuid_command, uuid)] ^= 1;
    auto wrongArchitecture = header;
    wrongArchitecture.cputype = CPU_TYPE_ARM64;
    put(mapped, 0, wrongArchitecture);
    assert(!api.load(reinterpret_cast<const mach_header_64*>(mapped.data())));
    assert(!api.dispatch && !api.commandListIid && !api.dynamicCast);
    put(mapped, 0, header);
    assert(api.load(reinterpret_cast<const mach_header_64*>(mapped.data())));
    // Addresses are resolved but NEVER called. This is static source-byte analysis,
    // not dlopen, proprietary target execution, game launch, or a GPU capture.
    std::puts("all named bindings resolve in the exact pristine image (no target execution): PASS");
    std::puts("shared image identity checks reject wrong UUID/CPU and clear stale bindings: PASS");
}

// Look up the real local implementation, not a replacement callback. Demangling
// avoids coupling this routing test to compiler-specific substitution indices.
void* findImplementation(const mach_header_64* image, const char* qualifiedName) {
    const yaagl::macho::Symbols symbols(image);
    assert(symbols.valid());
    const auto* base = reinterpret_cast<const std::uint8_t*>(image);
    const symtab_command* table = nullptr;
    const segment_command_64* linkedit = nullptr;
    std::size_t cursor = sizeof(*image);
    for (std::uint32_t i = 0; i < image->ncmds; ++i) {
        const auto* command = reinterpret_cast<const load_command*>(base + cursor);
        if (command->cmd == LC_SYMTAB)
            table = reinterpret_cast<const symtab_command*>(command);
        if (command->cmd == LC_SEGMENT_64) {
            const auto* segment = reinterpret_cast<const segment_command_64*>(command);
            if (std::strncmp(segment->segname, SEG_LINKEDIT, sizeof(segment->segname)) == 0)
                linkedit = segment;
        }
        cursor += command->cmdsize;
    }
    assert(table && linkedit);
    // These dylibs have __TEXT vmaddr zero; dyld maps __LINKEDIT at vmaddr.
    const auto* entries = reinterpret_cast<const nlist_64*>(
        base + linkedit->vmaddr + table->symoff - linkedit->fileoff);
    const auto* strings = reinterpret_cast<const char*>(
        base + linkedit->vmaddr + table->stroff - linkedit->fileoff);
    const std::string prefix = std::string(qualifiedName) + "(";
    void* result = nullptr;
    for (std::uint32_t i = 0; i < table->nsyms; ++i) {
        if ((entries[i].n_type & N_STAB) || (entries[i].n_type & N_TYPE) != N_SECT)
            continue;
        assert(entries[i].n_un.n_strx < table->strsize);
        const char* name = strings + entries[i].n_un.n_strx;
        if (std::strncmp(name, "__Z", 3)) continue;
        int status = 0;
        char* demangled = abi::__cxa_demangle(name + 1, nullptr, nullptr, &status);
        const bool matches = status == 0 && demangled &&
            std::strncmp(demangled, prefix.c_str(), prefix.size()) == 0;
        std::free(demangled);
        if (!matches) continue;
        void* candidate = symbols.find(name, true);
        assert(candidate && (!result || result == candidate));
        result = candidate;
    }
    if (!result) std::fprintf(stderr, "missing native implementation: %s\n", qualifiedName);
    assert(result);
    return result;
}

void testLoadedImage(const char* path, const char* pristinePath) {
    // Only the explicit opt-in runner uses this path. It executes the actual
    // dylib constructors in a bounded child with writes confined to scratch.
    void* handle = dlopen(path, RTLD_NOW | RTLD_LOCAL);
    if (!handle) std::fprintf(stderr, "native dlopen failed: %s\n", dlerror());
    assert(handle);
    Dl_info imageInfo{};
    assert(dladdr(dlsym(handle, "MDECreateContext"), &imageInfo));
    char requestedPath[PATH_MAX], loadedPath[PATH_MAX];
    assert(realpath(path, requestedPath) && realpath(imageInfo.dli_fname, loadedPath));
    assert(std::strcmp(requestedPath, loadedPath) == 0);
    const auto* image = static_cast<const mach_header_64*>(imageInfo.dli_fbase);
    yaagl::native::D3DMetalApi api;
    assert(api.load(image));
    const auto* base = reinterpret_cast<const std::uint8_t*>(image);
    constexpr char marker[] = "YAAGLB3SIDEcar\x1b";
    assert(std::memcmp(base + 0x4e07c0, marker, sizeof(marker) - 1) == 0);
    std::ifstream stream(pristinePath, std::ios::binary);
    assert(stream);
    const std::vector<std::uint8_t> pristine{std::istreambuf_iterator<char>(stream), {}};
    constexpr std::size_t nativeEntries[] = {
        0x1842b9, 0x184353, 0x1843fc, 0x1844a5, 0x126576, 0x96758, 0x60f54,
        0x138d28, 0x139418, 0x13c544, 0x92d6a, 0x1863fe, 0x12507c,
    };
    for (const auto offset : nativeEntries) {
        assert(offset + 64 <= pristine.size());
        assert(std::memcmp(base + offset, pristine.data() + offset, 64) == 0);
    }
    std::puts("all thirteen loaded native cache entries match pristine bytes: PASS");
    const auto* dispatch = yaagl::native::readObject<const std::uintptr_t*>(base + 0x563f18);
    assert(dispatch);
    void* fsr = dlsym(handle, "yaagl_fsr_api");
    void* fg = dlsym(handle, "yaagl_fsr_fg_api");
    assert(fsr && fg);
    Dl_info sidecarInfo{};
    assert(dladdr(fsr, &sidecarInfo));
    assert(sidecarInfo.dli_fbase != imageInfo.dli_fbase);
    Dl_info fgInfo{};
    assert(dladdr(fg, &fgInfo) && fgInfo.dli_fbase == sidecarInfo.dli_fbase);
    const auto* sidecar = static_cast<const mach_header_64*>(sidecarInfo.dli_fbase);
    constexpr const char* implementations[] = {
        "yaagl::pso::display::(anonymous namespace)::getContainingOutput",
        "yaagl::pso::display::(anonymous namespace)::setFullscreenState",
        "yaagl::pso::(anonymous namespace)::queryVideoMemoryInfo",
        "yaagl::pso::ngx::(anonymous namespace)::replay",
    };
    for (std::size_t index = 0; index < 4; ++index) {
        assert(dispatch[index] == reinterpret_cast<std::uintptr_t>(
            findImplementation(sidecar, implementations[index])));
    }
    // Exercise the real entry -> gate -> compact slot -> production hook with
    // its supported null-output contract; no fabricated DXGI object is used.
    using Query = std::int32_t (__attribute__((ms_abi)) *)(
        void*, std::uint32_t, std::uint32_t, void*);
    const auto query = reinterpret_cast<Query>(const_cast<std::uint8_t*>(base) + 0x1678ae);
    assert(static_cast<std::uint32_t>(query(nullptr, 0, 0, nullptr)) == 0x80070057u);
    std::puts("actual sidecar constructor, four implementation slots, native memory gate: PASS");

    // This is the original native singleton/caller flow at 0x5dada -> 0xaa066,
    // not a synthetic D3DMDevice or a replacement forwarding function. The
    // optimized Instance routine has no usable return value; discard it.
    const yaagl::macho::Symbols symbols(image);
    using InitializeCompiler = void (*)(id<MTLDevice>);
    const auto initializeCompiler = reinterpret_cast<InitializeCompiler>(symbols.find(
        "__ZN12D3DMCompiler8InstanceEPU19objcproto9MTLDevice11objc_object", true));
    const auto* initialized = static_cast<const std::uint8_t*>(symbols.find(
        "__ZGVZN12D3DMCompiler8InstanceEPU19objcproto9MTLDevice11objc_objectE8Compiler", false));
    assert(initializeCompiler && initialized && *initialized == 0);
    id<MTLDevice> device = MTLCreateSystemDefaultDevice();
    assert(device);
    initializeCompiler(device);
    assert(*initialized != 0);
    initializeCompiler(device);
    assert(*initialized != 0);
    [device release];
    std::puts("original D3DMCompiler singleton -> constructor returns with initialized guard: PASS");
    std::puts("NOT COVERED: DXIL CompileCompute, swapchain behavior, FSR replay, or GPU output");
    // Do not unload ObjC classes/native compiler state; this isolated process
    // exits immediately and owns the entire loaded image lifetime.
}
}

int main(int argc, char** argv) {
    std::setvbuf(stdout, nullptr, _IONBF, 0);
    @autoreleasepool {
        if (argc == 4 && std::strcmp(argv[1], "--load") == 0) {
            testLoadedImage(argv[2], argv[3]);
            return 0;
        }
        assert(argc == 1 || argc == 2);
        testSymbols();
        testOwnership();
        testCast();
        testMixedAbi();
        if (argc == 2) testRealImage(argv[1]);
    }
    return 0;
}
