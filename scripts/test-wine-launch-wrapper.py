#!/usr/bin/env python3
"""Exercise the real launch wrapper in temporary runtimes, without starting Wine."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

WRAPPER = Path(__file__).resolve().with_name("wine-launch-wrapper.sh")
MODULES = ("d3d10core.dll", "d3d11.dll", "dxgi.dll")
AMD = ["0x1002", "0x7550", "AMD Radeon RX 9070", "unset"]
NVIDIA = ["0x10de", "0x2d05", "NVIDIA GeForce RTX 5060", "unset"]
RECORDER = '''#!/bin/sh
printf '%s\n' "$D3DM_VENDOR_ID" "$D3DM_DEVICE_ID" "$D3DM_DEVICE_DESCRIPTION" "${YAAGL_GPU_IDENTITY-unset}"
'''
POLICY_KEYS = ('WINEDLLOVERRIDES', 'MTL_CAPTURE_ENABLED', 'YAAGL_FSR_FG_NATIVE_DLL',
               'CX_APPLEGPTK_LIBD3DSHARED_PATH', 'DYLD_FALLBACK_LIBRARY_PATH',
               'GST_PLUGIN_SYSTEM_PATH_1_0', 'GST_PLUGIN_SCANNER', 'MTL_HUD_ENABLED',
               'WINE_ENABLE_TIMEOUT_FIX', 'D3DM_VENDOR_ID', 'D3DM_DEVICE_ID')
# A native recorder sees DYLD_* after Wine's shell wrapper executes it; protected
# shell/Python interpreters discard DYLD_* before their script bodies can inspect it.
POLICY_RECORDER = r'''
#include <stdio.h>
#include <stdlib.h>
int main(int argc, char **argv) {
    const char *keys[] = {POLICY_KEYS_PLACEHOLDER};
    printf("%d%c", argc - 1, 0);
    for (int i = 1; i < argc; ++i) printf("%s%c", argv[i], 0);
    for (size_t i = 0; i < sizeof(keys) / sizeof(keys[0]); ++i) {
        const char *value = getenv(keys[i]);
        printf("%s%c", value ? value : "__unset__", 0);
    }
    return 0;
}
'''.replace('POLICY_KEYS_PLACEHOLDER', ', '.join(f'"{key}"' for key in POLICY_KEYS))


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

    def record_policy(self, args, **environment):
        real = self.bin / "wine.real"
        subprocess.run(["clang", "-x", "c", "-o", str(real), "-"], input=POLICY_RECORDER,
                       text=True, capture_output=True, check=True)
        result = self.launch(args, **environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        fields = result.stdout.split("\0")
        count = int(fields[0])
        args = fields[1:1 + count]
        values = fields[1 + count:-1]
        self.assertEqual(len(values), len(POLICY_KEYS))
        return {"args": args, "env": {key: None if value == "__unset__" else value
                                     for key, value in zip(POLICY_KEYS, values)}}

    def install_backups(self):
        for module in MODULES:
            (self.modules / module).write_bytes(b"launcher replacement")
            (self.modules / f"{module}.bak").write_bytes(b"packaged module")

    def test_final_manifest_controls_runtime_policy_and_preserves_launch_contract(self):
        gst = self.root / "lib/GStreamer.framework/Versions/1.0"
        (gst / "lib/gstreamer-1.0").mkdir(parents=True)
        scanner = gst / "libexec/gstreamer-1.0/gst-plugin-scanner"
        scanner.parent.mkdir(parents=True)
        scanner.write_text("#!/bin/sh\n")
        scanner.chmod(0o755)
        args = [r"Z:\Games\ZenlessZoneZero.exe", "--quality", "high detail"]
        inherited = {"WINEDLLOVERRIDES": "old=n", "DYLD_FALLBACK_LIBRARY_PATH": "/inherited/lib",
                     "MTL_HUD_ENABLED": "0"}
        absent = self.record_policy(args, YAAGL_FSR_UPSCALER="native", **inherited)
        self.assertEqual(absent["args"], args)
        self.assertEqual(absent["env"]["D3DM_VENDOR_ID"], "0x1002")
        self.assertIsNone(absent["env"]["WINEDLLOVERRIDES"])
        self.assertIsNone(absent["env"]["YAAGL_FSR_FG_NATIVE_DLL"])
        self.assertIsNone(absent["env"]["CX_APPLEGPTK_LIBD3DSHARED_PATH"])
        self.assertIsNone(absent["env"]["DYLD_FALLBACK_LIBRARY_PATH"])
        self.assertEqual(absent["env"]["MTL_HUD_ENABLED"], "0")
        self.assertEqual(absent["env"]["WINE_ENABLE_TIMEOUT_FIX"], "1")

        # Even the old stage and P3 markers together cannot activate the final wrapper.
        (self.root / "zzz-frame-probe-stage.json").write_text("{}")
        (self.root / "yaagl-wine-p3-runtime.txt").write_text("baseline")
        old_stage = self.record_policy(args, YAAGL_FSR_UPSCALER="native")
        self.assertIsNone(old_stage["env"]["WINEDLLOVERRIDES"])
        self.assertIsNone(old_stage["env"]["CX_APPLEGPTK_LIBD3DSHARED_PATH"])
        (self.root / "zzz-frame-probe-stage.json").unlink()
        (self.root / "yaagl-wine-p3-runtime.txt").unlink()

        (self.root / "yaagl-d3dmetal-runtime.json").write_text(
            json.dumps({"schemaVersion": 1, "runtimeId": "wine-11.17-d3dmetal-gptk4.0b2-4"}))
        native = self.record_policy(args, YAAGL_FSR_UPSCALER="native", MTL_HUD_ENABLED="1",
                                    WINE_ENABLE_TIMEOUT_FIX="0", **{
                                        "DYLD_FALLBACK_LIBRARY_PATH": "/inherited/lib"})
        env = native["env"]
        self.assertEqual(native["args"], args)
        self.assertEqual(env["WINEDLLOVERRIDES"],
                         "amd_fidelityfx_upscaler_dx12=n;amd_fidelityfx_framegeneration_dx12=b")
        self.assertEqual(env["YAAGL_FSR_FG_NATIVE_DLL"],
                         f"Z:{self.root}/lib/wine/x86_64-windows/amd_fidelityfx_framegeneration_dx12_native.dll")
        self.assertEqual(env["MTL_CAPTURE_ENABLED"], "0")
        self.assertEqual(env["CX_APPLEGPTK_LIBD3DSHARED_PATH"],
                         str(self.root / "lib/external/libd3dshared.dylib"))
        self.assertEqual(env["DYLD_FALLBACK_LIBRARY_PATH"],
                         f"{gst}/lib:{self.root}/lib")
        self.assertEqual(env["GST_PLUGIN_SYSTEM_PATH_1_0"], str(gst / "lib/gstreamer-1.0"))
        self.assertEqual(env["GST_PLUGIN_SCANNER"], str(scanner))
        self.assertEqual(env["MTL_HUD_ENABLED"], "1")
        self.assertEqual(env["WINE_ENABLE_TIMEOUT_FIX"], "0")
        default = self.record_policy(["winecfg"])
        self.assertEqual(default["env"]["WINEDLLOVERRIDES"],
                         "amd_fidelityfx_upscaler_dx12,amd_fidelityfx_framegeneration_dx12=b")
        self.assertEqual(default["env"]["D3DM_VENDOR_ID"], "0x10de")
        self.assertEqual(default["env"]["DYLD_FALLBACK_LIBRARY_PATH"],
                         f"{gst}/lib:{self.root}/lib")
        self.assertEqual(default["env"]["WINE_ENABLE_TIMEOUT_FIX"], "1")
        self.assertIsNone(default["env"]["MTL_HUD_ENABLED"])
        metalfx = self.record_policy(args, YAAGL_FSR_UPSCALER="metalfx",
                                     MTL_HUD_ENABLED="0", WINE_ENABLE_TIMEOUT_FIX="1")
        self.assertEqual(metalfx["env"]["WINEDLLOVERRIDES"],
                         default["env"]["WINEDLLOVERRIDES"])
        self.assertEqual(metalfx["env"]["MTL_HUD_ENABLED"], "0")
        self.assertEqual(metalfx["env"]["WINE_ENABLE_TIMEOUT_FIX"], "1")
        invalid = self.launch(args, YAAGL_FSR_UPSCALER="invalid")
        self.assertEqual(invalid.returncode, 64)
        self.assertEqual(invalid.stdout, "")
        self.assertIn("YAAGL_FSR_UPSCALER must be metalfx or native", invalid.stderr)

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
