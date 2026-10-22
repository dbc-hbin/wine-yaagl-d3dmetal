#pragma once

#include <cstdint>

namespace yaagl::pso::video_memory {

struct QueryVideoMemoryInfo final {
    std::uint64_t budget;
    std::uint64_t currentUsage;
    std::uint64_t availableForReservation;
    std::uint64_t currentReservation;
};

void updateAdapterDescription(void* adapter) noexcept;
std::int32_t query(void* adapter, std::uint32_t node, std::uint32_t group,
                   QueryVideoMemoryInfo* info) noexcept;

} // namespace yaagl::pso::video_memory
