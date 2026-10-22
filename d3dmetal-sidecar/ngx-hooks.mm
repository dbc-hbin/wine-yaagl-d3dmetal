#import "ngx-hooks.hpp"
#import "d3dmetal-transport.hpp"

#import <Foundation/Foundation.h>

#include <atomic>

namespace yaagl::pso::ngx {
namespace {

using Replay = void (*)(void*, const void*);
std::atomic<Replay> gReplay;

void replay(void* replayer, const void* command) {
    const auto result = d3dmetal::replay(replayer, command);
    if (result != d3dmetal::ReplayResult::NotRecorded) {
        if (result == d3dmetal::ReplayResult::Failed)
            [NSException raise:@"YaaglFSRReplayFailure"
                        format:@"FSR MetalFX command failed during Metal4 replay"];
        return;
    }
    gReplay.load(std::memory_order_acquire)(replayer, command);
}

} // namespace

std::array<std::uintptr_t, kHookCount> initializeHooks(
    const std::array<std::uintptr_t, kHookCount>& originals) noexcept {
    if (!originals[0]) return {};
    gReplay.store(reinterpret_cast<Replay>(originals[0]), std::memory_order_release);
    return {reinterpret_cast<std::uintptr_t>(&replay)};
}

} // namespace yaagl::pso::ngx
