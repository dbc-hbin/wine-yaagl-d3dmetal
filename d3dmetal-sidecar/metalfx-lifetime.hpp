#pragma once

namespace yaagl::pso::metalfx_lifetime {

// Call once after authenticating D3DMetal and before any MetalFX effect factory.
// Unsupported architectures/images remain untouched and return false.
bool initialize() noexcept;

// Install before creating Metal4 command-queue descriptors with feedback queues.
// This has its own Metal image guard, independent of the MetalFX effect repair.
bool initializeQueueDescriptor() noexcept;

} // namespace yaagl::pso::metalfx_lifetime
