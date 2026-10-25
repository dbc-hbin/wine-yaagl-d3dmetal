#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

namespace yaagl::pso::async_pipeline {
constexpr std::size_t kHookCount = 17;
// Order matches the async suffix of layout.json's dispatch table.
std::array<std::uintptr_t, kHookCount> initialize(
    const std::uint8_t* image,
    const std::array<std::uintptr_t, kHookCount>& originals);

struct Statistics {
    std::uint32_t version;
    std::uint32_t size;
    std::uint64_t queued;
    std::uint64_t ready;
    std::uint64_t failed;
    std::uint64_t skipped;
    std::uint64_t synchronous;
};
} // namespace yaagl::pso::async_pipeline

// Read-only production counters. They count native-cache jobs, not Metal cache misses.
extern "C" bool yaagl_d3dmetal_async_stats(
    yaagl::pso::async_pipeline::Statistics* statistics, std::uint32_t size) noexcept;
