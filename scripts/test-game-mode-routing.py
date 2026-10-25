#!/usr/bin/env python3
"""Exercise exact-game host routing through real Wine CreateProcess calls.

Uses a temporary runtime/prefix and a native image-path probe, not a production
host report. Does not invoke the policy supervisor or Apple policy tool.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

GUEST = r'''
#include <windows.h>
#include <stdio.h>
#include <wchar.h>
int wmain(int argc, wchar_t **argv)
{
    if (argc == 1) { puts("leaf completed"); return 0; }
    if (argc != 4) return 2;
    STARTUPINFOW si = {sizeof(si)};
    PROCESS_INFORMATION pi;
    wchar_t command[32768];
    if (wcscmp(argv[3], L"-"))
        _snwprintf(command, 32768, L"\"%ls\" \"%ls\" \"%ls\" -", argv[2], argv[3], argv[3]);
    else
        _snwprintf(command, 32768, L"\"%ls\"", argv[2]);
    if (!CreateProcessW(argv[1], command, NULL, NULL, FALSE, 0, NULL, NULL, &si, &pi))
    {
        fprintf(stderr, "CreateProcess failed: %lu\n", GetLastError());
        return 3;
    }
    if (WaitForSingleObject(pi.hProcess, 30000) != WAIT_OBJECT_0) return 4;
    DWORD code;
    if (!GetExitCodeProcess(pi.hProcess, &code)) return 5;
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return code;
}
'''

PROBE = r'''
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <unistd.h>
__attribute__((constructor)) static void record_image(void)
{
    char path[PATH_MAX], record[PATH_MAX + 32];
    uint32_t capacity = sizeof(path);
    if (_NSGetExecutablePath(path, &capacity)) _exit(90);
    int length = snprintf(record, sizeof(record), "NATIVE_IMAGE:%s\n", path);
    /* One atomic record: concurrent Wine services cannot interleave paths. */
    if (length < 0 || length > PIPE_BUF || (size_t)length >= sizeof(record)) _exit(91);
    if (write(STDOUT_FILENO, record, length) != length) _exit(92);
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runtime', type=Path)
    parser.add_argument('--compiler', default='x86_64-w64-mingw32-clang')
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    compiler = shutil.which(args.compiler)
    if not compiler:
        parser.error('supply --compiler with the installed LLVM MinGW compiler')
    with tempfile.TemporaryDirectory(prefix='yaagl-game-mode-routing-', dir='/tmp') as temporary:
        root = Path(temporary).resolve()
        test_runtime = root / 'runtime with spaces'
        shutil.copytree(runtime, test_runtime, symlinks=True)
        runtime = test_runtime
        for path in runtime.rglob('*'):
            if path.is_symlink():
                path.resolve(strict=True).relative_to(runtime.resolve())
        host = runtime / 'libexec/yaagl-game-mode/ZZZ.app/Contents/MacOS/wine'
        wine, server = runtime / 'bin/wine.real', runtime / 'bin/wineserver'
        source = root / 'spawn.c'
        source.write_text(GUEST)
        parent, target, helper = (root / name for name in ('launcher.exe', 'ZenlessZoneZero.exe', 'helper.exe'))
        subprocess.run([compiler, '-municode', '-O2', '-Wall', str(source), '-o', str(parent)], check=True)
        shutil.copy2(parent, target)
        shutil.copy2(parent, helper)
        alias = root / 'target-alias.exe'
        alias.symlink_to(target.name)
        probe_source, probe = root / 'probe.c', root / 'probe.dylib'
        probe_source.write_text(PROBE)
        subprocess.run(['xcrun', 'clang', '-arch', 'arm64', '-arch', 'x86_64', '-dynamiclib',
                        '-Wall', '-Wextra', '-Werror', str(probe_source), '-o', str(probe)], check=True)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('WINE', 'YAAGL_GAME_MODE', 'DYLD_'))}
        env.update(WINEPREFIX=str(root / 'prefix'), WINEDEBUG='-all',
                   WINEDLLOVERRIDES='mscoree,mshtml=',
                   DYLD_FALLBACK_LIBRARY_PATH=str(runtime / 'lib'))
        windows = lambda path: 'Z:' + str(path).replace('/', '\\')
        try:
            subprocess.run([str(wine), 'wineboot', '-i'], env=env, capture_output=True, check=True, timeout=180)
            subprocess.run([str(server), '-w'], env=env, check=True, timeout=60)
            cases = (
                ('off', target, target, '-', False, False),
                ('matching-image', target, target, '-', True, True),
                ('spoofed-command-line', helper, target, '-', True, False),
                ('different-command-line', target, helper, '-', True, True),
                ('file-alias', alias, alias, '-', True, True),
                ('non-target-descendant', target, target, windows(helper), True, True),
                ('missing-host', target, target, '-', True, False),
            )
            for name, image, token, descendant, enabled, expected in cases:
                if name == 'missing-host':
                    host.rename(host.with_name('wine.disabled'))
                launch_env = dict(env, DYLD_INSERT_LIBRARIES=str(probe))
                if enabled:
                    launch_env['YAAGL_GAME_MODE_TARGET'] = str(target)
                result = subprocess.run([str(wine), str(parent), windows(image), windows(token), descendant],
                                        env=launch_env, text=True, capture_output=True, timeout=60)
                if name == 'missing-host':
                    assert result.returncode != 0, (name, result.stdout, result.stderr)
                    assert 'leaf completed' not in result.stdout, (name, result.stdout)
                else:
                    assert result.returncode == 0, (name, result.stdout, result.stderr)
                    assert 'leaf completed' in result.stdout, (name, result.stdout, result.stderr)
                paths = [Path(line.removeprefix('NATIVE_IMAGE:')).resolve()
                         for line in result.stdout.splitlines() if line.startswith('NATIVE_IMAGE:')]
                # Require the probe to have run even in the negative-routing cases.
                assert wine.resolve() in paths, (name, paths, result.stderr)
                assert paths.count(host) == int(expected), (name, paths, result.stderr)
                print(f'PASS {name}: native game hosts={paths.count(host)}')
                subprocess.run([str(server), '-w'], env=env, check=True, timeout=60)
        finally:
            subprocess.run([str(server), '-k'], env=env, capture_output=True, timeout=30)
            subprocess.run([str(server), '-w'], env=env, capture_output=True, timeout=30)


if __name__ == '__main__':
    main()
