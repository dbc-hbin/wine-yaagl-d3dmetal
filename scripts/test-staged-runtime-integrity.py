#!/usr/bin/env python3
"""Integrity boundaries for staged NGX runtime inventory verification."""

import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).with_name('refresh-staged-runtime-metadata.py')
spec = importlib.util.spec_from_file_location('refresh_staged_runtime_metadata', SCRIPT)
metadata = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metadata)


class StagedRuntimeIntegrityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='staged-runtime-integrity-')
        self.addCleanup(temporary.cleanup)
        self.tree = Path(temporary.name)
        self.dll = metadata.NGX_MODULE_RELS[0]
        self.link = metadata.NGX_MODULE_RELS[1]
        self.bridge = metadata.SHARED_DYLIB_REL
        self.dll_path = self.tree / self.dll
        self.bridge_path = self.tree / self.bridge
        self.link_path = self.tree / self.link
        for path in (self.dll_path, self.bridge_path, self.link_path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self.dll_path.write_bytes(b'original NGX module')
        self.bridge_path.write_bytes(b'original shared bridge')
        self.link_path.symlink_to(metadata.NGX_UNIX_LINK)
        dll_sha = hashlib.sha256(self.dll_path.read_bytes()).hexdigest()
        bridge_sha = hashlib.sha256(self.bridge_path.read_bytes()).hexdigest()
        dll_patch = patch.object(metadata, 'NGX_SHA256', dll_sha)
        bridge_patch = patch.object(metadata, 'SHARED_SHA256', bridge_sha)
        dll_patch.start()
        bridge_patch.start()
        self.addCleanup(dll_patch.stop)
        self.addCleanup(bridge_patch.stop)
        self.stage = {
            'stage_schema': metadata.STAGE_SCHEMA,
            'signed_artifacts': {self.bridge: bridge_sha, self.link: bridge_sha, self.dll: dll_sha},
            'ngx_module': {'sha256': dll_sha, 'bridge_sha256': bridge_sha,
                           'runtime_path': self.dll, 'unix_bridge': self.link,
                           'bridge_target': metadata.NGX_UNIX_LINK},
        }

    def check(self):
        metadata.assert_tree_untouched(self.tree, self.stage)

    def test_changed_bytes_rejected_for_each_signed_module(self):
        for path in (self.dll_path, self.bridge_path):
            with self.subTest(path=path):
                original = path.read_bytes()
                path.write_bytes(original + b'corrupt')
                with self.assertRaises(SystemExit):
                    self.check()
                path.write_bytes(original)
                self.check()

    def test_contradictory_alias_digest_is_rejected(self):
        self.stage['signed_artifacts'][self.link] = '0' * 64
        with self.assertRaises(SystemExit):
            self.check()

    def test_required_inventory_and_provenance_are_checked(self):
        for required in (self.dll, self.link, self.bridge):
            with self.subTest(required=required):
                expected = self.stage['signed_artifacts'].pop(required)
                with self.assertRaises(SystemExit):
                    self.check()
                self.stage['signed_artifacts'][required] = expected
                self.check()
        self.stage['ngx_module']['bridge_sha256'] = '0' * 64
        with self.assertRaises(SystemExit):
            self.check()
        self.stage['ngx_module']['bridge_sha256'] = metadata.SHARED_SHA256
        self.dll_path.write_bytes(b'rebased replacement')
        replacement_sha = hashlib.sha256(self.dll_path.read_bytes()).hexdigest()
        self.stage['signed_artifacts'][self.dll] = replacement_sha
        self.stage['ngx_module']['sha256'] = replacement_sha
        with self.assertRaises(SystemExit):
            self.check()

    def test_missing_file_and_wrong_symlink_with_identical_bytes_rejected(self):
        self.dll_path.unlink()
        with self.assertRaises(SystemExit):
            self.check()
        self.dll_path.write_bytes(b'original NGX module')
        self.check()
        alternate = self.bridge_path.with_name('alternate-bridge.dylib')
        alternate.write_bytes(self.bridge_path.read_bytes())
        self.link_path.unlink()
        self.link_path.symlink_to('../../external/alternate-bridge.dylib')
        self.assertEqual(self.link_path.read_bytes(), self.bridge_path.read_bytes())
        with self.assertRaises(SystemExit):
            self.check()
        self.link_path.unlink()
        self.link_path.symlink_to(metadata.NGX_UNIX_LINK)
        self.check()

    def test_repeated_invocation_rechecks_current_bytes(self):
        self.check()
        self.bridge_path.write_bytes(b'modified after first verification')
        with self.assertRaises(SystemExit):
            self.check()

    def test_legacy_fsr_runtime_rejects_ngx(self):
        self.stage['stage_schema'] = 4
        with self.assertRaises(SystemExit):
            self.check()


if __name__ == '__main__':
    unittest.main()
