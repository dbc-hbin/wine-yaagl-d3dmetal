#!/usr/bin/env python3
"""Compile and execute Wine's actual surface/window functions with real CFArray and mutexes."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    "d3dmetal": ROOT / "dlls/winemac.drv/d3dmetal.c",
    "macdrv": ROOT / "dlls/winemac.drv/window.c",
    "win32u": ROOT / "dlls/win32u/window.c",
}


def body(source: str, name: str) -> str:
    match = re.search(r"(?m)^(?:static )?(?:struct [\w]+ \*|void |BOOL |macdrv_metal_view )"
                      + re.escape(name) + r"\s*\([^;]*?\)\s*\{", source)
    if match is None:
        raise RuntimeError(f"missing production function {name}")
    start = source.rfind("\n", 0, match.start()) + 1
    depth = 1
    for offset in range(match.end(), len(source)):
        depth += (source[offset] == "{") - (source[offset] == "}")
        if depth == 0:
            return source[start:offset + 1]
    raise RuntimeError(f"unterminated production function {name}")


# The fixture supplies only platform dependencies. The ownership/locking code is
# extracted from the build inputs below, never copied or reimplemented in the test.
FIXTURE = r'''
#include <CoreFoundation/CoreFoundation.h>
#include <pthread.h>
#include <stdatomic.h>
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef void *HWND;
typedef void *macdrv_window;
typedef void *macdrv_view;
typedef void *macdrv_metal_view;
typedef void *macdrv_metal_device;
typedef int BOOL;
typedef unsigned long ULONG;
typedef unsigned long COLORREF;
typedef void *HANDLE;
typedef struct { int left, top, right, bottom; } RECT;
#define TRUE 1
#define FALSE 0
#define GA_ROOT 2
#define TRACE(...) ((void)0)
#define CONTAINING_RECORD(pointer, type, member) ((type *)((char *)(pointer) - __builtin_offsetof(type, member)))
struct client_surface;
struct client_surface_funcs { void (*detach)(struct client_surface *); void (*destroy)(struct client_surface *); void (*update)(struct client_surface *); };
struct client_surface {
    _Atomic int ref;
    HWND hwnd, toplevel;
    RECT virtual_rect, monitor_rect;
    BOOL updated;
    const struct client_surface_funcs *funcs;
    void *entry;
};
struct macdrv_client_surface { struct client_surface client; macdrv_view cocoa_view; };
struct macdrv_win_data {
    HWND hwnd;
    CFMutableArrayRef d3dmetal_client_surfaces;
    macdrv_window cocoa_window;
    macdrv_view client_view;
    struct { RECT window, visible, client; } rects;
    int pixel_format;
    void *drag_event;
    unsigned int on_screen, shaped, layered, ulw_layered, per_pixel_alpha, minimized;
};
@@PRIVATE_STRUCT@@
struct metal_view_surface {
    macdrv_metal_view view;
    struct macdrv_client_surface *surface;
    struct metal_view_surface *next;
};
static pthread_mutex_t win_data_mutex, surfaces_lock;
static pthread_mutex_t metal_view_surfaces_mutex = PTHREAD_MUTEX_INITIALIZER;
static struct metal_view_surface *metal_view_surfaces;
static CFMutableDictionaryRef win_datas;
static pthread_mutex_t race_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t race_cond = PTHREAD_COND_INITIALIZER;
static int race_arrived;
static void rendezvous(void) {
    pthread_mutex_lock(&race_mutex);
    if (++race_arrived == 2) pthread_cond_broadcast(&race_cond);
    while (race_arrived < 2) pthread_cond_wait(&race_cond, &race_mutex);
    pthread_mutex_unlock(&race_mutex);
}
static _Atomic int race_mode, destroyed, disposed, released_views;
static _Thread_local int role, window_depth, surfaces_depth;
static BOOL native_view_fails;
static int observed_lock(pthread_mutex_t *mutex) {
    /* Entering surfaces_lock from beneath win_data_mutex is the regression. */
    assert(!(mutex == &surfaces_lock && window_depth));
    int result = pthread_mutex_lock(mutex);
    assert(!result);
    if (mutex == &win_data_mutex) window_depth++;
    if (mutex == &surfaces_lock) surfaces_depth++;
    if (atomic_load(&race_mode) &&
        ((role == 1 && mutex == &win_data_mutex) || (role == 2 && mutex == &surfaces_lock))) {
        rendezvous();
    }
    return result;
}
static int observed_unlock(pthread_mutex_t *mutex) {
    if (mutex == &win_data_mutex) { assert(window_depth); window_depth--; }
    if (mutex == &surfaces_lock) { assert(surfaces_depth); surfaces_depth--; }
    return pthread_mutex_unlock(mutex);
}
static void list_remove(void *entry) { (void)entry; }
static ULONG InterlockedIncrement(_Atomic int *ref) { return atomic_fetch_add(ref, 1) + 1; }
static ULONG InterlockedDecrement(_Atomic int *ref) { return atomic_fetch_sub(ref, 1) - 1; }
static HWND NtUserGetAncestor(HWND hwnd, int flag) { (void)flag; return hwnd; }
static RECT get_client_surface_rects(HWND top, HWND hwnd, RECT *monitor) {
    (void)top; (void)hwnd; *monitor = (RECT){0, 0, 64, 64}; return *monitor;
}
static BOOL EqualRect(const RECT *a, const RECT *b) { return !memcmp(a, b, sizeof(*a)); }
static RECT cgrect_from_rect(RECT rect) { return rect; }
static void macdrv_set_view_frame(macdrv_view view, RECT rect) { (void)view; (void)rect; }
static void macdrv_set_view_superview(macdrv_view view, macdrv_view parent, macdrv_window window,
                                      void *a, void *b) { (void)view; (void)parent; (void)window; (void)a; (void)b; }
static struct macdrv_client_surface *impl_from_client_surface(struct client_surface *client) {
    return CONTAINING_RECORD(client, struct macdrv_client_surface, client);
}
static void macdrv_dispose_view(macdrv_view view) { (void)view; atomic_fetch_add(&disposed, 1); }
static void destroy_cocoa_window(struct macdrv_win_data *data) { (void)data; }
static void NtSetEvent(void *event, void *previous) { (void)event; (void)previous; }
static void macdrv_set_view_d3dmetal_client_surface(macdrv_view view, void *client) {
    assert(view == client);
}
static void *macdrv_get_view_d3dmetal_client_surface(macdrv_view view) { return view; }
static macdrv_metal_view macdrv_view_create_metal_view(macdrv_view view, macdrv_metal_device device) {
    (void)device; return native_view_fails ? NULL : view;
}
static void macdrv_view_release_metal_view(macdrv_metal_view view) {
    (void)view; atomic_fetch_add(&released_views, 1);
}
static void client_surface_add_ref(struct client_surface *surface);
static void client_surface_release(struct client_surface *surface);
static struct macdrv_win_data *get_win_data(HWND hwnd);
static void release_win_data(struct macdrv_win_data *data);
static void macdrv_client_surface_detach(struct client_surface *client);
static void macdrv_client_surface_update(struct client_surface *client);
static void client_surface_update_locked(struct client_surface *client);
static void surface_destroy(struct client_surface *client) {
    assert(!atomic_load(&client->ref));
    atomic_fetch_add(&destroyed, 1);
}
static const struct client_surface_funcs callbacks = {
    .detach = macdrv_client_surface_detach, .destroy = surface_destroy, .update = macdrv_client_surface_update,
};
static struct client_surface *macdrv_CreateClientSurface(HWND hwnd, int format, BOOL raw) {
    (void)format; (void)raw;
    struct macdrv_client_surface *surface = calloc(1, sizeof(*surface));
    assert(surface);
    atomic_init(&surface->client.ref, 1);
    surface->client.hwnd = surface->client.toplevel = hwnd;
    surface->client.funcs = &callbacks;
    surface->cocoa_view = &surface->client;
    return &surface->client;
}
#define pthread_mutex_lock observed_lock
#define pthread_mutex_unlock observed_unlock
@@FUNCTIONS@@
#undef pthread_mutex_lock
#undef pthread_mutex_unlock
static struct macdrv_win_data *new_window(HWND hwnd) {
    struct macdrv_win_data *data = calloc(1, sizeof(*data));
    assert(data);
    data->hwnd = hwnd;
    if (!win_datas) win_datas = CFDictionaryCreateMutable(NULL, 0, NULL, NULL);
    CFDictionarySetValue(win_datas, hwnd, data);
    return data;
}
static struct macdrv_client_surface *new_surface(HWND hwnd, int refs) {
    struct macdrv_client_surface *surface = impl_from_client_surface(macdrv_CreateClientSurface(hwnd, 0, FALSE));
    atomic_store(&surface->client.ref, refs);
    return surface;
}
static void native_failure(void) {
    HWND hwnd = (HWND)0x101;
    new_window(hwnd);
    native_view_fails = TRUE;
    struct d3dmetal_macdrv_win_data *bridge = my_get_win_data(hwnd);
    assert(bridge);
    struct macdrv_win_data *data = bridge->padding[0];
    struct macdrv_client_surface *surface = bridge->padding[1];
    assert(CFArrayGetCount(data->d3dmetal_client_surfaces) == 1);
    assert(CFArrayGetValueAtIndex(data->d3dmetal_client_surfaces, 0) == surface);
    assert(!my_macdrv_view_create_metal_view(surface->cocoa_view, NULL));
    my_release_win_data(bridge);
    assert(atomic_load(&destroyed) == 1 && atomic_load(&disposed) == 1);
    macdrv_DestroyWindow(hwnd);
    assert(atomic_load(&destroyed) == 1);
    native_view_fails = FALSE;
}
static void active_view(void) {
    HWND hwnd = (HWND)0x102;
    new_window(hwnd);
    struct d3dmetal_macdrv_win_data *bridge = my_get_win_data(hwnd);
    assert(bridge);
    struct macdrv_win_data *data = bridge->padding[0];
    struct macdrv_client_surface *surface = bridge->padding[1];
    macdrv_metal_view view = my_macdrv_view_create_metal_view(surface->cocoa_view, NULL);
    assert(view && atomic_load(&surface->client.ref) == 2);
    my_release_win_data(bridge);
    assert(CFArrayGetCount(data->d3dmetal_client_surfaces) == 1);
    assert(atomic_load(&surface->client.ref) == 2);
    my_macdrv_view_release_metal_view(view);
    assert(CFArrayGetCount(data->d3dmetal_client_surfaces) == 0);
    assert(atomic_load(&destroyed) == 2 && atomic_load(&disposed) == 2);
    macdrv_DestroyWindow(hwnd);
    assert(atomic_load(&destroyed) == 2);
}
static void destroy_before_view_release(void) {
    HWND hwnd = (HWND)0x105;
    new_window(hwnd);
    struct d3dmetal_macdrv_win_data *bridge = my_get_win_data(hwnd);
    assert(bridge);
    struct macdrv_client_surface *surface = bridge->padding[1];
    macdrv_metal_view view = my_macdrv_view_create_metal_view(surface->cocoa_view, NULL);
    assert(view);
    my_release_win_data(bridge);
    assert(atomic_load(&surface->client.ref) == 2);
    macdrv_DestroyWindow(hwnd);
    assert(atomic_load(&surface->client.ref) == 1);
    assert(atomic_load(&destroyed) == 2);
    my_macdrv_view_release_metal_view(view);
    assert(atomic_load(&destroyed) == 3 && atomic_load(&disposed) == 3);
}
static void destroy_entries(void) {
    HWND hwnd = (HWND)0x103;
    struct macdrv_win_data *data = new_window(hwnd);
    struct macdrv_client_surface *first = new_surface(hwnd, 1), *second = new_surface(hwnd, 1);
    data->d3dmetal_client_surfaces = CFArrayCreateMutable(NULL, 0, NULL);
    CFArrayAppendValue(data->d3dmetal_client_surfaces, first);
    CFArrayAppendValue(data->d3dmetal_client_surfaces, second);
    macdrv_DestroyWindow(hwnd);
    assert(atomic_load(&destroyed) == 5 && atomic_load(&disposed) == 5);
}
static struct macdrv_client_surface *racing_surface;
static HWND racing_hwnd = (HWND)0x104;
static void *removing_thread(void *unused) {
    (void)unused;
    role = 1;
    struct macdrv_win_data *data = get_win_data(racing_hwnd);
    assert(data && remove_window_surface(data, racing_surface));
    assert(atomic_load(&racing_surface->client.ref) == 2);
    assert(CFArrayGetCount(data->d3dmetal_client_surfaces) == 0);
    assert(!remove_window_surface(data, racing_surface));
    release_win_data(data);
    client_surface_release(&racing_surface->client);
    return NULL;
}
static void *updating_thread(void *unused) {
    (void)unused;
    role = 2;
    client_surface_update(&racing_surface->client);
    return NULL;
}
static void race_forward_progress(void) {
    struct macdrv_win_data *data = new_window(racing_hwnd);
    racing_surface = new_surface(racing_hwnd, 2); /* array entry plus live updater */
    data->d3dmetal_client_surfaces = CFArrayCreateMutable(NULL, 0, NULL);
    CFArrayAppendValue(data->d3dmetal_client_surfaces, racing_surface);
    atomic_store(&race_mode, 1);
    pthread_t remove_thread, update_thread;
    assert(!pthread_create(&remove_thread, NULL, removing_thread, NULL));
    assert(!pthread_create(&update_thread, NULL, updating_thread, NULL));
    assert(!pthread_join(remove_thread, NULL));
    assert(!pthread_join(update_thread, NULL));
    atomic_store(&race_mode, 0);
    assert(race_arrived == 2);
    assert(atomic_load(&racing_surface->client.ref) == 1);
    client_surface_release(&racing_surface->client);
    macdrv_DestroyWindow(racing_hwnd);
    assert(atomic_load(&destroyed) == 6 && atomic_load(&disposed) == 6);
}
int main(void) {
    pthread_mutexattr_t attr;
    pthread_mutexattr_init(&attr);
    pthread_mutexattr_settype(&attr, PTHREAD_MUTEX_RECURSIVE);
    pthread_mutex_init(&win_data_mutex, &attr);
    pthread_mutexattr_destroy(&attr);
    pthread_mutex_init(&surfaces_lock, NULL);
    native_failure();
    active_view();
    destroy_before_view_release();
    destroy_entries();
    race_forward_progress();
    CFRelease(win_datas);
    puts("WINEMAC_SURFACE_LOCKS_PASS: failure, active view, destroy-before-view-release, destroy, race; 6 balanced releases");
    return 0;
}
'''


def main() -> None:
    text = {key: path.read_text() for key, path in SOURCES.items()}
    private = re.search(r"struct d3dmetal_macdrv_win_data\s*\{.*?\n\};", text["d3dmetal"], re.S)
    if private is None:
        raise RuntimeError("missing D3DMetal window data")
    selected = {
        "win32u": ("client_surface_detach_locked", "client_surface_release_locked",
                   "client_surface_add_ref", "client_surface_release", "client_surface_update_locked",
                   "client_surface_update"),
        "macdrv": ("get_win_data", "release_win_data", "macdrv_client_surface_detach",
                   "macdrv_client_surface_update", "macdrv_DestroyWindow"),
        "d3dmetal": ("remove_window_surface", "my_get_win_data", "my_release_win_data",
                     "my_macdrv_view_create_metal_view", "my_macdrv_view_release_metal_view"),
    }
    functions = "\n\n".join(body(text[key], name) for key, names in selected.items() for name in names)
    # Include the old production CF callback, if present, so restoring it
    # fails on the lock-order invariant rather than on an undefined symbol.
    try:
        functions = body(text["d3dmetal"], "cf_client_surface_release") + "\n\n" + functions
    except RuntimeError:
        pass
    source = FIXTURE.replace("@@PRIVATE_STRUCT@@", private.group()).replace("@@FUNCTIONS@@", functions)
    with tempfile.TemporaryDirectory(prefix="winemac-surface-locks-") as directory:
        fixture = Path(directory) / "production.c"
        executable = fixture.with_suffix("")
        fixture.write_text(source)
        subprocess.run(["xcrun", "clang", "-arch", "x86_64", "-std=gnu11", "-O1", "-Wall",
                        "-Wextra", "-Werror", "-Wno-unused-function", "-Wno-unused-variable", str(fixture), "-framework",
                        "CoreFoundation", "-o", str(executable)], check=True)
        subprocess.run(["codesign", "--force", "--sign", "-", str(executable)], check=True,
                       capture_output=True)
        result = subprocess.run([str(executable)], check=True, capture_output=True, text=True, timeout=20)
        print(result.stdout.strip())
    for name, path in SOURCES.items():
        print(f"{name} sha256={hashlib.sha256(path.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
