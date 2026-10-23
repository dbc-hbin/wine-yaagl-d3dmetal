#!/usr/bin/env python3
"""Exercise Game Mode child routing through real Wine CreateProcess calls.

The runtime must contain the game host and patched ntdll. Uses a temporary
prefix; no installed game or system Game Mode policy is changed.
"""
import argparse
import json
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runtime', type=Path)
    parser.add_argument('--compiler', default='x86_64-w64-mingw32-clang')
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    wine, server = runtime / 'bin/wine.real', runtime / 'bin/wineserver'
    compiler = shutil.which(args.compiler)
    if not compiler:
        parser.error('supply --compiler with the installed LLVM MinGW compiler')
    with tempfile.TemporaryDirectory(prefix='yaagl-game-mode-routing-') as temporary:
        root = Path(temporary)
        source = root / 'spawn.c'
        source.write_text(GUEST)
        parent, target, helper = (root / name for name in ('launcher.exe', 'ZenlessZoneZero.exe', 'helper.exe'))
        subprocess.run([compiler, '-municode', '-O2', '-Wall', str(source), '-o', str(parent)], check=True)
        shutil.copy2(parent, target)
        shutil.copy2(parent, helper)
        alias = root / 'target-alias.exe'
        alias.symlink_to(target.name)
        report = root / 'launch.json'
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
            )
            for name, image, token, descendant, enabled, expected in cases:
                report.unlink(missing_ok=True)
                launch_env = dict(env)
                if enabled:
                    launch_env.update(YAAGL_GAME_MODE_TARGET=str(target), YAAGL_GAME_MODE_REPORT=str(report))
                result = subprocess.run([str(wine), str(parent), windows(image), windows(token), descendant],
                                        env=launch_env, text=True, capture_output=True, timeout=60)
                assert result.returncode == 0, (name, result.stdout, result.stderr)
                assert 'leaf completed' in result.stdout, (name, result.stdout, result.stderr)
                assert report.exists() == expected, (name, result.stderr)
                events = result.stderr.count('game mode host host-started')
                assert events == int(expected), (name, result.stderr)
                if expected:
                    record = json.loads(report.read_text())
                    assert record['state'] == 'host-started' and record['pid'] > 0, (name, record)
                print(f'PASS {name}: host starts={events}')
                subprocess.run([str(server), '-w'], env=env, check=True, timeout=60)
        finally:
            subprocess.run([str(server), '-k'], env=env, capture_output=True, timeout=30)
            subprocess.run([str(server), '-w'], env=env, capture_output=True, timeout=30)


if __name__ == '__main__':
    main()
