/*
 * Mac graphics driver hooks used by D3DMetal (part of the Apple Game Porting Toolkit)
 *
 * Copyright 2023 Brendan Shanks for CodeWeavers, Inc.
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

#if 0
#pragma makedep unix
#endif

#if defined(__x86_64__)

#include "config.h"

#include "macdrv.h"
#include "../../include/yaagl_d3dmetal_display.h"

WINE_DEFAULT_DEBUG_CHANNEL(macdrv_d3dmtl);

typedef LONG LSTATUS;

struct macdrv_functions_t
{
    void (*macdrv_init_display_devices)(BOOL);
    struct d3dmetal_macdrv_win_data* (*get_win_data)(HWND hwnd);
    void (*release_win_data)(struct d3dmetal_macdrv_win_data *data);
    WineWindow *(*macdrv_get_cocoa_window)(HWND hwnd, BOOL require_on_screen);
    MTLDevice_id (*macdrv_create_metal_device)(void);
    void (*macdrv_release_metal_device)(MTLDevice_id d);
    WineMetalView *(*macdrv_view_create_metal_view)(WineContentView *v, MTLDevice_id d);
    CAMetalLayer *(*macdrv_view_get_metal_layer)(WineMetalView *v);
    void (*macdrv_view_release_metal_view)(WineMetalView *v);
    void (*on_main_thread)(dispatch_block_t b);
    LSTATUS(WINAPI*RegQueryValueExA)(HKEY, LPCSTR, LPDWORD, LPDWORD, BYTE*, LPDWORD);
    LSTATUS(WINAPI*RegSetValueExA)(HKEY, LPCSTR, DWORD, DWORD, const BYTE*, DWORD);
    LSTATUS(WINAPI*RegOpenKeyExA)(HKEY, LPCSTR, DWORD, DWORD, HKEY*);
    LSTATUS(WINAPI*RegCreateKeyExA)(HKEY, LPCSTR, DWORD, LPSTR, DWORD, DWORD, LPSECURITY_ATTRIBUTES, HKEY*, LPDWORD);
    LSTATUS(WINAPI*RegCloseKey)(HKEY);
    BOOL(WINAPI*EnumDisplayMonitors)(HDC,LPRECT,MONITORENUMPROC,LPARAM);
    BOOL(WINAPI*GetMonitorInfoA)(HMONITOR,LPMONITORINFO);
    BOOL(WINAPI*AdjustWindowRectEx)(LPRECT,DWORD,BOOL,DWORD);
    LONG_PTR(WINAPI*GetWindowLongPtrW)(HWND,int);
    BOOL(WINAPI*GetWindowRect)(HWND,LPRECT);
    BOOL(WINAPI*MoveWindow)(HWND,int,int,int,int,BOOL);
    BOOL(WINAPI*SetWindowPos)(HWND,HWND,int,int,int,int,UINT);
    INT(WINAPI*GetSystemMetrics)(INT);
    LONG_PTR(WINAPI*SetWindowLongPtrW)(HWND,INT,LONG_PTR);
};
C_ASSERT(sizeof(struct macdrv_functions_t) == 192);

/* macdrv private window data expected by D3DMetal */
struct d3dmetal_macdrv_win_data
{
    HWND                hwnd;                   /* hwnd that this private data belongs to */
    WineWindow         *cocoa_window;
    WineContentView    *cocoa_view;
    WineContentView    *client_cocoa_view;      /* opaque swapchain owner retained by D3DMetal */
    RECT                window_rect;            /* USER window rectangle relative to parent */
    RECT                whole_rect;             /* Mac window rectangle for the whole window relative to parent */
    RECT                client_rect;            /* client area relative to parent */
    int                 pixel_format;           /* pixel format for GL */
    COLORREF            color_key;              /* color key for layered window; CLR_INVALID is not color keyed */
    HANDLE              drag_event;             /* event to signal that Cocoa-driven window dragging has ended */
    unsigned int        on_screen : 1;          /* is window ordered in? (minimized or not) */
    unsigned int        shaped : 1;             /* is window using a custom region shape? */
    unsigned int        layered : 1;            /* is window layered and with valid attributes? */
    unsigned int        ulw_layered : 1;        /* has UpdateLayeredWindow() been called for window? */
    unsigned int        per_pixel_alpha : 1;    /* is window using per-pixel alpha? */
    unsigned int        minimized : 1;          /* is window minimized? */
    void *              padding[2];             /* used to be struct window_surface* surface/unminimized_surface */
};

C_ASSERT(sizeof(struct d3dmetal_macdrv_win_data) == 120);

void OnMainThread(dispatch_block_t block);

struct metal_view_surface
{
    WineMetalView *view;
    MTLDevice_id device;
    struct macdrv_client_surface *surface;
    HWND hwnd;
    unsigned int refs;
    struct metal_view_surface *next;
};

static pthread_mutex_t metal_view_surfaces_mutex = PTHREAD_MUTEX_INITIALIZER;
static struct metal_view_surface *metal_view_surfaces;

/* The window array owns the original reference; the native owner token owns another.
 * Removing an entry transfers its reference to the caller, which releases it
 * after dropping the window lock (detach reacquires it). */
static BOOL remove_window_surface(struct macdrv_win_data *data, struct macdrv_client_surface *surface)
{
    CFIndex index;

    if (!data || !data->d3dmetal_client_surfaces) return FALSE;
    index = CFArrayGetFirstIndexOfValue(data->d3dmetal_client_surfaces,
                                       CFRangeMake(0, CFArrayGetCount(data->d3dmetal_client_surfaces)), surface);
    if (index != kCFNotFound)
    {
        CFArrayRemoveValueAtIndex(data->d3dmetal_client_surfaces, index);
        return TRUE;
    }
    return FALSE;
}

static void release_metal_view_surface(struct metal_view_surface *entry)
{
    struct metal_view_surface **cursor;
    struct macdrv_win_data *data;
    WineContentView *content;
    BOOL removed;

    pthread_mutex_lock(&metal_view_surfaces_mutex);
    if (--entry->refs)
    {
        pthread_mutex_unlock(&metal_view_surfaces_mutex);
        return;
    }
    for (cursor = &metal_view_surfaces; *cursor; cursor = &(*cursor)->next)
        if (*cursor == entry)
        {
            *cursor = entry->next;
            break;
        }
    pthread_mutex_unlock(&metal_view_surfaces_mutex);

    content = entry->surface->cocoa_view;
    TRACE("destroy owner surface %p view %p device %p\n", entry->surface, entry->view, entry->device);
    if (entry->view) macdrv_view_release_metal_view(entry->view);
    if (entry->device) macdrv_release_metal_device(entry->device);
    data = get_win_data(entry->hwnd);
    removed = remove_window_surface(data, entry->surface);
    release_win_data(data);
    if (removed) client_surface_release(&entry->surface->client);
    client_surface_release(&entry->surface->client);
    macdrv_release_d3dmetal_content_view(content);
    free(entry);
}

/* Called off the Cocoa main thread, including when objc_release() ran there.
 * Final surface detach can acquire the HWND lock and synchronously dispose its
 * Cocoa view, so dispatching only the Metal view teardown would not be enough. */
void macdrv_destroy_d3dmetal_view_owner(void *context)
{
    release_metal_view_surface(context);
}

void macdrv_retain_d3dmetal_client_surface(void *surface)
{
    client_surface_add_ref(surface);
}

void macdrv_release_d3dmetal_client_surface(void *surface)
{
    client_surface_release(surface);
}

static void my_macdrv_init_display_devices(BOOL p1)
{
    TRACE("macdrv_init_display_devices %d - no-op\n", p1);
}

static struct d3dmetal_macdrv_win_data *my_get_win_data(HWND hwnd)
{
    struct macdrv_win_data *data;
    struct d3dmetal_macdrv_win_data *d3dm_data;
    struct macdrv_client_surface *client_surface;
    struct metal_view_surface *entry;
    void *owner;
    TRACE("get_win_data %p\n", hwnd);

    /* Creating a client surface on each call to get_win_data() means it's no longer idempotent,
     * but D3DMetal/DXMT both call it only when creating a new DXGI swapchain.
     * D3DMetal retains client_cocoa_view, then calls release_win_data() before
     * creating its Metal device, view and layer. Its destructor only releases
     * that object: it never calls the view/device release callbacks. Return an
     * NSObject owner token rather than letting the content view outlive them.
     */
    {
        struct client_surface *base = macdrv_CreateClientSurface(hwnd, 0, FALSE);
        if (!base) return NULL;
        client_surface = impl_from_client_surface(base);
    }

    /* get_win_data() needs to happen after client_surface creation to avoid deadlocks */
    data = get_win_data(hwnd);
    if (!data)
    {
        client_surface_release(&client_surface->client);
        return NULL;
    }

    macdrv_set_view_d3dmetal_client_surface(client_surface->cocoa_view, &client_surface->client);

    if (!data->d3dmetal_client_surfaces)
    {
        /* Entries own their original client-surface references, not CF callbacks. */
        data->d3dmetal_client_surfaces = CFArrayCreateMutable(NULL, 0, NULL);
    }
    d3dm_data = calloc(1, sizeof(*d3dm_data));
    entry = malloc(sizeof(*entry));
    if (!data->d3dmetal_client_surfaces || !d3dm_data || !entry)
    {
        release_win_data(data);
        client_surface_release(&client_surface->client);
        free(d3dm_data);
        free(entry);
        return NULL;
    }
    *entry = (struct metal_view_surface){.surface = client_surface, .hwnd = hwnd, .refs = 1};
    if (!(owner = macdrv_create_d3dmetal_view_owner(entry, client_surface->cocoa_view)))
    {
        release_win_data(data);
        client_surface_release(&client_surface->client);
        free(d3dm_data);
        free(entry);
        return NULL;
    }
    client_surface_add_ref(&client_surface->client);
    CFArrayAppendValue(data->d3dmetal_client_surfaces, client_surface);

    d3dm_data->hwnd = data->hwnd;
    d3dm_data->cocoa_window = data->cocoa_window;
    /* cocoa_view is no longer present in macdrv_win_data. D3DMetal doesn't use it. */
    d3dm_data->client_cocoa_view = owner;
    d3dm_data->window_rect = data->rects.window;
    d3dm_data->whole_rect = data->rects.visible;
    d3dm_data->client_rect = data->rects.client;
    d3dm_data->pixel_format = client_surface->client.format;
    /* color_key is no longer present in macdrv_win_data. Assume D3DMetal doesn't use it. */
    d3dm_data->drag_event = data->drag_event;
    d3dm_data->on_screen = data->on_screen;
    d3dm_data->shaped = data->shaped;
    d3dm_data->layered = data->layered;
    d3dm_data->ulw_layered = data->ulw_layered;
    d3dm_data->per_pixel_alpha = data->per_pixel_alpha;
    d3dm_data->minimized = data->minimized;
    /* swap_interval is no longer present in macdrv_win_data. Assume D3DMetal doesn't use it. */
    /* surface/unminimized_surface are no longer present in macdrv_win_data. Assume D3DMetal doesn't use it. */
    d3dm_data->padding[0] = data;
    d3dm_data->padding[1] = client_surface;

    return d3dm_data;
}

static void my_release_win_data(struct d3dmetal_macdrv_win_data *data)
{
    TRACE("release_win_data %p\n", data);
    if (!data) return;

    /* The snapshot and native caller have separate token references. Drop the
     * HWND lock before releasing ours: an abandoned snapshot can detach here. */
    release_win_data(data->padding[0]);
    macdrv_release_d3dmetal_view_owner(data->client_cocoa_view);
    free(data);
}

static WineWindow *my_macdrv_get_cocoa_window(HWND hwnd, BOOL require_on_screen)
{
    TRACE("macdrv_get_cocoa_window %p %d\n", hwnd, require_on_screen);
    return macdrv_get_cocoa_window(hwnd, require_on_screen);
}

static MTLDevice_id my_macdrv_create_metal_device(void)
{
    TRACE("macdrv_create_metal_device\n");
    return macdrv_create_metal_device();
}

static void my_macdrv_release_metal_device(MTLDevice_id d)
{
    TRACE("macdrv_release_metal_device %p\n", d);
    macdrv_release_metal_device(d);
}

static WineMetalView *my_macdrv_view_create_metal_view(WineContentView *v, MTLDevice_id d)
{
    struct metal_view_surface *entry = macdrv_get_d3dmetal_view_owner(v);
    WineMetalView *view;

    TRACE("macdrv_view_create_metal_view %p %p\n", v, d);
    /* The verified native caller passes the +1 create-device result straight
     * here and never uses it again. Transfer it even if view creation fails. */
    entry->device = d;
    if (!(view = macdrv_view_create_metal_view(entry->surface->cocoa_view, d))) return NULL;
    entry->view = view;
    pthread_mutex_lock(&metal_view_surfaces_mutex);
    entry->next = metal_view_surfaces;
    metal_view_surfaces = entry;
    pthread_mutex_unlock(&metal_view_surfaces_mutex);
    return view;
}

static CAMetalLayer *my_macdrv_view_get_metal_layer(WineMetalView *v)
{
    TRACE("macdrv_view_get_metal_layer %p\n", v);
    return macdrv_view_get_metal_layer(v);
}

static void my_macdrv_view_release_metal_view(WineMetalView *v)
{
    struct metal_view_surface **cursor, *entry = NULL;

    TRACE("macdrv_view_release_metal_view %p\n", v);
    pthread_mutex_lock(&metal_view_surfaces_mutex);
    for (cursor = &metal_view_surfaces; *cursor; cursor = &(*cursor)->next)
        if ((*cursor)->view == v)
        {
            entry = *cursor;
            *cursor = entry->next;
            entry->view = NULL;
            entry->refs++;
            break;
        }
    pthread_mutex_unlock(&metal_view_surfaces_mutex);

    macdrv_view_release_metal_view(v);
    if (entry) release_metal_view_surface(entry);
}

static void my_OnMainThread(dispatch_block_t b)
{
    TRACE("OnMainThread %p\n", b);
    OnMainThread(b);
}


static LSTATUS WINAPI my_RegQueryValueExA(HKEY p1, LPCSTR p2, LPDWORD p3, LPDWORD p4, BYTE* p5, LPDWORD p6)
{
    LSTATUS result;
    void *ret_ptr;
    ULONG ret_len;
    struct regqueryvalueexa_params params =
    {
        .dispatch = {.callback = regqueryvalueexa_callback},
        .hkey = HandleToUlong(p1),
        .name = (UINT_PTR)p2,
        .reserved = (UINT_PTR)p3,
        .type = (UINT_PTR)p4,
        .data = (UINT_PTR)p5,
        .count = (UINT_PTR)p6,
        .result = (UINT_PTR)&result,
    };

    TRACE("RegQueryValueExA %p %s %p %p %p %p\n", p1, p2, p3, p4, p5, p6);

    KeUserDispatchCallback(&params.dispatch, sizeof(params), &ret_ptr, &ret_len);

    return result;
}

static LSTATUS WINAPI my_RegSetValueExA(HKEY p1, LPCSTR p2, DWORD p3, DWORD p4, const BYTE* p5, DWORD p6)
{
    LSTATUS result;
    void *ret_ptr;
    ULONG ret_len;
    struct regsetvalueexa_params params =
    {
        .dispatch = {.callback = regsetvalueexa_callback},
        .hkey = HandleToUlong(p1),
        .name = (UINT_PTR)p2,
        .reserved = p3,
        .type = p4,
        .data = (UINT_PTR)p5,
        .count = p6,
        .result = (UINT_PTR)&result,
    };

    TRACE("RegSetValueExA %p %s(%p) %d %p %d\n", p1, p2, p2, p4, p5, p6);

    KeUserDispatchCallback(&params.dispatch, sizeof(params), &ret_ptr, &ret_len);

    return result;
}

static LSTATUS WINAPI my_RegOpenKeyExA(HKEY p1, LPCSTR p2, DWORD p3, DWORD p4, HKEY* p5)
{
    LSTATUS result;
    void *ret_ptr;
    ULONG ret_len;
    struct regcreateopenkeyexa_params params =
    {
        .dispatch = {.callback = regcreateopenkeyexa_callback},
        .create = 0,
        .hkey = HandleToUlong(p1),
        .name = (UINT_PTR)p2,
        .options = p3,
        .access = p4,
        .retkey = (UINT_PTR)p5,
        .result = (UINT_PTR)&result,
    };

    TRACE("RegOpenKeyExA %p %s\n", p1, p2);

    KeUserDispatchCallback(&params.dispatch, sizeof(params), &ret_ptr, &ret_len);

    return result;
}

static LSTATUS WINAPI my_RegCreateKeyExA(HKEY p1, LPCSTR p2, DWORD p3, LPSTR p4, DWORD p5, DWORD p6, LPSECURITY_ATTRIBUTES p7, HKEY* p8, LPDWORD p9)
{
    LSTATUS result;
    void *ret_ptr;
    ULONG ret_len;
    struct regcreateopenkeyexa_params params =
    {
        .dispatch = {.callback = regcreateopenkeyexa_callback},
        .create = 1,
        .hkey = HandleToUlong(p1),
        .name = (UINT_PTR)p2,
        .reserved = p3,
        .class = (UINT_PTR)p4,
        .options = p5,
        .access = p6,
        .security = (UINT_PTR)p7,
        .retkey = (UINT_PTR)p8,
        .disposition = (UINT_PTR)p9,
        .result = (UINT_PTR)&result,
    };

    TRACE("RegCreateKeyExA %p %s\n", p1, p2);

    KeUserDispatchCallback(&params.dispatch, sizeof(params), &ret_ptr, &ret_len);

    return result;
}

static LSTATUS WINAPI my_RegCloseKey(HKEY hkey)
{
    TRACE("RegCloseKey %p\n", hkey);
    if (!hkey) return ERROR_INVALID_HANDLE;
    if (hkey >= (HKEY)0x80000000) return ERROR_SUCCESS;
    return RtlNtStatusToDosError( NtClose( hkey ) );
}

static BOOL WINAPI my_EnumDisplayMonitors(HDC h, LPRECT p2, MONITORENUMPROC p3, LPARAM p4)
{
    TRACE("EnumDisplayMonitors %p %p %p %ld\n", h, p2, p3, p4);
    return NtUserEnumDisplayMonitors(h, p2, p3, p4);
}

static BOOL WINAPI my_GetMonitorInfoA(HMONITOR monitor, LPMONITORINFO info)
{
    MONITORINFOEXW miW;
    BOOL ret;

    TRACE("GetMonitorInfoA %p %p\n", monitor, info);

    if (info->cbSize == sizeof(MONITORINFO)) return NtUserGetMonitorInfo( monitor, info );
    if (info->cbSize != sizeof(MONITORINFOEXA)) return FALSE;

    miW.cbSize = sizeof(miW);
    ret = NtUserGetMonitorInfo( monitor, (MONITORINFO *)&miW );
    if (ret)
    {
        MONITORINFOEXA *miA = (MONITORINFOEXA *)info;
        ULONG size;
        miA->rcMonitor = miW.rcMonitor;
        miA->rcWork = miW.rcWork;
        miA->dwFlags = miW.dwFlags;
        RtlUnicodeToUTF8N(miA->szDevice, sizeof(miA->szDevice), &size, miW.szDevice, lstrlenW(miW.szDevice) * sizeof(WCHAR));
    }
    return ret;
}

static BOOL WINAPI my_AdjustWindowRectEx(LPRECT p1,DWORD p2,BOOL p3,DWORD p4)
{
    TRACE("AdjustWindowRectEx %p %u %d %u\n", p1, p2, p3, p4);
    return NtUserAdjustWindowRect(p1, p2, p3, p4, NtUserGetSystemDpiForProcess(NULL));
}

static LONG_PTR WINAPI my_GetWindowLongPtrW(HWND h,int nIndex)
{
    TRACE("GetWindowLongPtrW %p\n", h);
    /* ignore possibility of DWLP_DLGPROC */
    return NtUserGetWindowLongPtrW(h, nIndex);
}

static BOOL WINAPI my_GetWindowRect(HWND h, LPRECT rect)
{
    TRACE("GetWindowRect %p %p\n", h, rect);
    return NtUserGetWindowRect(h, rect, NtUserGetWinMonitorDpi(h, MDT_DEFAULT));
}

static BOOL WINAPI my_MoveWindow(HWND h, int X,int Y,int nWidth,int nHeight,BOOL bRepaint)
{
    TRACE("MoveWindow %p %d %d %d %d %d\n", h, X, Y, nWidth, nHeight, bRepaint);
    return NtUserMoveWindow(h, X, Y, nWidth, nHeight, bRepaint);
}

static BOOL WINAPI my_SetWindowPos(HWND h,HWND h2,int x,int y,int cx,int cy,UINT flags)
{
    TRACE("SetWindowPos %p %p %d %d %d %d %u\n", h, h2, x, y, cx, cy, flags);
    return NtUserSetWindowPos(h, h2, x, y, cx, cy, flags);
}

static INT WINAPI my_GetSystemMetrics(INT index)
{
    TRACE("GetSystemMetrics %d\n", index);
    return NtUserGetSystemMetrics( index );
}

static LONG_PTR WINAPI my_SetWindowLongPtrW(HWND hwnd, INT offset, LONG_PTR newval)
{
    TRACE("SetWindowLongPtrW %p %d %ld\n", hwnd, offset, newval);
    /* ignore possibility of DWLP_DLGPROC */
    return NtUserSetWindowLongPtr( hwnd, offset, newval, FALSE );
}

/* This is a separate SysV export, not part of the fixed macdrv_functions_t ABI. */
DECLSPEC_EXPORT int macdrv_query_d3dmetal_display(uintptr_t window, uintptr_t monitor_override,
                                                 struct yaagl_d3dmetal_display *out)
{
    MONITORINFOEXW info = {.cbSize = sizeof(info)};
    DEVMODEW mode = {.dmSize = sizeof(mode)};
    UNICODE_STRING device;
    HMONITOR monitor;

    if (!out) return 0;
    monitor = monitor_override ? (HMONITOR)monitor_override :
              NtUserMonitorFromWindow((HWND)window, MONITOR_DEFAULTTONEAREST);
    if (!monitor || !NtUserGetMonitorInfo(monitor, (MONITORINFO *)&info)) return 0;

    RtlInitUnicodeString(&device, info.szDevice);
    if (!NtUserEnumDisplaySettings(&device, ENUM_CURRENT_SETTINGS, &mode, 0) ||
        !(mode.dmFields & DM_DISPLAYFREQUENCY) || !mode.dmDisplayFrequency) return 0;

    out->monitor = (uintptr_t)monitor;
    out->refresh_rate = mode.dmDisplayFrequency;
    out->left = info.rcMonitor.left;
    out->top = info.rcMonitor.top;
    out->right = info.rcMonitor.right;
    out->bottom = info.rcMonitor.bottom;
    return 1;
}

DECLSPEC_EXPORT struct macdrv_functions_t macdrv_functions =
{
    &my_macdrv_init_display_devices,
    &my_get_win_data,
    &my_release_win_data,
    &my_macdrv_get_cocoa_window,
    &my_macdrv_create_metal_device,
    &my_macdrv_release_metal_device,
    &my_macdrv_view_create_metal_view,
    &my_macdrv_view_get_metal_layer,
    &my_macdrv_view_release_metal_view,
    &my_OnMainThread,
    &my_RegQueryValueExA,
    &my_RegSetValueExA,
    &my_RegOpenKeyExA,
    &my_RegCreateKeyExA,
    &my_RegCloseKey,
    &my_EnumDisplayMonitors,
    &my_GetMonitorInfoA,
    &my_AdjustWindowRectEx,
    &my_GetWindowLongPtrW,
    &my_GetWindowRect,
    &my_MoveWindow,
    &my_SetWindowPos,
    &my_GetSystemMetrics,
    &my_SetWindowLongPtrW,
};

void macdrv_client_surface_presented(const macdrv_event *event)
{
    TRACE("client_surface %p\n", event->client_surface_presented.client_surface);

    client_surface_present(event->client_surface_presented.client_surface);
}

#endif
