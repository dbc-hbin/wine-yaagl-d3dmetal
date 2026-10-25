#!/usr/bin/env python3
"""Stage a full-source Wine host with pristine beta3 graphics and the current sidecar.

Local operator artifact only: accepting Apple's license does not grant redistribution.
Inputs are never changed; output is published only after complete validation.
"""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.util.spec_from_file_location('stage_runtime', ROOT / 'scripts/stage-runtime.py')
stage = importlib.util.module_from_spec(loader)
loader.loader.exec_module(stage)


def replace(source: Path, target: Path) -> None:
    # Never follow a previous module alias when replacing one bridge or DLL.
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    if source.is_symlink():
        target.symlink_to(os.readlink(source))
    else:
        shutil.copy2(source, target)


def macho(path: Path) -> bool:
    with path.open('rb') as stream:
        return stream.read(4) in (b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe',
                                 b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf', b'\xbe\xba\xfe\xca')


def load_commands(path: Path) -> tuple[list[str], list[str], str | None]:
    text = subprocess.check_output(['otool', '-l', str(path)], text=True)
    dependencies, rpaths, identity = [], [], None
    for block in re.split(r'Load command \d+\n', text)[1:]:
        command = re.search(r'^\s*cmd (\S+)', block, re.M)
        if not command:
            continue
        kind = command.group(1)
        if kind == 'LC_RPATH':
            match = re.search(r'^\s*path (.*?) \(offset ', block, re.M)
            if match:
                rpaths.append(match.group(1))
        elif kind in ('LC_LOAD_DYLIB', 'LC_LOAD_WEAK_DYLIB', 'LC_REEXPORT_DYLIB', 'LC_LOAD_UPWARD_DYLIB', 'LC_ID_DYLIB'):
            match = re.search(r'^\s*name (.*?) \(offset ', block, re.M)
            if match:
                if kind == 'LC_ID_DYLIB':
                    identity = match.group(1)
                else:
                    dependencies.append(match.group(1))
    return list(dict.fromkeys(dependencies)), list(dict.fromkeys(rpaths)), identity


def relocate_runtime(runtime: Path) -> list[dict]:
    """Close the Mach-O dependency graph inside the staged tree or system libraries."""
    changes = []
    for path in sorted(runtime.rglob('*')):
        if path.is_symlink() or not path.is_file() or not macho(path):
            continue
        dependencies, rpaths, identity = load_commands(path)
        edits = []
        preserve = path.relative_to(runtime).as_posix() in (
            stage.IMAGE, stage.CONVERTER, stage.SHARED_DYLIB_PATH, stage.game_mode.POLICY_TOOL)
        def expand(value):
            return Path(value.replace('@loader_path', str(path.parent)).replace('@executable_path', str(runtime / 'bin')))
        for dep in dependencies:
            if dep.startswith(('/System/', '/usr/lib/')):
                continue
            if dep.startswith(('@loader_path/', '@executable_path/')):
                found = expand(dep)
                if not stage.under(found, runtime) or not found.is_file():
                    raise ValueError(f'unresolved relative dependency {dep} in {path}')
                continue
            if dep.startswith('@rpath/'):
                suffix = dep[len('@rpath/'):]
                candidates = [expand(p) / suffix for p in rpaths if not p.startswith('@rpath')]
                candidates += [runtime / base / suffix for base in ('lib', 'lib/external', 'lib/GStreamer.framework/Versions/1.0/lib')]
            elif dep.startswith('/'):
                # Absolute SDK/build paths must be replaced, including framework install names.
                suffix = dep[dep.index('GStreamer.framework/'):] if 'GStreamer.framework/' in dep else Path(dep).name
                candidates = [runtime / 'lib' / suffix,
                              runtime / 'lib/GStreamer.framework/Versions/1.0/lib' / Path(dep).name]
            else:
                raise ValueError(f'unsupported dependency {dep} in {path}')
            found = next((p for p in candidates if stage.under(p, runtime) and p.is_file()), None)
            if found is None:
                raise ValueError(f'unbundled dependency {dep} in {path}')
            if preserve:
                if dep.startswith('/'):
                    raise ValueError(f'Apple artifact requires a forbidden install-name change: {path}: {dep}')
                continue
            # Explicit loader-relative edges avoid dependence on the developer's rpaths.
            replacement = '@loader_path/' + os.path.relpath(found, path.parent)
            edits += ['-change', dep, replacement]
        if identity and identity.startswith('/') and not preserve:
            edits += ['-id', '@rpath/' + path.name]
        for rpath in rpaths:
            if not preserve and rpath.startswith('/') and not rpath.startswith(('/System/', '/usr/lib/')):
                edits += ['-delete_rpath', rpath]
        if edits:
            subprocess.run(['install_name_tool', *edits, str(path)], check=True)
            subprocess.run(['codesign', '--force', '--sign', '-', str(path)], check=True)
            changes.append({'path': str(path.relative_to(runtime)), 'installNameEdits': edits})
    return changes


def copy_dependencies(source: Path, framework: Path, output: Path) -> None:
    libraries = sorted(source.glob('*.dylib'))
    if not libraries:
        raise ValueError(f'no dependency dylibs found: {source}')
    for library in libraries:
        target = output / 'lib' / library.name
        # Dependency symlinks may be absolute in a local SDK; copy their actual bytes.
        replace(library.resolve(), target)
    dest = output / 'lib/GStreamer.framework'
    if dest.exists() or dest.is_symlink():
        raise ValueError('host already contains GStreamer.framework; use a fresh full source host')
    shutil.copytree(framework, dest, symlinks=True)
    for path in dest.rglob('*'):
        if not path.is_symlink():
            continue
        original = framework / path.relative_to(dest)
        resolved = original.resolve()
        if not stage.under(resolved, framework):
            raise ValueError(f'external GStreamer SDK symlink: {original}')
        if os.path.isabs(os.readlink(path)):
            path.unlink()
            path.symlink_to(os.path.relpath(dest / resolved.relative_to(framework), path.parent))
    # Preserve the established runtime-only GStreamer payload. SDK Python
    # plugins depend on a developer Python framework, not Wine's media runtime.
    version = (dest / 'Versions/Current').resolve()
    for path in (dest / 'Headers', dest / 'Commands', *(version / name for name in (
            'Headers', 'include', 'Commands', 'bin', 'lib/pkgconfig', 'lib/cmake',
            'lib/gstreamer-1.0/libgstpython.dylib', 'share/aclocal', 'share/cmake',
            'share/gir-1.0', 'share/glib-2.0', 'share/gobject-introspection-1.0',
            'share/gtk-doc', 'share/man', 'share/doc', 'share/gstreamer',
            'share/gstreamer-1.0', 'share/gst-validate-launcher')),
            *(version / 'lib').glob('python*')):
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
    scanner = version / 'libexec/gstreamer-1.0/gst-plugin-scanner'
    if not scanner.is_file():
        raise ValueError('GStreamer runtime plugin scanner is missing')
    for path in version.rglob('*'):
        if path.is_file() and (path.suffix in ('.a', '.la', '.pc') or
                              (version / 'libexec' in path.parents and path != scanner)):
            path.unlink()


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('host', 'graphics', 'sidecar', 'output'):
        parser.add_argument(name, type=Path)
    parser.add_argument('--dependency-lib', type=Path, required=True)
    parser.add_argument('--gstreamer-framework', type=Path, required=True)
    parser.add_argument('--native-fg', type=Path, required=True)
    parser.add_argument('--accept-apple-license', action='store_true', required=True,
                        help='explicitly accept the Apple GPTK beta3 license from the graphics input')
    args = parser.parse_args()
    host, graphics, sidecar = (p.expanduser().resolve() for p in (args.host, args.graphics, args.sidecar))
    output = args.output.expanduser().absolute()
    dependency = args.dependency_lib.expanduser().resolve()
    gstreamer = args.gstreamer_framework.expanduser().resolve()
    native_fg = args.native_fg.expanduser().resolve()
    helpers = graphics / 'share/yaagl/steam'
    helper_names = ('steam64.exe', 'steam32.exe', 'lsteamclient64.dll', 'lsteamclient32.dll')
    for name in helper_names:
        if not (helpers / name).is_file() or not stage.under(helpers / name, graphics):
            raise ValueError(f'missing donor Steam helper: {helpers / name}')
    if output.exists() or output.is_symlink():
        raise ValueError(f'refusing existing output: {output}')
    for source in (host, graphics, sidecar, dependency, gstreamer):
        if not source.is_dir() or stage.under(source, output) or stage.under(output, source):
            raise ValueError(f'input and output must be separate directories: {source}')
    spec = stage.load_spec()
    # Validate framework links before copying: replacing a child of an external
    # directory symlink would otherwise mutate an input outside the staging tree.
    stage.inventory(graphics / 'lib/external/D3DMetal.framework', excluded=set())
    stage.check_stock(graphics, spec)
    if stage.digest(graphics / stage.IMAGE) != spec['hashes']['pristineD3DMetal']:
        raise ValueError('graphics input must contain pristine beta3 D3DMetal')
    # The donor framework must be pristine: reject any custom sidecar resource.
    resources = graphics / stage.REL / 'Resources'
    if any(p.name == Path(stage.SIDECAR).name or p.name.startswith('libYaagl') for p in resources.iterdir()):
        raise ValueError('graphics input contains a custom sidecar; use pristine beta3')
    build = json.loads((host.parent / 'provenance.json').read_text())
    stage.validate_source_build(build)
    files, links = stage.inventory(host, excluded=set())
    if files != build['hostArtifacts'] or links != build['hostSymlinks']:
        raise ValueError('installed host differs from the build provenance')
    stage.game_mode.validate(host)
    stage.check_public_fsr(host, build)
    for relative in stage.REQUIRED_HOST:
        if relative not in files:
            raise ValueError(f'full source host is missing: {relative}')
        path = host / relative
        if macho(path):
            architectures = subprocess.check_output(['lipo', '-archs', str(path)], text=True).split()
            expected = (build['wineserverArchitecture'] if relative == 'bin/wineserver' else
                        'arm64' if relative == stage.game_mode.POLICY_TOOL else 'x86_64')
            if expected not in architectures:
                raise ValueError(f'host architecture mismatch: {relative}: expected {expected}')
    if source_tree := build.get('sourceTreeSha256'):
        if source_tree != stage.source_tree_hash(ROOT):
            raise ValueError('host was built from different source bytes; rebuild this worktree')
    if (host / 'bin/wine.real').exists() or any((host / name).exists() for name in stage.DERIVED_METADATA):
        raise ValueError('host is already packaged; use the original full-source install')
    fg = stage.original_fg_provenance(native_fg)
    native_build = json.loads((sidecar / 'build-manifest.json').read_text())
    stage.check_sources(native_build)
    module_source = sidecar / 'libD3DMetalSidecar.dylib'
    if module_source.is_symlink() or stage.digest(module_source) != native_build['module']['sha256']:
        raise ValueError('native sidecar differs from its build manifest')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=output.name + '.staging-', dir=output.parent))
    try:
        shutil.copytree(host, temporary, symlinks=True, dirs_exist_ok=True)
        copy_dependencies(dependency, gstreamer, temporary)
        for name in helper_names:
            replace((helpers / name).resolve(), temporary / 'share/yaagl/steam' / name)
        framework_relative = Path('lib/external/D3DMetal.framework')
        (temporary / 'lib/external').mkdir(parents=True, exist_ok=True)
        shutil.copytree(graphics / framework_relative, temporary / framework_relative, symlinks=True)
        for relative in spec['stock']:
            replace(graphics / relative, temporary / relative)
        for key in ('license', 'acknowledgements'):
            relative = Path('lib/external') / spec['release'][key]['name']
            replace(graphics / relative, temporary / relative)
        for relative, link in stage.NGX_ALIASES.items():
            path = temporary / relative
            path.unlink(missing_ok=True)
            path.symlink_to(link)
        fallback = temporary / stage.PRIVATE_FG_PATH
        replace(native_fg, fallback)
        fallback.chmod(0o444)
        image = temporary / stage.IMAGE
        image.unlink()
        subprocess.run(['node', str(ROOT / 'scripts/d3dmetal-sidecar-patch.mjs'), 'patch',
                        str(graphics / stage.IMAGE), str(image)], check=True)
        replace(module_source, temporary / stage.SIDECAR)
        (temporary / 'bin/wine').rename(temporary / 'bin/wine.real')
        replace(ROOT / 'scripts/wine-launch-wrapper.sh', temporary / 'bin/wine')
        (temporary / 'bin/wine').chmod(0o755)
        # The Apple bridge/converter must not be rewritten; their load paths are already portable.
        relocations = relocate_runtime(temporary)
        for path in (temporary / stage.SIDECAR, temporary / framework_relative,
                     temporary / stage.game_mode.SUPERVISOR, temporary / stage.game_mode.BUNDLE):
            subprocess.run(['codesign', '--force', '--sign', '-', str(path)], check=True)
            subprocess.run(['codesign', '--verify', '--strict', str(path)], check=True)
        stage.check_stock(temporary, spec)
        stage.game_mode.validate(temporary)
        inspection = stage.inspect_image(temporary)
        files, links = stage.inventory(temporary)
        manifest = {'stage_schema': stage.STAGE_SCHEMA, 'gptk_version': '4.0b3',
                    'licenseAcceptance': 'explicit-cli-flag', 'source_build': build,
                    'graphics_source': str(graphics), 'native_build_manifest': native_build,
                    'source_launcher': {'path': 'scripts/wine-launch-wrapper.sh',
                                        'sha256': stage.digest(ROOT / 'scripts/wine-launch-wrapper.sh')},
                    'ngx_policy': stage.NGX_POLICY, 'fsr_translator': stage.FSR_POLICY,
                    'gameMode': stage.game_mode.CAPABILITY,
                    'native_fg_fallback': fg, 'binary_inspection': inspection,
                    'display_bridge': stage.display_bridge_provenance(temporary),
                    'dependency_inputs': {'libraryDirectory': str(dependency), 'gstreamerFramework': str(gstreamer)},
                    'relocations': relocations, 'signed_artifacts': files, 'symlinks': links}
        write_json(temporary / stage.SOURCE_MANIFEST, build)
        write_json(temporary / stage.STAGE_MANIFEST, manifest)
        subprocess.run([sys.executable, str(ROOT / 'scripts/refresh-staged-runtime-metadata.py'),
                        '--tree', str(temporary)], check=True)
        report = stage.verify_runtime(temporary, current_sources=True)
        failures = [message for level, message in report if level == 'FAIL']
        if failures:
            raise ValueError('; '.join(failures))
        os.rename(temporary, output)
    except BaseException:
        shutil.rmtree(temporary)
        raise
    print(output)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        sys.exit(f'stage-full: {error}')
