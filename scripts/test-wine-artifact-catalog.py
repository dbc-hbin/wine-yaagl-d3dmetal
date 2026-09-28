#!/usr/bin/env python3
"""Fail-closed catalog and baseline packaging contracts."""

import copy
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

from wine_artifacts import load_catalog, profile_artifacts
spec = importlib.util.spec_from_file_location('package_yaagl_runtime',
                                              Path(__file__).with_name('package-yaagl-runtime.py'))
packager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packager)
verify_base = packager.verify_base
inventory = packager.inventory
verify_built_artifact = packager.verify_built_artifact


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.catalog_path = Path(self.temp.name) / 'catalog.json'
        self.catalog = load_catalog()

    def reject(self, mutation):
        candidate = copy.deepcopy(self.catalog)
        mutation(candidate)
        self.catalog_path.write_text(json.dumps(candidate))
        with self.assertRaises(ValueError):
            load_catalog(self.catalog_path)

    def test_unsafe_build_path_and_wrong_artifact_identity_fail_closed(self):
        self.reject(lambda data: data['profiles']['yaagl-overlay']['buildDirs'].update({'x86_64': '../build'}))
        self.reject(lambda data: data['artifacts'][0].update({'makeTarget': '/tmp/ntdll.so'}))
        self.reject(lambda data: data['artifacts'][0].update({'installedPath': 'lib/wine/i386-windows/ntdll.so'}))
        self.reject(lambda data: data['artifacts'][0].update({'installedPath': 'lib/wine/x86_64-unix/other.so'}))
        self.reject(lambda data: data['artifacts'][0].update({'architecture': 'arm64'}))

    def test_duplicate_destinations_and_unknown_profile_fail_closed(self):
        self.reject(lambda data: data['artifacts'][1].update({'installedPath': data['artifacts'][0]['installedPath']}))
        self.reject(lambda data: data['profiles']['yaagl-overlay']['artifacts'].append('ntdll'))
        self.reject(lambda data: data['profiles'].update({'unknown': {'artifacts': ['ntdll']}}))
        with self.assertRaises(ValueError):
            profile_artifacts('unknown', self.catalog)

class PackagingContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / 'baseline'
        self.base.mkdir()
        self.provenance = self.base / 'yaagl-wine-p3-provenance.json'
        self.manifest = self.base / 'yaagl-wine-runtime-files.json'
        self.provenance.write_text(json.dumps({'sourceCommit': 'f163d14e24fb1f1e2e8e6a1a319ac82fc9e98a8e',
                                               'runtimeId': 'verified-input'}))
        (self.base / 'bin').mkdir()
        (self.base / 'bin/wine').write_bytes(b'baseline')
        self.write_manifest()

    def write_manifest(self):
        self.manifest.write_text(json.dumps({'schemaVersion': 1, 'runtimeId': 'verified-input',
                                             'entries': inventory(self.base, (self.manifest.name,))}))

    def test_modified_baseline_byte_rejected_before_copy(self):
        verify_base(self.base)
        (self.base / 'bin/wine').write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'full-tree inventory mismatch'):
            verify_base(self.base)

    def test_wrong_baseline_commit_rejected_even_with_valid_tree_manifest(self):
        self.provenance.write_text(json.dumps({'sourceCommit': 'wrong', 'runtimeId': 'verified-input'}))
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, 'unexpected baseline source commit'):
            verify_base(self.base)

    def test_wrong_pe_machine_rejected(self):
        artifact = next(item for item in profile_artifacts('yaagl-overlay') if item['format'] == 'pe')
        pe = self.base / 'module.dll'
        header = bytearray(70)
        header[:2] = b'MZ'
        struct.pack_into('<I', header, 0x3c, 64)
        header[64:68] = b'PE\0\0'
        struct.pack_into('<H', header, 68, 0x014c)
        pe.write_bytes(header)
        with self.assertRaisesRegex(ValueError, 'invalid PE machine'):
            verify_built_artifact(pe, artifact)
        struct.pack_into('<H', header, 68, 0x8664)
        pe.write_bytes(header)
        verify_built_artifact(pe, artifact)

    def test_real_packager_rejects_missing_overlay_without_archive(self):
        output = self.base.parent / 'output'
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('package-yaagl-runtime.py')),
                                 str(self.base), str(self.base / 'overlay'),
                                 str(self.base / 'autopatch'), str(output)],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('bin/wineserver', result.stderr)
        self.assertFalse((output / packager.NAME).exists())


if __name__ == '__main__':
    unittest.main()
