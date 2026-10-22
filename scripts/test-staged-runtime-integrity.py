#!/usr/bin/env python3
"""Integrity boundaries for the beta3 full-source staging operator path."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import plistlib
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stage = load('stage-runtime')
full = load('stage-full-wine-runtime')
writer = load('write-wine-runtime-manifest')
package = load('package-yaagl-runtime')


class StagedRuntimeIntegrityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='beta3-stage-integrity-')
        self.addCleanup(temporary.cleanup)
        self.tree = Path(temporary.name) / 'wine'
        self.tree.mkdir()
        self.shared = self.tree / stage.SHARED_DYLIB_PATH
        self.pe = self.tree / 'lib/wine/x86_64-windows/nvngx-on-metalfx.dll'
        self.unix = self.tree / 'lib/wine/x86_64-unix/nvngx-on-metalfx.so'
        for path in (self.shared, self.pe, self.unix, self.tree / stage.CONVERTER):
            path.parent.mkdir(parents=True, exist_ok=True)
        self.shared.write_bytes(b'stock beta3 shared bridge')
        self.pe.write_bytes(b'stock beta3 NGX PE')
        self.unix.symlink_to(stage.NGX_UNIX_LINK)
        for relative, target in stage.NGX_ALIASES.items():
            (self.tree / relative).symlink_to(target)
        (self.tree / stage.CONVERTER).write_bytes(b'pristine beta3 converter')
        info = self.tree / stage.REL / 'Resources/Info.plist'
        info.write_bytes(plistlib.dumps({'CFBundleShortVersionString': '4.0b3'}))
        self.spec = {'stock': {str(path.relative_to(self.tree)): stage.digest(path)
                               for path in (self.shared, self.pe, self.unix)},
                     'resources': {path.name: stage.digest(path) for path in info.parent.iterdir()},
                     'hashes': {'pristineConverter': stage.digest(self.tree / stage.CONVERTER)},
                     'release': {}}
        for key, name in (('license', 'License.rtf'), ('acknowledgements', 'Acknowledgements.rtf')):
            path = self.tree / 'lib/external' / name
            path.write_bytes(name.encode())
            self.spec['release'][key] = {'name': name, 'sha256': stage.digest(path)}
        files, links = stage.inventory(self.tree)
        self.record = {'stage_schema': stage.STAGE_SCHEMA, 'signed_artifacts': files, 'symlinks': links}

    def check(self):
        stage.assert_tree_untouched(self.tree, self.record)
        stage.check_stock(self.tree, self.spec)

    def test_changed_signed_module_rejected(self):
        for path in (self.pe, self.shared, self.tree / stage.CONVERTER):
            with self.subTest(path=path):
                original = path.read_bytes()
                path.write_bytes(original + b'corrupt')
                with self.assertRaises(ValueError):
                    self.check()
                path.write_bytes(original)
                self.check()

    def test_required_inventory_entries_are_checked(self):
        for relative in list(self.record['signed_artifacts']):
            expected = self.record['signed_artifacts'].pop(relative)
            with self.subTest(path=relative), self.assertRaises(ValueError):
                self.check()
            self.record['signed_artifacts'][relative] = expected
        self.check()

    def test_rebased_stock_hash_is_rejected(self):
        self.pe.write_bytes(b'custom translator')
        self.record['signed_artifacts'], self.record['symlinks'] = stage.inventory(self.tree)
        with self.assertRaises(ValueError):
            self.check()

    def test_wrong_alias_with_identical_bytes_rejected(self):
        alias = self.tree / 'lib/wine/x86_64-windows/nvngx.dll'
        alternate = self.pe.with_name('alternate.dll')
        alternate.write_bytes(self.pe.read_bytes())
        alias.unlink()
        alias.symlink_to(alternate.name)
        self.record['signed_artifacts'], self.record['symlinks'] = stage.inventory(self.tree)
        with self.assertRaises(ValueError):
            self.check()

    def test_wrong_unix_shared_alias_rejected_even_after_reinventory(self):
        alternative = self.shared.with_name('alternative.dylib')
        alternative.write_bytes(self.shared.read_bytes())
        self.unix.unlink()
        self.unix.symlink_to('../../external/alternative.dylib')
        self.record['signed_artifacts'], self.record['symlinks'] = stage.inventory(self.tree)
        with self.assertRaises(ValueError):
            self.check()

    def test_replacement_does_not_follow_old_bridge_alias(self):
        source = self.pe.with_name('new.so')
        source.write_bytes(b'replacement module')
        original = self.shared.read_bytes()
        full.replace(source, self.unix)
        self.assertFalse(self.unix.is_symlink())
        self.assertEqual(self.unix.read_bytes(), b'replacement module')
        self.assertEqual(self.shared.read_bytes(), original)

    def test_repeated_checks_read_current_bytes(self):
        self.check()
        self.shared.write_bytes(b'changed after first check')
        with self.assertRaises(ValueError):
            self.check()

    def test_beta2_schema_and_version_rejected(self):
        self.record['stage_schema'] = 6
        with self.assertRaises(ValueError):
            self.check()
        self.record['stage_schema'] = stage.STAGE_SCHEMA
        info = self.tree / stage.REL / 'Resources/Info.plist'
        info.write_bytes(plistlib.dumps({'CFBundleShortVersionString': '4.0b2'}))
        self.record['signed_artifacts'], self.record['symlinks'] = stage.inventory(self.tree)
        with self.assertRaises(ValueError):
            self.check()

    def test_external_and_dangling_symlinks_rejected(self):
        alias = self.tree / 'escape'
        outside = self.tree.parent / 'external'
        outside.write_bytes(b'external')
        for target in (str(outside), '../external', 'absent'):
            with self.subTest(target=target):
                alias.symlink_to(target)
                with self.assertRaises(ValueError):
                    stage.inventory(self.tree)
                alias.unlink()

    def test_added_runtime_file_rejected(self):
        (self.tree / 'extra-module').write_bytes(b'unrecorded')
        with self.assertRaises(ValueError):
            self.check()

    def test_manifest_writer_rejects_external_symlink(self):
        (self.tree / 'escape').symlink_to('/etc/hosts')
        with self.assertRaises(SystemExit):
            writer.write_manifest(self.tree, '11.17-gptk4b3-current', 'wine-11.17')

    def test_preexisting_metadata_symlink_rejected(self):
        outside = self.tree.parent / 'metadata.json'
        outside.write_text('{}')
        (self.tree / stage.STAGE_MANIFEST).symlink_to(outside)
        with self.assertRaises(ValueError):
            stage.inventory(self.tree)

    def test_media_runtime_excludes_sdk_python_without_losing_codecs(self):
        dependencies = self.tree.parent / 'dependencies'
        dependencies.mkdir()
        (dependencies / 'libdependency.dylib').write_bytes(b'dependency')
        framework = self.tree.parent / 'GStreamer.framework'
        version = framework / 'Versions/1.0'
        payload = {
            'lib/gstreamer-1.0/libgstlibav.dylib': b'codec',
            'lib/gstreamer-1.0/libgstpython.dylib': b'python plugin',
            'lib/python3.9/site.py': b'python SDK',
            'lib/libgstreamer.a': b'static SDK',
            'libexec/gstreamer-1.0/gst-plugin-scanner': b'scanner',
            'share/licenses/COPYING': b'license',
        }
        for name, content in payload.items():
            path = version / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (framework / 'Versions/Current').symlink_to('1.0')
        (framework / 'Headers').symlink_to('Versions/Current/include')
        (version / 'include').mkdir()
        full.copy_dependencies(dependencies, framework, self.tree)
        installed = self.tree / 'lib/GStreamer.framework/Versions/Current'
        self.assertEqual((installed / 'lib/gstreamer-1.0/libgstlibav.dylib').read_bytes(), b'codec')
        self.assertEqual((installed / 'libexec/gstreamer-1.0/gst-plugin-scanner').read_bytes(), b'scanner')
        self.assertEqual((installed / 'share/licenses/COPYING').read_bytes(), b'license')
        for name in ('lib/gstreamer-1.0/libgstpython.dylib', 'lib/python3.9', 'lib/libgstreamer.a'):
            self.assertFalse((installed / name).exists())
        for name, content in payload.items():
            self.assertEqual((version / name).read_bytes(), content)

    def test_helper_must_match_current_sources_and_staged_native_build(self):
        source = Path(__file__).resolve()
        record = {'path': str(source.relative_to(stage.ROOT)), 'sha256': stage.digest(source)}
        native = {'sources': [dict(record)], 'module': {'sha256': 'a' * 64}}
        helper = {'sources': [dict(record)], 'nativeBuildManifest': native}
        staged = {'native_build_manifest': native}
        package.validate_helper_sources(helper, staged)
        helper['sources'][0]['sha256'] = 'b' * 64
        with self.assertRaises(ValueError):
            package.validate_helper_sources(helper, staged)
        helper['sources'][0] = dict(record)
        native['sources'][0]['sha256'] = 'b' * 64
        with self.assertRaises(ValueError):
            package.validate_helper_sources(helper, staged)
        native['sources'][0] = dict(record)
        staged['native_build_manifest'] = {**native, 'module': {'sha256': 'c' * 64}}
        with self.assertRaises(ValueError):
            package.validate_helper_sources(helper, staged)


class PackageVersionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='beta3-package-version-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.tree = self.root / 'staged'
        self.autopatch = self.root / 'autopatch'
        self.output = self.root / 'output'
        self.autopatch.mkdir()
        (self.tree / 'bin').mkdir(parents=True)
        for name in ('wine', 'wine.real', 'wineserver'):
            (self.tree / 'bin' / name).write_bytes(b'host fixture')
        framework = self.tree / 'lib/external/D3DMetal.framework'
        framework.mkdir(parents=True)
        (framework / 'D3DMetal').write_bytes(b'framework fixture')
        source = Path(__file__).resolve()
        sources = [{'path': str(source.relative_to(stage.ROOT)), 'sha256': stage.digest(source)}]
        native = {'sources': sources, 'module': {'sha256': 'a' * 64}}
        artifacts = {}
        for name, key in zip(package.HELPERS[:2], ('helper', 'sidecar')):
            artifact = self.autopatch / name
            artifact.write_bytes(name.encode())
            artifacts[key] = {'sha256': package.sha(artifact)}
        self.write_json(self.autopatch / 'build-manifest.json', {
            'schema': 2, 'gptkVersion': '4.0b3', 'sources': sources,
            'nativeBuildManifest': native, 'artifacts': artifacts,
        })
        self.record = {'native_build_manifest': native}
        self.set_version('11.17')

    def write_json(self, path, value):
        path.write_text(json.dumps(value))

    def set_version(self, version):
        self.record['source_build'] = {
            'schemaVersion': 3, 'buildKind': 'full-current-source',
            'wineVersion': version, 'runtimeId': 'operator-selected-id',
            'name': 'operator-selected-name',
        }
        self.write_json(self.tree / stage.STAGE_MANIFEST, self.record)
        self.write_json(self.tree / stage.SOURCE_MANIFEST, self.record['source_build'])
        self.write_json(self.tree / 'yaagl-wine-p3-provenance.json', self.record['source_build'])
        self.refresh_inventory()

    def refresh_inventory(self):
        build = self.record['source_build']
        writer.write_manifest(self.tree, build['runtimeId'], 'wine-' + build['wineVersion'])

    def run_package(self, output=None, verification_error=None):
        # Isolate native verification; exercise real metadata checks, copying,
        # inventory generation, and archive serialization without loading Wine.
        with patch.object(package.sys, 'argv', [
                'package-yaagl-runtime.py', str(self.tree), str(self.autopatch),
                str(output or self.output)]), \
                patch.object(package.subprocess, 'run', side_effect=verification_error), \
                contextlib.redirect_stdout(io.StringIO()):
            package.main()

    def test_verified_version_reaches_archive_and_manifest(self):
        for version in ('11.17', '11.18', '12.0-rc1'):
            with self.subTest(version=version):
                self.set_version(version)
                output = self.root / version
                self.run_package(output)
                runtime_id = f'wine-{version}-d3dmetal-gptk4.0b3'
                name = f'{runtime_id}-macos26.tar.xz'
                archive = output / name
                with tarfile.open(archive, 'r:xz') as stream:
                    manifest = json.load(stream.extractfile('wine/yaagl-d3dmetal-runtime.json'))
                    self.assertFalse(any('D3DMetal.framework' in name for name in stream.getnames()))
                self.assertEqual(manifest['runtimeId'], runtime_id)
                self.assertEqual(manifest['wineVersion'], 'wine-' + version)
                self.assertEqual(manifest['archive'], name)
                self.assertEqual(manifest['entries'], package.inventory(
                    output / 'wine', ('yaagl-d3dmetal-runtime.json',)))
                self.assertEqual((output / 'SHA256SUMS').read_text(),
                                 f'{package.sha(archive)}  {name}\n')

    def test_public_provenance_cannot_relabel_verified_build(self):
        path = self.tree / 'yaagl-wine-p3-provenance.json'
        for field, value in (('wineVersion', '11.18'), ('runtimeId', 'different-id'),
                             ('name', 'different-name')):
            with self.subTest(field=field):
                self.write_json(path, {**self.record['source_build'], field: value})
                self.refresh_inventory()
                with self.assertRaisesRegex(ValueError, 'public provenance differs'):
                    self.run_package()
                self.assertFalse(self.output.exists())

    def test_inventory_identity_and_bytes_must_match(self):
        path = self.tree / 'yaagl-wine-runtime-files.json'
        for field, value in (('wineVersion', 'wine-11.18'), ('runtimeId', 'different-id'),
                             ('schemaVersion', 2), ('entries', [])):
            with self.subTest(field=field):
                self.refresh_inventory()
                manifest = json.loads(path.read_text())
                manifest[field] = value
                self.write_json(path, manifest)
                with self.assertRaises(ValueError):
                    self.run_package()
                self.assertFalse(self.output.exists())

    def test_unsafe_version_cannot_become_archive_path(self):
        for version in ('../11.18', '11.18/escape', '11.18\nother', ''):
            with self.subTest(version=version):
                self.set_version(version)
                with self.assertRaisesRegex(ValueError, 'unsafe source Wine version'):
                    self.run_package()
                self.assertFalse(self.output.exists())

    def test_failed_stage_verification_stops_packaging(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_package(verification_error=subprocess.CalledProcessError(1, 'stage-runtime'))
        self.assertFalse(self.output.exists())

    def test_beta2_helper_still_rejected(self):
        path = self.autopatch / 'build-manifest.json'
        helper = json.loads(path.read_text())
        helper['gptkVersion'] = '4.0b2'
        self.write_json(path, helper)
        with self.assertRaisesRegex(ValueError, 'expected beta3 autopatch'):
            self.run_package()
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
