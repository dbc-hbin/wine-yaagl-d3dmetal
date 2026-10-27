#pragma once

#include <cstdint>

namespace yaagl::latency {

struct Hooks {
    std::uintptr_t present;
    std::uintptr_t queryInterface;
    std::uintptr_t completePresent;
    std::uintptr_t destroySwapchain;
    std::uintptr_t destroyDevice;
};

Hooks initialize(const std::uint8_t* image, Hooks originals);

} // namespace yaagl::latency
