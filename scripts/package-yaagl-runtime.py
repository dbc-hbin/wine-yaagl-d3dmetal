#!/usr/bin/env python3
"""Package current-source Wine overlay with GPTK bridges; omit Apple's framework only."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
NAME = 'wine-11.17-d3dmetal-gptk4.0b2-macos26.tar.xz'
CHANGES = {
    'bin/wineserver': 'arm64/server/wineserver',
    'lib/wine/x86_64-unix/ntdll.so': 'x64/dlls/ntdll/ntdll.so',
    'lib/wine/x86_64-unix/win32u.so': 'x64/dlls/win32u/win32u.so',
    'lib/wine/x86_64-unix/winemac.so': 'x64/dlls/winemac.drv/winemac.so',
    'lib/wine/x86_64-windows/amd_fidelityfx_framegeneration_dx12.dll':
        'x64/dlls/amd_fidelityfx_framegeneration_dx12/x86_64-windows/amd_fidelityfx_framegeneration_dx12.dll',
}
METADATA = ('yaagl-wine-p3-provenance.json', 'yaagl-wine-runtime-files.json',
            'yaagl-wine-p3-graphics-artifacts.json', 'yaagl-wine-p3-runtime.txt',
            'zzz-frame-probe-stage.json')
HELPERS = ('prepare-d3dmetal-runtime', 'libYaaglNativePsoCache.dylib', 'build-manifest.json')


def run(*args):
    subprocess.run(list(map(str, args)), check=True)


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def inventory(root, exclude=()):
    entries = []
    for parent, directories, files in os.walk(root, followlinks=False):
        directories.sort()
        files.sort()
        for name in [*directories, *files]:
            path = Path(parent) / name
            relative = path.relative_to(root).as_posix()
            if relative in exclude:
                continue
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                entries.append({'path': relative, 'type': 'symlink', 'target': os.readlink(path)})
            elif stat.S_ISREG(mode):
                entries.append({'path': relative, 'type': 'file', 'size': path.stat().st_size,
                                'sha256': sha(path)})
    return sorted(entries, key=lambda item: item['path'])


def verify_base(base):
    manifest = json.loads((base / METADATA[1]).read_text())
    provenance = json.loads((base / METADATA[0]).read_text())
    if manifest.get('schemaVersion') != 1 or manifest.get('entries') != inventory(base, (METADATA[1],)):
        raise ValueError('baseline full-tree inventory mismatch')
    if provenance.get('sourceCommit') != 'f163d14e24fb1f1e2e8e6a1a319ac82fc9e98a8e':
        raise ValueError('unexpected baseline source commit')
    if manifest.get('runtimeId') != provenance.get('runtimeId'):
        raise ValueError('baseline runtime identity mismatch')
    return provenance, sha(base / METADATA[1])


def relocate(source, target, baseline, wine):
    shutil.copy2(source, target)
    if target.suffix not in ('.so', '') or target.name.endswith('.dll'):
        return
    # wine server and Unix dylibs are Mach-O; preserve verified baseline runtime rpaths.
    if target.suffix == '.so':
        run('install_name_tool', '-id', '@rpath/' + target.name, target)
    listing = subprocess.check_output(['otool', '-L', str(target)], text=True)
    for dependency in re.findall(r'^\s+(.*?) \(compatibility version', listing, re.M):
        if dependency.startswith(('/System/', '/usr/lib/', '@')):
            continue
        name = Path(dependency).name
        # Dependencies may live beside the module or in the bundled GStreamer framework.
        locations = (wine / 'lib' / name, target.parent / name,
                     wine / 'lib/GStreamer.framework/Versions/1.0/lib' / name)
        if not any(path.exists() for path in locations):
            raise ValueError(f'unbundled dependency {dependency} in {target}')
        run('install_name_tool', '-change', dependency, '@rpath/' + name, target)
    def rpaths(path):
        return re.findall(r'cmd LC_RPATH\s+cmdsize \d+\s+path (.*?) \(offset',
                          subprocess.check_output(['otool', '-l', str(path)], text=True))
    for current in rpaths(target):
        run('install_name_tool', '-delete_rpath', current, target)
    for previous in rpaths(baseline):
        run('install_name_tool', '-add_rpath', previous, target)
    run('codesign', '--force', '--sign', '-', target)
    run('codesign', '--verify', '--strict', target)


def main():
    if len(sys.argv) != 5:
        raise SystemExit('usage: package-yaagl-runtime.py BASE_WINE_ROOT OVERLAY_DIR AUTOPATCH_DIR OUTPUT_DIR')
    base, overlay, autopatch, output = map(lambda p: Path(p).resolve(), sys.argv[1:])
    provenance, baseline_inventory_sha = verify_base(base)
    output.mkdir(parents=True, exist_ok=True)
    wine = output / 'wine'
    archive = output / NAME
    if wine.exists() or archive.exists():
        raise ValueError('refusing existing staged runtime or archive')
    shutil.copytree(base, wine, symlinks=True)
    # The verified baseline wrapper predates this release; ship the current policy.
    shutil.copy2(ROOT / 'scripts/wine-launch-wrapper.sh', wine / 'bin/wine')
    for relative, source in CHANGES.items():
        target = wine / relative
        built = overlay / source
        if not target.is_file() or not built.is_file():
            raise ValueError(f'missing built module or baseline: {relative}')
        relocate(built, target, base / relative, wine)
    if subprocess.check_output([str(wine / 'bin/wine'), '--version'], text=True).strip() != 'wine-11.17':
        raise ValueError('rebuilt ntdll does not identify Wine 11.17')
    helper_manifest = json.loads((autopatch / HELPERS[2]).read_text())
    if helper_manifest.get('schema') != 1:
        raise ValueError('unexpected autopatch manifest schema')
    helper_dest = wine / 'libexec/yaagl-d3dmetal'
    helper_dest.mkdir(parents=True)
    for filename in HELPERS:
        source = autopatch / filename
        if filename in ('prepare-d3dmetal-runtime', 'libYaaglNativePsoCache.dylib'):
            key = 'helper' if filename == 'prepare-d3dmetal-runtime' else 'sidecar'
            if sha(source) != helper_manifest['artifacts'][key]['sha256']:
                raise ValueError(f'autopatch artifact mismatch: {filename}')
            run('codesign', '--verify', '--strict', source)
        shutil.copy2(source, helper_dest / filename)
    framework = wine / 'lib/external/D3DMetal.framework'
    if not framework.is_dir():
        raise ValueError('baseline Apple framework absent')
    shutil.rmtree(framework)
    for name in METADATA:
        (wine / name).unlink()
    for relative in ('lib/external/libd3dshared.dylib',
                     'lib/wine/x86_64-windows/d3d12.dll', 'lib/wine/x86_64-unix/d3d12.so',
                     'lib/wine/x86_64-windows/nvngx.dll',
                     'lib/wine/x86_64-unix/amd_fidelityfx_upscaler_dx12.so'):
        if not (wine / relative).exists():
            raise ValueError(f'missing GPTK companion module: {relative}')
    source_paths = ['dlls/ntdll/unix/msync.c', 'dlls/ntdll/unix/msync.h',
                    'dlls/ntdll/unix/sync.c', 'dlls/ntdll/unix/process.c',
                    'dlls/ntdll/unix/server.c', 'server/msync.c', 'server/msync.h',
                    'server/inproc_sync.c', 'server/process.c', 'server/process.h',
                    'server/object.h', 'server/protocol.def', 'include/wine/server_protocol.h',
                    'server/request_handlers.h', 'server/request_trace.h',
                    'dlls/winemac.drv/cocoa_app.m', 'dlls/win32u/input.c',
                    'dlls/amd_fidelityfx_framegeneration_dx12/main.c',
                    'scripts/wine-launch-wrapper.sh']
    manifest = {
        'schemaVersion': 1, 'runtimeId': 'wine-11.17-d3dmetal-gptk4.0b2-2',
        'archive': NAME, 'archiveRoot': 'wine', 'wineVersion': 'wine-11.17',
        'framework': {'destination': 'lib/external/D3DMetal.framework',
                      'installation': 'native autopatch before loading Wine',
                      'included': False},
        'baseline': {'sourceCommit': provenance['sourceCommit'],
                     'inventorySha256': baseline_inventory_sha},
        'rebuiltArtifacts': [{'path': path, 'buildSha256': sha(overlay / source),
                              'packagedSha256': sha(wine / path)}
                             for path, source in CHANGES.items()],
        'changedSourceInputs': [{'path': path, 'sha256': sha(ROOT / path)} for path in source_paths],
        'autopatch': {'directory': 'libexec/yaagl-d3dmetal',
                      'manifestSha256': sha(helper_dest / HELPERS[2]),
                      'helperSha256': sha(helper_dest / HELPERS[0]),
                      'sidecarSha256': sha(helper_dest / HELPERS[1])},
        'entries': inventory(wine, ('yaagl-d3dmetal-runtime.json',)),
    }
    manifest_path = wine / 'yaagl-d3dmetal-runtime.json'
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    # Verify the final inventory before and after archive extraction (see release smoke).
    if inventory(wine, (manifest_path.name,)) != manifest['entries']:
        raise ValueError('staged inventory changed during packaging')
    with tarfile.open(archive, 'w:xz', preset=6) as stream:
        stream.add(wine, arcname='wine', recursive=True)
    with tarfile.open(archive, 'r:xz') as stream:
        members = stream.getmembers()
        if not members or any(m.name != 'wine' and not m.name.startswith('wine/') for m in members):
            raise ValueError('archive root is not wine/')
        if any('D3DMetal.framework' in m.name for m in members):
            raise ValueError('Apple framework leaked into runtime archive')
    (output / 'SHA256SUMS').write_text(f'{sha(archive)}  {archive.name}\n')
    print(json.dumps({'archive': str(archive), 'size': archive.stat().st_size,
                      'sha256': sha(archive), 'autopatch': manifest['autopatch']}, indent=2))


if __name__ == '__main__':
    main()
