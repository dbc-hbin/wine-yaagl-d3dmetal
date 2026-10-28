#pragma once

#include <cstdint>

namespace yaagl::pso::cache_lifetime {

struct Hooks {
    std::uintptr_t open;
    std::uintptr_t close;
};

Hooks initialize(Hooks originals);

} // namespace yaagl::pso::cache_lifetime
