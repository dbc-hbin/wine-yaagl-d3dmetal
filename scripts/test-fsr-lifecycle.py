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
    ++device.references;
    *output = &device;
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
    *output = {73, -12};
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


def compile_and_run(code, cxx=False):
    with tempfile.TemporaryDirectory(prefix="yaagl-fsr-lifecycle-") as directory:
        source = pathlib.Path(directory) / ("lifecycle.cpp" if cxx else "lifecycle.c")
        binary = pathlib.Path(directory) / "lifecycle"
        source.write_text(code)
        compiler = ("clang++", "-std=c++20", "-arch", "x86_64") if cxx else ("clang", "-std=c11")
        subprocess.run((*compiler, "-D_DARWIN_C_SOURCE", "-Wall", "-Wextra",
                        "-Werror", "-pthread", "-I", str(ROOT / "include"),
                        "-iquote", str(ROOT),
                        str(source), "-o", str(binary)), check=True, timeout=60)
        subprocess.run((str(binary),), check=True, timeout=30)


class FsrLifecycleTest(unittest.TestCase):
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
        resources = section(source, "static void release_resource(", "static void release_frame_config(")
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
