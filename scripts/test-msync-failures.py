#!/usr/bin/env python3
"""Compile the production msync bodies against real Mach APIs and check that
failures fail closed instead of crashing or flooding:

- wineserver get_shm() maps its shared page at a kernel-chosen address even
  when the stack slot starts as garbage (Clang pattern auto-init reproduces
  the uninitialized hint that turned VM_FLAGS_ANYWHERE into KERN_NO_SPACE);
- a real mach_vm_map() failure exits through fatal_error() instead of
  memset()ing an invalid address;
- ntdll's register/remove wait sends terminate the thread when the server
  port is dead, instead of returning STATUS_PENDING into an endless retry,
  while healthy sends still reach the server port intact.
"""

import pathlib
import platform
import signal
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER_SOURCE = (ROOT / "server/msync.c").read_text()
REQUEST_SOURCE = (ROOT / "server/request.c").read_text()
CLIENT_SOURCE = (ROOT / "dlls/ntdll/unix/msync.c").read_text()


def section(source, start, end):
    begin = source.index(start)
    return source[begin:source.index(end, begin)]


def architectures():
    arches = ["arm64"] if platform.machine() == "arm64" else ["x86_64"]
    if arches == ["arm64"] and subprocess.run(("arch", "-x86_64", "/usr/bin/true"),
                                              capture_output=True).returncode == 0:
        arches.append("x86_64")
    return arches


SERVER_PROLOGUE = r'''
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <mach/mach_error.h>
#include <mach/vm_page_size.h>

#define max(a,b) (((a) > (b)) ? (a) : (b))

void fatal_error( const char *err, ... ) __attribute__((noreturn, format(printf, 1, 2)));

static long pagesize;
static void **shm_addrs;
static int shm_addrs_size;
static int debug_level;
'''

SERVER_EPILOGUE = r'''
static void fail( const char *what )
{
    printf( "returned: %s\n", what );
    exit( 3 );
}

static void check_region( void *addr )
{
    mach_vm_address_t address = (mach_vm_address_t)addr;
    mach_vm_size_t size;
    vm_region_basic_info_data_64_t info;
    mach_msg_type_number_t count = VM_REGION_BASIC_INFO_COUNT_64;
    mach_port_t object;

    if (mach_vm_region( mach_task_self(), &address, &size, VM_REGION_BASIC_INFO_64,
                        (vm_region_info_t)&info, &count, &object ) != KERN_SUCCESS)
        fail( "no region" );
    if (address > (mach_vm_address_t)addr || address + size < (mach_vm_address_t)addr + pagesize)
        fail( "page not fully mapped" );
    if (info.inheritance != VM_INHERIT_SHARE) fail( "page not shared on inherit" );
    if ((info.protection & VM_PROT_DEFAULT) != VM_PROT_DEFAULT) fail( "page not read/write" );
}

int main( int argc, char **argv )
{
    shm_addrs = calloc( 128, sizeof(shm_addrs[0]) );
    shm_addrs_size = 128;

    if (!strcmp( argv[1], "map" ))
    {
        char *first, *second;
        unsigned int per_page;

        pagesize = (long)vm_kernel_page_size;
        per_page = pagesize / 16;

        first = get_shm( 0 );
        check_region( first );
        if (get_shm( 1 ) != first + 16) fail( "object 1 not 16 bytes into entry 0" );
        if (*(int *)(first + 16)) fail( "fresh page not zeroed" );
        *(int *)(first + 16) = 0x1234;
        if (get_shm( 0 ) != first) fail( "entry 0 remapped" );
        if (*(int *)get_shm( 1 ) != 0x1234) fail( "entry 0 lost state" );

        second = get_shm( per_page + 1 );
        if (second - 16 == first) fail( "entry 1 not a separate page" );
        check_region( second - 16 );
        if (shm_addrs[0] != first || shm_addrs[1] != second - 16) fail( "cache mismatch" );
        return 0;
    }

    /* A size the kernel must reject: exercises the real mach_vm_map() failure path. */
    pagesize = (long)1 << 62;
    get_shm( 0 );
    fail( "get_shm after failed mapping" );
}
'''

CLIENT_PROLOGUE = r'''
#include <AvailabilityMacros.h>
#include <dlfcn.h>
#include <errno.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>
#include <mach/mach.h>
#include <mach/mach_error.h>

#include "ntstatus.h"
#define WIN32_NO_STATUS
#include "windef.h"
#include "winternl.h"
#include "msync.h"

#define ERR(...) fprintf( stderr, __VA_ARGS__ )

/* ntdll's abort_thread() for the last thread: _exit() with the status. */
static void __attribute__((noreturn)) abort_thread( int status )
{
    fflush( stderr );
    _exit( status );
}

static NTSTATUS test_query_system_time( LARGE_INTEGER *time )
{
    struct timespec now;
    clock_gettime( CLOCK_REALTIME, &now );
    time->QuadPart = now.tv_sec * (ULONGLONG)10000000 + now.tv_nsec / 100 + 116444736000000000ull;
    return STATUS_SUCCESS;
}
#define NtQuerySystemTime test_query_system_time
'''

CLIENT_EPILOGUE = r'''
#undef NtQuerySystemTime

enum { OBJ = 42, TID = 7 };

typedef struct
{
    mach_msg_header_t header;
    unsigned int shm_idx[MAXIMUM_WAIT_OBJECTS + 1];
    mach_msg_trailer_t trailer;
} received_t;

static mach_port_t receive_right;
static int kill_port_after_register;

static void fail( const char *what, NTSTATUS status, void *shm )
{
    printf( "returned: %s status=%#x multiple_waiters=%d\n", what, (unsigned)status,
            ((struct event *)shm)->multiple_waiters );
    exit( 3 );
}

static void receive( received_t *msg )
{
    mach_msg_return_t mr = mach_msg( &msg->header, MACH_RCV_MSG, 0, sizeof(*msg), receive_right,
                                     MACH_MSG_TIMEOUT_NONE, MACH_PORT_NULL );
    if (mr != MACH_MSG_SUCCESS) { printf( "receive failed %#x\n", mr ); exit( 4 ); }
}

static void destroy_receive_right(void)
{
    mach_port_mod_refs( mach_task_self(), receive_right, MACH_PORT_RIGHT_RECEIVE, -1 );
}

/* Plays the wineserver pump's part: consume the registration, then release the spin. */
static void *server_thread( void *arg )
{
    received_t msg;

    receive( &msg );
    if (msg.header.msgh_id != ((TID << 8) | 1) || msg.shm_idx[0] != OBJ)
    {
        printf( "bad register message id=%#x idx=%#x\n", msg.header.msgh_id, msg.shm_idx[0] );
        exit( 4 );
    }
    if (kill_port_after_register) destroy_receive_right();
    __atomic_store_n( shm_tid_map + TID, 1, __ATOMIC_RELEASE );
    return NULL;
}

int main( int argc, char **argv )
{
    const char *scenario = argv[1];
    int objs[1] = { OBJ };
    void *objs_shm[1];
    struct event *event;
    ULONGLONG end = 0;  /* already expired: the timed path never blocks */
    pthread_t thread;
    received_t msg;
    NTSTATUS status;

    mach_msg2_trap = (mach_msg2_trap_ptr_t)dlsym( RTLD_DEFAULT, "mach_msg2_trap" );
    shm_tid_map = mmap( NULL, 4096, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_ANON, -1, 0 );
    event = mmap( NULL, 4096, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_ANON, -1, 0 );
    event->msync_type = MSYNC_AUTO_EVENT;
    event->refcount = 1;
    objs_shm[0] = event;

    mach_port_allocate( mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &receive_right );
    mach_port_insert_right( mach_task_self(), receive_right, receive_right, MACH_MSG_TYPE_MAKE_SEND );
    server_port = receive_right;

    if (!strcmp( scenario, "register-dead" ))
    {
        /* wineserver went away: our send right is now a dead name */
        destroy_receive_right();
        status = msync_wait_multiple( objs, objs_shm, 0, NULL, 1, &end, TID );
        fail( "register send failure", status, event );
    }

    if (!strcmp( scenario, "healthy-contention" ))
    {
        event->signaled = 1;
        status = msync_wait_multiple( objs, objs_shm, 0, NULL, 1, &end, TID );
        if (status != STATUS_PENDING) fail( "contention status", status, event );
        if (event->multiple_waiters) fail( "waiter count leaked", status, event );
        receive( &msg );
        if (msg.header.msgh_id != ((TID << 8) | 1) || msg.shm_idx[0] != OBJ)
            fail( "register message corrupted", status, event );
        return 0;
    }

    kill_port_after_register = !strcmp( scenario, "remove-dead" );
    pthread_create( &thread, NULL, server_thread, NULL );
    status = msync_wait_multiple( objs, objs_shm, 0, NULL, 1, &end, TID );
    pthread_join( thread, NULL );
    if (kill_port_after_register) fail( "remove send failure", status, event );

    if (status != STATUS_TIMEOUT) fail( "timeout status", status, event );
    if (event->multiple_waiters) fail( "waiter count leaked", status, event );
    receive( &msg );
    if (msg.header.msgh_id != ((TID << 8) | 1) || msg.shm_idx[0] != (OBJ | (1u << 29)))
        fail( "remove message corrupted", status, event );
    return 0;
}
'''


def server_program():
    return "\n".join((
        SERVER_PROLOGUE,
        section(SERVER_SOURCE, "#define MACH_CHECK_ERROR", "/* Private API"),
        section(REQUEST_SOURCE, "void fatal_error( const char *err, ... )\n", "/* allocate the reply data */"),
        section(SERVER_SOURCE, "static void *get_shm( unsigned int idx )\n{", "static unsigned int msync_alloc_shm("),
        SERVER_EPILOGUE,
    ))


def client_program():
    return "\n".join((
        CLIENT_PROLOGUE,
        section(CLIENT_SOURCE, "static LONGLONG update_timeout(", "\nint do_msync(void)"),
        CLIENT_EPILOGUE,
    ))


def build_and_run(code, arch, scenario, directory):
    source = pathlib.Path(directory) / f"msync-{arch}.c"
    binary = pathlib.Path(directory) / f"msync-{arch}"
    if not binary.exists():
        source.write_text(code)
        # Pattern auto-init makes every uninitialized stack slot deterministic garbage.
        subprocess.run(["clang", "-arch", arch, "-std=gnu11", "-Wall", "-Werror",
                        "-Wno-unused-function", "-Wno-deprecated-declarations",
                        "-ftrivial-auto-var-init=pattern",
                        "-I", str(ROOT / "include"), "-I", str(ROOT / "dlls/ntdll/unix"),
                        str(source), "-o", str(binary)], check=True)
    return subprocess.run(("arch", f"-{arch}", str(binary), scenario),
                          capture_output=True, text=True, timeout=30)


class MsyncFailureTest(unittest.TestCase):
    def run_scenarios(self, code, expectations):
        with tempfile.TemporaryDirectory() as directory:
            for arch in architectures():
                for scenario, expected in expectations.items():
                    with self.subTest(arch=arch, scenario=scenario):
                        result = build_and_run(code, arch, scenario, directory)
                        detail = f"{result.stdout}{result.stderr}"
                        if result.returncode < 0:
                            detail = f"killed by {signal.Signals(-result.returncode).name}\n{detail}"
                        self.assertEqual(result.returncode, expected, detail)

    def test_server_shared_page_mapping(self):
        # map: garbage hint must not matter; map-fail: fatal exit 1, never SIGSEGV.
        self.run_scenarios(server_program(), {"map": 0, "map-fail": 1})

    def test_client_wait_registration_sends(self):
        # Dead server port: abort_thread(1) instead of returning into a retry loop.
        self.run_scenarios(client_program(), {
            "register-dead": 1,
            "remove-dead": 1,
            "healthy-contention": 0,
            "healthy-timeout": 0,
        })


if __name__ == "__main__":
    unittest.main()
