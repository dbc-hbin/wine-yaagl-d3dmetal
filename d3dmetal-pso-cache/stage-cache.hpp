#pragma once

#import <Metal/Metal.h>

namespace yaagl::pso {

using StageGetAndRetainLibraryEntry = id (*)(void* stageResult, id metalDevice);

// Coalesces the nil-to-library transition at D3DMStageResult + 0x178. The
// returned object has exactly the ownership supplied by GetAndRetainLibrary.
[[nodiscard]] id getAndRetainLibrarySingleFlight(
    void* stageResult,
    id metalDevice,
    StageGetAndRetainLibraryEntry original);

} // namespace yaagl::pso
