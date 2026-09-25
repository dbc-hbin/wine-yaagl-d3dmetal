#!/usr/bin/env python3
"""Exercise the real launch wrapper in temporary runtimes, without starting Wine."""

from pathlib import Path
import subprocess
import tempfile
import unittest

WRAPPER = Path(__file__).resolve().with_name("wine-launch-wrapper.sh")
MODULES = ("d3d10core.dll", "d3d11.dll", "dxgi.dll")
AMD = ["0x1002", "0x7550", "AMD Radeon RX 9070", "unset"]
NVIDIA = ["0x10de", "0x2d05", "NVIDIA GeForce RTX 5060", "unset"]
RECORDER = '''#!/bin/sh
printf '%s\\n' "$D3DM_VENDOR_ID" "$D3DM_DEVICE_ID" "$D3DM_DEVICE_DESCRIPTION" "${YAAGL_GPU_IDENTITY-unset}"
'''


class WineLaunchWrapperTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="wine-launch-wrapper-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.wrapper = self.bin / "wine"
        self.wrapper.write_bytes(WRAPPER.read_bytes())
        real = self.bin / "wine.real"
        real.write_text(RECORDER)
        real.chmod(0o755)
        self.support = self.root / "Application Support" / "Yaagl"
        self.prefix = self.support / "wine"
        self.prefix.mkdir(parents=True)
        self.modules = self.root / "lib/wine/x86_64-windows"
        self.modules.mkdir(parents=True)

    def launch(self, args, **environment):
        return subprocess.run(
            ["/bin/sh", str(self.wrapper), *args], capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", **environment}, timeout=30)

    def assert_identity(self, args, expected, **environment):
        result = self.launch(args, **environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), expected)

    def install_backups(self):
        for module in MODULES:
            (self.modules / module).write_bytes(b"launcher replacement")
            (self.modules / f"{module}.bak").write_bytes(b"packaged module")

    def test_direct_and_steam_launches_override_inherited_gpu(self):
        cases = (
            ([r"Z:\Games\ZZZ\ZenlessZoneZero.exe"], AMD, "rtx5060"),
            ([r"C:\windows\system32\steam.exe",
              r"Z:\Games\ZZZ\ZenlessZoneZero.exe"], AMD, "rtx5060"),
            ([r"Z:\Games\StarRail.exe"], NVIDIA, "rx9070"),
        )
        for args, expected, inherited in cases:
            with self.subTest(args=args):
                self.assert_identity(args, expected, YAAGL_GPU_IDENTITY=inherited,
                                     D3DM_VENDOR_ID="0xdead", D3DM_DEVICE_ID="0xbeef",
                                     D3DM_DEVICE_DESCRIPTION="Inherited GPU")

    def test_executable_token_boundaries(self):
        cases = (
            ('"Z:\\Games\\My Folder\\zenlesszonezero.EXE"', AMD),
            ("Z:/Games/My Folder/ZenlessZoneZero.exe", AMD),
            (r"Z:\Games\NotZenlessZoneZero.exe", NVIDIA),
            (r"Z:\Games\Other-ZenlessZoneZero.exe", NVIDIA),
            ('"Z:\\Games\\Other ZenlessZoneZero.exe"', NVIDIA),
            ('"Z:\\Games\\ZenlessZoneZero.exe copy"', NVIDIA),
            (r"Z:\Games\ZenlessZoneZero.exe.bak", NVIDIA),
            (r"Z:\Games\ZenlessZoneZero.exe\Other.exe", NVIDIA),
        )
        for argument, expected in cases:
            with self.subTest(argument=argument):
                self.assert_identity([argument], expected)

    def test_standard_batch_selects_gpu_from_game_command(self):
        cases = (
            ('"Z:\\Games\\ZZZ Folder\\ZENLESSZONEZERO.EXE" -screen-fullscreen 1\r\n', AMD),
            ('"Z:\\Games\\StarRail.exe"\r\n', NVIDIA),
            ('"Z:\\Games\\Other-ZenlessZoneZero.exe"\r\n', NVIDIA),
            ('"Z:\\Games\\Other ZenlessZoneZero.exe"\r\n', NVIDIA),
        )
        for command, expected in cases:
            with self.subTest(command=command):
                (self.support / "config.bat").write_bytes(command.encode())
                self.assert_identity(["cmd", "/c", '"Z:\\Application Support\\Yaagl\\CONFIG.BAT" '],
                                     expected, WINEPREFIX=str(self.prefix))

    def test_stale_batch_does_not_affect_unrelated_invocation(self):
        (self.support / "config.bat").write_text('"Z:\\Games\\ZenlessZoneZero.exe"\n')
        cases = (
            ["winecfg"],
            ["cmd", "/c", r"Z:\support\other-config.bat"],
            ["cmd", "/c", r"Z:\config.bat\other.bat"],
        )
        for args in cases:
            with self.subTest(args=args):
                self.assert_identity(args, NVIDIA, WINEPREFIX=str(self.prefix))
        self.assert_identity(["cmd", "/c", r"Z:\config.bat"], NVIDIA)

    def test_non_zzz_launch_restores_modules_and_preserves_backups(self):
        self.install_backups()
        (self.support / "config.bat").write_text('"Z:\\Games\\StarRail.exe"\n')
        self.assert_identity(["cmd", "/c", r"Z:\support\config.bat"], NVIDIA,
                             WINEPREFIX=str(self.prefix))
        for module in MODULES:
            with self.subTest(module=module):
                self.assertEqual((self.modules / module).read_bytes(), b"packaged module")
                self.assertEqual((self.modules / f"{module}.bak").read_bytes(), b"packaged module")

    def test_failed_restore_does_not_launch_wine(self):
        self.install_backups()
        target = self.modules / MODULES[0]
        target.unlink()
        # Copying a file onto itself fails even when the test is run as root.
        target.symlink_to(f"{MODULES[0]}.bak")
        result = self.launch([r"Z:\Games\StarRail.exe"])
        self.assertEqual(result.returncode, 124)
        self.assertEqual(result.stdout, "")
        self.assertEqual(target.read_bytes(), b"packaged module")


if __name__ == "__main__":
    unittest.main()
