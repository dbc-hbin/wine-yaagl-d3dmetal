#!/bin/sh
# Package the source-built host and verified local GPTK overlay.
#
#   package-wine-p3-runtime.sh HOST_PREFIX GPTK_SOURCE PROVENANCE_JSON OUTPUT_DIR
#
# PROVENANCE_JSON (BuildDriver):
#   {
#     "name": "wine-11.17-git.<sha>-zzz-dx12-gptk4b2",
#     "wineVersion": "11.17",
#     "upstreamCommit": "<full sha>",
#     "patches": [{"file":"...","sha256":"..."}],
#     "configureArgs": ["..."],
#     "builtAt": "<ISO-8601>"
#   }
#
# Archive: <OUTPUT_DIR>/<provenance.name>.tar.xz (+ .sha256).
# Never mutates inputs. Never copies old host modules from GPTK/source.
# Host-linked deps come from MacDeps/MediaDeps trees only.
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

GPTK_D3D10CORE_SHA256=dc87193d17e1b48bd40acc295ee180031089740c87e2d4ddc36325773a3c2e27
GPTK_D3D11_SHA256=303b2bb41efa30c890e2e93d39c3d3c565c8557e069eee832f2cb8a37bd4ec26
GPTK_DXGI_SHA256=522a8b37216afb09e614489d88a74118076f4d7e08d2b289df6a6eb6f3e817af
GPTK_D3D12_SHA256=1b7a02cb37ec6b484e2aaa76b5ec9cbb47e63aeec29dbe087d5d1589a3347cfb
# Complete patched converter recorded by the recovered GPTK 4.0b2 package
# manifest, not merely the four modified instruction ranges.
GPTK_FP64_PATCHED_CONVERTER_SHA256=5c5619ef17a7d62e84db0a7f5181d746623b47364379271fd5827e6bd961ba34

OLD_HOST_WINESERVER_SHA256=66fbeff4f723ec9d807c418deae2159288044aa20dfbba2212eeadb8e4c89bba
OLD_HOST_NTDLL_SHA256=a56cc97e1cf9c6f861a8a6b1e1de84bd8265ced94189b7931f89b6c1fb6c95bb

P3_WINE_VERSION=11.17
P3_UPSTREAM_COMMIT=913e31f201d344223bdf3d13a50a41af35893d12

# Closed identity contract for tuned overlay provenance and package verification.
PSO_CACHE_MODULE_REL=lib/external/D3DMetal.framework/Versions/A/Resources/libYaaglNativePsoCache.dylib
D3DMETAL_BINARY_REL=lib/external/D3DMetal.framework/Versions/A/D3DMetal
METALIR_CONVERTER_REL=lib/external/D3DMetal.framework/Versions/A/Resources/libmetalirconverter.dylib
PSO_CACHE_DEPENDENCY=@loader_path/Resources/libYaaglNativePsoCache.dylib

TUNED_ARTIFACT_IDENTITIES='lib/wine/x86_64-unix/ntdll.so|x86_64|macho
lib/wine/x86_64-unix/winemac.so|x86_64|macho
lib/wine/x86_64-windows/winemac.drv|x86_64|pe
lib/wine/i386-windows/winemac.drv|i386|pe
bin/wineserver|arm64|macho
lib/wine/x86_64-unix/win32u.so|x86_64|macho
lib/wine/x86_64-windows/win32u.dll|x86_64|pe
lib/wine/i386-windows/win32u.dll|i386|pe
lib/wine/x86_64-unix/winegstreamer.so|x86_64|macho
lib/wine/x86_64-windows/winegstreamer.dll|x86_64|pe
lib/wine/i386-windows/winegstreamer.dll|i386|pe
lib/wine/x86_64-windows/resampledmo.dll|x86_64|pe
lib/wine/i386-windows/resampledmo.dll|i386|pe
lib/wine/x86_64-windows/mfreadwrite.dll|x86_64|pe
lib/wine/i386-windows/mfreadwrite.dll|i386|pe
lib/wine/x86_64-windows/amstream.dll|x86_64|pe
lib/wine/i386-windows/amstream.dll|i386|pe
lib/wine/x86_64-windows/imm32.dll|x86_64|pe
lib/wine/i386-windows/imm32.dll|i386|pe
lib/wine/x86_64-unix/winecoreaudio.so|x86_64|macho
lib/wine/x86_64-unix/winebus.so|x86_64|macho
lib/wine/x86_64-windows/winebus.sys|x86_64|pe
lib/wine/i386-windows/winebus.sys|i386|pe
lib/wine/x86_64-windows/hidclass.sys|x86_64|pe
lib/wine/i386-windows/hidclass.sys|i386|pe
lib/wine/x86_64-windows/dinput.dll|x86_64|pe
lib/wine/i386-windows/dinput.dll|i386|pe
lib/wine/x86_64-windows/dinput8.dll|x86_64|pe
lib/wine/i386-windows/dinput8.dll|i386|pe
lib/wine/x86_64-windows/xinput1_1.dll|x86_64|pe
lib/wine/i386-windows/xinput1_1.dll|i386|pe
lib/wine/x86_64-windows/xinput1_2.dll|x86_64|pe
lib/wine/i386-windows/xinput1_2.dll|i386|pe
lib/wine/x86_64-windows/xinput1_3.dll|x86_64|pe
lib/wine/i386-windows/xinput1_3.dll|i386|pe
lib/wine/x86_64-windows/xinput1_4.dll|x86_64|pe
lib/wine/i386-windows/xinput1_4.dll|i386|pe
lib/wine/x86_64-windows/xinputuap.dll|x86_64|pe
lib/wine/i386-windows/xinputuap.dll|i386|pe
lib/wine/x86_64-windows/wininet.dll|x86_64|pe
lib/wine/i386-windows/wininet.dll|i386|pe
lib/wine/x86_64-windows/winhttp.dll|x86_64|pe
lib/wine/i386-windows/winhttp.dll|i386|pe
lib/wine/x86_64-windows/ws2_32.dll|x86_64|pe
lib/wine/i386-windows/ws2_32.dll|i386|pe'

baseline_root=${WINE_P3_ROOT:-"$repo_dir/build/wine-p3"}
macports_deps_root="$baseline_root/deps/macports"
gstreamer_deps_root="$baseline_root/deps/gstreamer"
default_output_dir="$repo_dir/build/wine-p3/package"

usage() {
  cat <<'EOF' >&2
usage: package-wine-p3-runtime.sh HOST_PREFIX GPTK_SOURCE PROVENANCE_JSON OUTPUT_DIR

Positional arguments only:
  HOST_PREFIX      freshly installed upstream Wine prefix
  GPTK_SOURCE      verified patched GPTK wine/ or lib/ root
  PROVENANCE_JSON  driver provenance JSON (name, wineVersion, ...)
  OUTPUT_DIR       package output dir (contracts default: build/wine-p3/package)

Environment:
  WINE_P3_ROOT           prepared baseline dependencies (default: build/wine-p3)
  YAAGL_STEAM_HELPER_DIR  helper payload source (default: sidecar/protonextras)

Archive: <OUTPUT_DIR>/<provenance.name>.tar.xz
EOF
}

die() {
  echo "$*" >&2
  exit 1
}

require_file() {
  [ -f "$1" ] || die "missing file: $1"
}

require_exec() {
  [ -x "$1" ] || die "missing executable: $1"
}

sha256_file() {
  shasum -a 256 "$1" | awk '{print $1}'
}

verify_hash() {
  target=$1
  expected=$2
  actual=$(sha256_file "$target")
  if [ "$actual" != "$expected" ]; then
    echo "hash mismatch: $target: expected $expected, got $actual" >&2
    exit 5
  fi
}

write_packaged_artifacts() {
  runtime_root=$1
  manifest=$runtime_root/yaagl-wine-p3-provenance.json
  tmp=$manifest.tmp.$$
  TUNED_ARTIFACT_IDENTITIES_VALUE=$TUNED_ARTIFACT_IDENTITIES \
  /usr/bin/python3 - "$manifest" "$runtime_root" "$tmp" <<'PY'
import hashlib, json, os, pathlib, sys
manifest_path, runtime_root, output_path = sys.argv[1:]
root = pathlib.Path(runtime_root)
with open(manifest_path, encoding="utf-8") as stream:
    payload = json.load(stream)
artifacts = []
for line in os.environ["TUNED_ARTIFACT_IDENTITIES_VALUE"].splitlines():
    path, architecture, artifact_format = line.split("|", 2)
    digest = hashlib.sha256((root / path).read_bytes()).hexdigest()
    artifacts.append({"path": path, "architecture": architecture, "format": artifact_format,
                      "sha256": digest})
payload["artifactHashSemantics"] = {
    "rebuiltArtifacts": "SHA-256 of host/build bytes before package rewriting and signing",
    "packagedArtifacts": "SHA-256 of final packaged bytes after all package rewriting and signing",
}
payload["packagedArtifacts"] = artifacts
with open(output_path, "w", encoding="utf-8") as stream:
    json.dump(payload, stream, indent=2)
    stream.write("\n")
PY
  /bin/mv "$tmp" "$manifest"
}

verify_packaged_artifacts() {
  runtime_root=$1
  manifest=$runtime_root/yaagl-wine-p3-provenance.json
  TUNED_ARTIFACT_IDENTITIES_VALUE=$TUNED_ARTIFACT_IDENTITIES \
  /usr/bin/python3 - "$manifest" "$runtime_root" <<'PY'
import hashlib, json, os, pathlib, sys
manifest_path, runtime_root = sys.argv[1:]
root = pathlib.Path(runtime_root)
with open(manifest_path, encoding="utf-8") as stream:
    payload = json.load(stream)
expected = {
    path: (architecture, artifact_format)
    for path, architecture, artifact_format in
    (line.split("|", 2) for line in os.environ["TUNED_ARTIFACT_IDENTITIES_VALUE"].splitlines())
}
items = payload.get("packagedArtifacts")
if not isinstance(items, list):
    raise SystemExit("provenance.packagedArtifacts must be an array")
by_path = {}
for item in items:
    if not isinstance(item, dict) or not isinstance(item.get("path"), str):
        raise SystemExit("provenance.packagedArtifacts contains an invalid entry")
    path = item["path"]
    if path in by_path:
        raise SystemExit(f"duplicate packaged artifact: {path}")
    by_path[path] = item
if set(by_path) != set(expected):
    missing = sorted(set(expected) - set(by_path))
    extra = sorted(set(by_path) - set(expected))
    raise SystemExit(f"packaged artifact inventory mismatch; missing={missing}, extra={extra}")
for path, (architecture, artifact_format) in expected.items():
    item = by_path[path]
    if item.get("architecture") != architecture or item.get("format") != artifact_format:
        raise SystemExit(f"packaged artifact identity mismatch: {path}")
    digest = hashlib.sha256((root / path).read_bytes()).hexdigest()
    if item.get("sha256") != digest:
        raise SystemExit(f"packaged artifact hash mismatch: {path}")
semantics = payload.get("artifactHashSemantics")
if not isinstance(semantics, dict) or "before package" not in semantics.get("rebuiltArtifacts", "") \
        or "final packaged bytes" not in semantics.get("packagedArtifacts", ""):
    raise SystemExit("provenance artifact hash semantics are missing or stale")
PY
}

write_graphics_artifacts() {
  runtime_root=$1
  manifest=$runtime_root/yaagl-wine-p3-graphics-artifacts.json
  D3DMETAL_SOURCE_SHA256_VALUE=$d3dmetal_source_sha \
  D3DMETAL_PRE_SIGN_SHA256_VALUE=$d3dmetal_pre_sign_sha \
  PSO_MODULE_SOURCE_SHA256_VALUE=$pso_module_source_sha \
  PSO_MODULE_PRE_SIGN_SHA256_VALUE=$pso_module_pre_sign_sha \
  CONVERTER_SOURCE_SHA256_VALUE=$converter_sha \
  PSO_SYSTEM_DEPENDENCIES_VALUE=$pso_system_dependencies \
  /usr/bin/python3 - "$runtime_root" "$manifest" "$pso_build_manifest" <<'PY'
import hashlib, json, os, pathlib, sys
root, manifest_path, build_manifest_path = map(pathlib.Path, sys.argv[1:])
with build_manifest_path.open(encoding="utf-8") as stream:
    native_build = json.load(stream)
paths = {
    "framework": "lib/external/D3DMetal.framework/Versions/A/D3DMetal",
    "module": "lib/external/D3DMetal.framework/Versions/A/Resources/libYaaglNativePsoCache.dylib",
    "converter": "lib/external/D3DMetal.framework/Versions/A/Resources/libmetalirconverter.dylib",
}
artifacts = {
    "framework": {
        "path": paths["framework"], "architecture": "x86_64", "format": "macho",
        "sourceSha256": os.environ["D3DMETAL_SOURCE_SHA256_VALUE"],
        "preSignSha256": os.environ["D3DMETAL_PRE_SIGN_SHA256_VALUE"],
    },
    "module": {
        "path": paths["module"], "architecture": "x86_64", "format": "macho",
        "sourceFingerprintSha256": os.environ["PSO_MODULE_SOURCE_SHA256_VALUE"],
        "preSignSha256": os.environ["PSO_MODULE_PRE_SIGN_SHA256_VALUE"],
        "systemDependencies": os.environ["PSO_SYSTEM_DEPENDENCIES_VALUE"].splitlines(),
    },
    "converter": {
        "path": paths["converter"], "architecture": "x86_64", "format": "macho",
        "sourceSha256": os.environ["CONVERTER_SOURCE_SHA256_VALUE"],
    },
}
for item in artifacts.values():
    item["sha256"] = hashlib.sha256((root / item["path"]).read_bytes()).hexdigest()
    item["signature"] = "adhoc"
payload = {
    "schemaVersion": 3,
    "cache": {"scope": "per-native-device", "retention": "device-lifetime"},
    "functionCache": {"scope": "per-native-device", "retention": "device-lifetime"},
    "frameworkDependency": "@loader_path/Resources/libYaaglNativePsoCache.dylib",
    "nativePsoCacheBuild": native_build,
    "artifacts": artifacts,
}
with manifest_path.open("w", encoding="utf-8") as stream:
    json.dump(payload, stream, indent=2, sort_keys=True)
    stream.write("\n")
PY
}

verify_graphics_artifacts() {
  runtime_root=$1
  manifest=$runtime_root/yaagl-wine-p3-graphics-artifacts.json
  require_file "$manifest"
  /usr/bin/python3 - "$manifest" "$runtime_root" <<'PY'
import hashlib, json, pathlib, sys
manifest_path, runtime_root = sys.argv[1:]
root = pathlib.Path(runtime_root)
with open(manifest_path, encoding="utf-8") as stream:
    payload = json.load(stream)
expected = {
    "framework": "lib/external/D3DMetal.framework/Versions/A/D3DMetal",
    "module": "lib/external/D3DMetal.framework/Versions/A/Resources/libYaaglNativePsoCache.dylib",
    "converter": "lib/external/D3DMetal.framework/Versions/A/Resources/libmetalirconverter.dylib",
}
if payload.get("schemaVersion") != 3:
    raise SystemExit("graphics artifact manifest schema mismatch")
cache_contract = {"scope": "per-native-device", "retention": "device-lifetime"}
if payload.get("cache") != cache_contract:
    raise SystemExit("graphics cache contract mismatch")
if payload.get("functionCache") != cache_contract:
    raise SystemExit("graphics function cache contract mismatch")
if payload.get("frameworkDependency") != "@loader_path/Resources/libYaaglNativePsoCache.dylib":
    raise SystemExit("graphics framework dependency mismatch")
artifacts = payload.get("artifacts")
if not isinstance(artifacts, dict) or set(artifacts) != set(expected):
    raise SystemExit("graphics artifact inventory mismatch")
native_build = payload.get("nativePsoCacheBuild")
if not isinstance(native_build, dict) or native_build.get("schemaVersion") != 1 \
        or native_build.get("architecture") != "x86_64" \
        or native_build.get("deploymentTarget") != "14.0" \
        or not isinstance(native_build.get("sources"), list) or not native_build["sources"]:
    raise SystemExit("graphics native PSO cache build provenance is missing or invalid")
module_build = native_build.get("module")
if not isinstance(module_build, dict) \
        or module_build.get("file") != "libYaaglNativePsoCache.dylib" \
        or module_build.get("sha256") != artifacts["module"].get("preSignSha256"):
    raise SystemExit("graphics native PSO cache build module identity mismatch")
source_fingerprint = hashlib.sha256()
seen_sources = set()
for source in native_build["sources"]:
    if not isinstance(source, dict) or not isinstance(source.get("path"), str) \
            or not isinstance(source.get("sha256"), str) or len(source["sha256"]) != 64 \
            or source["path"] in seen_sources:
        raise SystemExit("graphics native PSO cache source provenance is invalid")
    seen_sources.add(source["path"])
    source_fingerprint.update(source["path"].encode("utf-8") + b"\0" + source["sha256"].encode("ascii") + b"\n")
if source_fingerprint.hexdigest() != artifacts["module"].get("sourceFingerprintSha256"):
    raise SystemExit("graphics native PSO cache source fingerprint mismatch")
module_dependencies = artifacts["module"].get("systemDependencies")
if not isinstance(module_dependencies, list) or not module_dependencies \
        or any(not isinstance(dep, str) or not (dep.startswith("/System/") or dep.startswith("/usr/lib/"))
               for dep in module_dependencies):
    raise SystemExit("graphics native PSO cache system dependencies are invalid")
for name, path in expected.items():
    item = artifacts[name]
    if not isinstance(item, dict) or item.get("path") != path \
            or item.get("architecture") != "x86_64" or item.get("format") != "macho" \
            or item.get("signature") != "adhoc":
        raise SystemExit(f"graphics artifact identity mismatch: {name}")
    digest = hashlib.sha256((root / path).read_bytes()).hexdigest()
    if item.get("sha256") != digest:
        raise SystemExit(f"graphics artifact hash mismatch: {name}")
    source_field = "sourceFingerprintSha256" if name == "module" else "sourceSha256"
    for field in (source_field, "preSignSha256") if name != "converter" else (source_field,):
        value = item.get(field)
        if not isinstance(value, str) or len(value) != 64:
            raise SystemExit(f"graphics artifact {field} missing: {name}")
PY
}

validate_pe_machine() {
  target=$1
  expected=$2
  python3 - "$target" "$expected" <<'PY' || die "invalid PE machine: $target (expected $expected)"
import pathlib
import struct
import sys

path = pathlib.Path(sys.argv[1])
expected = int(sys.argv[2], 0)
data = path.read_bytes()
if len(data) < 0x40 or data[:2] != b"MZ":
    raise SystemExit(1)
pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
if pe_offset + 6 > len(data) or data[pe_offset:pe_offset + 4] != b"PE\0\0":
    raise SystemExit(1)
machine = struct.unpack_from("<H", data, pe_offset + 4)[0]
if machine != expected:
    raise SystemExit(1)
PY
}

is_system_dep() {
  case "$1" in
    /System/*|/usr/lib/*|/usr/lib/system/*)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

macho_kind() {
  file -b "$1" 2>/dev/null | grep -Eq 'Mach-O'
}

# The tuned runtime has one deliberate exception to the x86_64 host layout:
# bin/wineserver may be a thin arm64 executable. All other Mach-Os must retain
# an x86_64 slice (GStreamer/framework binaries may additionally be universal).
macho_scan_arch() {
  target=$1
  if [ "$target" = "$host_prefix/bin/wineserver" ] \
    || { [ -n "${stage:-}" ] && [ "$target" = "$stage/wine/bin/wineserver" ]; }; then
    printf '%s\n' "$wineserver_arch"
  else
    printf '%s\n' x86_64
  fi
}

validate_macho_arch() {
  target=$1
  arch=$(macho_scan_arch "$target")
  /usr/bin/lipo -verify_arch "$arch" "$target" >/dev/null 2>&1 \
    || die "Mach-O lacks required $arch slice: $target"
  if [ "$arch" = arm64 ] && /usr/bin/lipo -verify_arch x86_64 "$target" >/dev/null 2>&1; then
    die "tuned wineserver must be thin arm64, not universal: $target"
  fi
}

# Accept only tab-indented install-name lines so otool's architecture headers
# are never treated as dependencies.
macho_deps() {
  target=$1
  arch=$(macho_scan_arch "$target")
  validate_macho_arch "$target"
  otool -arch "$arch" -L "$target" 2>/dev/null | awk '
    /^\t/ {
      dep = $1
      if (dep ~ /^(@|\/)/) print dep
    }
  '
}

verify_system_only_dependencies() {
  target=$1
  arch=$(macho_scan_arch "$target")
  dependencies=$(otool -arch "$arch" -l "$target" 2>/dev/null | awk '
    $1 == "cmd" { load = ($2 == "LC_LOAD_DYLIB"); next }
    load && $1 == "name" { print $2; load = 0 }
  ')
  [ -n "$dependencies" ] || die "native PSO cache has no recorded system dependencies"
  printf '%s\n' "$dependencies" | while IFS= read -r dep; do
    is_system_dep "$dep" || die "native PSO cache has non-system dependency: $dep"
  done
}

json_get() {
  file=$1
  path=$2
  python3 - "$file" "$path" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
cur = data
for part in sys.argv[2].split("."):
    if not part:
        continue
    if isinstance(cur, dict) and part in cur:
        cur = cur[part]
    else:
        print("")
        raise SystemExit(0)
if cur is None:
    print("")
elif isinstance(cur, (dict, list)):
    print(json.dumps(cur, separators=(",", ":")))
else:
    print(cur)
PY
}

resolve_gptk_lib_root() {
  root=$1
  if [ -d "$root/lib/external/D3DMetal.framework" ]; then
    printf '%s\n' "$root/lib"
    return 0
  fi
  if [ -d "$root/external/D3DMetal.framework" ]; then
    printf '%s\n' "$root"
    return 0
  fi
  if [ -d "$root/redist/lib/external/D3DMetal.framework" ]; then
    printf '%s\n' "$root/redist/lib"
    return 0
  fi
  die "unable to locate GPTK lib root under $root"
}

find_macports_lib() {
  name=$1
  # Prefer assembled ready/opt trees, then newest staging package copies.
  for base in \
    "$macports_deps_root/ready/lib" \
    "$macports_deps_root/opt/local/lib" \
    "$macports_deps_root/lib"
  do
    if [ -f "$base/$name" ]; then
      printf '%s\n' "$base/$name"
      return 0
    fi
  done
  # staging/<port>/opt/local/lib/<name> — prefer versioned port dirs (sort -V).
  hit=$(find "$macports_deps_root/staging" -path '*/opt/local/lib/'"$name" -type f 2>/dev/null | sort -V | tail -n 1 || true)
  if [ -n "$hit" ]; then
    printf '%s\n' "$hit"
    return 0
  fi
  return 1
}

find_gstreamer_framework() {
  for cand in \
    "$gstreamer_deps_root/ready/lib/GStreamer.framework" \
    "$gstreamer_deps_root/sdk/GStreamer.framework" \
    "$gstreamer_deps_root/GStreamer.framework" \
    "$gstreamer_deps_root/expand/GStreamer.framework"
  do
    if [ -d "$cand" ]; then
      printf '%s\n' "$cand"
      return 0
    fi
  done
  return 1
}

# OUTPUT_DIR may be omitted to use the default path; no flag aliases are accepted.
for arg in "$@"; do
  case "$arg" in
    -*)
      echo "flag arguments are not accepted; use the positional CLI" >&2
      usage
      exit 2
      ;;
  esac
done

if [ "$#" -eq 3 ]; then
  host_prefix=$1
  gptk_runtime=$2
  provenance_json=$3
  output_dir=$default_output_dir
elif [ "$#" -eq 4 ]; then
  host_prefix=$1
  gptk_runtime=$2
  provenance_json=$3
  output_dir=$4
else
  usage
  exit 2
fi

require_file "$provenance_json"
host_prefix=$(CDPATH= cd -- "$host_prefix" && pwd)
gptk_runtime=$(CDPATH= cd -- "$gptk_runtime" && pwd)
provenance_json=$(CDPATH= cd -- "$(dirname -- "$provenance_json")" && pwd)/$(basename -- "$provenance_json")

# Resolve and validate the complete launcher-helper payload before creating or
# changing anything under OUTPUT_DIR. An explicit source must use the same
# four-file layout as the repository's sidecar/protonextras directory.
steam_helper_dir=${YAAGL_STEAM_HELPER_DIR:-"$repo_dir/sidecar/protonextras"}
[ -d "$steam_helper_dir" ] || die "missing Steam helper directory: $steam_helper_dir"
steam_helper_dir=$(CDPATH= cd -- "$steam_helper_dir" && pwd)
for helper_name in steam64.exe steam32.exe lsteamclient64.dll lsteamclient32.dll; do
  require_file "$steam_helper_dir/$helper_name"
done
validate_pe_machine "$steam_helper_dir/steam64.exe" 0x8664
validate_pe_machine "$steam_helper_dir/steam32.exe" 0x014c
validate_pe_machine "$steam_helper_dir/lsteamclient64.dll" 0x8664
validate_pe_machine "$steam_helper_dir/lsteamclient32.dll" 0x014c

mkdir -p "$(dirname -- "$output_dir")"
output_dir=$(CDPATH= cd -- "$(dirname -- "$output_dir")" && pwd)/$(basename -- "$output_dir")

archive_name=$(json_get "$provenance_json" name)
wine_version_field=$(json_get "$provenance_json" wineVersion)
runtime_id=$(json_get "$provenance_json" runtimeId)
upstream_commit=$(json_get "$provenance_json" upstreamCommit)
built_at=$(json_get "$provenance_json" builtAt)
patches_json=$(json_get "$provenance_json" patches)
configure_args_json=$(json_get "$provenance_json" configureArgs)
schema_version=$(json_get "$provenance_json" schemaVersion)
build_kind=$(json_get "$provenance_json" buildKind)

[ -n "$archive_name" ] || die "provenance.name is required"
case "$archive_name" in
  *[!a-zA-Z0-9._-]*|.*) die "provenance.name must be a plain runtime filename" ;;
esac
[ -n "$wine_version_field" ] || die "provenance.wineVersion is required"
[ -n "$runtime_id" ] || die "provenance.runtimeId is required"
case "$runtime_id" in
  *[!a-zA-Z0-9._-]*|.*) die "provenance.runtimeId must be a catalog-safe runtime ID" ;;
esac
[ -n "$upstream_commit" ] || die "provenance.upstreamCommit is required"
[ "$wine_version_field" = "$P3_WINE_VERSION" ] \
  || die "provenance.wineVersion must be the pinned P3 version $P3_WINE_VERSION"
[ "$upstream_commit" = "$P3_UPSTREAM_COMMIT" ] \
  || die "provenance.upstreamCommit must be the pinned P3 source $P3_UPSTREAM_COMMIT"

require_exec "$host_prefix/bin/wine"
require_exec "$host_prefix/bin/wineserver"
require_file "$host_prefix/lib/wine/x86_64-unix/ntdll.so"
require_file "$host_prefix/lib/wine/x86_64-unix/winemac.so"

if /usr/bin/lipo -verify_arch arm64 "$host_prefix/bin/wineserver" >/dev/null 2>&1; then
  if /usr/bin/lipo -verify_arch x86_64 "$host_prefix/bin/wineserver" >/dev/null 2>&1; then
    die "host wineserver must be thin x86_64 (P3) or thin arm64 (tuned), not universal"
  fi
  wineserver_arch=arm64
elif /usr/bin/lipo -verify_arch x86_64 "$host_prefix/bin/wineserver" >/dev/null 2>&1; then
  wineserver_arch=x86_64
else
  die "host wineserver is neither arm64 nor x86_64"
fi
if [ "$wineserver_arch" = arm64 ]; then
  [ "$schema_version" = 2 ] || die "tuned ARM64 provenance.schemaVersion must be 2"
  [ "$build_kind" = incremental-module-overlay ] \
    || die "tuned ARM64 provenance.buildKind must be incremental-module-overlay"
fi
if [ "$build_kind" = incremental-module-overlay ]; then
  printf '%s\n' "$TUNED_ARTIFACT_IDENTITIES" | while IFS='|' read -r rel arch format; do
    require_file "$host_prefix/$rel"
  done
fi
validate_macho_arch "$host_prefix/bin/wineserver"
validate_macho_arch "$host_prefix/lib/wine/x86_64-unix/ntdll.so"
validate_macho_arch "$host_prefix/lib/wine/x86_64-unix/winemac.so"
if [ "$build_kind" = incremental-module-overlay ]; then
  printf '%s\n' "$TUNED_ARTIFACT_IDENTITIES" | while IFS='|' read -r rel arch format; do
    case "$format:$arch" in
      macho:*) validate_macho_arch "$host_prefix/$rel" ;;
      pe:x86_64) validate_pe_machine "$host_prefix/$rel" 0x8664 ;;
      pe:i386) validate_pe_machine "$host_prefix/$rel" 0x014c ;;
      *) die "unsupported tuned artifact identity: $format/$arch for $rel" ;;
    esac
  done
fi

host_wineserver_sha=$(sha256_file "$host_prefix/bin/wineserver")
host_ntdll_sha=$(sha256_file "$host_prefix/lib/wine/x86_64-unix/ntdll.so")
host_macdrv_sha=$(sha256_file "$host_prefix/lib/wine/x86_64-unix/winemac.so")
TUNED_ARTIFACT_IDENTITIES_VALUE=$TUNED_ARTIFACT_IDENTITIES \
/usr/bin/python3 - "$provenance_json" "$host_prefix" "$wineserver_arch" <<'PY'
import hashlib
import json
import os
import pathlib
import sys

provenance_path, host_root, wineserver_arch = sys.argv[1:]
with open(provenance_path, encoding="utf-8") as stream:
    provenance = json.load(stream)
if wineserver_arch == "arm64":
    if provenance.get("schemaVersion") != 2:
        raise SystemExit("tuned ARM64 provenance.schemaVersion must be 2")
    if provenance.get("buildKind") != "incremental-module-overlay":
        raise SystemExit("tuned ARM64 provenance.buildKind must be incremental-module-overlay")
artifacts = provenance.get("rebuiltArtifacts")
if provenance.get("buildKind") == "incremental-module-overlay" and artifacts is None:
    raise SystemExit("tuned provenance.rebuiltArtifacts is required")
if artifacts is not None:
    if not isinstance(artifacts, list):
        raise SystemExit("provenance.rebuiltArtifacts must be an array")
    if provenance.get("buildKind") == "incremental-module-overlay":
        expected = {
            line.split("|", 2)[0]: tuple(line.split("|", 2)[1:])
            for line in os.environ["TUNED_ARTIFACT_IDENTITIES_VALUE"].splitlines()
        }
    else:
        expected = {
            "bin/wineserver": (wineserver_arch, "macho"),
            "lib/wine/x86_64-unix/ntdll.so": ("x86_64", "macho"),
        }
    by_path = {}
    for item in artifacts:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise SystemExit("provenance.rebuiltArtifacts contains an invalid entry")
        path = item["path"]
        if path in by_path:
            raise SystemExit(f"duplicate provenance artifact: {path}")
        relative = pathlib.PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise SystemExit(f"invalid provenance artifact path: {path}")
        by_path[path] = item
        artifact = pathlib.Path(host_root, *relative.parts)
        try:
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        except OSError as error:
            raise SystemExit(f"unable to hash provenance artifact {artifact}: {error}")
        if item.get("sha256") != digest:
            raise SystemExit(f"host artifact does not match provenance: {path}")
    if provenance.get("buildKind") == "incremental-module-overlay" and set(by_path) != set(expected):
        missing = sorted(set(expected) - set(by_path))
        extra = sorted(set(by_path) - set(expected))
        raise SystemExit(f"tuned provenance artifact inventory mismatch; missing={missing}, extra={extra}")
    for path, (architecture, artifact_format) in expected.items():
        item = by_path.get(path)
        if item is None:
            raise SystemExit(f"provenance missing rebuilt artifact: {path}")
        actual_format = item.get("format")
        if provenance.get("buildKind") != "incremental-module-overlay" and actual_format is None:
            actual_format = artifact_format
        if item.get("architecture") != architecture or actual_format != artifact_format:
            raise SystemExit(
                f"provenance identity mismatch for {path}: expected {architecture}/{artifact_format}, "
                f"found {item.get('architecture')}/{item.get('format')}"
            )
    declared_server_arch = provenance.get("serverHostArch")
    if declared_server_arch is not None and declared_server_arch != wineserver_arch:
        raise SystemExit(
            f"provenance.serverHostArch={declared_server_arch} does not match host {wineserver_arch}"
        )
    declared_client_arch = provenance.get("clientHostArch")
    if declared_client_arch is not None and declared_client_arch != "x86_64":
        raise SystemExit(
            f"provenance.clientHostArch={declared_client_arch} is unsupported"
        )
PY
if [ "$host_wineserver_sha" = "$OLD_HOST_WINESERVER_SHA256" ] || [ "$host_ntdll_sha" = "$OLD_HOST_NTDLL_SHA256" ]; then
  echo "refusing old Wine 11.0 host fingerprints as upstream host prefix" >&2
  echo "wineserver=$host_wineserver_sha ntdll=$host_ntdll_sha" >&2
  exit 6
fi

host_wine_version=$("$host_prefix/bin/wine" --version 2>/dev/null || true)
[ -n "$host_wine_version" ] || die "unable to read host wine --version"
case "$host_wine_version" in
  *"$wine_version_field"*) ;;
  *)
    die "host wine version mismatch: provenance.wineVersion=$wine_version_field, wine --version=$host_wine_version"
    ;;
esac

gptk_lib=$(resolve_gptk_lib_root "$gptk_runtime")
d3dmetal_framework="$gptk_lib/external/D3DMetal.framework"
d3dmetal_binary="$d3dmetal_framework/Versions/A/D3DMetal"
converter="$d3dmetal_framework/Versions/A/Resources/libmetalirconverter.dylib"
require_file "$converter"
require_file "$gptk_lib/external/libd3dshared.dylib"
require_file "$d3dmetal_binary"
require_file "$d3dmetal_framework/Versions/A/Resources/Info.plist"

for rel in \
  wine/x86_64-windows/d3d10.dll \
  wine/x86_64-windows/d3d10core.dll \
  wine/x86_64-windows/d3d11.dll \
  wine/x86_64-windows/d3d12.dll \
  wine/x86_64-windows/dxgi.dll \
  wine/x86_64-windows/nvapi64.dll \
  wine/x86_64-unix/d3d10.so \
  wine/x86_64-unix/d3d11.so \
  wine/x86_64-unix/d3d12.so \
  wine/x86_64-unix/dxgi.so \
  wine/x86_64-unix/nvapi64.so
do
  [ -e "$gptk_lib/$rel" ] || die "missing GPTK graphics component: $rel"
done

if [ -f "$gptk_lib/wine/x86_64-windows/nvngx-on-metalfx.dll" ]; then
  gptk_nvngx_windows=$gptk_lib/wine/x86_64-windows/nvngx-on-metalfx.dll
elif [ -f "$gptk_lib/wine/x86_64-windows/nvngx.dll" ]; then
  gptk_nvngx_windows=$gptk_lib/wine/x86_64-windows/nvngx.dll
else
  die "missing GPTK nvngx Windows PE (nvngx-on-metalfx.dll or nvngx.dll)"
fi
if [ -e "$gptk_lib/wine/x86_64-unix/nvngx-on-metalfx.so" ]; then
  gptk_nvngx_unix=$gptk_lib/wine/x86_64-unix/nvngx-on-metalfx.so
elif [ -e "$gptk_lib/wine/x86_64-unix/nvngx.so" ]; then
  gptk_nvngx_unix=$gptk_lib/wine/x86_64-unix/nvngx.so
else
  die "missing GPTK nvngx Unix bridge (nvngx-on-metalfx.so or nvngx.so)"
fi

verify_hash "$gptk_lib/wine/x86_64-windows/d3d10core.dll" "$GPTK_D3D10CORE_SHA256"
verify_hash "$gptk_lib/wine/x86_64-windows/d3d11.dll" "$GPTK_D3D11_SHA256"
verify_hash "$gptk_lib/wine/x86_64-windows/dxgi.dll" "$GPTK_DXGI_SHA256"
verify_hash "$gptk_lib/wine/x86_64-windows/d3d12.dll" "$GPTK_D3D12_SHA256"

# The patch-site inspection is diagnostic; the artifact hash is the authority
# for every byte of the known-good patched converter before re-signing.
verify_hash "$converter" "$GPTK_FP64_PATCHED_CONVERTER_SHA256"
inspection=$(node "$repo_dir/scripts/metalir-fp64-codec-patch.mjs" inspect "$converter")
printf '%s\n' "$inspection" | grep -q '"mode": "patched"' || {
  echo "GPTK source converter is not the verified FP64 codec patch" >&2
  printf '%s\n' "$inspection" >&2
  exit 3
}
converter_sha=$(sha256_file "$converter")
converter_signature_normalized_sha=$(
  node "$repo_dir/scripts/metalir-fp64-codec-patch.mjs" \
    signature-normalized-sha256 "$converter"
)

d3dmetal_version=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' \
  "$d3dmetal_framework/Versions/A/Resources/Info.plist" 2>/dev/null || true)
if [ "$d3dmetal_version" != "4.0b2" ]; then
  die "D3DMetal version must be 4.0b2, found ${d3dmetal_version:-missing}"
fi

d3dmetal_source_sha=$(sha256_file "$d3dmetal_binary")
d3dmetal_source_inspection=$(node "$repo_dir/scripts/d3dmetal-pso-cache-patch.mjs" inspect "$d3dmetal_binary")
printf '%s\n' "$d3dmetal_source_inspection" | grep -q '"mode": "original"' || {
  echo "GPTK source D3DMetal is not the exact pristine composite patch input" >&2
  printf '%s\n' "$d3dmetal_source_inspection" >&2
  exit 3
}

stage="$output_dir/stage"
archive="$output_dir/${archive_name}.tar.xz"
pso_build_dir="$output_dir/native-pso-cache"
pso_build_module="$pso_build_dir/libYaaglNativePsoCache.dylib"
pso_build_manifest="$pso_build_dir/build-manifest.json"

mkdir -p "$output_dir"
if [ -e "$stage" ] || [ -e "$archive" ] || [ -e "$archive.sha256" ] || [ -e "$pso_build_dir" ]; then
  echo "refusing to overwrite existing build output under $output_dir" >&2
  exit 4
fi
node "$repo_dir/scripts/build-d3dmetal-pso-cache.mjs" "$pso_build_dir"
require_file "$pso_build_module"
require_file "$pso_build_manifest"
pso_module_pre_sign_sha=$(sha256_file "$pso_build_module")
pso_module_source_sha=$(/usr/bin/python3 - "$pso_build_manifest" "$repo_dir" "$pso_build_module" <<'PY'
import hashlib, json, pathlib, sys
manifest_path, repo_path, module_path = map(pathlib.Path, sys.argv[1:])
with manifest_path.open(encoding="utf-8") as stream:
    manifest = json.load(stream)
if manifest.get("schemaVersion") != 1 or manifest.get("architecture") != "x86_64" \
        or manifest.get("deploymentTarget") != "14.0":
    raise SystemExit("native PSO cache build manifest identity mismatch")
sources = manifest.get("sources")
if not isinstance(sources, list) or not sources:
    raise SystemExit("native PSO cache build manifest has no sources")
source_fingerprint = hashlib.sha256()
seen = set()
for source in sources:
    if not isinstance(source, dict) or not isinstance(source.get("path"), str):
        raise SystemExit("native PSO cache build manifest contains an invalid source")
    relative = pathlib.PurePosixPath(source["path"])
    if relative.is_absolute() or ".." in relative.parts or source["path"] in seen:
        raise SystemExit("native PSO cache build manifest contains an unsafe or duplicate source")
    seen.add(source["path"])
    digest = hashlib.sha256((repo_path / pathlib.Path(*relative.parts)).read_bytes()).hexdigest()
    if source.get("sha256") != digest:
        raise SystemExit(f"native PSO cache source hash mismatch: {source['path']}")
    source_fingerprint.update(source["path"].encode("utf-8") + b"\0" + digest.encode("ascii") + b"\n")
compiler = manifest.get("compiler")
if not isinstance(compiler, dict) or not isinstance(compiler.get("path"), str) \
        or not isinstance(compiler.get("version"), str) or not compiler["version"] \
        or not isinstance(manifest.get("compileArgs"), list):
    raise SystemExit("native PSO cache compiler provenance is incomplete")
module = manifest.get("module")
actual_module_hash = hashlib.sha256(module_path.read_bytes()).hexdigest()
if not isinstance(module, dict) or module.get("file") != "libYaaglNativePsoCache.dylib" \
        or module.get("sha256") != actual_module_hash:
    raise SystemExit("native PSO cache build module hash mismatch")
print(source_fingerprint.hexdigest())
PY
)
validate_macho_arch "$pso_build_module"
verify_system_only_dependencies "$pso_build_module"

mkdir -p "$stage/wine"
ditto "$host_prefix" "$stage/wine"

staged_host_wineserver_sha=$(sha256_file "$stage/wine/bin/wineserver")
staged_host_ntdll_sha=$(sha256_file "$stage/wine/lib/wine/x86_64-unix/ntdll.so")
staged_host_macdrv_sha=$(sha256_file "$stage/wine/lib/wine/x86_64-unix/winemac.so")
[ "$staged_host_wineserver_sha" = "$host_wineserver_sha" ] || die "host wineserver changed during stage copy"
[ "$staged_host_ntdll_sha" = "$host_ntdll_sha" ] || die "host ntdll changed during stage copy"
[ "$staged_host_macdrv_sha" = "$host_macdrv_sha" ] || die "host winemac changed during stage copy"
if [ "$build_kind" = incremental-module-overlay ]; then
  printf '%s\n' "$TUNED_ARTIFACT_IDENTITIES" | while IFS='|' read -r rel arch format; do
    [ "$(sha256_file "$stage/wine/$rel")" = "$(sha256_file "$host_prefix/$rel")" ] \
      || die "tuned artifact changed during stage copy: $rel"
    case "$format:$arch" in
      macho:*) validate_macho_arch "$stage/wine/$rel" ;;
      pe:x86_64) validate_pe_machine "$stage/wine/$rel" 0x8664 ;;
      pe:i386) validate_pe_machine "$stage/wine/$rel" 0x014c ;;
    esac
  done
fi

steam_stage_dir="$stage/wine/share/yaagl/steam"
mkdir -p "$steam_stage_dir"
for helper_name in steam64.exe steam32.exe lsteamclient64.dll lsteamclient32.dll; do
  cp -p "$steam_helper_dir/$helper_name" "$steam_stage_dir/$helper_name"
  cmp -s "$steam_helper_dir/$helper_name" "$steam_stage_dir/$helper_name" \
    || die "staged Steam helper differs from source: $helper_name"
done

rm -rf \
  "$stage/wine/lib/external" \
  "$stage/wine/lib/GStreamer.framework" \
  "$stage/wine/yaagl-wine-runtime.json" \
  "$stage/wine/yaagl-local-d3dmetal-runtime.txt"

mkdir -p "$stage/wine/lib"
deps_report="$stage/wine/yaagl-wine-p3-deps-report.txt"
: > "$deps_report"

bundle_dep_file() {
  src=$1
  dest_name=$2
  case "$dest_name" in
    ntdll.so|wineserver|wine|wine64|libwine*)
      die "refusing to import host Wine module as external dep: $src"
      ;;
  esac
  if [ ! -e "$stage/wine/lib/$dest_name" ]; then
    ditto "$src" "$stage/wine/lib/$dest_name"
    printf 'bundled %s <- %s\n' "$dest_name" "$src" >> "$deps_report"
  fi
}

prune_gstreamer_runtime_payload() {
  fw=$1
  ver="$fw/Versions/1.0"
  [ -d "$ver" ] || die "staged GStreamer.framework missing Versions/1.0: $fw"

  # Runtime-only payload after ditto of ready/lib/GStreamer.framework.
  # Keep framework shell (Versions/Current, Libraries/GStreamer/Resources),
  # lib/*.dylib (+ versioned siblings), lib/gstreamer-1.0 codecs,
  # gst-plugin-scanner, share/licenses, etc/fonts, share/fontconfig,
  # lib/gio/modules/*.so, and etc/ssl/certs for soup/TLS. SDK tree untouched.
  rm -rf \
    "$fw/Headers" \
    "$fw/Commands" \
    "$ver/Headers" \
    "$ver/include" \
    "$ver/Commands" \
    "$ver/bin" \
    "$ver/lib/pkgconfig" \
    "$ver/lib/cmake" \
    "$ver/lib/python3.9" \
    "$ver/lib/gstreamer-1.0/libgstpython.dylib" \
    "$ver/share/aclocal" \
    "$ver/share/cmake" \
    "$ver/share/gir-1.0" \
    "$ver/share/glib-2.0" \
    "$ver/share/gobject-introspection-1.0" \
    "$ver/share/gtk-doc" \
    "$ver/share/man" \
    "$ver/share/doc" \
    "$ver/share/gstreamer" \
    "$ver/share/gstreamer-1.0" \
    "$ver/share/gst-validate-launcher"

  # Keep libexec/.../gst-plugin-scanner only (gst-ptp-helper optional drop).
  if [ -d "$ver/libexec" ]; then
    scanner="$ver/libexec/gstreamer-1.0/gst-plugin-scanner"
    if [ ! -e "$scanner" ]; then
      die "GStreamer gst-plugin-scanner missing after stage: $scanner"
    fi
    find "$ver/libexec" -type f ! -name 'gst-plugin-scanner' -delete
    find "$ver/libexec" -mindepth 1 -type d -empty -delete 2>/dev/null || true
    [ -e "$scanner" ] || die "gst-plugin-scanner lost during GStreamer prune"
  fi

  # Drop static archives / pkgconfig anywhere under Versions/1.0, including
  # plugins and gio modules, while preserving *.dylib / *.so runtime modules.
  find "$ver" \( -name '*.a' -o -name '*.la' -o -name '*.pc' \) -type f -delete
  if [ -d "$ver/lib/gio/modules" ]; then
    find "$ver/lib/gio/modules" \( -name 'pkgconfig' -o -name '*.a' \) -delete 2>/dev/null || true
    [ -n "$(find "$ver/lib/gio/modules" -name '*.so' 2>/dev/null | head -n 1)" ] \
      || printf 'warning: GStreamer lib/gio/modules has no *.so after prune\n' >> "$deps_report"
  fi

  [ -d "$ver/share/licenses" ] || printf 'warning: GStreamer share/licenses missing after prune\n' >> "$deps_report"
  [ -d "$ver/lib/gstreamer-1.0" ] || die "GStreamer plugins dir missing after prune"
  [ -e "$ver/lib/libgstreamer-1.0.0.dylib" ] || [ -e "$ver/lib/libgstreamer-1.0.dylib" ] \
    || die "GStreamer core library missing after prune"
  [ -e "$ver/GStreamer" ] || die "GStreamer framework binary missing after prune"
  [ -e "$ver/Libraries" ] || [ -d "$ver/lib" ] \
    || die "GStreamer Libraries/lib shell missing after prune"
  [ -f "$ver/Resources/Info.plist" ] \
    || die "GStreamer Resources/Info.plist missing after prune"
  # Recommended small runtime support for pango/cairo/gio TLS paths.
  [ -d "$ver/etc/fonts" ] || printf 'warning: GStreamer etc/fonts missing after prune\n' >> "$deps_report"
  [ -d "$ver/share/fontconfig" ] || printf 'warning: GStreamer share/fontconfig missing after prune\n' >> "$deps_report"
  [ -f "$ver/etc/ssl/certs/ca-certificates.crt" ] \
    || printf 'warning: GStreamer etc/ssl/certs/ca-certificates.crt missing after prune\n' >> "$deps_report"

  printf 'pruned GStreamer to runtime-only payload under %s\n' "$fw" >> "$deps_report"
}

gst_needed=0
if [ -e "$host_prefix/lib/wine/x86_64-unix/winegstreamer.so" ] \
  || [ -e "$host_prefix/lib/wine/x86_64-unix/winedmo.so" ]; then
  gst_needed=1
fi
if [ "$gst_needed" -eq 1 ]; then
  if gst_fw=$(find_gstreamer_framework); then
    ditto "$gst_fw" "$stage/wine/lib/GStreamer.framework"
    printf 'bundled GStreamer.framework <- %s\n' "$gst_fw" >> "$deps_report"
    prune_gstreamer_runtime_payload "$stage/wine/lib/GStreamer.framework"
  else
    die "host links winegstreamer/winedmo but GStreamer.framework missing under $gstreamer_deps_root (MediaDeps not ready)"
  fi
fi

# Resolve Wine dlopen(SONAME_*) seeds. These are NOT otool-linked on host modules.
collect_dlopen_soname_seeds() {
  seeds_tmp=$(mktemp)
  config_h=""
  for cand in \
    "$repo_dir/build/wine-p3/build-x64/include/config.h" \
    "$(dirname -- "$host_prefix")/build-x64/include/config.h" \
    "$host_prefix/include/wine/config.h"
  do
    if [ -f "$cand" ]; then
      config_h=$cand
      break
    fi
  done

  printf '%s\n' \
    libgnutls.30.dylib \
    libfreetype.6.dylib \
    libSDL2-2.0.0.dylib \
    > "$seeds_tmp"

  if [ -n "$config_h" ]; then
    python3 - "$config_h" >> "$seeds_tmp" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
for m in re.finditer(r'#define\s+(SONAME_LIB[A-Z0-9_]+)\s+"([^"]+)"', text):
    name = m.group(2)
    if name.startswith("libcups.") or name == "libodbc.dylib":
        continue
    print(name)
PY
    printf 'dlopen SONAME seeds from %s\n' "$config_h" >> "$deps_report"
  else
    printf 'dlopen SONAME seeds: config.h not found; using GnuTLS/FreeType/SDL defaults\n' >> "$deps_report"
  fi

  sort -u "$seeds_tmp" -o "$seeds_tmp"
  cat "$seeds_tmp"
  rm -f "$seeds_tmp"
}

seed_files_tmp=$(mktemp)
: > "$seed_files_tmp"
while IFS= read -r soname; do
  [ -n "$soname" ] || continue
  case "$soname" in
    libcups.*|libodbc.dylib)
      continue
      ;;
  esac
  if [ -e "$stage/wine/lib/$soname" ]; then
    printf '%s\n' "$stage/wine/lib/$soname" >> "$seed_files_tmp"
    continue
  fi
  if resolved=$(find_macports_lib "$soname"); then
    bundle_dep_file "$resolved" "$soname"
    printf '%s\n' "$stage/wine/lib/$soname" >> "$seed_files_tmp"
  else
    die "missing MacDeps dlopen(SONAME) seed: $soname (GnuTLS/FreeType/SDL must be packaged)"
  fi
done <<EOF
$(collect_dlopen_soname_seeds)
EOF

# Transitive host-linked closure from MacDeps/MediaDeps (not old wine/lib dump),
# plus otool deps of explicit dlopen seeds.
work_list=$(mktemp)
seen_list=$(mktemp)
{
  find "$stage/wine/bin" "$stage/wine/lib/wine" -type f 2>/dev/null
  cat "$seed_files_tmp"
} | while IFS= read -r candidate; do
  [ -n "$candidate" ] || continue
  macho_kind "$candidate" || continue
  macho_deps "$candidate"
done | sort -u > "$work_list"
rm -f "$seed_files_tmp"

missing=0
while [ -s "$work_list" ]; do
  next_list=$(mktemp)
  while IFS= read -r dep; do
    [ -n "$dep" ] || continue
    if grep -Fxq -- "$dep" "$seen_list" 2>/dev/null; then
      continue
    fi
    printf '%s\n' "$dep" >> "$seen_list"

    case "$dep" in
      @rpath/*|@loader_path/*|@executable_path/*)
        name=$(basename -- "$dep")
        if [ -e "$stage/wine/lib/$name" ] \
          || [ -e "$stage/wine/lib/wine/x86_64-unix/$name" ] \
          || [ -e "$stage/wine/lib/GStreamer.framework/Versions/1.0/lib/$name" ] \
          || [ -e "$stage/wine/lib/GStreamer.framework/Libraries/$name" ]; then
          continue
        fi
        if resolved=$(find_macports_lib "$name"); then
          bundle_dep_file "$resolved" "$name"
          macho_deps "$stage/wine/lib/$name" >> "$next_list"
          continue
        fi
        echo "unresolved relocatable dependency: $dep" >&2
        missing=1
        ;;
      /*)
        is_system_dep "$dep" && continue
        case "$dep" in
          "$host_prefix"/*|"$stage/wine"/*)
            continue
            ;;
          */GStreamer.framework/*|"$gstreamer_deps_root"/*)
            if [ -d "$stage/wine/lib/GStreamer.framework" ]; then
              continue
            fi
            ;;
        esac
        base=$(basename -- "$dep")
        case "$base" in
          ntdll.so|wineserver|wine|wine64|libwine*)
            echo "refusing to import host Wine module from external path: $dep" >&2
            missing=1
            continue
            ;;
        esac
        if [ -e "$stage/wine/lib/$base" ]; then
          continue
        fi
        if resolved=$(find_macports_lib "$base"); then
          bundle_dep_file "$resolved" "$base"
          macho_deps "$stage/wine/lib/$base" >> "$next_list"
          continue
        fi
        case "$dep" in
          "$macports_deps_root"/*|"$gstreamer_deps_root"/*)
            if [ -f "$dep" ]; then
              bundle_dep_file "$dep" "$base"
              macho_deps "$stage/wine/lib/$base" >> "$next_list"
              continue
            fi
            ;;
        esac
        echo "missing external dependency: $dep" >&2
        missing=1
        ;;
    esac
  done < "$work_list"
  sort -u "$next_list" -o "$next_list"
  mv "$next_list" "$work_list"
done
rm -f "$work_list" "$seen_list"
[ "$missing" -eq 0 ] || die "host external dynamic dependencies incomplete; refusing to conceal ABI gaps by copying old host modules"

macho_rpaths() {
  target=$1
  arch=$(macho_scan_arch "$target")
  validate_macho_arch "$target"
  otool -arch "$arch" -l "$target" 2>/dev/null | awk '
    $1 == "cmd" && $2 == "LC_RPATH" { in_rpath=1; next }
    in_rpath && $1 == "path" { print $2; in_rpath=0 }
  '
}

has_rpath() {
  target=$1
  want=$2
  macho_rpaths "$target" | grep -Fxq -- "$want"
}

# Explicit || die: set -e does not reliably abort on install_name_tool
# failures inside shell functions on macOS /bin/sh.
add_rpath_if_missing() {
  target=$1
  rpath=$2
  if has_rpath "$target" "$rpath"; then
    return 0
  fi
  install_name_tool -add_rpath "$rpath" "$target" \
    || die "install_name_tool -add_rpath failed: $rpath -> $target"
}

delete_rpath_if_present() {
  target=$1
  rpath=$2
  if has_rpath "$target" "$rpath"; then
    install_name_tool -delete_rpath "$rpath" "$target" \
      || die "install_name_tool -delete_rpath failed: $rpath -> $target"
  fi
}

is_build_deps_rpath() {
  case "$1" in
    @loader_path*|@executable_path*|@rpath*)
      return 1
      ;;
    /System/*|/usr/lib/*)
      return 1
      ;;
    */build/wine-p3/deps/*|"$macports_deps_root"*|"$gstreamer_deps_root"*|/opt/local/*|*/opt/local/*)
      return 0
      ;;
    /*)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

strip_build_deps_rpaths() {
  target=$1
  rpath_tmp=$(mktemp)
  macho_rpaths "$target" > "$rpath_tmp"
  while IFS= read -r rp; do
    [ -n "$rp" ] || continue
    if is_build_deps_rpath "$rp"; then
      delete_rpath_if_present "$target" "$rp"
    fi
  done < "$rpath_tmp"
  rm -f "$rpath_tmp"
}

staged_gst_lib_has() {
  name=$1
  [ -e "$stage/wine/lib/GStreamer.framework/Versions/1.0/lib/$name" ] \
    || [ -e "$stage/wine/lib/GStreamer.framework/Libraries/$name" ]
}

# Rewrite bundled MacPorts dylib ids + references to @rpath, rewrite absolute
# GStreamer framework library refs to @rpath + relative GST rpaths.
# Remove absolute/build/deps LC_RPATH entries BEFORE adding relative ones so
# load-command space is freed first (host may still need headerpad).
# install_name_tool mutations are fatal via explicit || die.
rewrite_to_rpath() {
  target=$1
  macho_kind "$target" || return 0
  case "$target" in
    */lib/external/D3DMetal.framework/*|*/lib/external/libd3dshared.dylib|*/lib/GStreamer.framework/*)
      return 0
      ;;
  esac

  case "$target" in
    "$stage/wine/lib/"*.dylib|"$stage/wine/lib/"*.so)
      base=$(basename -- "$target")
      install_name_tool -id "@rpath/$base" "$target" \
        || die "install_name_tool -id failed: $target"
      ;;
  esac

  deps_tmp=$(mktemp)
  macho_deps "$target" > "$deps_tmp"
  while IFS= read -r dep; do
    [ -n "$dep" ] || continue
    is_system_dep "$dep" && continue
    case "$dep" in
      @rpath/*|@loader_path/*|@executable_path/*)
        continue
        ;;
    esac
    # Never treat the Mach-O's own path / install-id as a rewrite target.
    [ "$dep" = "$target" ] && continue
    base=$(basename -- "$dep")
    [ "$base" = "$(basename -- "$target")" ] && [ ! -e "$stage/wine/lib/$base" ] && continue
    if [ -e "$stage/wine/lib/$base" ]; then
      install_name_tool -change "$dep" "@rpath/$base" "$target" \
        || die "install_name_tool -change failed: $dep -> @rpath/$base ($target)"
      continue
    fi
    case "$dep" in
      */GStreamer.framework/*|"$gstreamer_deps_root"/*|/Library/Frameworks/GStreamer.framework/*)
        if staged_gst_lib_has "$base"; then
          install_name_tool -change "$dep" "@rpath/$base" "$target" \
            || die "install_name_tool -change failed: $dep -> @rpath/$base ($target)"
          continue
        fi
        # Skip non-library framework paths (e.g. libexec tools) — not link deps.
        case "$base" in
          *.dylib|*.so) ;;
          *) continue ;;
        esac
        echo "unable to rewrite absolute GStreamer ref: $dep (in $target)" >&2
        rm -f "$deps_tmp"
        return 1
        ;;
    esac
  done < "$deps_tmp"
  rm -f "$deps_tmp"

  # Free load-command space before adding relative rpaths.
  strip_build_deps_rpaths "$target"

  case "$target" in
    "$stage/wine/bin/"*)
      add_rpath_if_missing "$target" "@loader_path/../lib"
      if [ -d "$stage/wine/lib/GStreamer.framework/Versions/1.0/lib" ]; then
        add_rpath_if_missing "$target" "@loader_path/../lib/GStreamer.framework/Versions/1.0/lib"
      fi
      ;;
    "$stage/wine/lib/wine/"*)
      add_rpath_if_missing "$target" "@loader_path/../../"
      if [ -d "$stage/wine/lib/GStreamer.framework/Versions/1.0/lib" ]; then
        add_rpath_if_missing "$target" "@loader_path/../../GStreamer.framework/Versions/1.0/lib"
      fi
      ;;
    "$stage/wine/lib/"*)
      add_rpath_if_missing "$target" "@loader_path"
      ;;
  esac
}

# Overlay GPTK graphics only. Never copy bin/, ntdll, server, winemac.
mkdir -p "$stage/wine/lib/external" \
  "$stage/wine/lib/wine/x86_64-windows" \
  "$stage/wine/lib/wine/x86_64-unix"

staged_d3dmetal_framework="$stage/wine/lib/external/D3DMetal.framework"
staged_d3dmetal_binary="$staged_d3dmetal_framework/Versions/A/D3DMetal"
staged_converter="$staged_d3dmetal_framework/Versions/A/Resources/libmetalirconverter.dylib"
ditto "$d3dmetal_framework" "$staged_d3dmetal_framework"
ditto "$gptk_lib/external/libd3dshared.dylib" "$stage/wine/lib/external/libd3dshared.dylib"
staged_pso_module="$staged_d3dmetal_framework/Versions/A/Resources/libYaaglNativePsoCache.dylib"
ditto "$pso_build_module" "$staged_pso_module"
verify_hash "$staged_converter" "$GPTK_FP64_PATCHED_CONVERTER_SHA256"
verify_hash "$staged_pso_module" "$pso_module_pre_sign_sha"
validate_macho_arch "$staged_pso_module"
verify_system_only_dependencies "$staged_pso_module"

staged_d3dmetal_inspection=$(node "$repo_dir/scripts/d3dmetal-pso-cache-patch.mjs" inspect "$staged_d3dmetal_binary")
printf '%s\n' "$staged_d3dmetal_inspection" | grep -q '"mode": "original"' || {
  echo "staged D3DMetal is not the exact pristine composite patch input" >&2
  printf '%s\n' "$staged_d3dmetal_inspection" >&2
  exit 3
}
node "$repo_dir/scripts/d3dmetal-pso-cache-patch.mjs" patch \
  "$staged_d3dmetal_binary" "$staged_d3dmetal_binary" >/dev/null
staged_d3dmetal_inspection=$(node "$repo_dir/scripts/d3dmetal-pso-cache-patch.mjs" inspect "$staged_d3dmetal_binary")
printf '%s\n' "$staged_d3dmetal_inspection" | grep -q '"mode": "patched"' || {
  echo "staged D3DMetal composite patch verification failed" >&2
  printf '%s\n' "$staged_d3dmetal_inspection" >&2
  exit 3
}
[ "$(macho_deps "$staged_d3dmetal_binary" | grep -Fxc "$PSO_CACHE_DEPENDENCY")" -eq 1 ] \
  || die "staged D3DMetal does not have exactly one native PSO cache dependency"
d3dmetal_pre_sign_sha=$(sha256_file "$staged_d3dmetal_binary")

copy_gptk_node() {
  src=$1
  dst=$2
  base=$(basename -- "$dst")
  case "$base" in
    ntdll.so|wineserver|wine|wine64)
      die "internal error: refusing GPTK copy of host module $base"
      ;;
  esac
  mkdir -p "$(dirname -- "$dst")"
  rm -rf "$dst"
  cp -a "$src" "$dst"
}

for name in d3d10.dll d3d10core.dll d3d11.dll d3d12.dll dxgi.dll nvapi64.dll; do
  copy_gptk_node \
    "$gptk_lib/wine/x86_64-windows/$name" \
    "$stage/wine/lib/wine/x86_64-windows/$name"
done
for name in d3d10.so d3d11.so d3d12.so dxgi.so nvapi64.so; do
  copy_gptk_node \
    "$gptk_lib/wine/x86_64-unix/$name" \
    "$stage/wine/lib/wine/x86_64-unix/$name"
done

rm -f "$stage/wine/lib/wine/x86_64-windows/nvngx.dll" \
  "$stage/wine/lib/wine/x86_64-unix/nvngx.so" \
  "$stage/wine/lib/wine/x86_64-windows/nvngx-on-metalfx.dll" \
  "$stage/wine/lib/wine/x86_64-unix/nvngx-on-metalfx.so"
cp -a "$gptk_nvngx_windows" "$stage/wine/lib/wine/x86_64-windows/nvngx.dll"
cp -a "$gptk_nvngx_unix" "$stage/wine/lib/wine/x86_64-unix/nvngx.so"

rm -f "$stage/wine/lib/wine/x86_64-windows/nvapi.dll" \
  "$stage/wine/lib/wine/x86_64-unix/nvapi.so"
ln -s nvapi64.dll "$stage/wine/lib/wine/x86_64-windows/nvapi.dll"
ln -s nvapi64.so "$stage/wine/lib/wine/x86_64-unix/nvapi.so"

rm -rf \
  "$stage/wine/lib/external/D3DMetal.framework.pristine-before-rtxgi-tags" \
  "$stage/wine/lib/external/"*.bak \
  "$stage/wine/lib/external/"*cache*
rm -f \
  "$stage/wine/lib/wine/x86_64-unix/winemetal.so" \
  "$stage/wine/lib/wine/x86_64-windows/winemetal.dll" \
  "$stage/wine/lib/wine/i386-unix/winemetal.so" \
  "$stage/wine/lib/wine/i386-windows/winemetal.dll"

[ "$(sha256_file "$stage/wine/bin/wineserver")" = "$host_wineserver_sha" ] \
  || die "host wineserver was replaced during GPTK overlay"
[ "$(sha256_file "$stage/wine/lib/wine/x86_64-unix/ntdll.so")" = "$host_ntdll_sha" ] \
  || die "host ntdll.so was replaced during GPTK overlay"
[ "$(sha256_file "$stage/wine/lib/wine/x86_64-unix/winemac.so")" = "$host_macdrv_sha" ] \
  || die "host winemac.so was replaced during GPTK overlay"
if [ "$build_kind" = incremental-module-overlay ]; then
  printf '%s\n' "$TUNED_ARTIFACT_IDENTITIES" | while IFS='|' read -r rel arch format; do
    [ "$(sha256_file "$stage/wine/$rel")" = "$(sha256_file "$host_prefix/$rel")" ] \
      || die "tuned artifact was replaced during GPTK overlay: $rel"
  done
fi

if [ -e "$stage/wine/bin/wine.real" ]; then
  die "host prefix already contains wine.real; refusing ambiguous wrapper layout"
fi
mv "$stage/wine/bin/wine" "$stage/wine/bin/wine.real"
cp "$repo_dir/scripts/wine-launch-wrapper-p3.sh" "$stage/wine/bin/wine"
chmod 755 "$stage/wine/bin/wine" "$stage/wine/bin/wine.real"

rm -f "$stage/wine/yaagl-wine-runtime.json" \
  "$stage/wine/yaagl-wine-p3-provenance.json" \
  "$stage/wine/yaagl-wine-runtime-files.json"
cat > "$stage/wine/yaagl-wine-p3-runtime.txt" <<EOF
Wine identity: $archive_name
Host wine --version: $host_wine_version
Provenance wineVersion: $wine_version_field
Upstream commit: $upstream_commit
Built at: ${built_at:-unknown}
Host wineserver SHA-256: $host_wineserver_sha
Host ntdll.so SHA-256: $host_ntdll_sha
Host winemac.so SHA-256: $host_macdrv_sha
Patches: ${patches_json:-[]}
Configure args: ${configure_args_json:-[]}
Graphics backend: GPTK 4.0b2 D3DMetal
D3DMetal composite patch: stage lookup lock release + native PSO and DXIL function reuse hooks (pre-sign SHA-256: $d3dmetal_pre_sign_sha)
Native PSO cache: per native Metal device and exact descriptor key; retained for device lifetime without entry-count eviction
Native function cache: per native Metal device and exact extraction key; retained for device lifetime without entry-count eviction
Metal IR converter: verified local FP64 codec patch ($converter_sha)
Renderer selection: launcher-controlled; no game argument injection
DXMT components: excluded
Apple GPTK: local-only; do not redistribute
Archive layout: Yaagl-compatible top-level wine/
Deps: MacDeps ($macports_deps_root) + MediaDeps ($gstreamer_deps_root); host-linked + dlopen(SONAME) seeds
Runtime libs: package-relative DYLD_FALLBACK_LIBRARY_PATH + GST plugin/scanner via wrapper (P3 marker)
Wineserver Mach-O architecture: $wineserver_arch
EOF

# The only permitted non-x86_64 Mach-O is the tuned arm64 wineserver. This gate
# also covers copied GPTK and GStreamer payloads before otool/rewrite/signing.
find "$stage/wine/bin" "$stage/wine/lib" -type f 2>/dev/null \
  | while IFS= read -r candidate; do
      macho_kind "$candidate" || continue
      validate_macho_arch "$candidate" || exit 1
    done

# install_name rewrite for bundled deps + host Mach-Os + staged GStreamer.
# Failures are fatal (set -e); duplicate rpaths avoided via inspect.
find "$stage/wine/lib" -maxdepth 1 -type f \( -name '*.dylib' -o -name '*.so' \) 2>/dev/null \
  | while IFS= read -r f; do rewrite_to_rpath "$f" || exit 1; done
find "$stage/wine/bin" "$stage/wine/lib/wine" -type f 2>/dev/null \
  | while IFS= read -r f; do
      case "$f" in
        *.dll|*.exe|*.a|*.pc|*.h) continue ;;
      esac
      rewrite_to_rpath "$f" || exit 1
    done
if [ -d "$stage/wine/lib/GStreamer.framework" ]; then
  find "$stage/wine/lib/GStreamer.framework" -type f 2>/dev/null \
    | while IFS= read -r f; do
        macho_kind "$f" || continue
        rewrite_to_rpath "$f" || exit 1
      done
fi

# Ad hoc sign all rewritten Mach-Os (incl ntdll/winemac and bundled dylibs).
# No extra entitlements are required by the source-built host.
sign_macho() {
  target=$1
  codesign --force --sign - "$target"
  codesign --verify --strict "$target"
}

verify_adhoc_signature() {
  target=$1
  codesign -d --verbose=4 "$target" 2>&1 | grep -q '^Signature=adhoc$' \
    || die "graphics artifact does not have the expected ad hoc signature: $target"
}

find "$stage/wine/bin" "$stage/wine/lib" -type f 2>/dev/null | while IFS= read -r candidate; do
  case "$candidate" in
    */lib/external/D3DMetal.framework/*)
      continue
      ;;
  esac
  macho_kind "$candidate" || continue
  sign_macho "$candidate"
done

# The native cache was copied before framework signing. Sign it explicitly, then
# seal the modified framework and all of its nested code in one deep-sign pass.
sign_macho "$staged_pso_module"
codesign --force --deep --sign - "$staged_d3dmetal_framework"

codesign --verify --strict "$stage/wine/bin/wine.real"
codesign --verify --strict "$stage/wine/bin/wineserver"
codesign --verify --strict "$stage/wine/lib/wine/x86_64-unix/ntdll.so"
codesign --verify --strict "$stage/wine/lib/wine/x86_64-unix/winemac.so"
codesign --verify --deep --strict "$staged_d3dmetal_framework"
codesign --verify --strict "$staged_pso_module"
verify_adhoc_signature "$staged_d3dmetal_binary"
verify_adhoc_signature "$staged_pso_module"
verify_adhoc_signature "$staged_converter"
validate_macho_arch "$staged_pso_module"
verify_system_only_dependencies "$staged_pso_module"
[ "$(macho_deps "$staged_d3dmetal_binary" | grep -Fxc "$PSO_CACHE_DEPENDENCY")" -eq 1 ] \
  || die "signed D3DMetal does not have exactly one native PSO cache dependency"
if [ "$build_kind" = incremental-module-overlay ]; then
  printf '%s\n' "$TUNED_ARTIFACT_IDENTITIES" | while IFS='|' read -r rel arch format; do
    case "$format:$arch" in
      macho:*) validate_macho_arch "$stage/wine/$rel" ;;
      pe:x86_64)
        validate_pe_machine "$stage/wine/$rel" 0x8664
        [ "$(sha256_file "$stage/wine/$rel")" = "$(sha256_file "$host_prefix/$rel")" ] || die "staged PE changed: $rel"
        ;;
      pe:i386)
        validate_pe_machine "$stage/wine/$rel" 0x014c
        [ "$(sha256_file "$stage/wine/$rel")" = "$(sha256_file "$host_prefix/$rel")" ] || die "staged PE changed: $rel"
        ;;
    esac
  done
fi

# Ad-hoc signing may replace only the LC_CODE_SIGNATURE payload. Hashing the
# complete file with that declared blob zeroed rejects every other mutation.
signed_converter_signature_normalized_sha=$(
  node "$repo_dir/scripts/metalir-fp64-codec-patch.mjs" \
    signature-normalized-sha256 "$staged_converter"
)
[ "$signed_converter_signature_normalized_sha" = "$converter_signature_normalized_sha" ] || {
  echo "signed staged Metal IR converter differs outside its code signature" >&2
  echo "expected normalized SHA-256: $converter_signature_normalized_sha" >&2
  echo "actual normalized SHA-256: $signed_converter_signature_normalized_sha" >&2
  exit 3
}
signed_converter_inspection=$(node "$repo_dir/scripts/metalir-fp64-codec-patch.mjs" inspect "$staged_converter")
printf '%s\n' "$signed_converter_inspection" | grep -q '"mode": "patched"' || {
  echo "signed staged Metal IR converter patch verification failed" >&2
  printf '%s\n' "$signed_converter_inspection" >&2
  exit 3
}

signed_d3dmetal_inspection=$(node "$repo_dir/scripts/d3dmetal-pso-cache-patch.mjs" inspect "$staged_d3dmetal_binary")
printf '%s\n' "$signed_d3dmetal_inspection" | grep -q '"mode": "patched-signed"' || {
  echo "signed staged D3DMetal composite patch verification failed" >&2
  printf '%s\n' "$signed_d3dmetal_inspection" >&2
  exit 3
}
signed_d3dmetal_sha=$(sha256_file "$staged_d3dmetal_binary")
signed_pso_module_sha=$(sha256_file "$staged_pso_module")
pso_system_dependencies=$(otool -arch x86_64 -l "$staged_pso_module" 2>/dev/null | awk '
  $1 == "cmd" { load = ($2 == "LC_LOAD_DYLIB"); next }
  load && $1 == "name" { print $2; load = 0 }
')
printf 'D3DMetal signed artifact SHA-256: %s\nNative PSO cache signed artifact SHA-256: %s\n' \
  "$signed_d3dmetal_sha" "$signed_pso_module_sha" \
  >> "$stage/wine/yaagl-wine-p3-runtime.txt"
write_graphics_artifacts "$stage/wine"
verify_graphics_artifacts "$stage/wine"

verify_hash "$stage/wine/lib/wine/x86_64-windows/d3d10core.dll" "$GPTK_D3D10CORE_SHA256"
verify_hash "$stage/wine/lib/wine/x86_64-windows/d3d11.dll" "$GPTK_D3D11_SHA256"
verify_hash "$stage/wine/lib/wine/x86_64-windows/dxgi.dll" "$GPTK_DXGI_SHA256"
verify_hash "$stage/wine/lib/wine/x86_64-windows/d3d12.dll" "$GPTK_D3D12_SHA256"
[ "$(readlink "$stage/wine/lib/wine/x86_64-windows/nvapi.dll")" = "nvapi64.dll" ] \
  || die "nvapi.dll alias mismatch"
[ "$(readlink "$stage/wine/lib/wine/x86_64-unix/nvapi.so")" = "nvapi64.so" ] \
  || die "nvapi.so alias mismatch"
require_file "$stage/wine/lib/wine/x86_64-windows/nvngx.dll"
[ -e "$stage/wine/lib/wine/x86_64-unix/nvngx.so" ] || die "missing staged nvngx.so"
[ ! -e "$stage/wine/lib/wine/x86_64-unix/winemetal.so" ] \
  || die "winemetal.so still present in stage"
[ ! -e "$stage/wine/lib/wine/x86_64-windows/winemetal.dll" ] \
  || die "winemetal.dll still present in stage"
cmp -s "$repo_dir/scripts/wine-launch-wrapper-p3.sh" "$stage/wine/bin/wine" \
  || die "packaged baseline wrapper differs from current source"

# Generate package-specific provenance only after every overlay, install-name
# rewrite, signature, and generated graphics manifest is final. The input
# provenance identifies the requested catalog runtime; it is evidence, not a
# file that can be copied forward as self-attestation.
/usr/bin/python3 - "$provenance_json" "$stage/wine" "$runtime_id" "wine-$wine_version_field" <<'PY'
import hashlib
import json
import os
import pathlib
import sys

source_path, wine_root_arg, runtime_id, wine_version = sys.argv[1:]
wine_root = pathlib.Path(wine_root_arg)
with open(source_path, encoding="utf-8") as stream:
    source = json.load(stream)
if source.get("runtimeId") != runtime_id:
    raise SystemExit("input provenance runtimeId changed during packaging")
with open(wine_root / "yaagl-wine-p3-graphics-artifacts.json", encoding="utf-8") as stream:
    graphics = json.load(stream)

def artifact(relative_path):
    path = wine_root / relative_path
    data = path.read_bytes()
    return {"path": relative_path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

provenance = dict(source)
provenance.pop("packagingHandoff", None)
provenance.update({
    "runtimeId": runtime_id,
    "wineVersion": wine_version,
    "packageName": source.get("name"),
    "graphicsBackend": "d3dmetal",
    "precomposedD3DMetal": True,
    "d3dMetalGraphicsCache": True,
    "finalGraphicsArtifacts": graphics,
    "authenticatedArtifacts": [
        artifact("bin/wine"),
        artifact("bin/wine.real"),
        artifact("bin/wineserver"),
    ],
})
with open(wine_root / "yaagl-wine-p3-provenance.json", "w", encoding="utf-8", newline="\n") as stream:
    json.dump(provenance, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    stream.write("\n")
PY

if [ "$build_kind" = incremental-module-overlay ]; then
  write_packaged_artifacts "$stage/wine"
  verify_packaged_artifacts "$stage/wine"
fi

# Canonical trusted inventory is shared by every runtime producer.
/usr/bin/python3 "$repo_dir/scripts/write-wine-runtime-manifest.py" \
  "$stage/wine" "$runtime_id" "wine-$wine_version_field" >/dev/null
runtime_manifest="$stage/wine/yaagl-wine-runtime-files.json"
runtime_manifest_sha=$(sha256_file "$runtime_manifest")
runtime_manifest_size=$(wc -c < "$runtime_manifest" | tr -d ' ')

COPYFILE_DISABLE=1 XZ_OPT='-T0 -3' tar -C "$stage" -cJf "$archive" wine

archive_verify="$output_dir/.archive-verify.$$"
archive_members="$output_dir/.archive-members.$$"
[ ! -e "$archive_verify" ] || die "temporary archive verification directory already exists: $archive_verify"
[ ! -e "$archive_members" ] || die "temporary archive member list already exists: $archive_members"
cleanup_archive_verify() { /bin/rm -rf "$archive_verify" "$archive_members"; }
trap cleanup_archive_verify EXIT HUP INT TERM
/bin/mkdir -p "$archive_verify"
printf '%s\n' \
  wine/share/yaagl/steam/steam64.exe \
  wine/share/yaagl/steam/steam32.exe \
  wine/share/yaagl/steam/lsteamclient64.dll \
  wine/share/yaagl/steam/lsteamclient32.dll \
  wine/bin/wine \
  wine/bin/wine.real \
  wine/bin/wineserver \
  wine/yaagl-wine-p3-graphics-artifacts.json \
  wine/yaagl-wine-p3-provenance.json \
  wine/yaagl-wine-runtime-files.json \
  "wine/$D3DMETAL_BINARY_REL" \
  "wine/$PSO_CACHE_MODULE_REL" \
  "wine/$METALIR_CONVERTER_REL" > "$archive_members"
if [ "$build_kind" = incremental-module-overlay ]; then
  printf '%s\n' "$TUNED_ARTIFACT_IDENTITIES" \
    | while IFS='|' read -r rel arch format; do printf 'wine/%s\n' "$rel"; done \
    >> "$archive_members"
fi
tar -xJf "$archive" -C "$archive_verify" -T "$archive_members"
for helper_name in steam64.exe steam32.exe lsteamclient64.dll lsteamclient32.dll; do
  cmp -s "$steam_helper_dir/$helper_name" "$archive_verify/wine/share/yaagl/steam/$helper_name" \
    || die "archived Steam helper missing or differs from source: $helper_name"
done
for authenticated_path in \
  bin/wine bin/wine.real bin/wineserver \
  yaagl-wine-p3-provenance.json yaagl-wine-runtime-files.json; do
  cmp -s "$stage/wine/$authenticated_path" "$archive_verify/wine/$authenticated_path" \
    || die "archived authenticated runtime artifact differs from final stage: $authenticated_path"
done
if [ "$build_kind" = incremental-module-overlay ]; then
  verify_packaged_artifacts "$archive_verify/wine"
fi
verify_graphics_artifacts "$archive_verify/wine"
cleanup_archive_verify
trap - EXIT HUP INT TERM
shasum -a 256 "$archive" | tee "$archive.sha256"
du -h "$archive"

printf 'Staged: %s\nArchive: %s\nRuntime manifest SHA-256: %s\nRuntime manifest size: %s\n' \
  "$stage/wine" "$archive" "$runtime_manifest_sha" "$runtime_manifest_size"
