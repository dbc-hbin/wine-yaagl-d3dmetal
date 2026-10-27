#!/usr/bin/env python3
"""Exercise production FSR loader, COM ownership and FG configuration lifetimes."""

import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def section(source, start, end):
    begin = source.index(start)
    return source[begin:source.index(end, begin)]


COMMON = r'''
#include <assert.h>
#include <dlfcn.h>
#include <limits.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include "yaagl_fsr_bridge.h"
#include "yaagl_fsr_fg_bridge.h"
static void wait_for(pthread_cond_t *condition, pthread_mutex_t *mutex)
{
    struct timespec deadline;
    assert(!clock_gettime(CLOCK_REALTIME, &deadline));
    deadline.tv_sec += 10;
    assert(!pthread_cond_timedwait(condition, mutex, &deadline));
}
'''

LOADER = COMMON + r'''
typedef int32_t NTSTATUS;
#define STATUS_SUCCESS 0
#define STATUS_INVALID_PARAMETER ((NTSTATUS)0xc000000d)
#define STATUS_DLL_NOT_FOUND ((NTSTATUS)0xc0000135)
static pthread_mutex_t gate = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t changed = PTHREAD_COND_INITIALIZER;
static int module_available, symbol_available, hold_resolution;
static int resolving, allow_resolution, start_workers;
static unsigned workers_ready;
static unsigned opens, symbols, closes, locks, calls;
static int provider_state;
static uint32_t provider(uint32_t operation, void *packet)
{
    (void)packet;
    assert(provider_state == 73);
    assert(operation == 0);
    __atomic_fetch_add(&calls, 1, __ATOMIC_RELAXED);
    return 42;
}
static int fixture_dladdr(const void *address, Dl_info *info)
{
    (void)address;
    info->dli_fname = "/fixture/lib/wine/x86_64-unix/fsr.so";
    return 1;
}
static void *fixture_dlopen(const char *path, int flags)
{
    assert(strstr(path, "/../../external/D3DMetal.framework/Versions/A/Resources/libD3DMetalSidecar.dylib"));
    assert(flags == (RTLD_NOW | RTLD_NOLOAD));
    ++opens;
    return module_available ? &provider_state : NULL;
}
static void *fixture_dlsym(void *handle, const char *name)
{
    assert(handle == &provider_state);
    assert(!strcmp(name, API_SYMBOL));
    ++symbols;
    if (!symbol_available) return NULL;
    assert(!pthread_mutex_lock(&gate));
    if (hold_resolution)
    {
        resolving = 1;
        assert(!pthread_cond_broadcast(&changed));
        while (!allow_resolution) wait_for(&changed, &gate);
    }
    provider_state = 73;
    assert(!pthread_mutex_unlock(&gate));
    return (void *)provider;
}
static int fixture_dlclose(void *handle)
{
    assert(handle == &provider_state);
    ++closes;
    return 0;
}
static int loader_lock(pthread_mutex_t *mutex)
{
    __atomic_fetch_add(&locks, 1, __ATOMIC_RELAXED);
    return pthread_mutex_lock(mutex);
}
#define dladdr fixture_dladdr
#define dlopen fixture_dlopen
#define dlsym fixture_dlsym
#define dlclose fixture_dlclose
#define pthread_mutex_lock loader_lock
'''

LOADER_MAIN = r'''
#undef pthread_mutex_lock
static Packet packet(void)
{
    Packet p = {0};
    p.size = sizeof(p);
#ifdef FG
    p.version = YAAGL_FSR_FG_BRIDGE_VERSION;
#endif
    return p;
}
static void *worker(void *unused)
{
    Packet p = packet();
    (void)unused;
    assert(!pthread_mutex_lock(&gate));
    ++workers_ready;
    assert(!pthread_cond_broadcast(&changed));
    while (!start_workers) wait_for(&changed, &gate);
    assert(!pthread_mutex_unlock(&gate));
    assert(API_CALL(&p) == STATUS_SUCCESS && p.result == 42);
    return NULL;
}
static void wave(int cold)
{
    pthread_t threads[8];
    workers_ready = start_workers = 0;
    for (unsigned i = 0; i < sizeof(threads) / sizeof(*threads); ++i)
        assert(!pthread_create(&threads[i], NULL, worker, NULL));
    assert(!pthread_mutex_lock(&gate));
    while (workers_ready != sizeof(threads) / sizeof(*threads)) wait_for(&changed, &gate);
    start_workers = 1;
    assert(!pthread_cond_broadcast(&changed));
    if (cold)
    {
        while (!resolving) wait_for(&changed, &gate);
        allow_resolution = 1;
        assert(!pthread_cond_broadcast(&changed));
    }
    assert(!pthread_mutex_unlock(&gate));
    for (unsigned i = 0; i < sizeof(threads) / sizeof(*threads); ++i)
        assert(!pthread_join(threads[i], NULL));
}
int main(void)
{
    Packet p = packet(), invalid = p;
    invalid.size = 0;
    assert(API_CALL(NULL) == STATUS_INVALID_PARAMETER);
    assert(API_CALL(&invalid) == STATUS_INVALID_PARAMETER);
    assert(!opens && !locks);
    assert(API_CALL(&p) == STATUS_DLL_NOT_FOUND && p.result == 3);
    assert(opens == 1 && symbols == 0 && closes == 0);
    module_available = 1;
    assert(API_CALL(&p) == STATUS_DLL_NOT_FOUND && p.result == 3);
    assert(opens == 2 && symbols == 1 && closes == 1);
    symbol_available = hold_resolution = 1;
    wave(1);
    assert(opens == 3 && symbols == 2 && closes == 1 && calls == 8);
    unsigned cold_locks = locks;
    wave(0);
    assert(opens == 3 && symbols == 2 && closes == 1 && calls == 16);
    assert(locks == cold_locks);
    puts("FSR loader: retry, concurrent publication and warm fast path passed");
}
'''

WAKE = COMMON + r'''
typedef pthread_mutex_t SRWLOCK;
typedef pthread_cond_t CONDITION_VARIABLE;
struct fg_context { long references, closing; };
static SRWLOCK contexts_lock = PTHREAD_MUTEX_INITIALIZER;
static CONDITION_VARIABLE contexts_changed = PTHREAD_COND_INITIALIZER;
static pthread_cond_t waiter_ready = PTHREAD_COND_INITIALIZER;
static unsigned wakes;
static int waiting;
static void AcquireSRWLockExclusive(SRWLOCK *lock) { assert(!pthread_mutex_lock(lock)); }
static void ReleaseSRWLockExclusive(SRWLOCK *lock) { assert(!pthread_mutex_unlock(lock)); }
static void WakeAllConditionVariable(CONDITION_VARIABLE *condition)
{
    ++wakes;
    assert(!pthread_cond_broadcast(condition));
}
'''

WAKE_MAIN = r'''
static void *destroy_waiter(void *argument)
{
    struct fg_context *context = argument;
    AcquireSRWLockExclusive(&contexts_lock);
    waiting = 1;
    assert(!pthread_cond_signal(&waiter_ready));
    while (context->references != 1) wait_for(&contexts_changed, &contexts_lock);
    ReleaseSRWLockExclusive(&contexts_lock);
    return NULL;
}
int main(void)
{
    struct fg_context live = {2, 0}, closing = {3, 1};
    pthread_t thread;
    release_context(&live);
    assert(live.references == 1 && wakes == 0);
    assert(!pthread_create(&thread, NULL, destroy_waiter, &closing));
    AcquireSRWLockExclusive(&contexts_lock);
    while (!waiting) wait_for(&waiter_ready, &contexts_lock);
    ReleaseSRWLockExclusive(&contexts_lock);
    release_context(&closing);
    assert(closing.references == 2 && wakes == 0);
    release_context(&closing);
    assert(!pthread_join(thread, NULL));
    assert(closing.references == 1 && wakes == 1);
    puts("FSR destroy: live/nonfinal release stays quiet; final release wakes waiter");
}
'''


COM = r'''
#include <cassert>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <new>
#include <utility>
static unsigned allocations;
void* operator new(std::size_t size)
{
    ++allocations;
    if (void* value = std::malloc(size)) return value;
    throw std::bad_alloc();
}
void operator delete(void* value) noexcept { std::free(value); }
'''

COM_MAIN = r'''
struct Object {
    void** table;
    unsigned references = 1, queries = 0, gets = 0, releases = 0, luids = 0;
    int query_result = 0;
    bool null_result = false, bad_luid = false;
    Object* returned_device = nullptr;
    std::uint32_t adapter_low = 73;
};
static Object device{};
static std::int32_t __attribute__((ms_abi)) query(
    void* value, const TestGuid* guid, void** output)
{
    auto& object = *static_cast<Object*>(value);
    ++object.queries;
    const TestGuid expected{0x189819f1, 0x1db6, 0x4b57,
        {0xbe, 0x54, 0x18, 0x21, 0x33, 0x9b, 0x85, 0xf7}};
    assert(!std::memcmp(guid, &expected, sizeof(expected)));
    *output = nullptr;
    if (object.query_result < 0 || object.null_result) return object.query_result;
    auto* result = object.returned_device ? object.returned_device : &device;
    ++result->references;
    *output = result;
    return object.query_result;
}
static std::int32_t __attribute__((ms_abi)) get(
    void* value, const TestGuid* guid, void** output)
{
    auto& object = *static_cast<Object*>(value);
    ++object.gets;
    return query(value, guid, output);
}
static std::uint32_t __attribute__((ms_abi)) release(void* value)
{
    auto& object = *static_cast<Object*>(value);
    assert(object.references > 1);
    ++object.releases;
    return --object.references;
}
static TestLuid* __attribute__((ms_abi)) luid(void* value, TestLuid* output)
{
    auto& object = *static_cast<Object*>(value);
    ++object.luids;
    *output = {object.adapter_low, -12};
    return object.bad_luid ? nullptr : output;
}
int main()
{
    void* table[44]{};
    table[0] = reinterpret_cast<void*>(query);
    table[2] = reinterpret_cast<void*>(release);
    table[7] = reinterpret_cast<void*>(get);
    table[43] = reinterpret_cast<void*>(luid);
    device.table = table;
    Object child{table};
    const TestLuid expected{73, -12}, wrong{74, -12};
    assert(!testOwner(nullptr, false));
    releaseCom(nullptr);
    TestLuid value{};
    assert(!testLuid(nullptr, value));
    unsigned before = allocations;
    for (unsigned i = 0; i < 32; ++i)
    {
        auto temporary = testOwner(&child, true);
        assert(temporary.get() == &device && device.references == 2);
        assert(testLuid(temporary.get(), value));
        assert(value.low == expected.low && value.high == expected.high);
    }
    assert(allocations == before);
    assert(child.gets == 32 && child.queries == 32);
    assert(device.releases == 32 && device.references == 1 && device.luids == 32);
    assert(testMatches(&child, true, expected));
    assert(!testMatches(&child, true, wrong));
    device.bad_luid = true;
    unsigned released = device.releases;
    assert(!testMatches(&child, true, expected));
    assert(device.references == 1 && device.releases == released + 1);
    device.bad_luid = false;
    for (bool is_child : {false, true})
    {
        Object& object = is_child ? child : device;
        unsigned queries = object.queries, gets = object.gets;
        released = device.releases;
        object.query_result = -1;
        assert(!testOwner(&object, is_child));
        object.query_result = 0;
        object.null_result = true;
        assert(!testOwner(&object, is_child));
        object.null_result = false;
        assert(object.queries == queries + 2 && object.gets == gets + (is_child ? 2 : 0));
        assert(device.references == 1 && device.releases == released);
        std::shared_ptr<void> persistent;
        {
            auto temporary = testOwner(&object, is_child);
            assert(device.references == 2);
            persistent = std::move(temporary);
            assert(!temporary && persistent.get() == &device);
        }
        assert(device.references == 2 && device.releases == released);
        auto copy = persistent;
        persistent.reset();
        assert(device.references == 2 && device.releases == released);
        copy.reset();
        assert(device.references == 1 && device.releases == released + 1);
    }
}
'''

HUD = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <string.h>
/* Only the Windows export annotation is irrelevant to this native fixture. */
#define __declspec(value)
#include "d3dmetal-sidecar/third-party/fidelityfx/Kits/FidelityFX/framegeneration/include/ffx_framegeneration.h"
#define TRUE 1
typedef struct { unsigned references, adds, releases; } IUnknown;
static IUnknown *replacement;
static unsigned IUnknown_AddRef(IUnknown *object)
{
    ++object->adds;
    return ++object->references;
}
static unsigned IUnknown_Release(IUnknown *object)
{
    assert(object->references > 1);
    if (replacement) assert(replacement->references == 2);
    ++object->releases;
    return --object->references;
}
struct fg_context {
    struct FfxApiResource hudless;
    struct ffxConfigureDescFrameGeneration native_config;
    int native_config_valid;
};
'''

HUD_MAIN = r'''
int main(void)
{
    IUnknown first = {1, 0, 0}, second = {1, 0, 0};
    struct fg_context context = {0};
    struct ffxConfigureDescFrameGeneration desc = {0};
    ffxApiHeader extension = {0};
    desc.header.type = FFX_API_CONFIGURE_DESC_TYPE_FRAMEGENERATION;
    desc.header.pNext = &extension;
    desc.frameGenerationEnabled = true;
    desc.HUDLessColor.resource = &first;
    desc.HUDLessColor.description.width = 1920;
    desc.frameID = 1;
    store_native_config(&context, &desc);
    assert(first.references == 2 && first.adds == 1 && !first.releases);
    assert(context.native_config_valid && !context.native_config.header.pNext);
    assert(desc.header.pNext == &extension);

    desc.HUDLessColor.description.width = 2560;
    desc.HUDLessColor.description.height = 1440;
    desc.HUDLessColor.description.format = FFX_API_SURFACE_FORMAT_R16G16B16A16_FLOAT;
    desc.HUDLessColor.description.usage = 17;
    desc.HUDLessColor.state = 23;
    desc.frameID = 2;
    desc.flags = FFX_FRAMEGENERATION_FLAG_NO_SWAPCHAIN_CONTEXT_NOTIFY;
    desc.allowAsyncWorkloads = true;
    desc.onlyPresentGenerated = true;
    desc.generationRect.width = 2560;
    desc.generationRect.height = 1440;
    desc.presentCallbackUserContext = &first;
    desc.frameGenerationCallbackUserContext = &second;
    store_native_config(&context, &desc);
    assert(first.references == 2 && first.adds == 1 && !first.releases);
    assert(context.hudless.resource == &first && context.hudless.state == 23);
    assert(!memcmp(&context.hudless.description, &desc.HUDLessColor.description,
                   sizeof(context.hudless.description)));
    assert(context.native_config.HUDLessColor.state == 23);
    assert(context.native_config.HUDLessColor.description.width == 2560);
    assert(context.native_config.frameID == 2 && context.native_config.flags == desc.flags);
    assert(context.native_config.allowAsyncWorkloads && context.native_config.onlyPresentGenerated);
    assert(context.native_config.generationRect.width == 2560);
    assert(context.native_config.generationRect.height == 1440);
    assert(context.native_config.presentCallbackUserContext == &first);
    assert(context.native_config.frameGenerationCallbackUserContext == &second);
    assert(!context.native_config.header.pNext && desc.header.pNext == &extension);

    replacement = &second;
    desc.HUDLessColor.resource = &second;
    store_native_config(&context, &desc);
    replacement = NULL;
    assert(first.references == 1 && first.releases == 1);
    assert(second.references == 2 && second.adds == 1 && !second.releases);
    assert(context.hudless.resource == &second);
    /* Replay may alias the saved configuration itself. */
    store_native_config(&context, &context.native_config);
    assert(second.references == 2 && second.adds == 1 && !second.releases);

    desc.frameGenerationEnabled = false;
    desc.frameID = 3;
    store_native_config(&context, &desc);
    assert(second.references == 1 && second.releases == 1);
    assert(!context.hudless.resource && !context.hudless.description.width && !context.hudless.state);
    assert(!context.native_config.HUDLessColor.resource);
    assert(!context.native_config.frameGenerationEnabled && context.native_config.frameID == 3);
    store_native_config(&context, &desc);
    assert(second.adds == 1 && second.releases == 1);
    desc.frameGenerationEnabled = true;
    store_native_config(&context, &desc);
    assert(second.references == 2 && second.adds == 2);
    desc.HUDLessColor.resource = NULL;
    store_native_config(&context, &desc);
    assert(second.references == 1 && second.releases == 2);
    assert(!context.hudless.resource && !context.native_config.HUDLessColor.resource);
}
'''


SNAPSHOT = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include <stdio.h>
#include <time.h>
#define __declspec(value)
#include "d3dmetal-sidecar/third-party/fidelityfx/Kits/FidelityFX/framegeneration/include/ffx_framegeneration.h"
#define TRUE 1
#define FALSE 0
#define ARRAY_SIZE(value) (sizeof(value) / sizeof((value)[0]))
typedef int BOOL;
static unsigned lock_depth, allocations, frees, fail_allocation;
typedef struct { unsigned references; } IUnknown;
static unsigned IUnknown_AddRef(IUnknown *object) { return ++object->references; }
static unsigned IUnknown_Release(IUnknown *object)
{
    assert(!lock_depth && object->references > 1);
    return --object->references;
}
static void EnterCriticalSection(pthread_mutex_t *lock)
{
    assert(!pthread_mutex_lock(lock));
    ++lock_depth;
}
static void LeaveCriticalSection(pthread_mutex_t *lock)
{
    --lock_depth;
    assert(!pthread_mutex_unlock(lock));
}
static void *fixture_alloc(void *user, uint64_t size)
{
    assert(user == &allocations && !lock_depth);
    ++allocations;
    return fail_allocation ? NULL : malloc(size);
}
static void fixture_free(void *user, void *pointer)
{
    assert(user == &allocations && !lock_depth);
    ++frees;
    free(pointer);
}
static uint64_t pending_frame;
static BOOL pending;
static ffxReturnCode_t retire_result;
static ffxReturnCode_t retire_translation_frame(uint64_t context, uint64_t frame,
                                               uint64_t *oldest, BOOL *has_pending)
{
    (void)context;
    (void)frame;
    assert(lock_depth == 1);
    *oldest = pending_frame;
    *has_pending = pending;
    return retire_result;
}
'''

SNAPSHOT_CONTEXT = r'''
struct fg_context {
    ffxAllocationCallbacks allocation;
    BOOL custom_allocation;
    pthread_mutex_t dispatch_lock;
    FRAME_CONFIG_STORAGE
    uint64_t translated;
    BOOL have_last_prepare_frame_id;
};
'''


def snapshot_fixture(source):
    storage = section(source, "    struct frame_config_snapshot frame_configs[",
                      "    uint32_t display_width;")
    context = SNAPSHOT_CONTEXT.replace("    FRAME_CONFIG_STORAGE", storage)
    return (SNAPSHOT
            + section(source, "struct frame_config_snapshot\n", "struct prepare_desc_prefix\n")
            + context
            + section(source, "static void *allocate_memory(", "static BOOL matching_allocator(")
            + section(source, "static void release_resource(", "static void store_native_config("))


SNAPSHOT_MAIN = r'''
int main(void)
{
    struct fg_context context = {
        .allocation = {&allocations, fixture_alloc, fixture_free},
        .custom_allocation = TRUE, .dispatch_lock = PTHREAD_MUTEX_INITIALIZER
    };
    IUnknown first = {1}, second = {1};
    struct ffxConfigureDescFrameGeneration desc = {0};
    fail_allocation = TRUE;
    assert(!allocate_memory(context_allocator(&context), sizeof(context)));
    fail_allocation = FALSE;
    void *memory = allocate_memory(context_allocator(&context), sizeof(context));
    assert(memory);
    free_memory(context_allocator(&context), memory);
    allocations = frees = 0;
    assert(snapshot_frame_config(&context, &desc) == FFX_API_RETURN_OK);
    assert(!allocations);
    desc.frameGenerationEnabled = true;
    desc.HUDLessColor.resource = &first;
    /* Sparse, nonmonotonic IDs with exact collisions under modulo-64 indexing. */
    for (unsigned i = 0; i < 64; ++i) {
        desc.frameID = 64 * ((i * 17) % 64) + 64;
        assert(snapshot_frame_config(&context, &desc) == FFX_API_RETURN_OK);
    }
    assert(first.references == 65);
    assert(!find_frame_config(&context, 63));
    assert(find_frame_config(&context, 191)->frame_id == 128);
    desc.frameID = 4097;
    assert(snapshot_frame_config(&context, &desc) == FFX_API_RETURN_ERROR_RUNTIME_ERROR);
    assert(first.references == 65);
    desc.frameID = 128;
    desc.HUDLessColor.resource = &second;
    desc.HUDLessColor.state = 23;
    desc.HUDLessColor.description.width = 1920;
    fail_allocation = TRUE;
    /* No allocation can fail during replacement after context creation. */
    assert(snapshot_frame_config(&context, &desc) == FFX_API_RETURN_OK);
    assert(find_frame_config(&context, 128)->hudless.resource == &second);
    assert(!allocations && !frees);
    fail_allocation = FALSE;
    assert(snapshot_frame_config(&context, &desc) == FFX_API_RETURN_OK);
    assert(first.references == 64 && second.references == 2);
    assert(find_frame_config(&context, 191)->hudless.state == 23);
    assert(find_frame_config(&context, 191)->hudless.description.width == 1920);
    pending = TRUE;
    pending_frame = 191;
    retire_frame_configs(&context, 4096);
    assert(!find_frame_config(&context, 127));
    assert(find_frame_config(&context, 191)->frame_id == 128);
    assert(find_frame_config(&context, 4096)->frame_id == 4096);
    retire_result = FFX_API_RETURN_ERROR_RUNTIME_ERROR;
    pending = FALSE;
    retire_frame_configs(&context, 4096);
    assert(find_frame_config(&context, 191)->frame_id == 128);
    retire_result = FFX_API_RETURN_OK;
    retire_frame_configs(&context, 4096);
    assert(!find_frame_config(&context, 4095));
    assert(second.references == 1 && first.references == 2);
    /* An older callback must not discard the newer retained fallback. */
    retire_frame_configs(&context, 64);
    assert(find_frame_config(&context, 4096)->frame_id == 4096);
    context.have_last_prepare_frame_id = TRUE;
    clear_frame_configs(&context);
    assert(!find_frame_config(&context, UINT64_MAX) && !context.have_last_prepare_frame_id);
    assert(first.references == 1 && second.references == 1);
    assert(!allocations && !frees);
    assert(!pthread_mutex_destroy(&context.dispatch_lock));
}
'''


PRESENT_CALLBACK = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#define __declspec(value)
#include "d3dmetal-sidecar/third-party/fidelityfx/Kits/FidelityFX/framegeneration/include/ffx_framegeneration.h"
#include "yaagl_fsr_fg_bridge.h"
typedef int BOOL;
typedef struct { int unused; } IDXGISwapChain4;
struct stable_swapchain;
struct stable_swapchain_vtbl {
    IDXGISwapChain4 *(*real)(struct stable_swapchain *);
};
struct stable_swapchain { const struct stable_swapchain_vtbl *lpVtbl; };
struct fg_context { struct stable_swapchain *swapchain; uint64_t present_source; };
struct callback_binding {
    struct fg_context *context;
    FfxApiPresentCallbackFunc present;
    void *present_user;
    BOOL only_generated;
};
static struct { FfxApiPresentCallbackFunc default_present; } native;
static IDXGISwapChain4 real_swapchain;
static IDXGISwapChain4 *get_real(struct stable_swapchain *swapchain)
{
    assert(swapchain);
    return &real_swapchain;
}
static struct yaagl_fsr_fg_present_packet observed;
static unsigned notifications;
static ffxReturnCode_t bridge_call(void *packet)
{
    observed = *(struct yaagl_fsr_fg_present_packet *)packet;
    ++notifications;
    return FFX_API_RETURN_OK;
}
'''

PRESENT_CALLBACK_MAIN = r'''
static struct ffxCallbackDescFrameGenerationPresentPremulAlpha extension;
static unsigned app_calls, default_calls;
static ffxReturnCode_t default_present(ffxCallbackDescFrameGenerationPresent *desc, void *user)
{
    assert(!user && desc->header.pNext == &extension.header && extension.usePremulAlpha);
    ++default_calls;
    return FFX_API_RETURN_OK;
}
static ffxReturnCode_t app_present(ffxCallbackDescFrameGenerationPresent *desc, void *user)
{
    assert(user == &app_calls);
    ++app_calls;
    /* A nested presentation occurs before the outer callback returns.
     * Descriptor edits must not change the outer native packet's identity.
     */
    struct yaagl_fsr_fg_present_packet nested = {0};
    nested.frame_id = 99;
    bridge_call(&nested);
    desc->frameID = 99;
    desc->isGeneratedFrame = false;
    return FFX_API_RETURN_ERROR_RUNTIME_ERROR;
}
int main(void)
{
    const struct stable_swapchain_vtbl vtbl = {get_real};
    struct stable_swapchain swapchain = {&vtbl};
    struct fg_context context = {&swapchain, 7};
    struct callback_binding binding = {&context, app_present, &app_calls, 1};
    ffxCallbackDescFrameGenerationPresent desc = {0};
    native.default_present = default_present;
    desc.frameID = 3;
    desc.isGeneratedFrame = true;
    assert(compose_present(&binding, &desc) == FFX_API_RETURN_ERROR_RUNTIME_ERROR);
    assert(app_calls == 1 && notifications == 2);
    assert(observed.frame_id == 3 && observed.generated && observed.source == 7 &&
           observed.only_generated);
    assert(observed.swapchain == (uintptr_t)&real_swapchain);
    binding.present = NULL;
    extension.usePremulAlpha = true;
    desc.header.pNext = &extension.header;
    assert(compose_present(&binding, &desc) == FFX_API_RETURN_OK);
    assert(default_calls == 1 && notifications == 3 && !observed.generated);
    native.default_present = NULL;
    binding.present = app_present;
    assert(compose_present(&binding, &desc) == FFX_API_RETURN_ERROR_RUNTIME_ERROR);
    assert(app_calls == 2 && notifications == 4 && observed.frame_id == 99);
}
'''


BINDINGS = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#define __declspec(value)
#include "d3dmetal-sidecar/third-party/fidelityfx/Kits/FidelityFX/framegeneration/include/ffx_framegeneration.h"
typedef int BOOL;
struct fg_context;
'''

BINDINGS_NATIVE = r'''
struct stable_swapchain;
struct stable_swapchain_vtbl {
    bool (*wait_for_presents)(struct stable_swapchain *);
};
struct stable_swapchain { const struct stable_swapchain_vtbl *lpVtbl; };
struct fg_context {
    ffxContext original;
    struct stable_swapchain *swapchain;
    struct callback_binding *native_bindings, *active_native_binding;
};
static struct callback_binding *installed;
static unsigned allocations, frees, waits;
static bool drained = true;
static bool wait_presenter(struct stable_swapchain *swapchain)
{
    assert(swapchain);
    ++waits;
    return drained;
}
static const ffxAllocationCallbacks *context_allocator(struct fg_context *context)
{
    (void)context;
    return NULL;
}
static void *allocate_memory(const ffxAllocationCallbacks *callbacks, size_t size)
{
    (void)callbacks;
    ++allocations;
    return calloc(1, size);
}
static void free_memory(const ffxAllocationCallbacks *callbacks, void *memory)
{
    (void)callbacks;
    assert(memory != installed); /* Draining does not uninstall a callback. */
    ++frees;
    free(memory);
}
static ffxReturnCode_t native_present_callback(ffxCallbackDescFrameGenerationPresent *desc, void *user)
{
    (void)desc; (void)user;
    return FFX_API_RETURN_OK;
}
static ffxReturnCode_t native_generation_callback(ffxDispatchDescFrameGeneration *desc, void *user)
{
    (void)desc; (void)user;
    return FFX_API_RETURN_OK;
}
static ffxReturnCode_t configure_native(ffxContext *context, const ffxConfigureDescHeader *header)
{
    const struct ffxConfigureDescFrameGeneration *desc = (const void *)header;
    (void)context;
    if (!(desc->flags & FFX_FRAMEGENERATION_FLAG_NO_SWAPCHAIN_CONTEXT_NOTIFY))
        installed = desc->presentCallbackUserContext;
    return FFX_API_RETURN_OK;
}
static struct {
    FfxApiPresentCallbackFunc default_present;
    ffxReturnCode_t (*configure)(ffxContext *, const ffxConfigureDescHeader *);
} native = {native_present_callback, configure_native};
'''

BINDINGS_MAIN = r'''
int main(void)
{
    const struct stable_swapchain_vtbl vtbl = {wait_presenter};
    struct stable_swapchain swapchain = {&vtbl};
    struct fg_context context = {0};
    struct ffxConfigureDescFrameGeneration desc = {0};
    context.swapchain = &swapchain;
    assert(configure_native_callbacks(&context, &desc) == FFX_API_RETURN_OK);
    struct callback_binding *first = installed;
    assert(first && context.active_native_binding == first && allocations == 1 && !frees);
    unsigned previous_waits = waits;
    desc.flags = FFX_FRAMEGENERATION_FLAG_NO_SWAPCHAIN_CONTEXT_NOTIFY;
    desc.frameGenerationCallback = native_generation_callback;
    desc.onlyPresentGenerated = true;
    assert(configure_native_callbacks(&context, &desc) == FFX_API_RETURN_OK);
    assert(installed == first && context.active_native_binding == first);
    assert(allocations == 1 && !frees && waits == previous_waits && !first->only_generated);
    desc.flags = 0;
    drained = false;
    assert(configure_native_callbacks(&context, &desc) == FFX_API_RETURN_OK);
    assert(installed != first && installed->only_generated && allocations == 2 && !frees);
    drained = true;
    desc.frameGenerationCallback = NULL;
    assert(configure_native_callbacks(&context, &desc) == FFX_API_RETURN_OK);
    assert(allocations == 3 && frees == 2 && installed->only_generated);
    desc.onlyPresentGenerated = false;
    assert(configure_native_callbacks(&context, &desc) == FFX_API_RETURN_OK);
    assert(allocations == 4 && frees == 3 && !installed->only_generated);
    installed = context.active_native_binding = NULL;
    reclaim_native_bindings(&context);
    assert(!context.native_bindings && frees == allocations);
}
'''


CONFIGURE = r'''
#include "yaagl_fsr_fg_bridge.h"
typedef int LONG;
typedef pthread_mutex_t CRITICAL_SECTION;
typedef pthread_mutex_t SRWLOCK;
#define AcquireSRWLockExclusive EnterCriticalSection
#define ReleaseSRWLockExclusive LeaveCriticalSection
static LONG InterlockedCompareExchange(LONG *value, LONG replacement, LONG expected)
{
    LONG old = *value;
    if (old == expected) *value = replacement;
    return old;
}
static LONG InterlockedExchange(LONG *value, LONG replacement)
{
    LONG old = *value;
    *value = replacement;
    return old;
}
struct stable_swapchain;
struct stable_swapchain_vtbl {
    void (*set_config)(struct stable_swapchain *, const FfxFrameGenerationConfig *);
    bool (*wait_for_presents)(struct stable_swapchain *);
};
struct stable_swapchain { const struct stable_swapchain_vtbl *lpVtbl; };
'''

CONFIGURE_BOUNDARIES = r'''
static SRWLOCK contexts_lock = PTHREAD_MUTEX_INITIALIZER;
static struct fg_context *contexts;
static unsigned bridge_on, bridge_off, notifications, swapchain_references = 1;
static BOOL translator_enabled, reject_activation;
static FfxFrameGenerationConfig installed_config;
static struct stable_swapchain *get_stable_swapchain(void *object)
{
    ++swapchain_references;
    return object;
}
static void release_swapchain(struct stable_swapchain *swapchain)
{
    assert(swapchain && swapchain_references > 1);
    --swapchain_references;
}
static bool wait_presenter(struct stable_swapchain *swapchain)
{
    assert(swapchain && !lock_depth);
    return true;
}
static void set_config(struct stable_swapchain *swapchain, const FfxFrameGenerationConfig *config)
{
    struct callback_binding *binding = config->frameGenerationCallbackContext;
    assert(swapchain && !lock_depth);
    /* The native boundary consumes the snapshot synchronously, before enabled
     * is published by configure_frame_generation. */
    if (config->frameGenerationEnabled) {
        assert(binding && translator_enabled);
        struct frame_config_snapshot *snapshot = find_frame_config(binding->context, config->frameID);
        assert(snapshot && snapshot->frame_id == config->frameID);
        assert(snapshot->hudless.resource == config->HUDLessColor.resource);
        assert(snapshot->hudless.state == config->HUDLessColor.state);
    }
    installed_config = *config;
    ++notifications;
}
static ffxReturnCode_t bridge_call(void *opaque)
{
    const struct yaagl_fsr_fg_configure_packet *packet = opaque;
    assert(packet->header.operation == YAAGL_FSR_FG_CONFIGURE);
    assert(packet->header.context == 7);
    if (packet->enabled) {
        ++bridge_on;
        if (reject_activation) return FFX_API_RETURN_ERROR_RUNTIME_ERROR;
    } else ++bridge_off;
    translator_enabled = packet->enabled;
    return FFX_API_RETURN_OK;
}
/* Callback execution and the native provider are outside this configuration
 * fixture. Trap accidental entry instead of returning invented success. */
static ffxReturnCode_t present_callback(ffxCallbackDescFrameGenerationPresent *desc, void *user)
{
    (void)desc; (void)user;
    abort();
}
static ffxReturnCode_t generation_callback(ffxDispatchDescFrameGeneration *desc, void *user)
{
    (void)desc; (void)user;
    abort();
}
#define native_present_callback present_callback
#define native_generation_callback generation_callback
static ffxReturnCode_t native_configure(ffxContext *context, const ffxConfigureDescHeader *desc)
{
    (void)context; (void)desc;
    abort();
}
static struct {
    FfxApiPresentCallbackFunc default_present;
    ffxReturnCode_t (*configure)(ffxContext *, const ffxConfigureDescHeader *);
} native = {NULL, native_configure};
'''

CONFIGURE_MAIN = r'''
static void exercise_configuration(BOOL notify)
{
    const struct stable_swapchain_vtbl vtbl = {set_config, wait_presenter};
    struct stable_swapchain swapchain = {&vtbl};
    struct fg_context context = {
        .allocation = {&allocations, fixture_alloc, fixture_free},
        .custom_allocation = TRUE, .dispatch_lock = PTHREAD_MUTEX_INITIALIZER,
        .mode = CONTEXT_METALFX, .translated = 7
    };
    IUnknown first = {1}, second = {1};
    struct ffxConfigureDescFrameGeneration desc = {0};
    desc.swapChain = &swapchain;
    desc.flags = notify ? 0 : FFX_FRAMEGENERATION_FLAG_NO_SWAPCHAIN_CONTEXT_NOTIFY;
    desc.frameGenerationEnabled = true;
    desc.HUDLessColor.resource = &first;
    desc.frameID = 1;
    bridge_on = bridge_off = notifications = 0;
    contexts = &context;
    reject_activation = TRUE;
    assert(configure_frame_generation(&context, &desc) == FFX_API_RETURN_ERROR_RUNTIME_ERROR);
    assert(!context.enabled && !translator_enabled && !context.native_config_valid);
    assert(!context.frame_config_count && !context.swapchain && first.references == 1);
    reject_activation = FALSE;
    assert(configure_frame_generation(&context, &desc) == FFX_API_RETURN_OK);
    assert(context.enabled && translator_enabled && first.references == 3);
    struct callback_binding *binding = context.binding;
    desc.frameID = 2;
    desc.HUDLessColor.resource = &second;
    desc.HUDLessColor.state = 23;
    desc.HUDLessColor.description.width = 1920;
    assert(configure_frame_generation(&context, &desc) == FFX_API_RETURN_OK);
    assert(context.binding == binding && bridge_on == 2 && !bridge_off);
    assert(context.native_config.frameID == 2 && context.hudless.resource == &second);
    assert(find_frame_config(&context, 2)->hudless.state == 23);
    assert(first.references == 2 && second.references == 3);
    assert(notifications == (notify ? 2u : 0u));
    if (notify) {
        assert(installed_config.frameID == 2 && installed_config.HUDLessColor.resource == &second);
        assert(installed_config.HUDLessColor.description.width == 1920);
        assert(installed_config.frameGenerationCallbackContext == binding);
    }
    /* Fill the real fixed snapshot storage, then fail a repeated ON. */
    for (desc.frameID = 3; desc.frameID <= 64; ++desc.frameID)
        assert(snapshot_frame_config(&context, &desc) == FFX_API_RETURN_OK);
    assert(configure_frame_generation(&context, &desc) == FFX_API_RETURN_ERROR_RUNTIME_ERROR);
    assert(context.enabled && translator_enabled && bridge_on == 2 && !bridge_off);
    assert(context.native_config.frameID == 2 && context.frame_config_count == 64);
    assert(notifications == (notify ? 2u : 0u));
    desc.frameGenerationEnabled = false;
    assert(configure_frame_generation(&context, &desc) == FFX_API_RETURN_OK);
    assert(!context.enabled && !translator_enabled && !context.frame_config_count);
    assert(!context.hudless.resource && first.references == 1 && second.references == 1);
    assert(configure_frame_generation(&context, &desc) == FFX_API_RETURN_OK);
    assert(bridge_off == 2 && notifications == (notify ? 4u : 0u));
    desc.frameGenerationEnabled = true;
    /* A full snapshot table while disabled forces initial-activation rollback. */
    for (desc.frameID = 1; desc.frameID <= 64; ++desc.frameID)
        assert(snapshot_frame_config(&context, &desc) == FFX_API_RETURN_OK);
    assert(configure_frame_generation(&context, &desc) == FFX_API_RETURN_ERROR_RUNTIME_ERROR);
    assert(!context.enabled && !translator_enabled && bridge_on == 3 && bridge_off == 3);
    assert(!context.native_config.frameGenerationEnabled && !context.hudless.resource);
    clear_frame_configs(&context);
    assert(configure_frame_generation(&context, &desc) == FFX_API_RETURN_OK);
    assert(context.enabled && translator_enabled && bridge_on == 4);
    assert(context.native_config.frameID == 65 && find_frame_config(&context, 65));
    desc.frameGenerationEnabled = false;
    assert(configure_frame_generation(&context, &desc) == FFX_API_RETURN_OK);
    assert(!context.enabled && !translator_enabled && second.references == 1);
    assert(detach_swapchain(&context) == FFX_API_RETURN_OK);
    assert(swapchain_references == 1);
    /* Pending configurations deliberately notify on every call, including OFF,
     * without publishing selected-mode enabled state or snapshots. */
    context.mode = MODE_PENDING;
    unsigned on = bridge_on, off = bridge_off, notified = notifications;
    for (unsigned i = 0; i < 4; ++i) {
        desc.frameGenerationEnabled = i < 2;
        desc.frameID = 100 + i;
        assert(configure_frame_generation(&context, &desc) == FFX_API_RETURN_OK);
        assert(context_mode(&context) == MODE_PENDING && !context.enabled);
        assert(context.native_config.frameID == desc.frameID && !context.frame_config_count);
        assert(translator_enabled == desc.frameGenerationEnabled);
    }
    assert(bridge_on == on + 2 && bridge_off == off + 2 && notifications == notified);
    assert(second.references == 1 && !context.hudless.resource);
    retire_frame_configs(&context, desc.frameID);
    assert(!pthread_mutex_destroy(&context.dispatch_lock));
    contexts = NULL;
}
int main(void)
{
    exercise_configuration(TRUE);
    exercise_configuration(FALSE);
    assert(allocations == frees);
}
'''


def compile_and_run(code, cxx=False):
    with tempfile.TemporaryDirectory(prefix="yaagl-fsr-lifecycle-") as directory:
        source = pathlib.Path(directory) / ("lifecycle.cpp" if cxx else "lifecycle.c")
        binary = pathlib.Path(directory) / "lifecycle"
        source.write_text(code)
        compiler = ("clang++", "-std=c++20", "-arch", "x86_64", "-Wl,-adhoc_codesign") if cxx else ("clang", "-std=c11")
        subprocess.run((*compiler, "-D_DARWIN_C_SOURCE", "-Wall", "-Wextra",
                        "-Werror", "-pthread", "-I", str(ROOT / "include"),
                        "-iquote", str(ROOT),
                        str(source), "-o", str(binary)), check=True, timeout=60)
        subprocess.run((str(binary),), check=True, timeout=30)


class FsrLifecycleTest(unittest.TestCase):
    def test_selected_metalfx_configuration(self):
        source = (ROOT / "dlls/amd_fidelityfx_framegeneration_dx12/main.c").read_text()
        declarations = section(source, "enum context_mode\n", "static SRWLOCK contexts_lock")
        snapshots = section(source, "static void release_resource(", "static struct fg_context *acquire_context(")
        initialize = section(source, "static void initialize_packet(", "static ffxReturnCode_t bridge_call(")
        translation = section(source, "static ffxReturnCode_t configure_translation(", "static ffxReturnCode_t destroy_translation(")
        allocation = section(source, "static void *allocate_memory(", "static BOOL matching_allocator(")
        claims = section(source, "static BOOL claim_swapchain(", "struct normalized_dispatch\n")
        reclaim = section(source, "static void reclaim_native_bindings(", "static ffxReturnCode_t compose_present(")
        native = section(source, "static ffxReturnCode_t configure_native_callbacks(", "static void release_swapchain(")
        select = section(source, "static ffxReturnCode_t configure_frame_generation(", "static ffxReturnCode_t validate_public_dispatch(")
        configure = section(source, "static void build_swapchain_config(", "static void free_context_storage(")
        compile_and_run(SNAPSHOT + CONFIGURE + declarations + allocation + snapshots
                        + CONFIGURE_BOUNDARIES + initialize + translation + claims
                        + reclaim + native + select + configure + CONFIGURE_MAIN)

    def test_native_binding_installation(self):
        source = (ROOT / "dlls/amd_fidelityfx_framegeneration_dx12/main.c").read_text()
        binding = section(source, "struct callback_binding\n", "struct callback_scope\n")
        matches = section(source, "static BOOL binding_matches(", "struct frame_config_snapshot\n")
        reclaim = section(source, "static void reclaim_native_bindings(", "static ffxReturnCode_t compose_present(")
        configure = section(source, "static ffxReturnCode_t configure_native_callbacks(", "static void release_swapchain(")
        compile_and_run(BINDINGS + binding + matches + BINDINGS_NATIVE + reclaim + configure + BINDINGS_MAIN)

    def test_present_callback_identity(self):
        source = (ROOT / "dlls/amd_fidelityfx_framegeneration_dx12/main.c").read_text()
        initialize = section(source, "static void initialize_packet(", "static ffxReturnCode_t bridge_call(")
        compose = section(source, "static ffxReturnCode_t compose_present(", "static ffxReturnCode_t native_present_callback(")
        compile_and_run(PRESENT_CALLBACK + initialize + compose + PRESENT_CALLBACK_MAIN)

    def test_sr_dispatch_device_validation(self):
        source = (ROOT / "d3dmetal-sidecar/fsr-translator.mm").read_text()
        ownership = section(source, "struct ComGuid {", "struct State {")
        command = section(source, "    auto commandDevice = deviceOwner(commandList, true);",
                          "    const bool newGeneration =")
        resource = section(source, "        auto resourceDevice = deviceOwner(resources[i], true);",
                           "        const auto access = i == 3")
        fixture = section(COM_MAIN, "struct Object {", "int main()")
        compile_and_run(COM + ownership + '''
using TestGuid = ComGuid;
using TestLuid = AdapterLuid;
''' + fixture + '''
constexpr unsigned kParameter = 6, kOk = 0;
struct DispatchState {
    AdapterLuid adapter{73, -12};
    std::shared_ptr<void> executionDevice;
};
static unsigned validate(DispatchState* state, void* commandList, void* input) {
''' + command + '''
    void* resources[]{input};
    unsigned i = 0;
''' + resource + '''
    return kOk;
}
int main() {
    void* table[44]{};
    table[0] = reinterpret_cast<void*>(query);
    table[2] = reinterpret_cast<void*>(release);
    table[7] = reinterpret_cast<void*>(get);
    table[43] = reinterpret_cast<void*>(luid);
    device.table = table;
    Object command{table}, resource{table}, other{table};
    DispatchState state;
    assert(validate(&state, &command, &resource) == kOk);
    assert(state.executionDevice.get() == &device);
    assert(device.luids == 1 && device.references == 2 && device.releases == 1);
    assert(command.gets == 1 && resource.gets == 1);
    // Exercise the path where commandDevice is not moved into persistent state.
    assert(validate(&state, &command, &resource) == kOk);
    assert(device.luids == 2 && device.references == 2 && device.releases == 3);
    resource.returned_device = &other;
    assert(validate(&state, &command, &resource) == kOk);
    assert(other.luids == 1 && other.references == 1 && other.releases == 1);
    other.adapter_low = 74;
    assert(validate(&state, &command, &resource) == kParameter);
    assert(other.luids == 2 && other.references == 1 && other.releases == 2);
    other.adapter_low = 73;
    other.bad_luid = true;
    assert(validate(&state, &command, &resource) == kParameter);
    assert(other.luids == 3 && other.references == 1 && other.releases == 3);
    resource.query_result = -1;
    assert(validate(&state, &command, &resource) == kParameter);
    resource.query_result = 0;
    resource.null_result = true;
    assert(validate(&state, &command, &resource) == kParameter);
    assert(other.luids == 3 && other.references == 1 && other.releases == 3);
    unsigned resourceGets = resource.gets;
    command.query_result = -1;
    assert(validate(&state, &command, &resource) == kParameter);
    command.query_result = 0;
    device.adapter_low = 74;
    assert(validate(&state, &command, &resource) == kParameter);
    assert(resource.gets == resourceGets && device.references == 2);
    state.executionDevice.reset();
    assert(device.references == 1);
}
''', cxx=True)

    def test_frame_configuration_snapshots(self):
        source = (ROOT / "dlls/amd_fidelityfx_framegeneration_dx12/main.c").read_text()
        compile_and_run(snapshot_fixture(source) + SNAPSHOT_MAIN)

    def test_scoped_com_ownership(self):
        for fg in (False, True):
            with self.subTest(framegeneration=fg):
                name = "fsr-framegeneration.mm" if fg else "fsr-translator.mm"
                source = (ROOT / "d3dmetal-sidecar" / name).read_text()
                if fg:
                    body = section(source, "struct Guid {", "struct Command {")
                    aliases = '''
using TestGuid = Guid;
using TestLuid = Luid;
#define testOwner owner
#define testLuid getLuid
#define testMatches matches
'''
                else:
                    body = section(source, "struct ComGuid {", "struct State {")
                    aliases = '''
using TestGuid = ComGuid;
using TestLuid = AdapterLuid;
#define testOwner deviceOwner
#define testLuid adapterLuid
static bool testMatches(void* object, bool child, const TestLuid& expected)
{
    auto device = deviceOwner(object, child);
    TestLuid value{};
    return device && adapterLuid(device.get(), value) && sameAdapter(value, expected);
}
'''
                compile_and_run(COM + body + aliases + COM_MAIN, cxx=True)

    def test_hud_configuration_retention(self):
        source = (ROOT / "dlls/amd_fidelityfx_framegeneration_dx12/main.c").read_text()
        resources = section(source, "static void release_resource(", "static ffxReturnCode_t snapshot_frame_config(")
        store = section(source, "static void store_native_config(", "static struct fg_context *acquire_context(")
        compile_and_run(HUD + resources + store + HUD_MAIN)

    def test_loader_publication(self):
        for fg in (False, True):
            with self.subTest(framegeneration=fg):
                name = "amd_fidelityfx_framegeneration_dx12" if fg else "amd_fidelityfx_upscaler_dx12"
                source = (ROOT / "dlls" / name / "unixlib.c").read_text()
                prefix = ('#define FG 1\n' if fg else '')
                prefix += '#define API_SYMBOL "' + ("yaagl_fsr_fg_api" if fg else "yaagl_fsr_api") + '"\n'
                prefix += '#define API_CALL ' + ("fsr_fg_api" if fg else "fsr_api") + '\n'
                packet = "yaagl_fsr_fg_packet_header" if fg else "yaagl_fsr_packet_header"
                body = section(source, "typedef uint32_t (*", "const unixlib_entry_t")
                compile_and_run(prefix + LOADER + body + '\ntypedef struct ' + packet + ' Packet;\n' + LOADER_MAIN)

    def test_destroy_wakeup(self):
        source = (ROOT / "dlls/amd_fidelityfx_framegeneration_dx12/main.c").read_text()
        body = section(source, "static void release_context(", "static void report_validation(")
        compile_and_run(WAKE + body + WAKE_MAIN)


if __name__ == "__main__":
    unittest.main()
