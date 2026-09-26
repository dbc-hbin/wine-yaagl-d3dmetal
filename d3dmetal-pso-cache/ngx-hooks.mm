#import "ngx-hooks.hpp"
#import "d3dmetal-transport.hpp"
#import "d3dmetal-transport-legacy.hpp"

#import <Foundation/Foundation.h>

#include <atomic>

namespace yaagl::pso::ngx {
namespace {

using Replay = void (*)(void*, const void*);
using Encode = void (*)(void*, const void*);
std::atomic<Replay> gReplay;
std::atomic<Encode> gEncode;

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

void encode(void* encoder, const void* command) {
    const auto result = d3dmetal::legacy::replay(encoder, command);
    if (result != d3dmetal::ReplayResult::NotRecorded) {
        if (result == d3dmetal::ReplayResult::Failed)
            [NSException raise:@"YaaglFSRLegacyEncodeFailure"
                        format:@"FSR MetalFX command failed during legacy Metal encoding"];
        return;
    }
    gEncode.load(std::memory_order_acquire)(encoder, command);
}

} // namespace

std::array<std::uintptr_t, kHookCount> initializeHooks(
    const std::array<std::uintptr_t, kHookCount>& originals) noexcept {
    if (!originals[0] || !originals[1]) return {};
    gReplay.store(reinterpret_cast<Replay>(originals[0]), std::memory_order_release);
    gEncode.store(reinterpret_cast<Encode>(originals[1]), std::memory_order_release);
    return {reinterpret_cast<std::uintptr_t>(&replay),
            reinterpret_cast<std::uintptr_t>(&encode)};
}

} // namespace yaagl::pso::ngx
