#pragma once

#include <cstdint>

namespace yaagl::pso::display {

struct Hooks {
    std::uintptr_t getContainingOutput;
    std::uintptr_t setFullscreenState;
    std::uintptr_t presentFlush;
};

// Original entry points are the verified D3DMetal trampolines. The call-site
// hook has no trampoline: its unpublished gate executes the original flush.
Hooks initialize(std::uintptr_t imageBase, std::uintptr_t originalGetContainingOutput,
                 std::uintptr_t originalSetFullscreenState) noexcept;

} // namespace yaagl::pso::display
