#!/usr/bin/env python3
"""Compile the production Wine callback lifetime code against host ownership probes."""

from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

PREAMBLE = r'''
#include <CoreFoundation/CoreFoundation.h>
#include <assert.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

typedef int BOOL;
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
typedef struct WineMetalView { WineContentView *parent; } WineMetalView;
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
static unsigned int live_surfaces, live_content_views, live_metal_views, native_creates, native_releases;
static int fail_native_create, fail_tracker;
static void (*after_native_create)(void);

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
    assert(!view->metal); /* No active native view may lose its parent surface. */
    assert(!view->surface);
    release_content(arg);
}

static void provider_release_content(WineContentView *view)
{
    on_native_thread(release_content, view);
}

static void macdrv_set_view_d3dmetal_client_surface(WineContentView *view, void *surface)
{
    view->surface = surface;
}

static void *macdrv_get_view_d3dmetal_client_surface(WineContentView *view)
{
    return view->surface;
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
    macdrv_set_view_d3dmetal_client_surface(impl->cocoa_view, NULL);
    data = get_win_data(surface->hwnd);
    if (data && data->client_view == impl->cocoa_view) data->client_view = NULL;
    release_win_data(data);
    on_native_thread(dispose_content, impl->cocoa_view);
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

static MTLDevice_id macdrv_create_metal_device(void) { return (void *)1; }
static void macdrv_release_metal_device(MTLDevice_id device) { assert(device == (void *)1); }

struct native_create_args { WineContentView *content; WineMetalView *view; };
static void create_native(void *arg)
{
    struct native_create_args *args = arg;
    assert(pthread_equal(pthread_self(), native_thread));
    assert(!args->content->metal);
    if (args->content->surface) assert(args->content->surface->ref);
    native_creates++;
    if (fail_native_create) return;
    args->view = calloc(1, sizeof(*args->view));
    assert(args->view);
    args->view->parent = args->content;
    args->content->metal = args->view;
    live_metal_views++;
}

static WineMetalView *macdrv_view_create_metal_view(WineContentView *view, MTLDevice_id device)
{
    struct native_create_args args = {view, NULL};
    assert(device == (void *)1);
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
    view->parent->metal = NULL;
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

static WineContentView *prepare_surface(void)
{
    struct d3dmetal_macdrv_win_data *data = my_get_win_data(hwnd);
    WineContentView *view;
    assert(data && win_lock_depth == 1);
    view = data->client_cocoa_view;
    /* WineSwapchainCallbacks::InitializeForHWND retains the content view before
     * releasing the snapshot, independently of the client-surface reference. */
    view->ref++;
    /* Preserve the native release-before-create callback order. */
    my_release_win_data(data);
    assert(!win_lock_depth);
    assert(view->surface->ref == 1);
    return view;
}

static void assert_clean(void)
{
    assert(!array_count());
    assert(!metal_view_surfaces);
    assert(!live_surfaces && !live_content_views && !live_metal_views);
    assert(!win_lock_depth);
}

static void *release_from_worker(void *arg)
{
    WineContentView *content = ((WineMetalView *)arg)->parent;
    my_macdrv_view_release_metal_view(arg);
    provider_release_content(content);
    return NULL;
}

static void failure_loop(int tracker, int outstanding)
{
    unsigned int i;
    new_window();
    fail_tracker = tracker;
    fail_native_create = !tracker;
    for (i = 0; i < 128; i++)
    {
        WineContentView *content = prepare_surface();
        struct client_surface *surface = content->surface;
        assert(array_count() == 1);
        if (outstanding) client_surface_add_ref(surface);
        assert(!my_macdrv_view_create_metal_view(content, (void *)1));
        assert(!my_macdrv_view_get_metal_layer(NULL));
        assert(!array_count() && !metal_view_surfaces && !live_metal_views);
        if (outstanding)
        {
            assert(live_surfaces == 1 && live_content_views == 1);
            assert(surface->ref == 1 && content->surface == surface);
            client_surface_release(surface);
        }
        assert(!content->surface && content->ref == 1);
        provider_release_content(content);
        assert_clean();
    }
    assert(native_creates == 128);
    assert(native_releases == (tracker ? 128u : 0u));
    destroy_window();
}

static void success_loop(int destroy_first)
{
    unsigned int i;
    new_window();
    for (i = 0; i < 128; i++)
    {
        WineContentView *content = prepare_surface();
        WineMetalView *view = my_macdrv_view_create_metal_view(content, (void *)1);
        pthread_t worker;
        assert(view && my_macdrv_view_get_metal_layer(view));
        assert(content->surface->ref == 2 && array_count() == 1);
        if (destroy_first)
        {
            destroy_window();
            assert(content->surface->ref == 1 && live_surfaces == 1 && live_metal_views == 1);
        }
        assert(!pthread_create(&worker, NULL, release_from_worker, view));
        assert(!pthread_join(worker, NULL));
        assert_clean();
        if (destroy_first) new_window();
    }
    destroy_window();
    assert(native_creates == 128 && native_releases == 128);
}

static void failure_with_active_view(int tracker)
{
    WineContentView *content, *failed_content;
    WineMetalView *view;
    new_window();
    content = prepare_surface();
    view = my_macdrv_view_create_metal_view(content, (void *)1);
    assert(view && content->surface->ref == 2);
    failed_content = prepare_surface();
    fail_native_create = !tracker;
    fail_tracker = tracker;
    assert(!my_macdrv_view_create_metal_view(failed_content, (void *)1));
    provider_release_content(failed_content);
    assert(array_count() == 1 && live_surfaces == 1 && live_content_views == 1 && live_metal_views == 1);
    assert(CFArrayGetValueAtIndex(window->d3dmetal_client_surfaces, 0) == content->surface);
    assert(content->surface->ref == 2 && view->parent == content);
    my_macdrv_view_release_metal_view(view);
    provider_release_content(content);
    assert_clean();
    destroy_window();
}

static void destroy_during_create(int failure)
{
    WineContentView *content;
    WineMetalView *view;
    new_window();
    content = prepare_surface();
    fail_native_create = failure == 1;
    fail_tracker = failure == 2;
    after_native_create = destroy_window;
    view = my_macdrv_view_create_metal_view(content, (void *)1);
    after_native_create = NULL;
    assert(!window);
    if (!failure)
    {
        assert(view && content->surface->ref == 1);
        my_macdrv_view_release_metal_view(view);
    }
    else assert(!view);
    provider_release_content(content);
    assert_clean();
}

static void no_surface(int failure)
{
    WineContentView content = {0};
    WineMetalView *view;
    fail_native_create = failure;
    view = my_macdrv_view_create_metal_view(&content, (void *)1);
    if (failure) assert(!view);
    else
    {
        assert(view && !metal_view_surfaces);
        my_macdrv_view_release_metal_view(view);
    }
    assert_clean();
}

int main(int argc, char **argv)
{
    pthread_mutexattr_t attr;
    assert(argc == 2);
    assert(!pthread_mutexattr_init(&attr));
    assert(!pthread_mutexattr_settype(&attr, PTHREAD_MUTEX_RECURSIVE));
    assert(!pthread_mutex_init(&win_mutex, &attr));
    assert(!pthread_mutexattr_destroy(&attr));
    win_datas = CFDictionaryCreateMutable(NULL, 0, NULL, NULL);
    assert(win_datas);
    assert(!pthread_create(&native_thread, NULL, native_loop, NULL));
    my_macdrv_init_display_devices(FALSE);
    assert(!my_macdrv_get_cocoa_window(hwnd, FALSE));
    assert(my_macdrv_create_metal_device() == (void *)1);
    my_macdrv_release_metal_device((void *)1);
    if (!strcmp(argv[1], "null")) failure_loop(0, 0);
    else if (!strcmp(argv[1], "tracker")) failure_loop(1, 0);
    else if (!strcmp(argv[1], "null-outstanding")) failure_loop(0, 1);
    else if (!strcmp(argv[1], "tracker-outstanding")) failure_loop(1, 1);
    else if (!strcmp(argv[1], "success")) success_loop(0);
    else if (!strcmp(argv[1], "destroy-first")) success_loop(1);
    else if (!strcmp(argv[1], "null-active")) failure_with_active_view(0);
    else if (!strcmp(argv[1], "tracker-active")) failure_with_active_view(1);
    else if (!strcmp(argv[1], "destroy-during-success")) destroy_during_create(0);
    else if (!strcmp(argv[1], "destroy-during-null")) destroy_during_create(1);
    else if (!strcmp(argv[1], "destroy-during-tracker")) destroy_during_create(2);
    else if (!strcmp(argv[1], "no-surface")) no_surface(0);
    else if (!strcmp(argv[1], "no-surface-null")) no_surface(1);
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
    printf("WINEMAC_METAL_SURFACE %s surfaces=%u content=%u metal=%u creates=%u releases=%u\n",
           argv[1], live_surfaces, live_content_views, live_metal_views, native_creates, native_releases);
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
        window = (ROOT / "dlls/winemac.drv/window.c").read_text()
        destroy = window[window.index("void macdrv_DestroyWindow(HWND hwnd)"):]
        destroy = destroy[:destroy.index("\n}") + 2]
        fixture = output / "surface.c"
        fixture.write_text(PREAMBLE + snapshot + "\n#define malloc tracker_malloc\n" + callbacks +
                           "\n#undef malloc\n" + destroy + DRIVER)
        cls.driver = output / "surface"
        cls.environment = {**os.environ, "RUN_TMP": str(output), "TMPDIR": str(output),
                           "NODE_COMPILE_CACHE": str(output / "node-cache"),
                           "BUN_RUNTIME_TRANSPILER_CACHE_PATH": str(output / "bun-cache")}
        try:
            subprocess.run(["xcrun", "clang", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
                            str(fixture), "-framework", "CoreFoundation",
                            "-pthread", "-o", str(cls.driver)], env=cls.environment,
                           check=True, timeout=60)
        except BaseException:
            cls.temporary.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_lifetime_boundaries(self):
        for case in ("null", "tracker", "null-outstanding", "tracker-outstanding", "success",
                     "destroy-first", "null-active", "tracker-active", "destroy-during-success",
                     "destroy-during-null", "destroy-during-tracker", "no-surface", "no-surface-null"):
            with self.subTest(case=case):
                subprocess.run([str(self.driver), case], env=self.environment, check=True, timeout=15)


if __name__ == "__main__":
    unittest.main()
