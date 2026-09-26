#!/usr/bin/env python3
"""Refresh final-byte metadata in the staged FSR/NGX runtime tree.

Rewrites ONLY the four metadata files of the staged runtime:
  yaagl-wine-p3-graphics-artifacts.json
  yaagl-wine-p3-provenance.json
  yaagl-wine-runtime-files.json
  yaagl-wine-p3-runtime.txt

It never touches bin/, DLLs, the D3DMetal framework, zzz-frame-probe-stage.json,
or the signed_artifacts recorded there. The stage manifest is treated as an
immutable record: every signed artifact it lists is re-hashed from the staged
tree and must match before anything is written.

usage:
  python3 scripts/refresh-staged-runtime-metadata.py --tree <staged-wine-root> \
      --base <extracted-v1.0.5-root>/wine \
      --native-manifest build/release-v1.1.0/native-v3/build-manifest.json \
      [--check]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import sys

STAGE_MANIFEST = 'zzz-frame-probe-stage.json'
STAGE_SCHEMA = 6
LEGACY_NGX_SCHEMA = 5
NGX_SCHEMAS = (LEGACY_NGX_SCHEMA, STAGE_SCHEMA)
SUPPORTED_STAGE_SCHEMAS = (3, 4, LEGACY_NGX_SCHEMA, STAGE_SCHEMA)
GRAPHICS_MANIFEST = 'yaagl-wine-p3-graphics-artifacts.json'
PROVENANCE_MANIFEST = 'yaagl-wine-p3-provenance.json'
RUNTIME_MANIFEST = 'yaagl-wine-runtime-files.json'
RUNTIME_TXT = 'yaagl-wine-p3-runtime.txt'
PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
WRITER = PROJECT_ROOT / 'scripts' / 'write-wine-runtime-manifest.py'
PACKAGER = PROJECT_ROOT / 'scripts' / 'package-wine-p3-runtime.sh'

FRAMEWORK_REL = 'lib/external/D3DMetal.framework/Versions/A/D3DMetal'
MODULE_REL = 'lib/external/D3DMetal.framework/Versions/A/Resources/libYaaglNativePsoCache.dylib'
CONVERTER_REL = 'lib/external/D3DMetal.framework/Versions/A/Resources/libmetalirconverter.dylib'
GPTK_SOURCE_SHA256 = '32f8adb414806e63dcb46c4260f399a36b85454ad255e40c3dc5d8e0dfad95eb'
CONVERTER_SHA256 = '5c5619ef17a7d62e84db0a7f5181d746623b47364379271fd5827e6bd961ba34'
DEVICE_LIFETIME = {'scope': 'per-native-device', 'retention': 'device-lifetime'}
FRAMEWORK_DEPENDENCY = '@loader_path/Resources/libYaaglNativePsoCache.dylib'
NGX_MODULE_RELS = ('lib/wine/x86_64-windows/nvngx.dll', 'lib/wine/x86_64-unix/nvngx.so')
NGX_UNIX_LINK = '../../external/libd3dshared.dylib'
SHARED_DYLIB_REL = 'lib/external/libd3dshared.dylib'
NGX_SHA256 = 'f6bc9d77fd1e898fec8c6339d367bd8e0f338992c9c0c66d59b30c6e9e0743e4'
SHARED_SHA256 = 'd932330841e77682d47688641e0ac17049a2aff498deafac88921983dc16eedb'
NGX_POLICY = {'implementation': 'stock-gptk-ngx-to-metalfx', 'windows_module': NGX_MODULE_RELS[0],
              'unix_bridge': NGX_MODULE_RELS[1], 'bridge_target': NGX_UNIX_LINK,
              'supported_gpu_identities': ['rx9070', 'rtx5060'],
              'default_gpu_identity': 'rtx5060', 'gpu_identity_policy': 'per-game',
              'game_gpu_identities': {'ZenlessZoneZero.exe': 'rx9070'}}
# Schema 5 staged bytes ship the superseded manual YAAGL_GPU_IDENTITY launcher; record the
# policy they actually implement so old runtimes are never relabelled with the current one.
LEGACY_NGX_POLICY = {'implementation': 'stock-gptk-ngx-to-metalfx', 'windows_module': NGX_MODULE_RELS[0],
                     'unix_bridge': NGX_MODULE_RELS[1], 'bridge_target': NGX_UNIX_LINK,
                     'default_gpu_identity': 'rx9070', 'gpu_identity_environment': 'YAAGL_GPU_IDENTITY',
                     'supported_gpu_identities': ['rx9070', 'rtx5060']}
NGX_POLICIES = {LEGACY_NGX_SCHEMA: LEGACY_NGX_POLICY, STAGE_SCHEMA: NGX_POLICY}


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tuned_identities() -> list[tuple[str, str, str]]:
    text = PACKAGER.read_text(encoding='utf-8')
    match = re.search(r"TUNED_ARTIFACT_IDENTITIES='(.*?)'\n", text, re.S)
    if not match:
        raise SystemExit('cannot read TUNED_ARTIFACT_IDENTITIES from the packaging script')
    rows = []
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line:
            continue
        path, architecture, artifact_format = line.split('|', 2)
        rows.append((path, architecture, artifact_format))
    if not rows:
        raise SystemExit('TUNED_ARTIFACT_IDENTITIES is empty')
    return rows


def source_fingerprint(sources) -> str:
    accumulator = hashlib.sha256()
    for source in sources:
        accumulator.update(source['path'].encode('utf-8') + b'\0' + source['sha256'].encode('ascii') + b'\n')
    return accumulator.hexdigest()


def system_dependencies(path: pathlib.Path) -> list[str]:
    listing = subprocess.run(['otool', '-arch', 'x86_64', '-l', str(path)],
                             check=True, capture_output=True, text=True).stdout
    names, load = [], False
    for line in listing.splitlines():
        fields = line.split()
        if fields[:1] == ['cmd']:
            load = fields[1:2] == ['LC_LOAD_DYLIB']
            continue
        if load and fields[:1] == ['name']:
            names.append(fields[1])
            load = False
    return names


def read_stage_manifest(tree: pathlib.Path) -> dict:
    path = tree / STAGE_MANIFEST
    if not path.is_file():
        raise SystemExit(f'missing staged-runtime manifest: {path}')
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('stage_schema') not in SUPPORTED_STAGE_SCHEMAS:
        raise SystemExit(f'unsupported stage schema: {data.get("stage_schema")!r}')
    schema = data['stage_schema']
    if (data.get('dlss_translation') is not (schema in NGX_SCHEMAS) or
            data.get('model_policy') != {'all_gpu': 'system-default'} or
            (schema in NGX_SCHEMAS and data.get('ngx_policy') != NGX_POLICIES[schema])):
        raise SystemExit('staged runtime does not carry the recorded FSR/NGX system-default policy')
    return data


def assert_tree_untouched(tree: pathlib.Path, stage: dict) -> None:
    """The signed artifacts are the immutable part of the stage record."""
    recorded = stage.get('signed_artifacts')
    if not isinstance(recorded, dict) or not recorded:
        raise SystemExit('stage manifest has no signed_artifacts inventory')
    if stage['stage_schema'] in NGX_SCHEMAS:
        link = tree / NGX_MODULE_RELS[1]
        if not link.is_symlink() or link.readlink().as_posix() != NGX_UNIX_LINK:
            raise SystemExit('stock NGX Unix bridge symlink changed')
    actual_digests: dict[pathlib.Path, str] = {}
    for relative, expected in recorded.items():
        path = tree / relative
        if not path.is_file():
            raise SystemExit(f'signed artifact is missing from the staged tree: {relative}')
        target = path.resolve()
        if target not in actual_digests:
            actual_digests[target] = digest(target)
        if actual_digests[target] != expected:
            raise SystemExit(f'signed artifact changed since staging: {relative}')
    if stage['stage_schema'] not in NGX_SCHEMAS:
        for relative in NGX_MODULE_RELS:
            if (tree / relative).exists() or (tree / relative).is_symlink():
                raise SystemExit(f'NGX module is present in legacy FSR-only runtime: {relative}')
    else:
        ngx = stage.get('ngx_module')
        if (not isinstance(ngx, dict) or ngx.get('sha256') != NGX_SHA256 or
                ngx.get('bridge_sha256') != SHARED_SHA256 or
                ngx.get('runtime_path') != NGX_MODULE_RELS[0] or
                ngx.get('unix_bridge') != NGX_MODULE_RELS[1] or
                ngx.get('bridge_target') != NGX_UNIX_LINK or
                recorded.get(NGX_MODULE_RELS[0]) != NGX_SHA256 or
                recorded.get(NGX_MODULE_RELS[1]) != SHARED_SHA256 or
                recorded.get(SHARED_DYLIB_REL) != SHARED_SHA256):
            raise SystemExit('stock NGX provenance, signed bridge, or symlink is invalid')


def refreshed_graphics(tree: pathlib.Path, base: pathlib.Path, native_build: dict, stage: dict) -> dict:
    current = json.loads((base / GRAPHICS_MANIFEST).read_text(encoding='utf-8'))
    artifacts = current['artifacts']
    artifacts['framework'].update({
        'sha256': digest(tree / FRAMEWORK_REL),
        'preSignSha256': stage['d3dmetal_input']['sha256'],
        'inputKind': stage['d3dmetal_input']['kind'],
        'sourceSha256': GPTK_SOURCE_SHA256,
    })
    artifacts['module'].update({
        'sha256': digest(tree / MODULE_REL),
        'preSignSha256': native_build['module']['sha256'],
        'sourceFingerprintSha256': source_fingerprint(native_build['sources']),
        'systemDependencies': system_dependencies(tree / MODULE_REL),
    })
    artifacts['converter'].update({
        'sha256': digest(tree / CONVERTER_REL),
        'sourceSha256': CONVERTER_SHA256,
    })
    payload = dict(current)
    payload.update({
        'schemaVersion': 3,
        'cache': dict(DEVICE_LIFETIME),
        'functionCache': dict(DEVICE_LIFETIME),
        'frameworkDependency': FRAMEWORK_DEPENDENCY,
        'nativePsoCacheBuild': native_build,
        'artifacts': artifacts,
    })
    return payload


def refreshed_provenance(tree: pathlib.Path, base: pathlib.Path, graphics: dict, stage: dict,
                         identities, native_build: dict) -> dict:
    payload = json.loads((base / PROVENANCE_MANIFEST).read_text(encoding='utf-8'))

    authenticated = []
    for relative in ('bin/wine', 'bin/wine.real', 'bin/wineserver'):
        path = tree / relative
        authenticated.append({'path': relative, 'size': path.stat().st_size, 'sha256': digest(path)})
    payload['authenticatedArtifacts'] = authenticated

    payload['artifactHashSemantics'] = {
        'rebuiltArtifacts': 'SHA-256 of host/build bytes before package rewriting and signing',
        'packagedArtifacts': 'SHA-256 of final packaged bytes after all package rewriting and signing',
    }
    packaged = []
    for relative, architecture, artifact_format in identities:
        packaged.append({'path': relative, 'architecture': architecture, 'format': artifact_format,
                         'sha256': digest(tree / relative)})
    payload['packagedArtifacts'] = packaged
    payload['finalGraphicsArtifacts'] = graphics

    fsr_artifacts = []
    for relative in sorted(stage['signed_artifacts']):
        if 'amd_fidelityfx' in relative:
            fsr_artifacts.append({'path': relative, 'sha256': stage['signed_artifacts'][relative]})
    inherited = 0
    changed = []
    for relative, _, _ in identities:
        if digest(tree / relative) == digest(base / relative):
            inherited += 1
        else:
            changed.append(relative)
    payload['v11RuntimeOverlay'] = {
        'baseRuntime': str(base),
        'stageSchema': stage['stage_schema'],
        'stageManifest': STAGE_MANIFEST,
        'stageManifestSha256': digest(tree / STAGE_MANIFEST),
        'dlssTranslation': stage['dlss_translation'],
        'modelPolicy': dict(stage['model_policy']),
        'fsrTranslatorPolicy': dict(stage['fsr_translator']),
        'd3dmetalInput': dict(stage['d3dmetal_input']),
        'nativeFrameGenerationFallback': dict(stage['native_fg_fallback']),
        'fsrBuildManifest': stage['fsr_build_manifest'],
        'nativeBuildManifest': native_build,
        'fsrArtifacts': fsr_artifacts,
        'ngxPolicy': stage.get('ngx_policy') if stage['stage_schema'] in NGX_SCHEMAS else None,
        'ngxModule': stage.get('ngx_module') if stage['stage_schema'] in NGX_SCHEMAS else None,
        'inheritedCoreArtifacts': inherited,
        'changedCoreArtifacts': changed,
        'removedDlssModules': [] if stage['stage_schema'] in NGX_SCHEMAS else list(NGX_MODULE_RELS),
    }
    return payload


def refreshed_runtime_txt(tree: pathlib.Path, graphics: dict) -> str:
    path = tree / RUNTIME_TXT
    lines = path.read_text(encoding='utf-8').splitlines()
    while lines and lines[-1].startswith(('D3DMetal signed artifact SHA-256:', 'Native PSO cache signed artifact SHA-256:')):
        lines.pop()
    lines.append(f'D3DMetal signed artifact SHA-256: {graphics["artifacts"]["framework"]["sha256"]}')
    lines.append(f'Native PSO cache signed artifact SHA-256: {graphics["artifacts"]["module"]["sha256"]}')
    return '\n'.join(lines) + '\n'


def write_json(path: pathlib.Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tree', type=pathlib.Path, required=True, help='staged runtime tree to refresh')
    parser.add_argument('--base', type=pathlib.Path, required=True, help='extracted v1.0.5 baseline wine/ tree')
    parser.add_argument('--native-manifest', type=pathlib.Path,
                        default=pathlib.Path('build/release-v1.1.0/native-v3/build-manifest.json'))
    parser.add_argument('--check', action='store_true', help='verify without writing')
    args = parser.parse_args()

    tree = args.tree.resolve()
    base = args.base.resolve()
    if not tree.is_dir() or not base.is_dir():
        raise SystemExit('both --tree and --base must exist')
    native_build = json.loads(args.native_manifest.resolve().read_text(encoding='utf-8'))
    stage = read_stage_manifest(tree)
    assert_tree_untouched(tree, stage)
    identities = tuned_identities()

    graphics = refreshed_graphics(tree, base, native_build, stage)
    provenance = refreshed_provenance(tree, base, graphics, stage, identities, native_build)
    runtime_txt = refreshed_runtime_txt(tree, graphics)

    if args.check:
        print('CHECK: signed artifacts match the stage record; metadata refresh is safe to write')
        return 0

    write_json(tree / GRAPHICS_MANIFEST, graphics)
    write_json(tree / PROVENANCE_MANIFEST, provenance)
    (tree / RUNTIME_TXT).write_text(runtime_txt, encoding='utf-8')
    subprocess.run([sys.executable, str(WRITER), str(tree),
                    provenance['runtimeId'], provenance['wineVersion']], check=True)
    assert_tree_untouched(tree, stage)
    print(f'REFRESHED: {tree}')
    print(f'  framework {graphics["artifacts"]["framework"]["sha256"]}')
    print(f'  module    {graphics["artifacts"]["module"]["sha256"]}')
    print(f'  packaged artifacts {len(provenance["packagedArtifacts"])}; inherited core {provenance["v11RuntimeOverlay"]["inheritedCoreArtifacts"]}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        sys.exit(f'refresh-metadata: {error}')
