#pragma once

#include <cstdint>

namespace yaagl::pso::async_pipeline {
// Order matches the async suffix of layout.json's dispatch table.
struct Hooks {
    std::uintptr_t graphicsCtor;
    std::uintptr_t getRenderPipelineState;
    std::uintptr_t graphicsDestroy;
    std::uintptr_t draw;
    std::uintptr_t drawIndexed;
    std::uintptr_t rootCtor;
    std::uintptr_t rootDestroy;
    std::uintptr_t reset;
    std::uintptr_t beginQuery;
    std::uintptr_t endQuery;
    std::uintptr_t listDestroy;
    std::uintptr_t present;
    std::uintptr_t dx11Draw;
    std::uintptr_t dx11DrawIndexed;
    std::uintptr_t dx11Begin;
    std::uintptr_t dx11End;
    std::uintptr_t dx11Destroy;
};

Hooks initialize(const std::uint8_t* image, const Hooks& originals);

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
