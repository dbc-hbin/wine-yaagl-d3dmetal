#!/usr/bin/env python3
"""Validate beta3-only full-source runtimes; stage with stage-full-wine-runtime.py."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
host_loader = importlib.util.spec_from_file_location('game_mode_host', ROOT / 'scripts/game-mode-host.py')
game_mode = importlib.util.module_from_spec(host_loader)
host_loader.loader.exec_module(game_mode)
REL = Path('lib/external/D3DMetal.framework/Versions/A')
IMAGE = str(REL / 'D3DMetal')
CONVERTER = str(REL / 'Resources/libmetalirconverter.dylib')
SIDECAR = str(REL / 'Resources/libD3DMetalSidecar.dylib')
STAGE_MANIFEST = 'zzz-frame-probe-stage.json'
STAGE_SCHEMA = 7
GRAPHICS_MANIFEST = 'yaagl-d3dmetal-runtime.json'
SOURCE_MANIFEST = 'yaagl-wine-source-build.json'
DERIVED_METADATA = {STAGE_MANIFEST, GRAPHICS_MANIFEST, SOURCE_MANIFEST,
                    'yaagl-wine-p3-provenance.json', 'yaagl-wine-p3-graphics-artifacts.json',
                    'yaagl-wine-runtime-files.json', 'yaagl-wine-p3-runtime.txt'}
NGX_ALIASES = {'lib/wine/x86_64-windows/nvngx.dll': 'nvngx-on-metalfx.dll',
               'lib/wine/x86_64-unix/nvngx.so': 'nvngx-on-metalfx.so'}
SHARED_DYLIB_PATH = 'lib/external/libd3dshared.dylib'
NGX_UNIX_LINK = '../../external/libd3dshared.dylib'
PRIVATE_FG_PATH = 'lib/wine/x86_64-windows/amd_fidelityfx_framegeneration_dx12_native.dll'
ORIGINAL_FG_SHA256 = '3f5e674a59b400756e98ef31fd583a4eb08ad567ec8dee362886bc96e55ed347'
ORIGINAL_FG_EXPORTS = ((1, 'ffxConfigure'), (2, 'ffxCreateContext'), (3, 'ffxDestroyContext'),
                       (4, 'ffxDispatch'), (5, 'ffxQuery'))
FSR_ARTIFACT_PATHS = tuple(f'lib/wine/{arch}/amd_fidelityfx_{module}_dx12.{suffix}'
    for module in ('upscaler', 'framegeneration')
    for arch, suffix in (('x86_64-windows', 'dll'), ('x86_64-unix', 'so')))
FSR_POLICY = {'implementation': 'builtin-fsr-api-to-metalfx-with-metalfx-frame-interpolation',
              'dll_override': 'amd_fidelityfx_upscaler_dx12,amd_fidelityfx_framegeneration_dx12=b',
              'native_fg_fallback': PRIVATE_FG_PATH, 'default_upscaler': 'metalfx',
              'loader_override': False}
NGX_POLICY = {'implementation': 'stock-gptk-ngx-to-metalfx', 'aliases': NGX_ALIASES}
REQUIRED_HOST = ('bin/wine', 'bin/wineserver', 'lib/wine/x86_64-unix/ntdll.so',
                 'lib/wine/x86_64-unix/win32u.so', 'lib/wine/x86_64-unix/winemac.so',
                 'lib/wine/x86_64-unix/winegstreamer.so', game_mode.HOST,
                 game_mode.BUNDLE + '/Contents/Info.plist') + FSR_ARTIFACT_PATHS


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        value = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, RuntimeError):
        return False


def load_spec() -> dict:
    script = "import * as s from './scripts/d3dmetal-runtime-spec.mjs'; console.log(JSON.stringify({hashes:s.D3DMETAL_RUNTIME_HASHES,stock:s.D3DMETAL_STOCK_MODULE_HASHES,resources:s.D3DMETAL_FRAMEWORK_RESOURCE_HASHES,release:s.D3DMETAL_RELEASE}));"
    data = json.loads(subprocess.check_output(['node', '--input-type=module', '-e', script], cwd=ROOT, text=True))
    if not data.get('stock') or not data.get('resources') or not data.get('hashes', {}).get('pristineConverter'):
        raise ValueError('shared beta3 runtime spec has no stock module/converter contract')
    for relative in list(data['stock']):
        if relative.startswith('lib/wine/x86_64-windows/'):
            unix = relative.replace('x86_64-windows/', 'x86_64-unix/').removesuffix('.dll') + '.so'
            data['stock'][unix] = data['stock'][SHARED_DYLIB_PATH]
    return data


def inventory(runtime: Path, excluded=DERIVED_METADATA) -> tuple[dict, dict]:
    files, links = {}, {}
    for path in sorted(runtime.rglob('*')):
        relative = path.relative_to(runtime).as_posix()
        if relative in excluded:
            if path.is_symlink():
                raise ValueError(f'metadata must not be a symlink: {relative}')
            continue
        if not under(path, runtime):
            raise ValueError(f'external runtime symlink: {relative}')
        if path.is_symlink():
            link = os.readlink(path)
            if os.path.isabs(link) or not path.exists():
                raise ValueError(f'nonrelocatable or dangling runtime symlink: {relative}')
            links[relative] = link
        if path.is_file():
            files[relative] = digest(path)
        elif not path.is_dir():
            raise ValueError(f'unsupported runtime entry: {relative}')
    return files, links


def assert_tree_untouched(runtime: Path, stage: dict) -> None:
    if stage.get('stage_schema') != STAGE_SCHEMA:
        raise ValueError('unsupported stage schema; restage from a full-source beta3 build')
    files, links = inventory(runtime)
    if not files or files != stage.get('signed_artifacts'):
        raise ValueError('runtime file inventory changed since staging')
    if links != stage.get('symlinks'):
        raise ValueError('runtime symlink inventory changed since staging')
    for relative, target in NGX_ALIASES.items():
        if links.get(relative) != target:
            raise ValueError(f'stock NGX alias changed: {relative}')


def check_stock(runtime: Path, spec: dict) -> None:
    for relative, expected in spec['stock'].items():
        path = runtime / relative
        if not under(path, runtime) or not path.is_file() or digest(path) != expected:
            raise ValueError(f'beta3 stock module mismatch: {relative}')
        if relative.startswith('lib/wine/x86_64-unix/'):
            if not path.is_symlink() or os.readlink(path) != NGX_UNIX_LINK:
                raise ValueError(f'beta3 shared bridge alias mismatch: {relative}')
    if digest(runtime / CONVERTER) != spec['hashes']['pristineConverter']:
        raise ValueError('beta3 converter must remain pristine')
    resources = runtime / REL / 'Resources'
    if set(path.name for path in resources.iterdir()) - {'libD3DMetalSidecar.dylib'} != set(spec['resources']):
        raise ValueError('beta3 framework resource inventory mismatch')
    for name, expected in spec['resources'].items():
        path = resources / name
        if not under(path, runtime) or not path.is_file() or digest(path) != expected:
            raise ValueError(f'beta3 framework resource mismatch: {name}')
    for key in ('license', 'acknowledgements'):
        asset = spec['release'][key]
        if digest(runtime / 'lib/external' / asset['name']) != asset['sha256']:
            raise ValueError(f'beta3 Apple license material mismatch: {asset["name"]}')
    with (runtime / REL / 'Resources/Info.plist').open('rb') as stream:
        if plistlib.load(stream).get('CFBundleShortVersionString') != '4.0b3':
            raise ValueError('only D3DMetal 4.0b3 is supported')


def source_tree_hash(root: Path) -> str:
    names = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'])
    value = hashlib.sha256()
    for name in sorted(set(names.split(b'\0')) - {b''}):
        path = root / os.fsdecode(name)
        value.update(name + b'\0')
        if path.is_symlink():
            value.update(b'L' + os.fsencode(os.readlink(path)))
        elif path.is_file():
            value.update(b'F' + path.read_bytes())
        else:
            value.update(b'DELETED')
        value.update(b'\0')
    return value.hexdigest()


def validate_source_build(data: dict) -> None:
    if (data.get('schemaVersion') != 3 or data.get('buildKind') != 'full-current-source' or
            data.get('graphicsTarget') != 'gptk-4.0b3' or data.get('graphicsOverlayIncluded') is not False or
            not re.fullmatch(r'[0-9a-f]{64}', str(data.get('sourceTreeSha256', ''))) or
            not re.fullmatch(r'[0-9a-f]{40,64}', str(data.get('sourceCommit', ''))) or
            not data.get('hostArtifacts') or not isinstance(data.get('hostSymlinks'), dict)):
        raise ValueError('host lacks full-current-source beta3 build provenance; rebuild with build-wine-p3.sh')
    for field in ('runtimeId', 'name'):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', str(data.get(field, ''))):
            raise ValueError(f'unsafe build identity: {field}')
    if not data.get('wineVersion'):
        raise ValueError('missing source Wine version')


def check_sources(manifest: dict) -> None:
    sources = manifest.get('sources')
    if not isinstance(sources, list) or not sources:
        raise ValueError('native sidecar source inventory is missing')
    for entry in sources:
        path = ROOT / entry['path']
        if not under(path, ROOT) or not path.is_file() or digest(path) != entry['sha256']:
            raise ValueError(f'build source changed: {entry["path"]}')


def check_fsr_pe(path: Path) -> None:
    tool = os.environ.get('WINE_LLVM_READOBJ') or shutil.which('llvm-readobj')
    if not tool and os.environ.get('WINE_MINGW_ROOT'):
        tool = str(Path(os.environ['WINE_MINGW_ROOT']) / 'bin/llvm-readobj')
    if not tool:
        raise ValueError('set WINE_LLVM_READOBJ or WINE_MINGW_ROOT for FSR PE validation')
    output = subprocess.check_output([tool, '--coff-exports', str(path)], text=True)
    exports = tuple((int(n), name) for n, name in re.findall(r'Ordinal: (\d+)\s+Name: (\w+)', output))
    if 'Format: COFF-x86-64' not in output or exports != ORIGINAL_FG_EXPORTS:
        raise ValueError(f'FSR PE architecture/exports mismatch: {path}')


def original_fg_provenance(path: Path) -> dict:
    if not path.is_file() or digest(path) != ORIGINAL_FG_SHA256:
        raise ValueError(f'original FSR frame-generation fallback hash mismatch: {path}')
    check_fsr_pe(path)
    return {'source': str(path), 'runtime_path': PRIVATE_FG_PATH, 'sha256': ORIGINAL_FG_SHA256,
            'architecture': 'COFF-x86-64', 'exports': list(map(list, ORIGINAL_FG_EXPORTS)), 'loader_override': False}


def display_bridge_provenance(runtime: Path) -> dict:
    relative = 'lib/wine/x86_64-unix/winemac.so'
    path = runtime / relative
    symbols = subprocess.check_output(['nm', '-arch', 'x86_64', '-gU', str(path)], text=True)
    if '_macdrv_query_d3dmetal_display' not in symbols.split():
        raise ValueError('Wine display bridge is missing; rebuild current winemac')
    return {'path': relative, 'sha256': digest(path)}


def inspect_image(runtime: Path) -> dict:
    result = json.loads(subprocess.check_output(['node', str(ROOT / 'scripts/d3dmetal-sidecar-patch.mjs'),
                                               'inspect', str(runtime / IMAGE)], text=True))
    if result.get('mode') not in ('patched', 'patched-signed'):
        raise ValueError('D3DMetal does not match the current beta3-only patch payload')
    return result


def validate_stage(runtime: Path, current_sources=False) -> dict:
    stage = json.loads((runtime / STAGE_MANIFEST).read_text())
    assert_tree_untouched(runtime, stage)
    if (stage.get('gptk_version') != '4.0b3' or stage.get('ngx_policy') != NGX_POLICY or
            stage.get('fsr_translator') != FSR_POLICY or stage.get('licenseAcceptance') != 'explicit-cli-flag' or
            stage.get('gameMode') != game_mode.CAPABILITY):
        raise ValueError('unsupported beta3 stage policy or missing Apple license acceptance')
    game_mode.validate(runtime)
    build = stage['source_build']
    validate_source_build(build)
    if json.loads((runtime / SOURCE_MANIFEST).read_text()) != build:
        raise ValueError('source provenance differs from stage record')
    required = set(REQUIRED_HOST) | {'bin/wine.real', PRIVATE_FG_PATH, IMAGE, CONVERTER, SIDECAR}
    required.update('share/yaagl/steam/' + name for name in
                    ('steam64.exe', 'steam32.exe', 'lsteamclient64.dll', 'lsteamclient32.dll'))
    if not required <= stage['signed_artifacts'].keys():
        raise ValueError('full source runtime artifact inventory is incomplete')
    check_stock(runtime, load_spec())
    fallback = runtime / PRIVATE_FG_PATH
    if digest(fallback) != ORIGINAL_FG_SHA256 or fallback.stat().st_mode & 0o222:
        raise ValueError('native FSR fallback must be pinned and read-only')
    fg = stage.get('native_fg_fallback', {})
    if (fg.get('sha256') != ORIGINAL_FG_SHA256 or fg.get('runtime_path') != PRIVATE_FG_PATH or
            fg.get('architecture') != 'COFF-x86-64' or fg.get('exports') != list(map(list, ORIGINAL_FG_EXPORTS)) or
            fg.get('loader_override') is not False):
        raise ValueError('native FSR fallback provenance mismatch')
    launcher = stage.get('source_launcher', {})
    if (launcher.get('path') != 'scripts/wine-launch-wrapper.sh' or
            launcher.get('sha256') != digest(runtime / 'bin/wine')):
        raise ValueError('launcher source provenance mismatch')
    if stage.get('display_bridge') != display_bridge_provenance(runtime):
        raise ValueError('Wine display bridge identity mismatch')
    inspect_image(runtime)
    for path in (runtime / SIDECAR, runtime / 'lib/external/D3DMetal.framework', runtime / game_mode.BUNDLE):
        subprocess.run(['codesign', '--verify', '--strict', str(path)], check=True)
    if current_sources:
        check_sources(stage['native_build_manifest'])
        if source_tree_hash(ROOT) != build['sourceTreeSha256']:
            raise ValueError('Wine build source tree changed; rebuild the full host')
        if digest(ROOT / launcher['path']) != launcher['sha256']:
            raise ValueError('launcher source changed; restage the runtime')
    return stage


def verify_runtime(runtime: Path, current_sources=False) -> list[tuple[str, str]]:
    try:
        stage = validate_stage(runtime, current_sources)
        graphics = json.loads((runtime / GRAPHICS_MANIFEST).read_text())
        if (graphics.get('schemaVersion') != 2 or graphics.get('gptkVersion') != '4.0b3' or
                graphics.get('gameMode') != game_mode.CAPABILITY or
                graphics.get('stageManifestSha256') != digest(runtime / STAGE_MANIFEST) or
                graphics.get('files') != stage['signed_artifacts'] or graphics.get('symlinks') != stage['symlinks']):
            raise ValueError('graphics metadata does not match immutable stage record')
        return [('PASS', 'beta3 full-source runtime policy, stock modules, signed inventory and provenance match')]
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        return [('FAIL', str(error))]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-runtime', type=Path, required=True)
    parser.add_argument('--current-sources', action='store_true')
    args = parser.parse_args()
    report = verify_runtime(args.verify_runtime.resolve(), args.current_sources)
    for level, message in report:
        print(f'{level}: {message}')
    return int(any(level == 'FAIL' for level, _ in report))


if __name__ == '__main__':
    sys.exit(main())
