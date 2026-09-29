#!/usr/bin/env python3
"""Prepare pinned CX sources; package only clean installed Wine plus sealed external inputs."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile

ARCHIVE_SHA = 'ac99c8ca4b3848f3e81784135f023df266b61c2345726ea55a50b3e030dd6872'
ARCHIVE_NAME = 'crossover-sources-26.3.0.tar.gz'
PATCHES = ('0001-yaagl-compat.patch', '0002-arm64-server.patch',
           '0003-d3dmetal-display.patch', '0004-macos-vulkan-loader.patch',
           '0005-msync-failures.patch')
OVERLAY = (
    'include/yaagl_d3dmetal_display.h', 'include/yaagl_fsr_bridge.h',
    'include/yaagl_fsr_fg_bridge.h',
    *(f'dlls/amd_fidelityfx_upscaler_dx12/{name}' for name in
      ('Makefile.in', 'amd_fidelityfx_upscaler_dx12.spec', 'main.c', 'unixlib.c', 'unixlib.h')),
    *(f'dlls/amd_fidelityfx_framegeneration_dx12/{name}' for name in
      ('Makefile.in', 'amd_fidelityfx_framegeneration_dx12.spec', 'main.c', 'unixlib.c', 'unixlib.h')),
    *(f'd3dmetal-pso-cache/third-party/fidelityfx/Kits/FidelityFX/{name}' for name in
      ('api/include/ffx_api.h', 'api/include/ffx_api_types.h',
       'api/include/dx12/ffx_api_dx12.h', 'upscalers/include/ffx_upscale.h',
       'framegeneration/include/ffx_framegeneration.h',
       'framegeneration/include/ffx_framegeneration_api_types.h',
       'framegeneration/include/dx12/ffx_api_framegeneration_dx12.h')),
)
RUNTIME_ID = 'wine-cx26.3-d3dmetal-gptk4.0b2-1'
RUNTIME_NAME = 'Wine 11.0 D3DMetal (CX 26.3, GPTK 4.0b2, experimental)'
ARCHIVE_OUT = 'wine-cx26.3-d3dmetal-gptk4.0b2-macos26.tar.xz'
GRAPHICS_INPUTS = frozenset({
    'lib/external/libd3dshared.dylib',
    *(f'lib/wine/x86_64-windows/{name}' for name in
      ('d3d10.dll', 'd3d11.dll', 'd3d12.dll', 'dxgi.dll',
       'nvapi.dll', 'nvapi64.dll', 'nvngx.dll',
       'amd_fidelityfx_framegeneration_dx12_native.dll')),
    *(f'lib/wine/x86_64-unix/{name}' for name in
      ('d3d10.so', 'd3d11.so', 'd3d12.so', 'dxgi.so',
       'nvapi.so', 'nvapi64.so', 'nvngx.so')),
})
DONOR = Path(os.environ.get('WINE_CX_DONOR',
    '/Users/hanbinnoh/Documents/zzz-wine-release-base/beta-f163d14/wine'))
HELPERS = ('prepare-d3dmetal-runtime', 'libYaaglNativePsoCache.dylib', 'build-manifest.json')


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def run(*args, **kwargs):
    return subprocess.run([str(a) for a in args], **{'check': True, **kwargs})


def entries(root, exclude=()):
    result = []
    for parent, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        files.sort()
        for name in [*dirs, *files]:
            path = Path(parent) / name
            relative = path.relative_to(root).as_posix()
            if relative in exclude:
                continue
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                result.append({'path': relative, 'type': 'symlink', 'target': os.readlink(path)})
            elif stat.S_ISREG(mode):
                result.append({'path': relative, 'type': 'file', 'size': path.stat().st_size,
                               'sha256': sha(path)})
    return sorted(result, key=lambda item: item['path'])


def tree_fingerprint(root):
    records = entries(root)
    for item in records:
        item['mode'] = stat.S_IMODE((root / item['path']).lstat().st_mode)
    digest = hashlib.sha256(json.dumps(records, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'entries': len(records), 'sha256': digest}


def pins(repo, root):
    archive = root / 'source-cache' / ARCHIVE_NAME
    require(archive.is_file() and sha(archive) == ARCHIVE_SHA, 'CX source archive checksum mismatch')
    patches = [repo / 'patches/wine-cx' / name for name in PATCHES]
    for patch in patches:
        require(patch.is_file() and patch.stat().st_size, f'missing selected CX patch: {patch}')
    for relative in OVERLAY:
        require((repo / relative).is_file(), f'missing selected new source: {relative}')
    return archive, [{'path': f'patches/wine-cx/{p.name}', 'sha256': sha(p)} for p in patches], [
        {'path': relative, 'sha256': sha(repo / relative)} for relative in OVERLAY]


def patch_targets(patch):
    paths = re.findall(r'^\+\+\+ b/([^\r\n]+)$', patch.read_text(), re.M)
    require(paths and all(not Path(p).is_absolute() and '..' not in Path(p).parts for p in paths),
            f'unsafe or empty patch: {patch}')
    return paths


def prepared_metadata(repo, root):
    archive, patches, overlay = pins(repo, root)
    return archive, {'schemaVersion': 1, 'sourceArchive': {'name': archive.name, 'sha256': ARCHIVE_SHA},
                     'patches': patches, 'newSources': overlay}


def check_prepared(repo, root):
    source = root / 'source-root/sources/wine'
    stamp = root / 'source-root/yaagl-cx-prepared.json'
    _, expected = prepared_metadata(repo, root)
    require(stamp.is_file() and source.is_dir(), 'missing prepared CX source/stamp')
    actual = json.loads(stamp.read_text())
    for key in expected:
        require(actual.get(key) == expected[key], f'prepared source inputs changed: {key}')
    require((source / 'VERSION').read_text().strip() == 'Wine version 11.0', 'unexpected CX Wine version')
    protocol = (source / 'include/wine/server_protocol.h').read_text()
    require(re.search(r'^#define SERVER_PROTOCOL_VERSION 1809$', protocol, re.M) is not None,
            'unexpected CX server protocol')
    for item in actual['preparedFiles']:
        require(sha(source / item['path']) == item['sha256'], f'prepared source changed: {item["path"]}')
    require(actual.get('sourceTree') == tree_fingerprint(source),
            'prepared CX source tree differs from sealed pristine-plus-patches input')
    return actual


def prepare(repo, root):
    archive, metadata = prepared_metadata(repo, root)
    destination = root / 'source-root'
    if destination.exists():
        check_prepared(repo, root)
        return
    temporary = root / 'source-root.extracting'
    require(not temporary.exists(), f'incomplete extraction; inspect/remove manually: {temporary}')
    temporary.mkdir(parents=True)
    try:
        # This archive is content-addressed; extract only Wine, not bundled upstream extras.
        run('tar', '-xzf', archive, '-C', temporary, 'sources/wine')
        source = temporary / 'sources/wine'
        require((source / 'VERSION').read_text().strip() == 'Wine version 11.0',
                'unexpected CX Wine version')
        touched = set()
        for item in metadata['patches']:
            patch = repo / item['path']
            touched.update(patch_targets(patch))
            with patch.open('rb') as stream:
                run('patch', '-d', source, '-p1', '-F', '0', '--forward', '--batch', input=stream.read(),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for item in metadata['newSources']:
            relative = item['path']
            target = source / relative
            require(not target.exists() and not target.is_symlink(),
                    f'new-source overlay would replace official CX file: {relative}')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(repo / relative, target)
            touched.add(relative)
        metadata['preparedFiles'] = [{'path': name, 'sha256': sha(source / name)}
                                     for name in sorted(touched)]
        metadata['sourceTree'] = tree_fingerprint(source)
        (temporary / 'yaagl-cx-prepared.json').write_text(json.dumps(metadata, indent=2) + '\n')
        temporary.rename(destination)
    except BaseException:
        shutil.rmtree(temporary)
        raise
    check_prepared(repo, root)


def donor_inputs(repo):
    pins_file = repo / 'scripts/wine-crossover-inputs.json'
    data = json.loads(pins_file.read_text())
    require(data.get('schemaVersion') == 1, 'unknown CX donor pin schema')
    donor_manifest = DONOR / data['donor']['manifest']
    require(sha(donor_manifest) == data['donor']['manifestSha256'], 'donor inventory seal mismatch')
    manifest = json.loads(donor_manifest.read_text())
    provenance = json.loads((DONOR / 'yaagl-wine-p3-provenance.json').read_text())
    require(provenance.get('sourceCommit') == data['donor']['sourceCommit'],
            'wrong donor provenance')
    require(manifest.get('schemaVersion') == 1 and manifest.get('runtimeId') == provenance.get('runtimeId'),
            'wrong donor runtime inventory identity')
    # Read and hash the complete sealed donor tree before accepting even one borrowed artifact.
    require(entries(DONOR, (donor_manifest.name,)) == manifest['entries'],
            'donor full-tree inventory mismatch')
    by_path = {item['path']: item for item in manifest['entries']}
    selected = list(data['graphics'])
    require({item['path'] for item in selected} == GRAPHICS_INPUTS and
            len(selected) == len(GRAPHICS_INPUTS), 'unapproved GPTK donor module')
    require(data['dependencies'] == {'topLevelDylibs': True,
            'trees': ['lib/GStreamer.framework']}, 'unapproved dependency input scope')
    selected += [v for k, v in by_path.items() if k.startswith('lib/') and
                 k.count('/') == 1 and k.endswith('.dylib')]
    selected += [v for k, v in by_path.items() if k.startswith('lib/GStreamer.framework/')]
    # For framework symlink root itself, include the directory with copytree below.
    require(len({v['path'] for v in selected}) == len(selected), 'duplicate donor input')
    for item in selected:
        require(by_path.get(item['path']) == item, f'input not sealed by donor: {item["path"]}')
    return data, selected


def copy_input(donor, stage, item):
    relative = item['path']
    source, target = donor / relative, stage / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if item['type'] == 'symlink':
        require(source.is_symlink() and os.readlink(source) == item['target'],
                f'changed donor symlink: {relative}')
        if target.exists() or target.is_symlink():
            target.unlink()
        target.symlink_to(item['target'])
    else:
        require(source.is_file() and not source.is_symlink() and source.stat().st_size == item['size']
                and sha(source) == item['sha256'], f'changed donor input: {relative}')
        if target.exists() or target.is_symlink():
            target.unlink()
        shutil.copy2(source, target)


def verify_pe(path, machine):
    with path.open('rb') as stream:
        data = stream.read(64)
        require(len(data) == 64 and data[:2] == b'MZ', f'not PE: {path}')
        stream.seek(struct.unpack_from('<I', data, 0x3c)[0])
        header = stream.read(6)
        require(header == b'PE\0\0' + struct.pack('<H', machine), f'wrong PE machine: {path}')


def verify_helpers(root):
    autopatch = root / 'autopatch'
    helper_manifest = json.loads((autopatch / 'build-manifest.json').read_text())
    require(helper_manifest.get('schema') == 1 and
            helper_manifest.get('target') == 'arm64-apple-macos26.0',
            'unexpected autopatch build manifest')
    for key, filename in (('helper', HELPERS[0]), ('sidecar', HELPERS[1])):
        src = autopatch / filename
        require(src.is_file() and sha(src) == helper_manifest['artifacts'][key]['sha256'],
                f'autopatch helper hash mismatch: {filename}')
        run('codesign', '--verify', '--strict', src)
    return helper_manifest


def mach(path):
    with path.open('rb') as stream:
        return stream.read(4) in (b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf',
                                  b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe')


def relocate_mach(path, wine):
    lib = wine / 'lib'
    relative = os.path.relpath(lib, path.parent)
    rpath = '@loader_path' if relative == '.' else '@loader_path/' + relative
    listing = subprocess.check_output(['otool', '-L', str(path)], text=True)
    required_rpaths = set()
    for dependency in re.findall(r'^\s+(.*?) \(compatibility version', listing, re.M):
        if dependency.startswith(('/System/', '/usr/lib/')):
            continue
        name = dependency[len('@rpath/'):] if dependency.startswith('@rpath/') else Path(dependency).name
        require(not Path(name).is_absolute() and '..' not in Path(name).parts,
                f'unsafe Mach dependency: {dependency}')
        known = ((lib / name, rpath), (lib / 'external' / name, rpath + '/external'),
                 (lib / 'GStreamer.framework/Versions/1.0/lib' / name,
                  rpath + '/GStreamer.framework/Versions/1.0/lib'),
                 (path.parent / name, '@loader_path'))
        location = next((entry for candidate, entry in known if candidate.exists()), None)
        require(location is not None, f'unbundled Mach dependency: {path}: {dependency}')
        if dependency.startswith('/'):
            run('install_name_tool', '-change', dependency, '@rpath/' + name, path)
        if (dependency.startswith(('@rpath/', '/')) and
                dependency != '@rpath/' + path.name):
            required_rpaths.add(location)
    current = subprocess.check_output(['otool', '-l', str(path)], text=True)
    existing = re.findall(r'cmd LC_RPATH\s+cmdsize \d+\s+path (.*?) \(offset', current)
    for old in set(existing):
        if old.startswith('/') and not old.startswith(('/System/', '/usr/lib/')):
            run('install_name_tool', '-delete_rpath', old, path)
    for entry in sorted(required_rpaths):
        if entry not in existing:
            run('install_name_tool', '-add_rpath', entry, path)
    run('codesign', '--force', '--sign', '-', path)
    run('codesign', '--verify', '--strict', path)


def package(repo, root):
    source_info = check_prepared(repo, root)
    pins_data, borrowed = donor_inputs(repo)
    helper_manifest = verify_helpers(root)
    host = root / 'host'
    require(host.is_dir() and (host / 'bin/wine').is_file() and
            (host / 'bin/wineserver').is_file(), 'missing complete clean CX install')
    require(not (root / 'package').exists(), 'refusing existing package output')
    require(shutil.disk_usage(root).free >= 8 * 1024**3, 'need 8 GiB free before package')
    x64 = root / 'build-x64'
    arm = root / 'build-arm64'
    require((x64 / 'config.status').is_file() and (arm / 'config.status').is_file(),
            'missing configured build provenance')
    output = root / 'package'
    output.mkdir()
    wine = output / 'wine'
    shutil.copytree(host, wine, symlinks=True)
    (wine / 'bin/wine').rename(wine / 'bin/wine.real')
    shutil.copy2(repo / 'scripts/wine-launch-wrapper.sh', wine / 'bin/wine')
    (wine / 'bin/wine').chmod(0o755)
    for item in borrowed:
        copy_input(DONOR, wine, item)
        if item['type'] == 'file' and item['path'].startswith('lib/wine/x86_64-windows/'):
            verify_pe(wine / item['path'], 0x8664)
    # The helper is distributed; Apple's separately installed framework never is.
    autopatch = root / 'autopatch'
    helper_dir = wine / 'libexec/yaagl-d3dmetal'
    helper_dir.mkdir(parents=True)
    for filename in HELPERS:
        src = autopatch / filename
        require(src.is_file(), f'missing autopatch helper: {src}')
        shutil.copy2(src, helper_dir / filename)
    require(not (wine / 'lib/external/D3DMetal.framework').exists(),
            'Apple framework must not be shipped')
    for relative in ('lib/wine/x86_64-windows/amd_fidelityfx_upscaler_dx12.dll',
                     'lib/wine/x86_64-windows/amd_fidelityfx_framegeneration_dx12.dll'):
        verify_pe(wine / relative, 0x8664)
    for relative in ('lib/wine/i386-windows/kernel32.dll',
                     'lib/wine/x86_64-windows/kernel32.dll'):
        verify_pe(wine / relative, 0x014c if 'i386-' in relative else 0x8664)
    run('lipo', '-verify_arch', 'arm64', wine / 'bin/wineserver')
    require(run('lipo', '-verify_arch', 'x86_64', wine / 'bin/wineserver',
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode != 0,
            'server is not ARM64-only')
    run('lipo', '-verify_arch', 'x86_64', wine / 'bin/wine.real')
    # Rewrite all Mach imports to bundled dependencies; never ship build-host paths.
    for parent, _, files in os.walk(wine, followlinks=False):
        for name in sorted(files):
            path = Path(parent) / name
            if path.is_file() and not path.is_symlink() and mach(path):
                if path.parent == helper_dir:
                    imports = subprocess.check_output(['otool', '-L', str(path)], text=True)
                    for dependency in re.findall(r'^\s+(.*?) \(compatibility version', imports, re.M):
                        require(dependency.startswith(('/System/', '/usr/lib/')) or
                                dependency == '@rpath/' + path.name,
                                f'unexpected sealed helper import: {path}: {dependency}')
                    continue
                relocate_mach(path, wine)
    for key, filename in (('helper', HELPERS[0]), ('sidecar', HELPERS[1])):
        require(sha(helper_dir / filename) == helper_manifest['artifacts'][key]['sha256'],
                f'sealed helper changed during packaging: {filename}')
        run('codesign', '--verify', '--strict', helper_dir / filename)
    version = subprocess.check_output([str(wine / 'bin/wine'), '--version'], text=True).strip()
    require(version == 'wine-11.0', f'unexpected installed Wine version: {version}')
    for item in entries(wine):
        if item['type'] == 'symlink':
            target = wine / item['path']
            require(not os.path.isabs(item['target']) and target.exists(),
                    f'broken/absolute staged symlink: {item["path"]}')
    provenance = {
        'schemaVersion': 1, 'name': RUNTIME_NAME, 'runtimeId': RUNTIME_ID, 'wineVersion': version,
        'source': source_info, 'serverProtocol': 1809, 'deploymentTarget': '26.0',
        'buildArchitectures': {'loaderAndUnix': 'x86_64', 'windowsPE': ['i386', 'x86_64'],
                               'wineserver': 'arm64'},
        'configure': {'x86_64': sha(x64 / 'config.status'), 'arm64': sha(arm / 'config.status')},
        'donor': {'sourceCommit': pins_data['donor']['sourceCommit'],
                  'inventorySha256': pins_data['donor']['manifestSha256'],
                  'inputs': borrowed},
        'autopatch': {'manifestSha256': sha(helper_dir / 'build-manifest.json'),
                      'helperSha256': sha(helper_dir / HELPERS[0]),
                      'sidecarSha256': sha(helper_dir / HELPERS[1])},
        'framework': {'included': False, 'installation': 'native autopatch before Wine loading',
                      'destination': 'lib/external/D3DMetal.framework'},
        'archive': ARCHIVE_OUT, 'archiveRoot': 'wine',
        'entries': entries(wine),
    }
    metadata = wine / 'yaagl-d3dmetal-runtime.json'
    metadata.write_text(json.dumps(provenance, indent=2, sort_keys=True) + '\n')
    require(entries(wine, (metadata.name,)) == provenance['entries'], 'staged inventory changed')
    archive = output / ARCHIVE_OUT
    with tarfile.open(archive, 'w:xz', preset=6) as stream:
        stream.add(wine, arcname='wine')
    with tarfile.open(archive, 'r:xz') as stream:
        for member in stream:
            require(member.name == 'wine' or member.name.startswith('wine/'),
                    'invalid archive root')
            require('D3DMetal.framework' not in member.name, 'Apple framework leaked')
    (output / 'SHA256SUMS').write_text(f'{sha(archive)}  {archive.name}\n')
    print(json.dumps({'archive': str(archive), 'sha256': sha(archive),
                      'size': archive.stat().st_size}, indent=2))


def main():
    if len(sys.argv) != 4 or sys.argv[1] not in ('check-inputs', 'prepare', 'verify-prepared', 'package'):
        raise SystemExit('usage: package-wine-crossover.py check-inputs|prepare|verify-prepared|package REPO ROOT')
    command, repo, root = sys.argv[1], Path(sys.argv[2]).resolve(), Path(sys.argv[3]).resolve()
    if command == 'check-inputs':
        _, patches, overlay = pins(repo, root)
        _, borrowed = donor_inputs(repo)
        verify_helpers(root)
        print(f'CX archive verified; {len(patches)} patches, {len(overlay)} new sources, '
              f'{len(borrowed)} sealed external inputs, signed helper')
    elif command == 'prepare':
        prepare(repo, root)
    elif command == 'verify-prepared':
        check_prepared(repo, root)
    else:
        package(repo, root)


if __name__ == '__main__':
    main()
