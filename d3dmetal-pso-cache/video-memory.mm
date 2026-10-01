#import "video-memory.hpp"

#if __has_feature(objc_arc)
#error "d3dmetal-pso-cache must be compiled with Objective-C automatic reference counting disabled"
#endif

#import <Metal/Metal.h>

#include <algorithm>
#include <cstring>
#include <sys/sysctl.h>

namespace yaagl::pso::video_memory {
namespace {

// DXGIAdapter keeps its MTLDevice at +0x18; the pinned method reads it there.
constexpr std::size_t kAdapterDevice = 0x18;
constexpr std::uint32_t kLocalSegment = 0;
constexpr std::uint64_t kGiB = 1ull << 30;

struct MemoryLimits final {
    std::uint64_t physical;
    std::uint64_t graphics;
};

// Windows VidMm's system memory available for graphics on a UMA machine:
// MIN(RAM * 80%, MAX(RAM - 16GB, RAM * 50%)). Half of RAM up to 32 GB, so the
// game's CPU-side memory keeps the other half. Zero when RAM is unknown.
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

} // namespace

void updateAdapterDescription(void* adapter) noexcept {
    id<MTLDevice> device = nil;
    std::memcpy(&device, static_cast<const char*>(adapter) + kAdapterDevice, sizeof(device));
    if (device == nil) return;
    const std::uint64_t recommended = [device recommendedMaxWorkingSetSize];
    const MemoryLimits& limits = memoryLimits();
    const std::uint64_t dedicated = limits.graphics != 0
        ? std::min(recommended, limits.graphics) : recommended;
    // Match the existing discrete-GPU identity without claiming a second
    // dedicated system-memory pool. These are reported capacities, not heaps.
    const std::uint64_t memory[] = {
        dedicated,
        0,
        limits.physical > dedicated ? limits.physical - dedicated : 0,
    };
    std::memcpy(static_cast<char*>(adapter) + 0x1c0, memory, sizeof(memory));
}

// Fixed budget: Metal's recommended working set capped by the memory Windows
// grants graphics on the same RAM, with the device's exact 64-bit allocation
// as local usage. Stock D3DMetal reports twice the recommended size and
// truncates usage to 32 bits. D3DMetal never evicts (Evict/MakeResident are
// no-ops), so a budget that tracked free memory would only make the game drop
// texture detail without freeing anything. Apple GPUs are UMA, and D3D12
// reports UMA adapters with a single local segment group: the non-local group
// is all zero, as MoltenVK's single heap shows through DXVK. Reservations are
// not implemented (SetVideoMemoryReservation is a no-op). The node index is
// ignored, as stock D3DMetal does: the adapter has one node, and under Wine
// the node register holds an unrelated value (observed 0x4000364c for
// NodeIndex 0).
std::int32_t query(void* adapter, std::uint32_t, std::uint32_t group,
                   QueryVideoMemoryInfo* info) noexcept {
    if (info == nullptr) return 0;
    *info = {};
    if (adapter == nullptr || group != kLocalSegment) return 0;
    id<MTLDevice> device = nil;
    std::memcpy(&device, static_cast<const char*>(adapter) + kAdapterDevice, sizeof(device));
    if (device == nil) return 0;
    const std::uint64_t recommended = [device recommendedMaxWorkingSetSize];
    const std::uint64_t windows = memoryLimits().graphics;
    info->budget = windows != 0 ? std::min(recommended, windows) : recommended;
    info->currentUsage = [device currentAllocatedSize];
    return 0;
}

} // namespace yaagl::pso::video_memory
