#include "cache-lifetime.hpp"

#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <mach-o/loader.h>
#include <dlfcn.h>
#include <sys/mman.h>
#include <fcntl.h>
#include <unistd.h>

#include <array>
#include <atomic>
#include <cassert>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <vector>

#include "cache-lifetime-layout.hpp"
#include "cache-lifetime-image-gate.inc"

using Open = bool (*)(void*, const std::filesystem::path&);
using Close = void (*)(void*);
using View = void* (*)(void*, std::uint64_t, std::uint64_t);
using Fields = std::array<std::uint64_t, 7>;

// This is the existing cache-lifetime.cpp native before-control's exact VM
// interval oracle. Neither RSS nor address-space totals establish ownership.
static bool mapped(void* pointer) {
    mach_vm_address_t address = reinterpret_cast<std::uintptr_t>(pointer);
    const auto wanted = address;
    mach_vm_size_t size = 0;
    vm_region_basic_info_data_64_t info{};
    mach_msg_type_number_t count = VM_REGION_BASIC_INFO_COUNT_64;
    mach_port_t object = MACH_PORT_NULL;
    const auto result = mach_vm_region(mach_task_self(), &address, &size,
        VM_REGION_BASIC_INFO_64, reinterpret_cast<vm_region_info_t>(&info), &count, &object);
    if (object != MACH_PORT_NULL) mach_port_deallocate(mach_task_self(), object);
    return result == KERN_SUCCESS && address <= wanted && wanted - address < size;
}

static void* pointer(const Fields& fields, unsigned index) {
    return reinterpret_cast<void*>(fields[index]);
}

static void closed(int fd) {
    errno = 0;
    assert(fcntl(fd, F_GETFD) == -1 && errno == EBADF);
}

static void prepare(const std::filesystem::path& path, bool valid) {
    const int fd = ::open(path.c_str(), O_CREAT | O_TRUNC | O_RDWR, 0600);
    assert(fd >= 0);
    const std::uint64_t header[] = {valid ? 24u : 0u, 24, 24};
    assert(pwrite(fd, header, sizeof(header), 0) == static_cast<ssize_t>(sizeof(header)));
    assert(::close(fd) == 0);
}

static void* acquiredHeader = nullptr;
static int acquiredFD = -1;
static unsigned nativePayloadUnmaps = 0;
static unsigned nativeHeaderUnmaps = 0;
static unsigned nativeFileCloses = 0;

static void* trackMmap(void* address, std::size_t length, int protection,
    int flags, int fd, off_t offset) {
    void* result = mmap(address, length, protection, flags, fd, offset);
    assert(result != MAP_FAILED);
    if (length == 24) {
        acquiredHeader = result;
        acquiredFD = fd;
    }
    return result;
}

static int trackMunmap(void* address, std::size_t length) {
    if (length == 24) ++nativeHeaderUnmaps;
    else ++nativePayloadUnmaps;
    return munmap(address, length);
}

static int trackFclose(FILE* file) {
    ++nativeFileCloses;
    return fclose(file);
}

template<class T> static void bind(std::uint8_t* image, std::size_t offset, T function) {
    const auto value = reinterpret_cast<std::uintptr_t>(function);
    std::memcpy(image + offset, &value, sizeof(value));
}

static void imageGateControl(std::uint8_t* image) {
    const auto accepts = [&] {
        return yaagl::pso::matchesImage(reinterpret_cast<const mach_header*>(image));
    };
    assert(accepts());
    const auto cpu = reinterpret_cast<mach_header*>(image)->cputype;
    reinterpret_cast<mach_header*>(image)->cputype = CPU_TYPE_ARM64;
    assert(!accepts());
    reinterpret_cast<mach_header*>(image)->cputype = cpu;
    for (const auto& span : yaagl::pso::layout::kVerificationSpans) {
        // Test the exact production gate, including non-entry bytes in each
        // Open/Close/View body, instead of asserting its source text.
        const auto offset = span.offset + span.size - 1;
        image[offset] ^= 1;
        assert(!accepts());
        image[offset] ^= 1;
    }
    *reinterpret_cast<std::uintptr_t*>(image + kDispatchSlot) = 1;
    assert(!accepts());
    *reinterpret_cast<std::uintptr_t*>(image + kDispatchSlot) = 0;
    assert(accepts());
    std::puts("CACHE_PRODUCTION_IMAGE_GATE_PASS architecture all-spans dispatch-unpublished");
}

static void beforeControl(Open open, View view, Close close, const std::filesystem::path& path) {
    // Reproduce both confirmed defects through the actual unpublished gates,
    // then clean only the resources acquired by this before-control.
    prepare(path, true);
    Fields fields{};
    assert(open(fields.data(), path));
    auto* payload = static_cast<std::uint8_t*>(view(fields.data(), 0x10000, 1024 * 1024));
    assert(payload == pointer(fields, 1));
    const auto length = fields[3] - fields[2];
    payload[0] = 0x5a;
    close(fields.data());
    assert(mapped(payload) && payload[0] == 0x5a && fields[1] == 0);
    assert(munmap(payload, length) == 0 && !mapped(payload));
    for (unsigned retry = 0; retry != 2; ++retry) {
        prepare(path, false);
        fields = {};
        assert(!open(fields.data(), path));
        void* header = pointer(fields, 0);
        auto* file = static_cast<FILE*>(pointer(fields, 6));
        const int fd = fileno(file);
        assert(mapped(header) && fcntl(fd, F_GETFD) >= 0);
        if (retry) {
            prepare(path, true);
            assert(open(fields.data(), path));
            close(fields.data());
        } else {
            assert(open(fields.data(), std::filesystem::path{}));
            assert(fields[0] == 0 && fields[6] == 0);
        }
        assert(mapped(header) && fcntl(fd, F_GETFD) >= 0);
        assert(munmap(header, 24) == 0 && fclose(file) == 0);
        closed(fd);
    }
    std::puts("CACHE_NATIVE_BEFORE_CONTROL_PASS payload-leak failed-open-disable failed-open-retry");
}

static void fixedControl(Open open, View view, Close close,
    const std::filesystem::path& path, bool fixture) {
    // Full compiler retries reopen successful siblings of a failed reader.
    // Disabling after repeated failure must retire those siblings as well.
    for (unsigned disable = 0; disable != 2; ++disable) {
        prepare(path, true);
        Fields fields{};
        assert(open(fields.data(), path));
        void* header = pointer(fields, 0);
        const int fd = fileno(static_cast<FILE*>(pointer(fields, 6)));
        void* payload = view(fields.data(), 0x10000, 65536);
        assert(open(fields.data(), disable ? std::filesystem::path{} : path));
        if (!disable) close(fields.data());
        assert(!mapped(header) && !mapped(payload));
        closed(fd);
        assert(fields[0] == 0 && fields[6] == 0);
    }
    for (unsigned iteration = 1; iteration <= 16; ++iteration) {
        prepare(path, true);
        Fields fields{};
        assert(open(fields.data(), path));
        void* header = pointer(fields, 0);
        const int fd = fileno(static_cast<FILE*>(pointer(fields, 6)));
        auto* first = static_cast<std::uint8_t*>(view(fields.data(), 24, 65536));
        assert(first == static_cast<std::uint8_t*>(pointer(fields, 1)) + 24);
        first[0] = static_cast<std::uint8_t>(iteration);
        const auto unmaps = nativePayloadUnmaps;
        // A disjoint View must retire the old mapping with its original exact
        // length before installing the new interval.
        auto* payload = static_cast<std::uint8_t*>(view(fields.data(), 0x40000, 1024 * 1024));
        assert(payload == pointer(fields, 1));
        if (fixture) assert(nativePayloadUnmaps == unmaps + 1);
        assert(view(fields.data(), 0x40010, 1) == payload + 16);
        payload[0] = static_cast<std::uint8_t>(iteration);
        assert(mapped(payload) && fields[3] - fields[2] == 1024 * 1024);
        const auto headerUnmaps = nativeHeaderUnmaps;
        const auto payloadUnmaps = nativePayloadUnmaps;
        const auto fileCloses = nativeFileCloses;
        close(fields.data());
        assert(!mapped(payload) && !mapped(header));
        closed(fd);
        assert(fields[1] == 0 && fields[2] == 0 && fields[3] == 0 &&
            fields[4] == 0 && fields[5] == 0);
        if (fixture) {
            assert(nativeHeaderUnmaps == headerUnmaps + 1);
            assert(nativeFileCloses == fileCloses + 1);
            assert(nativePayloadUnmaps == payloadUnmaps);
        }
        // Native readers immediately reopen after Close. A generic Close
        // before Open would double-close the stale header and FILE here.
        assert(open(fields.data(), path));
        auto* readback = static_cast<std::uint8_t*>(view(fields.data(), 0x40000, 1));
        assert(readback[0] == iteration);
        close(fields.data());
        assert(open(fields.data(), std::filesystem::path{}));
    }
    for (unsigned retry = 0; retry != 2; ++retry) {
        for (unsigned iteration = 0; iteration != 16; ++iteration) {
            prepare(path, false);
            Fields fields{};
            assert(!open(fields.data(), path));
            assert(fields[0] == 0 && fields[6] == 0);
            if (fixture) {
                assert(acquiredHeader && !mapped(acquiredHeader));
                closed(acquiredFD);
            }
            if (retry) {
                prepare(path, true);
                assert(open(fields.data(), path));
                close(fields.data());
            } else {
                assert(!open(fields.data(), path));
                if (fixture) {
                    assert(acquiredHeader && !mapped(acquiredHeader));
                    closed(acquiredFD);
                }
                assert(open(fields.data(), std::filesystem::path{}));
            }
            assert(open(fields.data(), std::filesystem::path{}));
            assert(fields[0] == 0 && fields[6] == 0);
        }
    }
    assert(unlink(path.c_str()) == 0);
    std::puts("CACHE_LIFETIME_FIXED_PASS close=16 remap=16 reopen-readback=16 failed-open=48 disable=true");
}

int main(int argc, char** argv) {
    const bool loaded = argc == 4 && std::strcmp(argv[1], "--image") == 0;
    assert(loaded || argc == 3);
    std::uint8_t* image = nullptr;
    void* handle = nullptr;
    const std::filesystem::path path(argv[loaded ? 3 : 2]);
    std::array<std::uintptr_t, kHookCount> table{};
    if (loaded) {
        handle = dlopen(argv[2], RTLD_NOW | RTLD_LOCAL);
        if (!handle) std::fprintf(stderr, "dlopen: %s\n", dlerror());
        assert(handle);
        Dl_info info{};
        assert(dladdr(dlsym(handle, "MDECreateContext"), &info));
        assert(std::filesystem::equivalent(info.dli_fname, argv[2]));
        image = static_cast<std::uint8_t*>(info.dli_fbase);
        const auto* dispatch = *reinterpret_cast<const std::uintptr_t**>(image + kDispatchSlot);
        assert(dispatch && dispatch[kOpenIndex] && dispatch[kCloseIndex]);
    } else {
        image = static_cast<std::uint8_t*>(mmap(nullptr, kImageSize,
            PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0));
        assert(image != MAP_FAILED);
        std::ifstream stream(argv[1], std::ios::binary);
        assert(stream);
        stream.read(reinterpret_cast<char*>(image), kTextSize);
        assert(stream.gcount() == static_cast<std::streamsize>(kTextSize));
        imageGateControl(image);
        bind(image, kFopenSlot, &fopen);
        bind(image, kFseekSlot, &fseek);
        bind(image, kFtellSlot, &ftell);
        bind(image, kFilenoSlot, &fileno);
        bind(image, kFtruncateSlot, &ftruncate);
        bind(image, kMmapSlot, &trackMmap);
        bind(image, kMunmapSlot, &trackMunmap);
        bind(image, kFcloseSlot, &trackFclose);
        assert(mprotect(image, kTextSize, PROT_READ | PROT_EXEC) == 0);
        beforeControl(reinterpret_cast<Open>(image + kOpen),
            reinterpret_cast<View>(image + kView), reinterpret_cast<Close>(image + kClose), path);
        const auto hooks = yaagl::pso::cache_lifetime::initialize({
            reinterpret_cast<std::uintptr_t>(image + kOpenOriginal),
            reinterpret_cast<std::uintptr_t>(image + kCloseOriginal),
        });
        table[kOpenIndex] = hooks.open;
        table[kCloseIndex] = hooks.close;
        const auto dispatch = reinterpret_cast<std::uintptr_t>(table.data());
        std::memcpy(image + kDispatchSlot, &dispatch, sizeof(dispatch));
    }
    fixedControl(reinterpret_cast<Open>(image + kOpen),
        reinterpret_cast<View>(image + kView), reinterpret_cast<Close>(image + kClose), path, !loaded);
    if (!loaded) assert(munmap(image, kImageSize) == 0);
    // A loaded sidecar registers ObjC classes and process-lifetime compiler
    // state. Let this isolated process exit instead of unloading that image.
}
