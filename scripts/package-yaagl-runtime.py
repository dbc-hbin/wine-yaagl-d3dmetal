#!/usr/bin/env python3
"""Package a verified beta3 staged runtime, replacing the framework with a local-input helper."""
import hashlib
import importlib.util
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
HELPERS = ('prepare-d3dmetal-runtime', 'libD3DMetalSidecar.dylib', 'build-manifest.json')
loader = importlib.util.spec_from_file_location('stage_runtime', ROOT / 'scripts/stage-runtime.py')
stage = importlib.util.module_from_spec(loader)
loader.loader.exec_module(stage)


def validate_helper_sources(helper_manifest, stage_manifest):
    stage.check_sources(helper_manifest)
    native = helper_manifest.get('nativeBuildManifest')
    if not isinstance(native, dict):
        raise ValueError('autopatch native build provenance is missing')
    stage.check_sources(native)
    if native != stage_manifest.get('native_build_manifest'):
        raise ValueError('autopatch and staged runtime use different native builds')


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
                entries.append({'path': relative, 'type': 'file', 'size': path.stat().st_size, 'sha256': sha(path)})
    return sorted(entries, key=lambda item: item['path'])


def main():
    if len(sys.argv) != 4:
        raise SystemExit('usage: package-yaagl-runtime.py STAGED_WINE_ROOT AUTOPATCH_DIR OUTPUT_DIR')
    source, autopatch, output = (Path(p).resolve() for p in sys.argv[1:])
    if output == source or source in output.parents or output in source.parents:
        raise ValueError('source and output must not contain one another')
    subprocess.run([sys.executable, ROOT / 'scripts/stage-runtime.py', '--verify-runtime', source, '--current-sources'], check=True)
    stage_manifest = json.loads((source / stage.STAGE_MANIFEST).read_text())
    build = stage_manifest['source_build']
    provenance = json.loads((source / 'yaagl-wine-p3-provenance.json').read_text())
    if any(provenance.get(key) != value for key, value in build.items()):
        raise ValueError('public provenance differs from verified staged source build')
    version = build['wineVersion']
    if not isinstance(version, str) or not re.fullmatch(r'[0-9][A-Za-z0-9._-]*', version):
        raise ValueError('unsafe source Wine version')
    source_inventory = json.loads((source / 'yaagl-wine-runtime-files.json').read_text())
    if (source_inventory.get('schemaVersion') != 1 or
            source_inventory.get('runtimeId') != build['runtimeId'] or
            source_inventory.get('wineVersion') != 'wine-' + version):
        raise ValueError('runtime inventory and staged source identity differ')
    if source_inventory.get('entries') != inventory(source, ('yaagl-wine-runtime-files.json',)):
        raise ValueError('complete runtime inventory does not match final staged bytes')
    runtime_id = f'wine-{version}-d3dmetal-gptk4.0b3'
    archive_name = f'{runtime_id}-macos26.tar.xz'
    helper_manifest = json.loads((autopatch / 'build-manifest.json').read_text())
    if helper_manifest.get('schema') != 2 or helper_manifest.get('gptkVersion') != '4.0b3':
        raise ValueError('expected beta3 autopatch manifest schema 2')
    validate_helper_sources(helper_manifest, stage_manifest)
    for name, key in ((HELPERS[0], 'helper'), (HELPERS[1], 'sidecar')):
        artifact = autopatch / name
        if artifact.is_symlink() or not artifact.is_file() or sha(artifact) != helper_manifest['artifacts'][key]['sha256']:
            raise ValueError(f'autopatch artifact mismatch: {name}')
        subprocess.run(['/usr/bin/codesign', '--verify', '--strict', artifact], check=True)
    wine, archive = output / 'wine', output / archive_name
    if wine.exists() or archive.exists():
        raise ValueError('refusing existing staged runtime or archive')
    output.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, wine, symlinks=True)
    helper_dest = wine / 'libexec/yaagl-d3dmetal'
    helper_dest.mkdir(parents=True)
    for name in HELPERS:
        shutil.copy2(autopatch / name, helper_dest / name)
    framework = wine / 'lib/external/D3DMetal.framework'
    if framework.is_symlink() or not framework.is_dir():
        raise ValueError('expected staged beta3 framework directory')
    shutil.rmtree(framework)
    # These inventories describe the complete local staging tree, not this package.
    for name in ('yaagl-wine-runtime-files.json', 'yaagl-d3dmetal-runtime.json',
                 'yaagl-wine-p3-graphics-artifacts.json', 'zzz-frame-probe-stage.json'):
        (wine / name).unlink(missing_ok=True)
    manifest = {
        'schemaVersion': 2, 'runtimeId': runtime_id,
        'archive': archive_name, 'archiveRoot': 'wine', 'wineVersion': 'wine-' + version,
        'framework': {'version': '4.0b3', 'destination': 'lib/external/D3DMetal.framework',
                      'installation': 'native helper with local Apple beta3 input and explicit license acceptance before loading Wine',
                      'included': False},
        'sourceStageManifestSha256': sha(source / 'zzz-frame-probe-stage.json'),
        'autopatch': {'directory': 'libexec/yaagl-d3dmetal',
                      'manifestSha256': sha(helper_dest / HELPERS[2]),
                      'helperSha256': sha(helper_dest / HELPERS[0]),
                      'sidecarSha256': sha(helper_dest / HELPERS[1])},
        'entries': inventory(wine, ('yaagl-d3dmetal-runtime.json',)),
    }
    manifest_path = wine / 'yaagl-d3dmetal-runtime.json'
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    if inventory(wine, (manifest_path.name,)) != manifest['entries']:
        raise ValueError('staged inventory changed during packaging')
    with tarfile.open(archive, 'w:xz', preset=6) as stream:
        stream.add(wine, arcname='wine', recursive=True)
    with tarfile.open(archive, 'r:xz') as stream:
        names = stream.getnames()
        if not names or any(name != 'wine' and not name.startswith('wine/') for name in names):
            raise ValueError('archive root is not wine/')
        if any('D3DMetal.framework' in name for name in names):
            raise ValueError('Apple framework leaked into runtime archive')
    (output / 'SHA256SUMS').write_text(f'{sha(archive)}  {archive.name}\n')
    print(json.dumps({'archive': str(archive), 'sha256': sha(archive), 'autopatch': manifest['autopatch']}, indent=2))


if __name__ == '__main__':
    main()
