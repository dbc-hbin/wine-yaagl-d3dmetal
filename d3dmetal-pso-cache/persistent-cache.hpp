#pragma once

#include <cstddef>
#include <cstdint>

namespace yaagl::pso {

struct PersistentCacheWarmupResult final {
    std::size_t directoriesVisited = 0;
    std::size_t filesAdvised = 0;
    std::uint64_t bytesAdvised = 0;
};

// Advises the kernel to read ahead a bounded prefix of D3DMetal's existing
// persistent cache files. All errors are deliberately reported only through
// the counters: startup and native cache loading must remain authoritative.
// Runs only when YAAGL_D3DMETAL_CACHE_WARMUP is exactly "1" and uses
// _CS_DARWIN_USER_CACHE_DIR and getprogname.
PersistentCacheWarmupResult warmPersistentCachesFromEnvironment() noexcept;

} // namespace yaagl::pso
