#!/usr/bin/env python3
"""Compile the production MSync wait functions with controlled shared-memory races."""

import os
import pathlib
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = pathlib.Path(os.environ.get("MSYNC_WAIT_SOURCE", ROOT / "dlls/ntdll/unix/msync.c")).read_text()


def section(start, end):
    return SOURCE[SOURCE.index(start):SOURCE.index(end, SOURCE.index(start))]


WAIT_SINGLE = section("static inline NTSTATUS msync_wait_single(", "static int wait_objects_ready(")
DO_SINGLE = section("static NTSTATUS do_single_wait(", "NTSTATUS msync_wait_objs(")
WAIT_OBJS = section("NTSTATUS msync_wait_objs(", "\n#else /* __APPLE__ */")

HARNESS = r'''
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#define MAXIMUM_WAIT_OBJECTS 64
#define TRUE 1
#define FALSE 0
#define TIMEOUT_INFINITE INT64_MAX
#define STATUS_SUCCESS 0
#define STATUS_ABANDONED 0x80
#define STATUS_ABANDONED_WAIT_0 STATUS_ABANDONED
#define STATUS_USER_APC 0xc0
#define STATUS_TIMEOUT 0x102
#define STATUS_PENDING 0x103
#define STATUS_UNSUCCESSFUL 0xc0000001u
#define SELECT_INTERRUPTIBLE 1
#define SELECT_ALERTABLE 2
#define UL_COMPARE_AND_WAIT_SHARED 3
#define ULF_NO_ERRNO 0x1000000
#define ERR(...) abort()

typedef uint32_t DWORD;
typedef int BOOLEAN;
typedef int BOOL;
typedef uint64_t ULONGLONG;
typedef int64_t LONGLONG;
typedef uint32_t NTSTATUS;
typedef struct { int64_t QuadPart; } LARGE_INTEGER;
enum { MSYNC_MUTEX = 1, MSYNC_SEMAPHORE, MSYNC_AUTO_EVENT, MSYNC_AUTO_SERVER,
       MSYNC_MANUAL_EVENT, MSYNC_MANUAL_SERVER };
struct semaphore { int count, max; unsigned short msync_type, refcount; int multiple_waiters; };
struct event { int signaled, unused; unsigned short msync_type, refcount; int multiple_waiters; };
struct mutex { int tid, count; unsigned short msync_type, refcount; int multiple_waiters; };
static union { struct semaphore sem; struct event event; struct mutex mutex; } objects[8];
static __thread int thread_tid = 42;
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t changed = PTHREAD_COND_INITIALIZER;
static int start_observer, observer_waiting, observer_done, watched, trigger, fail_once, alert_on_fail;
static NTSTATUS observer_result;
static int64_t now = 100;

static void NtQuerySystemTime(LARGE_INTEGER *time) { time->QuadPart = __atomic_load_n(&now, __ATOMIC_SEQ_CST); }
static LONGLONG update_timeout(ULONGLONG end)
{
    LONGLONG left = end - __atomic_load_n(&now, __ATOMIC_SEQ_CST);
    return left > 0 ? left : 0;
}
static int GetCurrentThreadId(void) { return thread_tid; }
static void *get_shm(int obj) { return &objects[obj]; }
static NTSTATUS server_wait(void *unused, int count, int flags, const LARGE_INTEGER *timeout)
{
    (void)unused; (void)count; (void)flags; (void)timeout;
    return STATUS_USER_APC;
}
static NTSTATUS msync_wait_multiple(const int *objs, void **shm, int alert, void *alert_shm,
                                   int count, ULONGLONG *end, int tid)
{
    (void)objs; (void)shm; (void)alert; (void)alert_shm; (void)count; (void)end; (void)tid;
    abort();
}
static int ulock_wait(uint32_t op, void *addr, uint64_t expected, uint64_t timeout)
{
    (void)op;
    if (thread_tid == 42 && timeout) return -ETIMEDOUT;
    pthread_mutex_lock(&lock);
    observer_waiting = 1;
    pthread_cond_broadcast(&changed);
    while (__atomic_load_n((int *)addr, __ATOMIC_SEQ_CST) == (int)expected)
        pthread_cond_wait(&changed, &lock);
    pthread_mutex_unlock(&lock);
    return 0;
}
static void signal_all(void *shm, unsigned int obj)
{
    (void)shm;
    pthread_mutex_lock(&lock);
    pthread_cond_broadcast(&changed);
    if (thread_tid == 42 && (int)obj == watched && observer_waiting)
        while (!observer_done) pthread_cond_wait(&changed, &lock);
    pthread_mutex_unlock(&lock);
}
static int cas_hook(int *addr, int expected, int desired)
{
    if (thread_tid == 42 && addr == &objects[trigger].event.signaled && fail_once)
    {
        fail_once = 0;
        pthread_mutex_lock(&lock);
        if (watched)
        {
            start_observer = 1;
            pthread_cond_broadcast(&changed);
            while (!observer_waiting) pthread_cond_wait(&changed, &lock);
        }
        pthread_mutex_unlock(&lock);
        __atomic_store_n(addr, 0, __ATOMIC_SEQ_CST);
        if (alert_on_fail) __atomic_store_n(&objects[5].event.signaled, 1, __ATOMIC_SEQ_CST);
        __atomic_store_n(&now, 20000, __ATOMIC_SEQ_CST);
        return 0; /* another waiter consumed this auto-event before our CAS */
    }
    __atomic_compare_exchange_n(addr, &expected, desired, 0, __ATOMIC_SEQ_CST, __ATOMIC_SEQ_CST);
    return expected;
}
#define __sync_val_compare_and_swap(addr, expected, desired) cas_hook((int *)(addr), expected, desired)
'''

CHECKS = r'''
static void reset(void)
{
    for (int i = 0; i < 8; i++) objects[i].mutex.tid = objects[i].mutex.count =
        objects[i].mutex.msync_type = 0;
    now = 100;
    watched = trigger = fail_once = alert_on_fail = start_observer = observer_waiting = observer_done = 0;
    thread_tid = 42;
}
static NTSTATUS wait_all(const int *ids, int count)
{
    LARGE_INTEGER timeout = { -10000 };
    return msync_wait_objs(count, ids, 0, 0, &timeout);
}
static void abandoned_and_success(void)
{
    int ids[] = { 1, 2 };
    reset();
    objects[1].mutex.msync_type = MSYNC_MUTEX;
    objects[1].mutex.tid = ~0;
    objects[2].event.msync_type = MSYNC_AUTO_EVENT;
    objects[2].event.signaled = 1;
    assert(wait_all(ids, 2) == STATUS_ABANDONED);
    assert(objects[1].mutex.tid == 42 && objects[1].mutex.count == 1);
    assert(objects[2].event.signaled == 0);
    assert(wait_all(ids, 2) == STATUS_TIMEOUT);
    assert(objects[1].mutex.count == 1);

    reset();
    objects[1].mutex.msync_type = MSYNC_MUTEX;
    objects[1].mutex.tid = 42;
    objects[1].mutex.count = 1;
    objects[2].event.msync_type = MSYNC_AUTO_EVENT;
    objects[2].event.signaled = 1;
    assert(wait_all(ids, 2) == STATUS_SUCCESS);
    assert(objects[1].mutex.tid == 42 && objects[1].mutex.count == 2);
    assert(objects[2].event.signaled == 0);
    assert(wait_all(ids, 2) == STATUS_TIMEOUT);
    assert(objects[1].mutex.count == 2);
}
static void rollback_owned_and_abandoned(void)
{
    int ids[] = { 1, 2, 3, 4 };
    reset();
    objects[1].mutex.msync_type = MSYNC_MUTEX;
    objects[1].mutex.tid = 42;
    objects[1].mutex.count = 1;
    objects[2].event.msync_type = MSYNC_AUTO_EVENT;
    objects[2].event.signaled = 1;
    objects[3].event.msync_type = MSYNC_MANUAL_EVENT;
    objects[3].event.signaled = 1;
    objects[4].event.msync_type = MSYNC_AUTO_EVENT;
    objects[4].event.signaled = 1;
    trigger = 4;
    fail_once = 1;
    assert(wait_all(ids, 4) == STATUS_TIMEOUT);
    assert(objects[1].mutex.tid == 42 && objects[1].mutex.count == 1);
    assert(objects[2].event.signaled == 1);
    assert(objects[3].event.signaled == 1);

    reset();
    objects[1].mutex.msync_type = MSYNC_MUTEX;
    objects[1].mutex.tid = ~0;
    objects[2].sem.msync_type = MSYNC_SEMAPHORE;
    objects[2].sem.count = 1;
    objects[3].event.msync_type = MSYNC_AUTO_EVENT;
    objects[3].event.signaled = 1;
    objects[4].event.msync_type = MSYNC_AUTO_EVENT;
    objects[4].event.signaled = 1;
    trigger = 4;
    fail_once = 1;
    assert(wait_all(ids, 4) == STATUS_TIMEOUT);
    assert(objects[1].mutex.tid == ~0 && objects[1].mutex.count == 0);
    assert(objects[2].sem.count == 1 && objects[3].event.signaled == 1);
    objects[4].event.signaled = 1;
    now = 100;
    assert(wait_all(ids, 4) == STATUS_ABANDONED);
    assert(objects[1].mutex.tid == 42 && objects[1].mutex.count == 1);
    assert(objects[2].sem.count == 0 && objects[3].event.signaled == 0);
}
static void alertable_rollback(void)
{
    LARGE_INTEGER timeout = { -10000 };
    int ids[] = { 1, 2, 3 };
    reset();
    objects[1].mutex.msync_type = MSYNC_MUTEX;
    objects[1].mutex.tid = 42;
    objects[1].mutex.count = 1;
    objects[2].event.msync_type = MSYNC_AUTO_EVENT;
    objects[2].event.signaled = 1;
    objects[3].event.msync_type = MSYNC_AUTO_EVENT;
    objects[3].event.signaled = 1;
    objects[5].event.msync_type = MSYNC_MANUAL_EVENT;
    trigger = 3;
    fail_once = alert_on_fail = 1;
    assert(msync_wait_objs(3, ids, 0, 5, &timeout) == STATUS_USER_APC);
    assert(objects[1].mutex.tid == 42 && objects[1].mutex.count == 1);
    assert(objects[2].event.signaled == 1);
}
static void expired_pending(void)
{
    struct mutex mutex = { .tid = ~0, .msync_type = MSYNC_MUTEX };
    ULONGLONG end = 100;
    reset();
    now = 101;
    assert(msync_wait_single(1, &mutex, &end, 42) == STATUS_TIMEOUT);
}
static void *observer(void *arg)
{
    int obj = *(int *)arg;
    thread_tid = 77;
    pthread_mutex_lock(&lock);
    while (!start_observer) pthread_cond_wait(&changed, &lock);
    pthread_mutex_unlock(&lock);
    observer_result = msync_wait_objs(1, &obj, 1, 0, NULL);
    pthread_mutex_lock(&lock);
    observer_done = 1;
    pthread_cond_broadcast(&changed);
    pthread_mutex_unlock(&lock);
    return NULL;
}
static void restored_resource_wakes(int type)
{
    pthread_t thread;
    int ids[] = { 1, 2 }, target = 1;
    reset();
    objects[1].event.msync_type = type;
    objects[1].event.signaled = type == MSYNC_MUTEX ? 0 : 1;
    objects[2].event.msync_type = MSYNC_AUTO_EVENT;
    objects[2].event.signaled = 1;
    watched = 1;
    trigger = 2;
    fail_once = 1;
    assert(!pthread_create(&thread, NULL, observer, &target));
    assert(wait_all(ids, 2) == STATUS_TIMEOUT);
    assert(observer_waiting);
    assert(!pthread_join(thread, NULL));
    assert(observer_result == STATUS_SUCCESS && observer_done);
    if (type == MSYNC_MUTEX)
        assert(objects[1].mutex.tid == 77 && objects[1].mutex.count == 1);
    else
        assert(objects[1].event.signaled == 0);
}
int main(void)
{
#ifdef MSYNC_WAKE_ONLY
    restored_resource_wakes(MSYNC_MUTEX);
    puts("MSync WaitAll: restored mutex waiter woke after rollback");
#else
    abandoned_and_success();
    rollback_owned_and_abandoned();
    alertable_rollback();
    expired_pending();
    restored_resource_wakes(MSYNC_MUTEX);
    restored_resource_wakes(MSYNC_SEMAPHORE);
    restored_resource_wakes(MSYNC_AUTO_EVENT);
    puts("MSync WaitAll: abandoned, recursive, rollback, deadline and three waiter wakeups passed");
#endif
    return 0;
}
'''

with tempfile.TemporaryDirectory(prefix="msync-waitall-") as tmp:
    source = pathlib.Path(tmp) / "msync-waitall.c"
    binary = pathlib.Path(tmp) / "msync-waitall"
    source.write_text(HARNESS + WAIT_SINGLE + DO_SINGLE + WAIT_OBJS + CHECKS)
    flags = ["-DMSYNC_WAKE_ONLY", "-Wno-unused-function"] if os.environ.get("MSYNC_WAKE_ONLY") else []
    subprocess.run(["/usr/bin/clang", "-std=gnu11", "-O1", "-g", "-Wall", "-Werror",
                    "-pthread", *flags, str(source), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=10)
