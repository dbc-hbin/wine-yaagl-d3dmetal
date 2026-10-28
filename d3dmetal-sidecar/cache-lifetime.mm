#include "cache-lifetime.hpp"
#include "../include/yaagl_d3dmetal_abi.hpp"

#include <cstdio>
#include <cstring>
#include <sys/mman.h>

namespace yaagl::pso::cache_lifetime {
namespace {

// The authenticated beta3 Open/Close/View bodies use the macOS SysV ABI.
// path is an opaque pointer to the caller's native libc++ filesystem::path.
using Open = bool (*)(void*, const void*);
using Close = void (*)(void*);

Hooks original{};

void close(void* self) {
    auto* bytes = static_cast<std::uint8_t*>(self);
    void* payload = native::readObject<void*>(bytes + 0x08);
    if (payload != nullptr) {
        const auto begin = native::readObject<std::uint64_t>(bytes + 0x10);
        const auto end = native::readObject<std::uint64_t>(bytes + 0x18);
        if (munmap(payload, end - begin) == 0) {
            // Skip only the original zero-length payload unmap. Its header
            // unmap, fclose and remaining field clearing run unchanged.
            payload = nullptr;
            std::memcpy(bytes + 0x08, &payload, sizeof(payload));
        }
    }
    reinterpret_cast<Close>(original.close)(self);
    // Native Close leaves these two values stale. Clear them together with
    // the native payload fields so the next Open can distinguish a live owner
    // from a cache that was already closed.
    void* header = nullptr;
    FILE* file = nullptr;
    std::memcpy(bytes, &header, sizeof(header));
    std::memcpy(bytes + 0x30, &file, sizeof(file));
}

bool open(void* self, const void* path) {
    auto* bytes = static_cast<std::uint8_t*>(self);
    // OpenCache retries all readers, including successful siblings of the
    // failed reader. Disabling also calls Open(empty) on those live siblings.
    // Paired Close normalization makes this ownership check safe; native
    // constructors zero both fields before their first Open.
    if (native::readObject<void*>(bytes) || native::readObject<FILE*>(bytes + 0x30))
        close(self);
    const bool result = reinterpret_cast<Open>(original.open)(self, path);
    // The gated false return is the header-size mismatch after acquisition.
    // Empty Open succeeds, preserving the native disabled-cache contract.
    if (!result) close(self);
    return result;
}

} // namespace

Hooks initialize(Hooks originals) {
    original = originals;
    return {
        reinterpret_cast<std::uintptr_t>(&open),
        reinterpret_cast<std::uintptr_t>(&close),
    };
}

} // namespace yaagl::pso::cache_lifetime
