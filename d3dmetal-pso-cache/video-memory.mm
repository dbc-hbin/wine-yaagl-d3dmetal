#import "video-memory.hpp"

#if __has_feature(objc_arc)
#error "d3dmetal-pso-cache must be compiled with Objective-C automatic reference counting disabled"
#endif

#import <Metal/Metal.h>

#include <cstring>

namespace yaagl::pso::video_memory {
namespace {

// DXGIAdapter keeps its MTLDevice at +0x18; the pinned method reads it there.
constexpr std::size_t kAdapterDevice = 0x18;
constexpr std::uint32_t kLocalSegment = 0;

} // namespace

// Fixed budget, as DXMT and MoltenVK report it: Metal's recommended working
// set, with the device's exact 64-bit allocation as local usage. Stock
// D3DMetal reports twice the recommended size and truncates usage to 32 bits.
// D3DMetal never evicts (Evict/MakeResident are no-ops), so a budget that
// tracked system memory would only make the game drop texture detail without
// freeing anything. Apple GPUs are UMA, and D3D12 reports UMA adapters with a
// single local segment group: the non-local group is all zero, as MoltenVK's
// single heap shows through DXVK. Reservations are not implemented
// (SetVideoMemoryReservation is a no-op).
std::int32_t query(void* adapter, std::uint32_t node, std::uint32_t group,
                   QueryVideoMemoryInfo* info) noexcept {
    if (info == nullptr) return 0;
    *info = {};
    if (adapter == nullptr || node != 0 || group != kLocalSegment) return 0;
    id<MTLDevice> device = nil;
    std::memcpy(&device, static_cast<const char*>(adapter) + kAdapterDevice, sizeof(device));
    if (device == nil) return 0;
    info->budget = [device recommendedMaxWorkingSetSize];
    info->currentUsage = [device currentAllocatedSize];
    return 0;
}

} // namespace yaagl::pso::video_memory
