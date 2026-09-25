#!/usr/bin/env python3
"""Compile the production MSync Mach message pump and close sender and exercise
the server-only close discriminator and registration/signal dispatch paths.

The generated C harness links the VERBATIM production bodies from
server/msync.c (message pump, close sender, registration/unregister, signal,
reference bookkeeping) against native Mach headers.  Only the kernel
send/receive boundary and the shared-memory/tid-map supplies are scripted,
so every assertion below observes real consumer effects: reference counts,
free-list state, wait-list linkage, wait-state publication and wakeups.

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


def section(start, end):
    begin = SERVER_SOURCE.index(start)
    return SERVER_SOURCE[begin:SERVER_SOURCE.index(end, begin)]


# Verbatim production bodies: wire constants/ids, message structs, wait-state
# bookkeeping, signal/close internals, close sender and the message pump.
CORE = section("#define UL_COMPARE_AND_WAIT_SHARED", "\nint do_msync(void)")

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

/* Kernel boundary capture.  real_mach_msg is bound before the rename so the
 * harness can forward to the real Mach kernel when a case asks for it. */
static mach_msg_return_t (*real_mach_msg)(mach_msg_header_t *, mach_msg_option_t, mach_msg_size_t,
                                          mach_msg_size_t, mach_port_name_t, mach_msg_timeout_t,
                                          mach_port_name_t) = mach_msg;

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
    int real_rcv;
    mach_msg_size_t len;
    unsigned char bytes[MAX_WIRE];
};
static struct wire_op script[MAX_SCRIPT], sent[32], captured;
static int script_len, script_pos, sent_len;
static int forward_sends;
'''

EPILOGUE = r'''
#define FAKE_SHM_SLOTS 256
static struct msync_shm fake_shm[FAKE_SHM_SLOTS];
static unsigned int shm_access[512];
static int shm_access_len;
static int *wake_addr[512];
static int wake_len;

static void *get_shm( unsigned int idx )
{
    /* The only index supplier the production paths touch: bounds-checked, and
     * every resolution is recorded so "nothing was released" is observable. */
    (void)next_unused_shm_idx;
    assert(idx < FAKE_SHM_SLOTS);
    assert(shm_access_len < (int)ARRAY_SIZE(shm_access));
    shm_access[shm_access_len++] = idx;
    return &fake_shm[idx];
}

int harness_ulock_wake( uint32_t operation, void *addr, uint64_t wake_value )
{
    (void)operation;
    (void)wake_value;
    /* A wakeup must target the shared wait-state slot of some tid. */
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
        if (op->real_rcv)
            return real_mach_msg( msg, option, send_size, rcv_size, rcv_name, timeout, notify );
        assert(rcv_name == receive_port);
        if (op->len > rcv_size) return MACH_RCV_TOO_LARGE;
        /* Emulate the kernel receive boundary: copy the wire bytes, relocate
         * the ports (reply port lands remote, receiving port lands local) and
         * append the trailer.  Stale bytes beyond the wire size survive, as
         * with the real kernel. */
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
    assert(send_size <= MAX_WIRE);
    assert(sent_len < (int)ARRAY_SIZE(sent));
    sent[sent_len].len = send_size;
    memcpy( sent[sent_len].bytes, msg, send_size );
    sent_len++;
    if (forward_sends)
        return real_mach_msg( msg, option, send_size, rcv_size, rcv_name, timeout, notify );
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
    size_t table_size = (size_t)MSYNC_SHM_INDEX_COUNT * sizeof(*wait_lists);

    shm_tid_map = calloc( 1u << 24, sizeof(int) );
    assert(shm_tid_map);
    close_cookie = 0x8899aabbccddeeffull;
    receive_port = make_receive_port();
    /* Sparse virtual table covering every 28-bit index, so even the huge
     * fallthrough index of the reserved close id resolves to a wait list
     * without touching more than a page of memory. */
    wait_lists = mmap( NULL, table_size, PROT_READ | PROT_WRITE,
                       MAP_PRIVATE | MAP_ANON | MAP_NORESERVE, -1, 0 );
    assert(wait_lists != MAP_FAILED);
    wait_lists_size = MSYNC_SHM_INDEX_COUNT;
}

static void prime_slot( unsigned int idx, unsigned int refs )
{
    assert(idx < FAKE_SHM_SLOTS);
    fake_shm[idx].refcount = refs;
    fake_shm[idx].msync_type = 3;
    fake_shm[idx].low = 0;
    fake_shm[idx].multiple_waiters = 0;
}

static void queue_wire( const void *bytes, mach_msg_size_t len )
{
    assert(script_len < MAX_SCRIPT && len <= MAX_WIRE);
    script[script_len].real_rcv = 0;
    script[script_len].len = len;
    memcpy( script[script_len].bytes, bytes, len );
    script_len++;
}

static void queue_real_rcv(void)
{
    assert(script_len < MAX_SCRIPT);
    script[script_len].real_rcv = 1;
    script[script_len].len = 0;
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

static void capture_destroy( unsigned int shm_idx )
{
    int before = sent_len;

    assert(destroy_all( shm_idx ) == MACH_MSG_SUCCESS);
    assert(sent_len == before + 1);
    captured = sent[sent_len - 1];
}

static void kernel_send( const struct wire_op *op )
{
    unsigned char buf[MAX_WIRE];

    memcpy( buf, op->bytes, op->len );
    assert(real_mach_msg( (mach_msg_header_t *)buf, MACH_SEND_MSG, op->len, 0,
                          MACH_PORT_NULL, MACH_MSG_TIMEOUT_NONE, MACH_PORT_NULL ) == MACH_MSG_SUCCESS);
}

static void poke_field( struct wire_op *op, size_t offset, const void *value, size_t size )
{
    assert(offset + size <= op->len);
    memcpy( op->bytes + offset, value, size );
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
    header.msgh_id = shm_idx;
    header.msgh_size = sizeof(header);
    header.msgh_remote_port = 1;
    queue_wire( &header, header.msgh_size );
}

static struct wire_op queue_close_variant( const struct wire_op *base, mach_msg_size_t len,
                                           const void *cookie, const void *shm_idx )
{
    struct wire_op op = *base;
    mach_msg_size_t wire_size = len;

    op.len = len;
    if (len > base->len) memset( op.bytes + base->len, 0xa5, len - base->len );
    /* The kernel reports the true wire size in msgh_size; a crafted short or
     * long close must present its malformed size for the pump to reject. */
    poke_field( &op, offsetof(mach_msg_header_t, msgh_size ), &wire_size, sizeof(wire_size) );
    if (cookie) poke_field( &op, offsetof(mach_close_message_t, cookie), cookie, sizeof(uint64_t) );
    if (shm_idx) poke_field( &op, offsetof(mach_close_message_t, shm_idx), shm_idx, sizeof(unsigned int) );
    queue_wire( op.bytes, op.len );
    return op;
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

/* A planted wait node makes "did anything dispatch?" observable without
 * touching the shared memory supply: a spurious signal completes it (state
 * flips to WOKEN plus a wake), a spurious release never does. */
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

/* Bytes 24..27 of a close message overlay register shm_idx[0]; a close that
 * falls through into the registration dispatch publishes state for tid
 * 0x7fffff (MSYNC_CLOSE_MESSAGE_ID >> 8) starting from that word. */
static unsigned int close_token_word( const struct wire_op *op )
{
    unsigned int word;

    assert(op->len >= offsetof(mach_close_message_t, cookie) + sizeof(word));
    memcpy( &word, op->bytes + offsetof(mach_close_message_t, cookie), sizeof(word) );
    return word;
}

#define FALLTHROUGH_TID ((unsigned int)MSYNC_CLOSE_MESSAGE_ID >> 8)
#define FALLTHROUGH_IDX ((unsigned int)MSYNC_CLOSE_MESSAGE_ID & MSYNC_SHM_INDEX_MASK)

/* Publish the state a fallthrough close would CAS from (register/unregister
 * decode of the close id uses the cookie word as the token) so any stray
 * dispatch is observable as a state change. */
static void preset_fallthrough_state( const struct wire_op *close )
{
    shm_tid_map[FALLTHROUGH_TID] = (int)close_token_word( close );
}

static void expect_fallthrough_untouched( const struct wire_op *close )
{
    assert((unsigned int)shm_tid_map[FALLTHROUGH_TID] == close_token_word( close ));
    assert(!find_registration( FALLTHROUGH_TID ));
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
    run_single_register_case( 1u << 20, 3 );  /* bit-20 tid */
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
    run_pump();
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

static void case_close_authorized(void)
{
    struct wire_op close;

    setup_tid_map();
    prime_slot( 3, 2 );
    prime_slot( 4, 2 );
    capture_destroy( 3 );
    close = captured;

    queue_wire( close.bytes, close.len );
    run_pump();
    assert(fake_shm[3].refcount == 1); /* exactly one reference released */
    assert(fake_shm[4].refcount == 2); /* and only for the named index */
    assert(fake_shm[3].multiple_waiters == 0);
    assert(free_shm_idx == UINT32_MAX); /* still referenced, not recycled */
    assert(shm_access_len == 1 && shm_access[0] == 3);
    assert(wake_len == 0); /* a close never mutates wait state */

    queue_wire( close.bytes, close.len );
    run_pump();
    assert(fake_shm[3].refcount == 0 && fake_shm[4].refcount == 2);
    assert(fake_shm[3].msync_type == 0);
    assert(free_shm_idx == 3 && fake_shm[3].low == -1); /* recycled exactly once */
    assert(shm_access_len == 2 && shm_access[1] == 3);
    assert(wake_len == 0);
    puts("ok close_authorized");
}

static void case_close_real_port(void)
{
    struct wire_op forged;

    setup_tid_map();
    prime_slot( 5, 2 );
    prime_slot( 6, 2 );
    capture_destroy( 5 ); /* production sender wire bytes */
    forged = captured;
    {
        uint64_t bad_cookie = close_cookie ^ 1;
        unsigned int bad_idx = 6;

        poke_field( &forged, offsetof(mach_close_message_t, cookie), &bad_cookie, sizeof(bad_cookie) );
        poke_field( &forged, offsetof(mach_close_message_t, shm_idx), &bad_idx, sizeof(bad_idx) );
    }
    kernel_send( &forged ); /* forged close over the real Mach port */
    forward_sends = 1;
    assert(destroy_all( 5 ) == MACH_MSG_SUCCESS); /* production sender, real send */
    queue_real_rcv();
    queue_real_rcv();
    run_pump();
    assert(fake_shm[5].refcount == 1); /* authorized close released once */
    assert(fake_shm[6].refcount == 2); /* forged close discarded */
    assert(wake_len == 0);
    puts("ok close_real_port");
}

static void case_close_bad_cookie(void)
{
    uint64_t zero = 0, wrong = 0x00000000ull << 32 | 0xdeadbeefu;
    unsigned int idx8 = 8;
    struct wait_registration *waiter, *fallthrough_waiter;
    struct wire_op variant;

    setup_tid_map();
    prime_slot( 7, 2 );
    prime_slot( 8, 2 );
    capture_destroy( 7 );
    waiter = plant_waiter( 8, 50, make_token( 0x60 ) );
    fallthrough_waiter = plant_waiter( FALLTHROUGH_IDX, 60, make_token( 0x62 ) );

    variant = queue_close_variant( &captured, captured.len, &zero, &idx8 ); /* zero cookie */
    preset_fallthrough_state( &variant );
    run_pump();
    assert(fake_shm[7].refcount == 2 && fake_shm[8].refcount == 2);
    assert(shm_access_len == 0); /* no slot was even resolved */
    expect_waiter_untouched( waiter, 8, 50, make_token( 0x60 ) );
    expect_waiter_untouched( fallthrough_waiter, FALLTHROUGH_IDX, 60, make_token( 0x62 ) );
    expect_fallthrough_untouched( &variant );

    variant = queue_close_variant( &captured, captured.len, &wrong, &idx8 ); /* unknown cookie */
    preset_fallthrough_state( &variant );
    run_pump();
    assert(fake_shm[7].refcount == 2 && fake_shm[8].refcount == 2);
    assert(fake_shm[8].multiple_waiters == 0); /* planted waiter needs no shm ref */
    assert(free_shm_idx == UINT32_MAX);
    assert(shm_access_len == 0); /* no slot was even resolved */
    expect_waiter_untouched( waiter, 8, 50, make_token( 0x60 ) );
    expect_waiter_untouched( fallthrough_waiter, FALLTHROUGH_IDX, 60, make_token( 0x62 ) );
    expect_fallthrough_untouched( &variant );
    assert(wake_len == 0);
    puts("ok close_bad_cookie");
}

static void case_close_bad_size(void)
{
    struct wire_op wire;
    struct wait_registration *waiter, *fallthrough_waiter;

    setup_tid_map();
    prime_slot( 9, 2 );
    prime_slot( 10, 2 );
    capture_destroy( 10 );
    wire = captured;
    assert(wire.len == sizeof(mach_close_message_t));
    waiter = plant_waiter( 9, 50, make_token( 0x61 ) );
    fallthrough_waiter = plant_waiter( FALLTHROUGH_IDX, 60, make_token( 0x63 ) );
    preset_fallthrough_state( &wire );

    queue_wire( wire.bytes, wire.len );           /* valid: releases 10 once */
    queue_close_variant( &wire, wire.len - sizeof(uint32_t), NULL, NULL ); /* truncated */
    queue_close_variant( &wire, wire.len + sizeof(uint32_t), NULL, NULL ); /* oversized */
    queue_close_variant( &wire, sizeof(mach_msg_header_t), NULL, NULL ); /* header-only close id */
    run_pump();
    assert(fake_shm[10].refcount == 1); /* only the well-formed close landed */
    assert(fake_shm[9].refcount == 2 && fake_shm[9].multiple_waiters == 0);
    assert(free_shm_idx == UINT32_MAX);
    assert(shm_access_len == 1 && shm_access[0] == 10);
    expect_waiter_untouched( waiter, 9, 50, make_token( 0x61 ) );
    expect_waiter_untouched( fallthrough_waiter, FALLTHROUGH_IDX, 60, make_token( 0x63 ) );
    expect_fallthrough_untouched( &wire );
    assert(wake_len == 0); /* the 24-byte variant must not signal either */
    puts("ok close_bad_size");
}

static void case_close_bad_index(void)
{
    unsigned int past_mask = MSYNC_SHM_INDEX_MASK + 1;
    unsigned int all_ones = ~0u;
    struct wait_registration *fallthrough_waiter;

    setup_tid_map();
    prime_slot( 11, 2 );
    capture_destroy( 11 );
    fallthrough_waiter = plant_waiter( FALLTHROUGH_IDX, 60, make_token( 0x64 ) );
    preset_fallthrough_state( &captured );
    queue_close_variant( &captured, captured.len, NULL, &past_mask );
    queue_close_variant( &captured, captured.len, NULL, &all_ones );
    run_pump();
    assert(fake_shm[11].refcount == 2);
    assert(free_shm_idx == UINT32_MAX);
    assert(shm_access_len == 0); /* out-of-range index must not resolve a slot */
    expect_waiter_untouched( fallthrough_waiter, FALLTHROUGH_IDX, 60, make_token( 0x64 ) );
    expect_fallthrough_untouched( &captured );
    assert(wake_len == 0);
    puts("ok close_bad_index");
}

int main(int argc, char **argv)
{
    if (argc != 2) return 1;
    if (!strcmp( argv[1], "register_count1" )) case_register_count1();
    else if (!strcmp( argv[1], "high_tid_32byte" )) case_high_tid_32byte();
    else if (!strcmp( argv[1], "register_count65" )) case_register_count65();
    else if (!strcmp( argv[1], "unregister_same_id" )) case_unregister_same_id();
    else if (!strcmp( argv[1], "signal_header_only" )) case_signal_header_only();
    else if (!strcmp( argv[1], "close_authorized" )) case_close_authorized();
    else if (!strcmp( argv[1], "close_real_port" )) case_close_real_port();
    else if (!strcmp( argv[1], "close_bad_cookie" )) case_close_bad_cookie();
    else if (!strcmp( argv[1], "close_bad_size" )) case_close_bad_size();
    else if (!strcmp( argv[1], "close_bad_index" )) case_close_bad_index();
    else return 2;
    return 0;
}
'''

HARNESS = PROLOGUE + CORE + EPILOGUE


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

    def test_authorized_close_releases_correct_index_once(self):
        self.run_case("close_authorized")

    def test_close_over_real_mach_port(self):
        self.run_case("close_real_port")

    def test_bad_cookie_close_is_discarded(self):
        self.run_case("close_bad_cookie")

    def test_bad_size_close_is_discarded(self):
        self.run_case("close_bad_size")

    def test_bad_index_close_is_discarded(self):
        self.run_case("close_bad_index")


if __name__ == "__main__":
    unittest.main()
