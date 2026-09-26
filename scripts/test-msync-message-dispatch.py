#!/usr/bin/env python3
"""Compile the production MSync Mach message pump and exercise internal
reference retirement and size-qualified dispatch.

The generated C harness links the VERBATIM production bodies from
server/msync.c (message pump, internal reference-retirement sender,
registration, unregister, signal and reference bookkeeping) against native
Mach headers. Only the kernel send/receive boundary and shared-memory/tid-map supplies are
scripted, so every assertion below observes real consumer effects: reference
counts, free-list state, wait-list linkage, wait-state publication and wakeups.

Internal close and signal share one header-only message: the low 28 bits of
msgh_id are the shared index and bit 28 (MSYNC_SHM_CLOSE_FLAG) selects close
over signal, so only a message whose msgh_size is exactly
sizeof(mach_msg_header_t) may be dispatched that way.  A larger message
carrying the same bits is a wait message, which is what keeps a high thread id
(id >= 1 << 20 sets bit 28) from being read as a close.  Exported references are retired through process-owned server requests.

No Wine install, no wineserver, no game, no sleeps.
"""

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER_SOURCE = (ROOT / "server" / "msync.c").read_text()
INPROC_SOURCE = (ROOT / "server" / "inproc_sync.c").read_text()
PROCESS_SOURCE = (ROOT / "server" / "process.c").read_text()
CLIENT_SYNC_SOURCE = (ROOT / "dlls" / "ntdll" / "unix" / "sync.c").read_text()


def section(text, start, end):
    begin = text.index(start)
    return text[begin:text.index(end, begin)]


# Verbatim production bodies: wire constants/ids, message structs, wait-state
# bookkeeping, signal/close internals, the internal retirement sender and the
# message pump.
CORE = section(SERVER_SOURCE, "#define UL_COMPARE_AND_WAIT_SHARED", "\nint do_msync(void)")


PROLOGUE = r'''
#include <assert.h>
#include <errno.h>
#include <limits.h>
#include <pthread.h>
#include <signal.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <mach/mach_init.h>
#include <mach/mach_port.h>
#include <mach/mach_vm.h>
#include <mach/mach_error.h>
#include <mach/message.h>
#include <mach/port.h>
#include <mach/semaphore.h>
#include <mach/task.h>
#include <mach/thread_act.h>
#include <mach/vm_map.h>
#include <mach/vm_page_size.h>
#include <servers/bootstrap.h>

#include "wine/msync.h"
#include "wine/list.h"

#define MAXIMUM_WAIT_OBJECTS 64
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define max(a, b) ((a) > (b) ? (a) : (b))

/* Kernel boundary capture: mach_msg is renamed so every send performed by the
 * production bodies lands in the harness recorder and every receive is
 * answered from the queued wire script. */
static mach_msg_return_t harness_mach_msg( mach_msg_header_t *msg, mach_msg_option_t option,
    mach_msg_size_t send_size, mach_msg_size_t rcv_size, mach_port_name_t rcv_name,
    mach_msg_timeout_t timeout, mach_port_name_t notify );
#define mach_msg harness_mach_msg

/* Wakeups land in the shared tid map; route them through a recording stub. */
#define __ulock_wake harness_ulock_wake

#define MAX_WIRE 320
#define MAX_SCRIPT 16
struct wire_op
{
    mach_msg_size_t len;
    unsigned char bytes[MAX_WIRE];
};
static struct wire_op script[MAX_SCRIPT], sent[32];
static int script_len, script_pos, sent_len;
'''

EPILOGUE = r'''
/* The recorded shared-object supply spans the whole 28-bit index space, so a
 * close of a high index resolves without touching more than a page. */
#define FAKE_SHM_BYTES ((size_t)MSYNC_SHM_INDEX_COUNT * sizeof(struct msync_shm))
static struct msync_shm *fake_shm;
static unsigned int shm_access[512];
static int shm_access_len;
static int *wake_addr[128];
static int wake_len;

static void *get_shm( unsigned int idx )
{
    /* The only index supplier the production paths touch.  Every resolution is
     * recorded, so "nothing was released" and "only the named index was
     * touched" are observable. */
    (void)next_unused_shm_idx;
    assert(idx < MSYNC_SHM_INDEX_COUNT);
    assert(shm_access_len < (int)ARRAY_SIZE(shm_access));
    shm_access[shm_access_len++] = idx;
    return &fake_shm[idx];
}

static int resolved_index( unsigned int idx )
{
    int i;

    for (i = 0; i < shm_access_len; i++)
        if (shm_access[i] == idx) return 1;
    return 0;
}

int harness_ulock_wake( uint32_t operation, void *addr, uint64_t wake_value )
{
    (void)operation;
    (void)wake_value;
    /* Wait-state publication wakes the shared slot of the tid it published. */
    assert(shm_tid_map);
    assert((int *)addr >= shm_tid_map && (int *)addr < shm_tid_map + (1u << 24));
    assert(wake_len < (int)ARRAY_SIZE(wake_addr));
    wake_addr[wake_len++] = (int *)addr;
    return 0;
}

static mach_msg_return_t harness_mach_msg( mach_msg_header_t *msg, mach_msg_option_t option,
    mach_msg_size_t send_size, mach_msg_size_t rcv_size, mach_port_name_t rcv_name,
    mach_msg_timeout_t timeout, mach_port_name_t notify )
{
    if (option & MACH_RCV_MSG)
    {
        struct wire_op *op;

        if (script_pos == script_len) pthread_exit( NULL ); /* script drained */
        op = &script[script_pos++];
        assert(rcv_name == receive_port);
        if (op->len > rcv_size) return MACH_RCV_TOO_LARGE;
        /* Emulate the kernel receive boundary: copy the wire bytes, relocate
         * the ports (a client's reply port lands remote, the receiving port
         * lands local) and append the trailer.  Stale bytes beyond the wire
         * size survive, as with the real kernel. */
        memcpy( msg, op->bytes, op->len );
        msg->msgh_remote_port = ((mach_msg_header_t *)op->bytes)->msgh_local_port;
        msg->msgh_local_port = rcv_name;
        if (op->len + sizeof(mach_msg_trailer_t) <= rcv_size)
        {
            mach_msg_trailer_t trailer = { MACH_MSG_TRAILER_FORMAT_0, sizeof(trailer) };
            memcpy( (char *)msg + op->len, &trailer, sizeof(trailer) );
        }
        return MACH_MSG_SUCCESS;
    }

    assert(option & MACH_SEND_MSG);
    assert(msg->msgh_remote_port == receive_port); /* clients send to the public port */
    assert(send_size <= MAX_WIRE);
    assert(sent_len < (int)ARRAY_SIZE(sent));
    sent[sent_len].len = send_size;
    memcpy( sent[sent_len].bytes, msg, send_size );
    sent_len++;
    return MACH_MSG_SUCCESS;
}

static mach_port_t make_receive_port(void)
{
    mach_port_limits_t limits = { .mpl_qlimit = 16 };
    mach_port_t port = MACH_PORT_NULL;

    assert(mach_port_allocate( mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &port ) == KERN_SUCCESS);
    assert(mach_port_insert_right( mach_task_self(), port, port, MACH_MSG_TYPE_MAKE_SEND ) == KERN_SUCCESS);
    assert(mach_port_set_attributes( mach_task_self(), port, MACH_PORT_LIMITS_INFO,
                                     (mach_port_info_t)&limits, MACH_PORT_LIMITS_INFO_COUNT ) == KERN_SUCCESS);
    return port;
}

static void setup_tid_map(void)
{
    size_t wait_table_size = (size_t)MSYNC_SHM_INDEX_COUNT * sizeof(*wait_lists);

    shm_tid_map = calloc( 1u << 24, sizeof(int) );
    assert(shm_tid_map);
    receive_port = make_receive_port();
    /* Sparse virtual tables covering every 28-bit index, so even the huge
     * fallthrough index of a rejected close resolves to a wait list without
     * touching more than a page of memory. */
    wait_lists = mmap( NULL, wait_table_size, PROT_READ | PROT_WRITE,
                       MAP_PRIVATE | MAP_ANON | MAP_NORESERVE, -1, 0 );
    assert(wait_lists != MAP_FAILED);
    wait_lists_size = MSYNC_SHM_INDEX_COUNT;
    fake_shm = mmap( NULL, FAKE_SHM_BYTES, PROT_READ | PROT_WRITE,
                     MAP_PRIVATE | MAP_ANON | MAP_NORESERVE, -1, 0 );
    assert(fake_shm != MAP_FAILED);
}

static void prime_slot( unsigned int idx, unsigned int refs )
{
    assert(idx < MSYNC_SHM_INDEX_COUNT);
    fake_shm[idx].refcount = refs;
    fake_shm[idx].msync_type = 3;
    fake_shm[idx].low = 0;
    fake_shm[idx].multiple_waiters = 0;
}

static void queue_wire( const void *bytes, mach_msg_size_t len )
{
    assert(script_len < MAX_SCRIPT && len <= MAX_WIRE);
    script[script_len].len = len;
    memcpy( script[script_len].bytes, bytes, len );
    script_len++;
}

static void run_pump(void)
{
    pthread_t thread;

    script_pos = 0;
    assert(!pthread_create( &thread, NULL, mach_message_pump, NULL ));
    assert(!pthread_join( thread, NULL ));
    assert(script_pos == script_len);
    script_len = 0;
}

/* The internal retirement sender: the server retires its own reference when an
 * msync object is destroyed. */
static struct wire_op capture_destroy( unsigned int shm_idx )
{
    struct wire_op op;
    int before = sent_len;

    assert(destroy_all( shm_idx ) == MACH_MSG_SUCCESS);
    assert(sent_len == before + 1);
    op = sent[sent_len - 1];
    return op;
}

static unsigned int message_id_of( const struct wire_op *op )
{
    unsigned int id;

    assert(op->len >= offsetof(mach_msg_header_t, msgh_id) + sizeof(id));
    memcpy( &id, op->bytes + offsetof(mach_msg_header_t, msgh_id), sizeof(id) );
    return id;
}

/* Wire fixtures for registration/unregister/signal mirror the client format of
 * dlls/ntdll/unix/msync.c server_register_wait/server_unregister_wait; the
 * dispatch algorithms under test are the verbatim production bodies above. */
static void queue_register( unsigned int tid, unsigned int count, unsigned int token,
                            const unsigned int *idxs )
{
    mach_register_message_t message = {{0}};
    unsigned int i;

    message.header.msgh_bits = MACH_MSGH_BITS_REMOTE(MACH_MSG_TYPE_COPY_SEND);
    message.header.msgh_id = (tid << 8) | count;
    message.header.msgh_size = sizeof(mach_msg_header_t) + (count + 1) * sizeof(unsigned int);
    message.header.msgh_remote_port = 1; /* sent to the server port */
    message.shm_idx[0] = token;
    for (i = 0; i < count; i++) message.shm_idx[i + 1] = idxs[i];
    queue_wire( &message, message.header.msgh_size );
}

static void queue_unregister( unsigned int tid, unsigned int count, unsigned int token )
{
    mach_register_message_t message = {{0}};

    message.header.msgh_bits = MACH_MSGH_BITS_REMOTE(MACH_MSG_TYPE_COPY_SEND);
    message.header.msgh_id = (tid << 8) | count; /* same id as the registration */
    message.header.msgh_size = sizeof(mach_msg_header_t) + sizeof(unsigned int);
    message.header.msgh_remote_port = 1;
    message.shm_idx[0] = token;
    queue_wire( &message, message.header.msgh_size );
}

static void queue_signal( unsigned int shm_idx )
{
    mach_msg_header_t header = {0};

    header.msgh_bits = MACH_MSGH_BITS_REMOTE(MACH_MSG_TYPE_COPY_SEND);
    header.msgh_id = shm_idx; /* header-only without the close flag */
    header.msgh_size = sizeof(header);
    header.msgh_remote_port = 1;
    queue_wire( &header, header.msgh_size );
}

static int last_wake_is( unsigned int tid )
{
    return wake_len > 0 && wake_addr[wake_len - 1] == &shm_tid_map[tid];
}

static int wakes_include( unsigned int tid )
{
    int i;

    for (i = 0; i < wake_len; i++)
        if (wake_addr[i] == &shm_tid_map[tid]) return 1;
    return 0;
}

/* A planted wait node makes "did this message touch wait state?" observable
 * without a requester: only a signal or a wait dispatch completes it. */
static struct wait_registration *plant_waiter( unsigned int shm_idx, unsigned int tid,
                                               unsigned int token )
{
    struct wait_list *list = get_wait_list( shm_idx, 1 );
    struct wait_registration *registration = calloc( 1, sizeof(*registration) +
                                                     sizeof(registration->nodes[0]) );

    assert(list && registration);
    registration->tid = tid;
    registration->token = token;
    registration->capacity = 1;
    registration->nodes[0].shm_idx = shm_idx;
    shm_tid_map[tid] = wait_token_with_state( token, MSYNC_WAIT_ARMED );
    assert(link_wait_node( registration, shm_idx ));
    return registration;
}

static void expect_waiter_untouched( struct wait_registration *registration,
                                     unsigned int shm_idx, unsigned int tid, unsigned int token )
{
    struct wait_list *list = get_wait_list( shm_idx, 0 );

    assert(list && list->head == registration->nodes);
    assert(registration->node_count == 1 && registration->nodes[0].shm_idx == shm_idx);
    assert((unsigned int)shm_tid_map[tid] == wait_token_with_state( token, MSYNC_WAIT_ARMED ));
    assert(!wakes_include( tid ));
}

static void expect_registered( unsigned int tid, unsigned int count, unsigned int token,
                               unsigned int first_idx )
{
    struct wait_registration *registration = find_registration( tid );
    unsigned int i;

    assert(registration && registration->tid == tid && registration->token == token);
    assert(registration->node_count == count);
    for (i = 0; i < count; i++)
    {
        struct wait_list *list = get_wait_list( first_idx + i, 0 );

        assert(list && list->head && list->head->registration == registration);
        assert(fake_shm[first_idx + i].refcount == 2); /* held base ref + wait ref */
        assert(fake_shm[first_idx + i].multiple_waiters == 1);
    }
    assert((unsigned int)shm_tid_map[tid] == wait_token_with_state( token, MSYNC_WAIT_ARMED ));
    assert(wakes_include( tid ));
}

static unsigned int make_token(unsigned int generation)
{
    return wait_token_with_state( generation << 3, MSYNC_WAIT_REGISTERING );
}

/* ---- cases ------------------------------------------------------------- */

static void run_single_register_case( unsigned int tid, unsigned int first_idx )
{
    unsigned int idxs[1] = { first_idx };
    unsigned int token = make_token( 0x12 );

    prime_slot( first_idx, 1 );
    shm_tid_map[tid] = token;
    queue_register( tid, 1, token, idxs );
    run_pump();
    expect_registered( tid, 1, token, first_idx );
}

static void case_register_count1(void)
{
    setup_tid_map();
    run_single_register_case( 42, 2 );        /* low tid */
    run_single_register_case( 1u << 20, 3 );  /* bit-20 tid sets bit 28 of the id */
    puts("ok register_count1");
}

static void case_high_tid_32byte(void)
{
    unsigned int idxs[1] = { 4 };
    unsigned int token = make_token( 0x12 );

    setup_tid_map();
    prime_slot( 4, 1 );
    shm_tid_map[0xffffffu] = token;
    queue_register( 0xffffffu, 1, token, idxs );
    assert(script[0].len == 32); /* exactly header + token + index */
    assert(message_id_of( &script[0] ) & MSYNC_SHM_CLOSE_FLAG); /* close flag bit is set */
    run_pump();
    /* Size decides the branch: the close flag bit alone must not retire an
     * index, so this stays a registration. */
    expect_registered( 0xffffffu, 1, token, 4 );
    assert(fake_shm[4].refcount == 2); /* a register must never release */
    assert(free_shm_idx == UINT32_MAX);
    puts("ok high_tid_32byte");
}

static void case_register_count65(void)
{
    unsigned int idxs[65];
    unsigned int i;

    setup_tid_map();
    for (i = 0; i < 3; i++)
    {
        static const unsigned int tids[3] = { 42, 1u << 20, 0xffffffu };
        unsigned int first = 2 + i * 68;
        unsigned int token = make_token( 0x20 + i );
        unsigned int j;

        for (j = 0; j < 65; j++)
        {
            idxs[j] = first + j;
            prime_slot( idxs[j], 1 );
        }
        shm_tid_map[tids[i]] = token;
        queue_register( tids[i], 65, token, idxs );
        run_pump();
        expect_registered( tids[i], 65, token, first );
    }
    puts("ok register_count65");
}

static void run_unregister_case( unsigned int tid )
{
    unsigned int idxs[2] = { 20, 21 };
    unsigned int token = make_token( 0x33 );

    prime_slot( 20, 1 );
    prime_slot( 21, 1 );
    shm_tid_map[tid] = token;
    queue_register( tid, 2, token, idxs );
    run_pump();
    expect_registered( tid, 2, token, 20 );

    queue_signal( 20 );
    run_pump();
    assert((unsigned int)shm_tid_map[tid] == wait_token_with_state( token, MSYNC_WAIT_WOKEN ));

    queue_unregister( tid, 2, token ); /* same msgh_id as the registration */
    run_pump();
    assert(find_registration( tid ) == NULL);
    assert(!get_wait_list( 20, 0 ) || !get_wait_list( 20, 0 )->head);
    assert(!get_wait_list( 21, 0 ) || !get_wait_list( 21, 0 )->head);
    assert(fake_shm[20].refcount == 1 && fake_shm[21].refcount == 1);
    assert(fake_shm[20].multiple_waiters == 0 && fake_shm[21].multiple_waiters == 0);
    assert((unsigned int)shm_tid_map[tid] == wait_token_with_state( token, MSYNC_WAIT_IDLE ));
    assert(last_wake_is( tid ));
}

static void case_unregister_same_id(void)
{
    setup_tid_map();
    run_unregister_case( 77 );
    run_unregister_case( 1u << 20 );
    run_unregister_case( 0xffffffu );
    puts("ok unregister_same_id");
}

static void case_signal_header_only(void)
{
    unsigned int idx_fd[1] = { 253 }; /* low byte 0xfd must stay a plain index */
    unsigned int idx_plain[1] = { 7 };
    unsigned int token_fd = make_token( 0x41 ), token_plain = make_token( 0x42 );

    setup_tid_map();
    prime_slot( 253, 1 );
    prime_slot( 7, 1 );
    shm_tid_map[43] = token_fd;
    shm_tid_map[42] = token_plain;
    queue_register( 43, 1, token_fd, idx_fd );
    queue_register( 42, 1, token_plain, idx_plain );
    run_pump();
    expect_registered( 43, 1, token_fd, 253 );
    expect_registered( 42, 1, token_plain, 7 );

    queue_signal( 253 );
    run_pump();
    assert(fake_shm[253].refcount == 2); /* signaled, never released */
    assert(!get_wait_list( 253, 0 ) || !get_wait_list( 253, 0 )->head);
    assert((unsigned int)shm_tid_map[43] == wait_token_with_state( token_fd, MSYNC_WAIT_WOKEN ));
    assert(last_wake_is( 43 ));

    queue_signal( 7 );
    run_pump();
    assert(fake_shm[7].refcount == 2);
    assert(!get_wait_list( 7, 0 ) || !get_wait_list( 7, 0 )->head);
    assert((unsigned int)shm_tid_map[42] == wait_token_with_state( token_plain, MSYNC_WAIT_WOKEN ));
    assert(last_wake_is( 42 ));
    puts("ok signal_header_only");
}

static void case_server_retire_close(void)
{
    unsigned int token = make_token( 0x61 );
    struct wait_registration *waiter;
    struct wire_op close;

    setup_tid_map();
    prime_slot( 3, 2 );
    prime_slot( 4, 2 );
    waiter = plant_waiter( 4, 51, token );

    close = capture_destroy( 3 );
    assert(close.len == sizeof(mach_msg_header_t)); /* internal header-only close */

    queue_wire( close.bytes, close.len );
    run_pump();
    assert(fake_shm[3].refcount == 1); /* exactly one reference released */
    assert(fake_shm[4].refcount == 2); /* and only for the named index */
    assert(free_shm_idx == UINT32_MAX);
    assert(shm_access_len == 1 && shm_access[0] == 3);
    assert(wake_len == 0);
    expect_waiter_untouched( waiter, 4, 51, token );
    assert(!find_registration( message_id_of( &close ) >> 8 ));

    queue_wire( close.bytes, close.len );
    run_pump();
    assert(fake_shm[3].refcount == 0 && fake_shm[4].refcount == 2);
    assert(fake_shm[3].msync_type == 0);
    assert(free_shm_idx == 3 && fake_shm[3].low == -1); /* recycled exactly once */
    assert(shm_access_len == 2 && shm_access[1] == 3);
    assert(wake_len == 0);
    puts("ok server_retire_close");
}

static void case_close_size_qualified(void)
{
    /* Bit 28 of msgh_id selects close over signal, but only for a message whose
     * size is exactly a header.  A 32-byte message carrying the same bits is a
     * one-object wait registration for the thread encoded above the flag. */
    const unsigned int close_idx = (MSYNC_SHM_INDEX_MASK & ~0xffu) | 1u; /* low byte 1 */
    const unsigned int waited_idx = 5;
    unsigned int id = (close_idx & MSYNC_SHM_INDEX_MASK) | MSYNC_SHM_CLOSE_FLAG;
    unsigned int tid = id >> 8;
    unsigned int token = make_token( 0x77 );
    mach_register_message_t message = {{0}};

    setup_tid_map();
    prime_slot( close_idx, 2 );
    prime_slot( waited_idx, 1 );
    shm_tid_map[tid] = token;

    message.header.msgh_bits = MACH_MSGH_BITS_REMOTE(MACH_MSG_TYPE_COPY_SEND);
    message.header.msgh_id = id;
    message.header.msgh_size = sizeof(mach_msg_header_t) + 2 * sizeof(unsigned int);
    message.header.msgh_remote_port = 1;
    message.shm_idx[0] = token;
    message.shm_idx[1] = waited_idx;
    queue_wire( &message, message.header.msgh_size );
    run_pump();

    assert(fake_shm[close_idx].refcount == 2); /* no reference released */
    assert(fake_shm[close_idx].multiple_waiters == 0);
    assert(free_shm_idx == UINT32_MAX);
    assert(!resolved_index( close_idx ));     /* the index was never even resolved */
    expect_registered( tid, 1, token, waited_idx );
    puts("ok close_size_qualified");
}

int main(int argc, char **argv)
{
    if (argc != 2) return 1;
    if (!strcmp( argv[1], "register_count1" )) case_register_count1();
    else if (!strcmp( argv[1], "high_tid_32byte" )) case_high_tid_32byte();
    else if (!strcmp( argv[1], "register_count65" )) case_register_count65();
    else if (!strcmp( argv[1], "unregister_same_id" )) case_unregister_same_id();
    else if (!strcmp( argv[1], "signal_header_only" )) case_signal_header_only();
    else if (!strcmp( argv[1], "server_retire_close" )) case_server_retire_close();
    else if (!strcmp( argv[1], "close_size_qualified" )) case_close_size_qualified();
    else return 2;
    return 0;
}
'''

HARNESS = PROLOGUE + CORE + EPILOGUE

# Compile the actual export grant, close handler and process-death sweep along
# with the same production MSync shared-index retention and Mach message pump.
EXPORT_CORE = section(SERVER_SOURCE, "int msync_release_export(", "#else /* __APPLE__ */")
EXPORT_CORE += section(INPROC_SOURCE, "struct msync_export\n", "#else /* NTSYNC_IOC_EVENT_READ */")
EXPORT_CORE += INPROC_SOURCE[INPROC_SOURCE.index("DECL_HANDLER(close_inproc_sync_export)"):]
CLIENT_RELEASE = section(CLIENT_SYNC_SOURCE, "static void release_inproc_sync(",
                         "static struct inproc_sync *get_cached_inproc_sync(")
EXIT_CORE = section(PROCESS_SOURCE, "#ifdef __APPLE__\n/* A Wine server thread",
                    "/* start the sigkill timer for a process upon exit */")


EXPORT_SETUP = r'''
#define __int64 long long
#define STATUS_NO_MEMORY 1
#define STATUS_UNSUCCESSFUL 2
#define STATUS_INVALID_HANDLE 3
#define INPROC_SYNC_EVENT 2
#define fatal_error(...) abort()
struct msync { unsigned int shm_idx; };
struct object { const void *ops; };
struct inproc_sync { struct object obj; int type; struct msync *msync; };
struct timeout_user { int active; };
struct process {
    struct list msync_exports;
    int unix_pid;
    int msync_pid_start_valid;
    unsigned long long msync_pid_start_sec, msync_pid_start_usec;
    int msync_sigkill_sent;
    long long sigkill_delay;
    struct timeout_user *sigkill_timeout;
};
#define PROC_PIDTBSDINFO 3
#define SZOMB 5
#define TICKS_PER_SEC 10000000LL
struct proc_bsdinfo {
    unsigned int pbi_pid, pbi_status;
    unsigned long long pbi_start_tvsec, pbi_start_tvusec;
};
enum probe_state { PROBE_LIVE, PROBE_ZOMBIE, PROBE_MISSING, PROBE_CHANGED, PROBE_ERROR };
static enum probe_state probe = PROBE_LIVE;
static int sigkill_count, died_count, signal_errno;
static struct timeout_user timer;
static int harness_proc_pidinfo(int pid, int flavor, unsigned long long arg, void *buffer, int size)
{
    struct proc_bsdinfo *info = buffer;
    (void)flavor; (void)arg;
    assert(size == sizeof(*info));
    if (probe == PROBE_MISSING) { errno = ESRCH; return 0; }
    if (probe == PROBE_ERROR) { errno = EIO; return -1; }
    info->pbi_pid = pid;
    info->pbi_start_tvsec = probe == PROBE_CHANGED ? 42 : 11;
    info->pbi_start_tvusec = 22;
    info->pbi_status = probe == PROBE_ZOMBIE ? SZOMB : 2;
    return size;
}
#define proc_pidinfo harness_proc_pidinfo
static int harness_kill(int pid, int signal)
{
    (void)pid;
    if (probe == PROBE_MISSING) { errno = ESRCH; return -1; }
    if (!signal) return 0;
    assert(signal == SIGKILL);
    sigkill_count++;
    if (signal_errno) { errno = signal_errno; return -1; }
    return 0;
}
#define kill harness_kill
static struct timeout_user *add_timeout_user(long long timeout, void (*cb)(void *), void *arg)
{
    assert(timeout < 0 && cb && arg);
    timer.active = 1;
    return &timer;
}
static void process_died(struct process *process);

static const int inproc_sync_ops;
static struct { struct process *process; } request_context, *current = &request_context;
static int request_error;
static int do_msync(void) { return 1; }
static void set_error(int error) { request_error = error; }
static void *mem_alloc(size_t size) { return malloc(size); }
static struct object *get_obj_sync(struct object *obj) { return obj; }
static void release_object(struct object *obj) { (void)obj; }
struct close_inproc_sync_export_request { unsigned int shm_idx; unsigned long long export_id; };
#define DECL_HANDLER(name) static void req_##name(const struct close_inproc_sync_export_request *req)
'''

EXIT_STUB = r'''
static void process_died(struct process *process)
{
    died_count++;
    release_process_msync_exports( process );
}
'''

CLIENT_SETUP = r'''
typedef int LONG;
struct client_inproc_sync { LONG refcount; int fd; unsigned long long export_id; };
static int deliver_close = 1;
static LONG InterlockedDecrement(LONG *value) { return --*value; }
static int wine_server_call(struct close_inproc_sync_export_request *req)
{
    if (!deliver_close) return STATUS_UNSUCCESSFUL; /* client lost before delivery */
    request_error = 0;
    req_close_inproc_sync_export( req );
    return request_error;
}
#define SERVER_START_REQ(name) { struct close_inproc_sync_export_request request = {0}; \
    struct close_inproc_sync_export_request *req = &request;
#define SERVER_END_REQ }
#define ERR(...) do { } while (0)
#define close(fd) abort()
#define inproc_sync client_inproc_sync
'''

EXPORT_CASES = r'''
static unsigned long long export_index(struct process *process, unsigned int idx)
{
    struct msync object = {idx};
    struct inproc_sync sync = {{&inproc_sync_ops}, INPROC_SYNC_EVENT, &object};
    unsigned long long id = 0;
    int type = 0;

    current->process = process;
    assert(get_obj_inproc_sync( &sync.obj, &type, &id ) == (int)idx);
    assert(type == INPROC_SYNC_EVENT && id);
    return id;
}

static void close_export(struct process *process, unsigned int idx, unsigned long long id, int expected)
{
    struct close_inproc_sync_export_request req = {idx, id};
    current->process = process;
    request_error = 0;
    req_close_inproc_sync_export( &req );
    assert(request_error == expected);
}

static void flush_retirements(int *cursor)
{
    int i;
    assert(*cursor < sent_len);
    for (i = *cursor; i < sent_len; i++) queue_wire( sent[i].bytes, sent[i].len );
    *cursor = sent_len;
    run_pump();
}

static void case_export_close_and_reuse(void)
{
    struct process first = {0}, other = {0};
    unsigned long long a, b, c, d, reused;
    int cursor = 0, before;

    setup_tid_map();
    list_init( &first.msync_exports );
    list_init( &other.msync_exports );
    first.unix_pid = 101; other.unix_pid = 102;
    prime_slot( 8, 1 );
    prime_slot( 9, 1 );
    a = export_index( &first, 8 ); /* two local handles for one object */
    b = export_index( &first, 8 );
    c = export_index( &other, 8 ); /* inherited/duplicated into another process */
    d = export_index( &first, 9 ); /* a distinct object in the same process */
    assert(fake_shm[8].refcount == 4 && fake_shm[9].refcount == 2);
    assert(a != b && b != c && c != d);

    close_export( &first, 8, a, 0 );
    before = sent_len;
    close_export( &first, 8, a, STATUS_INVALID_HANDLE );
    close_export( &other, 8, b, STATUS_INVALID_HANDLE );
    close_export( &first, 9, b, STATUS_INVALID_HANDLE );
    assert(sent_len == before); /* duplicates/foreign/mismatched index never retire */
    assert(destroy_all( 9 ) == MACH_MSG_SUCCESS); /* distinct object handle teardown */
    release_process_msync_exports( &first ); /* process_killed before second NtClose */
    assert(list_empty( &first.msync_exports ));
    before = sent_len;
    release_process_msync_exports( &first ); /* process_destroy fallback */
    assert(sent_len == before);
    flush_retirements( &cursor );
    assert(fake_shm[8].refcount == 2 && fake_shm[9].refcount == 0);
    assert(free_shm_idx == 9);
    close_export( &other, 8, c, 0 );
    assert(destroy_all( 8 ) == MACH_MSG_SUCCESS); /* final server object owner */
    flush_retirements( &cursor );
    assert(fake_shm[8].refcount == 0 && free_shm_idx == 8);

    /* A reused index belongs to a new export id. Old closes cannot touch it. */
    free_shm_idx = UINT32_MAX; /* pop from fake allocator's free list */
    prime_slot( 8, 1 );
    reused = export_index( &first, 8 );
    assert(reused != a && reused != b && reused != c);
    before = sent_len;
    close_export( &first, 8, a, STATUS_INVALID_HANDLE );
    close_export( &other, 8, c, STATUS_INVALID_HANDLE );
    assert(sent_len == before && fake_shm[8].refcount == 2);
    close_export( &first, 8, reused, 0 );
    assert(destroy_all( 8 ) == MACH_MSG_SUCCESS);
    flush_retirements( &cursor );
    assert(fake_shm[8].refcount == 0 && free_shm_idx == 8);
    puts("ok export_close_and_reuse");
}

static void case_export_death_during_wait(void)
{
    struct process dead = {0};
    unsigned int idx = 12, tid = 43, token = make_token( 0x56 );
    unsigned int indices[] = {12};
    unsigned long long id;
    int cursor = 0;

    setup_tid_map();
    list_init( &dead.msync_exports );
    dead.unix_pid = 103;
    prime_slot( idx, 1 );
    id = export_index( &dead, idx );
    shm_tid_map[tid] = token;
    queue_register( tid, 1, token, indices );
    run_pump();
    assert(fake_shm[idx].refcount == 3); /* base + export + concurrent wait */
    assert(destroy_all( idx ) == MACH_MSG_SUCCESS); /* handle table teardown */
    release_process_msync_exports( &dead );
    flush_retirements( &cursor );
    assert(fake_shm[idx].refcount == 1 && free_shm_idx == UINT32_MAX);
    close_export( &dead, idx, id, STATUS_INVALID_HANDLE );
    queue_signal( idx ); /* complete the outstanding wait before unregister */
    run_pump();
    queue_unregister( tid, 1, token );
    run_pump();
    assert(fake_shm[idx].refcount == 0 && free_shm_idx == idx);
    puts("ok export_death_during_wait");
}

static void case_client_last_ref_crash(void)
{
    struct process alive = {0};
    struct client_inproc_sync normal, crashed;
    unsigned long long first, second;
    int cursor = 0;

    setup_tid_map();
    list_init( &alive.msync_exports );
    alive.unix_pid = 104;
    prime_slot( 16, 1 );
    first = export_index( &alive, 16 );
    normal.refcount = 2; normal.fd = 16; normal.export_id = first;
    current->process = &alive;
    release_inproc_sync( &normal ); /* a concurrent local waiter remains */
    assert(normal.refcount == 1 && sent_len == 0);
    release_inproc_sync( &normal );
    assert(normal.refcount == 0 && sent_len == 1);

    second = export_index( &alive, 16 );
    crashed.refcount = 1; crashed.fd = 16; crashed.export_id = second;
    deliver_close = 0;
    release_inproc_sync( &crashed ); /* local ref dies before RPC reaches server */
    assert(crashed.refcount == 0 && sent_len == 1);
    release_process_msync_exports( &alive );
    assert(destroy_all( 16 ) == MACH_MSG_SUCCESS);
    flush_retirements( &cursor );
    assert(fake_shm[16].refcount == 0 && free_shm_idx == 16);
    puts("ok client_last_ref_crash");
}

static void case_live_then_zombie(void)
{
    struct process exiting = {0};
    int cursor = 0;

    setup_tid_map();
    list_init( &exiting.msync_exports );
    exiting.unix_pid = 105;
    exiting.sigkill_delay = TICKS_PER_SEC / 2;
    prime_slot( 20, 1 );
    export_index( &exiting, 20 );
    assert(exiting.msync_pid_start_valid);
    process_sigkill( &exiting ); /* SIGKILL delivered, Unix process still live */
    assert(sigkill_count == 1 && died_count == 0 && fake_shm[20].refcount == 2);
    process_sigkill( &exiting ); /* not a second SIGKILL */
    assert(sigkill_count == 1 && died_count == 0);
    probe = PROBE_ERROR;
    process_sigkill( &exiting ); /* probe failure cannot imply death */
    assert(sigkill_count == 1 && died_count == 0 && fake_shm[20].refcount == 2);
    probe = PROBE_ZOMBIE;
    process_sigkill( &exiting );
    assert(died_count == 1 && list_empty( &exiting.msync_exports ));
    flush_retirements( &cursor );
    assert(fake_shm[20].refcount == 1);
    assert(destroy_all( 20 ) == MACH_MSG_SUCCESS);
    flush_retirements( &cursor );
    assert(fake_shm[20].refcount == 0 && free_shm_idx == 20);
    puts("ok live_then_zombie");
}

static void case_missing_or_reused_pid(int changed)
{
    struct process exiting = {0};
    int cursor = 0;
    unsigned int idx = changed ? 22 : 21;

    setup_tid_map();
    list_init( &exiting.msync_exports );
    exiting.unix_pid = changed ? 107 : 106;
    exiting.sigkill_delay = TICKS_PER_SEC / 2;
    prime_slot( idx, 1 );
    export_index( &exiting, idx );
    probe = changed ? PROBE_CHANGED : PROBE_MISSING;
    process_sigkill( &exiting );
    assert(sigkill_count == 0 && died_count == 1);
    flush_retirements( &cursor );
    assert(fake_shm[idx].refcount == 1);
    assert(destroy_all( idx ) == MACH_MSG_SUCCESS);
    flush_retirements( &cursor );
    assert(fake_shm[idx].refcount == 0 && free_shm_idx == idx);
    puts(changed ? "ok changed_pid" : "ok missing_pid");
}

static void case_identity_probe_error(void)
{
    struct process exiting = {0};
    struct msync object = {23};
    struct inproc_sync sync = {{&inproc_sync_ops}, INPROC_SYNC_EVENT, &object};
    unsigned long long id = 0;
    int type = 0, cursor = 0;

    setup_tid_map();
    list_init( &exiting.msync_exports );
    exiting.unix_pid = 108;
    exiting.sigkill_delay = TICKS_PER_SEC / 2;
    prime_slot( 23, 1 );
    current->process = &exiting;
    probe = PROBE_ERROR;
    assert(get_obj_inproc_sync( &sync.obj, &type, &id ) == -1);
    assert(request_error == STATUS_UNSUCCESSFUL && list_empty( &exiting.msync_exports ));
    assert(fake_shm[23].refcount == 1);
    probe = PROBE_LIVE;
    export_index( &exiting, 23 );
    exiting.msync_pid_start_valid = 0; /* deliberately unknown identity */
    probe = PROBE_ERROR;
    process_sigkill( &exiting );
    assert(died_count == 0 && sigkill_count == 0 && fake_shm[23].refcount == 2);
    exiting.msync_pid_start_valid = 1;
    probe = PROBE_MISSING;
    process_sigkill( &exiting );
    assert(died_count == 1);
    flush_retirements( &cursor );
    assert(fake_shm[23].refcount == 1);
    puts("ok identity_probe_error");
}

static void case_signal_error_one_shot(void)
{
    struct process exiting = {0};
    int cursor = 0;

    setup_tid_map();
    list_init( &exiting.msync_exports );
    exiting.unix_pid = 109;
    exiting.sigkill_delay = TICKS_PER_SEC / 2;
    prime_slot( 24, 1 );
    export_index( &exiting, 24 );
    signal_errno = EPERM;
    process_sigkill( &exiting );
    assert(sigkill_count == 1 && exiting.msync_sigkill_sent && died_count == 0);
    signal_errno = 0;
    process_sigkill( &exiting );
    assert(sigkill_count == 1 && fake_shm[24].refcount == 2);
    probe = PROBE_MISSING;
    process_sigkill( &exiting );
    assert(died_count == 1);
    flush_retirements( &cursor );
    assert(fake_shm[24].refcount == 1);
    puts("ok signal_error_one_shot");
}

'''

EXPORT_HARNESS = HARNESS.replace("int main(int argc, char **argv)",
                                EXPORT_SETUP + EXPORT_CORE + EXIT_CORE + EXIT_STUB + CLIENT_SETUP + CLIENT_RELEASE +
                                "\n#undef inproc_sync\n" + EXPORT_CASES +
                                "int main(int argc, char **argv)")
EXPORT_HARNESS = EXPORT_HARNESS.replace(
    'if (!strcmp( argv[1], "register_count1" ))',
    'if (!strcmp( argv[1], "signal_error_one_shot" )) case_signal_error_one_shot();\n'
    '    else if (!strcmp( argv[1], "live_then_zombie" )) case_live_then_zombie();\n'
    '    else if (!strcmp( argv[1], "missing_pid" )) case_missing_or_reused_pid(0);\n'
    '    else if (!strcmp( argv[1], "changed_pid" )) case_missing_or_reused_pid(1);\n'
    '    else if (!strcmp( argv[1], "identity_probe_error" )) case_identity_probe_error();\n'
    '    else if (!strcmp( argv[1], "client_last_ref_crash" )) case_client_last_ref_crash();\n'
    '    else if (!strcmp( argv[1], "export_close_and_reuse" )) case_export_close_and_reuse();\n'
    '    else if (!strcmp( argv[1], "export_death_during_wait" )) case_export_death_during_wait();\n'
    '    else if (!strcmp( argv[1], "register_count1" ))')

class MSyncMessageDispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compiler = shutil.which("cc") or "/usr/bin/clang"
        cls.temporary = tempfile.TemporaryDirectory(prefix="msync-message-dispatch-")
        cls.root = pathlib.Path(cls.temporary.name)
        cls.source = cls.root / "msync-message-dispatch.c"
        cls.binary = cls.root / "msync-message-dispatch"
        cls.source.write_text(HARNESS)
        subprocess.run([cls.compiler, "-std=gnu11", "-O1", "-g", "-Wall", "-Werror",
                        "-Wno-unused-function", "-Wno-unused-parameter", "-Wno-pointer-sign",
                        "-pthread",
                        "-I", str(ROOT / "include"), str(cls.source), "-o", str(cls.binary)],
                       check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def run_case(self, name):
        result = subprocess.run([str(self.binary), name], capture_output=True, text=True,
                                timeout=30, env={**os.environ, "WINE_MSYNC_TEST_TRACE": ""})
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn("ok " + name, result.stdout)

    def test_register_low_and_bit20_tids_count1(self):
        self.run_case("register_count1")

    def test_same_32byte_high_tid_register(self):
        self.run_case("high_tid_32byte")

    def test_register_low_bit20_max24bit_tids_count65(self):
        self.run_case("register_count65")

    def test_unregister_same_id_retires_refs(self):
        self.run_case("unregister_same_id")

    def test_signal_header_only_stays_signal(self):
        self.run_case("signal_header_only")

    def test_internal_retire_close_releases_named_index_once(self):
        self.run_case("server_retire_close")

    def test_close_flag_requires_header_only_size(self):
        self.run_case("close_size_qualified")


class MSyncExportLifetimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="msync-export-lifetime-")
        root = pathlib.Path(cls.temporary.name)
        source = root / "msync-export-lifetime.c"
        cls.binary = root / "msync-export-lifetime"
        source.write_text(EXPORT_HARNESS)
        subprocess.run([shutil.which("cc") or "/usr/bin/clang", "-std=gnu11", "-O1", "-g",
                        "-Wall", "-Werror", "-Wno-unused-function", "-Wno-unused-parameter",
                        "-Wno-pointer-sign", "-pthread", "-I", str(ROOT / "include"),
                        str(source), "-o", str(cls.binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def run_case(self, name):
        result = subprocess.run([str(self.binary), name], capture_output=True, text=True,
                                timeout=30, env={**os.environ, "WINE_MSYNC_TEST_TRACE": ""})
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn("ok " + name, result.stdout)

    def test_normal_close_duplicate_process_and_slot_reuse(self):
        self.run_case("export_close_and_reuse")

    def test_abrupt_death_preserves_concurrent_wait(self):
        self.run_case("export_death_during_wait")

    def test_client_last_ref_crash_is_reclaimed_by_server(self):
        self.run_case("client_last_ref_crash")

    def test_live_after_sigkill_then_zombie(self):
        self.run_case("live_then_zombie")

    def test_missing_process_pid(self):
        self.run_case("missing_pid")

    def test_reused_process_pid(self):
        self.run_case("changed_pid")

    def test_identity_probe_error_does_not_release(self):
        self.run_case("identity_probe_error")

    def test_failed_signal_never_retries_reused_pid(self):
        self.run_case("signal_error_one_shot")


if __name__ == "__main__":
    unittest.main()
