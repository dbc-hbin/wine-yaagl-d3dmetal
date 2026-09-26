#!/usr/bin/env python3
"""Compile the production MSync Mach message pump together with the production
client close sender and exercise the public header-only close and the
size-qualified dispatch that serves it.

The generated C harness links the VERBATIM production bodies from
server/msync.c (message pump, internal reference-retirement sender,
registration, unregister, signal and reference bookkeeping) and the client
close sender from dlls/ntdll/unix/msync.c against native Mach headers.  Only
the kernel send/receive boundary and the shared-memory/tid-map supplies are
scripted, so every assertion below observes real consumer effects: reference
counts, free-list state, wait-list linkage, wait-state publication and wakeups.

Close and signal share one public header-only message: the low 28 bits of
msgh_id are the shared index and bit 28 (MSYNC_SHM_CLOSE_FLAG) selects close
over signal, so only a message whose msgh_size is exactly
sizeof(mach_msg_header_t) may be dispatched that way.  A larger message
carrying the same bits is a wait message, which is what keeps a high thread id
(id >= 1 << 20 sets bit 28) from being read as a close.  There is no cookie, no
dedicated close message id and no owned export.

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
CLIENT_SOURCE = (ROOT / "dlls" / "ntdll" / "unix" / "msync.c").read_text()


def section(text, start, end):
    begin = text.index(start)
    return text[begin:text.index(end, begin)]


# Verbatim production bodies: wire constants/ids, message structs, wait-state
# bookkeeping, signal/close internals, the internal retirement sender and the
# message pump.
CORE = section(SERVER_SOURCE, "#define UL_COMPARE_AND_WAIT_SHARED", "\nint do_msync(void)")

# Verbatim production client sender of the public header-only close.  Wine
# logging collapses to nothing and the harness supplies the server port name.
CLIENT_BRIDGE = r'''
#define TRACE(...) do { } while (0)
#define ERR(...) do { } while (0)
static mach_port_name_t server_port;
'''

CLIENT_CLOSE = section(CLIENT_SOURCE, "void msync_close( int obj )", "void msync_init(void)")

PROLOGUE = r'''
#include <assert.h>
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
    server_port = receive_port;
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

/* The public close sender from dlls/ntdll/unix/msync.c. */
static struct wire_op capture_client_close( int obj )
{
    struct wire_op op;
    int before = sent_len;

    msync_close( obj );
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

static void case_client_close_header_only(void)
{
    const unsigned int close_idx = MSYNC_SHM_INDEX_MASK & ~0xfu; /* 0x0ffffff0 */
    const unsigned int neighbour = close_idx + 1;
    unsigned int token = make_token( 0x60 );
    struct wait_registration *waiter;
    struct wire_op close;

    setup_tid_map();
    prime_slot( close_idx, 2 );
    prime_slot( neighbour, 2 );
    /* A planted waiter on the closed index: retiring a reference must never
     * complete or wake a wait. */
    waiter = plant_waiter( close_idx, 50, token );

    close = capture_client_close( close_idx );
    assert(close.len == sizeof(mach_msg_header_t)); /* public header-only close */

    queue_wire( close.bytes, close.len );
    run_pump();
    assert(fake_shm[close_idx].refcount == 1); /* exactly one reference released */
    assert(fake_shm[neighbour].refcount == 2); /* and only for the named index */
    assert(free_shm_idx == UINT32_MAX);        /* still referenced, not recycled */
    assert(shm_access_len == 1 && shm_access[0] == close_idx);
    assert(wake_len == 0);                     /* a close never mutates wait state */
    expect_waiter_untouched( waiter, close_idx, 50, token );
    assert(!find_registration( message_id_of( &close ) >> 8 )); /* not a wait message */

    close = capture_client_close( close_idx );
    queue_wire( close.bytes, close.len );
    run_pump();
    assert(fake_shm[close_idx].refcount == 0 && fake_shm[neighbour].refcount == 2);
    assert(fake_shm[close_idx].msync_type == 0);
    assert(free_shm_idx == close_idx && fake_shm[close_idx].low == -1); /* recycled once */
    assert(shm_access_len == 2 && shm_access[1] == close_idx);
    assert(wake_len == 0);
    puts("ok client_close_header_only");
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
    assert(close.len == sizeof(mach_msg_header_t)); /* same public header-only close */

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
    else if (!strcmp( argv[1], "client_close_header_only" )) case_client_close_header_only();
    else if (!strcmp( argv[1], "server_retire_close" )) case_server_retire_close();
    else if (!strcmp( argv[1], "close_size_qualified" )) case_close_size_qualified();
    else return 2;
    return 0;
}
'''

HARNESS = PROLOGUE + CORE + CLIENT_BRIDGE + CLIENT_CLOSE + EPILOGUE


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

    def test_client_header_only_close_releases_named_index_once(self):
        self.run_case("client_close_header_only")

    def test_internal_retire_close_releases_named_index_once(self):
        self.run_case("server_retire_close")

    def test_close_flag_requires_header_only_size(self):
        self.run_case("close_size_qualified")


if __name__ == "__main__":
    unittest.main()
