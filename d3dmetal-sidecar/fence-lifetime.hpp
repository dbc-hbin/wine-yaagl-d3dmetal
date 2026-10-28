#pragma once

#include <cstdint>

namespace yaagl::pso::fence_lifetime {

struct Hooks {
    std::uintptr_t single;
    std::uintptr_t multiple;
    std::uintptr_t dx12Native;
    std::uintptr_t dx12Unpack;
    std::uintptr_t dx11Public;
    std::uintptr_t dx11Native;
    std::uintptr_t dx11Unpack;
    std::uintptr_t multiplePublic;
    std::uintptr_t multipleUnpack;
};

Hooks initialize(const std::uint8_t* image, Hooks originals) noexcept;

} // namespace yaagl::pso::fence_lifetime
