#!/usr/bin/env python3
"""Exercise production FG dispatch and native retired-context ownership without
Wine or a game. Dispatch tests capture the bridge/native DLL boundaries; the
ownership test uses real Metal4/MetalFX objects and GPU completion.
"""

import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "dlls/amd_fidelityfx_framegeneration_dx12/main.c").read_text()
SDK = ROOT / "d3dmetal-pso-cache/third-party/fidelityfx/Kits/FidelityFX/framegeneration/include"


def section(start, end):
    begin = SOURCE.index(start)
    return SOURCE[begin:SOURCE.index(end, begin)]


PROLOGUE = r'''
#include <assert.h>
#include <math.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <wchar.h>
#define __declspec(attribute)
#include "ffx_framegeneration.h"
#include "yaagl_fsr_fg_bridge.h"
#define WINAPI
#define TRUE 1
#define FALSE 0
#define BRIDGE_INELIGIBLE 4u
#define MAX_DESCRIPTOR_CHAIN 64u
#define FFX_API_DISPATCH_DESC_TYPE_FRAMEGENERATIONSWAPCHAIN_WAIT_FOR_PRESENTS_DX12 999u
#define TRACE_ON(channel) 0
#define TRACE(...) ((void)0)
#define yaagl_fsr_fg 0
typedef int BOOL;
typedef int LONG;
typedef int SRWLOCK;
typedef int CRITICAL_SECTION;
enum context_mode { CONTEXT_NATIVE, MODE_PENDING, CONTEXT_METALFX, CONTEXT_SWAPCHAIN };
struct frame_config_snapshot { uint64_t frame_id; struct FfxApiResource hudless; };
struct prepare_desc_prefix { ffxDispatchDescHeader header; uint64_t frame_id; uint32_t flags; };
struct fg_context
{
    LONG mode;
    ffxContext original;
    uint64_t translated;
    SRWLOCK configure_lock;
    CRITICAL_SECTION dispatch_lock;
    struct ffxConfigureDescFrameGeneration native_config;
    BOOL native_config_valid;
    uint32_t display_width, display_height, max_render_width, max_render_height;
    uint64_t last_prepare_frame_id;
    BOOL have_last_prepare_frame_id, depth_infinite;
};
struct callback_binding
{
    struct fg_context *context;
    FfxApiPresentCallbackFunc present;
    void *present_user;
    FfxApiFrameGenerationDispatchFunc generate;
    void *generate_user;
};
struct callback_scope { int unused; };
struct native_api
{
    ffxReturnCode_t (*dispatch)(ffxContext *, const ffxDispatchDescHeader *);
};
static struct native_api native;
static struct fg_context ctx;
static ffxContext handle;
static int callbacks, native_calls, bridge_calls, retired, reported, lock_promotes;
static ffxReturnCode_t bridge_result;
static struct yaagl_fsr_fg_prepare_packet last_prepare;
static struct yaagl_fsr_fg_dispatch_packet last_generation;
static struct frame_config_snapshot snapshot;

static LONG InterlockedCompareExchange(LONG *value, LONG exchange, LONG compare)
{ LONG old = *value; if (old == compare) *value = exchange; return old; }
static LONG InterlockedExchange(LONG *value, LONG exchange)
{ LONG old = *value; *value = exchange; return old; }
static LONG InterlockedIncrement(LONG *value) { return ++*value; }
static void AcquireSRWLockExclusive(SRWLOCK *lock)
{ (void)lock; if (lock_promotes) { ctx.mode = CONTEXT_METALFX; lock_promotes = 0; } }
static void ReleaseSRWLockExclusive(SRWLOCK *lock) { (void)lock; }
static void EnterCriticalSection(CRITICAL_SECTION *lock) { (void)lock; }
static void LeaveCriticalSection(CRITICAL_SECTION *lock) { (void)lock; }
static BOOL in_context_callback(struct fg_context *context)
{ (void)context; return callbacks != 0; }
static BOOL in_callback(void) { return callbacks != 0; }
static void enter_callback(struct callback_scope *scope, struct fg_context *context)
{ (void)scope; (void)context; ++callbacks; }
static void leave_callback(struct callback_scope *scope) { (void)scope; --callbacks; }
static struct frame_config_snapshot *find_frame_config(struct fg_context *context,
                                                       uint64_t frame_id)
{ (void)context; return snapshot.frame_id == frame_id ? &snapshot : NULL; }
static void retire_frame_configs(struct fg_context *context, uint64_t frame_id)
{ (void)context; (void)frame_id; ++retired; }
static void initialize_packet(struct yaagl_fsr_fg_packet_header *header,
                              uint32_t size, uint32_t operation, uint64_t context)
{
    header->size = size;
    header->version = YAAGL_FSR_FG_BRIDGE_VERSION;
    header->operation = operation;
    header->result = FFX_API_RETURN_ERROR_RUNTIME_ERROR;
    header->context = context;
}
static ffxReturnCode_t bridge_call(void *packet)
{
    struct yaagl_fsr_fg_packet_header *header = packet;
    ++bridge_calls;
    if (header->operation == YAAGL_FSR_FG_PREPARE)
        memcpy(&last_prepare, packet, sizeof(last_prepare));
    else if (header->operation == YAAGL_FSR_FG_DISPATCH)
        memcpy(&last_generation, packet, sizeof(last_generation));
    else assert(0);
    return bridge_result;
}
static ffxReturnCode_t native_dispatch(ffxContext *original,
                                        const ffxDispatchDescHeader *desc)
{ (void)original; (void)desc; ++native_calls; return FFX_API_RETURN_OK; }
static ffxReturnCode_t configure_frame_generation(
    struct fg_context *context, const struct ffxConfigureDescFrameGeneration *desc)
{ (void)context; (void)desc; return FFX_API_RETURN_OK; }
static struct fg_context *acquire_context(ffxContext *passed)
{ return passed == &handle ? &ctx : NULL; }
static void release_context(struct fg_context *context) { (void)context; }
static void report_validation(struct fg_context *context, const wchar_t *message)
{ (void)context; (void)message; ++reported; }
'''

EPILOGUE = r'''
static void reset_state(int mode)
{
    memset(&ctx, 0, sizeof(ctx));
    memset(&snapshot, 0, sizeof(snapshot));
    memset(&last_prepare, 0, sizeof(last_prepare));
    memset(&last_generation, 0, sizeof(last_generation));
    ctx.mode = mode;
    ctx.display_width = ctx.display_height = 1920;
    ctx.max_render_width = ctx.max_render_height = 1920;
    ctx.translated = 17;
    native.dispatch = native_dispatch;
    callbacks = native_calls = bridge_calls = retired = reported = lock_promotes = 0;
    bridge_result = FFX_API_RETURN_OK;
}
static struct ffxDispatchDescFrameGenerationPrepareV2 valid_prepare(uint64_t id)
{
    struct ffxDispatchDescFrameGenerationPrepareV2 desc = {0};
    desc.header.type = FFX_API_DISPATCH_DESC_TYPE_FRAMEGENERATION_PREPARE_V2;
    desc.frameID = id;
    desc.commandList = (void *)1;
    desc.depth.resource = (void *)2;
    desc.motionVectors.resource = (void *)3;
    desc.renderSize.width = desc.renderSize.height = 100;
    desc.frameTimeDelta = 16.0f;
    desc.cameraNear = 0.1f;
    desc.cameraFar = 100.0f;
    desc.cameraFovAngleVertical = 1.0f;
    desc.viewSpaceToMetersFactor = 1.0f;
    return desc;
}
static ffxDispatchDescFrameGeneration valid_generation(uint64_t id)
{
    ffxDispatchDescFrameGeneration desc = {0};
    desc.header.type = FFX_API_DISPATCH_DESC_TYPE_FRAMEGENERATION;
    desc.commandList = (void *)1;
    desc.presentColor.resource = (void *)2;
    desc.outputs[0].resource = (void *)3;
    desc.numGeneratedFrames = 1;
    desc.minMaxLuminance[1] = 1.0f;
    desc.frameID = id;
    return desc;
}
#define DISPATCH(desc) ffxDispatch(&handle, &(desc).header)
static ffxReturnCode_t mutate_and_dispatch(ffxDispatchDescFrameGeneration *desc, void *user)
{
    (void)user;
    desc->numGeneratedFrames = 2;
    desc->outputs[1].resource = (void *)4;
    return ffxDispatch(&handle, &desc->header);
}
static void check_prepare(void)
{
    struct ffxDispatchDescFrameGenerationPrepareV2 v2 = valid_prepare(10);
    struct ffxDispatchDescFrameGenerationPrepare v1 = {0};
    struct ffxDispatchDescFrameGenerationPrepareCameraInfo camera = {0};
    ffxApiHeader unknown = {123456, NULL};
    int start;

    reset_state(MODE_PENDING);
    assert(DISPATCH(v2) == FFX_API_RETURN_OK);
    assert(ctx.mode == CONTEXT_METALFX && bridge_calls == 1);
    assert(last_prepare.frame_id == 10 && !last_prepare.reset && last_prepare.camera_info_present);
    v2.frameID = 12;
    assert(DISPATCH(v2) == FFX_API_RETURN_OK && last_prepare.reset);
    assert(ctx.last_prepare_frame_id == 12);
    v2.frameID = 13;
    v2.reset = true;
    assert(DISPATCH(v2) == FFX_API_RETURN_OK && last_prepare.reset);
    v2.reset = false;
    v2.frameID = 14;
    assert(DISPATCH(v2) == FFX_API_RETURN_OK && !last_prepare.reset);
    v1.header.type = FFX_API_DISPATCH_DESC_TYPE_FRAMEGENERATION_PREPARE;
    v1.frameID = 15;
    v1.commandList = v2.commandList;
    v1.depth = v2.depth;
    v1.motionVectors = v2.motionVectors;
    v1.renderSize = v2.renderSize;
    v1.frameTimeDelta = v2.frameTimeDelta;
    v1.cameraNear = v2.cameraNear;
    v1.cameraFar = v2.cameraFar;
    v1.cameraFovAngleVertical = v2.cameraFovAngleVertical;
    v1.viewSpaceToMetersFactor = v2.viewSpaceToMetersFactor;
    v1.unused_reset = true;
    camera.header.type = FFX_API_DISPATCH_DESC_TYPE_FRAMEGENERATION_PREPARE_CAMERAINFO;
    v1.header.pNext = &camera.header;
    assert(DISPATCH(v1) == FFX_API_RETURN_OK);
    assert(!last_prepare.reset && last_prepare.camera_info_present);
    camera.cameraForward[0] = NAN;
    start = bridge_calls;
    assert(DISPATCH(v1) == FFX_API_RETURN_ERROR_PARAMETER && bridge_calls == start);
    camera.cameraForward[0] = 0;
    v2.cameraNear = NAN;
    assert(DISPATCH(v2) == FFX_API_RETURN_ERROR_PARAMETER && bridge_calls == start);
    v2.cameraNear = 0.1f;
    v2.header.pNext = &unknown;
    assert(DISPATCH(v2) == FFX_API_RETURN_ERROR_UNKNOWN_DESCTYPE && bridge_calls == start);

    reset_state(MODE_PENDING);
    v1.header.pNext = &unknown;
    assert(DISPATCH(v1) == FFX_API_RETURN_OK);
    assert(ctx.mode == CONTEXT_NATIVE && native_calls == 1 && bridge_calls == 0);
    reset_state(MODE_PENDING);
    v2.header.pNext = &unknown;
    assert(DISPATCH(v2) == FFX_API_RETURN_OK);
    assert(ctx.mode == CONTEXT_NATIVE && native_calls == 1 && bridge_calls == 0);
    v2.header.pNext = NULL;
    reset_state(MODE_PENDING);
    v2.flags = FFX_FRAMEGENERATION_FLAG_DRAW_DEBUG_VIEW;
    assert(DISPATCH(v2) == FFX_API_RETURN_OK && native_calls == 1 && bridge_calls == 0);
    v2.flags = 0;
    reset_state(MODE_PENDING);
    bridge_result = BRIDGE_INELIGIBLE;
    assert(DISPATCH(v2) == FFX_API_RETURN_OK);
    assert(ctx.mode == CONTEXT_NATIVE && native_calls == 1 && bridge_calls == 1);
    reset_state(MODE_PENDING);
    bridge_result = FFX_API_RETURN_ERROR_RUNTIME_ERROR;
    assert(DISPATCH(v2) == FFX_API_RETURN_ERROR_RUNTIME_ERROR);
    assert(ctx.mode == MODE_PENDING && native_calls == 0 && !ctx.have_last_prepare_frame_id);
    reset_state(MODE_PENDING);
    lock_promotes = 1;
    v2.header.pNext = &unknown;
    assert(DISPATCH(v2) == FFX_API_RETURN_ERROR_UNKNOWN_DESCTYPE);
    assert(ctx.mode == CONTEXT_METALFX && native_calls == 0 && bridge_calls == 0);
}
static void check_generation(void)
{
    ffxDispatchDescFrameGeneration desc = valid_generation(20);
    ffxApiHeader extension = {123456, NULL};
    struct callback_binding binding = {0};
    int start;

    reset_state(CONTEXT_METALFX);
    ctx.have_last_prepare_frame_id = true;
    ctx.last_prepare_frame_id = 20;
    assert(DISPATCH(desc) == FFX_API_RETURN_OK && bridge_calls == 1 && !last_generation.reset);
    assert(last_generation.present_color == 2 && last_generation.output == 3);
    desc.frameID = 21;
    assert(DISPATCH(desc) == FFX_API_RETURN_OK && last_generation.reset);
    desc.reset = true;
    desc.frameID = 20;
    assert(DISPATCH(desc) == FFX_API_RETURN_OK && last_generation.reset);
    start = bridge_calls;
    desc.numGeneratedFrames = 2;
    desc.outputs[1].resource = (void *)4;
    assert(DISPATCH(desc) == FFX_API_RETURN_ERROR_PARAMETER && bridge_calls == start);
    desc.numGeneratedFrames = 1;
    desc.outputs[0].resource = desc.presentColor.resource;
    assert(DISPATCH(desc) == FFX_API_RETURN_ERROR_PARAMETER && bridge_calls == start);
    desc.outputs[0].resource = (void *)3;
    desc.header.pNext = &extension;
    assert(DISPATCH(desc) == FFX_API_RETURN_ERROR_UNKNOWN_DESCTYPE && bridge_calls == start);
    desc.header.pNext = NULL;
    desc.generationRect.left = INT32_MAX;
    desc.generationRect.width = INT32_MAX;
    assert(DISPATCH(desc) == FFX_API_RETURN_ERROR_PARAMETER && bridge_calls == start);
    desc.generationRect = (struct FfxApiRect2D){0};
    snapshot.frame_id = 20;
    snapshot.hudless.resource = desc.outputs[0].resource;
    assert(DISPATCH(desc) == FFX_API_RETURN_ERROR_PARAMETER && bridge_calls == start);
    snapshot.frame_id = 0;
    ctx.native_config_valid = true;
    ctx.native_config.flags = FFX_FRAMEGENERATION_FLAG_NO_SWAPCHAIN_CONTEXT_NOTIFY;
    bridge_result = FFX_API_RETURN_ERROR_RUNTIME_ERROR;
    assert(DISPATCH(desc) == FFX_API_RETURN_ERROR_RUNTIME_ERROR);
    assert(bridge_calls == start + 1 && retired == 1 && native_calls == 0);

    reset_state(CONTEXT_NATIVE);
    desc.numGeneratedFrames = 4;
    for (unsigned i = 0; i < 4; ++i) desc.outputs[i].resource = (void *)(uintptr_t)(i + 3);
    assert(DISPATCH(desc) == FFX_API_RETURN_OK && native_calls == 1 && bridge_calls == 0);
    desc.numGeneratedFrames = 0;
    assert(DISPATCH(desc) == FFX_API_RETURN_ERROR_PARAMETER && native_calls == 1);
    desc.numGeneratedFrames = 5;
    assert(DISPATCH(desc) == FFX_API_RETURN_ERROR_PARAMETER && native_calls == 1);
    desc.numGeneratedFrames = 4;
    desc.outputs[3].resource = NULL;
    assert(DISPATCH(desc) == FFX_API_RETURN_ERROR_PARAMETER && native_calls == 1);
    desc.outputs[3].resource = (void *)6;
    desc.header.pNext = &extension;
    assert(DISPATCH(desc) == FFX_API_RETURN_OK && native_calls == 2);
    desc.header.pNext = NULL;
    reset_state(MODE_PENDING);
    lock_promotes = 1;
    assert(DISPATCH(desc) == FFX_API_RETURN_ERROR_PARAMETER && native_calls == 0 && bridge_calls == 0);
    reset_state(MODE_PENDING);
    lock_promotes = 1;
    desc.numGeneratedFrames = 1;
    desc.header.pNext = &extension;
    assert(DISPATCH(desc) == FFX_API_RETURN_ERROR_UNKNOWN_DESCTYPE && native_calls == 0);
    desc.header.pNext = NULL;

    reset_state(CONTEXT_METALFX);
    binding.context = &ctx;
    desc = valid_generation(30);
    desc.numGeneratedFrames = 2;
    desc.outputs[1].resource = (void *)4;
    assert(generation_callback(&desc, &binding) == FFX_API_RETURN_ERROR_PARAMETER);
    assert(desc.numGeneratedFrames == 0 && bridge_calls == 0 && retired == 1);
    reset_state(CONTEXT_METALFX);
    desc = valid_generation(31);
    binding.generate = mutate_and_dispatch;
    assert(generation_callback(&desc, &binding) == FFX_API_RETURN_ERROR_PARAMETER);
    assert(desc.numGeneratedFrames == 0 && bridge_calls == 0 && retired == 1);
    reset_state(CONTEXT_METALFX);
    desc = valid_generation(32);
    binding.generate = NULL;
    assert(generation_callback(&desc, &binding) == FFX_API_RETURN_OK);
    assert(desc.numGeneratedFrames == 1 && bridge_calls == 1 && retired == 1);
}
int main(void)
{
    check_prepare();
    check_generation();
    puts("FG production dispatch normalization: OK");
    return 0;
}
'''


NATIVE_DESTROY = r'''
#include <array>
#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <memory>
#include <mutex>
#include <new>
#include <unordered_map>
#include <utility>
#include <vector>
#include "metalfx-backend.hpp"
// Only expose the native frame constructor; standard headers are already loaded.
#define private public
#include "fsr-framegeneration.hpp"
#undef private
#include "fsr-framegeneration.mm"
#include <cassert>
extern "C" id objc_initWeak(id*, id);
extern "C" id objc_loadWeakRetained(id*);
extern "C" void objc_destroyWeak(id*);
using namespace yaagl::pso::fsr::framegeneration;

int main() {
    std::array<id, 3> weak{}; // Unused factory, recorded factory, retired history.
    std::shared_ptr<const PreparedFrame> recorded;
    id<MTLDevice> device = MTLCreateSystemDefaultDevice();
    @autoreleasepool {
        auto descriptor = [MTL4CompilerDescriptor new];
        NSError* error = nil;
        auto compiler = [device newCompilerWithDescriptor:descriptor error:&error];
        [descriptor release];
        assert(device && compiler);
        auto state = std::make_shared<State>();
        state->creation.display_width = state->creation.display_height = 136;
        Command command;
        command.value.kind = transport::CommandListKind::mpl;
        command.value.device = device;
        command.value.compiler = compiler;
        auto snapshot = std::make_shared<Snapshot>();
        auto& parameters = snapshot->parameters;
        parameters.render_width = parameters.render_height = 64;
        parameters.camera_near = .1f; parameters.camera_far = 1000;
        parameters.camera_fov_vertical_radians = 1;
        parameters.view_space_to_meters = 1; parameters.frame_time_delta_ms = 16;
        snapshot->depth = privateTexture(device, MTLPixelFormatR32Float, 64, 64, MTLTextureUsageShaderRead);
        snapshot->motion = privateTexture(device, MTLPixelFormatRG16Float, 64, 64, MTLTextureUsageShaderRead);
        snapshot->depthWidth = snapshot->depthHeight = snapshot->motionWidth = snapshot->motionHeight = 64;
        for (unsigned i = 0; i < 2; ++i) {
            auto configuration = configure(*state, command, parameters,
                (id<MTLTexture>)snapshot->depth.get(), MTLPixelFormatRGBA16Float,
                MTLPixelFormatInvalid, 128 + i * 8, 128 + i * 8,
                FFX_API_BACKBUFFER_TRANSFER_FUNCTION_SRGB);
            assert(configuration);
            objc_initWeak(&weak[i], configuration->factory.get());
            snapshot->configuration = configuration;
        }
        state->historyConfiguration = state->configurations.front();
        state->history = privateTexture(device, MTLPixelFormatRGBA16Float, 128, 128, MTLTextureUsageShaderRead);
        objc_initWeak(&weak[2], state->history.get());
        auto impl = std::make_shared<PreparedFrame::Impl>();
        impl->state = state; impl->snapshot = snapshot;
        impl->kind = PreparedFrame::Impl::Kind::Generate;
        impl->generationEpoch = state->generationEpoch;
        impl->first = privateTexture(device, MTLPixelFormatRGBA16Float, 136, 136, MTLTextureUsageShaderRead);
        impl->second = privateTexture(device, MTLPixelFormatRGBA16Float, 136, 136,
                                     MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite);
        impl->dispatch.frame_id = 1;
        impl->dispatch.backbuffer_transfer_function = FFX_API_BACKBUFFER_TRANSFER_FUNCTION_SRGB;
        recorded = std::shared_ptr<const PreparedFrame>(new PreparedFrame(impl));
        // Install native ownership directly; this test does not need COM or transport mapping.
        { std::lock_guard lock(registryMutex); contexts.emplace(1, state); }
        yaagl_fsr_fg_destroy_packet destroy{};
        destroy.header = {sizeof(destroy), YAAGL_FSR_FG_BRIDGE_VERSION, YAAGL_FSR_FG_DESTROY, 0, 1};
        assert(yaagl_fsr_fg_api(YAAGL_FSR_FG_DESTROY, &destroy) == Ok);
        state.reset(); impl.reset(); snapshot.reset(); command.value = {};
        [compiler release];
    }
    for (unsigned i : {0u, 2u}) {
        id object = objc_loadWeakRetained(&weak[i]);
        assert(!object && "DESTROY kept an unrelated factory or history texture");
        [object release];
    }
    @autoreleasepool {
        id required = objc_loadWeakRetained(&weak[1]);
        assert(required && "recorded snapshot lost its required configuration");
        auto queue = [device newMTL4CommandQueue];
        auto allocator = [device newCommandAllocator];
        auto buffer = [device newCommandBuffer];
        auto fence = [device newFence];
        [buffer beginCommandBufferWithAllocator:allocator];
        auto initialization = [buffer computeCommandEncoder];
        [initialization updateFence:fence afterEncoderStages:MTLStageDispatch];
        [initialization endEncoding];
        std::shared_ptr<const ExecutionLease> lease;
        assert(recorded->encode(buffer, fence, lease) && lease);
        [buffer endCommandBuffer];
        auto options = [MTL4CommitOptions new];
        auto completed = dispatch_semaphore_create(0);
        [options addFeedbackHandler:^(id<MTL4CommitFeedback> feedback) {
            assert(!feedback.error); dispatch_semaphore_signal(completed);
        }];
        [queue commit:&buffer count:1 options:options];
        assert(dispatch_semaphore_wait(completed, dispatch_time(DISPATCH_TIME_NOW, 30 * NSEC_PER_SEC)) == 0);
        [options release]; dispatch_release(completed);
        lease.reset(); recorded.reset();
        [buffer release]; [fence release]; [allocator release]; [queue release]; [required release];
        [device release];
    }
    for (auto& reference : weak) {
        id object = objc_loadWeakRetained(&reference);
        assert(!object); [object release]; objc_destroyWeak(&reference);
    }
    puts("FG native DESTROY: unrelated resources freed; outstanding frame encoded successfully");
}
'''


class FrameGenerationNormalizationTest(unittest.TestCase):
    def test_native_destroy_ownership(self):
        native = ROOT / "d3dmetal-pso-cache"
        kernels = (native / "fsr-kernels.metal").read_text()
        self.assertNotIn(')YAAGL_METAL"', kernels)
        with tempfile.TemporaryDirectory(prefix="yaagl-fg-destroy-") as directory:
            directory = pathlib.Path(directory)
            source = directory / "fg-destroy.mm"
            binary = directory / "fg-destroy"
            source.write_text(NATIVE_DESTROY)
            (directory / "fsr-kernels.inc").write_text(
                'static const char kFsrKernelsSource[] = R"YAAGL_METAL(' + kernels + ')YAAGL_METAL";\n')
            subprocess.run(("xcrun", "clang++", "-arch", "x86_64", "-std=c++20", "-fno-objc-arc",
                            "-I", str(native), "-I", str(directory), str(source),
                            *(str(native / file) for file in ("metalfx-backend.mm",
                              "d3dmetal-transport.mm", "d3dmetal-transport-legacy.mm")),
                            "-framework", "Foundation", "-framework", "Metal",
                            "-framework", "MetalFX", "-o", str(binary)), check=True)
            subprocess.run((str(binary),), check=True)

    def test_production_dispatch(self):
        code = "\n".join((
            PROLOGUE,
            section("static enum context_mode context_mode(", "static SRWLOCK contexts_lock"),
            section("static BOOL valid_chain(", "static void initialize_packet("),
            section("struct normalized_dispatch\n", "static ffxReturnCode_t present_callback("),
            section("static ffxReturnCode_t generation_callback(", "static void build_swapchain_config("),
            section("ffxReturnCode_t ffxDispatch(", "BOOL WINAPI DllMain("),
            EPILOGUE,
        ))
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "fg.c"
            binary = pathlib.Path(directory) / "fg"
            source.write_text(code)
            subprocess.run(("clang", "-std=c11", "-Wno-deprecated-declarations", "-Wall", "-Wextra",
                            "-Werror", "-I", str(SDK), "-I", str(ROOT / "include"),
                            str(source), "-o", str(binary)), check=True)
            subprocess.run((str(binary),), check=True)


if __name__ == "__main__":
    unittest.main()
