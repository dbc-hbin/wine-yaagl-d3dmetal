#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

namespace yaagl::pso::ngx {

inline constexpr std::size_t kHookCount = 1;

// Wrap only the stock Metal4 ReplayTemporalScale entry point. Both custom FSR
// SR and FG commands use opcode 0x3f; no native FG or NGX producer is patched.
// Recorded FSR commands are handled first; stock NGX commands call the original directly.
// An all-zero result means the pinned helper entries are unavailable.
std::array<std::uintptr_t, kHookCount> initializeHooks(
    const std::array<std::uintptr_t, kHookCount>& originals) noexcept;

} // namespace yaagl::pso::ngx
