#!/bin/sh
set -eu

[ "$#" -eq 1 ] || { echo "usage: scripts/build-fsr3-lifetime.sh <output-dir>" >&2; exit 64; }
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
out=$1
case "$out" in /*) ;; *) out="$PWD/$out" ;; esac
if [ -n "${WINE_MINGW_ROOT:-}" ]; then PATH="$WINE_MINGW_ROOT/bin:$PATH"; fi
clang=${WINE_MINGW_CLANG:-$(command -v x86_64-w64-mingw32-clang || true)}
[ -n "$clang" ] && [ -x "$clang" ] || { echo "select LLVM MinGW with WINE_MINGW_ROOT or WINE_MINGW_CLANG" >&2; exit 69; }
mingw=$(CDPATH= cd -- "$(dirname -- "$clang")" && pwd)
cxx="$mingw/x86_64-w64-mingw32-clang++"
readobj="$mingw/llvm-readobj"
[ -x "$cxx" ] && [ -x "$readobj" ] || { echo "LLVM MinGW C++ compiler/readobj missing" >&2; exit 69; }
sdk="$root/d3dmetal-sidecar/third-party/fidelityfx/Kits/FidelityFX"
build="$out/build"
windows="$out/lib/wine/x86_64-windows"
mkdir -p "$build" "$windows"
native_sha=$(python3 - "$root" <<'PY'
import importlib.util, pathlib, sys
root = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location('stage_runtime', root / 'scripts/stage-runtime.py')
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)
print(stage.ORIGINAL_FG_SHA256)
PY
)
"$cxx" -std=c++20 -O2 -g -Wall -Wextra -Werror -fms-extensions -shared -static \
  -DFFX_BACKEND_DX12=1 -DFFX_FRAMEGENERATION=1 -D_WINDOWS=1 \
  "-DYAAGL_FSR3_NATIVE_SHA256=\"$native_sha\"" \
  "$root/dlls/amd_fidelityfx_framegeneration_dx12/fsr3-lifetime.cpp" \
  "$sdk/api/internal/ffx_assert.cpp" \
  "$sdk/api/internal/ffx_object_management.cpp" \
  "$sdk/backend/dx12/ffx_backends_dx12.cpp" \
  "$sdk/backend/dx12/ffx_dx12.cpp" \
  "$sdk/framegeneration/fsr3/internal/ffx_provider_fsr3framegeneration.cpp" \
  "$sdk/framegeneration/fsr3/internal/ffx_opticalflow.cpp" \
  "$sdk/framegeneration/fsr3/internal/ffx_frameinterpolation.cpp" \
  -Wl,--exclude-all-symbols,--no-insert-timestamp \
  -ld3d12 -ldxgi -ldxguid -ld3dcompiler -lole32 -luuid -lbcrypt \
  -o "$build/yaagl_fsr3_lifetime.dll"
python3 - "$root" "$build/yaagl_fsr3_lifetime.dll" "$windows" "$cxx" "$readobj" "$out/build-manifest.json" <<'PY'
import hashlib, json, pathlib, re, shutil, subprocess, sys
root, source, destination = map(pathlib.Path, sys.argv[1:4])
cxx, readobj, manifest = sys.argv[4:]
inspection = subprocess.check_output([readobj, '--coff-exports', '--coff-imports', str(source)], text=True)
exports = [(int(n), name) for n, name in re.findall(r'Ordinal: (\d+)\s+Name: (\w+)', inspection)]
if 'Format: COFF-x86-64\n' not in inspection or exports != [(1, 'yaagl_fsr3_install')]:
    raise SystemExit('unexpected FSR3 lifetime architecture or exports')
imports = re.findall(r'Import \{\s+Name: ([^\n]+)', inspection)
if any(re.search(r'(libc\+\+|libstdc\+\+|libunwind|libgcc|winpthread)', name, re.I) for name in imports):
    raise SystemExit('private FSR3 lifetime DLL must not depend on an external C++ runtime')
target = destination / source.name
shutil.copy2(source, target)
sdk = root / 'd3dmetal-sidecar/third-party/fidelityfx/Kits/FidelityFX'
inputs = [root / 'dlls/amd_fidelityfx_framegeneration_dx12/fsr3-lifetime.cpp',
          root / 'include/yaagl_fsr3_lifetime.h', root / 'scripts/build-fsr3-lifetime.sh',
          root / 'scripts/stage-runtime.py']
inputs += sorted(path for path in sdk.rglob('*') if path.is_file() and path.suffix in ('.h', '.hpp', '.cpp'))
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
data = {'schemaVersion': 1, 'architecture': 'x86_64', 'cxxRuntime': 'static',
        'compiler': {'path': cxx, 'version': subprocess.check_output([cxx, '--version'], text=True).strip()},
        'sources': [{'path': str(path.relative_to(root)), 'sha256': sha(path)} for path in inputs],
        'artifact': {'path': str(target), 'sha256': sha(target), 'exports': exports, 'imports': imports}}
pathlib.Path(manifest).write_text(json.dumps(data, indent=2) + '\n')
print(target)
PY
