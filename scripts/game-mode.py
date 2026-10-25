#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
"""Build and package the ZZZ Wine host and external policy helpers."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import plistlib
import re
import shutil
import struct
import subprocess

SUPERVISOR = 'libexec/yaagl-game-mode/game-policy-supervisor'
POLICY_TOOL = 'libexec/yaagl-game-mode/gamepolicyctl'
POLICY_REQUIREMENT = 'identifier "com.apple.gamepolicyctl" and anchor apple'
HOST = 'libexec/yaagl-game-mode/ZZZ.app/Contents/MacOS/wine'
BUNDLE = 'libexec/yaagl-game-mode/ZZZ.app'
INFO_PLIST = BUNDLE + '/Contents/Info.plist'
CAPABILITY = {'version': 3, 'target': 'zzz', 'supervisor': SUPERVISOR, 'host': HOST}
ROOT = Path(__file__).resolve().parents[1]


def embedded_plist(path: Path) -> dict:
    data = path.read_bytes()
    if len(data) < 32 or struct.unpack_from('<II', data) != (0xfeedfacf, 0x1000007):
        raise ValueError('Game Mode host must be a thin x86_64 Mach-O')
    count, size = struct.unpack_from('<II', data, 16)
    end = 32 + size
    if end > len(data):
        raise ValueError('truncated host load commands')
    offset = 32
    metadata = None
    for _ in range(count):
        if offset + 8 > end:
            raise ValueError('truncated host load command')
        command, size = struct.unpack_from('<II', data, offset)
        if size < 8 or offset + size > end:
            raise ValueError('invalid host load command')
        if command == 0x19:  # LC_SEGMENT_64
            if size < 72:
                raise ValueError('truncated host segment')
            sections = struct.unpack_from('<I', data, offset + 64)[0]
            if 72 + sections * 80 > size:
                raise ValueError('truncated host sections')
            for index in range(sections):
                name, segment, _, length, start = struct.unpack_from(
                    '<16s16sQQI', data, offset + 72 + index * 80)
                if name.rstrip(b'\0') == b'__info_plist' and segment.rstrip(b'\0') == b'__TEXT':
                    if metadata is not None or start + length > len(data):
                        raise ValueError('invalid host embedded metadata')
                    metadata = plistlib.loads(data[start:start + length].rstrip(b'\0'))
        offset += size
    if offset != end or metadata is None:
        raise ValueError('host has no valid embedded Info.plist')
    return metadata


def validate_policy_tool(path: Path) -> None:
    if path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError('missing regular executable Apple gamepolicyctl')
    subprocess.run(['/usr/bin/codesign', '--verify', '--strict',
                    '-R', '=' + POLICY_REQUIREMENT, str(path)], check=True)
    entitlements = plistlib.loads(subprocess.check_output(
        ['/usr/bin/codesign', '-d', '--entitlements', '-', '--xml', str(path)]))
    if entitlements.get('com.apple.gamepolicyd.tool.privileged.xpc') is not True:
        raise ValueError('Apple gamepolicyctl lacks the privileged policy entitlement')


def copy_policy_tool(source: Path, destination: Path) -> None:
    validate_policy_tool(source)
    shutil.copy2(source, destination)
    if source.read_bytes() != destination.read_bytes():
        raise ValueError('Apple gamepolicyctl changed during copying')
    validate_policy_tool(destination)


def validate(tree: Path) -> None:
    supervisor = tree / SUPERVISOR
    if supervisor.is_symlink() or not supervisor.is_file() or not os.access(supervisor, os.X_OK):
        raise ValueError('missing regular executable Game Mode policy supervisor')
    with supervisor.open('rb') as stream:
        if stream.read(4) != b'\xca\xfe\xba\xbe':
            raise ValueError('Game Mode policy supervisor must be a universal Mach-O')
    subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(supervisor)], check=True)
    validate_policy_tool(tree / POLICY_TOOL)
    host = tree / HOST
    if host.is_symlink() or not host.is_file() or not os.access(host, os.X_OK):
        raise ValueError('missing regular executable Game Mode host')
    for directory in (host.parent, host.parent.parent, tree / BUNDLE):
        if directory.is_symlink():
            raise ValueError('Game Mode bundle must not use symlink directories')
    info = tree / INFO_PLIST
    if info.is_symlink() or not info.is_file():
        raise ValueError('missing regular Game Mode Info.plist')
    expected = plistlib.loads((ROOT / 'scripts/yaagl-game-mode.plist').read_bytes())
    if plistlib.loads(info.read_bytes()) != expected or embedded_plist(host) != expected:
        raise ValueError('Game Mode bundle and embedded metadata differ from source')
    # main.c must fall back to the canonical Wine argv[0], not a bundle-local ntdll.
    override = host.parent / 'ntdll.so'
    if override.exists() or override.is_symlink():
        raise ValueError('Game Mode host must not override ntdll')
    (tree / 'lib/wine/x86_64-unix/ntdll.so').resolve(strict=True).relative_to(tree.resolve())
    subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(host)], check=True)
    subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(tree / BUNDLE)], check=True)


def build(build_dir: Path) -> None:
    policy_tool = Path(subprocess.check_output(
        ['/usr/bin/xcrun', '--find', 'gamepolicyctl'], text=True).strip())
    validate_policy_tool(policy_tool)
    output = build_dir / 'game-mode'
    output.mkdir(parents=True, exist_ok=True)
    makefile = (build_dir / 'Makefile').read_text().replace('\\\n', ' ')
    # Reuse makedep's actual loader object and flags, including its memory layout.
    match = re.search(r'^loader/wine:([^\n]*)\n(\t[^\n]*\$\(CC\)[^\n]*)$', makefile, re.M)
    if not match or 'loader/main.o' not in match[1]:
        raise ValueError('unsupported generated Wine loader link rule')
    recipe = match[2]
    if recipe.count('loader/wine_info.plist') != 1 or '$(CC) -o $@' not in recipe:
        raise ValueError('unsupported Wine embedded-plist link flags')
    if '#define HAVE_WINE_PRELOADER 1' in (build_dir / 'include/config.h').read_text():
        raise ValueError('Game Mode host requires the current no-preloader macOS build')
    plist = ROOT / 'scripts/yaagl-game-mode.plist'
    shutil.copyfile(plist, output / 'Info.plist')
    target = 'game-mode/wine'
    (output / 'host.mk').write_text(
        target + ':' + match[1] + ' game-mode/Info.plist\n' +
        recipe.replace('loader/wine_info.plist', 'game-mode/Info.plist') + '\n')
    (build_dir / target).unlink(missing_ok=True)
    subprocess.run(['/usr/bin/arch', '-x86_64', '/usr/bin/make', '-C', str(build_dir),
                    '-f', 'Makefile', '-f', 'game-mode/host.mk', target], check=True)
    if embedded_plist(build_dir / target) != plistlib.loads(plist.read_bytes()):
        raise ValueError('linked Game Mode metadata mismatch')
    subprocess.run(['/usr/bin/xcrun', 'clang', '-arch', 'arm64', '-arch', 'x86_64',
                    '-O2', '-Wall', '-Wextra', '-Werror',
                    str(ROOT / 'scripts/game-policy-supervisor.c'),
                    '-framework', 'Security', '-framework', 'CoreFoundation',
                    '-o', str(output / 'game-policy-supervisor')], check=True)
    copy_policy_tool(policy_tool, output / 'gamepolicyctl')


def install(build_dir: Path, tree: Path) -> None:
    supervisor = tree / SUPERVISOR
    directory = supervisor.parent
    if directory.exists() or directory.is_symlink():
        raise ValueError('refusing to overwrite existing Game Mode helpers')
    directory.mkdir(parents=True)
    host = tree / HOST
    host.parent.mkdir(parents=True)
    shutil.copy2(build_dir / 'game-mode/wine', host)
    shutil.copy2(build_dir / 'game-mode/Info.plist', tree / INFO_PLIST)
    shutil.copy2(build_dir / 'game-mode/game-policy-supervisor', supervisor)
    policy_source = build_dir / 'game-mode/gamepolicyctl'
    copy_policy_tool(policy_source, tree / POLICY_TOOL)
    subprocess.run(['codesign', '--force', '--sign', '-', str(supervisor)], check=True)
    subprocess.run(['codesign', '--force', '--sign', '-', str(tree / BUNDLE)], check=True)
    # Apple's privileged helper must retain its original bytes and signature.
    if policy_source.read_bytes() != (tree / POLICY_TOOL).read_bytes():
        raise ValueError('Game Mode signing changed Apple gamepolicyctl')
    validate(tree)


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
