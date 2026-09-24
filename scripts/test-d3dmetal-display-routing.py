#!/usr/bin/env python3
"""Exercise native D3DMetal DXGI routing and queue residency in isolated Wine."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "d3dmetal-pso-cache/display-routing.d3d12.test.cpp"
COMPILER = Path("/opt/llvm-mingw-20260616-ucrt-macos-universal/bin/x86_64-w64-mingw32-clang++")


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--compiler", default=COMPILER, type=Path)
    parser.add_argument("--case", choices=("baseline", "saved", "residency", "both"), default="both")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    runtime = args.runtime.resolve()
    wine = runtime / "bin/wine.real"
    d3dmetal = runtime / "lib/external/D3DMetal.framework/Versions/A/D3DMetal"
    shared = runtime / "lib/external/libd3dshared.dylib"
    bridge = runtime / "lib/wine/x86_64-unix/winemac.so"
    sidecar = runtime / "lib/external/D3DMetal.framework/Versions/A/Resources/libYaaglNativePsoCache.dylib"
    wineserver = runtime / "bin/wineserver"
    if not all(path.is_file() for path in (wine, d3dmetal, shared, bridge, sidecar, wineserver, args.compiler)):
        parser.error("runtime must provide Wine, D3DMetal, libd3dshared, winemac and sidecar plus x86_64 compiler")
    root = args.out.resolve() / f"run-{time.time_ns()}"
    root.mkdir(parents=True)
    executable = root / "display-routing.exe"
    build_log = root / "build.log"
    command = [str(args.compiler), "-std=c++20", "-O2", "-static", "-Wall", "-Wextra",
               "-Werror", str(SOURCE), "-ld3d12", "-ldxgi", "-lole32", "-luser32",
               "-o", str(executable)]
    with build_log.open("w") as stream:
        built = subprocess.run(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT,
                               timeout=args.timeout, check=False)
    evidence: dict[str, object] = {
        "source": str(SOURCE.relative_to(ROOT)),
        "source_sha256": digest(SOURCE),
        "compiler": str(args.compiler),
        "build_command": command,
        "build_exit": built.returncode,
        "build_log": str(build_log),
        "runtime": str(runtime),
        "d3dmetal_sha256": digest(d3dmetal),
        "libd3dshared_sha256": digest(shared),
        "winemac_sha256": digest(bridge),
        "sidecar_sha256": digest(sidecar),
        "cases": [],
    }
    if built.returncode:
        (root / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
        raise SystemExit(f"display routing test build failed: {build_log}")
    evidence["executable_sha256"] = digest(executable)

    prefix = root / "prefix"
    prefix.mkdir()
    environment = dict(os.environ)
    for key in list(environment):
        if key.startswith("YAAGL_") or key in {"WINEPREFIX", "WINEDLLOVERRIDES", "DYLD_INSERT_LIBRARIES",
                                               "D3DM_MAX_FPS", "DXMT_CONFIG"}:
            environment.pop(key, None)
    environment.update({
        "WINEPREFIX": str(prefix),
        "WINEARCH": "win64",
        "WINEDEBUG": "-all",
        "CX_ACTIVE_GRAPHICS_BACKEND": "d3dmetal",
        "CX_APPLEGPTK_LIBD3DSHARED_PATH": str(shared),
        "DYLD_FALLBACK_LIBRARY_PATH": str(runtime / "lib"),
        "D3DM_MTL4": "1",
        "D3DM_ENABLE_METALFX": "0",
        "D3DM_SUPPORT_DXR": "0",
        "WINE_ENABLE_TIMEOUT_FIX": "1",
        "WINEMSYNC": "1",
    })
    cases = ("baseline", "saved") if args.case == "both" else (args.case,)
    try:
        for name in cases:
            output = root / f"{name}.log"
            invocation = [str(wine), str(executable), name]
            started = time.monotonic()
            with output.open("w") as stream:
                try:
                    process = subprocess.run(invocation, cwd=root, env=environment, stdout=stream,
                                             stderr=subprocess.STDOUT, timeout=args.timeout, check=False)
                    exit_code: int | None = process.returncode
                except subprocess.TimeoutExpired:
                    exit_code = None
            text = output.read_text(errors="replace")
            marker = f"DISPLAY_ROUTING_{name.upper()}_PASS"
            result = {
                "case": name,
                "command": invocation,
                "exit": exit_code,
                "seconds": round(time.monotonic() - started, 3),
                "log": str(output),
                "saved_split": "DISPLAY_ROUTING_SAVED_SPLIT" in text,
                "two_outputs": "DISPLAY_ROUTING_MULTIMONITOR_PASS" in text,
                "two_outputs_skipped": "DISPLAY_ROUTING_MULTIMONITOR_SKIP" in text,
                "passed": exit_code == 0 and marker in text and
                          (name != "saved" or "DISPLAY_ROUTING_SAVED_SPLIT" in text),
                "result_lines": [line for line in text.splitlines() if line.startswith("DISPLAY_ROUTING_")],
            }
            evidence["cases"].append(result)
            (root / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
            print(f"{name}: {'PASS' if result['passed'] else 'FAIL'} ({output})", flush=True)
            if not result["passed"]:
                raise SystemExit(1)
        print("DISPLAY_ROUTING_D3D12_PASS", flush=True)
    finally:
        # The freshly created prefix belongs solely to this run; never stop other Wine sessions.
        cleanup = subprocess.run([str(wineserver), "-k"], env=environment, cwd=root,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 timeout=30, check=False)
        evidence["wineserver_cleanup_exit"] = cleanup.returncode
        (root / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")


if __name__ == "__main__":
    main()
