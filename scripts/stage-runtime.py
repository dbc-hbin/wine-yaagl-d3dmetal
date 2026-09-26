#!/usr/bin/env python3
"""Stage an isolated FSR/NGX-to-MetalFX Wine runtime; never modify the source or game."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REL = Path('lib/external/D3DMetal.framework/Versions/A')
STAGE_MANIFEST = 'zzz-frame-probe-stage.json'
STAGE_SCHEMA = 6
LEGACY_NGX_SCHEMA = 5  # staged bytes carry the superseded manual YAAGL_GPU_IDENTITY launcher
NGX_DLL_SHA256 = 'f6bc9d77fd1e898fec8c6339d367bd8e0f338992c9c0c66d59b30c6e9e0743e4'
SHARED_DYLIB_PATH = 'lib/external/libd3dshared.dylib'
SHARED_DYLIB_SHA256 = 'd932330841e77682d47688641e0ac17049a2aff498deafac88921983dc16eedb'
NGX_UNIX_LINK = '../../external/libd3dshared.dylib'
DISPLAY_BRIDGE_PATH = 'lib/wine/x86_64-unix/winemac.so'
PLAY_PROFILE = 'play'
PLAY_MODEL_POLICY = {'all_gpu': 'system-default'}
FSR_OVERRIDE = 'amd_fidelityfx_upscaler_dx12,amd_fidelityfx_framegeneration_dx12=b'
ORIGINAL_FG_SOURCE = Path('/Applications/Zenless Zone Zero/amd_fidelityfx_framegeneration_dx12.dll')
ORIGINAL_FG_SHA256 = '3f5e674a59b400756e98ef31fd583a4eb08ad567ec8dee362886bc96e55ed347'
ORIGINAL_FG_EXPORTS = ((1, 'ffxConfigure'), (2, 'ffxCreateContext'), (3, 'ffxDestroyContext'),
                       (4, 'ffxDispatch'), (5, 'ffxQuery'))
PRIVATE_FG_PATH = 'lib/wine/x86_64-windows/amd_fidelityfx_framegeneration_dx12_native.dll'
FSR_ARTIFACT_PATHS = (
    'lib/wine/x86_64-windows/amd_fidelityfx_upscaler_dx12.dll',
    'lib/wine/x86_64-unix/amd_fidelityfx_upscaler_dx12.so',
    'lib/wine/x86_64-windows/amd_fidelityfx_framegeneration_dx12.dll',
    'lib/wine/x86_64-unix/amd_fidelityfx_framegeneration_dx12.so', PRIVATE_FG_PATH)
FSR_POLICY = {'implementation': 'builtin-fsr-api-to-metalfx-with-metalfx-frame-interpolation',
              'dll_override': FSR_OVERRIDE, 'native_fg_fallback': PRIVATE_FG_PATH,
              'upscaler_selection_environment': 'YAAGL_FSR_UPSCALER',
              'default_upscaler': 'metalfx',
              'loader_override': False, 'diagnostic_log_environment': 'YAAGL_FSR_LOG'}
ARTIFACT_PATHS = ('bin/wine', 'bin/wine.real',
    str(REL / 'D3DMetal'), str(REL / 'Resources/libYaaglNativePsoCache.dylib'),
    str(REL / 'Resources/libmetalirconverter.dylib'),
    'lib/wine/x86_64-windows/d3d12.dll', 'lib/wine/x86_64-unix/d3d12.so')
NGX_ARTIFACT_PATHS = ('lib/wine/x86_64-windows/nvngx.dll', 'lib/wine/x86_64-unix/nvngx.so', SHARED_DYLIB_PATH)
NGX_POLICY = {'implementation': 'stock-gptk-ngx-to-metalfx', 'windows_module': NGX_ARTIFACT_PATHS[0],
              'unix_bridge': NGX_ARTIFACT_PATHS[1], 'bridge_target': NGX_UNIX_LINK,
              'supported_gpu_identities': ['rx9070', 'rtx5060'],
              'default_gpu_identity': 'rtx5060', 'gpu_identity_policy': 'per-game',
              'game_gpu_identities': {'ZenlessZoneZero.exe': 'rx9070'}}
# Schema 5 runtime bytes shipped the superseded manual YAAGL_GPU_IDENTITY selection; keep
# describing them with the policy they actually implement instead of the current one.
LEGACY_NGX_POLICY = {'implementation': 'stock-gptk-ngx-to-metalfx', 'windows_module': NGX_ARTIFACT_PATHS[0],
                     'unix_bridge': NGX_ARTIFACT_PATHS[1], 'bridge_target': NGX_UNIX_LINK,
                     'default_gpu_identity': 'rx9070', 'gpu_identity_environment': 'YAAGL_GPU_IDENTITY',
                     'supported_gpu_identities': ['rx9070', 'rtx5060']}
NGX_POLICIES = {LEGACY_NGX_SCHEMA: LEGACY_NGX_POLICY, STAGE_SCHEMA: NGX_POLICY}
NGX_SCHEMAS = (LEGACY_NGX_SCHEMA, STAGE_SCHEMA)
SUPPORTED_STAGE_SCHEMAS = (4, LEGACY_NGX_SCHEMA, STAGE_SCHEMA)


def run(args: list[str]) -> None:
    print('+', shlex.join(args), flush=True)
    subprocess.run(args, check=True)


def under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def artifact_hashes(runtime: Path, paths=ARTIFACT_PATHS + FSR_ARTIFACT_PATHS) -> dict[str, str]:
    result = {}
    for relative in paths:
        path = runtime / relative
        if not under(path, runtime):
            raise ValueError(f'external runtime artifact: {relative}')
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def display_bridge_provenance(runtime: Path) -> dict[str, str]:
    path = runtime / DISPLAY_BRIDGE_PATH
    if not under(path, runtime) or not path.is_file():
        raise ValueError('missing runtime Wine display bridge')
    symbols = subprocess.check_output(['nm', '-arch', 'x86_64', '-gU', str(path)], text=True)
    if '_macdrv_query_d3dmetal_display' not in symbols.split():
        raise ValueError('Wine display bridge is too old; rebuild winemac with current display routing')
    return {'path': DISPLAY_BRIDGE_PATH, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def original_fg_provenance(path: Path) -> dict:
    if not path.is_file():
        raise ValueError(f'missing original frame-generation fallback: {path}')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != ORIGINAL_FG_SHA256:
        raise ValueError(f'original frame-generation SHA mismatch: expected {ORIGINAL_FG_SHA256}, got {digest}')
    readobj = Path('/opt/llvm-mingw-20260616-ucrt-macos-universal/bin/llvm-readobj')
    if not readobj.is_file():
        raise ValueError(f'missing PE inspection tool: {readobj}')
    output = subprocess.check_output([str(readobj), '--coff-exports', str(path)], text=True)
    if 'Format: COFF-x86-64' not in output:
        raise ValueError('original frame-generation fallback is not x86_64 PE')
    exports = tuple((int(ordinal), name) for ordinal, name in re.findall(
        r'Ordinal: (\d+)\s+Name: (\w+)', output))
    if exports != ORIGINAL_FG_EXPORTS:
        raise ValueError(f'original frame-generation exports mismatch: {exports!r}')
    return {'source': str(path), 'runtime_path': PRIVATE_FG_PATH, 'sha256': digest,
            'size': path.stat().st_size, 'architecture': 'COFF-x86-64',
            'exports': [{'ordinal': ordinal, 'name': name} for ordinal, name in exports],
            'source_access': 'read-only', 'loader_override': False}


def artifact_problems(runtime: Path, recorded, schema: int) -> list[str]:
    paths = ARTIFACT_PATHS + FSR_ARTIFACT_PATHS + (NGX_ARTIFACT_PATHS if schema in NGX_SCHEMAS else ())
    if not isinstance(recorded, dict) or set(recorded) != set(paths):
        return ['signed artifact inventory is missing or incomplete; restage with current tooling']
    if schema in NGX_SCHEMAS:
        link = runtime / NGX_ARTIFACT_PATHS[1]
        if not link.is_symlink() or os.readlink(link) != NGX_UNIX_LINK:
            return ['NGX Unix bridge symlink target changed']
    try:
        actual = artifact_hashes(runtime, paths)
    except (OSError, ValueError) as error:
        return [str(error)]
    problems = [f'signed runtime artifact changed: {name}' for name in actual if recorded[name] != actual[name]]
    if schema == 4:
        for relative in NGX_ARTIFACT_PATHS[:2]:
            path = runtime / relative
            if path.exists() or path.is_symlink():
                problems.append(f'NGX artifact remains in FSR-only schema 4 runtime: {relative}')
    elif schema in NGX_SCHEMAS:
        if actual[NGX_ARTIFACT_PATHS[0]] != NGX_DLL_SHA256 or actual[SHARED_DYLIB_PATH] != SHARED_DYLIB_SHA256:
            problems.append('stock NGX module or shared bridge does not match pinned source')
    return problems


def stage_manifest_report(source: Path) -> list[tuple[str, str]]:
    path = source / STAGE_MANIFEST
    if not path.exists():
        return [('INFO', 'source has no staged-runtime manifest')]
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        return [('FAIL', str(error))]
    if not isinstance(data, dict) or data.get('stage_schema') not in SUPPORTED_STAGE_SCHEMAS:
        return [('FAIL', 'unsupported stage schema; use an intact base runtime and restage with current tooling')]
    schema = data['stage_schema']
    problems = artifact_problems(source, data.get('signed_artifacts'), schema)
    if (data.get('profile') != PLAY_PROFILE or data.get('fsr_translator') != FSR_POLICY or
            data.get('model_policy') != PLAY_MODEL_POLICY or data.get('render_size_override') is not False or
            data.get('rendering_changes_by_default') is not True or
            data.get('dlss_translation') is not (schema in NGX_SCHEMAS)):
        problems.append('unsupported staged rendering policy')
    if schema in NGX_SCHEMAS:
        ngx = data.get('ngx_module')
        if (data.get('ngx_policy') != NGX_POLICIES[schema] or not isinstance(ngx, dict) or
                ngx.get('runtime_path') != NGX_ARTIFACT_PATHS[0] or
                ngx.get('sha256') != NGX_DLL_SHA256 or ngx.get('architecture') != 'COFF-x86-64' or
                ngx.get('unix_bridge') != NGX_ARTIFACT_PATHS[1] or
                ngx.get('bridge_target') != NGX_UNIX_LINK or
                ngx.get('bridge_sha256') != SHARED_DYLIB_SHA256):
            problems.append('stock NGX provenance or identity selection policy is missing or invalid')
    launcher = source / 'bin/wine'
    launcher_record = data.get('source_launcher')
    launcher_hash = launcher_record.get('sha256') if isinstance(launcher_record, dict) else None
    current_hash = hashlib.sha256((ROOT / 'scripts/wine-launch-wrapper-p3.sh').read_bytes()).hexdigest()
    if (not launcher.is_file() or not isinstance(launcher_record, dict) or
            launcher_record.get('path') != 'scripts/wine-launch-wrapper-p3.sh' or
            not re.fullmatch(r'[0-9a-f]{64}', str(launcher_hash)) or
            hashlib.sha256(launcher.read_bytes()).hexdigest() != launcher_hash or
            (schema == STAGE_SCHEMA and launcher_hash != current_hash) or
            data.get('launcher_policy') != {'path': 'bin/wine', 'game_launch_only': False}):
        problems.append('staged launcher does not match its recorded source hash and policy')
    if (source / 'bin/yaagl-frame-probe-exec').exists():
        problems.append('obsolete FSR launch helper remains in staged runtime')
    fallback = data.get('native_fg_fallback')
    expected_exports = [{'ordinal': ordinal, 'name': name} for ordinal, name in ORIGINAL_FG_EXPORTS]
    if (not isinstance(fallback, dict) or fallback.get('runtime_path') != PRIVATE_FG_PATH or
            fallback.get('sha256') != ORIGINAL_FG_SHA256 or fallback.get('architecture') != 'COFF-x86-64' or
            fallback.get('exports') != expected_exports or fallback.get('loader_override') is not False):
        problems.append('native frame-generation fallback provenance is missing or invalid')
    private_fallback = source / PRIVATE_FG_PATH
    if not private_fallback.is_file() or private_fallback.stat().st_mode & 0o222:
        problems.append('renamed native frame-generation fallback is missing or writable')
    d3dmetal_input = data.get('d3dmetal_input')
    if (not isinstance(d3dmetal_input, dict) or d3dmetal_input.get('kind') not in ('pristine', 'stage-patched', 'stage-patched-signed', 'patched') or
            not re.fullmatch(r'[0-9a-f]{64}', str(d3dmetal_input.get('sha256', '')))):
        problems.append('recorded D3DMetal input kind or SHA is invalid')
    if problems:
        return [('FAIL', message) for message in problems]
    return [('PASS', 'staged FSR/NGX policy and signed artifact hashes match')]


def verify_runtime(runtime: Path, current_sources: bool = False) -> list[tuple[str, str]]:
    report = stage_manifest_report(runtime)
    if not (runtime / STAGE_MANIFEST).is_file():
        return [('FAIL', 'staged-runtime manifest is missing')]
    if any(level == 'FAIL' for level, _ in report):
        return report
    data = json.loads((runtime / STAGE_MANIFEST).read_text())
    try:
        if data.get('display_bridge') != display_bridge_provenance(runtime):
            report.append(('FAIL', 'Wine display bridge identity does not match the staged runtime'))
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        report.append(('FAIL', str(error)))
    if not current_sources:
        return report
    for label, key in (('native', 'native_build_manifest'), ('FSR translator', 'fsr_build_manifest')):
        build_manifest = data.get(key)
        sources = build_manifest.get('sources') if isinstance(build_manifest, dict) else None
        if not isinstance(sources, list) or not sources:
            report.append(('FAIL', f'{label} build source inventory is missing'))
            continue
        for source in sources:
            if not isinstance(source, dict) or not isinstance(source.get('path'), str):
                report.append(('FAIL', f'{label} has an invalid source inventory entry'))
                continue
            path = ROOT / source['path']
            if (not under(path, ROOT) or not path.is_file() or
                    hashlib.sha256(path.read_bytes()).hexdigest() != source.get('sha256')):
                report.append(('FAIL', f'build source changed: {source["path"]}'))
    launcher = data.get('source_launcher', {})
    if launcher.get('sha256') != hashlib.sha256((ROOT / 'scripts/wine-launch-wrapper-p3.sh').read_bytes()).hexdigest():
        report.append(('FAIL', 'build source changed: scripts/wine-launch-wrapper-p3.sh'))
    if not any(level == 'FAIL' for level, _ in report):
        report.append(('PASS', 'native, FSR translator, and launcher source hashes match current sources'))
    return report


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--wine-source', type=Path)
    p.add_argument('--wine-dest', type=Path)
    d3dmetal = p.add_mutually_exclusive_group()
    d3dmetal.add_argument('--pristine-d3dmetal', type=Path, help='pinned pre-PSO f864 framework')
    d3dmetal.add_argument('--patched-d3dmetal', type=Path, help='strictly verified current-layout patched framework')
    p.add_argument('--verify-runtime', type=Path)
    p.add_argument('--current-sources', action='store_true')
    p.add_argument('--build-dir', type=Path, default=ROOT / 'build/fsr-stage')
    p.add_argument('--original-fg', type=Path, default=ORIGINAL_FG_SOURCE)
    p.add_argument('--ngx-dll', type=Path, help='pinned stock GPTK nvngx-on-metalfx.dll input')
    p.add_argument('--play', action='store_true', help='select ordinary play with automatic captures disabled')
    p.add_argument('--fsr-translator', action='store_true', help='select the FSR-only MetalFX runtime')
    p.add_argument('--check', action='store_true')
    a = p.parse_args()
    if a.verify_runtime:
        if a.wine_source or a.wine_dest or a.pristine_d3dmetal or a.patched_d3dmetal or a.ngx_dll or a.check or a.play or a.fsr_translator:
            p.error('--verify-runtime cannot be combined with staging arguments')
        report = verify_runtime(a.verify_runtime.expanduser().resolve(), a.current_sources)
        for level, message in report:
            print(f'{level}: {message}')
        return int(any(level == 'FAIL' for level, _ in report))
    if not a.wine_source or not a.wine_dest or not a.ngx_dll or not (a.pristine_d3dmetal or a.patched_d3dmetal):
        p.error('staging requires --wine-source, --wine-dest, --ngx-dll and exactly one D3DMetal input')
    if a.current_sources:
        p.error('--current-sources requires --verify-runtime')
    if not a.play or not a.fsr_translator:
        p.error('the FSR/NGX runtime requires --play --fsr-translator')
    source = a.wine_source.expanduser().resolve()
    dest = a.wine_dest.expanduser().absolute()
    original_fg = a.original_fg.expanduser().resolve()
    ngx_dll = a.ngx_dll.expanduser().resolve()
    d3dmetal_input = (a.patched_d3dmetal or a.pristine_d3dmetal).expanduser().resolve()
    input_kind = 'patched' if a.patched_d3dmetal else 'pristine'
    build = a.build_dir.expanduser().absolute()
    if dest.exists() or dest.is_symlink():
        p.error('wine-dest must not already exist')
    if under(dest, source) or under(source, dest):
        p.error('source and destination must not contain one another')
    if not (source / 'bin/wine').is_file() or not (source / REL / 'D3DMetal').is_file():
        p.error('expected a complete Wine runtime with bin/wine and D3DMetal.framework')
    source_report = stage_manifest_report(source)
    failed = [message for level, message in source_report if level == 'FAIL']
    if failed:
        p.error('; '.join(failed))
    display_bridge_provenance(source)
    launcher_path = ROOT / 'scripts/wine-launch-wrapper-p3.sh'
    wrapper = launcher_path.read_text()
    actual = hashlib.sha256(d3dmetal_input.read_bytes()).hexdigest()
    checker = ROOT / 'scripts/d3dmetal-pso-cache-patch.mjs'
    if input_kind == 'pristine':
        expected = json.loads((ROOT / 'd3dmetal-pso-cache/layout.json').read_text())['source']['sha256']
        if actual != expected:
            inspection = json.loads(subprocess.check_output(['node', str(checker), 'inspect', str(d3dmetal_input)], text=True))
            if inspection.get('mode') not in ('stage-patched', 'stage-patched-signed'):
                p.error(f'D3DMetal is neither pinned pristine ({expected}) nor verified stage-lock input: {actual}')
            input_kind = inspection['mode']
    else:
        inspection = json.loads(subprocess.check_output(['node', str(checker), 'inspect', str(d3dmetal_input)], text=True))
        if inspection.get('mode') not in ('patched', 'patched-signed'):
            p.error('patched D3DMetal must pass the current pinned layout and payload inspection')
    fg_provenance = original_fg_provenance(original_fg)
    ngx_hash = hashlib.sha256(ngx_dll.read_bytes()).hexdigest()
    if ngx_hash != NGX_DLL_SHA256:
        p.error(f'stock NGX DLL SHA mismatch: expected {NGX_DLL_SHA256}, got {ngx_hash}')
    readobj = Path('/opt/llvm-mingw-20260616-ucrt-macos-universal/bin/llvm-readobj')
    if 'Format: COFF-x86-64' not in subprocess.check_output(
            [str(readobj), '--coff-exports', str(ngx_dll)], text=True):
        p.error('stock NGX DLL is not x86_64 PE')
    shared = source / SHARED_DYLIB_PATH
    if not under(shared, source) or hashlib.sha256(shared.read_bytes()).hexdigest() != SHARED_DYLIB_SHA256:
        p.error('source libd3dshared.dylib does not match pinned GPTK bridge')
    ngx_provenance = {'source': str(ngx_dll), 'runtime_path': NGX_ARTIFACT_PATHS[0],
                      'sha256': ngx_hash, 'size': ngx_dll.stat().st_size, 'architecture': 'COFF-x86-64',
                      'unix_bridge': NGX_ARTIFACT_PATHS[1], 'bridge_target': NGX_UNIX_LINK,
                      'bridge_sha256': SHARED_DYLIB_SHA256, 'source_access': 'read-only'}
    if a.check:
        print(f'PASS: isolated destination, {input_kind} D3DMetal identity, pinned native FSR and stock NGX providers verified')
        print('PASS: source launcher will be replaced with the committed FSR/NGX wrapper')
        print('No changes.')
        return 0
    if platform.system() != 'Darwin':
        p.error('staging requires macOS, full Xcode, Node.js and codesign')
    dest.parent.mkdir(parents=True, exist_ok=True)
    build.mkdir(parents=True, exist_ok=True)
    run(['node', str(ROOT / 'scripts/build-d3dmetal-pso-cache.mjs'), str(build)])
    fsr_build = build / 'fsr-translator'
    run([str(ROOT / 'scripts/build-fsr-translator.sh'), str(fsr_build)])
    temporary = dest.parent / (dest.name + '.staging-' + uuid.uuid4().hex)
    try:
        run(['ditto', str(source), str(temporary)])
        binary = temporary / REL / 'D3DMetal'
        module = temporary / REL / 'Resources/libYaaglNativePsoCache.dylib'
        for path in (binary, module, temporary / 'bin/wine',
                     *(temporary / relative for relative in FSR_ARTIFACT_PATHS)):
            if not under(path, temporary):
                raise RuntimeError(f'copied runtime contains an external replacement symlink: {path}')
        for relative in NGX_ARTIFACT_PATHS[:2]:
            (temporary / relative).unlink(missing_ok=True)
        staged_ngx = temporary / NGX_ARTIFACT_PATHS[0]
        staged_ngx.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ngx_dll, staged_ngx)
        staged_bridge = temporary / SHARED_DYLIB_PATH
        if not under(staged_bridge, temporary) or hashlib.sha256(staged_bridge.read_bytes()).hexdigest() != SHARED_DYLIB_SHA256:
            raise RuntimeError('copied GPTK shared bridge changed during staging')
        staged_link = temporary / NGX_ARTIFACT_PATHS[1]
        staged_link.parent.mkdir(parents=True, exist_ok=True)
        staged_link.symlink_to(NGX_UNIX_LINK)
        if input_kind in ('pristine', 'stage-patched', 'stage-patched-signed'):
            patched = build / 'D3DMetal.fsr-ngx'
            run(['node', str(checker), 'patch', str(d3dmetal_input), str(patched)])
            shutil.copy2(patched, binary)
        else:
            shutil.copy2(d3dmetal_input, binary)
        module.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(build / 'libYaaglNativePsoCache.dylib', module)
        for relative in FSR_ARTIFACT_PATHS[:-1]:
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fsr_build / relative, target)
        fallback = temporary / PRIVATE_FG_PATH
        fallback.unlink(missing_ok=True)
        shutil.copy2(original_fg, fallback)
        fallback.chmod(0o444)
        if original_fg_provenance(fallback)['sha256'] != fg_provenance['sha256']:
            raise RuntimeError('copied native FSR provider changed during staging')
        for path in (temporary / FSR_ARTIFACT_PATHS[1], temporary / FSR_ARTIFACT_PATHS[3], module):
            run(['codesign', '--force', '--sign', '-', str(path)])
        framework = temporary / 'lib/external/D3DMetal.framework'
        run(['codesign', '--force', '--sign', '-', str(framework)])
        run(['codesign', '--verify', '--strict', '--verbose=2', str(framework)])
        inspection = json.loads(subprocess.check_output(['node', str(checker), 'inspect', str(binary)], text=True))
        if inspection.get('mode') not in ('patched', 'patched-signed'):
            raise RuntimeError('post-signature D3DMetal byte-span validation failed')
        # Old staged sources may carry the obsolete helper; the new launcher never calls it.
        (temporary / 'bin/yaagl-frame-probe-exec').unlink(missing_ok=True)
        (temporary / 'bin/wine').write_text(wrapper)
        (temporary / 'bin/wine').chmod(0o755)
        manifest = {
            'stage_schema': STAGE_SCHEMA, 'profile': PLAY_PROFILE, 'diagnostic_only': False,
            'rendering_changes_by_default': True, 'render_size_override': False,
            'model_policy': PLAY_MODEL_POLICY.copy(), 'dlss_translation': True,
            'ngx_policy': NGX_POLICY.copy(), 'ngx_module': ngx_provenance,
            'source_runtime': str(source), 'source_launcher': {
                'path': 'scripts/wine-launch-wrapper-p3.sh', 'sha256': hashlib.sha256(wrapper.encode()).hexdigest()},
            'launcher_policy': {'path': 'bin/wine', 'game_launch_only': False},
            'd3dmetal_input': {'kind': input_kind, 'sha256': actual}, 'binary_inspection': inspection,
            'native_build_manifest': json.loads((build / 'build-manifest.json').read_text()),
            'fsr_build_manifest': json.loads((fsr_build / 'build-manifest.json').read_text()),
            'fsr_translator': FSR_POLICY.copy(), 'native_fg_fallback': fg_provenance,
            'display_bridge': display_bridge_provenance(temporary),
            'signed_artifacts': artifact_hashes(temporary, ARTIFACT_PATHS + FSR_ARTIFACT_PATHS + NGX_ARTIFACT_PATHS)}
        (temporary / STAGE_MANIFEST).write_text(json.dumps(manifest, indent=2) + '\n')
        problems = [message for level, message in verify_runtime(temporary, current_sources=True) if level == 'FAIL']
        if problems:
            raise RuntimeError('; '.join(problems))
        os.rename(temporary, dest)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    print(f'FSR/NGX runtime staged: {dest}')
    print('ZenlessZoneZero.exe runs as RX 9070; every other program defaults to RTX 5060.')
    print('FSR and stock NGX modules retained; no manual GPU identity environment.')
    print('System-default MetalFX model, unchanged game sizing, no automatic GPU capture.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
