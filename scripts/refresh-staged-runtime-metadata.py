#!/usr/bin/env python3
"""Derive beta3 package metadata from the immutable full-source staging record.

No old baseline metadata is accepted or relabelled. Binary and symlink inventories
must match before writing; --check performs validation without changing files.
"""
from __future__ import annotations
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.util.spec_from_file_location('stage_runtime', ROOT / 'scripts/stage-runtime.py')
stage = importlib.util.module_from_spec(loader)
loader.loader.exec_module(stage)


def derived_metadata(tree: Path, record: dict) -> tuple[dict, dict, str]:
    hashes = stage.load_spec()['hashes']
    graphics = {'schemaVersion': 2, 'gptkVersion': '4.0b3',
                'stageManifestSha256': stage.digest(tree / stage.STAGE_MANIFEST),
                'ngxProvider': 'stock-gptk-ngx-to-metalfx',
                'licenseAcceptance': record['licenseAcceptance'],
                'files': record['signed_artifacts'], 'symlinks': record['symlinks'],
                'nativeSidecarBuild': record['native_build_manifest'],
                'artifacts': {
                    'framework': {'path': stage.IMAGE, 'sourceSha256': hashes['pristineD3DMetal'],
                                  'payloadSha256': hashes['compositePayload'], 'sha256': stage.digest(tree / stage.IMAGE)},
                    'converter': {'path': stage.CONVERTER, 'sourceSha256': hashes['pristineConverter'],
                                  'sha256': stage.digest(tree / stage.CONVERTER), 'patched': False},
                    'module': {'path': stage.SIDECAR, 'sha256': stage.digest(tree / stage.SIDECAR),
                               'preSignSha256': record['native_build_manifest']['module']['sha256']}}}
    provenance = dict(record['source_build'])
    provenance.update(stageManifestSha256=graphics['stageManifestSha256'],
                      finalGraphicsArtifacts=graphics,
                      artifactHashSemantics={'hostArtifacts': 'installed source-build bytes before staging',
                                             'signed_artifacts': 'final bytes after relocation and signing'},
                      distribution='local-operator-artifact; Apple redistribution permission not implied')
    text = (f"Wine full current-source build: {provenance['wineVersion']}\n"
            f"Runtime ID: {provenance['runtimeId']}\n"
            f"Source commit: {provenance['sourceCommit']}\n"
            f"Source tree SHA-256: {provenance['sourceTreeSha256']}\n"
            'Graphics: GPTK 4.0b3; native GPTK caches; four-hook sidecar; stock NGX-to-MetalFX; pristine converter\n'
            f"D3DMetal signed artifact SHA-256: {graphics['artifacts']['framework']['sha256']}\n"
            f"Native sidecar signed artifact SHA-256: {graphics['artifacts']['module']['sha256']}\n")
    return graphics, provenance, text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tree', type=Path, required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    tree = args.tree.expanduser().resolve()
    record = stage.validate_stage(tree)
    graphics, provenance, text = derived_metadata(tree, record)
    if args.check:
        print('CHECK: immutable beta3 stage record matches all runtime bytes and symlinks')
        return 0
    for name, value in ((stage.GRAPHICS_MANIFEST, graphics),
                        ('yaagl-wine-p3-provenance.json', provenance)):
        path = tree / name
        if path.is_symlink():
            raise ValueError(f'metadata output must not be a symlink: {name}')
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    text_path = tree / 'yaagl-wine-p3-runtime.txt'
    if text_path.is_symlink():
        raise ValueError('runtime text metadata must not be a symlink')
    text_path.write_text(text)
    subprocess.run([sys.executable, str(ROOT / 'scripts/write-wine-runtime-manifest.py'),
                    str(tree), provenance['runtimeId'], 'wine-' + provenance['wineVersion']], check=True)
    print(f'REFRESHED: {tree}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        sys.exit(f'refresh-metadata: {error}')
