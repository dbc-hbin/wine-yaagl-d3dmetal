#!/usr/bin/env python3
"""Exercise actual XAudio2 PE DLLs with real audio dispatch in a temporary prefix.

Only the DLL's imported allocator is gated (or made to fail) to select the first
registration/growth interleave. No registry source extraction or fake XAudio API.
Foreign-thread Unregister is synchronous: a callback must not wait for a thread
that is unregistering that same callback. Windows forbids callback-thread
Unregister; self/next removal here deliberately tests our safe extension.
"""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import traceback

MINGW = Path('/opt/llvm-mingw-20260616-ucrt-macos-universal/bin')

GUEST = r'''
#define COBJMACROS
#include <windows.h>
#include <objbase.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "assertion line %d: %s (error %lu)\n", __LINE__, #x, GetLastError()); fflush(stderr); ExitProcess(1); } } while (0)
#define WAIT(x) CHECK(WaitForSingleObject((x), 10000) == WAIT_OBJECT_0)
typedef struct { void **v; } Interface;
typedef struct Callback Callback;
typedef struct { void (WINAPI *start)(Callback *); void (WINAPI *end)(Callback *); void (WINAPI *error)(Callback *, HRESULT); } CallbackVtbl;
struct Callback { const CallbackVtbl *v; LONG starts, ends; unsigned id; };
static Interface *audio, *master, *source;
static unsigned version, offset, destroy_index;
static HMODULE module;
static HANDLE pass, entered, proceed, completed, waiting;
static LONG mode, allocation_action, action_done, frame, first_frame;
static LONG voice_gate;
static unsigned dispatch_event;
static DWORD allocation_thread;
static DWORD release_tid;
static Callback callbacks[40];
static void *(__cdecl *real_malloc)(size_t);
static void *(__cdecl *real_realloc)(void *, size_t);
static BOOL (WINAPI *real_sleep_cv)(PCONDITION_VARIABLE, PCRITICAL_SECTION, DWORD);
static DWORD (WINAPI *real_wait)(HANDLE, DWORD);

static HRESULT reg(Callback *cb) { return ((HRESULT (WINAPI *)(void *, void *))audio->v[3 + offset])(audio, cb); }
static void unreg(Callback *cb) { ((void (WINAPI *)(void *, void *))audio->v[4 + offset])(audio, cb); }
static void destroy(Interface *voice) { ((void (WINAPI *)(void *))voice->v[destroy_index])(voice); }
static void *patch_import(const char *name, void *replacement)
{
    BYTE *base = (BYTE *)module;
    IMAGE_NT_HEADERS *nt = (void *)(base + ((IMAGE_DOS_HEADER *)base)->e_lfanew);
    IMAGE_IMPORT_DESCRIPTOR *descriptor = (void *)(base + nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT].VirtualAddress);
    for (; descriptor->Name; ++descriptor)
    {
        IMAGE_THUNK_DATA *lookup = (void *)(base + descriptor->OriginalFirstThunk);
        IMAGE_THUNK_DATA *iat = (void *)(base + descriptor->FirstThunk);
        for (; lookup->u1.AddressOfData; ++lookup, ++iat)
        {
            IMAGE_IMPORT_BY_NAME *entry;
            DWORD old;
            void *original;
            if (IMAGE_SNAP_BY_ORDINAL(lookup->u1.Ordinal)) continue;
            entry = (void *)(base + lookup->u1.AddressOfData);
            if (strcmp((char *)entry->Name, name)) continue;
            original = (void *)(uintptr_t)iat->u1.Function;
            CHECK(VirtualProtect(&iat->u1.Function, sizeof(iat->u1.Function), PAGE_READWRITE, &old));
            iat->u1.Function = (uintptr_t)replacement;
            CHECK(VirtualProtect(&iat->u1.Function, sizeof(iat->u1.Function), old, &old));
            return original;
        }
    }
    return NULL;
}
static int allocation_gate(void)
{
    LONG action;
    if (GetCurrentThreadId() != allocation_thread) return 0;
    action = InterlockedExchange(&allocation_action, 0);
    if (action == 1)
    {
        SetEvent(entered);
        WAIT(proceed);
    }
    return action == 2;
}
static void *__cdecl gate_malloc(size_t size) { return allocation_gate() ? NULL : real_malloc(size); }
static void *__cdecl gate_realloc(void *ptr, size_t size) { return allocation_gate() ? NULL : real_realloc(ptr, size); }
static BOOL WINAPI gate_sleep_cv(PCONDITION_VARIABLE cv, PCRITICAL_SECTION cs, DWORD timeout)
{
    SetEvent(waiting);
    return real_sleep_cv(cv, cs, timeout);
}
static DWORD WINAPI gate_wait(HANDLE handle, DWORD timeout)
{
    if (GetCurrentThreadId() == release_tid) SetEvent(waiting);
    return real_wait(handle, timeout);
}
static void cb_event(Callback *cb, unsigned event)
{
    if (event != dispatch_event) return;
    if (cb->id == 0 && InterlockedCompareExchange(&action_done, 1, 0) == 0)
    {
        if (mode == 1)
        {
            first_frame = frame;
            unreg(&callbacks[1]);
            unreg(cb);
            CHECK(reg(&callbacks[3]) == S_OK);
        }
        else if (mode == 2 || mode == 3 || mode == 4)
        {
            SetEvent(entered);
            WAIT(proceed);
        }
    }
    if (mode == 1 && cb->id == 2 && frame == first_frame) SetEvent(waiting);
    if (mode == 1 && cb->id == 3)
    {
        CHECK(frame > first_frame);
        SetEvent(completed);
    }
    if (mode == 0) SetEvent(completed);
}
static void WINAPI cb_start(Callback *cb) { InterlockedIncrement(&cb->starts); cb_event(cb, 0); }
static void WINAPI cb_end(Callback *cb) { InterlockedIncrement(&cb->ends); cb_event(cb, 1); }
static void WINAPI cb_error(Callback *cb, HRESULT error) { CHECK(0 && "unexpected critical audio device error"); }
static const CallbackVtbl callback_vtbl = {cb_start, cb_end, cb_error};
static void WINAPI voice_start(void *cb, UINT32 bytes) { }
static void WINAPI voice_start0(void *cb) { }
static void WINAPI voice_end(void *cb)
{
    if (InterlockedCompareExchange(&voice_gate, 0, 1) == 1)
    {
        SetEvent(entered);
        WAIT(proceed);
    }
    InterlockedIncrement(&frame);
    SetEvent(pass);
}
static void WINAPI voice_stream(void *cb) { }
static void WINAPI voice_buffer(void *cb, void *context) { }
static void WINAPI voice_error(void *cb, void *context, HRESULT error) { CHECK(0 && "unexpected voice error"); }
static void *voice_vtbl[7];
static Interface voice_callback = {voice_vtbl};
static Interface *create_source(void *callback)
{
    WAVEFORMATEX format = {WAVE_FORMAT_PCM, 1, 44100, 88200, 2, 16, 0};
    Interface *voice = NULL;
    CHECK(((HRESULT (WINAPI *)(void *, void **, const WAVEFORMATEX *, UINT32, float, void *, void *, void *))audio->v[5 + offset])
          (audio, (void **)&voice, &format, 0, 2.0f, callback, NULL, NULL) == S_OK);
    return voice;
}
static void create_engine(void)
{
    static const char *clsids[] = {
        "{fac23f48-31f5-45a8-b49b-5225d61401aa}", "{e21a7345-eb21-468e-be50-804db97cf708}",
        "{b802058a-464a-42db-bc10-b650d6f2586a}", "{4c5e637a-16c7-4de3-9c46-5ed22181962d}",
        "{03219e78-5bc3-44d1-b92e-f63d89cc6526}", "{4c9b6dde-6809-46e6-a278-9b6a97588670}",
        "{3eda9b49-2085-498b-9bb2-39a6778493de}", "{5a508685-a254-4fba-9b82-9a24b00306af}"
    };
    CHECK(!audio);
    if (version >= 8)
    {
        HRESULT (WINAPI *create)(void **, UINT32, UINT32) = (void *)GetProcAddress(module, "XAudio2Create");
        CHECK(create && create((void **)&audio, 0, 0xffffffff) == S_OK);
    }
    else
    {
        WCHAR wide[80];
        GUID clsid, iid;
        IClassFactory *factory = NULL;
        HRESULT (WINAPI *get_factory)(REFCLSID, REFIID, void **) = (void *)GetProcAddress(module, "DllGetClassObject");
        MultiByteToWideChar(CP_UTF8, 0, clsids[version], -1, wide, 80);
        CHECK(CLSIDFromString(wide, &clsid) == S_OK);
        CHECK(CLSIDFromString(L"{8bcf1f58-9fe7-4583-8ac6-e2adc465c8bb}", &iid) == S_OK);
        CHECK(get_factory && get_factory(&clsid, &IID_IClassFactory, (void **)&factory) == S_OK);
        CHECK(IClassFactory_CreateInstance(factory, NULL, &iid, (void **)&audio) == S_OK);
        IClassFactory_Release(factory);
        CHECK(((HRESULT (WINAPI *)(void *, UINT32, UINT32))audio->v[5])(audio, 0, 0xffffffff) == S_OK);
    }
}
static void setup(void)
{
    unsigned i;
    create_engine();
    if (version >= 8)
        CHECK(((HRESULT (WINAPI *)(void *, void **, UINT32, UINT32, UINT32, const WCHAR *, void *, UINT32))audio->v[7])
              (audio, (void **)&master, 1, 44100, 0, NULL, NULL, 6) == S_OK);
    else
        CHECK(((HRESULT (WINAPI *)(void *, void **, UINT32, UINT32, UINT32, UINT32, void *))audio->v[10])
              (audio, (void **)&master, 1, 44100, 0, 0, NULL) == S_OK);
    mode = action_done = frame = first_frame = 0;
    for (i = 0; i < 40; ++i) { callbacks[i].v = &callback_vtbl; callbacks[i].id = i; callbacks[i].starts = callbacks[i].ends = 0; }
    ResetEvent(pass); ResetEvent(entered); ResetEvent(proceed); ResetEvent(completed); ResetEvent(waiting);
    source = create_source(&voice_callback);
    CHECK(((HRESULT (WINAPI *)(void *, UINT32, UINT32))source->v[destroy_index + 1])(source, 0, 0) == S_OK);
    WAIT(pass);
}
static void cleanup(void)
{
    unsigned i;
    for (i = 0; i < 40; ++i) unreg(&callbacks[i]);
    destroy(source); destroy(master);
    CHECK(((ULONG (WINAPI *)(void *))audio->v[2])(audio) == 0);
    audio = source = master = NULL;
}
static DWORD WINAPI register_thread(void *arg)
{
    allocation_thread = GetCurrentThreadId();
    allocation_action = 1;
    CHECK(reg(arg) == S_OK);
    return 0;
}
static DWORD WINAPI mutate_thread(void *arg)
{
    unsigned i;
    Interface *voice;
    for (i = 4; i < 40; ++i) CHECK(reg(&callbacks[i]) == S_OK);
    CHECK(reg(&callbacks[4]) == S_OK);
    for (i = 4; i < 40; ++i) unreg(&callbacks[i]);
    voice = create_source(NULL);
    destroy(voice);
    SetEvent(proceed);
    return 0;
}
static DWORD WINAPI unregister_thread(void *arg) { unreg(arg); SetEvent(completed); return 0; }
static DWORD WINAPI release_thread(void *arg)
{
    release_tid = GetCurrentThreadId();
    CHECK(((ULONG (WINAPI *)(void *))audio->v[2])(audio) == 0);
    SetEvent(completed);
    release_tid = 0;
    return 0;
}
static void first_registration(void)
{
    HANDLE worker;
    setup();
    worker = CreateThread(NULL, 0, register_thread, &callbacks[0], 0, NULL); CHECK(worker);
    WAIT(entered);
    ResetEvent(pass);
    /* Registry allocation is stopped before publishing. The real audio thread
     * must complete a source pass while no callback exists. Old ncbs++ crashes. */
    WAIT(pass);
    CHECK(callbacks[0].starts == 0 && callbacks[0].ends == 0);
    SetEvent(proceed); WAIT(worker); CloseHandle(worker);
    WAIT(completed);
    cleanup();
    puts("PASS first-registration: real empty-registry audio pass during allocation");
}
static void allocation_failure(void)
{
    setup();
    allocation_thread = GetCurrentThreadId(); allocation_action = 2;
    CHECK(reg(&callbacks[0]) == E_OUTOFMEMORY);
    ResetEvent(pass); WAIT(pass);
    CHECK(callbacks[0].starts == 0 && callbacks[0].ends == 0);
    CHECK(reg(&callbacks[0]) == S_OK); WAIT(completed);
    allocation_action = 2;
    CHECK(reg(&callbacks[1]) == E_OUTOFMEMORY);
    ResetEvent(completed); WAIT(completed);
    CHECK(callbacks[1].starts == 0 && callbacks[1].ends == 0);
    CHECK(reg(&callbacks[1]) == S_OK);
    cleanup();
    puts("PASS allocation-failure: first/growth failure preserve registry and recovery");
}
static void reentrant(void)
{
    setup(); mode = 1;
    /* Gate a real voice pass so StopEngine cannot leave an already-running
     * End dispatch racing partial setup of the three engine callbacks. */
    InterlockedExchange(&voice_gate, 1);
    WAIT(entered);
    ((void (WINAPI *)(void *))audio->v[9 + offset])(audio);
    unreg(&callbacks[1]); unreg(&callbacks[2]);
    CHECK(reg(&callbacks[0]) == S_OK); CHECK(reg(&callbacks[1]) == S_OK); CHECK(reg(&callbacks[2]) == S_OK);
    SetEvent(proceed);
    CHECK(((HRESULT (WINAPI *)(void *))audio->v[8 + offset])(audio) == S_OK);
    WAIT(completed); WAIT(waiting);
    CHECK((dispatch_event ? callbacks[0].ends : callbacks[0].starts) == 1);
    CHECK((dispatch_event ? callbacks[1].ends : callbacks[1].starts) == 0);
    cleanup();
    puts("PASS reentrant: self/next unregister, survivor, deferred registration");
}
static void cross_thread(void)
{
    HANDLE worker;
    setup(); mode = 2; CHECK(reg(&callbacks[0]) == S_OK); WAIT(entered);
    worker = CreateThread(NULL, 0, mutate_thread, NULL, 0, NULL); CHECK(worker);
    WAIT(worker); CloseHandle(worker);
    cleanup();
    puts("PASS cross-thread: callback waits unrelated growth/removal and voice creation/destruction");
}
static void synchronous_unregister(void)
{
    HANDLE worker;
    LONG starts, ends;
    setup(); mode = 3; CHECK(reg(&callbacks[0]) == S_OK); WAIT(entered);
    CHECK(real_sleep_cv && "candidate DLL must expose actual condition-variable wait");
    worker = CreateThread(NULL, 0, unregister_thread, &callbacks[0], 0, NULL); CHECK(worker);
    WAIT(waiting);
    CHECK(WaitForSingleObject(completed, 0) == WAIT_TIMEOUT);
    CHECK(reg(&callbacks[1]) == S_OK); unreg(&callbacks[1]);
    SetEvent(proceed); WAIT(worker); CloseHandle(worker);
    starts = callbacks[0].starts; ends = callbacks[0].ends;
    ResetEvent(pass); WAIT(pass); ResetEvent(pass); WAIT(pass);
    CHECK(callbacks[0].starts == starts && callbacks[0].ends == ends);
    cleanup();
    puts("PASS synchronous-unregister: active foreign callback drained, no later calls");
}
static void growth_progress(void)
{
    HANDLE worker;
    setup(); CHECK(reg(&callbacks[0]) == S_OK); WAIT(completed);
    worker = CreateThread(NULL, 0, register_thread, &callbacks[1], 0, NULL); CHECK(worker);
    WAIT(entered);
    /* The allocator gate is arbitrary external work; registry/dispatch must
     * remain usable while it is stopped, including duplicate registration. */
    CHECK(reg(&callbacks[0]) == S_OK);
    ResetEvent(completed); WAIT(completed);
    SetEvent(proceed); WAIT(worker); CloseHandle(worker);
    cleanup();
    puts("PASS growth-progress: gated allocator never holds registry lock");
}
static void teardown(void)
{
    HANDLE worker;
    setup(); mode = 4; CHECK(reg(&callbacks[0]) == S_OK); WAIT(entered);
    worker = CreateThread(NULL, 0, release_thread, NULL, 0, NULL); CHECK(worker);
    WAIT(waiting);
    CHECK(WaitForSingleObject(completed, 0) == WAIT_TIMEOUT);
    SetEvent(proceed); WAIT(worker); CloseHandle(worker);
    audio = source = master = NULL;
    puts("PASS teardown: final foreign Release drains audio thread before registry free");
}
static unsigned long long heap_live_bytes(void)
{
    HANDLE heap = GetProcessHeap();
    PROCESS_HEAP_ENTRY entry = {0};
    unsigned long long bytes = 0;
    CHECK(HeapLock(heap));
    while (HeapWalk(heap, &entry))
        if (entry.wFlags & PROCESS_HEAP_ENTRY_BUSY) bytes += entry.cbData;
    CHECK(GetLastError() == ERROR_NO_MORE_ITEMS);
    CHECK(HeapUnlock(heap));
    return bytes;
}
static void lock_lifetime(void)
{
    unsigned i;
    unsigned long long before, after;
    create_engine();
    CHECK(((ULONG (WINAPI *)(void *))audio->v[2])(audio) == 0);
    audio = NULL; /* Warm up persistent COM allocations, without opening a device. */
    before = heap_live_bytes();
    for (i = 0; i < 8; ++i)
    {
        create_engine();
        CHECK(((ULONG (WINAPI *)(void *))audio->v[2])(audio) == 0);
        audio = NULL;
    }
    after = heap_live_bytes();
    printf("HEAP totals: before=%llu after=%llu cycles=8\n", before, after);
    CHECK(after == before);
    puts("PASS lock-lifetime: repeated public engine creation/Release retains no heap bytes");
}
int main(int argc, char **argv)
{
    char name[32];
    const char *which;
    CHECK(argc == 3);
    version = (unsigned)atoi(argv[1]); CHECK(version <= 9); which = argv[2];
    offset = version <= 7 ? 3 : 0; destroy_index = version < 4 ? 16 : 18;
    snprintf(name, sizeof(name), "xaudio2_%u.dll", version);
    CHECK(SUCCEEDED(CoInitializeEx(NULL, COINIT_MULTITHREADED)));
    module = LoadLibraryA(name); CHECK(module);
    pass = CreateEventW(NULL, FALSE, FALSE, NULL); entered = CreateEventW(NULL, TRUE, FALSE, NULL);
    proceed = CreateEventW(NULL, TRUE, FALSE, NULL); completed = CreateEventW(NULL, TRUE, FALSE, NULL);
    waiting = CreateEventW(NULL, TRUE, FALSE, NULL); CHECK(pass && entered && proceed && completed && waiting);
    voice_vtbl[0] = version ? (void *)voice_start : (void *)voice_start0; voice_vtbl[1] = voice_end;
    voice_vtbl[2] = voice_stream; voice_vtbl[3] = voice_buffer; voice_vtbl[4] = voice_buffer;
    voice_vtbl[5] = voice_buffer; voice_vtbl[6] = voice_error;
    real_malloc = patch_import("malloc", gate_malloc); real_realloc = patch_import("realloc", gate_realloc);
    CHECK(real_malloc);
    real_sleep_cv = patch_import("SleepConditionVariableCS", gate_sleep_cv);
    real_wait = patch_import("WaitForSingleObject", gate_wait); CHECK(real_wait);
    if (!strcmp(which, "first-registration")) first_registration();
    else if (!strcmp(which, "allocation-failure")) allocation_failure();
    else if (!strcmp(which, "reentrant") || !strcmp(which, "reentrant-end"))
    { dispatch_event = strstr(which, "-end") != NULL; reentrant(); }
    else if (!strcmp(which, "cross-thread") || !strcmp(which, "cross-thread-end"))
    { dispatch_event = strstr(which, "-end") != NULL; cross_thread(); }
    else if (!strcmp(which, "synchronous-unregister") || !strcmp(which, "synchronous-unregister-end"))
    { dispatch_event = strstr(which, "-end") != NULL; synchronous_unregister(); }
    else if (!strcmp(which, "growth-progress")) growth_progress();
    else if (!strcmp(which, "teardown")) teardown();
    else if (!strcmp(which, "lock-lifetime")) lock_lifetime();
    else CHECK(0 && "unknown case");
    CloseHandle(pass); CloseHandle(entered); CloseHandle(proceed); CloseHandle(completed); CloseHandle(waiting);
    FreeLibrary(module); CoUninitialize();
    return 0;
}
'''

CASES = ('first-registration', 'allocation-failure', 'reentrant', 'reentrant-end',
         'cross-thread', 'cross-thread-end', 'synchronous-unregister',
         'synchronous-unregister-end', 'growth-progress', 'teardown', 'lock-lifetime')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runtime', type=Path, help='staged runtime with bin/wine and bin/wineserver')
    parser.add_argument('--arch', choices=('x86_64', 'i386'), default='x86_64')
    parser.add_argument('--version', type=int, choices=range(10), action='append', help='repeatable; defaults to 0..9')
    parser.add_argument('--case', choices=CASES, action='append', help='repeatable; defaults to all cases')
    parser.add_argument('--compiler', type=Path)
    parser.add_argument('--trace', action='store_true', help='enable diagnostic memory tracing for guests, never prefix boot')
    parser.add_argument('--keep-output', type=Path, help='retain guest/logs in a NEW directory, never the prefix')
    args = parser.parse_args()
    if args.keep_output is not None and args.keep_output.exists():
        parser.error(f'output directory already exists: {args.keep_output}')
    runtime = args.runtime.resolve()
    wine, server = runtime / 'bin/wine', runtime / 'bin/wineserver'
    compiler = args.compiler or MINGW / ('x86_64-w64-mingw32-gcc' if args.arch == 'x86_64' else 'i686-w64-mingw32-gcc')
    for binary in (wine, server, compiler):
        if not binary.is_file() or not os.access(binary, os.X_OK):
            parser.error(f'missing executable: {binary}')
    with tempfile.TemporaryDirectory(prefix='wine-xaudio-callback-') as temporary:
        root = Path(temporary)
        traces = root / 'traces'
        source, guest = root / 'guest.c', root / 'xaudio-callback-guest.exe'
        source.write_text(GUEST)
        environment = os.environ.copy()
        for name in ('WINE_MEMORY_TRACE_DIR', 'WINEPREFIX', 'WINELOADER', 'WINESERVER', 'WINEDLLPATH',
                     'WINEARCH', 'WINEDLLOVERRIDES', 'DYLD_INSERT_LIBRARIES'):
            environment.pop(name, None)
        environment.update(WINEPREFIX=str(root / 'prefix'), WINEDEBUG='-all', WINEARCH='win64',
                           WINESERVER=str(server), WINELOADER=str(wine), WINEDLLOVERRIDES='winemenubuilder.exe=d')
        failure = None
        def run(command, label, timeout=60):
            result = subprocess.run([str(part) for part in command], env=environment,
                                    capture_output=True, text=True, timeout=timeout)
            (root / f'{label}.stdout').write_text(result.stdout)
            (root / f'{label}.stderr').write_text(result.stderr)
            if result.returncode:
                raise RuntimeError(f'{label}: exit {result.returncode}\n{result.stdout}\n{result.stderr}')
            return result.stdout
        try:
            run([compiler, '-O0', '-g', '-Wall', '-Wextra', '-Wno-unused-parameter', source, '-o', guest,
                 '-lole32', '-luuid'], 'compile')
            run([wine, 'wineboot', '-i'], 'wineboot', 180)
            run([server, '-w'], 'wineboot-wait', 180)
            if args.trace:
                traces.mkdir()
                environment['WINE_MEMORY_TRACE_DIR'] = str(traces)
            for version in args.version if args.version is not None else range(10):
                for case in args.case or CASES:
                    label = f'{args.arch}-xaudio2_{version}-{case}'
                    stdout = run([wine, guest, version, case], label)
                    if f'PASS {case.removesuffix("-end")}:' not in stdout:
                        raise AssertionError(f'{label}: missing consumer success result')
                    print(f'{label}: {stdout.strip()}', flush=True)
            run([server, '-w'], 'final-wait', 180)
            if args.trace:
                streams = list(traces.glob('*.wmtrace'))
                if not streams:
                    raise AssertionError('diagnostic trace requested but no producer streams appeared')
                total = sum(path.stat().st_size for path in streams)
                summary = f'{len(streams)} actual producer streams, {total} bytes\n'
                (root / 'trace-summary.txt').write_text(summary)
                print(summary.strip(), flush=True)
        except BaseException:
            failure = traceback.format_exc()
            raise
        finally:
            try:
                subprocess.run([str(server), '-k'], env=environment, capture_output=True, timeout=30)
                subprocess.run([str(server), '-w'], env=environment, capture_output=True, timeout=30)
            finally:
                if args.keep_output is not None:
                    args.keep_output.mkdir(parents=True)
                    for path in root.iterdir():
                        if path.is_file():
                            shutil.copy2(path, args.keep_output / path.name)
                    if traces.is_dir():
                        shutil.copytree(traces, args.keep_output / 'traces')
                    if failure:
                        (args.keep_output / 'failure.txt').write_text(failure)
                    print(f'Retained regression output: {args.keep_output.resolve()}', flush=True)


if __name__ == '__main__':
    main()
