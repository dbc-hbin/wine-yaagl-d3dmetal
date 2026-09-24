#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

namespace yaagl::pso::ngx {

inline constexpr std::size_t kHookCount = 2;

// Wrap only the stock Metal4 replay and legacy encode entry points.
// Recorded FSR commands are handled first; stock NGX commands call the original directly.
// An all-zero result means the pinned helper entries are unavailable.
std::array<std::uintptr_t, kHookCount> initializeHooks(
    const std::array<std::uintptr_t, kHookCount>& originals) noexcept;

} // namespace yaagl::pso::ngx
