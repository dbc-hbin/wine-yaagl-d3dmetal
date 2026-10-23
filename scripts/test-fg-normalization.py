#!/usr/bin/env python3
"""Exercise production FG dispatch, camera depth, jitter history, and ownership.
Dispatch tests capture the bridge/native DLL boundaries; native tests use real
Metal4/MetalFX objects and exact GPU completion callbacks without Wine or a game.
The camera fixture links both public camera packet producers to native encoding,
checking emitted matrices and final pixels. FG_CAMERA_NATIVE_MASK=1 additionally
reads the mover mask only on the UUID-pinned, reviewed MetalFX implementation.
The history fixture feeds public producer packets through the native registry
and real Metal4 consumption, substituting COM/D3DMetal transport and context setup.
The barrier fixture exercises real native recording with deliberately invalid
transport, not successful Wine/GPU submission.
"""

import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "dlls/amd_fidelityfx_framegeneration_dx12/main.c").read_text()
SDK = ROOT / "d3dmetal-sidecar/third-party/fidelityfx/Kits/FidelityFX/framegeneration/include"


def section(start, end):
    begin = SOURCE.index(start)
    return SOURCE[begin:SOURCE.index(end, begin)]


def d3d12_states():
    source = (ROOT / "include/d3d12.idl").read_text()
    begin = source.index("typedef enum D3D12_RESOURCE_STATES\n")
    end = source.index("} D3D12_RESOURCE_STATES;", begin)
    return source[begin:end + len("} D3D12_RESOURCE_STATES;")]


def dispatch_source():
    return "\n".join((
        d3d12_states(),
        PROLOGUE,
        section("static enum context_mode context_mode(", "static SRWLOCK contexts_lock"),
        section("static BOOL valid_chain(", "static void initialize_packet("),
        section("struct normalized_dispatch\n", "static ffxReturnCode_t present_callback("),
        section("static ffxReturnCode_t generation_callback(", "static void build_swapchain_config("),
        section("ffxReturnCode_t ffxDispatch(", "BOOL WINAPI DllMain("),
        EPILOGUE,
    ))


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
static const ffxDispatchDescHeader *last_native_descriptor;
static struct ffxDispatchDescFrameGenerationPrepareV2 last_native_prepare;
static ffxDispatchDescFrameGeneration last_native_generation;
static struct ffxConfigureDescFrameGeneration last_native_config;
static int configure_calls;
#ifdef FG_HISTORY_FIXTURE
extern ffxReturnCode_t fg_native_packet(void *packet);
#endif
#ifdef FG_RECORD_OBSERVER
extern void fg_observe_packet(const struct yaagl_fsr_fg_packet_header *packet);
static bool observe_recording;
#endif

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
#ifdef FG_RECORD_OBSERVER
    if (observe_recording) fg_observe_packet(header);
#endif
#ifdef FG_HISTORY_FIXTURE
    return fg_native_packet(packet);
#else
    return bridge_result;
#endif
}
static ffxReturnCode_t native_dispatch(ffxContext *original,
                                        const ffxDispatchDescHeader *desc)
{
    assert(original == &ctx.original);
    last_native_descriptor = desc;
    if (desc->type == FFX_API_DISPATCH_DESC_TYPE_FRAMEGENERATION_PREPARE_V2)
        memcpy(&last_native_prepare, desc, sizeof(last_native_prepare));
    else if (desc->type == FFX_API_DISPATCH_DESC_TYPE_FRAMEGENERATION)
        memcpy(&last_native_generation, desc, sizeof(last_native_generation));
    ++native_calls;
    return FFX_API_RETURN_OK;
}
static ffxReturnCode_t configure_frame_generation(
    struct fg_context *context, const struct ffxConfigureDescFrameGeneration *desc)
{
    (void)context;
    memcpy(&last_native_config, desc, sizeof(last_native_config));
    ++configure_calls;
    return FFX_API_RETURN_OK;
}
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
    memset(&last_native_prepare, 0, sizeof(last_native_prepare));
    memset(&last_native_generation, 0, sizeof(last_native_generation));
    memset(&last_native_config, 0, sizeof(last_native_config));
    last_native_descriptor = NULL;
    configure_calls = 0;
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
    desc.depth.state = FFX_API_RESOURCE_STATE_DEPTH_ATTACHMENT;
    desc.motionVectors.state = FFX_API_RESOURCE_STATE_COMPUTE_READ;
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
    desc.presentColor.state = FFX_API_RESOURCE_STATE_PRESENT;
    desc.outputs[0].state = FFX_API_RESOURCE_STATE_UNORDERED_ACCESS;
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
    desc->outputs[1].state = FFX_API_RESOURCE_STATE_UNORDERED_ACCESS;
    return ffxDispatch(&handle, &desc->header);
}
#ifdef FG_HISTORY_FIXTURE
void fg_history_begin(void)
{
    reset_state(CONTEXT_METALFX);
    ctx.display_width = ctx.display_height = 128;
}
int fg_history_prepare(unsigned frame, unsigned width, float jitter, bool reset,
                       void *list, void *depth, void *motion)
{
    struct ffxDispatchDescFrameGenerationPrepareV2 desc = valid_prepare(frame);
    desc.commandList = list;
    desc.depth.resource = depth;
    desc.motionVectors.resource = motion;
    desc.renderSize.width = width;
    desc.renderSize.height = 32;
    desc.jitterOffset.x = jitter;
    desc.jitterOffset.y = -.25f;
    desc.motionVectorScale.x = 8;
    desc.motionVectorScale.y = 4;
    desc.cameraRight[0] = desc.cameraUp[1] = desc.cameraForward[2] = 1;
    desc.reset = reset;
    return DISPATCH(desc);
}
static ffxReturnCode_t history_reentry(ffxDispatchDescFrameGeneration *desc, void *user)
{
    (void)user;
    assert(in_context_callback(&ctx));
    return DISPATCH(*desc);
}
int fg_history_generate(unsigned frame, unsigned route, bool reset,
                        void *list, void *color, void *output)
{
    ffxDispatchDescFrameGeneration desc = valid_generation(frame);
    struct callback_binding binding = {0};
    desc.commandList = list;
    desc.presentColor.resource = color;
    desc.outputs[0].resource = output;
    desc.reset = reset;
    if (!route) return DISPATCH(desc);
    binding.context = &ctx;
    if (route == 2) binding.generate = history_reentry;
    return generation_callback(&desc, &binding);
}
#endif
/* Also linked into the native camera fixture: these packets come from the
 * actual public normalizer, not a second implementation of its camera copy. */
void fg_camera_packet(struct yaagl_fsr_fg_prepare_packet *packet,
                      unsigned producer, unsigned frame, unsigned infinite)
{
    struct ffxDispatchDescFrameGenerationPrepareV2 v2 = valid_prepare(frame);
    struct ffxDispatchDescFrameGenerationPrepare v1 = {0};
    struct ffxDispatchDescFrameGenerationPrepareCameraInfo camera = {0};
    reset_state(CONTEXT_METALFX);
    ctx.depth_infinite = infinite;
    ctx.have_last_prepare_frame_id = frame > 1;
    ctx.last_prepare_frame_id = frame - 1;
    v2.renderSize.width = 192;
    v2.renderSize.height = 128;
    v2.motionVectorScale.x = v2.motionVectorScale.y = 1;
    v2.cameraFar = infinite ? INFINITY : 100.0f;
    v2.viewSpaceToMetersFactor = 2.5f;
    v2.cameraPosition[2] = -.08f * (frame - 1);
    v2.cameraRight[2] = -1;
    v2.cameraUp[1] = 1;
    v2.cameraForward[0] = 1;
    if (producer == 0) {
        assert(DISPATCH(v2) == FFX_API_RETURN_OK);
    } else {
        v1.header.type = FFX_API_DISPATCH_DESC_TYPE_FRAMEGENERATION_PREPARE;
        v1.frameID = v2.frameID;
        v1.commandList = v2.commandList;
        v1.depth = v2.depth; v1.motionVectors = v2.motionVectors;
        v1.renderSize = v2.renderSize;
        v1.motionVectorScale = v2.motionVectorScale;
        v1.frameTimeDelta = v2.frameTimeDelta;
        v1.cameraNear = v2.cameraNear; v1.cameraFar = v2.cameraFar;
        v1.cameraFovAngleVertical = v2.cameraFovAngleVertical;
        v1.viewSpaceToMetersFactor = v2.viewSpaceToMetersFactor;
        camera.header.type = FFX_API_DISPATCH_DESC_TYPE_FRAMEGENERATION_PREPARE_CAMERAINFO;
        memcpy(camera.cameraPosition, v2.cameraPosition, sizeof(camera.cameraPosition));
        memcpy(camera.cameraRight, v2.cameraRight, sizeof(camera.cameraRight));
        memcpy(camera.cameraUp, v2.cameraUp, sizeof(camera.cameraUp));
        memcpy(camera.cameraForward, v2.cameraForward, sizeof(camera.cameraForward));
        v1.header.pNext = &camera.header;
        assert(DISPATCH(v1) == FFX_API_RETURN_OK);
    }
    assert(bridge_calls == 1 && native_calls == 0 && last_prepare.camera_info_present);
    assert(!last_prepare.projection_matrix_present && !last_prepare.reset);
    assert(last_prepare.camera_position[2] == v2.cameraPosition[2]);
    assert(last_prepare.camera_right[2] == -1 && last_prepare.camera_up[1] == 1 &&
           last_prepare.camera_forward[0] == 1);
    assert(last_prepare.view_space_to_meters == 2.5f);
    *packet = last_prepare;
}
static void check_camera_producers(void)
{
    for (unsigned infinite = 0; infinite != 2; ++infinite)
        for (unsigned frame = 1; frame <= 4; ++frame) {
            struct yaagl_fsr_fg_prepare_packet v1, v2;
            fg_camera_packet(&v2, 0, frame, infinite);
            fg_camera_packet(&v1, 1, frame, infinite);
            assert(!memcmp(&v1, &v2, sizeof(v1)));
        }
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
    struct ffxDispatchDescFrameGenerationPrepareV2 prepare = valid_prepare(20);
    ffxApiHeader extension = {123456, NULL};
    struct callback_binding binding = {0};
    int start;

    reset_state(CONTEXT_METALFX);
    assert(DISPATCH(prepare) == FFX_API_RETURN_OK);
    assert(DISPATCH(desc) == FFX_API_RETURN_OK && bridge_calls == 2 && !last_generation.reset);
    assert(last_generation.present_color == 2 && last_generation.output == 3);
    /* An older, still-pending ID is legal. Missing/consumed IDs are rejected
     * by the real native registry in test_producer_native_history. */
    prepare.frameID = 21;
    assert(DISPATCH(prepare) == FFX_API_RETURN_OK);
    prepare.frameID = 22;
    assert(DISPATCH(prepare) == FFX_API_RETURN_OK);
    desc.frameID = 21;
    assert(DISPATCH(desc) == FFX_API_RETURN_OK && !last_generation.reset);
    desc.reset = true;
    desc.frameID = 20;
    assert(DISPATCH(desc) == FFX_API_RETURN_OK && last_generation.reset);
    start = bridge_calls;
    desc.numGeneratedFrames = 2;
    desc.outputs[1].resource = (void *)4;
    desc.outputs[1].state = FFX_API_RESOURCE_STATE_UNORDERED_ACCESS;
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
    snapshot.hudless.state = FFX_API_RESOURCE_STATE_COMPUTE_READ;
    assert(DISPATCH(desc) == FFX_API_RETURN_ERROR_PARAMETER && bridge_calls == start);
    snapshot.frame_id = 0;
    ctx.native_config_valid = true;
    ctx.native_config.flags = FFX_FRAMEGENERATION_FLAG_NO_SWAPCHAIN_CONTEXT_NOTIFY;
    bridge_result = FFX_API_RETURN_ERROR_RUNTIME_ERROR;
    assert(DISPATCH(desc) == FFX_API_RETURN_ERROR_RUNTIME_ERROR);
    assert(bridge_calls == start + 1 && retired == 1 && native_calls == 0);

    reset_state(CONTEXT_NATIVE);
    desc.numGeneratedFrames = 4;
    for (unsigned i = 0; i < 4; ++i)
    {
        desc.outputs[i].resource = (void *)(uintptr_t)(i + 3);
        desc.outputs[i].state = FFX_API_RESOURCE_STATE_UNORDERED_ACCESS;
    }
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
    desc.outputs[1].state = FFX_API_RESOURCE_STATE_UNORDERED_ACCESS;
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
static void check_callback_wait(void)
{
    ffxDispatchDescHeader wait = {
        FFX_API_DISPATCH_DESC_TYPE_FRAMEGENERATIONSWAPCHAIN_WAIT_FOR_PRESENTS_DX12, NULL};
    struct callback_scope scope;
    struct ffxDispatchDescFrameGenerationPrepareV2 prepare = valid_prepare(40);
    ffxDispatchDescFrameGeneration generate = valid_generation(40);

    reset_state(CONTEXT_SWAPCHAIN);
    assert(ffxDispatch(&handle, &wait) == FFX_API_RETURN_OK && native_calls == 1);
    enter_callback(&scope, &ctx);
    assert(ffxDispatch(&handle, &wait) == FFX_API_RETURN_ERROR_PARAMETER);
    assert(native_calls == 1 && bridge_calls == 0);
    leave_callback(&scope);
    assert(ffxDispatch(&handle, &wait) == FFX_API_RETURN_OK && native_calls == 2);

    reset_state(CONTEXT_METALFX);
    enter_callback(&scope, &ctx);
    assert(DISPATCH(prepare) == FFX_API_RETURN_OK);
    assert(DISPATCH(generate) == FFX_API_RETURN_OK);
    assert(bridge_calls == 2 && native_calls == 0 && last_generation.frame_id == 40);
    leave_callback(&scope);
}
struct state_case { uint32_t ffx, d3d12; };
static const struct state_case state_cases[] =
{
    {FFX_API_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_COMMON},
    {FFX_API_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_UNORDERED_ACCESS},
    {FFX_API_RESOURCE_STATE_COMPUTE_READ, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE},
    {FFX_API_RESOURCE_STATE_PIXEL_READ, D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE},
    {FFX_API_RESOURCE_STATE_PIXEL_COMPUTE_READ,
     D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE | D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE},
    {FFX_API_RESOURCE_STATE_COPY_SRC, D3D12_RESOURCE_STATE_COPY_SOURCE},
    {FFX_API_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_COPY_DEST},
    {FFX_API_RESOURCE_STATE_GENERIC_READ, D3D12_RESOURCE_STATE_GENERIC_READ},
    {FFX_API_RESOURCE_STATE_INDIRECT_ARGUMENT, D3D12_RESOURCE_STATE_INDIRECT_ARGUMENT},
    {FFX_API_RESOURCE_STATE_PRESENT, D3D12_RESOURCE_STATE_PRESENT},
    {FFX_API_RESOURCE_STATE_RENDER_TARGET, D3D12_RESOURCE_STATE_RENDER_TARGET},
    {FFX_API_RESOURCE_STATE_DEPTH_ATTACHMENT, D3D12_RESOURCE_STATE_DEPTH_WRITE},
};
static const uint32_t invalid_states[] =
{
    0,
    FFX_API_RESOURCE_STATE_COMMON | FFX_API_RESOURCE_STATE_COMPUTE_READ,
    FFX_API_RESOURCE_STATE_COPY_DEST | FFX_API_RESOURCE_STATE_PIXEL_READ,
    UINT32_C(1) << 31,
    FFX_API_RESOURCE_STATE_COMPUTE_READ | (UINT32_C(1) << 31),
};
#define ARRAY_COUNT(values) (sizeof(values) / sizeof((values)[0]))
static void retain_config(uint64_t frame_id, uint32_t state)
{
    ctx.native_config_valid = TRUE;
    ctx.native_config.HUDLessColor.resource = (void *)4;
    ctx.native_config.HUDLessColor.state = state;
    snapshot.frame_id = frame_id;
    snapshot.hudless = ctx.native_config.HUDLessColor;
}
static void check_state_mappings(void)
{
    for (unsigned i = 0; i < ARRAY_COUNT(state_cases); ++i)
    {
        const struct state_case test = state_cases[i];
        struct ffxDispatchDescFrameGenerationPrepareV2 prepare = valid_prepare(100 + i);
        ffxDispatchDescFrameGeneration generate = valid_generation(prepare.frameID);
        struct ffxDispatchDescFrameGenerationPrepareV2 original_prepare;
        ffxDispatchDescFrameGeneration original_generate;
        struct ffxConfigureDescFrameGeneration original_config;
        struct frame_config_snapshot original_snapshot;
        reset_state(MODE_PENDING);
        prepare.depth.state = prepare.motionVectors.state = test.ffx;
        generate.presentColor.state = generate.outputs[0].state = test.ffx;
        retain_config(prepare.frameID, test.ffx);
        memcpy(&original_prepare, &prepare, sizeof(prepare));
        memcpy(&original_generate, &generate, sizeof(generate));
        memcpy(&original_config, &ctx.native_config, sizeof(original_config));
        memcpy(&original_snapshot, &snapshot, sizeof(snapshot));
        for (unsigned repeat = 0; repeat < 2; ++repeat)
        {
            assert(DISPATCH(prepare) == FFX_API_RETURN_OK);
            assert(last_prepare.depth == 2 && last_prepare.motion_vectors == 3);
            assert(last_prepare.depth_state == test.d3d12);
            assert(last_prepare.motion_vectors_state == test.d3d12);
            assert(ctx.mode == CONTEXT_METALFX && native_calls == 0);
            assert(ctx.have_last_prepare_frame_id && ctx.last_prepare_frame_id == prepare.frameID);
            assert(DISPATCH(generate) == FFX_API_RETURN_OK);
            assert(last_generation.present_color == 2 && last_generation.output == 3);
            assert(last_generation.hudless_color == 4);
            assert(last_generation.present_color_state == test.d3d12);
            assert(last_generation.output_state == test.d3d12);
            assert(last_generation.hudless_color_state == test.d3d12);
            assert(bridge_calls == (int)(2 * (repeat + 1)) && retired == 0);
            assert(!memcmp(&prepare, &original_prepare, sizeof(prepare)));
            assert(!memcmp(&generate, &original_generate, sizeof(generate)));
            assert(!memcmp(&ctx.native_config, &original_config, sizeof(original_config)));
            assert(!memcmp(&snapshot, &original_snapshot, sizeof(snapshot)));
        }
        assert(configure_calls == 1);
        assert(!memcmp(&last_native_config, &original_config, sizeof(original_config)));
    }
}
static void check_invalid_prepare_states(void)
{
    const int modes[] = {MODE_PENDING, CONTEXT_METALFX};
    for (unsigned i = 0; i < ARRAY_COUNT(invalid_states); ++i)
    for (unsigned field = 0; field < 2; ++field)
    for (unsigned mode = 0; mode < ARRAY_COUNT(modes); ++mode)
    for (unsigned history = 0; history < 2; ++history)
    {
        struct ffxDispatchDescFrameGenerationPrepareV2 prepare = valid_prepare(201);
        struct ffxDispatchDescFrameGenerationPrepareV2 original;
        reset_state(modes[mode]);
        ctx.have_last_prepare_frame_id = history;
        ctx.last_prepare_frame_id = 200;
        if (field == 0) prepare.depth.state = invalid_states[i];
        else prepare.motionVectors.state = invalid_states[i];
        memcpy(&original, &prepare, sizeof(prepare));
        assert(DISPATCH(prepare) == FFX_API_RETURN_ERROR_PARAMETER);
        assert(bridge_calls == 0 && native_calls == 0 && retired == 0 && configure_calls == 0);
        assert(ctx.mode == modes[mode]);
        assert(ctx.have_last_prepare_frame_id == (BOOL)history && ctx.last_prepare_frame_id == 200);
        assert(!memcmp(&prepare, &original, sizeof(prepare)));
        prepare.depth.state = FFX_API_RESOURCE_STATE_DEPTH_ATTACHMENT;
        prepare.motionVectors.state = FFX_API_RESOURCE_STATE_COMPUTE_READ;
        assert(DISPATCH(prepare) == FFX_API_RETURN_OK);
        assert(bridge_calls == 1 && native_calls == 0 && ctx.mode == CONTEXT_METALFX);
        assert(ctx.have_last_prepare_frame_id && ctx.last_prepare_frame_id == 201);
        assert(!last_prepare.reset);
    }
}
static void check_invalid_generation_states(void)
{
    for (unsigned i = 0; i < ARRAY_COUNT(invalid_states); ++i)
    for (unsigned field = 0; field < 3; ++field)
    {
        ffxDispatchDescFrameGeneration generate = valid_generation(300);
        ffxDispatchDescFrameGeneration original;
        struct frame_config_snapshot original_snapshot;
        struct ffxConfigureDescFrameGeneration original_config;
        reset_state(CONTEXT_METALFX);
        retain_config(generate.frameID, FFX_API_RESOURCE_STATE_COMPUTE_READ);
        ctx.native_config.flags = FFX_FRAMEGENERATION_FLAG_NO_SWAPCHAIN_CONTEXT_NOTIFY;
        ctx.have_last_prepare_frame_id = TRUE;
        ctx.last_prepare_frame_id = generate.frameID;
        if (field == 0) generate.presentColor.state = invalid_states[i];
        else if (field == 1) generate.outputs[0].state = invalid_states[i];
        else snapshot.hudless.state = ctx.native_config.HUDLessColor.state = invalid_states[i];
        memcpy(&original, &generate, sizeof(generate));
        memcpy(&original_snapshot, &snapshot, sizeof(snapshot));
        memcpy(&original_config, &ctx.native_config, sizeof(original_config));
        assert(DISPATCH(generate) == FFX_API_RETURN_ERROR_PARAMETER);
        assert(bridge_calls == 0 && native_calls == 0 && retired == 0);
        assert(ctx.mode == CONTEXT_METALFX && ctx.have_last_prepare_frame_id);
        assert(ctx.last_prepare_frame_id == generate.frameID);
        assert(!memcmp(&generate, &original, sizeof(generate)));
        assert(!memcmp(&snapshot, &original_snapshot, sizeof(snapshot)));
        assert(!memcmp(&ctx.native_config, &original_config, sizeof(original_config)));
        generate.presentColor.state = FFX_API_RESOURCE_STATE_PRESENT;
        generate.outputs[0].state = FFX_API_RESOURCE_STATE_UNORDERED_ACCESS;
        snapshot.hudless.state = ctx.native_config.HUDLessColor.state = FFX_API_RESOURCE_STATE_COMPUTE_READ;
        assert(DISPATCH(generate) == FFX_API_RETURN_OK);
        assert(bridge_calls == 1 && retired == 1 && native_calls == 0);
        assert(last_generation.frame_id == 300 && !last_generation.reset);
        assert(last_generation.present_color_state == D3D12_RESOURCE_STATE_PRESENT);
        assert(last_generation.output_state == D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        assert(last_generation.hudless_color_state == D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    }
    for (unsigned i = 0; i < ARRAY_COUNT(invalid_states); ++i)
    {
        ffxDispatchDescFrameGeneration generate = valid_generation(301);
        struct callback_binding binding = {0};
        reset_state(CONTEXT_METALFX);
        retain_config(generate.frameID, invalid_states[i]);
        snapshot.hudless.resource = ctx.native_config.HUDLessColor.resource = NULL;
        assert(DISPATCH(generate) == FFX_API_RETURN_OK);
        assert(last_generation.hudless_color == 0 && bridge_calls == 1 && retired == 0);
        assert(snapshot.hudless.state == invalid_states[i]);
        assert(ctx.native_config.HUDLessColor.state == invalid_states[i]);
        // Callback failure still retires even though direct validation does not.
        reset_state(CONTEXT_METALFX);
        binding.context = &ctx;
        generate.outputs[0].state = invalid_states[i];
        assert(generation_callback(&generate, &binding) == FFX_API_RETURN_ERROR_PARAMETER);
        assert(generate.numGeneratedFrames == 0 && bridge_calls == 0 && native_calls == 0 && retired == 1);
    }
}
static void check_native_state_preservation(void)
{
    ffxApiHeader extension = {123456, NULL};
    for (unsigned i = 0; i < ARRAY_COUNT(state_cases) + ARRAY_COUNT(invalid_states); ++i)
    {
        const bool valid = i < ARRAY_COUNT(state_cases);
        const uint32_t state = valid ? state_cases[i].ffx : invalid_states[i - ARRAY_COUNT(state_cases)];
        for (unsigned route = 0; route < 4; ++route)
        {
            struct ffxDispatchDescFrameGenerationPrepareV2 prepare = valid_prepare(400);
            struct ffxDispatchDescFrameGenerationPrepareV2 original;
            struct ffxConfigureDescFrameGeneration original_config;
            struct frame_config_snapshot original_snapshot;
            // Only a genuinely submitted, valid packet may yield bridge-ineligibility.
            if (route == 3 && !valid) continue;
            reset_state(route == 0 ? CONTEXT_NATIVE : MODE_PENDING);
            retain_config(prepare.frameID, state);
            prepare.depth.state = prepare.motionVectors.state = state;
            if (route == 1) prepare.header.pNext = &extension;
            if (route == 2) prepare.flags = FFX_FRAMEGENERATION_FLAG_DRAW_DEBUG_VIEW;
            if (route == 3) bridge_result = BRIDGE_INELIGIBLE;
            memcpy(&original, &prepare, sizeof(prepare));
            memcpy(&original_config, &ctx.native_config, sizeof(original_config));
            memcpy(&original_snapshot, &snapshot, sizeof(snapshot));
            assert(DISPATCH(prepare) == FFX_API_RETURN_OK);
            assert(ctx.mode == CONTEXT_NATIVE && native_calls == 1);
            assert(bridge_calls == (route == 3));
            assert(last_native_descriptor == &prepare.header);
            assert(!memcmp(&last_native_prepare, &original, sizeof(original)));
            assert(!memcmp(&prepare, &original, sizeof(original)));
            assert(!memcmp(&ctx.native_config, &original_config, sizeof(original_config)));
            assert(!memcmp(&snapshot, &original_snapshot, sizeof(snapshot)));
            assert(!ctx.have_last_prepare_frame_id && retired == 0);
            if (route != 0)
            {
                assert(configure_calls == 1);
                assert(!memcmp(&last_native_config, &original_config, sizeof(original_config)));
            }
            if (route == 3)
            {
                assert(last_prepare.depth_state == state_cases[i].d3d12);
                assert(last_prepare.motion_vectors_state == state_cases[i].d3d12);
            }
        }
        ffxDispatchDescFrameGeneration generate = valid_generation(401), original;
        reset_state(CONTEXT_NATIVE);
        retain_config(generate.frameID, state);
        generate.presentColor.state = generate.outputs[0].state = state;
        memcpy(&original, &generate, sizeof(generate));
        assert(DISPATCH(generate) == FFX_API_RETURN_OK);
        assert(last_native_descriptor == &generate.header && native_calls == 1 && bridge_calls == 0);
        assert(!memcmp(&last_native_generation, &original, sizeof(original)));
        assert(!memcmp(&generate, &original, sizeof(original)));
        assert(snapshot.hudless.state == state && ctx.native_config.HUDLessColor.state == state);
    }
    // Once selected, result 4 is a submitted failure, never native fallback.
    reset_state(CONTEXT_METALFX);
    ffxDispatchDescFrameGeneration generate = valid_generation(402);
    retain_config(generate.frameID, FFX_API_RESOURCE_STATE_COMPUTE_READ);
    ctx.native_config.flags = FFX_FRAMEGENERATION_FLAG_NO_SWAPCHAIN_CONTEXT_NOTIFY;
    bridge_result = BRIDGE_INELIGIBLE;
    assert(DISPATCH(generate) == BRIDGE_INELIGIBLE);
    assert(bridge_calls == 1 && native_calls == 0 && retired == 1);
}
#ifdef FG_RECORD_OBSERVER
static void check_record_packets(void)
{
    const uint32_t inputs[] = {FFX_API_RESOURCE_STATE_INDIRECT_ARGUMENT,
                              FFX_API_RESOURCE_STATE_COMMON,
                              FFX_API_RESOURCE_STATE_COMPUTE_READ};
    const uint32_t outputs[] = {FFX_API_RESOURCE_STATE_PIXEL_READ,
                               FFX_API_RESOURCE_STATE_PRESENT,
                               FFX_API_RESOURCE_STATE_UNORDERED_ACCESS};
    const uint32_t hudless[] = {FFX_API_RESOURCE_STATE_GENERIC_READ,
                               FFX_API_RESOURCE_STATE_COMMON,
                               FFX_API_RESOURCE_STATE_COMPUTE_READ};
    observe_recording = true;
    for (unsigned i = 0; i < ARRAY_COUNT(inputs); ++i)
    {
        struct ffxDispatchDescFrameGenerationPrepareV2 prepare = valid_prepare(900 + i);
        ffxDispatchDescFrameGeneration generate = valid_generation(prepare.frameID);
        reset_state(MODE_PENDING);
        retain_config(prepare.frameID, hudless[i]);
        prepare.depth.state = inputs[i];
        prepare.motionVectors.state = i == 0 ? FFX_API_RESOURCE_STATE_GENERIC_READ :
                                     i == 1 ? FFX_API_RESOURCE_STATE_PRESENT :
                                              FFX_API_RESOURCE_STATE_COMPUTE_READ;
        generate.presentColor.state = inputs[i];
        generate.outputs[0].state = outputs[i];
        assert(DISPATCH(prepare) == FFX_API_RETURN_OK);
        assert(DISPATCH(generate) == FFX_API_RETURN_OK);
        assert(bridge_calls == 2 && native_calls == 0);
    }
    observe_recording = false;
}
#endif
#ifdef FG_RECORD_OBSERVER
int fg_producer_main(void)
#else
int main(void)
#endif
{
#ifdef FG_RECORD_OBSERVER
    check_record_packets();
#endif
    check_prepare();
    check_generation();
    check_callback_wait();
    check_state_mappings();
    check_invalid_prepare_states();
    check_invalid_generation_states();
    check_native_state_preservation();
    check_camera_producers();
    puts("FG production dispatch normalization: OK");
    return 0;
}
'''


NATIVE_BARRIERS = r'''
#include <array>
#include <algorithm>
#include <atomic>
#include <cassert>
#include <cmath>
#include <cstdio>
#include <initializer_list>
#include <memory>
#include <mutex>
#include <new>
#include <unordered_map>
#include <utility>
#include <vector>
#include "metalfx-backend.hpp"
#include "fsr-framegeneration.hpp"
#include "fsr-framegeneration.mm"
using namespace yaagl::pso::fsr::framegeneration;

extern "C" int fg_producer_main(void);
static std::vector<std::vector<Barrier>> captured;
static unsigned observations = 0;

static void __attribute__((ms_abi)) capture_barriers(
    void*, std::uint32_t count, const Barrier* barriers) {
    captured.emplace_back(barriers, barriers + count);
}

struct ExpectedBarrier {
    std::uint64_t address;
    std::uint32_t before, after;
};

static void check_barriers(std::initializer_list<ExpectedBarrier> expected) {
    if (expected.size() == 0) {
        assert(captured.empty() && "equal target states must not emit barriers");
        return;
    }
    assert(captured.size() == 2 && "record must restore even when transport rejects");
    assert(captured[0].size() == expected.size());
    assert(captured[1].size() == expected.size());
    unsigned i = 0;
    for (const auto& wanted : expected) {
        const auto& forward = captured[0][i];
        const auto& reverse = captured[1][i++];
        assert(reinterpret_cast<std::uintptr_t>(forward.resource) == wanted.address);
        assert(reverse.resource == forward.resource);
        assert(forward.type == 0 && reverse.type == 0);
        assert(forward.flags == 0 && reverse.flags == 0);
        assert(forward.subresource == UINT32_MAX && reverse.subresource == UINT32_MAX);
        assert(forward.before == wanted.before && forward.after == wanted.after);
        assert(reverse.before == wanted.after && reverse.after == wanted.before);
    }
}

extern "C" void fg_observe_packet(const yaagl_fsr_fg_packet_header* header) {
    assert(header->version == YAAGL_FSR_FG_BRIDGE_VERSION);
    const bool generate = header->operation == YAAGL_FSR_FG_DISPATCH;
    assert(generate || header->operation == YAAGL_FSR_FG_PREPARE);
    std::array<std::uint64_t, 8> addresses{};
    std::array<std::uint32_t, 8> states{};
    std::uint64_t frame;
    if (generate) {
        assert(header->size == sizeof(yaagl_fsr_fg_dispatch_packet));
        const auto& p = *reinterpret_cast<const yaagl_fsr_fg_dispatch_packet*>(header);
        addresses = {p.present_color, p.output, p.hudless_color};
        states = {p.present_color_state, p.output_state, p.hudless_color_state};
        frame = p.frame_id;
        assert(addresses[0] == 2 && addresses[1] == 3 && addresses[2] == 4);
    } else {
        assert(header->size == sizeof(yaagl_fsr_fg_prepare_packet));
        const auto& p = *reinterpret_cast<const yaagl_fsr_fg_prepare_packet*>(header);
        addresses = {p.depth, p.motion_vectors};
        states = {p.depth_state, p.motion_vectors_state};
        frame = p.frame_id;
        assert(addresses[0] == 2 && addresses[1] == 3);
    }
    // Only the COM barrier method is replaced. No texture mapping, runtime,
    // successful transport, or GPU submission is synthesized by this fixture.
    std::array<void*, 27> vtable{};
    vtable[26] = reinterpret_cast<void*>(&capture_barriers);
    struct FakeList { void** vtable; } list{vtable.data()};
    Command invalid_command{};
    Resources resources{};
    const auto original_states = states;
    captured.clear();
    assert(!record(&list, invalid_command, resources, {}, addresses, states,
                   generate ? 3 : 2, generate));
    assert(states == original_states);
    if (frame == 900) {
        if (generate) {
            // Raw FFX PIXEL_READ collides with D3D12 UAV, and raw FFX
            // INDIRECT_ARGUMENT collides with NON_PIXEL_SHADER_RESOURCE.
            check_barriers({
                {2, D3D12_RESOURCE_STATE_INDIRECT_ARGUMENT,
                    D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE},
                {3, D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE,
                    D3D12_RESOURCE_STATE_UNORDERED_ACCESS},
                {4, D3D12_RESOURCE_STATE_GENERIC_READ,
                    D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE},
            });
        } else {
            check_barriers({
                {2, D3D12_RESOURCE_STATE_INDIRECT_ARGUMENT,
                    D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE},
                {3, D3D12_RESOURCE_STATE_GENERIC_READ,
                    D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE},
            });
        }
    } else if (frame == 901) {
        if (generate) {
            check_barriers({
                {2, D3D12_RESOURCE_STATE_COMMON,
                    D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE},
                {3, D3D12_RESOURCE_STATE_PRESENT,
                    D3D12_RESOURCE_STATE_UNORDERED_ACCESS},
                {4, D3D12_RESOURCE_STATE_COMMON,
                    D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE},
            });
        } else {
            check_barriers({
                {2, D3D12_RESOURCE_STATE_COMMON,
                    D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE},
                {3, D3D12_RESOURCE_STATE_PRESENT,
                    D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE},
            });
        }
    } else {
        assert(frame == 902);
        check_barriers({});
    }
    ++observations;
}

static void check_alias_barriers() {
    std::array<void*, 27> vtable{};
    vtable[26] = reinterpret_cast<void*>(&capture_barriers);
    struct FakeList { void** vtable; } list{vtable.data()};
    Command invalid_command{};
    Resources resources{};
    std::array<std::uint64_t, 8> addresses{2, 3, 2};
    std::array<std::uint32_t, 8> states{
        D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE,
        D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
        D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE};
    captured.clear();
    assert(!record(&list, invalid_command, resources, {}, addresses, states, 3, true));
    // Present and HUDless name one resource: one transition and one restoration.
    check_barriers({{2, D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE,
                       D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE}});

    states[0] = states[2] = D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE;
    captured.clear();
    assert(!record(&list, invalid_command, resources, {}, addresses, states, 3, true));
    check_barriers({});

    // Contradictory initial states and read/write aliases must fail before
    // emitting any transition, not after partially mutating the command list.
    states[0] = D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE;
    states[2] = D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE;
    captured.clear();
    assert(!record(&list, invalid_command, resources, {}, addresses, states, 3, true));
    check_barriers({});
    addresses[1] = addresses[0];
    states[0] = states[1] = states[2] = D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE;
    captured.clear();
    assert(!record(&list, invalid_command, resources, {}, addresses, states, 3, true));
    check_barriers({});
}

int main() {
    assert(fg_producer_main() == 0);
    assert(observations == 6);
    check_alias_barriers();
    puts("FG native barriers: converted C packets; forward/reverse transport-failure-path recording OK");
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
    bool uncounted = false;
    releaseHudRow(uncounted);
    assert(!uncounted && hudRow.interpolating == 0 && hudRow.activity == 1);
    hudRow.interpolating = 1;
    bool counted = true;
    releaseHudRow(counted);
    assert(!counted && hudRow.interpolating == 0 && hudRow.activity == 2);
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
        parameters.reconstruct_camera_motion = 1;
        for (unsigned i = 0; i < 16; ++i)
            parameters.clip_to_previous_clip[i] = i % 5 == 0 ? 1.0f : 0.0f;
        // Public FSR Prepare does not supply optional motion extents. Native
        // preparation derives them from render/display size before mapping.
        assert(validParameters(parameters, 0));
        assert(validParameters(parameters, YAAGL_FSR_FG_DISPLAY_RESOLUTION_MOTION_VECTORS));
        parameters.render_width = 0;
        assert(!validParameters(parameters, 0));
        parameters.render_width = 64;
        snapshot->depth = privateTexture(device, MTLPixelFormatR32Float, 64, 64, MTLTextureUsageShaderRead);
        snapshot->motion = privateTexture(device, MTLPixelFormatRG16Float, 64, 64, MTLTextureUsageShaderRead);
        snapshot->depthWidth = snapshot->depthHeight = snapshot->motionWidth = snapshot->motionHeight = 64;
        for (unsigned i = 0; i < 2; ++i) {
            auto configuration = configure(*state, command, parameters,
                (id<MTLTexture>)snapshot->depth.get(), MTLPixelFormatRGBA16Float,
                MTLPixelFormatInvalid, 128 + i * 8, 128 + i * 8,
                FFX_API_BACKBUFFER_TRANSFER_FUNCTION_SRGB, false);
            assert(configuration);
            if (i == 0) objc_initWeak(&weak[0], configuration->factory.get());
            snapshot->configuration = configuration;
        }
        auto distortionConfiguration = configure(*state, command, parameters,
            (id<MTLTexture>)snapshot->depth.get(), MTLPixelFormatRGBA16Float,
            MTLPixelFormatInvalid, 136, 136,
            FFX_API_BACKBUFFER_TRANSFER_FUNCTION_SRGB, true);
        assert(distortionConfiguration);
        objc_initWeak(&weak[1], distortionConfiguration->factory.get());
        snapshot->configuration = distortionConfiguration;
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
        impl->fourth = privateTexture(device, MTLPixelFormatRGBA16Float, 136, 136,
                                      MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite);
        impl->uiAlpha = privateTexture(device, MTLPixelFormatR8Unorm, 136, 136,
                                       MTLTextureUsageShaderRead);
        impl->distortion = privateTexture(device, MTLPixelFormatRGBA8Unorm, 64, 64,
                                          MTLTextureUsageShaderRead);
        id disableInterpolation = [device newBufferWithLength:sizeof(std::uint32_t)
                                                      options:MTLResourceStorageModeShared];
        impl->disableInterpolation = Object(disableInterpolation);
        [disableInterpolation release];
        impl->dispatch.frame_id = 1;
        impl->dispatch.backbuffer_transfer_function = FFX_API_BACKBUFFER_TRANSFER_FUNCTION_SRGB;
        impl->dispatch.ui_alpha_x = impl->dispatch.ui_alpha_y = 0;
        impl->dispatch.distortion_width = impl->dispatch.distortion_height = 64;
        impl->dispatch.distortion_low_precision = 1;
        impl->dispatch.distortion_scale = 1.0f;
        impl->dispatch.distortion_bias = 0.0f;
        snapshot->production->produced = true;
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


NATIVE_JITTER = r'''
#include <array>
#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <initializer_list>
#include <memory>
#include <mutex>
#include <new>
#include <unordered_map>
#include <utility>
#include <vector>
#include "metalfx-backend.hpp"
#define private public
#include "fsr-framegeneration.hpp"
#undef private
#include "fsr-framegeneration.mm"
#include <cassert>
using namespace yaagl::pso::fsr::framegeneration;

#ifdef FG_CAMERA_FIXTURE
extern "C" void fg_camera_before_encode(id effect);
extern "C" void fg_camera_after_completion(id effect);
#endif

struct JitterFrame {
    const char* name;
    std::uint64_t frame;
    unsigned renderWidth, renderHeight;
    float jitterX, jitterY;
    // Literal expectations, in target-domain pixels and output pixels respectively.
    float cancellationX, cancellationY, pixelsX, pixelsY;
    unsigned motionWidth = 0, motionHeight = 0;
    float scaleX = 8, scaleY = 4;
    bool prepareReset = false, dispatchReset = false;
    bool previousValid = true;
    unsigned outputWidth = 128;
};

template<class Pixel>
static Object uniformTexture(id<MTLDevice> device, MTLPixelFormat format,
                             unsigned width, unsigned height, Pixel value,
                             MTLTextureUsage usage = MTLTextureUsageShaderRead) {
    auto descriptor = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:format
        width:width height:height mipmapped:NO];
    descriptor.storageMode = MTLStorageModeShared;
    descriptor.usage = usage;
    auto texture = [device newTextureWithDescriptor:descriptor];
    assert(texture);
    std::vector<Pixel> pixels(std::size_t(width) * height, value);
    [texture replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0
                 withBytes:pixels.data() bytesPerRow:width * sizeof(Pixel)];
    Object result(texture);
    [texture release];
    return result;
}

template<class Pixel>
static Object pixelTexture(id<MTLDevice> device, MTLPixelFormat format,
                           unsigned width, unsigned height,
                           const std::vector<Pixel>& pixels) {
    assert(pixels.size() == std::size_t(width) * height);
    auto result = uniformTexture(device, format, width, height, pixels.front());
    [(id<MTLTexture>)result.get()
        replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0
        withBytes:pixels.data() bytesPerRow:width * sizeof(Pixel)];
    return result;
}

static bool near(const char* name, const char* field, float actual, float expected) {
    if (std::isfinite(actual) && std::fabs(actual - expected) <= 0.0001f) return true;
    std::fprintf(stderr, "%s: %s expected %.6f, got %.6f\n",
                 name, field, expected, actual);
    return false;
}

struct JitterRunner {
    id<MTLDevice> device = MTLCreateSystemDefaultDevice();
    id<MTL4Compiler> compiler = nil;
    id<MTL4CommandQueue> queue = nil;
    unsigned failures = 0, frames = 0;

    JitterRunner() {
        auto descriptor = [MTL4CompilerDescriptor new];
        NSError* error = nil;
        compiler = [device newCompilerWithDescriptor:descriptor error:&error];
        [descriptor release];
        queue = [device newMTL4CommandQueue];
        assert(device && compiler && queue && !error);
    }
    ~JitterRunner() {
        [queue release]; [compiler release]; [device release];
    }

    std::shared_ptr<PreparedFrame> prepare(const std::shared_ptr<State>& state,
                                          const JitterFrame& test,
                                          bool configurePrepare = false) {
        auto snapshot = std::make_shared<Snapshot>();
        auto& p = snapshot->parameters;
        p.render_width = test.renderWidth; p.render_height = test.renderHeight;
        p.motion_width = test.motionWidth; p.motion_height = test.motionHeight;
        p.jitter_x = test.jitterX; p.jitter_y = test.jitterY;
        p.motion_scale_x = test.scaleX; p.motion_scale_y = test.scaleY;
        p.reset = test.prepareReset;
        p.camera_near = .1f; p.camera_far = 1000;
        p.camera_fov_vertical_radians = 1;
        p.view_space_to_meters = 1; p.frame_time_delta_ms = 16;
        assert(validParameters(p, state->creation.flags));
        const bool displayMotion = state->creation.flags &
            YAAGL_FSR_FG_DISPLAY_RESOLUTION_MOTION_VECTORS;
        snapshot->depthWidth = test.renderWidth;
        snapshot->depthHeight = test.renderHeight;
        snapshot->motionWidth = test.motionWidth ? test.motionWidth :
            displayMotion ? state->creation.display_width : test.renderWidth;
        snapshot->motionHeight = test.motionHeight ? test.motionHeight :
            displayMotion ? state->creation.display_height : test.renderHeight;
        snapshot->depth = uniformTexture(device, MTLPixelFormatR32Float,
            snapshot->depthWidth, snapshot->depthHeight, 0.5f);
        // Binary16 (+0.25, -0.5): exact, uniform object motion, no camera term.
        snapshot->motion = uniformTexture(device, MTLPixelFormatRG16Float,
            snapshot->motionWidth, snapshot->motionHeight,
            std::array<std::uint16_t, 2>{0x3400, 0xb800});
        Command command;
        command.value.kind = transport::CommandListKind::mpl;
        command.value.device = device; command.value.compiler = compiler;
        if (configurePrepare) {
            // Real Prepare inserts a render-sized configuration before Dispatch
            // selects the output-sized one. Both share the FIFO cache.
            auto renderConfiguration = configure(*state, command, p,
                (id<MTLTexture>)snapshot->depth.get(), MTLPixelFormatRGBA16Float,
                MTLPixelFormatInvalid, p.render_width, p.render_height,
                FFX_API_BACKBUFFER_TRANSFER_FUNCTION_SRGB, false);
            assert(renderConfiguration);
        }
        snapshot->configuration = configure(*state, command, p,
            (id<MTLTexture>)snapshot->depth.get(), MTLPixelFormatRGBA16Float,
            MTLPixelFormatInvalid, test.outputWidth, 128,
            FFX_API_BACKBUFFER_TRANSFER_FUNCTION_SRGB, false);
        assert(snapshot->configuration);
        // Exercise production history capture with raw public jitter and sizes.
        state->capturePreparedJitter(*snapshot, test.frame);
        assert(snapshot->hasPreviousJitter == test.previousValid);
        snapshot->production->produced = true;
        auto impl = std::make_shared<PreparedFrame::Impl>();
        impl->state = state; impl->snapshot = snapshot;
        impl->kind = PreparedFrame::Impl::Kind::Generate;
        impl->generationEpoch = state->generationEpoch;
        impl->reset = test.dispatchReset;
        // Binary16 (0.25, 0.5, 0.75, 1): every source texel is initialized.
        impl->first = uniformTexture(device, MTLPixelFormatRGBA16Float,
            state->creation.display_width, state->creation.display_height,
            std::array<std::uint16_t, 4>{0x3400, 0x3800, 0x3a00, 0x3c00});
        impl->second = privateTexture(device, MTLPixelFormatRGBA16Float,
            state->creation.display_width, state->creation.display_height,
            MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite);
        assert(impl->second);
        impl->dispatch.frame_id = test.frame;
        impl->dispatch.reset = test.dispatchReset;
        impl->dispatch.backbuffer_transfer_function = FFX_API_BACKBUFFER_TRANSFER_FUNCTION_SRGB;
        impl->dispatch.max_luminance = 1;
        return std::shared_ptr<PreparedFrame>(new PreparedFrame(impl));
    }

    void encode(const std::shared_ptr<PreparedFrame>& prepared, const JitterFrame& test,
                const std::vector<std::array<__fp16, 4>>* expectedOutput = nullptr,
                const std::vector<std::array<__fp16, 2>>* expectedMotion = nullptr,
                const std::vector<float>* expectedDepth = nullptr,
                std::vector<std::array<__fp16, 4>>* capturedOutput = nullptr,
                int expectedDisable = -1) {
        auto allocator = [device newCommandAllocator];
        auto buffer = [device newCommandBuffer];
        auto fence = [device newFence];
        assert(allocator && buffer && fence);
        [buffer beginCommandBufferWithAllocator:allocator];
        auto initialization = [buffer computeCommandEncoder];
        [initialization updateFence:fence afterEncoderStages:MTLStageDispatch];
        [initialization endEncoding];
        std::shared_ptr<const ExecutionLease> lease;
#ifdef FG_CAMERA_FIXTURE
        fg_camera_before_encode(prepared->impl_->snapshot->configuration->factory.get());
#endif
        assert(prepared->encode(buffer, fence, lease) && lease);

        id<MTLTexture> motion = nil, depth = nil;
        const FgParams* parameters = nullptr;
        unsigned parameterCount = 0, textureCount = 0;
        for (const auto& object : lease->impl_->objects) {
            if ([object.get() conformsToProtocol:@protocol(MTLBuffer)]) {
                auto candidate = (id<MTLBuffer>)object.get();
                if (candidate.storageMode != MTLStorageModeShared ||
                    candidate.length != sizeof(FgParams)) continue;
                auto values = static_cast<const FgParams*>(candidate.contents);
                if (values->motionScale[0] != 0 && values->motionScale[1] != 0) {
                    parameters = values; ++parameterCount;
                }
            }
            if ([object.get() conformsToProtocol:@protocol(MTLTexture)]) {
                auto candidate = (id<MTLTexture>)object.get();
                if (candidate.pixelFormat == MTLPixelFormatRG16Float &&
                    (candidate.usage & MTLTextureUsageShaderWrite) &&
                    candidate.width == test.outputWidth && candidate.height == 128) {
                    motion = candidate; ++textureCount;
                }
                if (candidate.pixelFormat == MTLPixelFormatR32Float &&
                    (candidate.usage & MTLTextureUsageShaderWrite) &&
                    candidate.width == test.outputWidth && candidate.height == 128)
                    depth = candidate;
            }
        }
        // No distortion input: only motion parameters have nonzero motionScale.
        assert(parameterCount == 1 && textureCount == 1);
        const NSUInteger alignment = std::max(
            [device minimumTextureBufferAlignmentForPixelFormat:motion.pixelFormat],
            [device minimumTextureBufferAlignmentForPixelFormat:MTLPixelFormatR32Float]);
        const NSUInteger pixelBytes = sizeof(std::array<std::uint16_t, 2>);
        const NSUInteger rowBytes =
            (motion.width * pixelBytes + alignment - 1) / alignment * alignment;
        if (expectedMotion) assert(expectedMotion->size() == test.outputWidth * 128);
        if (expectedDepth) assert(depth && expectedDepth->size() == test.outputWidth * 128);
        auto readback = [device newBufferWithLength:rowBytes * 128 * (expectedDepth ? 2 : 1)
                                           options:MTLResourceStorageModeShared];
        assert(readback);
        std::memset(readback.contents, 0xff, readback.length);
        id<MTLTexture> finalOutput = (id<MTLTexture>)prepared->impl_->second.get();
        id<MTLBuffer> colorReadback = nil;
        NSUInteger colorRowBytes = 0;
        if (expectedOutput || capturedOutput) {
            assert(finalOutput.pixelFormat == MTLPixelFormatRGBA16Float);
            if (expectedOutput)
                assert(expectedOutput->size() == finalOutput.width * finalOutput.height);
            const NSUInteger colorAlignment =
                [device minimumTextureBufferAlignmentForPixelFormat:finalOutput.pixelFormat];
            colorRowBytes = (finalOutput.width * sizeof(std::array<__fp16, 4>) +
                            colorAlignment - 1) / colorAlignment * colorAlignment;
            colorReadback = [device newBufferWithLength:colorRowBytes * finalOutput.height
                                                options:MTLResourceStorageModeShared];
            assert(colorReadback);
            std::memset(colorReadback.contents, 0xff, colorReadback.length);
        }
        auto residency = (id<MTLResidencySet>)lease->impl_->residency.get();
        assert(residency);
        [residency addAllocation:readback];
        if (colorReadback) [residency addAllocation:colorReadback];
        [residency commit];
        [buffer useResidencySet:residency];
        auto copy = [buffer computeCommandEncoder];
        [copy waitForFence:fence beforeEncoderStages:MTLStageBlit];
        [copy copyFromTexture:motion sourceSlice:0 sourceLevel:0
                 sourceOrigin:MTLOriginMake(0, 0, 0)
                   sourceSize:MTLSizeMake(test.outputWidth, 128, 1)
                     toBuffer:readback destinationOffset:0
       destinationBytesPerRow:rowBytes destinationBytesPerImage:rowBytes * 128];
        if (expectedDepth) {
            [copy copyFromTexture:depth sourceSlice:0 sourceLevel:0
                     sourceOrigin:MTLOriginMake(0, 0, 0)
                       sourceSize:MTLSizeMake(test.outputWidth, 128, 1)
                         toBuffer:readback destinationOffset:rowBytes * 128
           destinationBytesPerRow:rowBytes destinationBytesPerImage:rowBytes * 128];
        }
        if (colorReadback) {
            [copy copyFromTexture:finalOutput sourceSlice:0 sourceLevel:0
                     sourceOrigin:MTLOriginMake(0, 0, 0)
                       sourceSize:MTLSizeMake(finalOutput.width, finalOutput.height, 1)
                         toBuffer:colorReadback destinationOffset:0
           destinationBytesPerRow:colorRowBytes
         destinationBytesPerImage:colorRowBytes * finalOutput.height];
        }
        [copy endEncoding];
        [buffer endCommandBuffer];
        auto options = [MTL4CommitOptions new];
        auto completed = dispatch_semaphore_create(0);
        [options addFeedbackHandler:^(id<MTL4CommitFeedback> feedback) {
            if (feedback.error)
                std::fprintf(stderr, "%s: GPU error: %s\n", test.name,
                             feedback.error.description.UTF8String);
            assert(!feedback.error);
            dispatch_semaphore_signal(completed);
        }];
        [queue commit:&buffer count:1 options:options];
        assert(dispatch_semaphore_wait(completed,
            dispatch_time(DISPATCH_TIME_NOW, 30 * NSEC_PER_SEC)) == 0);
#ifdef FG_CAMERA_FIXTURE
        fg_camera_after_completion(prepared->impl_->snapshot->configuration->factory.get());
#endif

        bool good = near(test.name, "cancellation.x", parameters->jitterCancellation[0],
                         test.cancellationX);
        good &= near(test.name, "cancellation.y", parameters->jitterCancellation[1],
                     test.cancellationY);
        // Read actual GPU-normalized vectors, not an interpolated-color MAE.
        // All pixels must match, including edges; stop diagnostics at the first mismatch.
        bool pixelsGood = true;
        for (unsigned y = 0; y < 128 && pixelsGood; ++y) {
            for (unsigned x = 0; x < test.outputWidth && pixelsGood; ++x) {
                __fp16 value[2];
                std::memcpy(value, static_cast<const char*>(readback.contents) +
                            y * rowBytes + x * sizeof(value), sizeof(value));
                const auto index = y * test.outputWidth + x;
                pixelsGood = near(test.name, "GPU motion.x", float(value[0]),
                    expectedMotion ? float((*expectedMotion)[index][0]) : test.pixelsX);
                pixelsGood &= near(test.name, "GPU motion.y", float(value[1]),
                    expectedMotion ? float((*expectedMotion)[index][1]) : test.pixelsY);
                if (expectedDepth) {
                    float actualDepth;
                    std::memcpy(&actualDepth, static_cast<const char*>(readback.contents) +
                        rowBytes * 128 + y * rowBytes + x * sizeof(float), sizeof(float));
                    pixelsGood &= near(test.name, "GPU depth", actualDepth,
                                       (*expectedDepth)[index]);
                }
                if (!pixelsGood) std::fprintf(stderr, "  pixel (%u, %u)\n", x, y);
            }
        }
        bool colorGood = true;
        if (capturedOutput) {
            capturedOutput->resize(finalOutput.width * finalOutput.height);
            for (NSUInteger y = 0; y < finalOutput.height; ++y)
                std::memcpy(capturedOutput->data() + y * finalOutput.width,
                    static_cast<const char*>(colorReadback.contents) + y * colorRowBytes,
                    finalOutput.width * sizeof((*capturedOutput)[0]));
        }
        if (expectedOutput) {
            // Read the actual post-MetalFX, post-encode_scatter destination.
            // Allow half-float / transfer-function rounding, not a second UI blend.
            for (unsigned y = 0; y < finalOutput.height && colorGood; ++y) {
                for (unsigned x = 0; x < finalOutput.width && colorGood; ++x) {
                    std::array<__fp16, 4> actual;
                    std::memcpy(actual.data(),
                        static_cast<const char*>(colorReadback.contents) +
                        y * colorRowBytes + x * sizeof(actual), sizeof(actual));
                    const auto& expected = (*expectedOutput)[y * finalOutput.width + x];
                    for (unsigned channel = 0; channel < 4; ++channel) {
                        if (std::isfinite(float(actual[channel])) &&
                            std::fabs(float(actual[channel]) - float(expected[channel])) <= .004f)
                            continue;
                        std::fprintf(stderr,
                            "%s: final GPU color (%u, %u) channel %u: "
                            "single composition expected %.6f, got %.6f\n",
                            test.name, x, y, channel,
                            float(expected[channel]), float(actual[channel]));
                        colorGood = false;
                        break;
                    }
                }
            }
        }
        if (expectedDisable >= 0) {
            auto hint = (id<MTLBuffer>)prepared->impl_->disableInterpolation.get();
            assert(hint && hint.storageMode == MTLStorageModeShared);
            const auto* words = static_cast<const std::uint32_t*>(hint.contents);
            // Read the actual destination of the production GPU copy, after the
            // commit callback, including guard words around its nonzero offset.
            const bool hintGood = words[0] == 0xfeedfaceu &&
                words[1] == unsigned(expectedDisable) && words[2] == 0xfeedfaceu;
            if (!hintGood)
                std::fprintf(stderr, "%s: cache-history GPU disable_interpolation "
                    "expected %d, got %u (guards %08x/%08x)\n",
                    test.name, expectedDisable, words[1], words[0], words[2]);
            good &= hintGood;
        }
        failures += !(good && pixelsGood && colorGood); ++frames;
        [options release]; dispatch_release(completed);
        // The execution lease and readback allocation outlive completion and all reads.
        lease.reset();
        [colorReadback release];
        [readback release]; [buffer release]; [fence release]; [allocator release];
    }

    void spatialJitter(bool displayMotion) {
        @autoreleasepool {
            auto state = std::make_shared<State>();
            // Jitter-free MV values still live on a jittered low-resolution grid.
            state->creation.flags = displayMotion
                ? YAAGL_FSR_FG_DISPLAY_RESOLUTION_MOTION_VECTORS : 0;
            state->creation.display_width = state->creation.display_height = 128;
            const unsigned motionSize = displayMotion ? 128 : 64;
            std::vector<float> depths(64 * 64);
            std::vector<std::array<__fp16, 2>> motions(motionSize * motionSize);
            for (unsigned y = 0; y < 64; ++y)
                for (unsigned x = 0; x < 64; ++x)
                    depths[y * 64 + x] = .125f + float(y * 64 + x) / 8192.f;
            for (unsigned y = 0; y < motionSize; ++y)
                for (unsigned x = 0; x < motionSize; ++x)
                    motions[y * motionSize + x] = {__fp16(x / 8.f), __fp16(y / 8.f)};
            for (std::uint64_t frame = 1; frame <= 2; ++frame) {
                JitterFrame test{
                    .name = displayMotion ? "display-grid spatial jitter" : "render-grid spatial jitter",
                    .frame = frame, .renderWidth = 64, .renderHeight = 64,
                    .jitterX = frame == 1 ? .5f : -.5f,
                    .jitterY = frame == 1 ? -.5f : .5f,
                    .cancellationX = 0, .cancellationY = 0, .pixelsX = 0, .pixelsY = 0,
                    .scaleX = 1, .scaleY = 1, .previousValid = frame != 1
                };
                auto prepared = prepare(state, test);
                auto& snapshot = *prepared->impl_->snapshot;
                [(id<MTLTexture>)snapshot.depth.get()
                    replaceRegion:MTLRegionMake2D(0, 0, 64, 64) mipmapLevel:0
                    withBytes:depths.data() bytesPerRow:64 * sizeof(float)];
                [(id<MTLTexture>)snapshot.motion.get()
                    replaceRegion:MTLRegionMake2D(0, 0, motionSize, motionSize) mipmapLevel:0
                    withBytes:motions.data() bytesPerRow:motionSize * sizeof(motions[0])];
                std::vector<float> expectedDepth(128 * 128);
                std::vector<std::array<__fp16, 2>> expectedMotion(128 * 128);
                for (unsigned y = 0; y < 128; ++y) {
                    for (unsigned x = 0; x < 128; ++x) {
                        // A half-render-pixel jitter at 2x shifts the doubled
                        // texel runs by one output pixel; clamp the uncovered edge.
                        const unsigned sx = frame == 1 ? std::min((x + 1) / 2, 63u)
                                                        : (x ? (x - 1) / 2 : 0);
                        const unsigned sy = frame == 2 ? std::min((y + 1) / 2, 63u)
                                                        : (y ? (y - 1) / 2 : 0);
                        const auto index = y * 128 + x;
                        expectedDepth[index] = depths[sy * 64 + sx];
                        expectedMotion[index] = displayMotion
                            ? motions[y * motionSize + x]
                            : std::array<__fp16, 2>{__fp16(sx / 4.f), __fp16(sy / 4.f)};
                    }
                }
                encode(prepared, test, nullptr, &expectedMotion, &expectedDepth);
            }
            releaseHudRow(state->hudCounted);
        }
    }

    void uiComposition(bool explicitUi, bool inferredUi = false) {
        @autoreleasepool {
            auto state = std::make_shared<State>();
            state->creation.display_width = 128;
            state->creation.display_height = 128;
            using Pixel = std::array<__fp16, 4>;
            const Pixel scene{.25f, .5f, .75f, 1.f};
            // UI-only premultiplied RGBA, NOT a precomposited scene. The transparent
            // quadrant and distinct translucent patches expose MetalFX treating
            // this input as precomposited UI before encode_scatter blends it again.
            const std::array<Pixel, 4> patches{{
                {0.f, 0.f, 0.f, 0.f}, {.5f, 0.f, 0.f, .5f},
                {0.f, .25f, 0.f, .25f}, {0.f, 0.f, .75f, .75f}
            }};
            std::vector<Pixel> uiPixels(128 * 128), expected(128 * 128);
            for (unsigned y = 0; y < 128; ++y) {
                for (unsigned x = 0; x < 128; ++x) {
                    const auto& overlay = patches[(y / 64) * 2 + x / 64];
                    const auto index = y * 128 + x;
                    uiPixels[index] = overlay;
                    // Literal public contract: one composition in supplied sRGB
                    // values, not linear-light blending or a MetalFX output oracle.
                    for (unsigned channel = 0; channel < 4; ++channel)
                        expected[index][channel] = explicitUi || inferredUi
                            ? float(overlay[channel]) +
                              (1.f - float(overlay[3])) * float(scene[channel])
                            : float(scene[channel]);
                }
            }
            // Identical scene, depth, jitter and zero motion across all frames:
            // warm up history twice, then check two steady-state generated outputs.
            for (std::uint64_t frame = 1; frame <= 4; ++frame) {
                JitterFrame test{
                    .name = explicitUi ? "explicit UI single composition" :
                        inferredUi ? "inferred UI composition" : "no UI color control",
                    .frame = frame, .renderWidth = 128, .renderHeight = 128,
                    .jitterX = 0, .jitterY = 0,
                    .cancellationX = 0, .cancellationY = 0, .pixelsX = 0, .pixelsY = 0,
                    .previousValid = frame != 1
                };
                auto prepared = prepare(state, test);
                auto& impl = *prepared->impl_;
                impl.snapshot->motion = uniformTexture(device, MTLPixelFormatRG16Float,
                    128, 128, std::array<std::uint16_t, 2>{0, 0});
                if (explicitUi || inferredUi) {
                    impl.third = impl.first; // HUDless scene.
                    impl.first = uniformTexture(device, MTLPixelFormatRGBA16Float,
                        128, 128, scene);
                    [(id<MTLTexture>)impl.first.get()
                        replaceRegion:MTLRegionMake2D(0, 0, 128, 128) mipmapLevel:0
                        withBytes:expected.data() bytesPerRow:128 * sizeof(Pixel)];
                    if (explicitUi) {
                        impl.ui = uniformTexture(device, MTLPixelFormatRGBA16Float,
                            128, 128, Pixel{0, 0, 0, 0});
                        [(id<MTLTexture>)impl.ui.get()
                            replaceRegion:MTLRegionMake2D(0, 0, 128, 128) mipmapLevel:0
                            withBytes:uiPixels.data() bytesPerRow:128 * sizeof(Pixel)];
                    }
                    Command command;
                    command.value.kind = transport::CommandListKind::mpl;
                    command.value.device = device; command.value.compiler = compiler;
                    // Public dispatch selects the explicit d.ui texture format;
                    // Invalid here would hide the erroneous native UI allocation.
                    impl.snapshot->configuration = configure(*state, command,
                        impl.snapshot->parameters, (id<MTLTexture>)impl.snapshot->depth.get(),
                        MTLPixelFormatRGBA16Float, MTLPixelFormatRGBA16Float,
                        128, 128, FFX_API_BACKBUFFER_TRANSFER_FUNCTION_SRGB, false);
                    assert(impl.snapshot->configuration);
                }
                encode(prepared, test, frame >= 3 ? &expected : nullptr);
            }
            releaseHudRow(state->hudCounted);
        }
    }

    void alphaMask() {
        @autoreleasepool {
            using Pixel = std::array<__fp16, 4>;
            // Independent interpolators receive identical scene/motion/history.
            // Include transparent and translucent UI over a moving scene.
            std::array<std::shared_ptr<State>, 3> states;
            for (auto& state : states) {
                state = std::make_shared<State>();
                state->creation.display_width = state->creation.display_height = 128;
            }
            unsigned sensitivePixels = 0;
            for (std::uint64_t frame = 1; frame <= 6; ++frame) {
                std::vector<Pixel> scene(128 * 128), overlay(128 * 128), present(128 * 128);
                for (unsigned y = 0; y < 128; ++y)
                    for (unsigned x = 0; x < 128; ++x) {
                        // A translating, nonuniform scene avoids the static-color
                        // identity that hides a wrong alpha texture binding.
                        const unsigned sx = (x + 4 * unsigned(frame)) % 128;
                        scene[y * 128 + x] = {
                            __fp16(.25f + .5f * ((sx / 8 + y / 16) & 1u)),
                            __fp16(.125f + sx / 256.f),
                            __fp16(.25f + y / 256.f), 1};
                        const float alpha = float(x / 32) * .25f;
                        overlay[y * 128 + x] = {
                            __fp16(.75f * alpha), __fp16(.125f * alpha),
                            __fp16(.25f * alpha), __fp16(alpha)};
                        for (unsigned c = 0; c < 4; ++c)
                            present[y * 128 + x][c] = float(overlay[y * 128 + x][c]) +
                                (1.f - alpha) * float(scene[y * 128 + x][c]);
                    }
                std::vector<Pixel> control, explicitOutput;
                for (unsigned mode = 0; mode < states.size(); ++mode) {
                    JitterFrame test{
                        .name = mode == 0 ? "alpha no-UI control" :
                            mode == 1 ? "alpha explicit UI control" : "alpha mask reconstruction",
                        .frame = frame, .renderWidth = 128, .renderHeight = 128,
                        .jitterX = 0, .jitterY = 0,
                        .cancellationX = 0, .cancellationY = 0, .pixelsX = 4, .pixelsY = 0,
                        .scaleX = 16, .scaleY = 4, .previousValid = frame != 1
                    };
                    auto prepared = prepare(states[mode], test);
                    auto& impl = *prepared->impl_;
                    impl.first = pixelTexture(device, MTLPixelFormatRGBA16Float, 128, 128, scene);
                    impl.snapshot->motion = uniformTexture(device, MTLPixelFormatRG16Float,
                        128, 128, std::array<__fp16, 2>{.25f, 0});
                    if (mode != 0) impl.third = impl.first;
                    if (mode == 1) {
                        impl.first = pixelTexture(device, MTLPixelFormatRGBA16Float,
                            128, 128, present);
                        impl.ui = pixelTexture(device, MTLPixelFormatRGBA16Float,
                            128, 128, overlay);
                        Command command;
                        command.value.kind = transport::CommandListKind::mpl;
                        command.value.device = device; command.value.compiler = compiler;
                        impl.snapshot->configuration = configure(*states[mode], command,
                            impl.snapshot->parameters, (id<MTLTexture>)impl.snapshot->depth.get(),
                            MTLPixelFormatRGBA16Float, MTLPixelFormatRGBA16Float,
                            128, 128, FFX_API_BACKBUFFER_TRANSFER_FUNCTION_SRGB, false);
                        assert(impl.snapshot->configuration);
                    } else if (mode == 2) {
                        // Distinct alpha/present origins; pad both inputs so a
                        // wrong present binding still reads defined texels.
                        std::vector<Pixel> padded(136 * 140, Pixel{.625f, .25f, .5f, 1});
                        std::vector<float> alpha(136 * 140, 1.f);
                        for (unsigned y = 0; y < 128; ++y)
                            for (unsigned x = 0; x < 128; ++x) {
                                padded[y * 136 + x] = present[y * 128 + x];
                                alpha[(y + 9) * 136 + x + 5] = float(overlay[y * 128 + x][3]);
                            }
                        impl.first = pixelTexture(device, MTLPixelFormatRGBA16Float,
                            136, 140, padded);
                        impl.uiAlpha = pixelTexture(device, MTLPixelFormatR32Float,
                            136, 140, alpha);
                        impl.dispatch.ui_alpha_x = 5; impl.dispatch.ui_alpha_y = 9;
                        // Copy only the active present area, not its padding.
                        impl.dispatch.output_subrect_only = true;
                    }
                    std::vector<Pixel> compositedControl;
                    if (mode == 1) {
                        compositedControl.resize(control.size());
                        for (std::size_t i = 0; i < control.size(); ++i)
                            for (unsigned c = 0; c < 4; ++c)
                                compositedControl[i][c] = float(overlay[i][c]) +
                                    (1.f - float(overlay[i][3])) * float(control[i][c]);
                    }
                    const auto* expected = frame < 3 || mode == 0 ? nullptr :
                        mode == 1 ? &compositedControl : &explicitOutput;
                    encode(prepared, test, expected, nullptr, nullptr,
                        mode == 0 ? &control : mode == 1 ? &explicitOutput : nullptr);
                    if (mode == 0 && frame >= 3)
                        for (std::size_t i = 0; i < control.size(); ++i)
                            if (std::isfinite(float(control[i][0])) &&
                                std::fabs(float(control[i][0]) - float(scene[i][0])) > .02f)
                                ++sensitivePixels;
                }
            }
            // Do not let equal scene/interpolated colors hide the wrong alpha binding.
            if (sensitivePixels < 128) {
                std::fprintf(stderr, "alpha sensitivity: only %u generated pixels differ "
                    "from current scene (need 128)\n", sensitivePixels);
                ++failures;
            }
            std::fprintf(stderr, "alpha sensitivity: %u changed control pixels\n", sensitivePixels);
            for (auto& state : states) releaseHudRow(state->hudCounted);
        }
    }

    void outputOrigin() {
        @autoreleasepool {
            using Pixel = std::array<__fp16, 4>;
            constexpr unsigned width = 192, height = 160, activeWidth = 64;
            constexpr unsigned outputX = 96, outputY = 16;
            const Pixel sentinel{.0625f, .9375f, .125f, 1};
            std::array<std::shared_ptr<State>, 2> states;
            for (auto& state : states) {
                state = std::make_shared<State>();
                state->creation.display_width = width;
                state->creation.display_height = height;
            }
            std::vector<Pixel> scene(width * height);
            for (unsigned y = 0; y < height; ++y)
                for (unsigned x = 0; x < width; ++x)
                    scene[y * width + x] = {__fp16(.25f + x / 512.f),
                        __fp16(.125f + y / 512.f), .5f, 1};
            for (std::uint64_t frame = 1; frame <= 4; ++frame) {
                std::vector<Pixel> control, expected(width * height, sentinel);
                for (unsigned mode = 0; mode < states.size(); ++mode) {
                    JitterFrame test{
                        .name = mode == 0 ? "output-origin control" : "output-origin independent",
                        .frame = frame, .renderWidth = 64, .renderHeight = 128,
                        .jitterX = 0, .jitterY = 0,
                        .cancellationX = 0, .cancellationY = 0, .pixelsX = 0, .pixelsY = 0,
                        .previousValid = frame != 1, .outputWidth = activeWidth
                    };
                    auto prepared = prepare(states[mode], test);
                    auto& impl = *prepared->impl_;
                    impl.first = pixelTexture(device, MTLPixelFormatRGBA16Float,
                        width, height, scene);
                    impl.second = uniformTexture(device, MTLPixelFormatRGBA16Float,
                        width, height, sentinel,
                        MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite);
                    impl.snapshot->motion = uniformTexture(device, MTLPixelFormatRG16Float,
                        64, 128, std::array<__fp16, 2>{0, 0});
                    auto& d = impl.dispatch;
                    d.generation_rect_width = activeWidth; d.generation_rect_height = 128;
                    d.independent_subrects = true; d.output_subrect_only = true;
                    d.present_x = 16; d.present_y = 8;
                    d.output_x = mode == 0 ? 0 : outputX;
                    d.output_y = mode == 0 ? 0 : outputY;
                    if (mode == 1)
                        for (unsigned y = 0; y < 128; ++y)
                            std::copy_n(control.data() + y * width, activeWidth,
                                expected.data() + (y + outputY) * width + outputX);
                    // Compare the full destination: generated pixels belong only
                    // at output_x/y, and every surrounding sentinel must survive.
                    encode(prepared, test, mode == 1 ? &expected : nullptr,
                        nullptr, nullptr, mode == 0 ? &control : nullptr);
                }
            }
            for (auto& state : states) releaseHudRow(state->hudCounted);
        }
    }

    void cacheHistory() {
        @autoreleasepool {
            auto state = std::make_shared<State>();
            state->creation.display_width = state->creation.display_height = 128;
            Object previousFactory, originalFactory;
            unsigned replacements = 0, continuous = 0;
            for (std::uint64_t frame = 1; frame <= 11; ++frame) {
                const unsigned renderSize = 64 + std::min(unsigned(frame - 1), 8u);
                JitterFrame test{
                    .name = "cache-history render-size pressure",
                    .frame = frame, .renderWidth = renderSize, .renderHeight = renderSize,
                    .jitterX = 0, .jitterY = 0,
                    .cancellationX = 0, .cancellationY = 0,
                    .pixelsX = 0, .pixelsY = 0,
                    .previousValid = frame != 1
                };
                auto prepared = prepare(state, test, true);
                auto& impl = *prepared->impl_;
                impl.snapshot->motion = uniformTexture(device, MTLPixelFormatRG16Float,
                    renderSize, renderSize, std::array<__fp16, 2>{0, 0});
                const auto factory = impl.snapshot->configuration->factory;
                assert(factory);
                const bool changed = previousFactory && previousFactory.get() != factory.get();
                const int expectedDisable = frame == 1 || changed ? 1 : 0;
                replacements += changed;
                continuous += previousFactory && !changed;
                if (frame == 1) originalFactory = factory; // Prevent address reuse after eviction.
                auto hint = [device newBufferWithLength:3 * sizeof(std::uint32_t)
                                                options:MTLResourceStorageModeShared];
                assert(hint);
                auto* words = static_cast<std::uint32_t*>(hint.contents);
                words[0] = words[1] = words[2] = 0xfeedfaceu;
                impl.disableInterpolation = Object(hint);
                impl.disableInterpolationOffset = sizeof(std::uint32_t);
                impl.dispatch.disable_interpolation_offset = sizeof(std::uint32_t);
                [hint release];
                std::fprintf(stderr, "cache-history render=%u factory=%p previous=%p expected_reset=%d\n",
                    renderSize, (void*)factory.get(), (void*)previousFactory.get(), expectedDisable);
                encode(prepared, test, nullptr, nullptr, nullptr, nullptr, expectedDisable);
                previousFactory = factory;
            }
            if (!replacements || !continuous) {
                std::fprintf(stderr, "cache-history pressure failed to exercise both identities: "
                    "%u replacements, %u continuous\n", replacements, continuous);
                ++failures;
            }
            releaseHudRow(state->hudCounted);
        }
    }

    void scenario(std::uint32_t flags, std::initializer_list<JitterFrame> tests,
                  bool prepareAhead = false, unsigned displayWidth = 128) {
        @autoreleasepool {
            auto state = std::make_shared<State>();
            state->creation.flags = flags;
            state->creation.display_width = displayWidth;
            state->creation.display_height = 128;
            std::vector<std::shared_ptr<PreparedFrame>> pending;
            for (const auto& test : tests) {
                auto prepared = prepare(state, test);
                if (prepareAhead) pending.push_back(std::move(prepared));
                else encode(prepared, test);
            }
            if (prepareAhead) {
                unsigned index = 0;
                for (const auto& test : tests) encode(pending[index++], test);
            }
            releaseHudRow(state->hudCounted);
        }
    }
};

int main() {
    @autoreleasepool {
        JitterRunner runner;
        constexpr auto jittered = YAAGL_FSR_FG_MOTION_VECTORS_JITTERED;
        constexpr auto display = jittered | YAAGL_FSR_FG_DISPLAY_RESOLUTION_MOTION_VECTORS;
        runner.scenario(jittered, {
            {.name = "render first", .frame = 1, .renderWidth = 64, .renderHeight = 32,
             .jitterX = .5f, .jitterY = -.25f,
             .cancellationX = 0, .cancellationY = 0, .pixelsX = 4, .pixelsY = -8,
             .previousValid = false},
            {.name = "render omitted extents", .frame = 2, .renderWidth = 64, .renderHeight = 32,
             .jitterX = -.5f, .jitterY = .25f,
             .cancellationX = 1, .cancellationY = -.5f, .pixelsX = 2, .pixelsY = -6},
        });
        runner.scenario(display, {
            {.name = "display first", .frame = 1, .renderWidth = 64, .renderHeight = 32,
             .jitterX = .5f, .jitterY = -.25f,
             .cancellationX = 0, .cancellationY = 0, .pixelsX = 4, .pixelsY = -4,
             .scaleX = 16, .scaleY = 8, .previousValid = false},
            {.name = "display omitted extents", .frame = 2, .renderWidth = 64, .renderHeight = 32,
             .jitterX = -.5f, .jitterY = .25f,
             .cancellationX = 2, .cancellationY = -2, .pixelsX = 2, .pixelsY = -2,
             .scaleX = 16, .scaleY = 8},
        });
        runner.scenario(jittered, {
            {.name = "resize raw first", .frame = 1, .renderWidth = 64, .renderHeight = 32,
             .jitterX = .5f, .jitterY = -.25f,
             .cancellationX = 0, .cancellationY = 0, .pixelsX = -4, .pixelsY = -8,
             .scaleX = -8, .previousValid = false},
            {.name = "resize equal raw signed scale", .frame = 2, .renderWidth = 32, .renderHeight = 64,
             .jitterX = .5f, .jitterY = -.25f,
             .cancellationX = -.25f, .cancellationY = -.25f, .pixelsX = -7, .pixelsY = -3.5f,
             .scaleX = -8},
        });
        runner.scenario(jittered, {
            {.name = "resize normalized first", .frame = 1, .renderWidth = 64, .renderHeight = 32,
             .jitterX = .5f, .jitterY = -.25f,
             .cancellationX = 0, .cancellationY = 0, .pixelsX = 4, .pixelsY = -8,
             .previousValid = false},
            {.name = "resize equal normalized", .frame = 2, .renderWidth = 32, .renderHeight = 64,
             .jitterX = .25f, .jitterY = -.5f,
             .cancellationX = 0, .cancellationY = 0, .pixelsX = 8, .pixelsY = -4},
        });
        runner.scenario(display, {
            {.name = "explicit source first", .frame = 1, .renderWidth = 64, .renderHeight = 32,
             .jitterX = .5f, .jitterY = -.25f,
             .cancellationX = 0, .cancellationY = 0, .pixelsX = 4, .pixelsY = -4,
             .motionWidth = 48, .motionHeight = 24, .scaleX = 16, .scaleY = 8,
             .previousValid = false},
            {.name = "display target not source", .frame = 2, .renderWidth = 64, .renderHeight = 32,
             .jitterX = -.5f, .jitterY = .25f,
             .cancellationX = 2, .cancellationY = -2, .pixelsX = 2, .pixelsY = -2,
             .motionWidth = 48, .motionHeight = 24, .scaleX = 16, .scaleY = 8},
        });
        runner.scenario(0, {
            {.name = "flag off first", .frame = 1, .renderWidth = 64, .renderHeight = 32,
             .jitterX = .5f, .jitterY = -.25f,
             .cancellationX = 0, .cancellationY = 0, .pixelsX = 4, .pixelsY = -8,
             .previousValid = false},
            {.name = "flag off pair", .frame = 2, .renderWidth = 64, .renderHeight = 32,
             .jitterX = -.5f, .jitterY = .25f,
             .cancellationX = 0, .cancellationY = 0, .pixelsX = 4, .pixelsY = -8},
        });
        runner.scenario(jittered, {
            {.name = "gap first", .frame = 1, .renderWidth = 64, .renderHeight = 32,
             .jitterX = .5f, .jitterY = -.25f,
             .cancellationX = 0, .cancellationY = 0, .pixelsX = 4, .pixelsY = -8,
             .previousValid = false},
            {.name = "prepare gap", .frame = 3, .renderWidth = 64, .renderHeight = 32,
             .jitterX = -.5f, .jitterY = .25f,
             .cancellationX = 0, .cancellationY = 0, .pixelsX = 4, .pixelsY = -8,
             .previousValid = false},
            {.name = "pair after gap", .frame = 4, .renderWidth = 64, .renderHeight = 32,
             .jitterX = .5f, .jitterY = -.25f,
             .cancellationX = -1, .cancellationY = .5f, .pixelsX = 6, .pixelsY = -10},
        });
        runner.scenario(jittered, {
            {.name = "prepare reset first", .frame = 1, .renderWidth = 64, .renderHeight = 32,
             .jitterX = .5f, .jitterY = -.25f,
             .cancellationX = 0, .cancellationY = 0, .pixelsX = 4, .pixelsY = -8,
             .previousValid = false},
            {.name = "prepare reset", .frame = 2, .renderWidth = 64, .renderHeight = 32,
             .jitterX = -.5f, .jitterY = .25f,
             .cancellationX = 0, .cancellationY = 0, .pixelsX = 4, .pixelsY = -8,
             .prepareReset = true, .previousValid = false},
            {.name = "pair after prepare reset", .frame = 3, .renderWidth = 64, .renderHeight = 32,
             .jitterX = .5f, .jitterY = -.25f,
             .cancellationX = -1, .cancellationY = .5f, .pixelsX = 6, .pixelsY = -10},
        });
        runner.scenario(jittered, {
            {.name = "dispatch reset first", .frame = 1, .renderWidth = 64, .renderHeight = 32,
             .jitterX = .5f, .jitterY = -.25f,
             .cancellationX = 0, .cancellationY = 0, .pixelsX = 4, .pixelsY = -8,
             .previousValid = false},
            {.name = "dispatch reset", .frame = 2, .renderWidth = 64, .renderHeight = 32,
             .jitterX = -.5f, .jitterY = .25f,
             .cancellationX = 0, .cancellationY = 0, .pixelsX = 4, .pixelsY = -8,
             .dispatchReset = true},
            {.name = "pair after dispatch reset", .frame = 3, .renderWidth = 64, .renderHeight = 32,
             .jitterX = .5f, .jitterY = -.25f,
             .cancellationX = -1, .cancellationY = .5f, .pixelsX = 6, .pixelsY = -10},
        });
        // Capture all three snapshots before any encode. Later Prepare calls must
        // not replace the earlier snapshot's previous jitter or render domain.
        runner.scenario(jittered, {
            {.name = "queued first", .frame = 1, .renderWidth = 64, .renderHeight = 32,
             .jitterX = .5f, .jitterY = -.25f,
             .cancellationX = 0, .cancellationY = 0, .pixelsX = 4, .pixelsY = -8,
             .previousValid = false},
            {.name = "queued second resized", .frame = 2, .renderWidth = 32, .renderHeight = 64,
             .jitterX = .5f, .jitterY = -.25f,
             .cancellationX = -.25f, .cancellationY = -.25f, .pixelsX = 9, .pixelsY = -3.5f},
            {.name = "queued third resized", .frame = 3, .renderWidth = 64, .renderHeight = 32,
             .jitterX = -.5f, .jitterY = .25f,
             .cancellationX = 1.5f, .cancellationY = -.375f, .pixelsX = 1, .pixelsY = -6.5f},
        }, true);
        runner.scenario(jittered, {
            {.name = "output domain first", .frame = 1, .renderWidth = 64, .renderHeight = 32,
             .jitterX = .5f, .jitterY = -.25f,
             .cancellationX = 0, .cancellationY = 0, .pixelsX = 4, .pixelsY = -8,
             .previousValid = false},
            {.name = "output domain reset", .frame = 2, .renderWidth = 64, .renderHeight = 32,
             .jitterX = -.5f, .jitterY = .25f,
             .cancellationX = 0, .cancellationY = 0, .pixelsX = 4.25f, .pixelsY = -8,
             .outputWidth = 136},
            {.name = "pair after output reset", .frame = 3, .renderWidth = 64, .renderHeight = 32,
             .jitterX = .5f, .jitterY = -.25f,
             .cancellationX = -1, .cancellationY = .5f, .pixelsX = 6.375f, .pixelsY = -10,
             .outputWidth = 136},
        }, false, 136);
        runner.uiComposition(false);
        runner.uiComposition(true);
        runner.uiComposition(false, true);
        runner.alphaMask();
        runner.outputOrigin();
        runner.cacheHistory();
        runner.spatialJitter(false);
        runner.spatialJitter(true);
        std::fprintf(stderr, "FG native jitter: %u frames, %u failed\n", runner.frames, runner.failures);
        assert(runner.failures == 0 && "production jitter cancellation / GPU motion regression");
    }
}
'''


NATIVE_CAMERA = NATIVE_JITTER[:NATIVE_JITTER.index("\nint main() {")] + r'''
#include <cstdlib>
#include <dlfcn.h>
#include <mach-o/loader.h>
#include <objc/runtime.h>

extern "C" void fg_camera_packet(yaagl_fsr_fg_prepare_packet*, unsigned, unsigned, unsigned);
static IMP nativeCameraEncode = nullptr;
static id encodedCamera = nil;
static bool encodedCameraReset = false;
static id encodedPreviousColor = nil;
static unsigned cameraEncodeCount = 0;
static void observeCameraEncode(id effect, SEL selector, id<MTL4CommandBuffer> command) {
    encodedCamera = effect;
    encodedCameraReset = [(id<MTLFXFrameInterpolatorBase>)effect shouldResetHistory];
    encodedPreviousColor = [(id<MTLFXFrameInterpolatorBase>)effect prevColorTexture];
    ++cameraEncodeCount;
    reinterpret_cast<void (*)(id, SEL, id<MTL4CommandBuffer>)>(nativeCameraEncode)(
        effect, selector, command);
}
// Debugger checkpoints bracket the real PreparedFrame::encode, MetalFX encode,
// submission and GPU completion. No shader parameters are supplied by this fixture.
extern "C" __attribute__((noinline)) void fg_camera_before_encode(id effect) {
    if (!nativeCameraEncode) {
        const auto method = class_getInstanceMethod(object_getClass(effect),
                                                    @selector(encodeToCommandBuffer:));
        assert(method);
        nativeCameraEncode = method_setImplementation(method, (IMP)observeCameraEncode);
    }
    cameraEncodeCount = 0;
    asm volatile("" : : "r"(effect) : "memory");
}
extern "C" __attribute__((noinline)) void fg_camera_after_completion(id effect) {
    asm volatile("" : : "r"(effect) : "memory");
}

// Optional retained-binary instrumentation, not a portable MetalFX contract.
// The UUID gate MUST pass before reading the reviewed _impl + 0x210 mask slot.
// Portable matrix, raw-depth, and final-pixel assertions below do not need it.
static std::vector<float> nativeCameraMask(id effect, id<MTLDevice> device) {
    if (!std::getenv("FG_CAMERA_NATIVE_MASK")) return {};
    const auto method = class_getInstanceMethod(object_getClass(effect),
                                                @selector(encodeToCommandBuffer:));
    Dl_info image{};
    assert(method && nativeCameraEncode && dladdr((void*)nativeCameraEncode, &image));
    const auto* header = static_cast<const mach_header_64*>(image.dli_fbase);
    assert(header && header->magic == MH_MAGIC_64 && header->cputype == CPU_TYPE_X86_64);
    const unsigned char expected[] = {
        0x20,0xd7,0xf7,0x3a,0x90,0x1c,0x39,0x00,
        0x92,0xfb,0xfa,0xf2,0x05,0xba,0xe1,0xcd
    };
    bool matched = false;
    auto* command = reinterpret_cast<const load_command*>(header + 1);
    for (unsigned i = 0; i < header->ncmds; ++i) {
        assert(command->cmdsize >= sizeof(load_command));
        if (command->cmd == LC_UUID)
            matched = !std::memcmp(reinterpret_cast<const uuid_command*>(command)->uuid,
                                   expected, sizeof(expected));
        command = reinterpret_cast<const load_command*>(
            reinterpret_cast<const char*>(command) + command->cmdsize);
    }
    assert(matched && "native mask observation requires reviewed MetalFX UUID");
    auto ivar = class_getInstanceVariable(object_getClass(effect), "_impl");
    assert(ivar);
    unsigned char* implementation = nullptr;
    std::memcpy(&implementation, reinterpret_cast<const char*>(effect) +
                ivar_getOffset(ivar), sizeof(implementation));
    assert(implementation && implementation[0x2d1] == 1);
    id<MTLTexture> mask = nil;
    std::memcpy(&mask, implementation + 0x210, sizeof(mask));
    assert(mask && mask.pixelFormat == MTLPixelFormatR16Float);
    const NSUInteger alignment =
        [device minimumTextureBufferAlignmentForPixelFormat:mask.pixelFormat];
    const NSUInteger rowBytes =
        (mask.width * sizeof(__fp16) + alignment - 1) / alignment * alignment;
    auto destination = [device newBufferWithLength:rowBytes * mask.height
                                           options:MTLResourceStorageModeShared];
    auto queue = [device newCommandQueue];
    auto buffer = [queue commandBuffer];
    auto copy = [buffer blitCommandEncoder];
    assert(destination && queue && buffer && copy);
    [copy copyFromTexture:mask sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(0, 0, 0)
        sourceSize:MTLSizeMake(mask.width, mask.height, 1) toBuffer:destination
        destinationOffset:0 destinationBytesPerRow:rowBytes
        destinationBytesPerImage:rowBytes * mask.height];
    [copy endEncoding];
    auto completed = dispatch_semaphore_create(0);
    [buffer addCompletedHandler:^(id<MTLCommandBuffer> finished) {
        assert(finished.status == MTLCommandBufferStatusCompleted && !finished.error);
        dispatch_semaphore_signal(completed);
    }];
    [buffer commit];
    assert(dispatch_semaphore_wait(completed, dispatch_time(DISPATCH_TIME_NOW,60*NSEC_PER_SEC)) == 0);
    assert(buffer.status == MTLCommandBufferStatusCompleted && !buffer.error);
    dispatch_release(completed);
    std::vector<float> result(mask.width * mask.height);
    for (NSUInteger y = 0; y < mask.height; ++y)
        for (NSUInteger x = 0; x < mask.width; ++x) {
            __fp16 value;
            std::memcpy(&value, static_cast<const char*>(destination.contents) +
                        y * rowBytes + x * sizeof(value), sizeof(value));
            result[y * mask.width + x] = float(value);
        }
    [destination release]; [queue release];
    return result;
}

static void cameraSequence(JitterRunner& runner, unsigned producer,
                           bool infinite, unsigned control) {
    // control: moving camera, no camera, reset on third frame, private override.
    constexpr unsigned width = 192, height = 128;
    std::shared_ptr<State> states[2];
    for (unsigned reversed = 0; reversed != 2; ++reversed) {
        states[reversed] = std::make_shared<State>();
        states[reversed]->creation.display_width = width;
        states[reversed]->creation.display_height = height;
        states[reversed]->creation.flags =
            (reversed ? YAAGL_FSR_FG_DEPTH_INVERTED : 0) |
            (infinite ? YAAGL_FSR_FG_DEPTH_INFINITE : 0);
    }
    for (unsigned frame = 1; frame <= 4; ++frame) {
        std::vector<std::array<__fp16, 4>> colors[2];
        std::vector<float> masks[2];
        for (unsigned reversed = 0; reversed != 2; ++reversed) {
            JitterFrame test{
                .name = "camera depth-domain", .frame = frame,
                .renderWidth = width, .renderHeight = height,
                .jitterX = 0, .jitterY = 0, .cancellationX = 0, .cancellationY = 0,
                .pixelsX = 0, .pixelsY = 0, .scaleX = 1, .scaleY = 1,
                .dispatchReset = control == 2 && frame == 3,
                .previousValid = frame != 1, .outputWidth = width
            };
            auto prepared = runner.prepare(states[reversed], test);
            auto& impl = *prepared->impl_;
            auto& p = impl.snapshot->parameters;
            fg_camera_packet(&p, producer, frame, infinite);
            if (control == 1) p.camera_info_present = 0;
            const float n = .1f * 2.5f, f = 100.0f * 2.5f;
            const float scaleY = 1.0f / std::tan(.5f);
            const float scaleX = scaleY / 1.5f * (control == 3 ? .8f : 1.0f);
            const float shear = control == 3 ? .07f : 0;
            simd_float4x4 expected{};
            expected.columns[0] = {scaleX, 0, 0, 0};
            expected.columns[1] = {0, scaleY, 0, 0};
            expected.columns[2] = {shear, 0, infinite ? 0 : n / (n - f), 1};
            expected.columns[3] = {0, 0, infinite ? n : n * f / (f - n), 0};
            if (control == 3) {
                p.projection_matrix_present = 1;
                std::memcpy(p.view_to_clip, &expected, sizeof(expected));
            }
            assert(validParameters(p, states[reversed]->creation.flags));
            std::vector<float> depth(width * height);
            std::vector<std::array<__fp16, 2>> motion(width * height);
            std::vector<std::array<__fp16, 4>> scene(width * height);
            const float cameraX = control == 1 ? 0 : .08f * (frame - 1);
            for (unsigned y = 0; y < height; ++y) {
                // A static scene with different distances across rows; camera
                // translation is horizontal, so each row keeps the same depth.
                const float z = 1.0f + 7.0f * float(y) / float(height - 1);
                const float reversedDepth = infinite ? .1f / z
                    : .1f / (.1f - 100.0f) + .1f * 100.0f / (100.0f - .1f) / z;
                const float cameraPixels = control == 1 ? 0 : .08f * scaleX * .5f * width / z;
                const bool movingObject = y >= 48 && y < 80;
                const float objectPixels = movingObject ? 2.0f : 0;
                for (unsigned x = 0; x < width; ++x) {
                    const auto index = y * width + x;
                    // Use the same representable working depth in both cases;
                    // normal-depth storage loses precision near one.
                    const float normalDepth = 1.0f - reversedDepth;
                    depth[index] = reversed ? 1.0f - normalDepth : normalDepth;
                    motion[index] = {__fp16(frame == 1 ? 0 : cameraPixels - objectPixels), 0};
                    const float worldX = cameraX +
                        ((2 * (x + .5f - objectPixels * (frame - 1)) / width - 1) -
                         shear) * z / scaleX;
                    // Keep the textured object inside the view as the camera
                    // moves. Native depth-halo reads at the outer texture edge
                    // have a separate normal/reversed-depth boundary behavior.
                    const float aperture = std::max(0.0f,
                        1.0f - std::fabs(worldX / z) * scaleX * 2);
                    scene[index] = {__fp16(aperture * (.45f + .3f * std::sin(worldX * 6))),
                        __fp16(aperture * (.2f + .6f * float(y) / height)),
                        __fp16(aperture * (movingObject ? .8f : .15f)), __fp16(1)};
                }
            }
            impl.snapshot->depth = pixelTexture(runner.device, MTLPixelFormatR32Float,
                                                 width, height, depth);
            impl.snapshot->motion = pixelTexture(runner.device, MTLPixelFormatRG16Float,
                                                  width, height, motion);
            impl.first = pixelTexture(runner.device, MTLPixelFormatRGBA16Float,
                                       width, height, scene);
            Command command;
            command.value.kind = transport::CommandListKind::mpl;
            command.value.device = runner.device; command.value.compiler = runner.compiler;
            // Match production Dispatch's camera-present configuration key.
            impl.snapshot->configuration = configure(*states[reversed], command, p,
                (id<MTLTexture>)impl.snapshot->depth.get(), MTLPixelFormatRGBA16Float,
                MTLPixelFormatInvalid, width, height,
                FFX_API_BACKBUFFER_TRANSFER_FUNCTION_SRGB, false);
            assert(impl.snapshot->configuration);
            std::fprintf(stderr, "CAMERA producer=%u infinite=%u control=%u reversed=%u frame=%u\n",
                         producer, infinite, control, reversed, frame);
            runner.encode(prepared, test, reversed ? &colors[0] : nullptr,
                          &motion, &depth, &colors[reversed]);
            auto effect = (id<MTLFXFrameInterpolatorBase>)impl.snapshot->configuration->factory.get();
            assert(effect.depthReversed == bool(reversed));
            // MetalFX consumes and clears shouldResetHistory during encode.
            // Assert the value at its actual entry, not the post-encode property.
            assert(cameraEncodeCount == 1 && encodedCamera == effect);
            assert(encodedCameraReset == (frame == 1 || test.dispatchReset));
            const auto projection = effect.viewToClipMatrix;
            const auto view = effect.worldToViewMatrix;
            simd_float4x4 expectedView = matrix_identity_float4x4;
            if (control == 1) expected = matrix_identity_float4x4;
            else {
                expectedView.columns[0] = {0, 0, 1, 0};
                expectedView.columns[1] = {0, 1, 0, 0};
                expectedView.columns[2] = {-1, 0, 0, 0};
                expectedView.columns[3] = {-cameraX * 2.5f, 0, 0, 1};
            }
            for (unsigned c = 0; c != 4; ++c) {
                for (unsigned r = 0; r != 4; ++r) {
                    assert(near(test.name, "emitted projection", projection.columns[c][r],
                                expected.columns[c][r]));
                    assert(near(test.name, "emitted view", view.columns[c][r],
                                expectedView.columns[c][r]));
                }
                std::fprintf(stderr, "CAMERA_MATRIX column=%u view=%g,%g,%g,%g projection=%g,%g,%g,%g\n",
                    c, view.columns[c][0], view.columns[c][1], view.columns[c][2], view.columns[c][3],
                    projection.columns[c][0], projection.columns[c][1],
                    projection.columns[c][2], projection.columns[c][3]);
            }
            // Reset/no-camera/first-frame masks are not promised to be written.
            if (control != 1 && frame > 1 && !test.dispatchReset)
                masks[reversed] = nativeCameraMask(effect, runner.device);
        }
        if (!masks[0].empty()) {
            assert(masks[0].size() == width * height && masks[1].size() == masks[0].size());
            float staticMax = 0, moverMax = 0, domainDelta = 0;
            for (unsigned y = 0; y < height; ++y)
                for (unsigned x = 0; x < width; ++x) {
                    const auto i = y * width + x;
                    assert(std::isfinite(masks[0][i]) && std::isfinite(masks[1][i]));
                    domainDelta = std::max(domainDelta, std::fabs(masks[0][i] - masks[1][i]));
                    if (y >= 50 && y < 78) moverMax = std::max(moverMax, masks[0][i]);
                    else if (y < 46 || y >= 82) staticMax = std::max(staticMax, masks[0][i]);
                }
            std::fprintf(stderr, "CAMERA_MASK frame=%u staticMax=%g moverMax=%g domainDelta=%g\n",
                         frame, staticMax, moverMax, domainDelta);
            assert(domainDelta < .002f && staticMax < .01f && moverMax > .1f);
        }
        double delta = 0, checksum = 0;
        for (std::size_t i = 0; i < colors[0].size(); ++i)
            for (unsigned c = 0; c < 4; ++c) {
                assert(std::isfinite(float(colors[0][i][c])) &&
                       std::isfinite(float(colors[1][i][c])));
                delta += std::fabs(float(colors[0][i][c]) - float(colors[1][i][c]));
                checksum += float(colors[0][i][c]);
            }
        std::fprintf(stderr, "CAMERA_PIXELS frame=%u meanDomainDelta=%g checksum=%g\n",
                     frame, delta / (width * height * 4), checksum);
    }
    for (auto& state : states) releaseHudRow(state->hudCounted);
}

int main() {
    @autoreleasepool {
        JitterRunner runner;
        for (unsigned producer = 0; producer != 2; ++producer)
            for (unsigned infinite = 0; infinite != 2; ++infinite)
                cameraSequence(runner, producer, infinite, 0);
        for (unsigned control = 1; control != 4; ++control)
            cameraSequence(runner, 0, false, control);
        std::fprintf(stderr, "FG camera depth-domain: %u frames, %u failed\n",
                     runner.frames, runner.failures);
        assert(runner.failures == 0);
    }
}
'''


NATIVE_HISTORY = r'''
// Substitute only Wine/D3DMetal transport. Compile the unchanged native api(),
// registry, snapshot capture, configuration selection and encoder in this TU.
#define FG_CAMERA_FIXTURE 1
#define unwrapCommandList historyUnwrap
#define releaseCommandList historyReleaseCommand
#define mapResource historyMap
#define releaseResource historyReleaseResource
#define record historyRecord
''' + NATIVE_CAMERA[:NATIVE_CAMERA.index("// Optional retained-binary instrumentation")] + r'''
#undef unwrapCommandList
#undef releaseCommandList
#undef mapResource
#undef releaseResource
#undef record

extern "C" void fg_history_begin(void);
extern "C" int fg_history_prepare(unsigned, unsigned, float, bool, void*, void*, void*);
extern "C" int fg_history_generate(unsigned, unsigned, bool, void*, void*, void*);

static JitterRunner* historyRunner;
static bool historyCompilerAvailable = true;
static std::shared_ptr<const PreparedFrame> recordedHistory;
struct HistoryResource {
    void** vtable;
    id texture = nil;
};
static std::array<void*, 44> historyVtable{};
static HistoryResource historyDevice{historyVtable.data()};
static std::uint32_t __attribute__((ms_abi)) historyRelease(void*) { return 1; }
static std::int32_t __attribute__((ms_abi)) historyGetDevice(void*, const Guid*, void** out) {
    *out = &historyDevice;
    return 0;
}
static Luid* __attribute__((ms_abi)) historyLuid(void*, Luid* out) {
    *out = {};
    return out;
}
static void __attribute__((ms_abi)) historyBarrier(void*, std::uint32_t, const Barrier*) {}

namespace yaagl::pso::d3dmetal {
bool historyUnwrap(void*, NativeCommandList& out) noexcept {
    out.kind = CommandListKind::mpl;
    out.device = historyRunner->device;
    out.compiler = historyCompilerAvailable ? historyRunner->compiler : nil;
    return true;
}
void historyReleaseCommand(NativeCommandList& out) noexcept { out = {}; }
ResourceMapResult historyMap(void* resource, MetalResource& out,
                            ResourceAccess, ResourceViewPolicy) noexcept {
    out.texture = [static_cast<HistoryResource*>(resource)->texture retain];
    out.view.mipCount = out.view.sliceCount = out.view.planes = 1;
    return ResourceMapResult::mapped;
}
void historyReleaseResource(MetalResource& out) noexcept {
    [(id)out.texture release];
    out = {};
}
bool historyRecord(NativeCommandList&, const RecordRequest& request) noexcept {
    recordedHistory = std::get<std::shared_ptr<const
        fsr::framegeneration::PreparedFrame>>(request.prepared.value);
    return bool(recordedHistory);
}
}

extern "C" int fg_native_packet(void* packet) {
    auto* header = static_cast<yaagl_fsr_fg_packet_header*>(packet);
    return api(header->operation, packet);
}

// Prepare's real GPU copy must finish before generation consumes its private
// textures. Use the same exact completion signal as JitterRunner, never a sleep.
static void completeHistoryPrepare(JitterRunner& runner) {
    assert(recordedHistory);
    auto allocator = [runner.device newCommandAllocator];
    auto buffer = [runner.device newCommandBuffer];
    auto fence = [runner.device newFence];
    [buffer beginCommandBufferWithAllocator:allocator];
    auto initialization = [buffer computeCommandEncoder];
    [initialization updateFence:fence afterEncoderStages:MTLStageDispatch];
    [initialization endEncoding];
    std::shared_ptr<const ExecutionLease> lease;
    assert(recordedHistory->encode(buffer, fence, lease) && lease);
    [buffer endCommandBuffer];
    auto completed = dispatch_semaphore_create(0);
    auto options = [MTL4CommitOptions new];
    [options addFeedbackHandler:^(id<MTL4CommitFeedback> feedback) {
        assert(!feedback.error);
        dispatch_semaphore_signal(completed);
    }];
    [runner.queue commit:&buffer count:1 options:options];
    assert(dispatch_semaphore_wait(completed,
        dispatch_time(DISPATCH_TIME_NOW, 30 * NSEC_PER_SEC)) == 0);
    lease.reset();
    recordedHistory.reset();
    [options release]; dispatch_release(completed);
    [fence release]; [buffer release]; [allocator release];
}

static void producerHistory(JitterRunner& runner, unsigned route,
                            bool ahead, unsigned resetCase) {
    // resetCase: none, explicit Dispatch101, explicit Prepare101, future
    // Prepare102 reset + render configuration change (must not leak into 101).
    fg_history_begin();
    auto state = std::make_shared<State>();
    state->creation.flags = YAAGL_FSR_FG_MOTION_VECTORS_JITTERED;
    state->creation.display_width = state->creation.display_height = 128;
    state->creation.max_render_width = state->creation.max_render_height = 128;
    contexts.emplace(17, state);
    auto depth = uniformTexture(runner.device, MTLPixelFormatR32Float, 64, 32, .5f);
    auto motion = uniformTexture(runner.device, MTLPixelFormatRG16Float, 64, 32,
        std::array<std::uint16_t, 2>{0x3400, 0xb800});
    auto color = uniformTexture(runner.device, MTLPixelFormatRGBA16Float, 128, 128,
        std::array<std::uint16_t, 4>{0x3400, 0x3800, 0x3a00, 0x3c00});
    auto output = privateTexture(runner.device, MTLPixelFormatRGBA16Float, 128, 128,
        MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite);
    HistoryResource list{historyVtable.data()};
    HistoryResource resources[] = {
        {historyVtable.data(), depth.get()}, {historyVtable.data(), motion.get()},
        {historyVtable.data(), color.get()}, {historyVtable.data(), output.get()}
    };
    auto prepare = [&](unsigned frame) {
        const bool future = frame == 102 && resetCase == 3;
        if (!route && !ahead && !resetCase && frame != 102) {
            const auto pendingCount = state->frames.size();
            const auto configurationCount = state->configurations.size();
            historyCompilerAvailable = false;
            assert(fg_history_prepare(frame, 64, frame == 101 ? -.5f : .5f, false,
                &list, &resources[0], &resources[1]) == Unsupported);
            assert(!recordedHistory && state->frames.size() == pendingCount &&
                   state->configurations.size() == configurationCount);
            historyCompilerAvailable = true;
        }
        assert(fg_history_prepare(frame, future ? 32 : 64,
            frame == 101 ? -.5f : .5f,
            future || (frame == 101 && resetCase == 2),
            &list, &resources[0], &resources[1]) == Ok);
        completeHistoryPrepare(runner);
        assert(state->frames.at(frame)->production->produced);
    };
    auto generate = [&](unsigned frame) {
        const auto pending = state->frames.at(frame);
        const auto history = state->history;
        if (!route && !ahead && !resetCase && frame != 102) {
            const auto configurationCount = state->configurations.size();
            historyCompilerAvailable = false;
            assert(fg_history_generate(frame, route, false,
                &list, &resources[2], &resources[3]) == Unsupported);
            assert(!recordedHistory && state->frames.at(frame) == pending &&
                   state->history.get() == history.get() &&
                   state->configurations.size() == configurationCount);
            historyCompilerAvailable = true;
        }
        if (frame == 101) {
            assert(state->historyFrame == 100 && history);
            assert(pending->parameters.frame_id == 101);
            assert(pending->parameters.render_width == 64);
            assert(pending->hasPreviousJitter == (resetCase != 2));
            assert(pending->previousJitterUvX == .5f / 64);
            assert(pending->parameters.jitter_x == -.5f);
        }
        assert(fg_history_generate(frame, route, frame == 101 && resetCase == 1,
            &list, &resources[2], &resources[3]) == Ok);
        assert(recordedHistory && !state->frames.count(frame));
        auto work = std::const_pointer_cast<PreparedFrame>(recordedHistory);
        assert(work->impl_->snapshot->production == pending->production);
        assert(work->impl_->snapshot->parameters.frame_id == frame);
        const bool reset = frame == 100 || (frame == 101 &&
            (resetCase == 1 || resetCase == 2)) ||
            (frame == 102 && resetCase == 3);
        const float cancellation = reset ? 0 : frame == 101 ? 1 : -1;
        const unsigned width = frame == 102 && resetCase == 3 ? 32 : 64;
        JitterFrame test{
            .name = ahead ? "producer prepare-ahead" : "producer in-order",
            .frame = frame, .renderWidth = width, .renderHeight = 32,
            .jitterX = frame == 101 ? -.5f : .5f, .jitterY = -.25f,
            .cancellationX = cancellation, .cancellationY = 0,
            .pixelsX = (2 - cancellation) * 128 / width, .pixelsY = -8
        };
        runner.encode(work, test);
        assert(cameraEncodeCount == 1);
        assert(encodedCameraReset == reset &&
            "older pending producer frame must retain native temporal history");
        if (!reset)
            assert(encodedPreviousColor == history.get());
        assert(state->historyFrame == frame);
        recordedHistory.reset();
    };
    prepare(100); generate(100);
    prepare(101);
    if (ahead) prepare(102);
    generate(101);
    if (!ahead) prepare(102);
    generate(102);
    // Real registry rejection, not the packet-capture fixture accepting a
    // fabricated unprepared Generate and pinning its forced-reset bit.
    assert(fg_history_generate(102, route, false, &list,
        &resources[2], &resources[3]) == Parameter);
    assert(fg_history_generate(103, route, false, &list,
        &resources[2], &resources[3]) == Parameter);
    assert(!recordedHistory && state->frames.empty());
    yaagl_fsr_fg_destroy_packet destroy{};
    destroy.header.size = sizeof(destroy);
    destroy.header.version = YAAGL_FSR_FG_BRIDGE_VERSION;
    destroy.header.operation = YAAGL_FSR_FG_DESTROY;
    destroy.header.context = 17;
    assert(api(YAAGL_FSR_FG_DESTROY, &destroy) == Ok);
    assert(!lookup(17));
}

int main() {
    @autoreleasepool {
        JitterRunner runner;
        historyRunner = &runner;
        // Runtime discovery is substituted together with the transport boundary.
        ready.store(true, std::memory_order_release);
        historyVtable[2] = reinterpret_cast<void*>(&historyRelease);
        historyVtable[7] = reinterpret_cast<void*>(&historyGetDevice);
        historyVtable[43] = reinterpret_cast<void*>(&historyLuid);
        historyVtable[26] = reinterpret_cast<void*>(&historyBarrier);
        for (unsigned route = 0; route != 3; ++route) {
            producerHistory(runner, route, false, 0);
            producerHistory(runner, route, true, 0);
            for (unsigned reset = 1; reset != 4; ++reset)
                producerHistory(runner, route, true, reset);
        }
        std::fprintf(stderr, "FG producer/native history: %u frames, %u failed\n",
                     runner.frames, runner.failures);
        assert(runner.failures == 0);
    }
}
'''


class FrameGenerationNormalizationTest(unittest.TestCase):
    def test_producer_native_history(self):
        native = ROOT / "d3dmetal-sidecar"
        kernels = (native / "fsr-kernels.metal").read_text()
        with tempfile.TemporaryDirectory(prefix="yaagl-fg-history-") as directory:
            directory = pathlib.Path(directory)
            producer = directory / "fg-producer.c"
            producer_object = directory / "fg-producer.o"
            source = directory / "fg-history.mm"
            binary = directory / "fg-history"
            producer.write_text(dispatch_source())
            source.write_text(NATIVE_HISTORY)
            (directory / "fsr-kernels.inc").write_text(
                'static const char kFsrKernelsSource[] = R"YAAGL_METAL(' + kernels + ')YAAGL_METAL";\n')
            subprocess.run(("xcrun", "clang", "-arch", "x86_64", "-std=c11",
                            "-Wno-deprecated-declarations", "-Wall", "-Wextra", "-Werror",
                            "-DFG_HISTORY_FIXTURE=1", "-Dmain=fg_dispatch_main",
                            "-I", str(SDK), "-I", str(ROOT / "include"),
                            "-c", str(producer), "-o", str(producer_object)), check=True)
            subprocess.run(("xcrun", "clang++", "-arch", "x86_64", "-std=c++20", "-fno-objc-arc",
                            "-I", str(native), "-I", str(directory), str(source),
                            str(producer_object),
                            *(str(native / file) for file in ("metalfx-backend.mm",
                              "d3dmetal-transport.mm")),
                            "-framework", "Foundation", "-framework", "Metal",
                            "-framework", "MetalFX", "-o", str(binary)), check=True)
            subprocess.run((str(binary),), check=True)

    def test_native_camera_depth_domain(self):
        native = ROOT / "d3dmetal-sidecar"
        kernels = (native / "fsr-kernels.metal").read_text()
        with tempfile.TemporaryDirectory(prefix="yaagl-fg-camera-") as directory:
            directory = pathlib.Path(directory)
            producer = directory / "fg-producer.c"
            producer_object = directory / "fg-producer.o"
            source = directory / "fg-camera.mm"
            binary = directory / "fg-camera"
            producer.write_text(dispatch_source())
            source.write_text("#define FG_CAMERA_FIXTURE 1\n" + NATIVE_CAMERA)
            (directory / "fsr-kernels.inc").write_text(
                'static const char kFsrKernelsSource[] = R"YAAGL_METAL(' + kernels + ')YAAGL_METAL";\n')
            subprocess.run(("xcrun", "clang", "-arch", "x86_64", "-std=c11",
                            "-Wno-deprecated-declarations", "-Wall", "-Wextra", "-Werror",
                            "-Dmain=fg_dispatch_main",
                            "-I", str(SDK), "-I", str(ROOT / "include"),
                            "-c", str(producer), "-o", str(producer_object)), check=True)
            subprocess.run(("xcrun", "clang++", "-arch", "x86_64", "-std=c++20", "-fno-objc-arc",
                            "-g", "-I", str(native), "-I", str(directory), str(source),
                            str(producer_object),
                            *(str(native / file) for file in ("metalfx-backend.mm",
                              "d3dmetal-transport.mm")),
                            "-framework", "Foundation", "-framework", "Metal",
                            "-framework", "MetalFX", "-o", str(binary)), check=True)
            subprocess.run((str(binary),), check=True)

    def test_native_barrier_failure_path(self):
        native = ROOT / "d3dmetal-sidecar"
        kernels = (native / "fsr-kernels.metal").read_text()
        with tempfile.TemporaryDirectory(prefix="yaagl-fg-barriers-") as directory:
            directory = pathlib.Path(directory)
            producer = directory / "fg-producer.c"
            producer_object = directory / "fg-producer.o"
            source = directory / "fg-barriers.mm"
            binary = directory / "fg-barriers"
            producer.write_text(dispatch_source())
            source.write_text(d3d12_states() + "\n" + NATIVE_BARRIERS)
            (directory / "fsr-kernels.inc").write_text(
                'static const char kFsrKernelsSource[] = R"YAAGL_METAL(' + kernels + ')YAAGL_METAL";\n')
            # Match the existing C fixture's compiler gate, including its
            # established exception for intentionally tested SDK Prepare V1.
            subprocess.run(("xcrun", "clang", "-arch", "x86_64", "-std=c11",
                            "-Wno-deprecated-declarations", "-Wall", "-Wextra", "-Werror",
                            "-DFG_RECORD_OBSERVER", "-I", str(SDK), "-I", str(ROOT / "include"),
                            "-c", str(producer), "-o", str(producer_object)), check=True)
            subprocess.run(("xcrun", "clang++", "-arch", "x86_64", "-std=c++20", "-fno-objc-arc",
                            "-I", str(native), "-I", str(directory), str(source),
                            str(producer_object),
                            *(str(native / file) for file in ("metalfx-backend.mm",
                              "d3dmetal-transport.mm")),
                            "-framework", "Foundation", "-framework", "Metal",
                            "-framework", "MetalFX", "-o", str(binary)), check=True)
            subprocess.run((str(binary),), check=True)

    def test_native_jitter_history(self):
        native = ROOT / "d3dmetal-sidecar"
        kernels = (native / "fsr-kernels.metal").read_text()
        self.assertNotIn(')YAAGL_METAL"', kernels)
        with tempfile.TemporaryDirectory(prefix="yaagl-fg-jitter-") as directory:
            directory = pathlib.Path(directory)
            source = directory / "fg-jitter.mm"
            binary = directory / "fg-jitter"
            source.write_text(NATIVE_JITTER)
            (directory / "fsr-kernels.inc").write_text(
                'static const char kFsrKernelsSource[] = R"YAAGL_METAL(' + kernels + ')YAAGL_METAL";\n')
            subprocess.run(("xcrun", "clang++", "-arch", "x86_64", "-std=c++20", "-fno-objc-arc",
                            "-I", str(native), "-I", str(directory), str(source),
                            *(str(native / file) for file in ("metalfx-backend.mm",
                              "d3dmetal-transport.mm")),
                            "-framework", "Foundation", "-framework", "Metal",
                            "-framework", "MetalFX", "-o", str(binary)), check=True)
            subprocess.run((str(binary),), check=True)

    def test_native_destroy_ownership(self):
        native = ROOT / "d3dmetal-sidecar"
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
                              "d3dmetal-transport.mm")),
                            "-framework", "Foundation", "-framework", "Metal",
                            "-framework", "MetalFX", "-o", str(binary)), check=True)
            subprocess.run((str(binary),), check=True)

    def test_production_dispatch(self):
        code = dispatch_source()
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
