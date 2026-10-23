#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
"""Build/package the ZZZ host using this build's Wine loader object."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import plistlib
import re
import shutil
import struct
import subprocess

HOST = 'libexec/yaagl-game-mode/ZZZ.app/Contents/MacOS/wine'
BUNDLE = 'libexec/yaagl-game-mode/ZZZ.app'
NTDLL = 'lib/wine/x86_64-unix/ntdll.so'
CAPABILITY = {'version': 1, 'host': HOST, 'target': 'zzz'}
ROOT = Path(__file__).resolve().parents[1]


def embedded_plist(path: Path) -> dict:
    # The host is deliberately the same thin AMD64 loader as the selected build.
    with path.open('rb') as stream:
        header = stream.read(32)
        if len(header) != 32 or struct.unpack_from('<II', header) != (0xfeedfacf, 0x1000007):
            raise ValueError('Game Mode host must be a thin x86_64 Mach-O')
        count = struct.unpack_from('<I', header, 16)[0]
        for _ in range(count):
            start = stream.tell()
            command, size = struct.unpack('<II', stream.read(8))
            if size < 8:
                raise ValueError('invalid host load command')
            body = stream.read(size - 8)
            if command == 0x19:  # LC_SEGMENT_64
                sections = struct.unpack_from('<I', body, 56)[0]
                for index in range(sections):
                    section = body[64 + index * 80:64 + (index + 1) * 80]
                    name, segment, _, length, offset = struct.unpack_from('<16s16sQQI', section)
                    if name.rstrip(b'\0') == b'__info_plist' and segment.rstrip(b'\0') == b'__TEXT':
                        stream.seek(offset)
                        return plistlib.loads(stream.read(length).rstrip(b'\0'))
            stream.seek(start + size)
    raise ValueError('host has no embedded Info.plist')


def validate(tree: Path) -> None:
    host = tree / HOST
    bundle = tree / BUNDLE
    expected = plistlib.loads((ROOT / 'loader/yaagl-game-mode.plist').read_bytes())
    if host.is_symlink() or not host.is_file() or not os.access(host, os.X_OK):
        raise ValueError('missing regular executable Game Mode host')
    if plistlib.loads((bundle / 'Contents/Info.plist').read_bytes()) != expected or embedded_plist(host) != expected:
        raise ValueError('Game Mode bundle and embedded metadata differ from source')
    # loader/main.c resolves ntdll from the fixed bundle location. An external
    # symlink inside Contents/MacOS would invalidate strict bundle signing.
    if (host.parent / 'ntdll.so').exists() or (host.parent / 'ntdll.so').is_symlink():
        raise ValueError('Game Mode host must use runtime-relative ntdll loading')
    (tree / NTDLL).resolve(strict=True).relative_to(tree.resolve())


def build(build_dir: Path) -> None:
    makefile = (build_dir / 'Makefile').read_text().replace('\\\n', ' ')
    # makedep expands Wine's loader-specific flags into this recipe. Reuse it,
    # including CC/LDFLAGS, rather than freezing another version's memory layout.
    match = re.search(r'^loader/wine:([^\n]*)\n(\t[^\n]*\$\(CC\)[^\n]*)$', makefile, re.M)
    if not match or 'loader/main.o' not in match[1]:
        raise ValueError('unsupported generated Wine loader link rule')
    recipe = match[2]
    old = 'loader/wine_info.plist'
    if recipe.count(old) != 1 or '$(CC) -o $@' not in recipe:
        raise ValueError('unsupported Wine embedded-plist link flags')
    if '#define HAVE_WINE_PRELOADER 1' in (build_dir / 'include/config.h').read_text():
        raise ValueError('Game Mode host requires the current no-preloader macOS build')
    shutil.copyfile(ROOT / 'loader/yaagl-game-mode.plist', build_dir / 'loader/yaagl-game-mode.plist')
    target = 'loader/yaagl-game-mode-wine'
    fragment = build_dir / 'loader/yaagl-game-mode.mk'
    fragment.write_text(target + ':' + match[1] + ' loader/yaagl-game-mode.plist\n' +
                        recipe.replace(old, 'loader/yaagl-game-mode.plist') + '\n')
    # Always relink: configured flags may have changed since an earlier build.
    (build_dir / target).unlink(missing_ok=True)
    subprocess.run(['/usr/bin/arch', '-x86_64', '/usr/bin/make', '-C', str(build_dir),
                    '-f', 'Makefile', '-f', 'loader/yaagl-game-mode.mk', target], check=True)
    if embedded_plist(build_dir / target) != plistlib.loads((ROOT / 'loader/yaagl-game-mode.plist').read_bytes()):
        raise ValueError('linked Game Mode metadata mismatch')


def install(build_dir: Path, tree: Path) -> None:
    bundle = tree / BUNDLE
    if bundle.exists() or bundle.is_symlink():
        raise ValueError('refusing to overwrite an existing Game Mode bundle')
    host = tree / HOST
    host.parent.mkdir(parents=True)
    shutil.copy2(build_dir / 'loader/yaagl-game-mode-wine', host)
    shutil.copy2(ROOT / 'loader/yaagl-game-mode.plist', bundle / 'Contents/Info.plist')
    validate(tree)
    subprocess.run(['codesign', '--force', '--sign', '-', str(bundle)], check=True)
    subprocess.run(['codesign', '--verify', '--strict', str(bundle)], check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('build', 'install', 'validate'))
    parser.add_argument('path', type=Path)
    parser.add_argument('--tree', type=Path)
    args = parser.parse_args()
    if args.operation == 'build':
        build(args.path.resolve())
    elif args.operation == 'install':
        if args.tree is None:
            parser.error('install requires --tree')
        install(args.path.resolve(), args.tree.resolve())
    else:
        validate(args.path.resolve())
