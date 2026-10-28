#!/usr/bin/env python3
"""Exercise production Wine callbacks and the NSObject owner with native release-only teardown."""

from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

PREAMBLE = r'''
#import <Foundation/Foundation.h>
#include <dispatch/dispatch.h>
#include <CoreFoundation/CoreFoundation.h>
#include <assert.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

typedef void *HWND;
typedef void *HANDLE;
typedef unsigned int COLORREF;
typedef void *MTLDevice_id;
typedef void CAMetalLayer;
typedef struct { int left, top, right, bottom; } RECT;
#define TRUE 1
#define FALSE 0
#define TRACE(...) do { if (0) printf(__VA_ARGS__); } while (0)

struct client_surface { HWND hwnd; int format; unsigned int ref; };
struct WineMetalView;
typedef struct WineContentView
{
    struct client_surface *surface;
    struct WineMetalView *metal;
    unsigned int ref;
} WineContentView;
struct device { unsigned int refs; };
typedef struct WineMetalView { WineContentView *parent; struct device *device; } WineMetalView;
typedef struct WineWindow { int unused; } WineWindow;
struct macdrv_client_surface
{
    struct client_surface client;
    WineContentView *cocoa_view;
};
struct macdrv_win_data
{
    HWND hwnd;
    WineWindow *cocoa_window;
    WineContentView *client_view;
    struct { RECT window, visible, client; } rects;
    HANDLE drag_event;
    unsigned int on_screen, shaped, layered, ulw_layered, per_pixel_alpha, minimized;
    CFMutableArrayRef d3dmetal_client_surfaces;
};

static CFMutableDictionaryRef win_datas;
static pthread_mutex_t win_mutex;
static _Thread_local unsigned int win_lock_depth;
static struct macdrv_win_data *window;
static unsigned int live_surfaces, live_content_views, live_metal_views, live_devices;
static unsigned int native_creates, native_releases, device_creates, device_releases;
static int fail_native_create, fail_tracker, fail_owner, fail_device, fail_snapshot, fail_array;
static void (*after_native_create)(void);
static dispatch_semaphore_t cleanup_done, state_released, release_started, allow_release;
static int pause_release;

void macdrv_destroy_d3dmetal_view_owner(void *context);

/* A separate host thread executes every native create/release/dispose request.
 * Condition signals, never sleeps, bracket both directions of the handoff. */
static pthread_t native_thread;
static pthread_mutex_t request_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t request_cond = PTHREAD_COND_INITIALIZER;
static pthread_cond_t complete_cond = PTHREAD_COND_INITIALIZER;
static void (*request)(void *);
static void *request_arg;
static int stopping;

static void bounded_wait(pthread_cond_t *cond)
{
    struct timespec deadline;
    assert(!clock_gettime(CLOCK_REALTIME, &deadline));
    deadline.tv_sec += 5;
    assert(!pthread_cond_timedwait(cond, &request_mutex, &deadline));
}

static void *native_loop(void *unused)
{
    (void)unused;
    assert(!pthread_mutex_lock(&request_mutex));
    for (;;)
    {
        while (!request && !stopping) bounded_wait(&request_cond);
        if (stopping) break;
        request(request_arg);
        request = NULL;
        assert(!pthread_cond_signal(&complete_cond));
    }
    assert(!pthread_mutex_unlock(&request_mutex));
    return NULL;
}

static void on_native_thread(void (*job)(void *), void *arg)
{
    assert(!win_lock_depth); /* Native dispatch must never inherit the HWND lock. */
    assert(!pthread_equal(pthread_self(), native_thread));
    assert(!pthread_mutex_lock(&request_mutex));
    assert(!request);
    request_arg = arg;
    request = job;
    assert(!pthread_cond_signal(&request_cond));
    while (request) bounded_wait(&complete_cond);
    assert(!pthread_mutex_unlock(&request_mutex));
}

static struct macdrv_win_data *get_win_data(HWND hwnd)
{
    struct macdrv_win_data *data;
    if (!hwnd) return NULL;
    assert(!pthread_mutex_lock(&win_mutex));
    data = (void *)CFDictionaryGetValue(win_datas, hwnd);
    if (data) { win_lock_depth++; return data; }
    assert(!pthread_mutex_unlock(&win_mutex));
    return NULL;
}

static void release_win_data(struct macdrv_win_data *data)
{
    if (!data) return;
    assert(win_lock_depth);
    win_lock_depth--;
    assert(!pthread_mutex_unlock(&win_mutex));
}

static struct macdrv_client_surface *impl_from_client_surface(struct client_surface *surface)
{
    return (void *)surface;
}

static void release_content(void *arg)
{
    WineContentView *view = arg;
    assert(view->ref);
    if (!--view->ref)
    {
        assert(!view->metal && !view->surface);
        free(view);
        live_content_views--;
    }
}

static void dispose_content(void *arg)
{
    WineContentView *view = arg;
    assert(!view->surface);
    release_content(arg);
}

static void *macdrv_retain_d3dmetal_content_view(WineContentView *view)
{
    assert(view->ref);
    view->ref++;
    return view;
}

static void macdrv_release_d3dmetal_content_view(WineContentView *view)
{
    on_native_thread(release_content, view);
    dispatch_semaphore_signal(cleanup_done);
}

static void macdrv_set_view_d3dmetal_client_surface(WineContentView *view, void *surface)
{
    view->surface = surface;
}

static void client_surface_add_ref(struct client_surface *surface)
{
    assert(surface->ref);
    surface->ref++;
}

static void client_surface_release(struct client_surface *surface)
{
    struct macdrv_client_surface *impl = impl_from_client_surface(surface);
    struct macdrv_win_data *data;
    assert(!win_lock_depth); /* Final detach reacquires the HWND lock. */
    assert(surface->ref);
    if (--surface->ref) return;
    if (surface->hwnd)
    {
        macdrv_set_view_d3dmetal_client_surface(impl->cocoa_view, NULL);
        data = get_win_data(surface->hwnd);
        if (data && data->client_view == impl->cocoa_view) data->client_view = NULL;
        release_win_data(data);
        on_native_thread(dispose_content, impl->cocoa_view);
    }
    free(surface);
    live_surfaces--;
}

static struct client_surface *macdrv_CreateClientSurface(HWND hwnd, int format, BOOL raw)
{
    struct macdrv_client_surface *surface = calloc(1, sizeof(*surface));
    (void)raw;
    assert(surface);
    surface->client = (struct client_surface){hwnd, format, 1};
    surface->cocoa_view = calloc(1, sizeof(*surface->cocoa_view));
    assert(surface->cocoa_view);
    surface->cocoa_view->ref = 1;
    live_surfaces++;
    live_content_views++;
    window->client_view = surface->cocoa_view;
    return &surface->client;
}

static WineWindow *macdrv_get_cocoa_window(HWND hwnd, BOOL require_on_screen)
{
    (void)hwnd;
    (void)require_on_screen;
    return NULL;
}

static MTLDevice_id macdrv_create_metal_device(void)
{
    struct device *device;
    if (fail_device) return NULL;
    device = calloc(1, sizeof(*device));
    assert(device);
    device->refs = 1;
    live_devices++;
    device_creates++;
    return device;
}

static void release_device_ref(struct device *device)
{
    if (!device) return;
    assert(device->refs);
    if (--device->refs) return;
    free(device);
    live_devices--;
}

static void macdrv_release_metal_device(MTLDevice_id device)
{
    if (!device) return;
    device_releases++;
    release_device_ref(device);
}

struct native_create_args { WineContentView *content; WineMetalView *view; struct device *device; };
static void create_native(void *arg)
{
    struct native_create_args *args = arg;
    assert(pthread_equal(pthread_self(), native_thread));
    assert(!args->content->metal);
    if (args->content->surface) assert(args->content->surface->ref);
    native_creates++;
    if (fail_native_create || !args->device) return;
    args->view = calloc(1, sizeof(*args->view));
    assert(args->view);
    args->view->parent = args->content;
    args->view->device = args->device;
    assert(args->device->refs);
    args->device->refs++;
    args->content->metal = args->view;
    live_metal_views++;
}

static WineMetalView *macdrv_view_create_metal_view(WineContentView *view, MTLDevice_id device)
{
    struct native_create_args args = {view, NULL, device};
    on_native_thread(create_native, &args);
    if (after_native_create) after_native_create();
    return args.view;
}

static void release_native(void *arg)
{
    WineMetalView *view = arg;
    assert(pthread_equal(pthread_self(), native_thread));
    assert(view->parent->metal == view);
    if (view->parent->surface) assert(view->parent->surface->ref);
    if (pause_release)
    {
        dispatch_semaphore_signal(release_started);
        assert(!dispatch_semaphore_wait(allow_release, dispatch_time(DISPATCH_TIME_NOW, 5 * NSEC_PER_SEC)));
    }
    view->parent->metal = NULL;
    release_device_ref(view->device);
    free(view);
    live_metal_views--;
    native_releases++;
}

static void macdrv_view_release_metal_view(WineMetalView *view)
{
    on_native_thread(release_native, view);
}

static CAMetalLayer *macdrv_view_get_metal_layer(WineMetalView *view)
{
    if (!view) return NULL; /* The native provider also asks for a nil view's layer. */
    assert(view->parent->metal == view);
    return (void *)view;
}

static void *tracker_malloc(size_t size)
{
    return fail_tracker ? NULL : malloc(size);
}

static void *snapshot_calloc(size_t count, size_t size)
{
    return fail_snapshot ? NULL : calloc(count, size);
}

static CFMutableArrayRef surface_array_create(CFAllocatorRef allocator, CFIndex capacity,
                                              const CFArrayCallBacks *callbacks)
{
    return fail_array ? NULL : CFArrayCreateMutable(allocator, capacity, callbacks);
}

static void NtSetEvent(HANDLE event, void *unused) { (void)event; (void)unused; abort(); }
static WineWindow *detach_cocoa_window(struct macdrv_win_data *data)
{
    assert(win_lock_depth);
    (void)data;
    return NULL;
}
static void macdrv_destroy_cocoa_window(WineWindow *view) { (void)view; abort(); }
'''

DRIVER = r'''
static HWND hwnd = (void *)0x1234;

static void new_window(void)
{
    assert(!window);
    window = calloc(1, sizeof(*window));
    assert(window);
    window->hwnd = hwnd;
    CFDictionarySetValue(win_datas, hwnd, window);
}

static void destroy_window(void)
{
    assert(window);
    macdrv_DestroyWindow(hwnd);
    window = NULL;
}

static CFIndex array_count(void)
{
    return window && window->d3dmetal_client_surfaces ? CFArrayGetCount(window->d3dmetal_client_surfaces) : 0;
}

static WineContentView *prepare_owner(void)
{
    struct d3dmetal_macdrv_win_data *data = my_get_win_data(hwnd);
    WineContentView *owner;
    struct metal_view_surface *entry;
    assert(data && win_lock_depth == 1);
    owner = data->client_cocoa_view;
    entry = macdrv_get_d3dmetal_view_owner(owner);
    assert((void *)owner != (void *)entry->surface->cocoa_view);
    /* This is the actual native ABI: objc_retain(snapshot + 24), release the
     * snapshot, create device/view, and eventually only objc_release(owner). */
    [(id)owner retain];
    my_release_win_data(data);
    assert(!win_lock_depth);
    assert(entry->surface->client.ref == 2);
    return owner;
}

static void assert_clean(void)
{
    assert(!array_count());
    assert(!metal_view_surfaces);
    assert(!live_surfaces && !live_content_views && !live_metal_views && !live_devices);
    assert(device_creates == device_releases);
    assert(!win_lock_depth);
}

static void *release_from_worker(void *arg)
{
    @autoreleasepool { [(id)arg release]; }
    return NULL;
}

static void await_cleanup(void)
{
    assert(!dispatch_semaphore_wait(cleanup_done, dispatch_time(DISPATCH_TIME_NOW, 5 * NSEC_PER_SEC)));
}

static void release_owner(WineContentView *owner, int on_main)
{
    pthread_t worker;
    if (on_main) [(id)owner release];
    else
    {
        assert(!pthread_create(&worker, NULL, release_from_worker, owner));
        assert(!pthread_join(worker, NULL));
    }
    await_cleanup();
    assert(!dispatch_semaphore_wait(state_released, dispatch_time(DISPATCH_TIME_NOW, 5 * NSEC_PER_SEC)));
}

static WineMetalView *create_view(WineContentView *owner)
{
    return my_macdrv_view_create_metal_view(owner, my_macdrv_create_metal_device());
}

static void failure_loop(int device, int outstanding)
{
    unsigned int i;
    new_window();
    fail_device = device;
    fail_native_create = !device;
    for (i = 0; i < 128; i++)
    {
        WineContentView *owner = prepare_owner();
        struct metal_view_surface *entry = macdrv_get_d3dmetal_view_owner(owner);
        struct client_surface *surface = &entry->surface->client;
        WineContentView *content = entry->surface->cocoa_view;
        assert(array_count() == 1);
        if (outstanding) client_surface_add_ref(surface);
        assert(!create_view(owner));
        assert(!my_macdrv_view_get_metal_layer(NULL));
        release_owner(owner, i & 1);
        assert(!array_count() && !metal_view_surfaces && !live_metal_views && !live_devices);
        if (outstanding)
        {
            assert(live_surfaces == 1 && live_content_views == 1);
            assert(surface->ref == 1 && content->surface == surface);
            client_surface_release(surface);
        }
        assert_clean();
    }
    assert(native_creates == 128 && !native_releases);
    destroy_window();
}

static void success_loop(int mode)
{
    unsigned int i;
    new_window();
    for (i = 0; i < 128; i++)
    {
        WineContentView *owner = prepare_owner();
        struct metal_view_surface *entry = macdrv_get_d3dmetal_view_owner(owner);
        WineContentView *content = entry->surface->cocoa_view;
        WineMetalView *view;
        if (mode == 4) destroy_window();
        view = create_view(owner);
        assert(view && my_macdrv_view_get_metal_layer(view));
        if (mode != 4) assert(content->surface->ref == 2 && array_count() == 1);
        if (mode == 2)
        {
            destroy_window();
        }
        if (mode == 2 || mode == 4)
            assert(content->surface->ref == 1 && live_surfaces == 1 && live_metal_views == 1);
        if (mode == 3) my_macdrv_view_release_metal_view(view);
        release_owner(owner, mode == 1);
        assert_clean();
        if (mode == 2 || mode == 4) new_window();
    }
    destroy_window();
    assert(native_creates == 128 && native_releases == 128);
}

static void failure_with_active_view(int allocation)
{
    WineContentView *owner, *content;
    struct metal_view_surface *entry;
    WineMetalView *view;
    new_window();
    owner = prepare_owner();
    entry = macdrv_get_d3dmetal_view_owner(owner);
    content = entry->surface->cocoa_view;
    view = create_view(owner);
    assert(view && content->surface->ref == 2);
    if (allocation)
    {
        fail_tracker = 1;
        assert(!my_get_win_data(hwnd));
    }
    else
    {
        WineContentView *failed = prepare_owner();
        fail_native_create = 1;
        assert(!create_view(failed));
        release_owner(failed, 1);
    }
    assert(array_count() == 1 && live_surfaces == 1 && live_content_views == 1 && live_metal_views == 1);
    assert(CFArrayGetValueAtIndex(window->d3dmetal_client_surfaces, 0) == content->surface);
    assert(content->surface->ref == 2 && view->parent == content);
    release_owner(owner, 0);
    assert_clean();
    destroy_window();
}

static void destroy_during_create(int failure)
{
    WineContentView *owner;
    WineMetalView *view;
    new_window();
    owner = prepare_owner();
    fail_native_create = failure == 1;
    fail_device = failure == 2;
    after_native_create = destroy_window;
    view = create_view(owner);
    after_native_create = NULL;
    assert(!window);
    if (!failure)
    {
        assert(view && view->parent->surface->ref == 1);
    }
    else assert(!view);
    release_owner(owner, 1);
    assert_clean();
}

static void allocation_failure(int which)
{
    unsigned int i;
    new_window();
    fail_tracker = which == 0;
    fail_owner = which == 1;
    fail_snapshot = which == 2;
    fail_array = which == 3;
    for (i = 0; i < 128; i++)
    {
        assert(!my_get_win_data(hwnd));
        assert_clean();
    }
    destroy_window();
}

static void abandon_snapshot(int retained)
{
    unsigned int i;
    new_window();
    for (i = 0; i < 128; i++)
    {
        if (retained) release_owner(prepare_owner(), i & 1);
        else
        {
            struct d3dmetal_macdrv_win_data *data = my_get_win_data(hwnd);
            assert(data);
            my_release_win_data(data);
            await_cleanup();
            assert(!dispatch_semaphore_wait(state_released, dispatch_time(DISPATCH_TIME_NOW, 5 * NSEC_PER_SEC)));
        }
        assert_clean();
    }
    destroy_window();
}

static void coexist_and_reuse_hwnd(int reuse)
{
    WineContentView *owners[16];
    WineMetalView *views[16];
    WineContentView *fresh_owner = NULL;
    WineMetalView *fresh_view = NULL;
    unsigned int i;
    new_window();
    for (i = 0; i < 16; i++)
    {
        owners[i] = prepare_owner();
        views[i] = create_view(owners[i]);
        assert(views[i]);
    }
    assert(array_count() == 16 && live_metal_views == 16);
    if (reuse)
    {
        destroy_window();
        new_window();
        fresh_owner = prepare_owner();
        fresh_view = create_view(fresh_owner);
        assert(fresh_view && array_count() == 1);
    }
    for (i = 0; i < 16; i++)
    {
        assert(views[i]->parent->metal == views[i]);
        release_owner(owners[i], i & 1);
        assert(array_count() == (reuse ? 1 : 15 - i));
        assert(live_metal_views == (reuse ? 16 - i : 15 - i));
        if (reuse)
        {
            assert(fresh_view->parent->metal == fresh_view);
            assert(CFArrayGetValueAtIndex(window->d3dmetal_client_surfaces, 0) == fresh_view->parent->surface);
        }
    }
    if (reuse) release_owner(fresh_owner, 1);
    assert_clean();
    destroy_window();
}

static void *explicit_release_worker(void *view)
{
    my_macdrv_view_release_metal_view(view);
    return NULL;
}

static void detached_surface(int before_create)
{
    WineContentView *owner;
    WineContentView *content;
    struct metal_view_surface *entry;
    struct macdrv_win_data *data;
    WineMetalView *view = NULL;
    new_window();
    owner = prepare_owner();
    entry = macdrv_get_d3dmetal_view_owner(owner);
    content = entry->surface->cocoa_view;
    assert(content->ref == 2);
    if (!before_create) view = create_view(owner);
    /* win32u can detach a live surface, disposing its original NSView
     * reference and clearing surface->hwnd before the token is released. */
    data = get_win_data(hwnd);
    if (data->client_view == content) data->client_view = NULL;
    release_win_data(data);
    content->surface = NULL;
    entry->surface->client.hwnd = NULL;
    on_native_thread(dispose_content, content);
    assert(content->ref == 1 && array_count() == 1);
    if (before_create) view = create_view(owner);
    assert(view && view->parent == content);
    release_owner(owner, 1);
    assert_clean();
    destroy_window();
}

static void concurrent_explicit_release(void)
{
    WineContentView *owner;
    WineMetalView *view;
    pthread_t worker;
    new_window();
    owner = prepare_owner();
    view = create_view(owner);
    assert(view);
    pause_release = 1;
    assert(!pthread_create(&worker, NULL, explicit_release_worker, view));
    assert(!dispatch_semaphore_wait(release_started, dispatch_time(DISPATCH_TIME_NOW, 5 * NSEC_PER_SEC)));
    /* The explicit release has pinned the state before the native reference dies. */
    [(id)owner release];
    assert(!dispatch_semaphore_wait(state_released, dispatch_time(DISPATCH_TIME_NOW, 5 * NSEC_PER_SEC)));
    assert(live_surfaces == 1 && live_metal_views == 1 && live_devices == 1);
    dispatch_semaphore_signal(allow_release);
    assert(!pthread_join(worker, NULL));
    await_cleanup();
    assert_clean();
    destroy_window();
}

int main(int argc, char **argv)
{
    pthread_mutexattr_t attr;
    assert(argc == 2);
    assert([NSThread isMainThread]);
    cleanup_done = dispatch_semaphore_create(0);
    state_released = dispatch_semaphore_create(0);
    release_started = dispatch_semaphore_create(0);
    allow_release = dispatch_semaphore_create(0);
    assert(!pthread_mutexattr_init(&attr));
    assert(!pthread_mutexattr_settype(&attr, PTHREAD_MUTEX_RECURSIVE));
    assert(!pthread_mutex_init(&win_mutex, &attr));
    assert(!pthread_mutexattr_destroy(&attr));
    win_datas = CFDictionaryCreateMutable(NULL, 0, NULL, NULL);
    assert(win_datas);
    assert(!pthread_create(&native_thread, NULL, native_loop, NULL));
    my_macdrv_init_display_devices(FALSE);
    assert(!my_macdrv_get_cocoa_window(hwnd, FALSE));
    my_macdrv_release_metal_device(my_macdrv_create_metal_device());
    if (!strcmp(argv[1], "null")) failure_loop(0, 0);
    else if (!strcmp(argv[1], "device-null")) failure_loop(1, 0);
    else if (!strcmp(argv[1], "null-outstanding")) failure_loop(0, 1);
    else if (!strcmp(argv[1], "device-null-outstanding")) failure_loop(1, 1);
    else if (!strcmp(argv[1], "success")) success_loop(0);
    else if (!strcmp(argv[1], "main-release")) success_loop(1);
    else if (!strcmp(argv[1], "destroy-first")) success_loop(2);
    else if (!strcmp(argv[1], "explicit-release")) success_loop(3);
    else if (!strcmp(argv[1], "destroy-before-create")) success_loop(4);
    else if (!strcmp(argv[1], "null-active")) failure_with_active_view(0);
    else if (!strcmp(argv[1], "tracker-active")) failure_with_active_view(1);
    else if (!strcmp(argv[1], "destroy-during-success")) destroy_during_create(0);
    else if (!strcmp(argv[1], "destroy-during-null")) destroy_during_create(1);
    else if (!strcmp(argv[1], "destroy-during-device-null")) destroy_during_create(2);
    else if (!strcmp(argv[1], "tracker")) allocation_failure(0);
    else if (!strcmp(argv[1], "owner-null")) allocation_failure(1);
    else if (!strcmp(argv[1], "snapshot-null")) allocation_failure(2);
    else if (!strcmp(argv[1], "array-null")) allocation_failure(3);
    else if (!strcmp(argv[1], "abandon-snapshot")) abandon_snapshot(0);
    else if (!strcmp(argv[1], "abandon-retained")) abandon_snapshot(1);
    else if (!strcmp(argv[1], "coexisting")) coexist_and_reuse_hwnd(0);
    else if (!strcmp(argv[1], "reuse-hwnd")) coexist_and_reuse_hwnd(1);
    else if (!strcmp(argv[1], "detach-before-create")) detached_surface(1);
    else if (!strcmp(argv[1], "detach-active")) detached_surface(0);
    else if (!strcmp(argv[1], "concurrent-explicit-release")) concurrent_explicit_release();
    else return 2;
    assert_clean();
    assert(!window && !CFDictionaryGetCount(win_datas));
    assert(!pthread_mutex_lock(&request_mutex));
    stopping = 1;
    assert(!pthread_cond_signal(&request_cond));
    assert(!pthread_mutex_unlock(&request_mutex));
    assert(!pthread_join(native_thread, NULL));
    CFRelease(win_datas);
    assert(!pthread_mutex_destroy(&win_mutex));
    dispatch_release(cleanup_done);
    dispatch_release(state_released);
    dispatch_release(release_started);
    dispatch_release(allow_release);
    printf("WINEMAC_METAL_SURFACE %s surfaces=%u content=%u metal=%u devices=%u creates=%u releases=%u\n",
           argv[1], live_surfaces, live_content_views, live_metal_views, live_devices, native_creates, native_releases);
    return 0;
}
'''


class MetalSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="winemac-metal-surface-")
        output = Path(cls.temporary.name)
        source = (ROOT / "dlls/winemac.drv/d3dmetal.c").read_text()
        snapshot = source[source.index("struct d3dmetal_macdrv_win_data\n{"):]
        snapshot = snapshot[:snapshot.index("\n};") + 3]
        callbacks = source[source.index("struct metal_view_surface\n{"):source.index("static void my_OnMainThread(")]
        objc = (ROOT / "dlls/winemac.drv/d3dmetal_objc.m").read_text()
        owner = objc[objc.index("@interface WineD3DMetalViewOwner"):objc.index("@implementation WineMetalLayer")]
        window = (ROOT / "dlls/winemac.drv/window.c").read_text()
        destroy = window[window.index("void macdrv_DestroyWindow(HWND hwnd)"):]
        destroy = destroy[:destroy.index("\n}") + 2]
        fixture = output / "surface.m"
        fixture.write_text(
            PREAMBLE + owner + r'''
static void *owner_create_probe(void *context, WineContentView *view)
{
    return fail_owner ? NULL : macdrv_create_d3dmetal_view_owner(context, view);
}
''' + snapshot + r'''
#define malloc tracker_malloc
#define calloc snapshot_calloc
#define CFArrayCreateMutable surface_array_create
#define macdrv_create_d3dmetal_view_owner owner_create_probe
#define macdrv_destroy_d3dmetal_view_owner production_destroy_d3dmetal_view_owner
''' + callbacks + r'''
#undef malloc
#undef calloc
#undef CFArrayCreateMutable
#undef macdrv_create_d3dmetal_view_owner
#undef macdrv_destroy_d3dmetal_view_owner
void macdrv_destroy_d3dmetal_view_owner(void *context)
{
    assert(![NSThread isMainThread]);
    production_destroy_d3dmetal_view_owner(context);
    dispatch_semaphore_signal(state_released);
}
''' + destroy + DRIVER)
        cls.driver = output / "surface"
        cls.environment = {**os.environ, "RUN_TMP": str(output), "TMPDIR": str(output),
                           "NODE_COMPILE_CACHE": str(output / "node-cache"),
                           "BUN_RUNTIME_TRANSPILER_CACHE_PATH": str(output / "bun-cache")}
        try:
            subprocess.run(["xcrun", "clang", "-std=gnu11", "-fblocks", "-Wall", "-Wextra", "-Werror",
                            str(fixture), "-framework", "CoreFoundation", "-framework", "Foundation",
                            "-pthread", "-o", str(cls.driver)], env=cls.environment,
                           check=True, timeout=60)
        except BaseException:
            cls.temporary.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_lifetime_boundaries(self):
        for case in ("null", "device-null", "null-outstanding", "device-null-outstanding",
                     "success", "main-release", "explicit-release", "destroy-first",
                     "destroy-before-create", "null-active", "tracker-active",
                     "destroy-during-success", "destroy-during-null", "destroy-during-device-null",
                     "tracker", "owner-null", "snapshot-null", "array-null",
                     "abandon-snapshot", "abandon-retained", "coexisting", "reuse-hwnd",
                     "detach-before-create", "detach-active", "concurrent-explicit-release"):
            with self.subTest(case=case):
                subprocess.run([str(self.driver), case], env=self.environment, check=True, timeout=15)


if __name__ == "__main__":
    unittest.main()
