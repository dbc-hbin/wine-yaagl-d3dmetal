#!/usr/bin/env python3
"""Exercise production FSR loader publication/retry and FG destruction wakeups."""

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


def compile_and_run(code):
    with tempfile.TemporaryDirectory(prefix="yaagl-fsr-lifecycle-") as directory:
        source = pathlib.Path(directory) / "lifecycle.c"
        binary = pathlib.Path(directory) / "lifecycle"
        source.write_text(code)
        subprocess.run(("clang", "-std=c11", "-D_DARWIN_C_SOURCE", "-Wall", "-Wextra",
                        "-Werror", "-pthread", "-I", str(ROOT / "include"),
                        str(source), "-o", str(binary)), check=True, timeout=60)
        subprocess.run((str(binary),), check=True, timeout=30)


class FsrLifecycleTest(unittest.TestCase):
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
