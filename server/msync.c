/*
 * mach semaphore-based synchronization objects
 *
 * Copyright (C) 2018 Zebediah Figura
 * Copyright (C) 2023 Marc-Aurel Zent
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 *
 * This library is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
 * Lesser General Public License for more details.
 *
 * You should have received a copy of the GNU Lesser General Public
 * License along with this library; if not, write to the Free Software
 * Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301, USA
 */

#ifdef __APPLE__

#include "config.h"

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <stdio.h>
#include <stdarg.h>
#include <sys/mman.h>
#ifdef HAVE_SYS_STAT_H
# include <sys/stat.h>
#endif
#include <mach/mach_init.h>
#include <mach/mach_port.h>
#include <mach/mach_vm.h>
#include <mach/vm_page_size.h>
#include <mach/vm_map.h>
#include <mach/message.h>
#include <mach/port.h>
#include <mach/task.h>
#include <mach/semaphore.h>
#include <mach/mach_error.h>
#include <mach/thread_act.h>
#include <servers/bootstrap.h>
#include <sched.h>
#include <dlfcn.h>
#include <signal.h>
#include <pthread.h>
#include <unistd.h>

#include "ntstatus.h"
#define WIN32_NO_STATUS
#include "windef.h"
#include "winternl.h"

#include "handle.h"
#include "request.h"
#include "msync.h"

#define UL_COMPARE_AND_WAIT_SHARED  0x3
#define ULF_WAKE_ALL                0x00000100
extern int __ulock_wake( uint32_t operation, void *addr, uint64_t wake_value );


#define MACH_CHECK_ERROR(ret, operation) \
    if (ret != KERN_SUCCESS) \
        fprintf(stderr, "msync: error: %s failed with %d: %s\n", \
            operation, ret, mach_error_string(ret));

/* Private API to register a mach port with the bootstrap server */
extern kern_return_t bootstrap_register2( mach_port_t bp, name_t service_name, mach_port_t sp, int flags );

/*
 * Faster to directly do the syscall and inline everything, taken and slightly adapted
 * from xnu/libsyscall/mach/mach_msg.c
 */

#define LIBMACH_OPTIONS64 (MACH_SEND_INTERRUPT|MACH_RCV_INTERRUPT)
#define MACH64_SEND_MQ_CALL 0x0000000400000000ull

typedef mach_msg_return_t (*mach_msg2_trap_ptr_t)( void *data, uint64_t options,
    uint64_t msgh_bits_and_send_size, uint64_t msgh_remote_and_local_port,
    uint64_t msgh_voucher_and_id, uint64_t desc_count_and_rcv_name,
    uint64_t rcv_size_and_priority, uint64_t timeout );

static mach_msg2_trap_ptr_t mach_msg2_trap;

static inline mach_msg_return_t mach_msg2_internal( void *data, uint64_t option64, uint64_t msgh_bits_and_send_size,
    uint64_t msgh_remote_and_local_port, uint64_t msgh_voucher_and_id, uint64_t desc_count_and_rcv_name,
    uint64_t rcv_size_and_priority, uint64_t timeout)
{
    mach_msg_return_t mr;

    mr = mach_msg2_trap( data, option64 & ~LIBMACH_OPTIONS64, msgh_bits_and_send_size,
             msgh_remote_and_local_port, msgh_voucher_and_id, desc_count_and_rcv_name,
             rcv_size_and_priority, timeout );

    if (mr == MACH_MSG_SUCCESS)
        return MACH_MSG_SUCCESS;

    while (mr == MACH_SEND_INTERRUPTED)
        mr = mach_msg2_trap( data, option64 & ~LIBMACH_OPTIONS64, msgh_bits_and_send_size,
                 msgh_remote_and_local_port, msgh_voucher_and_id, desc_count_and_rcv_name,
                 rcv_size_and_priority, timeout );

    while (mr == MACH_RCV_INTERRUPTED)
        mr = mach_msg2_trap( data, option64 & ~LIBMACH_OPTIONS64, msgh_bits_and_send_size & 0xffffffffull,
                 msgh_remote_and_local_port, msgh_voucher_and_id, desc_count_and_rcv_name,
                 rcv_size_and_priority, timeout);

    return mr;
}

static inline mach_msg_return_t mach_msg2( mach_msg_header_t *data, uint64_t option64,
    mach_msg_size_t send_size, mach_msg_size_t rcv_size, mach_port_t rcv_name, uint64_t timeout,
    uint32_t priority)
{
    mach_msg_base_t *base;
    mach_msg_size_t descriptors;

    if (!mach_msg2_trap)
        return mach_msg( data, (mach_msg_option_t)option64, send_size,
                         rcv_size, rcv_name, timeout, priority );

    base = (mach_msg_base_t *)data;

    if ((option64 & MACH_SEND_MSG) &&
        (base->header.msgh_bits & MACH_MSGH_BITS_COMPLEX))
        descriptors = base->body.msgh_descriptor_count;
    else
        descriptors = 0;

#define MACH_MSG2_SHIFT_ARGS(lo, hi) ((uint64_t)hi << 32 | (uint32_t)lo)
    return mach_msg2_internal(data, option64 | MACH64_SEND_MQ_CALL,
               MACH_MSG2_SHIFT_ARGS(data->msgh_bits, send_size),
               MACH_MSG2_SHIFT_ARGS(data->msgh_remote_port, data->msgh_local_port),
               MACH_MSG2_SHIFT_ARGS(data->msgh_voucher_port, data->msgh_id),
               MACH_MSG2_SHIFT_ARGS(descriptors, rcv_name),
               MACH_MSG2_SHIFT_ARGS(rcv_size, priority), timeout);
#undef MACH_MSG2_SHIFT_ARGS
}

static mach_port_name_t receive_port;

enum wait_state
{
    MSYNC_WAIT_IDLE,
    MSYNC_WAIT_REGISTERING,
    MSYNC_WAIT_ARMED,
    MSYNC_WAIT_WOKEN,
    MSYNC_WAIT_CANCELING,
    MSYNC_WAIT_FAILED
};

#define WAIT_STATE_MASK 7u
#define WAIT_GENERATION_STEP 8u
#define MSYNC_MAP_MESSAGE_WIRE_SIZE 32u
#define MSYNC_CLEANUP_MESSAGE_ID ((mach_msg_id_t)0x7ffffffe)

static inline unsigned int wait_token_state( unsigned int token )
{
    return token & WAIT_STATE_MASK;
}

static inline unsigned int wait_token_with_state( unsigned int token, enum wait_state state )
{
    return (token & ~WAIT_STATE_MASK) | state;
}

struct wait_registration;

struct wait_node
{
    struct wait_node *next;
    struct wait_node **prev;
    struct wait_registration *registration;
    unsigned int shm_idx;
};

struct wait_registration
{
    struct wait_registration *free_next;
    struct wait_registration *hash_next;
    unsigned int tid;
    unsigned int token;
    unsigned int node_count;
    unsigned int capacity;
    struct wait_node nodes[];
};

struct wait_list
{
    struct wait_node *head;
};

#define MAX_REGISTRATION_NODES 0x80000
#define REGISTRATION_HASH_SIZE 16384

static struct wait_registration *free_registrations[MAXIMUM_WAIT_OBJECTS + 2];
static struct wait_registration *registration_hash[REGISTRATION_HASH_SIZE];
static unsigned int allocated_registration_nodes;
static struct wait_list *wait_lists;
static size_t wait_lists_size;
static int *shm_tid_map;
static const mach_vm_size_t shm_tid_size = 64 * 1024 * 1024; /* 64 MB to index 24 bit tids */

static inline unsigned int registration_hash_idx( unsigned int tid )
{
    return (tid * 2654435761u) & (REGISTRATION_HASH_SIZE - 1);
}

static struct wait_registration *find_registration( unsigned int tid )
{
    struct wait_registration *registration;

    for (registration = registration_hash[registration_hash_idx( tid )]; registration;
         registration = registration->hash_next)
        if (registration->tid == tid) return registration;

    return NULL;
}

static void insert_registration( struct wait_registration *registration )
{
    unsigned int idx = registration_hash_idx( registration->tid );

    registration->hash_next = registration_hash[idx];
    registration_hash[idx] = registration;
}

static void remove_registration( struct wait_registration *registration )
{
    struct wait_registration **cursor = registration_hash + registration_hash_idx( registration->tid );

    while (*cursor && *cursor != registration) cursor = &(*cursor)->hash_next;
    if (*cursor) *cursor = registration->hash_next;
}

static int reclaim_cached_registration(void)
{
    struct wait_registration *registration;
    unsigned int capacity;

    for (capacity = 1; capacity < ARRAY_SIZE(free_registrations); capacity++)
    {
        if (!(registration = free_registrations[capacity])) continue;
        free_registrations[capacity] = registration->free_next;
        allocated_registration_nodes -= registration->capacity;
        free( registration );
        return 1;
    }
    return 0;
}

static struct wait_registration *alloc_registration( unsigned int count )
{
    struct wait_registration *registration;

    if ((registration = free_registrations[count]))
    {
        free_registrations[count] = registration->free_next;
        return registration;
    }

    while (count > MAX_REGISTRATION_NODES - allocated_registration_nodes)
    {
        if (reclaim_cached_registration()) continue;
        fprintf( stderr, "msync: error: wait registration node pool exhausted\n" );
        return NULL;
    }

    if (!(registration = malloc( sizeof(*registration) + count * sizeof(registration->nodes[0]) )))
    {
        fprintf( stderr, "msync: error: failed to allocate wait registration\n" );
        return NULL;
    }
    registration->capacity = count;
    allocated_registration_nodes += count;
    return registration;
}

static void free_registration( struct wait_registration *registration )
{
    registration->free_next = free_registrations[registration->capacity];
    free_registrations[registration->capacity] = registration;
}

static int grow_wait_lists( unsigned int shm_idx )
{
    size_t new_size = max(wait_lists_size ? wait_lists_size * 2 : 256, (size_t)shm_idx + 1);
    struct wait_list *new_wait_lists;
    size_t i;

    if (new_size < wait_lists_size ||
        !(new_wait_lists = realloc( wait_lists, new_size * sizeof(*new_wait_lists) )))
    {
        fprintf( stderr, "msync: error: failed to grow wait list array to %zu entries\n", new_size );
        return 0;
    }

    memset( new_wait_lists + wait_lists_size, 0,
            (new_size - wait_lists_size) * sizeof(*new_wait_lists) );
    for (i = 0; i < wait_lists_size; i++)
        if (new_wait_lists[i].head) new_wait_lists[i].head->prev = &new_wait_lists[i].head;
    wait_lists = new_wait_lists;
    wait_lists_size = new_size;
    return 1;
}

static inline struct wait_list *get_wait_list( unsigned int shm_idx, int create )
{
    if (shm_idx >= wait_lists_size)
    {
        if (!create || !grow_wait_lists( shm_idx )) return NULL;
    }
    return wait_lists + shm_idx;
}

static void unlink_wait_node( struct wait_node *node )
{
    if (!node->prev) return;

    *node->prev = node->next;
    if (node->next) node->next->prev = node->prev;
    node->next = NULL;
    node->prev = NULL;
}

static void detach_registration( struct wait_registration *registration )
{
    unsigned int i;

    for (i = 0; i < registration->node_count; i++)
        unlink_wait_node( registration->nodes + i );
}

static int link_wait_node( struct wait_registration *registration, unsigned int shm_idx )
{
    struct wait_node *node = registration->nodes + registration->node_count;
    struct wait_list *list = get_wait_list( shm_idx, 1 );

    if (!list) return 0;

    node->registration = registration;
    node->prev = &list->head;
    node->next = list->head;
    if (node->next) node->next->prev = &node->next;
    list->head = node;
    registration->node_count++;
    return 1;
}

static void *get_shm( unsigned int idx );

typedef struct
{
    mach_msg_header_t header;
    unsigned int shm_idx[MAXIMUM_WAIT_OBJECTS + 2];
    mach_msg_trailer_t trailer;
} mach_register_message_t;

typedef struct
{
    mach_msg_header_t header;
    unsigned int wire_version;
    int entry;
    mach_msg_trailer_t trailer;
} mach_map_message_t;

typedef struct
{
    mach_msg_header_t header;
    unsigned int tid;
    mach_msg_trailer_t trailer;
} mach_cleanup_message_t;

typedef struct
{
    mach_msg_header_t header;
    mach_msg_trailer_t trailer;
} mach_cleanup_reply_t;

typedef struct
{
    mach_msg_header_t header;
    mach_msg_body_t body;
    mach_msg_port_descriptor_t descriptor;
} mach_map_message_reply_t;

static void *shm_addrs[MSYNC_SHM_MAX_PAGES];

static void send_shm_to_client( mach_map_message_t *message )
{
    static mach_map_message_reply_t reply;
    mach_msg_return_t mr;
    kern_return_t kr;
    memory_object_offset_t offset = 0;
    mach_vm_size_t entry_size = 0;
    mach_port_t entry_port = MACH_PORT_NULL;

    if (message->header.msgh_size != MSYNC_MAP_MESSAGE_WIRE_SIZE)
        fprintf( stderr, "msync: error: client sent malformed shared mapping request of %u bytes (expected %u)\n",
                 message->header.msgh_size, MSYNC_MAP_MESSAGE_WIRE_SIZE );
    else if (message->wire_version != MSYNC_SHM_WIRE_VERSION)
        fprintf( stderr, "msync: error: client requested incompatible shared layout %u (expected %u)\n",
                 message->wire_version, MSYNC_SHM_WIRE_VERSION );
    else if (message->header.msgh_id)
    {
        offset = (memory_object_offset_t)shm_tid_map;
        entry_size = shm_tid_size;
    }
    else if (message->entry >= 0 && message->entry < MSYNC_SHM_MAX_PAGES &&
             __atomic_load_n( &shm_addrs[message->entry], __ATOMIC_ACQUIRE ))
    {
        offset = (memory_object_offset_t)shm_addrs[message->entry];
        entry_size = MSYNC_SHM_PAGE_SIZE;
    }
    else
        fprintf( stderr, "msync: error: client requested invalid shm entry %d\n", message->entry );

    if (entry_size)
    {
        kr = mach_make_memory_entry_64( mach_task_self(), &entry_size, offset, VM_PROT_DEFAULT,
                                        &entry_port, MACH_PORT_NULL );

        if (kr != KERN_SUCCESS)
            fprintf( stderr, "msync: error: mach_make_memory_entry_64 failed with %d: %s\n",
                     kr, mach_error_string( kr ) );
    }

    reply.header.msgh_bits = MACH_MSGH_BITS_SET( MACH_MSG_TYPE_COPY_SEND, 0, 0, MACH_MSGH_BITS_COMPLEX );
    reply.header.msgh_id = message->header.msgh_id;
    reply.header.msgh_size = sizeof(reply);
    reply.header.msgh_remote_port = message->header.msgh_remote_port;
    reply.body.msgh_descriptor_count = 1;
    reply.descriptor.name = entry_port;
    reply.descriptor.disposition = MACH_MSG_TYPE_COPY_SEND;
    reply.descriptor.type = MACH_MSG_PORT_DESCRIPTOR;

    mr = mach_msg2( &reply.header, MACH_SEND_MSG, reply.header.msgh_size,
                    0, MACH_PORT_NULL, MACH_MSG_TIMEOUT_NONE, 0 );

    if (mr != MACH_MSG_SUCCESS)
        fprintf( stderr, "msync: error: failed to send shm entry to client: %x\n", mr );

    mach_port_deallocate( mach_task_self(), message->header.msgh_remote_port );
    mach_port_deallocate( mach_task_self(), entry_port );
}

static inline void wake_wait_state( unsigned int tid )
{
    __ulock_wake( UL_COMPARE_AND_WAIT_SHARED, shm_tid_map + tid, 0 );
}

static int publish_wait_state( unsigned int tid, int from, int to )
{
    int expected = from;

    if (!__atomic_compare_exchange_n( shm_tid_map + tid, &expected, to, 0,
                                      __ATOMIC_RELEASE, __ATOMIC_ACQUIRE ))
        return 0;

    wake_wait_state( tid );
    return 1;
}

static void retire_registration( struct wait_registration *registration );

static void complete_registration( struct wait_registration *registration,
                                   enum wait_state from, enum wait_state to )
{
    detach_registration( registration );
    publish_wait_state( registration->tid, wait_token_with_state( registration->token, from ),
                        wait_token_with_state( registration->token, to ) );
}

static void acknowledge_unregister( unsigned int tid, unsigned int token )
{
    struct wait_registration *registration = find_registration( tid );
    unsigned int state = __atomic_load_n( shm_tid_map + tid, __ATOMIC_ACQUIRE );

    if ((state & ~WAIT_STATE_MASK) != (token & ~WAIT_STATE_MASK)) return;
    if (wait_token_state( state ) != MSYNC_WAIT_WOKEN && wait_token_state( state ) != MSYNC_WAIT_CANCELING &&
        wait_token_state( state ) != MSYNC_WAIT_FAILED)
    {
        fprintf( stderr, "msync: error: unregister for tid %u in state %#x\n", tid, state );
        return;
    }

    if (registration)
    {
        if (registration->token != token) return;
        detach_registration( registration );
        retire_registration( registration );
        remove_registration( registration );
        free_registration( registration );
    }

    if (!publish_wait_state( tid, state, wait_token_with_state( token, MSYNC_WAIT_IDLE ) ))
        fprintf( stderr, "msync: error: unregister state changed for tid %u\n", tid );
}

static inline void signal_all_internal( unsigned int shm_idx )
{
    struct wait_list *list = get_wait_list( shm_idx, 0 );

    while (list && list->head)
    {
        struct wait_registration *registration = list->head->registration;

        complete_registration( registration, MSYNC_WAIT_ARMED, MSYNC_WAIT_WOKEN );
    }
}

/* shm layout for msync objects. */
struct msync_shm
{
    int low;
    int high;
    unsigned short msync_type;
    unsigned short refcount;
    int multiple_waiters;
};

static pthread_mutex_t shm_index_mutex = PTHREAD_MUTEX_INITIALIZER;
static unsigned int free_shm_idx = UINT32_MAX;
static unsigned int next_unused_shm_idx = 2;

static int retain_shm_ref( unsigned int shm_idx )
{
    struct msync_shm *obj = get_shm( shm_idx );
    unsigned short refs = __atomic_load_n( &obj->refcount, __ATOMIC_RELAXED );

    do
    {
        if (!refs || refs == USHRT_MAX) return 0;
    } while (!__atomic_compare_exchange_n( &obj->refcount, &refs, refs + 1, 1,
                                           __ATOMIC_SEQ_CST, __ATOMIC_RELAXED ));
    __atomic_add_fetch( &obj->multiple_waiters, 1, __ATOMIC_SEQ_CST );
    return 1;
}

static void release_shm_ref( unsigned int shm_idx )
{
    struct msync_shm *obj = get_shm( shm_idx );
    unsigned short refs;

    pthread_mutex_lock( &shm_index_mutex );
    refs = __atomic_load_n( &obj->refcount, __ATOMIC_SEQ_CST );
    if (!refs)
        fprintf( stderr, "msync: error: refcount underflow for shm idx %u\n", shm_idx );
    else if (!__atomic_sub_fetch( &obj->refcount, 1, __ATOMIC_SEQ_CST ))
    {
        obj->msync_type = 0;
        obj->low = free_shm_idx;
        free_shm_idx = shm_idx;
    }
    pthread_mutex_unlock( &shm_index_mutex );
}

static void retire_registration( struct wait_registration *registration )
{
    unsigned int i;

    for (i = 0; i < registration->capacity; i++)
    {
        struct msync_shm *obj = get_shm( registration->nodes[i].shm_idx );
        int waiters = __atomic_sub_fetch( &obj->multiple_waiters, 1, __ATOMIC_SEQ_CST );
        if (waiters < 0)
            fprintf( stderr, "msync: error: waiter count underflow for shm idx %u\n",
                     registration->nodes[i].shm_idx );
        release_shm_ref( registration->nodes[i].shm_idx );
    }
}

static void retire_message_refs( mach_register_message_t *message, unsigned int count )
{
    unsigned int i;

    for (i = 0; i < count; i++)
    {
        struct msync_shm *obj;
        unsigned int shm_idx = message->shm_idx[i + 1] & MSYNC_SHM_INDEX_MASK;
        int waiters;

        obj = get_shm( shm_idx );
        waiters = __atomic_sub_fetch( &obj->multiple_waiters, 1, __ATOMIC_SEQ_CST );
        if (waiters < 0)
            fprintf( stderr, "msync: error: waiter count underflow for shm idx %u\n", shm_idx );
        release_shm_ref( shm_idx );
    }
}

static inline void destroy_all_internal( unsigned int shm_idx )
{
    release_shm_ref( shm_idx );
}

/* Client registration and unregister messages are ordered on the Mach port. Shared
 * state publication still uses release/acquire ordering for weakly ordered CPUs. */
static inline mach_msg_return_t destroy_all( unsigned int shm_idx )
{
    static mach_msg_header_t send_header;
    send_header.msgh_bits = MACH_MSGH_BITS_REMOTE(MACH_MSG_TYPE_COPY_SEND);
    send_header.msgh_id = (shm_idx & MSYNC_SHM_INDEX_MASK) | MSYNC_SHM_CLOSE_FLAG;
    send_header.msgh_size = sizeof(send_header);
    send_header.msgh_remote_port = receive_port;

    return mach_msg2( &send_header, MACH_SEND_MSG, send_header.msgh_size,
                0, MACH_PORT_NULL, MACH_MSG_TIMEOUT_NONE, 0);
}

static inline mach_msg_return_t signal_all( unsigned int shm_idx, int *shm )
{
    static mach_msg_header_t send_header;
    struct msync_shm *obj = (struct msync_shm *)shm;

    __ulock_wake( UL_COMPARE_AND_WAIT_SHARED | ULF_WAKE_ALL, (void *)shm, 0 );
    if (!__atomic_load_n( &obj->multiple_waiters, __ATOMIC_SEQ_CST ))
        return MACH_MSG_SUCCESS;

    send_header.msgh_bits = MACH_MSGH_BITS_REMOTE(MACH_MSG_TYPE_COPY_SEND);
    send_header.msgh_id = shm_idx;
    send_header.msgh_size = sizeof(send_header);
    send_header.msgh_remote_port = receive_port;

    return mach_msg2( &send_header, MACH_SEND_MSG, send_header.msgh_size,
                0, MACH_PORT_NULL, MACH_MSG_TIMEOUT_NONE, 0 );
}

static inline mach_msg_return_t receive_mach_msg( mach_register_message_t *buffer )
{
    return mach_msg2( (mach_msg_header_t *)buffer, MACH_RCV_MSG, 0,
            sizeof(*buffer), receive_port, MACH_MSG_TIMEOUT_NONE, 0 );
}

static inline void decode_msgh_id( unsigned int msgh_id, unsigned int *tid, unsigned int *count )
{
    *tid = msgh_id >> 8;
    *count = msgh_id & 0xFF;
}

static inline unsigned int check_bit( const unsigned int bit, unsigned int *shm_idx )
{
    unsigned int bit_val = (*shm_idx >> bit) & 1;
    *shm_idx &= ~(1u << bit);
    return bit_val;
}

static void register_wait( mach_register_message_t *message, unsigned int tid, unsigned int count )
{
    struct wait_registration *registration;
    unsigned int i, token = message->shm_idx[0];
    unsigned int state;

    state = __atomic_load_n( shm_tid_map + tid, __ATOMIC_ACQUIRE );
    if (state != token || wait_token_state( token ) != MSYNC_WAIT_REGISTERING)
        return;

    if (find_registration( tid ))
    {
        fprintf( stderr, "msync: error: duplicate registration for tid %u\n", tid );
        publish_wait_state( tid, token, wait_token_with_state( token, MSYNC_WAIT_FAILED ) );
        return;
    }

    if (!(registration = alloc_registration( count )))
    {
        publish_wait_state( tid, token, wait_token_with_state( token, MSYNC_WAIT_FAILED ) );
        return;
    }

    for (i = 0; i < count; i++)
    {
        unsigned int shm_idx = message->shm_idx[i + 1] & MSYNC_SHM_INDEX_MASK;

        if (retain_shm_ref( shm_idx )) continue;
        retire_message_refs( message, i );
        free_registration( registration );
        publish_wait_state( tid, token, wait_token_with_state( token, MSYNC_WAIT_FAILED ) );
        return;
    }

    registration->tid = tid;
    registration->token = message->shm_idx[0];
    registration->node_count = 0;
    for (i = 0; i < count; i++)
        registration->nodes[i].shm_idx = message->shm_idx[i + 1] & MSYNC_SHM_INDEX_MASK;
    insert_registration( registration );

    for (i = 0; i < count; i++)
    {
        struct msync_shm *obj;
        unsigned int shm_idx = message->shm_idx[i + 1];
        unsigned int is_mutex = check_bit( 28, &shm_idx );
        int val;

        obj = get_shm( shm_idx );
        val = __atomic_load_n( &obj->low, __ATOMIC_ACQUIRE );
        if ((is_mutex && (val == 0 || val == ~0 || val == tid)) || (!is_mutex && val != 0))
        {
            complete_registration( registration, MSYNC_WAIT_REGISTERING, MSYNC_WAIT_WOKEN );
            return;
        }

        if (!link_wait_node( registration, shm_idx ))
        {
            complete_registration( registration, MSYNC_WAIT_REGISTERING, MSYNC_WAIT_FAILED );
            return;
        }
    }

    if (publish_wait_state( tid, token, wait_token_with_state( token, MSYNC_WAIT_ARMED ) ))
    {
        if (getenv("WINE_MSYNC_TEST_TRACE") && getenv("WINE_MSYNC_TEST_TRACE")[0] == '1' &&
            !getenv("WINE_MSYNC_TEST_TRACE")[1])
            fprintf( stderr, "msync: MSYNC_WAIT_ARMED tid %u count %u\n", tid, count );
    }
    else
        detach_registration( registration );
}

static void cleanup_thread_registration( mach_cleanup_message_t *message )
{
    struct wait_registration *registration = find_registration( message->tid );
    mach_msg_header_t reply = {0};
    unsigned int state, next;
    mach_msg_return_t mr;

    if (registration)
    {
        detach_registration( registration );
        retire_registration( registration );
        remove_registration( registration );
        free_registration( registration );
    }

    state = __atomic_load_n( shm_tid_map + message->tid, __ATOMIC_ACQUIRE );
    for (;;)
    {
        next = ((state & ~WAIT_STATE_MASK) + WAIT_GENERATION_STEP) | MSYNC_WAIT_IDLE;
        if (__atomic_compare_exchange_n( shm_tid_map + message->tid, &state, next, 0,
                                         __ATOMIC_RELEASE, __ATOMIC_ACQUIRE ))
            break;
    }
    wake_wait_state( message->tid );

    reply.msgh_bits = MACH_MSGH_BITS_REMOTE(MACH_MSG_TYPE_COPY_SEND);
    reply.msgh_size = sizeof(reply);
    reply.msgh_remote_port = message->header.msgh_remote_port;
    reply.msgh_id = MSYNC_CLEANUP_MESSAGE_ID;
    mr = mach_msg2( &reply, MACH_SEND_MSG, reply.msgh_size, 0,
                    MACH_PORT_NULL, MACH_MSG_TIMEOUT_NONE, 0 );
    if (mr != MACH_MSG_SUCCESS)
        fprintf( stderr, "msync: error: failed to acknowledge tid %u cleanup: %#x\n",
                 message->tid, mr );
    mach_port_deallocate( mach_task_self(), message->header.msgh_remote_port );
}

static void *mach_message_pump( void *args )
{
    unsigned int tid, count, body_count;
    mach_msg_return_t mr;
    mach_register_message_t receive_message = { 0 };
    sigset_t set;

    sigfillset( &set );
    pthread_sigmask( SIG_BLOCK, &set, NULL );

    for (;;)
    {
        mr = receive_mach_msg( &receive_message );
        if (mr != MACH_MSG_SUCCESS)
        {
            fprintf( stderr, "msync: failed to receive message\n");
            continue;
        }

        /*
         * A complex mach message, where the client expects a reply,
         * is a send back a mach memory entry request.
         */
        if (receive_message.header.msgh_remote_port != MACH_PORT_NULL)
        {
            if (receive_message.header.msgh_id == MSYNC_CLEANUP_MESSAGE_ID)
                cleanup_thread_registration( (mach_cleanup_message_t *)&receive_message );
            else
                send_shm_to_client( (mach_map_message_t *)&receive_message );
            continue;
        }

        /* Header-only messages carry a 28-bit shared index and a signal or close flag. */
        if (receive_message.header.msgh_size == sizeof(mach_msg_header_t))
        {
            unsigned int message_id = receive_message.header.msgh_id;
            unsigned int shm_idx = message_id & MSYNC_SHM_INDEX_MASK;

            if (message_id & MSYNC_SHM_CLOSE_FLAG)
                destroy_all_internal( shm_idx );
            else
                signal_all_internal( shm_idx );
            continue;
        }

        /* Finally, registration and ordered unregister messages. */
        decode_msgh_id( receive_message.header.msgh_id, &tid, &count );
        body_count = (receive_message.header.msgh_size - sizeof(mach_msg_header_t)) /
                     sizeof(receive_message.shm_idx[0]);

        if (body_count == 1)
        {
            acknowledge_unregister( tid, receive_message.shm_idx[0] );
            continue;
        }

        if (!count || count > MAXIMUM_WAIT_OBJECTS + 1 || count + 1 != body_count)
        {
            fprintf( stderr, "msync: error: invalid wait registration size %u/%u for tid %u\n",
                     count, body_count, tid );
            if (body_count)
                publish_wait_state( tid, receive_message.shm_idx[0],
                                    wait_token_with_state( receive_message.shm_idx[0], MSYNC_WAIT_FAILED ) );
            continue;
        }

        register_wait( &receive_message, tid, count );
    }

    return NULL;
}

int do_msync(void)
{
    static int do_msync_cached = -1;

    if (do_msync_cached == -1)
    {
        do_msync_cached = getenv("WINEMSYNC") && atoi(getenv("WINEMSYNC"));
    }

    return do_msync_cached;
}

static void set_thread_policy_qos( mach_port_t mach_thread_id )
{
    thread_extended_policy_data_t extended_policy;
    thread_precedence_policy_data_t precedence_policy;
    int throughput_qos, latency_qos;
    kern_return_t kr;

    latency_qos = LATENCY_QOS_TIER_0;
    kr = thread_policy_set( mach_thread_id, THREAD_LATENCY_QOS_POLICY,
                            (thread_policy_t)&latency_qos,
                            THREAD_LATENCY_QOS_POLICY_COUNT);
    if (kr != KERN_SUCCESS)
        fprintf( stderr, "msync: error setting thread latency QoS.\n" );

    throughput_qos = THROUGHPUT_QOS_TIER_0;
    kr = thread_policy_set( mach_thread_id, THREAD_THROUGHPUT_QOS_POLICY,
                            (thread_policy_t)&throughput_qos,
                            THREAD_THROUGHPUT_QOS_POLICY_COUNT);
    if (kr != KERN_SUCCESS)
        fprintf( stderr, "msync: error setting thread throughput QoS.\n" );

    extended_policy.timeshare = 0;
    kr = thread_policy_set( mach_thread_id, THREAD_EXTENDED_POLICY,
                            (thread_policy_t)&extended_policy,
                            THREAD_EXTENDED_POLICY_COUNT );
    if (kr != KERN_SUCCESS)
        fprintf( stderr, "msync: error setting extended policy\n" );

    precedence_policy.importance = 63;
    kr = thread_policy_set( mach_thread_id, THREAD_PRECEDENCE_POLICY,
                            (thread_policy_t)&precedence_policy,
                            THREAD_PRECEDENCE_POLICY_COUNT );
    if (kr != KERN_SUCCESS)
        fprintf( stderr, "msync: error setting precedence policy\n" );
}

void msync_init_shm(void)
{
    kern_return_t kr;

    if (!do_msync()) return;

    kr = mach_vm_map( mach_task_self(), (mach_vm_address_t *)&shm_tid_map, shm_tid_size, 0, VM_FLAGS_ANYWHERE,
                      MACH_PORT_NULL, 0, FALSE, VM_PROT_DEFAULT, VM_PROT_DEFAULT, VM_INHERIT_SHARE );

    if (kr != KERN_SUCCESS)
    {
        fprintf( stderr, "msync: error: mach_vm_map failed with %d: %s\n", kr, mach_error_string( kr ) );
        fatal_error( "could not map tid shared memory\n" );
    }
}

void msync_cleanup_thread( thread_id_t tid )
{
    mach_cleanup_message_t message = {0};
    mach_cleanup_reply_t reply = {0};
    mach_port_t reply_port = MACH_PORT_NULL;
    mach_msg_return_t mr;
    kern_return_t kr;

    if (!do_msync() || !tid) return;

    if ((kr = mach_port_allocate( mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &reply_port )) != KERN_SUCCESS)
        fatal_error( "could not allocate msync cleanup reply port: %s\n", mach_error_string( kr ) );
    if ((kr = mach_port_insert_right( mach_task_self(), reply_port, reply_port,
                                      MACH_MSG_TYPE_MAKE_SEND )) != KERN_SUCCESS)
    {
        mach_port_destroy( mach_task_self(), reply_port );
        fatal_error( "could not insert msync cleanup reply right: %s\n", mach_error_string( kr ) );
    }

    message.header.msgh_bits = MACH_MSGH_BITS_SET( MACH_MSG_TYPE_COPY_SEND,
                                                   MACH_MSG_TYPE_COPY_SEND, 0, 0 );
    message.header.msgh_size = sizeof(message) - sizeof(message.trailer);
    message.header.msgh_remote_port = receive_port;
    message.header.msgh_local_port = reply_port;
    message.header.msgh_id = MSYNC_CLEANUP_MESSAGE_ID;
    message.tid = tid;

    mr = mach_msg_overwrite( &message.header, MACH_SEND_MSG | MACH_RCV_MSG,
                             message.header.msgh_size, sizeof(reply), reply_port,
                             MACH_MSG_TIMEOUT_NONE, MACH_PORT_NULL, &reply.header, 0 );
    kr = mach_port_destroy( mach_task_self(), reply_port );
    if (mr != MACH_MSG_SUCCESS)
        fatal_error( "msync thread cleanup failed for tid %u: %#x\n", tid, mr );
    if (kr != KERN_SUCCESS)
        fatal_error( "could not destroy msync cleanup reply port: %s\n", mach_error_string( kr ) );
}

void msync_init(void)
{
    struct stat st;
    mach_port_t bootstrap_port;
    mach_port_limits_t limits;
    void *dlhandle = dlopen( NULL, RTLD_NOW );
    pthread_t message_thread;
    char message_port_name[64];

    if (!do_msync()) return;

    if (fstat( config_dir_fd, &st ) == -1)
        fatal_error( "cannot stat config dir\n" );

    snprintf( message_port_name, sizeof(message_port_name), "wine-%" PRIxMAX "-msync-v%u",
              (uintmax_t)st.st_ino, MSYNC_SHM_WIRE_VERSION );

    /* Bootstrap mach server message pump */

    mach_msg2_trap = (mach_msg2_trap_ptr_t)dlsym( dlhandle, "mach_msg2_trap" );
    if (!mach_msg2_trap)
        fprintf( stderr, "msync: warning: using mach_msg instead of mach_msg2\n");
    dlclose( dlhandle );

    MACH_CHECK_ERROR(mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &receive_port), "mach_port_allocate");

    MACH_CHECK_ERROR(mach_port_insert_right(mach_task_self(), receive_port, receive_port, MACH_MSG_TYPE_MAKE_SEND), "mach_port_insert_right");

    limits.mpl_qlimit = 50;

    if (getenv("WINEMSYNC_QLIMIT"))
        limits.mpl_qlimit = atoi(getenv("WINEMSYNC_QLIMIT"));

    MACH_CHECK_ERROR(mach_port_set_attributes( mach_task_self(), receive_port, MACH_PORT_LIMITS_INFO,
                                        (mach_port_info_t)&limits, MACH_PORT_LIMITS_INFO_COUNT), "mach_port_set_attributes");

    MACH_CHECK_ERROR(task_get_special_port(mach_task_self(), TASK_BOOTSTRAP_PORT, &bootstrap_port), "task_get_special_port");

    MACH_CHECK_ERROR(bootstrap_register2(bootstrap_port, message_port_name, receive_port, 0), "bootstrap_register2");

    if (pthread_create( &message_thread, NULL, mach_message_pump, NULL ))
    {
        perror("pthread_create");
        fatal_error( "could not create mach message pump thread\n" );
    }

    set_thread_policy_qos( pthread_mach_thread_np( message_thread )) ;

    fprintf( stderr, "msync: bootstrapped mach port on %s.\n", message_port_name );

    fprintf( stderr, "msync: up and running.\n" );
}

static struct list mutex_list = LIST_INIT(mutex_list);

void msync_destroy( struct msync *msync )
{
    struct msync_shm *shm = get_shm( msync->shm_idx );

    if (shm->msync_type == MSYNC_MUTEX)
        list_remove( &msync->mutex_entry );
    if (!msync->shm_idx) return;

    destroy_all( msync->shm_idx );
    free( msync );
}

static void *get_shm( unsigned int idx )
{
    unsigned int entry = idx / MSYNC_SHM_OBJECTS_PER_PAGE;
    unsigned int offset = (idx % MSYNC_SHM_OBJECTS_PER_PAGE) * MSYNC_SHM_OBJECT_SIZE;

    void *page;

    if (entry >= MSYNC_SHM_MAX_PAGES)
        fatal_error( "invalid msync shm index %u\n", idx );

    if (!(page = __atomic_load_n( &shm_addrs[entry], __ATOMIC_ACQUIRE )))
    {
        kern_return_t kr;
        mach_vm_address_t address = 0;

        kr = mach_vm_map( mach_task_self(), &address, MSYNC_SHM_PAGE_SIZE, 0, VM_FLAGS_ANYWHERE,
                          MACH_PORT_NULL, 0, FALSE, VM_PROT_DEFAULT, VM_PROT_DEFAULT, VM_INHERIT_SHARE );
        if (kr != KERN_SUCCESS)
            fatal_error( "could not map msync shm page %u for index %u: %d: %s\n",
                         entry, idx, kr, mach_error_string( kr ) );
        memset( (void *)address, 0, MSYNC_SHM_PAGE_SIZE );

        if (debug_level)
            fprintf( stderr, "msync: Mapping page %u at %llu.\n", entry, address );

        page = NULL;
        if (!__atomic_compare_exchange_n( &shm_addrs[entry], &page, (void *)address, 0,
                                          __ATOMIC_RELEASE, __ATOMIC_ACQUIRE ))
            mach_vm_deallocate( mach_task_self(), address, MSYNC_SHM_PAGE_SIZE );
        else
            page = (void *)address;
    }

    return (void *)((unsigned long)page + offset);
}

static unsigned int msync_alloc_shm( int low, int high, enum msync_type type )
{
    unsigned int shm_idx;
    struct msync_shm *shm;
    int allocated_new = 0;

    pthread_mutex_lock( &shm_index_mutex );
    if (free_shm_idx != UINT32_MAX)
    {
        shm_idx = free_shm_idx;
        shm = get_shm( shm_idx );
        free_shm_idx = shm->low;
    }
    else
    {
        if (next_unused_shm_idx >= MSYNC_SHM_INDEX_COUNT)
            fatal_error( "msync shared object index space exhausted\n" );
        shm_idx = next_unused_shm_idx++;
        shm = get_shm( shm_idx );
        allocated_new = 1;
    }

    assert( shm && !__atomic_load_n( &shm->refcount, __ATOMIC_SEQ_CST ) );
    shm->low = low;
    shm->high = high;
    shm->msync_type = type;
    shm->multiple_waiters = 0;
    __atomic_store_n( &shm->refcount, 1, __ATOMIC_RELEASE );
    if (allocated_new && !(shm_idx % MSYNC_SHM_OBJECTS_PER_PAGE) &&
        getenv("WINE_MSYNC_TEST_TRACE") && getenv("WINE_MSYNC_TEST_TRACE")[0] == '1' &&
        !getenv("WINE_MSYNC_TEST_TRACE")[1])
        fprintf( stderr, "msync: MSYNC_SHM_HIGH_WATER index %u page %u\n", shm_idx,
                 shm_idx / MSYNC_SHM_OBJECTS_PER_PAGE );
    pthread_mutex_unlock( &shm_index_mutex );

    return shm_idx;
}

struct msync *create_msync( int low, int high, enum msync_type type )
{
    struct msync *msync = mem_alloc( sizeof(struct msync) );

    if (msync)
    {
        msync->shm_idx = msync_alloc_shm( low, high, type );
        if (type == MSYNC_MUTEX)
            list_add_tail( &mutex_list, &msync->mutex_entry );
    }

    return msync;
}

/* shm layout for events or event-like objects. */
struct msync_event
{
    int signaled;
    int unused;
    unsigned short msync_type;
    unsigned short refcount;
    int multiple_waiters;
};

void msync_set_event( struct msync *msync )
{
    struct msync_event *event = get_shm( msync->shm_idx );

    if (!__atomic_exchange_n( &event->signaled, 1, __ATOMIC_SEQ_CST ))
        signal_all( msync->shm_idx, (int *)event );
}

void msync_reset_event( struct msync *msync )
{
    struct msync_event *event = get_shm( msync->shm_idx );

    __atomic_store_n( &event->signaled, 0, __ATOMIC_SEQ_CST );
}

struct mutex
{
    int tid;
    int count;  /* recursion count */
    unsigned short msync_type;
    unsigned short refcount;
    int multiple_waiters;
};

void msync_abandon_mutexes( thread_id_t tid )
{
    struct msync *msync;

    LIST_FOR_EACH_ENTRY( msync, &mutex_list, struct msync, mutex_entry )
    {
        struct mutex *mutex = get_shm( msync->shm_idx );

        if (mutex->tid == tid)
        {
            if (debug_level)
                fprintf( stderr, "msync_abandon_mutexes() idx=%d\n", msync->shm_idx );
            mutex->tid = ~0;
            mutex->count = 0;
            signal_all ( msync->shm_idx, (int *)mutex );
        }
    }
}

int msync_grab_object( struct msync *msync )
{
    struct msync_shm *obj = get_shm( msync->shm_idx );
    unsigned short refs = __atomic_load_n( &obj->refcount, __ATOMIC_RELAXED );

    do
    {
        if (!refs)
            fatal_error( "cannot grab destroyed msync object %u\n", msync->shm_idx );
        if (refs == USHRT_MAX) return 0;
    } while (!__atomic_compare_exchange_n( &obj->refcount, &refs, refs + 1, 1,
                                           __ATOMIC_SEQ_CST, __ATOMIC_RELAXED ));
    return 1;
}

#else /* __APPLE__ */

int do_msync(void)
{
    return 0;
}

void msync_init_shm(void)
{
}

void msync_init(void)
{
}

void msync_cleanup_thread( thread_id_t tid )
{
}

#endif /* __APPLE__ */
