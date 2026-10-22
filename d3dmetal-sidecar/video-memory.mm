#import "video-memory.hpp"

#if __has_feature(objc_arc)
#error "d3dmetal-sidecar must be compiled with Objective-C automatic reference counting disabled"
#endif

#import <Metal/Metal.h>

#include <algorithm>
#include <cstring>
#include <sys/sysctl.h>

namespace yaagl::pso::video_memory {
namespace {

constexpr std::size_t kAdapterDevice = 0x18;
constexpr std::size_t kAdapterReservation = 0x1f0;
constexpr std::uint32_t kLocalSegment = 0;
constexpr std::uint64_t kGiB = 1ull << 30;

struct MemoryLimits final {
    std::uint64_t physical;
    std::uint64_t graphics;
};

const MemoryLimits& memoryLimits() noexcept {
    static const MemoryLimits value = [] {
        std::uint64_t ram = 0;
        std::size_t size = sizeof(ram);
        if (sysctlbyname("hw.memsize", &ram, &size, nullptr, 0) != 0) return MemoryLimits{};
        const std::uint64_t aboveReserve = ram > 16 * kGiB ? ram - 16 * kGiB : 0;
        return MemoryLimits{ram, std::min(ram / 5 * 4, std::max(aboveReserve, ram / 2))};
    }();
    return value;
}

id<MTLDevice> adapterDevice(const void* adapter) noexcept {
    id<MTLDevice> device = nil;
    if (adapter != nullptr) {
        std::memcpy(&device, static_cast<const char*>(adapter) + kAdapterDevice, sizeof(device));
    }
    return device;
}

} // namespace

void updateAdapterDescription(void* adapter) noexcept {
    id<MTLDevice> device = adapterDevice(adapter);
    if (device == nil) return;
    const std::uint64_t recommended = [device recommendedMaxWorkingSetSize];
    const MemoryLimits& limits = memoryLimits();
    const std::uint64_t dedicated = limits.graphics != 0
        ? std::min(recommended, limits.graphics) : recommended;
    const std::uint64_t memory[] = {
        dedicated,
        0,
        limits.physical > dedicated ? limits.physical - dedicated : 0,
    };
    std::memcpy(static_cast<char*>(adapter) + 0x1c0, memory, sizeof(memory));
}

std::int32_t query(void* adapter, std::uint32_t, std::uint32_t group,
                   QueryVideoMemoryInfo* info) noexcept {
    if (info == nullptr) return 0x80070057;
    *info = {};
    if (adapter == nullptr || group != kLocalSegment) return 0;
    id<MTLDevice> device = adapterDevice(adapter);
    if (device == nil) return 0;
    const std::uint64_t recommended = [device recommendedMaxWorkingSetSize];
    const std::uint64_t windows = memoryLimits().graphics;
    info->budget = windows != 0 ? std::min(recommended, windows) : recommended;
    info->currentUsage = [device currentAllocatedSize];
    info->availableForReservation = info->budget >> 1;
    std::memcpy(&info->currentReservation,
                static_cast<const char*>(adapter) + kAdapterReservation + group * sizeof(std::uint64_t),
                sizeof(info->currentReservation));
    return 0;
}

} // namespace yaagl::pso::video_memory
