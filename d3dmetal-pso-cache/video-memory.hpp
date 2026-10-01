#pragma once

#include <cstdint>

namespace yaagl::pso::video_memory {

// Updates the stored description after the original adapter constructor. All
// native, COM and Wine unixcall GetDesc variants copy these same fields.
void updateAdapterDescription(void* adapter) noexcept;

// DXGI_QUERY_VIDEO_MEMORY_INFO.
struct QueryVideoMemoryInfo final {
    std::uint64_t budget;
    std::uint64_t currentUsage;
    std::uint64_t availableForReservation;
    std::uint64_t currentReservation;
};

// Replaces DXGIAdapter::QueryVideoMemoryInfo(this, node, group, info).
std::int32_t query(void* adapter, std::uint32_t node, std::uint32_t group,
                   QueryVideoMemoryInfo* info) noexcept;

} // namespace yaagl::pso::video_memory
