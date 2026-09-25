/* Opt-in cursor diagnostics. No changes to input delivery or server protocol.
 *
 * Define WINE_CURSOR_TRACE_IMPLEMENTATION and WINE_CURSOR_TRACE_MODULE in one
 * Unix translation unit per DLL. Hidden symbols keep each DLL's ring separate.
 * The mapping lives until process exit, including while other threads unwind.
 */
#ifndef __WINE_CURSOR_TRACE_H
#define __WINE_CURSOR_TRACE_H

#include <stdint.h>

enum wine_cursor_trace_kind
{
    WCT_CLIP = 1, WCT_SET_POS, WCT_WARP, WCT_WARP_MATCH, WCT_TAP,
    WCT_COCOA, WCT_FILTER, WCT_QUEUE_NEW, WCT_QUEUE_MERGE, WCT_QUEUE_DROP,
    WCT_QUEUE_TAKE, WCT_DRIVER, WCT_ACCUM, WCT_SEND, WCT_REGISTER,
    WCT_RAW_READ, WCT_RAW_BUFFER, WCT_CURSOR_POS, WCT_FOCUS, WCT_DISPLAY,
    WCT_GEOMETRY, WCT_APP_MESSAGE
};

enum wine_cursor_trace_flags
{
    WCT_BEFORE = 1, WCT_AFTER = 2, WCT_SUCCESS = 4, WCT_NOOP = 8,
    WCT_RETINA = 16, WCT_CONFINEMENT = 32, WCT_EVENT_TAP = 64,
    WCT_OLD = 128, WCT_ZERO = 256, WCT_ABSOLUTE = 512,
    WCT_SIZE_ONLY = 1024, WCT_HEADER_ONLY = 2048, WCT_ERROR = 4096,
    WCT_HOST_QUERY = 8192, WCT_TIME_NS = 16384, WCT_TIME_MS = 32768,
    WCT_TRANSITION = 0x80000000u
};

/* All integers are native little-endian on the supported macOS hosts.
 * sequence is published last with release semantics; UINT64_MAX means busy.
 * source_time preserves the input timestamp; clock_ns is CLOCK_MONOTONIC.
 * object/related are diagnostic identities, never injected into input payloads.
 */
struct wine_cursor_trace_record
{
    uint64_t sequence, clock_ns, thread_id, object, related;
    double source_time;
    uint64_t epoch;
    uint32_t kind, flags;
    double value[8];
};

struct wine_cursor_trace_header
{
    char magic[8];
    uint32_t version, record_size, capacity, header_size;
    uint64_t pid, start_ns, next_sequence, dropped, epoch;
    char module[16];
    char reserved[48];
};

extern int wine_cursor_trace_active __attribute__((visibility("hidden")));
extern uint64_t wine_cursor_trace_emit(uint32_t kind, uint32_t flags, uint64_t object,
                                      uint64_t related, double source_time, const double value[8])
    __attribute__((visibility("hidden")));

/* Arguments, including floating-point conversion, are not evaluated when off. */
#define CURSOR_TRACE(kind, flags, object, related, time, a, b, c, d, e, f, g, h) \
    do { if (wine_cursor_trace_active) { \
        const double wct_values[8] = {a, b, c, d, e, f, g, h}; \
        wine_cursor_trace_emit(kind, flags, (uint64_t)(uintptr_t)(object), \
                               (uint64_t)(uintptr_t)(related), time, wct_values); \
    } } while (0)

#ifdef WINE_CURSOR_TRACE_IMPLEMENTATION

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#ifndef WINE_CURSOR_TRACE_CAPACITY
#define WINE_CURSOR_TRACE_CAPACITY 65536u
#endif

typedef char wct_record_size_check[sizeof(struct wine_cursor_trace_record) == 128 ? 1 : -1];
typedef char wct_header_size_check[sizeof(struct wine_cursor_trace_header) == 128 ? 1 : -1];

int wine_cursor_trace_active;
static struct wine_cursor_trace_header *wct_header;
static struct wine_cursor_trace_record *wct_records;

static void wine_cursor_trace_after_fork(void)
{
    /* A fork child must not publish into the parent process's shared mapping. */
    wine_cursor_trace_active = 0;
}

static void __attribute__((constructor)) wine_cursor_trace_init(void)
{
    const char *directory = getenv("YAAGL_CURSOR_TRACE");
    struct timespec now;
    struct wine_cursor_trace_header *header;
    uint64_t ns;
    size_t size = sizeof(*header) + WINE_CURSOR_TRACE_CAPACITY * sizeof(*wct_records);
    char name[128];
    int dir_fd = -1, fd = -1, saved_errno = errno;

    if (!directory || !*directory) return;
    if (*directory != '/') { errno = EINVAL; goto failed; }
    if (clock_gettime(CLOCK_MONOTONIC, &now)) goto failed;
    ns = (uint64_t)now.tv_sec * 1000000000 + now.tv_nsec;
    snprintf(name, sizeof(name), "cursor-%u-%s-%llu.bin", (unsigned)getpid(),
             WINE_CURSOR_TRACE_MODULE, (unsigned long long)ns);
    if ((dir_fd = open(directory, O_RDONLY | O_DIRECTORY | O_CLOEXEC)) < 0) goto failed;
    if ((fd = openat(dir_fd, name, O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600)) < 0)
        goto failed;
    if (ftruncate(fd, size)) goto failed;
    header = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (header == MAP_FAILED) goto failed;

    /* Fault in the bounded mapping at module load, not on the input path. */
    memset(header, 0, size);
    header->version = 1;
    header->record_size = sizeof(*wct_records);
    header->capacity = WINE_CURSOR_TRACE_CAPACITY;
    header->header_size = sizeof(*header);
    header->pid = getpid();
    header->start_ns = ns;
    memcpy(header->module, WINE_CURSOR_TRACE_MODULE, sizeof(WINE_CURSOR_TRACE_MODULE));
    memcpy(header->magic, "YACUR01", 8);
    wct_header = header;
    wct_records = (struct wine_cursor_trace_record *)(header + 1);
    pthread_atfork(NULL, NULL, wine_cursor_trace_after_fork);
    wine_cursor_trace_active = 1;
    close(fd);
    close(dir_fd);
    errno = saved_errno;
    return;

failed:
    fprintf(stderr, "cursor trace (%s): cannot initialize %s: %s\n",
            WINE_CURSOR_TRACE_MODULE, directory, strerror(errno));
    if (fd >= 0) { close(fd); unlinkat(dir_fd, name, 0); }
    if (dir_fd >= 0) close(dir_fd);
    errno = saved_errno;
}

uint64_t wine_cursor_trace_emit(uint32_t kind, uint32_t flags, uint64_t object,
                               uint64_t related, double source_time, const double value[8])
{
    struct wine_cursor_trace_record *record;
    struct timespec now;
    uint64_t sequence, previous, thread_id;
    int saved_errno = errno;

    if (!wine_cursor_trace_active) return 0;
    sequence = __atomic_add_fetch(&wct_header->next_sequence, 1, __ATOMIC_RELAXED);
    record = &wct_records[(sequence - 1) % WINE_CURSOR_TRACE_CAPACITY];
    previous = __atomic_load_n(&record->sequence, __ATOMIC_ACQUIRE);
    /* A producer never waits for a preempted producer, nor overwrites newer data. */
    if (previous == UINT64_MAX || previous >= sequence ||
        !__atomic_compare_exchange_n(&record->sequence, &previous, UINT64_MAX, 0,
                                     __ATOMIC_ACQ_REL, __ATOMIC_RELAXED))
    {
        __atomic_fetch_add(&wct_header->dropped, 1, __ATOMIC_RELAXED);
        return 0;
    }
    clock_gettime(CLOCK_MONOTONIC, &now);
#ifdef __APPLE__
    pthread_threadid_np(NULL, &thread_id);
#else
    thread_id = (uintptr_t)pthread_self();
#endif
    if (flags & WCT_TRANSITION) __atomic_fetch_add(&wct_header->epoch, 1, __ATOMIC_RELAXED);
    record->clock_ns = (uint64_t)now.tv_sec * 1000000000 + now.tv_nsec;
    record->thread_id = thread_id;
    record->object = object;
    record->related = related;
    record->source_time = source_time;
    record->epoch = __atomic_load_n(&wct_header->epoch, __ATOMIC_RELAXED);
    record->kind = kind;
    record->flags = flags;
    memcpy(record->value, value, sizeof(record->value));
    __atomic_store_n(&record->sequence, sequence, __ATOMIC_RELEASE);
    errno = saved_errno;
    return sequence;
}
#endif /* WINE_CURSOR_TRACE_IMPLEMENTATION */
#endif /* __WINE_CURSOR_TRACE_H */
