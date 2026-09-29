#!/usr/bin/env python3
"""Compile the production SR and FG GPU-memory query bodies against the pinned
SDK and check the D3DMetal DLSS-compatible contract: automatic/MetalFX contexts
and pre-creation V2 queries report zero usage with success, every error clears
the output, and native/swapchain contexts keep the native provider's answer.
"""

import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FFX = ROOT / "d3dmetal-pso-cache/third-party/fidelityfx/Kits/FidelityFX"
SR_SOURCE = (ROOT / "dlls/amd_fidelityfx_upscaler_dx12/main.c").read_text()
FG_SOURCE = (ROOT / "dlls/amd_fidelityfx_framegeneration_dx12/main.c").read_text()


def section(source, start, end):
    begin = source.index(start)
    return source[begin:source.index(end, begin)]


COMMON = r'''
#include <assert.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <wchar.h>
#define __declspec(attribute)
#include "../../api/include/ffx_api_types.h"
#define WINAPI
#define TRUE 1
#define FALSE 0
typedef int BOOL;
typedef long LONG;
typedef int SRWLOCK;
typedef int CRITICAL_SECTION;
typedef wchar_t WCHAR;
#define SRWLOCK_INIT 0
static int locks_held;
static void AcquireSRWLockShared(SRWLOCK *lock) { (void)lock; ++locks_held; }
static void ReleaseSRWLockShared(SRWLOCK *lock) { (void)lock; --locks_held; }
static void EnterCriticalSection(CRITICAL_SECTION *lock) { (void)lock; ++locks_held; }
static void LeaveCriticalSection(CRITICAL_SECTION *lock) { (void)lock; --locks_held; }
static LONG InterlockedIncrement(LONG *value) { return ++*value; }
static LONG InterlockedDecrement(LONG *value) { return --*value; }
static LONG InterlockedCompareExchange(LONG *value, LONG exchange, LONG compare)
{ LONG old = *value; if (old == compare) *value = exchange; return old; }
static void expect_zero(const struct FfxApiEffectMemoryUsage *usage)
{ assert(!usage->totalUsageInBytes && !usage->aliasableUsageInBytes); }
static void poison(struct FfxApiEffectMemoryUsage *usage)
{ usage->totalUsageInBytes = usage->aliasableUsageInBytes = 0xdeadbeef; }
'''

SR_PROLOGUE = COMMON + r'''
#include "ffx_upscale.h"
#define PROVIDER_ID 0x4d46580000000001ull
static const char provider_name[] = "MetalFX (FSR 4 API)";
'''

SR_EPILOGUE = r'''
static void context_free(const ffxAllocationCallbacks *callbacks, void *memory)
{ (void)callbacks; (void)memory; assert(0 && "live context freed by a query"); }
int main(void)
{
    struct fsr_context live = {0}, stale = {0};
    ffxContext handle = &live, stale_handle = &stale;
    struct FfxApiEffectMemoryUsage usage;
    struct ffxQueryDescUpscaleGetGPUMemoryUsage v1 = {0};
    struct ffxQueryDescUpscaleGetGPUMemoryUsageV2 v2 = {0}, bad;

    live.native = 77; live.references = 1; contexts = &live;
    v1.header.type = FFX_API_QUERY_DESC_TYPE_UPSCALE_GPU_MEMORY_USAGE;
    v1.gpuMemoryUsageUpscaler = &usage;
    v2.header.type = FFX_API_QUERY_DESC_TYPE_UPSCALE_GPU_MEMORY_USAGE_V2;
    v2.device = (void *)1;
    v2.maxRenderSize.width = 1280; v2.maxRenderSize.height = 720;
    v2.maxUpscaleSize.width = 2560; v2.maxUpscaleSize.height = 1440;
    v2.gpuMemoryUsageUpscaler = &usage;

    /* A live context reports unreported (zero) usage with success. */
    poison(&usage);
    assert(query_impl(&handle, &v1.header) == FFX_API_RETURN_OK);
    expect_zero(&usage);
    assert(live.references == 1 && locks_held == 0);

    /* V1 requires a live context. */
    poison(&usage);
    assert(query_impl(NULL, &v1.header) == FFX_API_RETURN_ERROR_PARAMETER);
    expect_zero(&usage);
    poison(&usage);
    assert(query_impl(&stale_handle, &v1.header) == FFX_API_RETURN_ERROR_PARAMETER);
    expect_zero(&usage);
    v1.gpuMemoryUsageUpscaler = NULL;
    assert(query_impl(&handle, &v1.header) == FFX_API_RETURN_ERROR_PARAMETER);

    /* V2 answers before creation, with or without a live handle. */
    poison(&usage);
    assert(query_impl(NULL, &v2.header) == FFX_API_RETURN_OK);
    expect_zero(&usage);
    poison(&usage);
    assert(query_impl(&handle, &v2.header) == FFX_API_RETURN_OK);
    expect_zero(&usage);
    poison(&usage);
    assert(query_impl(&stale_handle, &v2.header) == FFX_API_RETURN_ERROR_PARAMETER);
    expect_zero(&usage);
    bad = v2; bad.maxRenderSize.width = 2561;  /* render wider than upscale */
    poison(&usage);
    assert(query_impl(NULL, &bad.header) == FFX_API_RETURN_ERROR_PARAMETER);
    expect_zero(&usage);
    bad = v2; bad.maxUpscaleSize.height = 0;
    assert(query_impl(NULL, &bad.header) == FFX_API_RETURN_ERROR_PARAMETER);
    bad = v2; bad.device = NULL;
    poison(&usage);
    assert(query_impl(NULL, &bad.header) == FFX_API_RETURN_ERROR_PARAMETER);
    expect_zero(&usage);
    bad = v2; bad.gpuMemoryUsageUpscaler = NULL;
    assert(query_impl(NULL, &bad.header) == FFX_API_RETURN_ERROR_PARAMETER);
    assert(live.references == 1 && locks_held == 0);
    puts("SR memory query: OK");
    return 0;
}
'''

FG_PROLOGUE = COMMON + r'''
#include "ffx_framegeneration.h"
#define AUTOMATIC_PROVIDER_ID UINT64_C(0x5941474647000001)
#define METALFX_PROVIDER_ID   UINT64_C(0x4d46584647000001)
#define MAX_DESCRIPTOR_CHAIN 64u
static const char automatic_provider_name[] = "automatic";
static const char metalfx_provider_name[] = "metalfx";
enum context_mode { CONTEXT_NATIVE, MODE_PENDING, CONTEXT_METALFX, CONTEXT_SWAPCHAIN };
struct fg_context { LONG mode; ffxContext original; uint64_t translated; };
struct native_api { ffxReturnCode_t (*query)(ffxContext *, ffxQueryDescHeader *); };
static struct native_api native;
static struct fg_context ctx;
static ffxContext handle = &ctx, stale_handle = (ffxContext)&native;
static int native_calls, references;
static ffxReturnCode_t native_result;
static uint64_t native_total, native_alias;
static BOOL have_native(void) { return TRUE; }
static struct fg_context *acquire_context(ffxContext *passed)
{
    if (!passed || *passed != &ctx) return NULL;
    ++references;
    return &ctx;
}
static void release_context(struct fg_context *context) { assert(context == &ctx); --references; }
static ffxReturnCode_t native_query(ffxContext *context, ffxQueryDescHeader *desc)
{
    struct FfxApiEffectMemoryUsage *usage =
        desc->type == FFX_API_QUERY_DESC_TYPE_FRAMEGENERATION_GPU_MEMORY_USAGE ?
        ((struct ffxQueryDescFrameGenerationGetGPUMemoryUsage *)desc)->gpuMemoryUsageFrameGeneration :
        ((struct ffxQueryDescFrameGenerationGetGPUMemoryUsageV2 *)desc)->gpuMemoryUsageFrameGeneration;
    ++native_calls;
    assert(context == &ctx.original);
    /* Like a real provider, may write before failing. */
    usage->totalUsageInBytes = native_total;
    usage->aliasableUsageInBytes = native_alias;
    return native_result;
}
'''

FG_EPILOGUE = r'''
static struct FfxApiEffectMemoryUsage usage;
static struct ffxQueryDescFrameGenerationGetGPUMemoryUsage v1;
static struct ffxQueryDescFrameGenerationGetGPUMemoryUsageV2 v2;
static void reset_state(int mode, uint64_t translated)
{
    memset(&ctx, 0, sizeof(ctx));
    ctx.mode = mode;
    ctx.original = (ffxContext)&v2;
    ctx.translated = translated;
    native.query = native_query;
    native_calls = references = 0;
    native_result = FFX_API_RETURN_OK;
    native_total = 4096; native_alias = 1024;
    memset(&v1, 0, sizeof(v1));
    v1.header.type = FFX_API_QUERY_DESC_TYPE_FRAMEGENERATION_GPU_MEMORY_USAGE;
    v1.gpuMemoryUsageFrameGeneration = &usage;
    memset(&v2, 0, sizeof(v2));
    v2.header.type = FFX_API_QUERY_DESC_TYPE_FRAMEGENERATION_GPU_MEMORY_USAGE_V2;
    v2.device = (void *)1;
    v2.maxRenderSize.width = 1280; v2.maxRenderSize.height = 720;
    v2.displaySize.width = 2560; v2.displaySize.height = 1440;
    v2.gpuMemoryUsageFrameGeneration = &usage;
    poison(&usage);
}
static void check_balanced(void) { assert(references == 0 && locks_held == 0); }
int main(void)
{
    static const int unreported_modes[] = { MODE_PENDING, CONTEXT_METALFX };
    ffxApiHeader extension = {123456, NULL};
    struct ffxQueryDescFrameGenerationGetGPUMemoryUsageV2 bad;
    unsigned int i;

    /* Automatic and MetalFX contexts report unreported (zero) usage, never the
     * original native context's totals. */
    for (i = 0; i < sizeof(unreported_modes) / sizeof(unreported_modes[0]); ++i)
    {
        reset_state(unreported_modes[i], 17);
        assert(ffxQuery(&handle, &v1.header) == FFX_API_RETURN_OK);
        expect_zero(&usage);
        poison(&usage);
        assert(ffxQuery(&handle, &v2.header) == FFX_API_RETURN_OK);
        expect_zero(&usage);
        assert(native_calls == 0);
        check_balanced();
        poison(&usage);
        v1.header.pNext = &extension;
        assert(ffxQuery(&handle, &v1.header) == FFX_API_RETURN_ERROR_UNKNOWN_DESCTYPE);
        expect_zero(&usage);
        bad = v2; bad.maxRenderSize.height = 1441;  /* render taller than display */
        poison(&usage);
        assert(ffxQuery(&handle, &bad.header) == FFX_API_RETURN_ERROR_PARAMETER);
        expect_zero(&usage);
        check_balanced();
    }

    /* Native fallback routes by mode even though it still owns a translator. */
    reset_state(CONTEXT_NATIVE, 17);
    assert(ffxQuery(&handle, &v1.header) == FFX_API_RETURN_OK);
    assert(usage.totalUsageInBytes == 4096 && usage.aliasableUsageInBytes == 1024);
    assert(native_calls == 1);
    reset_state(CONTEXT_NATIVE, 0);
    assert(ffxQuery(&handle, &v2.header) == FFX_API_RETURN_OK);
    assert(usage.totalUsageInBytes == 4096 && native_calls == 1);
    reset_state(CONTEXT_SWAPCHAIN, 0);
    assert(ffxQuery(&handle, &v1.header) == FFX_API_RETURN_OK);
    assert(usage.totalUsageInBytes == 4096 && native_calls == 1);
    check_balanced();

    /* A failed native query never leaks what the provider wrote. */
    reset_state(CONTEXT_NATIVE, 0);
    native_result = FFX_API_RETURN_ERROR_RUNTIME_ERROR;
    assert(ffxQuery(&handle, &v1.header) == FFX_API_RETURN_ERROR_RUNTIME_ERROR);
    expect_zero(&usage);
    check_balanced();

    /* Pre-creation V2 validates the basic create contract. */
    reset_state(MODE_PENDING, 17);
    assert(ffxQuery(NULL, &v2.header) == FFX_API_RETURN_OK);
    expect_zero(&usage);
    assert(native_calls == 0);
    bad = v2; bad.device = NULL;
    poison(&usage);
    assert(ffxQuery(NULL, &bad.header) == FFX_API_RETURN_ERROR_PARAMETER);
    expect_zero(&usage);
    bad = v2; bad.displaySize.width = 0;
    poison(&usage);
    assert(ffxQuery(NULL, &bad.header) == FFX_API_RETURN_ERROR_PARAMETER);
    expect_zero(&usage);
    bad = v2; bad.maxRenderSize.width = 0;
    assert(ffxQuery(NULL, &bad.header) == FFX_API_RETURN_ERROR_PARAMETER);
    bad = v2; bad.maxRenderSize.width = 2561;  /* render wider than display */
    assert(ffxQuery(NULL, &bad.header) == FFX_API_RETURN_ERROR_PARAMETER);
    bad = v2; bad.maxRenderSize.width = 2560; bad.maxRenderSize.height = 1440;
    assert(ffxQuery(NULL, &bad.header) == FFX_API_RETURN_OK);

    /* Parameter errors clear outputs first. */
    reset_state(MODE_PENDING, 17);
    assert(ffxQuery(NULL, &v1.header) == FFX_API_RETURN_ERROR_PARAMETER);
    expect_zero(&usage);
    poison(&usage);
    assert(ffxQuery(&stale_handle, &v1.header) == FFX_API_RETURN_ERROR_PARAMETER);
    expect_zero(&usage);
    poison(&usage);
    assert(ffxQuery(&stale_handle, &v2.header) == FFX_API_RETURN_ERROR_PARAMETER);
    expect_zero(&usage);
    poison(&usage);
    v1.header.pNext = &v1.header;
    assert(ffxQuery(&handle, &v1.header) == FFX_API_RETURN_ERROR_PARAMETER);
    expect_zero(&usage);
    v1.header.pNext = NULL;
    v1.gpuMemoryUsageFrameGeneration = NULL;
    assert(ffxQuery(&handle, &v1.header) == FFX_API_RETURN_ERROR_PARAMETER);
    v2.gpuMemoryUsageFrameGeneration = NULL;
    assert(ffxQuery(NULL, &v2.header) == FFX_API_RETURN_ERROR_PARAMETER);
    assert(native_calls == 0);
    check_balanced();
    puts("FG memory query: OK");
    return 0;
}
'''


def compile_and_run(code, includes):
    with tempfile.TemporaryDirectory() as directory:
        source = pathlib.Path(directory) / "query.c"
        binary = pathlib.Path(directory) / "query"
        source.write_text(code)
        command = ["clang", "-std=c11", "-Wno-deprecated-declarations", "-Wall", "-Wextra",
                   "-Werror", "-Wno-unused-function", "-I", str(ROOT / "include")]
        for include in includes:
            command += ["-I", str(include)]
        subprocess.run(command + [str(source), "-o", str(binary)], check=True)
        subprocess.run((str(binary),), check=True)


class MemoryQueryTest(unittest.TestCase):
    def test_upscaler_memory_query(self):
        code = "\n".join((
            SR_PROLOGUE,
            section(SR_SOURCE, "struct fsr_context\n", "static ffxApiMessage global_message;"),
            "static void context_free(const ffxAllocationCallbacks *, void *);",
            section(SR_SOURCE, "static struct fsr_context *find_context(", "static void report("),
            section(SR_SOURCE, "static void release_context(", "static int allocation_compatible("),
            section(SR_SOURCE, "static float upscale_ratio(", "\n\nstatic volatile LONG pe_log_count"),
            section(SR_SOURCE, "static ffxReturnCode_t query_memory(", "static ffxReturnCode_t dispatch_impl("),
            SR_EPILOGUE,
        ))
        compile_and_run(code, (FFX / "upscalers/include",))

    def test_framegeneration_memory_query(self):
        code = "\n".join((
            FG_PROLOGUE,
            section(FG_SOURCE, "static enum context_mode context_mode(", "static SRWLOCK contexts_lock"),
            section(FG_SOURCE, "static BOOL valid_chain(", "static void initialize_packet("),
            section(FG_SOURCE, "static ffxReturnCode_t query_versions(", "ffxReturnCode_t ffxDispatch("),
            FG_EPILOGUE,
        ))
        compile_and_run(code, (FFX / "framegeneration/include",))


if __name__ == "__main__":
    unittest.main()
