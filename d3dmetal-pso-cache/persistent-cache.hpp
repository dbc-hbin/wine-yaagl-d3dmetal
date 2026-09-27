#pragma once

#include <cstddef>
#include <cstdint>

namespace yaagl::pso {

// Advises the kernel to read ahead a bounded prefix of D3DMetal's existing
// persistent cache files. All errors are deliberately ignored: startup and
// native cache loading must remain authoritative.
// Runs only when YAAGL_D3DMETAL_CACHE_WARMUP is exactly "1" and uses
// _CS_DARWIN_USER_CACHE_DIR and getprogname.
void warmPersistentCachesFromEnvironment() noexcept;

} // namespace yaagl::pso
