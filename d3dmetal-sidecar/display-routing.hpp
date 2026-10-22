#pragma once

#include <cstdint>

namespace yaagl::pso::display {

struct Hooks {
    std::uintptr_t getContainingOutput;
    std::uintptr_t setFullscreenState;
};

// Original entry points are the verified beta3 D3DMetal trampolines.
Hooks initialize(std::uintptr_t imageBase, std::uintptr_t originalGetContainingOutput,
                 std::uintptr_t originalSetFullscreenState) noexcept;

} // namespace yaagl::pso::display
