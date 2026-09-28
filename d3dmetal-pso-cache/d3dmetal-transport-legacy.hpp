#pragma once

#include "d3dmetal-transport.hpp"

namespace yaagl::pso::d3dmetal::legacy {

// Pins the GPTK 4.0b2 legacy MTL3 command transport. Passing nullptr locates
// D3DMetal through MPLCreateContext, matching the primary transport helper.
bool initialize(const void* d3dmetalImageBase = nullptr) noexcept;

// Completes the legacy half of d3dmetal::unwrapCommandList(). The primary
// helper identifies D3D12GraphicsCommandListMTL and owns its private interface;
// this resolves its D3DMCommandListMTL, allocator and actual MTLDevice.
bool resolveCommandList(NativeCommandList& commandList) noexcept;

// Reserve the native legacy temporal opcode as a carrier, replace only its
// payload with our private tag/owner, and retain that owner through the native
// D3DMCommandAllocator resource lifetime list.
bool record(NativeCommandList& commandList, const RecordRequest& request) noexcept;

// Prime's legacy EncodeTemporallyScaleMTLFX hook calls this before the native
// parser. Only NotRecorded may fall through; recognized failures remain ours.
ReplayResult replay(void* d3dmCommandEncoder, const void* command) noexcept;

} // namespace yaagl::pso::d3dmetal::legacy
