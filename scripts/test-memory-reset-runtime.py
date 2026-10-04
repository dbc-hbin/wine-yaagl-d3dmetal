#!/usr/bin/env python3
"""Check Darwin MEM_RESET discard eligibility through real Windows APIs in a temporary prefix."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

MINGW = Path('/opt/llvm-mingw-20260616-ucrt-macos-universal/bin')
GUEST = r'''
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#define SIZE (256u * 1024u * 1024u)
#define UNIT 0x10000
#define CHECK(x) do { if (!(x)) { fprintf(stderr, "assertion %u: %s (error %lu)\n", __LINE__, #x, GetLastError()); ExitProcess(1); } } while (0)
static void phase(const char *name, void *base)
{
    printf("PHASE %s %p\n", name, base);
    fflush(stdout);
    CHECK(getchar() == '\n');
}
int main(void)
{
    MEMORY_BASIC_INFORMATION info;
    uint32_t *allocation, *base, random = 0x12345678;
    CHECK((allocation = VirtualAlloc(NULL, SIZE + 2 * UNIT, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE)));
    base = (void *)((char *)allocation + UNIT);
    allocation[0] = 0x13579bdf;
    *(uint32_t *)((char *)base + SIZE) = 0x2468ace0;
    for (unsigned i = 0; i < SIZE / sizeof(*base); i++)
    {
        random ^= random << 13;
        random ^= random >> 17;
        random ^= random << 5;
        base[i] = random;
    }
    phase("dirty", base);
    CHECK(VirtualAlloc(base, SIZE, MEM_RESET, PAGE_NOACCESS) == base);
    CHECK(VirtualQuery(base, &info, sizeof(info)) == sizeof(info));
    CHECK(info.State == MEM_COMMIT && info.Protect == PAGE_READWRITE);
    CHECK(allocation[0] == 0x13579bdf && *(uint32_t *)((char *)base + SIZE) == 0x2468ace0);
    phase("reset", base);
    base[0] = 0x12345678;
    base[SIZE / sizeof(*base) - 1] = 0x87654321;
    CHECK(base[0] == 0x12345678 && base[SIZE / sizeof(*base) - 1] == 0x87654321);
    SetLastError(0xdeadbeef);
    CHECK(!VirtualAlloc((char *)allocation + SIZE + 2 * UNIT - 0x1000, 0x2000, MEM_RESET, PAGE_READWRITE));
    CHECK(GetLastError() == ERROR_INVALID_ADDRESS);
    CHECK(VirtualFree(base, SIZE, MEM_DECOMMIT));
    CHECK(VirtualQuery(base, &info, sizeof(info)) == sizeof(info) && info.State == MEM_RESERVE);
    CHECK(VirtualAlloc(base, SIZE, MEM_COMMIT, PAGE_READWRITE) == base);
    CHECK(base[0] == 0 && base[SIZE / sizeof(*base) - 1] == 0);
    CHECK(VirtualFree(allocation, 0, MEM_RELEASE));
    puts("PASS API semantics");
    return 0;
}
'''


OBSERVER = r'''
#include <errno.h>
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <stdint.h>
#include <stdio.h>
#include <sys/mman.h>
static kern_return_t regions(void *base, size_t length, uint64_t *dirty, unsigned *private)
{
    mach_vm_address_t cursor = (uintptr_t)base, end = cursor + length;
    *dirty = 0;
    *private = 1;
    while (cursor < end)
    {
        mach_vm_address_t address = cursor;
        mach_vm_size_t size;
        mach_port_t object = MACH_PORT_NULL;
        vm_region_extended_info_data_t info;
        mach_msg_type_number_t count = VM_REGION_EXTENDED_INFO_COUNT;
        kern_return_t status = mach_vm_region(mach_task_self(), &address, &size, VM_REGION_EXTENDED_INFO, (vm_region_info_t)&info, &count, &object);
        if (object != MACH_PORT_NULL) mach_port_deallocate(mach_task_self(), object);
        if (status) return status;
        if (address > cursor || address + size <= cursor) return KERN_INVALID_ADDRESS;
        *dirty += info.pages_dirtied;
        *private &= info.ref_count == 1 && info.shadow_depth == 0;
        cursor = address + size;
    }
    return KERN_SUCCESS;
}
static int observe(void *base, size_t size, int advice)
{
    if (size != 256 * 1024 * 1024 || (advice != MADV_DONTNEED && advice != MADV_FREE))
        return madvise(base, size, advice);
    uint64_t before = 0, after = 0;
    unsigned private_before = 0, private_after = 0;
    int incoming = errno;
    kern_return_t first = regions(base, size, &before, &private_before);
    errno = incoming;
    /* dyld binds the interposer's own madvise reference to the original function. */
    int result = madvise(base, size, advice), saved = errno;
    kern_return_t second = regions(base, size, &after, &private_after);
    fprintf(stderr, "ADVICE advice=%d return=%d first=%d second=%d dirty_before=%llu dirty_after=%llu private_before=%u private_after=%u\n", advice, result, first, second, (unsigned long long)before, (unsigned long long)after, private_before, private_after);
    errno = saved;
    return result;
}
__attribute__((used)) static struct { const void *replacement, *replacee; } interpose
__attribute__((section("__DATA,__interpose"))) = {(const void *)observe, (const void *)madvise};
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runtime', type=Path, help='scratch runtime containing bin/wine.real and bin/wineserver')
    parser.add_argument('--arch', choices=('x86_64', 'i386'), default='x86_64')
    parser.add_argument('--compiler', type=Path)
    parser.add_argument('--keep-output', type=Path, help='new directory for source, binaries and observation evidence; never the prefix')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('native discard observation requires macOS Mach region information')
    if args.keep_output and args.keep_output.exists():
        parser.error('output directory already exists')
    runtime = args.runtime.resolve()
    wine, server = runtime / 'bin/wine.real', runtime / 'bin/wineserver'
    compiler = args.compiler or MINGW / ('x86_64-w64-mingw32-gcc' if args.arch == 'x86_64' else 'i686-w64-mingw32-gcc')
    for binary in (wine, server, compiler, Path('/usr/bin/clang')):
        if not os.access(binary, os.X_OK):
            parser.error(f'missing executable: {binary}')
    with tempfile.TemporaryDirectory(prefix='wine-memory-reset-') as temporary:
        root = Path(temporary)
        source, guest = root / 'guest.c', root / 'reset.exe'
        source.write_text(GUEST)
        native, observer = root / 'observer.c', root / 'observer.dylib'
        native.write_text(OBSERVER)
        env = os.environ.copy()
        for name in ('WINE_MEMORY_TRACE_DIR', 'WINEPREFIX', 'WINELOADER', 'WINESERVER', 'WINEDLLPATH',
                     'WINEDLLPATH_PREPEND', 'WINEARCH', 'WINEDLLOVERRIDES', 'DYLD_INSERT_LIBRARIES'):
            env.pop(name, None)
        env.update(WINEPREFIX=str(root / 'prefix'), WINELOADER=str(wine), WINESERVER=str(server),
                   WINEARCH='win64', WINEDEBUG='-all', DYLD_FALLBACK_LIBRARY_PATH=str(runtime / 'lib'))
        subprocess.run([str(compiler), '-O2', '-Wall', '-o', str(guest), str(source)], check=True)
        # Rosetta can launch arm64 helpers which inherit the inserted observer.
        subprocess.run(['/usr/bin/clang', '-arch', 'x86_64', '-arch', 'arm64', '-O2', '-Wall',
                        '-dynamiclib', str(native), '-o', str(observer)], check=True)
        try:
            subprocess.run([str(wine), 'wineboot', '-i'], env=env, check=True, capture_output=True, timeout=180)
            subprocess.run([str(server), '-w'], env=env, check=True, timeout=180)
            result = subprocess.run([str(wine), str(guest)], env=env | {'DYLD_INSERT_LIBRARIES': str(observer)},
                                    input='\n\n', capture_output=True, text=True, timeout=90)
            (root / 'guest.stdout').write_text(result.stdout)
            (root / 'guest.stderr').write_text(result.stderr)
            assert result.returncode == 0, result.stderr
            observed = [dict(field.split('=') for field in line.split()[1:])
                        for line in result.stderr.splitlines() if line.startswith('ADVICE ')]
            assert len(observed) == 1, result.stderr
            sample = {key: int(value) for key, value in observed[0].items()}
            assert sample['advice'] == 5 and sample['return'] == sample['first'] == sample['second'] == 0, sample
            assert sample['private_before'] == sample['private_after'] == 1 and sample['dirty_before'] > 0, sample
            # Dirty guard pages outside RESET remain; resident/RSS bytes need not decrease.
            assert sample['dirty_after'] < sample['dirty_before'] * 0.01, sample
            print(f'MEM_RESET made dirty pages discardable ({args.arch}): {sample}; API semantics passed.')
        finally:
            subprocess.run([str(server), '-k'], env=env, capture_output=True, timeout=30)
            subprocess.run([str(server), '-w'], env=env, capture_output=True, timeout=30)
            if args.keep_output:
                args.keep_output.mkdir(parents=True)
                for path in root.iterdir():
                    if path.is_file():
                        shutil.copy2(path, args.keep_output / path.name)


if __name__ == '__main__':
    main()
