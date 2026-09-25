#!/bin/sh
# Incremental Wine 11.17 ZZZ DX12 overlay builds.
# Reuses the verified P3 payload, rebuilding only the closed artifact inventory
# below with the x86_64 WoW64 and arm64-server configure trees.
set -eu

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
WINE_BUILD_PROFILE=${WINE_BUILD_PROFILE:-tuned}
case "$WINE_BUILD_PROFILE" in
  tuned)
    BUILD_LABEL=build-wine-tuned
    ROOT="$REPO_DIR/build/wine-tuned"
    PROVENANCE_NAME=wine-11.17-git.913e31f-zzz-dx12-tuned-gptk4b2
    RUNTIME_ID=11.17-zzz-dx12-tuned
    PACKAGE_COMMAND=scripts/package-wine-p3-runtime.sh
    ;;
  safe-msync)
    BUILD_LABEL=build-wine-safe-msync
    ROOT="$REPO_DIR/build/wine-safe-msync"
    PROVENANCE_NAME=wine-11.17-git.913e31f-zzz-dx12-p3-safe-msync-gptk4b2
    RUNTIME_ID=11.17-p3-safe-msync
    PACKAGE_COMMAND=scripts/package-wine-safe-msync-runtime.sh
    ;;
  *)
    echo "build-wine-tuned: unknown WINE_BUILD_PROFILE: $WINE_BUILD_PROFILE" >&2
    exit 2
    ;;
esac
PROVENANCE_NAME=${WINE_PACKAGE_NAME:-$PROVENANCE_NAME}
RUNTIME_ID=${WINE_RUNTIME_ID:-$RUNTIME_ID}
SOURCE_DIR="$ROOT/source"
BUILD_X64="$ROOT/build-x64"
BUILD_ARM64="$ROOT/build-arm64"
HOST_DIR="$ROOT/host"
PREPARED_FILE="$ROOT/prepared.json"
BUILD_STATE_FILE="$ROOT/build-state.json"
CONFIG_X64_STATE_FILE="$ROOT/configure-x64.state.json"
CONFIG_ARM64_STATE_FILE="$ROOT/configure-arm64.state.json"
PROVENANCE_FILE="$ROOT/provenance.json"
ROOT_REL=${ROOT#"$REPO_DIR/"}
BASE_ROOT=${WINE_P3_ROOT:-"$REPO_DIR/build/wine-p3"}
BASE_SOURCE="$BASE_ROOT/source"
BASE_HOST="$BASE_ROOT/host"
BASE_PROVENANCE="$BASE_ROOT/provenance.json"
BASE_DEPS_ENV="$BASE_ROOT/deps/env.sh"
PATCH_DIR="$REPO_DIR/patches/wine-tuned"
SOURCE_PIN=913e31f201d344223bdf3d13a50a41af35893d12
INSPECTED_UPSTREAM_TIP=788d90c4e1d628fab6672623f0c8094b984ea2fa
if [ "$WINE_BUILD_PROFILE" = tuned ]; then
  BACKPORTED_UPSTREAM_COMMITS="91fabe0d1dc0f766df0c54809b0b2811f28bcf1d 5960f8049bb185b1a789b4ee8efaedbe1feb8b6f 5b7db73f174be3ef98fd80e2a7ccf608f357fb7b f360ce3e9bf650bc41abf0cf26d420b2180add93 2f73d9efc2a060b56cde465e3d3cac523823ef5d 6850fba76d365e8855b5a81432d32781693cce2f 0e36c06949adbd1a409990fce171060968c6935b 7c48881f4fe2edba88f70b2e1b0892d3d98c8b19 d119a0c94a0a9cdabc42b28c607584e357295413"
else
  BACKPORTED_UPSTREAM_COMMITS=
fi
WINE_VERSION=11.17
MINGW_ROOT=/opt/llvm-mingw-20260616-ucrt-macos-universal
MINGW_CLANG="$MINGW_ROOT/bin/clang"
PKG_CONFIG_BIN=/opt/homebrew/bin/pkg-config
JOBS=$(/usr/sbin/sysctl -n hw.ncpu 2>/dev/null || echo 4)

# patch basename, in application order
PATCH_INVENTORY='0001-msync-tuned.patch
0002-native-x86-server.patch
0003-msync-reliability.patch
0004-macdrv-reset-rawinput-baseline.patch
0005-upstream-media-fixes.patch
0006-upstream-macos-fixes.patch
0007-msync-resource-reuse.patch
0008-media-resources.patch
0009-device-resources.patch
0010-network-resources.patch
0011-audio-resources.patch
0012-msync-shared-pages.patch
0013-window-resources.patch
0014-core-resources.patch
0015-msync-owned-exports.patch
0016-cursor-diagnostics.patch'

# key|build tree|make target|installed path|architecture|format|changed sources (comma-separated)
# This is the single authoritative source/artifact inventory. Source exclusions,
# build state, install overlay, and provenance are all derived from it.
ARTIFACT_INVENTORY='ntdll|x86_64|dlls/ntdll/ntdll.so|lib/wine/x86_64-unix/ntdll.so|x86_64|macho|dlls/ntdll/unix/msync.c,dlls/ntdll/unix/msync.h,dlls/ntdll/unix/loader.c,server/protocol.def,include/wine/server_protocol.h,include/wine/msync.h,include/Makefile.in,dlls/ntdll/unix/signal_arm.c,dlls/ntdll/unix/signal_arm64.c,dlls/ntdll/unix/signal_i386.c,dlls/ntdll/unix/signal_x86_64.c,dlls/ntdll/unix/thread.c,dlls/ntdll/unix/unix_private.h,dlls/ntdll/unix/sync.c
winemac|x86_64|dlls/winemac.drv/winemac.so|lib/wine/x86_64-unix/winemac.so|x86_64|macho|dlls/winemac.drv/cocoa_app.h,dlls/winemac.drv/cocoa_app.m,dlls/winemac.drv/cocoa_event.h,dlls/winemac.drv/cocoa_event.m,dlls/winemac.drv/cocoa_window.h,dlls/winemac.drv/cocoa_window.m,dlls/winemac.drv/macdrv.h,dlls/winemac.drv/macdrv_cocoa.h,dlls/winemac.drv/event.c,dlls/winemac.drv/mouse.c,dlls/winemac.drv/surface.c,dlls/winemac.drv/window.c,include/wine/gdi_driver.h,dlls/winemac.drv/macdrv_main.c,dlls/winemac.drv/cocoa_cursorclipping.m,include/wine/cursor_trace.h
winemac64|x86_64|dlls/winemac.drv/x86_64-windows/winemac.drv|lib/wine/x86_64-windows/winemac.drv|x86_64|pe|dlls/winemac.drv/cocoa_app.h,dlls/winemac.drv/cocoa_app.m,dlls/winemac.drv/cocoa_event.h,dlls/winemac.drv/cocoa_event.m,dlls/winemac.drv/cocoa_window.h,dlls/winemac.drv/cocoa_window.m,dlls/winemac.drv/surface.c,include/wine/gdi_driver.h
winemac32|x86_64|dlls/winemac.drv/i386-windows/winemac.drv|lib/wine/i386-windows/winemac.drv|i386|pe|dlls/winemac.drv/cocoa_app.h,dlls/winemac.drv/cocoa_app.m,dlls/winemac.drv/cocoa_event.h,dlls/winemac.drv/cocoa_event.m,dlls/winemac.drv/cocoa_window.h,dlls/winemac.drv/cocoa_window.m,dlls/winemac.drv/surface.c,include/wine/gdi_driver.h
wineserver|arm64|server/wineserver|bin/wineserver|arm64|macho|server/msync.c,server/msync.h,server/main.c,include/wine/msync.h,include/Makefile.in,server/thread.c,server/thread.h,server/request.c,server/sock.c,server/mach.c,server/registry.c,server/inproc_sync.c,server/queue.c,server/request_handlers.h,server/request_trace.h,server/user.h,server/window.c,server/protocol.def,include/wine/server_protocol.h,server/process.c,server/process.h
win32u|x86_64|dlls/win32u/win32u.so|lib/wine/x86_64-unix/win32u.so|x86_64|macho|dlls/win32u/opengl.c,dlls/win32u/dce.c,dlls/win32u/message.c,server/protocol.def,include/wine/server_protocol.h,include/wine/gdi_driver.h,dlls/win32u/input.c,dlls/win32u/rawinput.c,include/wine/cursor_trace.h
win32u64|x86_64|dlls/win32u/x86_64-windows/win32u.dll|lib/wine/x86_64-windows/win32u.dll|x86_64|pe|dlls/win32u/dce.c,dlls/win32u/message.c,include/wine/gdi_driver.h
win32u32|x86_64|dlls/win32u/i386-windows/win32u.dll|lib/wine/i386-windows/win32u.dll|i386|pe|dlls/win32u/dce.c,dlls/win32u/message.c,include/wine/gdi_driver.h
winegstreamer|x86_64|dlls/winegstreamer/winegstreamer.so|lib/wine/x86_64-unix/winegstreamer.so|x86_64|macho|dlls/winegstreamer/wg_parser.c,dlls/winegstreamer/wg_transform.c,dlls/winegstreamer/media_sink.c,dlls/winegstreamer/gst_private.h,dlls/winegstreamer/main.c,dlls/winegstreamer/media_source.c,dlls/winegstreamer/quartz_parser.c,dlls/winegstreamer/wm_reader.c
winegstreamer64|x86_64|dlls/winegstreamer/x86_64-windows/winegstreamer.dll|lib/wine/x86_64-windows/winegstreamer.dll|x86_64|pe|dlls/winegstreamer/wg_parser.c,dlls/winegstreamer/wg_transform.c,dlls/winegstreamer/media_sink.c,dlls/winegstreamer/gst_private.h,dlls/winegstreamer/main.c,dlls/winegstreamer/media_source.c,dlls/winegstreamer/quartz_parser.c,dlls/winegstreamer/wm_reader.c
winegstreamer32|x86_64|dlls/winegstreamer/i386-windows/winegstreamer.dll|lib/wine/i386-windows/winegstreamer.dll|i386|pe|dlls/winegstreamer/wg_parser.c,dlls/winegstreamer/wg_transform.c,dlls/winegstreamer/media_sink.c,dlls/winegstreamer/gst_private.h,dlls/winegstreamer/main.c,dlls/winegstreamer/media_source.c,dlls/winegstreamer/quartz_parser.c,dlls/winegstreamer/wm_reader.c
resampledmo64|x86_64|dlls/resampledmo/x86_64-windows/resampledmo.dll|lib/wine/x86_64-windows/resampledmo.dll|x86_64|pe|dlls/resampledmo/resampler.c
resampledmo32|x86_64|dlls/resampledmo/i386-windows/resampledmo.dll|lib/wine/i386-windows/resampledmo.dll|i386|pe|dlls/resampledmo/resampler.c
mfreadwrite64|x86_64|dlls/mfreadwrite/x86_64-windows/mfreadwrite.dll|lib/wine/x86_64-windows/mfreadwrite.dll|x86_64|pe|dlls/mfreadwrite/reader.c,dlls/mfreadwrite/writer.c
mfreadwrite32|x86_64|dlls/mfreadwrite/i386-windows/mfreadwrite.dll|lib/wine/i386-windows/mfreadwrite.dll|i386|pe|dlls/mfreadwrite/reader.c,dlls/mfreadwrite/writer.c
amstream64|x86_64|dlls/amstream/x86_64-windows/amstream.dll|lib/wine/x86_64-windows/amstream.dll|x86_64|pe|dlls/amstream/ddrawstream.c
amstream32|x86_64|dlls/amstream/i386-windows/amstream.dll|lib/wine/i386-windows/amstream.dll|i386|pe|dlls/amstream/ddrawstream.c
imm3264|x86_64|dlls/imm32/x86_64-windows/imm32.dll|lib/wine/x86_64-windows/imm32.dll|x86_64|pe|dlls/imm32/Makefile.in
imm3232|x86_64|dlls/imm32/i386-windows/imm32.dll|lib/wine/i386-windows/imm32.dll|i386|pe|dlls/imm32/Makefile.in
winecoreaudio|x86_64|dlls/winecoreaudio.drv/winecoreaudio.so|lib/wine/x86_64-unix/winecoreaudio.so|x86_64|macho|dlls/winecoreaudio.drv/coreaudio.c
winebus|x86_64|dlls/winebus.sys/winebus.so|lib/wine/x86_64-unix/winebus.so|x86_64|macho|dlls/winebus.sys/main.c,dlls/winebus.sys/bus_iohid.c,dlls/winebus.sys/bus_sdl.c,dlls/winebus.sys/hid.c
winebus64|x86_64|dlls/winebus.sys/x86_64-windows/winebus.sys|lib/wine/x86_64-windows/winebus.sys|x86_64|pe|dlls/winebus.sys/main.c,dlls/winebus.sys/bus_iohid.c,dlls/winebus.sys/bus_sdl.c,dlls/winebus.sys/hid.c
winebus32|x86_64|dlls/winebus.sys/i386-windows/winebus.sys|lib/wine/i386-windows/winebus.sys|i386|pe|dlls/winebus.sys/main.c,dlls/winebus.sys/bus_iohid.c,dlls/winebus.sys/bus_sdl.c,dlls/winebus.sys/hid.c
hidclass64|x86_64|dlls/hidclass.sys/x86_64-windows/hidclass.sys|lib/wine/x86_64-windows/hidclass.sys|x86_64|pe|dlls/hidclass.sys/pnp.c
hidclass32|x86_64|dlls/hidclass.sys/i386-windows/hidclass.sys|lib/wine/i386-windows/hidclass.sys|i386|pe|dlls/hidclass.sys/pnp.c
dinput64|x86_64|dlls/dinput/x86_64-windows/dinput.dll|lib/wine/x86_64-windows/dinput.dll|x86_64|pe|dlls/dinput/joystick_hid.c
dinput32|x86_64|dlls/dinput/i386-windows/dinput.dll|lib/wine/i386-windows/dinput.dll|i386|pe|dlls/dinput/joystick_hid.c
dinput864|x86_64|dlls/dinput8/x86_64-windows/dinput8.dll|lib/wine/x86_64-windows/dinput8.dll|x86_64|pe|dlls/dinput/joystick_hid.c
dinput832|x86_64|dlls/dinput8/i386-windows/dinput8.dll|lib/wine/i386-windows/dinput8.dll|i386|pe|dlls/dinput/joystick_hid.c
xinput1164|x86_64|dlls/xinput1_1/x86_64-windows/xinput1_1.dll|lib/wine/x86_64-windows/xinput1_1.dll|x86_64|pe|dlls/xinput1_3/main.c
xinput1132|x86_64|dlls/xinput1_1/i386-windows/xinput1_1.dll|lib/wine/i386-windows/xinput1_1.dll|i386|pe|dlls/xinput1_3/main.c
xinput1264|x86_64|dlls/xinput1_2/x86_64-windows/xinput1_2.dll|lib/wine/x86_64-windows/xinput1_2.dll|x86_64|pe|dlls/xinput1_3/main.c
xinput1232|x86_64|dlls/xinput1_2/i386-windows/xinput1_2.dll|lib/wine/i386-windows/xinput1_2.dll|i386|pe|dlls/xinput1_3/main.c
xinput1364|x86_64|dlls/xinput1_3/x86_64-windows/xinput1_3.dll|lib/wine/x86_64-windows/xinput1_3.dll|x86_64|pe|dlls/xinput1_3/main.c
xinput1332|x86_64|dlls/xinput1_3/i386-windows/xinput1_3.dll|lib/wine/i386-windows/xinput1_3.dll|i386|pe|dlls/xinput1_3/main.c
xinput1464|x86_64|dlls/xinput1_4/x86_64-windows/xinput1_4.dll|lib/wine/x86_64-windows/xinput1_4.dll|x86_64|pe|dlls/xinput1_3/main.c
xinput1432|x86_64|dlls/xinput1_4/i386-windows/xinput1_4.dll|lib/wine/i386-windows/xinput1_4.dll|i386|pe|dlls/xinput1_3/main.c
xinputuap64|x86_64|dlls/xinputuap/x86_64-windows/xinputuap.dll|lib/wine/x86_64-windows/xinputuap.dll|x86_64|pe|dlls/xinput1_3/main.c
xinputuap32|x86_64|dlls/xinputuap/i386-windows/xinputuap.dll|lib/wine/i386-windows/xinputuap.dll|i386|pe|dlls/xinput1_3/main.c
wininet64|x86_64|dlls/wininet/x86_64-windows/wininet.dll|lib/wine/x86_64-windows/wininet.dll|x86_64|pe|dlls/wininet/netconnection.c,dlls/wininet/http.c
wininet32|x86_64|dlls/wininet/i386-windows/wininet.dll|lib/wine/i386-windows/wininet.dll|i386|pe|dlls/wininet/netconnection.c,dlls/wininet/http.c
winhttp64|x86_64|dlls/winhttp/x86_64-windows/winhttp.dll|lib/wine/x86_64-windows/winhttp.dll|x86_64|pe|dlls/winhttp/net.c
winhttp32|x86_64|dlls/winhttp/i386-windows/winhttp.dll|lib/wine/i386-windows/winhttp.dll|i386|pe|dlls/winhttp/net.c
ws2_3264|x86_64|dlls/ws2_32/x86_64-windows/ws2_32.dll|lib/wine/x86_64-windows/ws2_32.dll|x86_64|pe|dlls/ws2_32/socket.c
ws2_3232|x86_64|dlls/ws2_32/i386-windows/ws2_32.dll|lib/wine/i386-windows/ws2_32.dll|i386|pe|dlls/ws2_32/socket.c'

if [ "$WINE_BUILD_PROFILE" = safe-msync ]; then
  PATCH_INVENTORY='0001-msync-tuned.patch
0002-native-x86-server.patch
0003-msync-reliability.patch
0007-msync-resource-reuse.patch
0012-msync-shared-pages.patch
0015-msync-owned-exports.patch'
  ARTIFACT_INVENTORY='ntdll|x86_64|dlls/ntdll/ntdll.so|lib/wine/x86_64-unix/ntdll.so|x86_64|macho|dlls/ntdll/unix/msync.c,dlls/ntdll/unix/msync.h,include/wine/msync.h,dlls/ntdll/unix/sync.c,dlls/ntdll/unix/thread.c,server/protocol.def,include/wine/server_protocol.h
wineserver|arm64|server/wineserver|bin/wineserver|arm64|macho|server/msync.c,server/msync.h,server/thread.c,server/inproc_sync.c,server/main.c,server/registry.c,server/mach.c,include/wine/msync.h,include/Makefile.in,server/process.c,server/process.h,server/protocol.def,include/wine/server_protocol.h,server/request_handlers.h,server/request_trace.h'
fi

usage() {
  cat <<'EOF'
Usage: scripts/build-wine-tuned.sh <action>

Actions:
  preflight  Check the immutable P3 inputs and local toolchains
  prepare    Create/verify P3 source copy and apply/validate profile patch overlay
  configure  Configure isolated x86_64 and arm64 build directories
  build      Incrementally build the closed runtime artifact inventory
  install    Copy P3 host and overlay every rebuilt artifact
  all        prepare + configure + build + install

Profile: selected by WINE_BUILD_PROFILE (tuned or safe-msync; default tuned).
Environment: WINE_P3_ROOT selects prepared baseline inputs (default build/wine-p3).
MACOSX_DEPLOYMENT_TARGET defaults to 26.0; SDKROOT selects the installed SDK.
WINE_PACKAGE_NAME and WINE_RUNTIME_ID override the profile package metadata.
The driver never writes build/wine-p3. It intentionally does not perform a full
Wine rebuild: unchanged PE and Unix files are inherited byte-for-byte from
build/wine-p3/host. Changed x86_64/i386 PE DLLs use the existing x86_64 WoW64
configure; the arm64 configure uses --enable-archs=none and builds only wineserver. WINE_TUNED_X86_SERVER is defined only for
the arm64 native build.
EOF
}

die() {
  echo "$BUILD_LABEL: $*" >&2
  exit 1
}

info() {
  echo "$BUILD_LABEL: $*"
}

require_file() {
  [ -f "$1" ] || die "missing required file: $1"
}

require_exec() {
  [ -x "$1" ] || die "missing required executable: $1"
}

sha256_file() {
  /usr/bin/shasum -a 256 "$1" | /usr/bin/awk '{print $1}'
}

patch_inventory() {
  printf '%s\n' "$PATCH_INVENTORY"
}

artifact_inventory() {
  printf '%s\n' "$ARTIFACT_INVENTORY"
}

source_inventory() {
  ARTIFACT_INVENTORY_VALUE=$ARTIFACT_INVENTORY /usr/bin/python3 - <<'PY'
import os
sources = set()
for line in os.environ["ARTIFACT_INVENTORY_VALUE"].splitlines():
    sources.update(line.split("|", 6)[6].split(","))
print("\n".join(sorted(sources)))
PY
}

validate_pe_machine() {
  target=$1
  expected=$2
  /usr/bin/python3 - "$target" "$expected" <<'PY' \
    || die "invalid PE machine: $target (expected $expected)"
import pathlib, struct, sys
data = pathlib.Path(sys.argv[1]).read_bytes()
expected = int(sys.argv[2], 0)
if len(data) < 0x40 or data[:2] != b"MZ": raise SystemExit(1)
offset = struct.unpack_from("<I", data, 0x3c)[0]
if offset + 6 > len(data) or data[offset:offset + 4] != b"PE\0\0": raise SystemExit(1)
if struct.unpack_from("<H", data, offset + 4)[0] != expected: raise SystemExit(1)
PY
}

json_get() {
  /usr/bin/python3 - "$1" "$2" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
for key in sys.argv[2].split("."):
    value = value.get(key) if isinstance(value, dict) else None
    if value is None:
        break
if value is not None:
    print(value)
PY
}

# Stable digest of files and symlinks. Directory mtimes, .git metadata, and the
# explicitly rebuilt source inputs are excluded.
tree_digest() {
  ARTIFACT_INVENTORY_VALUE=$ARTIFACT_INVENTORY /usr/bin/python3 - "$1" <<'PY'
import hashlib, os, stat, sys
root = os.path.realpath(sys.argv[1])
excluded = set()
for line in os.environ["ARTIFACT_INVENTORY_VALUE"].splitlines():
    excluded.update(line.split("|", 6)[6].split(","))
digest = hashlib.sha256()
for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
    rel_dir = os.path.relpath(current, root)
    symlink_dirs = [name for name in dirs if os.path.islink(os.path.join(current, name))]
    dirs[:] = sorted(name for name in dirs if name not in symlink_dirs and name != ".git" and
                     os.path.join(rel_dir, name).removeprefix("./") not in excluded)
    for name in sorted(files + symlink_dirs):
        path = os.path.join(current, name)
        rel = os.path.relpath(path, root)
        if rel in excluded or rel == ".git" or rel.startswith(".git" + os.sep):
            continue
        mode = os.lstat(path).st_mode
        digest.update(rel.encode("utf-8", "surrogateescape") + b"\0")
        digest.update(f"{stat.S_IMODE(mode):04o}".encode() + b"\0")
        if stat.S_ISLNK(mode):
            digest.update(b"L\0" + os.readlink(path).encode("utf-8", "surrogateescape") + b"\0")
        elif stat.S_ISREG(mode):
            digest.update(b"F\0")
            with open(path, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
        else:
            raise SystemExit(f"unsupported source-tree entry: {path}")
print(digest.hexdigest())
PY
}

complete_source_tree_digest() {
  /usr/bin/python3 - "$1" <<'PY'
import hashlib, os, stat, sys
root = os.path.realpath(sys.argv[1])
digest = hashlib.sha256()
for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
    symlink_dirs = [name for name in dirs if os.path.islink(os.path.join(current, name))]
    dirs[:] = sorted(name for name in dirs if name not in symlink_dirs and name != ".git")
    for name in sorted(files + symlink_dirs):
        path = os.path.join(current, name)
        rel = os.path.relpath(path, root)
        if rel == ".git" or rel.startswith(".git" + os.sep):
            continue
        mode = os.lstat(path).st_mode
        digest.update(rel.encode("utf-8", "surrogateescape") + b"\0")
        digest.update(f"{stat.S_IMODE(mode):04o}".encode() + b"\0")
        if stat.S_ISLNK(mode):
            digest.update(b"L\0" + os.readlink(path).encode("utf-8", "surrogateescape") + b"\0")
        elif stat.S_ISREG(mode):
            digest.update(b"F\0")
            with open(path, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
        else:
            raise SystemExit(f"unsupported source-tree entry: {path}")
print(digest.hexdigest())
PY
}

host_tree_digest() {
  ARTIFACT_INVENTORY_VALUE=$ARTIFACT_INVENTORY /usr/bin/python3 - "$1" <<'PY'
import hashlib, os, stat, sys
root = os.path.realpath(sys.argv[1])
excluded = {line.split("|", 6)[3] for line in os.environ["ARTIFACT_INVENTORY_VALUE"].splitlines()}
digest = hashlib.sha256()
for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
    symlink_dirs = [name for name in dirs if os.path.islink(os.path.join(current, name))]
    dirs[:] = sorted(name for name in dirs if name not in symlink_dirs)
    for name in sorted(files + symlink_dirs):
        path = os.path.join(current, name)
        rel = os.path.relpath(path, root)
        if rel in excluded:
            continue
        mode = os.lstat(path).st_mode
        digest.update(rel.encode("utf-8", "surrogateescape") + b"\0")
        digest.update(f"{stat.S_IMODE(mode):04o}".encode() + b"\0")
        if stat.S_ISLNK(mode):
            digest.update(b"L\0" + os.readlink(path).encode("utf-8", "surrogateescape") + b"\0")
        elif stat.S_ISREG(mode):
            digest.update(b"F\0")
            with open(path, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
        else:
            raise SystemExit(f"unsupported host-tree entry: {path}")
print(digest.hexdigest())
PY
}

create_source_copy_if_missing() {
  [ ! -e "$SOURCE_DIR" ] || return 0
  require_file "$BASE_PROVENANCE"
  [ -d "$BASE_SOURCE/.git" ] || die "baseline source lacks Git metadata: $BASE_SOURCE/.git"
  tmp_source="$ROOT/.source.tmp.$$"
  [ ! -e "$tmp_source" ] || die "temporary source already exists: $tmp_source"
  cleanup_source() { /bin/rm -rf "$tmp_source"; }
  trap cleanup_source EXIT HUP INT TERM
  /bin/mkdir -p "$ROOT"
  /usr/bin/ditto "$BASE_SOURCE" "$tmp_source"
  /bin/rm -rf "$tmp_source/.git"
  /bin/mv "$tmp_source" "$SOURCE_DIR"
  trap - EXIT HUP INT TERM
  info "created $WINE_BUILD_PROFILE source from prepared P3 without Git metadata"
}

apply_or_validate_patch_stack() {
  # Build the only valid patched state away from SOURCE_DIR, then compare whole
  # trees so an earlier patch cannot be mistaken for applied after a later one.
  expected_source="$ROOT/.source.expected.$$"
  previous_source="$ROOT/.source.previous.$$"
  [ ! -e "$expected_source" ] || die "temporary source already exists: $expected_source"
  [ ! -e "$previous_source" ] || die "temporary source already exists: $previous_source"

  cleanup_patch_stack() {
    /bin/rm -rf "$expected_source"
    if [ -e "$previous_source" ]; then
      if [ ! -e "$SOURCE_DIR" ]; then
        /bin/mv "$previous_source" "$SOURCE_DIR" || true
      else
        /bin/rm -rf "$previous_source"
      fi
    fi
  }
  trap cleanup_patch_stack EXIT HUP INT TERM

  /usr/bin/ditto "$BASE_SOURCE" "$expected_source"
  /bin/rm -rf "$expected_source/.git"
  patch_inventory | while IFS= read -r patch_name; do
    /usr/bin/patch --batch --forward -V none -d "$expected_source" -p1 < "$PATCH_DIR/$patch_name" \
      || die "failed to construct ordered $WINE_BUILD_PROFILE overlay at $patch_name"
  done

  source_digest=$(complete_source_tree_digest "$SOURCE_DIR")
  expected_digest=$(complete_source_tree_digest "$expected_source")
  if [ "$source_digest" = "$expected_digest" ]; then
    /bin/rm -rf "$expected_source"
    trap - EXIT HUP INT TERM
    info "complete ordered $WINE_BUILD_PROFILE patch overlay already applied"
    return 0
  fi

  baseline_digest=$(complete_source_tree_digest "$BASE_SOURCE")
  [ "$source_digest" = "$baseline_digest" ] \
    || die "source is neither pristine nor the complete ordered $WINE_BUILD_PROFILE overlay (source left untouched)"

  /bin/mv "$SOURCE_DIR" "$previous_source"
  /bin/mv "$expected_source" "$SOURCE_DIR" \
    || die "failed to install complete ordered $WINE_BUILD_PROFILE overlay"
  /bin/rm -rf "$previous_source"
  trap - EXIT HUP INT TERM
  info "applied complete ordered $WINE_BUILD_PROFILE patch overlay"
}

require_profile_source_inventory() {
  require_file "$SOURCE_DIR/configure"
  source_inventory | while IFS= read -r source; do require_file "$SOURCE_DIR/$source"; done
}

cmd_preflight() {
  require_exec /usr/bin/arch
  require_exec /usr/bin/clang
  require_exec /usr/bin/clang++
  require_exec /usr/bin/ditto
  require_exec /usr/bin/git
  require_exec /usr/bin/lipo
  require_exec /usr/bin/make
  require_exec /usr/bin/patch
  require_exec /usr/bin/python3
  require_exec /usr/bin/shasum
  require_exec /usr/bin/xcrun
  require_exec "$MINGW_CLANG"
  require_exec "$PKG_CONFIG_BIN"
  require_file "$BASE_SOURCE/configure"
  require_file "$BASE_PROVENANCE"
  require_file "$BASE_DEPS_ENV"
  patch_inventory | while IFS= read -r patch_name; do require_file "$PATCH_DIR/$patch_name"; done
  require_exec "$BASE_HOST/bin/wine"
  require_exec "$BASE_HOST/bin/wineserver"
  artifact_inventory | while IFS='|' read -r key build target installed arch format sources; do
    require_file "$BASE_HOST/$installed"
  done
  [ -d "$BASE_SOURCE/.git" ] || die "baseline source lacks Git metadata: $BASE_SOURCE/.git"
  [ "$(/usr/bin/git -C "$BASE_SOURCE" rev-parse HEAD)" = "$SOURCE_PIN" ] \
    || die "baseline source is not pinned to $SOURCE_PIN"
  [ "$(json_get "$BASE_PROVENANCE" sourcePin)" = "$SOURCE_PIN" ] \
    || die "baseline provenance sourcePin is not $SOURCE_PIN"
  [ "$(json_get "$BASE_PROVENANCE" wineVersion)" = "$WINE_VERSION" ] \
    || die "baseline provenance wineVersion is not $WINE_VERSION"
  /usr/bin/lipo -verify_arch x86_64 "$BASE_HOST/bin/wineserver" >/dev/null \
    || die "baseline wineserver lacks x86_64"
  /usr/bin/lipo -verify_arch x86_64 "$BASE_HOST/lib/wine/x86_64-unix/ntdll.so" >/dev/null \
    || die "baseline ntdll.so lacks x86_64"
  /usr/bin/lipo -verify_arch x86_64 "$BASE_HOST/lib/wine/x86_64-unix/winemac.so" >/dev/null \
    || die "baseline winemac.so lacks x86_64"
  artifact_inventory | while IFS='|' read -r key build target installed arch format sources; do
    case "$format:$arch" in
      macho:x86_64) /usr/bin/lipo -verify_arch x86_64 "$BASE_HOST/$installed" >/dev/null || die "baseline $installed lacks x86_64" ;;
      pe:x86_64) validate_pe_machine "$BASE_HOST/$installed" 0x8664 ;;
      pe:i386) validate_pe_machine "$BASE_HOST/$installed" 0x014c ;;
    esac
  done
  info "preflight OK"
}

write_prepared_file() {
  base_source_digest=$1
  base_provenance_sha=$2
  tmp="$PREPARED_FILE.tmp.$$"
  BASE_SOURCE_DIGEST=$base_source_digest BASE_PROVENANCE_SHA=$base_provenance_sha \
  ARTIFACT_INVENTORY_VALUE=$ARTIFACT_INVENTORY PATCH_INVENTORY_VALUE=$PATCH_INVENTORY \
  SOURCE_DIR_VALUE=$SOURCE_DIR BASE_SOURCE_VALUE=$BASE_SOURCE BASE_PROVENANCE_VALUE=$BASE_PROVENANCE \
  PATCH_DIR_VALUE=$PATCH_DIR BUILD_PROFILE_VALUE=$WINE_BUILD_PROFILE /usr/bin/python3 - "$tmp" <<'PY'
import hashlib, json, os, pathlib, sys

def sha(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()

sources = set()
for line in os.environ["ARTIFACT_INVENTORY_VALUE"].splitlines():
    sources.update(line.split("|", 6)[6].split(","))
source_dir = pathlib.Path(os.environ["SOURCE_DIR_VALUE"])
patch_dir = pathlib.Path(os.environ["PATCH_DIR_VALUE"])
payload = {
    "schemaVersion": 2,
    "wineVersion": "11.17",
    "sourcePin": "913e31f201d344223bdf3d13a50a41af35893d12",
    "buildKind": "incremental-module-overlay",
    "profile": os.environ["BUILD_PROFILE_VALUE"],
    "sourceDir": str(source_dir),
    "baselineSourceDir": os.environ["BASE_SOURCE_VALUE"],
    "baselineProvenance": os.environ["BASE_PROVENANCE_VALUE"],
    "baselineProvenanceSha256": os.environ["BASE_PROVENANCE_SHA"],
    "inheritedSourceTreeSha256": os.environ["BASE_SOURCE_DIGEST"],
    "overlayPatches": [
        {"file": name, "sha256": sha(patch_dir / name)}
        for name in os.environ["PATCH_INVENTORY_VALUE"].splitlines()
    ],
    "excludedRebuiltSourceInputs": [
        {"path": path, "sha256": sha(source_dir / path)} for path in sorted(sources)
    ],
}
with open(sys.argv[1], "w", encoding="utf-8") as stream:
    json.dump(payload, stream, indent=2)
    stream.write("\n")
PY
  /bin/mv "$tmp" "$PREPARED_FILE"
}

cmd_prepare() {
  create_source_copy_if_missing
  cmd_preflight
  apply_or_validate_patch_stack
  require_profile_source_inventory
  base_digest=$(tree_digest "$BASE_SOURCE")
  profile_digest=$(tree_digest "$SOURCE_DIR")
  [ "$base_digest" = "$profile_digest" ] \
    || die "$WINE_BUILD_PROFILE source differs from P3 outside the declared rebuilt source inputs"
  source_inventory | while IFS= read -r rel; do
    if /usr/bin/cmp -s "$BASE_SOURCE/$rel" "$SOURCE_DIR/$rel"; then
      die "required $WINE_BUILD_PROFILE source input is unchanged from P3: $rel"
    fi
  done
  write_prepared_file "$base_digest" "$(sha256_file "$BASE_PROVENANCE")"
  info "prepared source inheritance recorded in $PREPARED_FILE"
}

require_prepared() {
  require_profile_source_inventory
  require_file "$PREPARED_FILE"
  expected_base=$(json_get "$PREPARED_FILE" inheritedSourceTreeSha256)
  [ "$(tree_digest "$SOURCE_DIR")" = "$expected_base" ] \
    || die "prepared source inheritance is stale; run prepare again"
  [ "$(tree_digest "$BASE_SOURCE")" = "$expected_base" ] \
    || die "baseline source changed since prepare"
  [ "$(sha256_file "$BASE_PROVENANCE")" = "$(json_get "$PREPARED_FILE" baselineProvenanceSha256)" ] \
    || die "baseline provenance changed since prepare"
  ARTIFACT_INVENTORY_VALUE=$ARTIFACT_INVENTORY PATCH_INVENTORY_VALUE=$PATCH_INVENTORY \
  PATCH_DIR_VALUE=$PATCH_DIR /usr/bin/python3 - "$PREPARED_FILE" "$SOURCE_DIR" <<'PY'
import hashlib, json, os, pathlib, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
if payload.get("schemaVersion") != 2:
    raise SystemExit("prepared inventory schema is stale; run prepare again")
for item in payload["excludedRebuiltSourceInputs"]:
    digest = hashlib.sha256(pathlib.Path(sys.argv[2], item["path"]).read_bytes()).hexdigest()
    if digest != item["sha256"]:
        raise SystemExit(f"prepared profile input is stale: {item['path']}; run prepare again")
expected_sources = set()
for line in os.environ["ARTIFACT_INVENTORY_VALUE"].splitlines():
    expected_sources.update(line.split("|", 6)[6].split(","))
if [item["path"] for item in payload["excludedRebuiltSourceInputs"]] != sorted(expected_sources):
    raise SystemExit("prepared source inventory mismatch; run prepare again")
patches = os.environ["PATCH_INVENTORY_VALUE"].splitlines()
if [item.get("file") for item in payload["overlayPatches"]] != patches:
    raise SystemExit("prepared overlay patch inventory mismatch; run prepare again")
for item in payload["overlayPatches"]:
    path = pathlib.Path(os.environ["PATCH_DIR_VALUE"], item["file"])
    if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
        raise SystemExit(f"prepared overlay patch is stale: {path}; run prepare again")
PY
}

append_pc_dir() {
  if [ -d "$1" ]; then
    if [ -n "$PC_PATHS" ]; then PC_PATHS="$PC_PATHS:$1"; else PC_PATHS=$1; fi
  fi
}

setup_x64_env() {
  # Match the verified P3 dependency/toolchain environment, while configuring
  # only inside the selected profile's build-x64. Clear the arm64 phase first so an
  # `all` invocation cannot leak native flags into this configure or build.
  unset CC CXX CFLAGS CXXFLAGS CROSSCFLAGS CROSSLDFLAGS CPPFLAGS LDFLAGS \
    PKG_CONFIG PKG_CONFIG_PATH PKG_CONFIG_LIBDIR PKG_CONFIG_SYSROOT_DIR \
    GSTREAMER_CFLAGS GSTREAMER_LIBS FFMPEG_CFLAGS FFMPEG_LIBS 2>/dev/null || true
  # shellcheck disable=SC1090
  . "$BASE_DEPS_ENV"
  export SDKROOT="${SDKROOT:-$(/usr/bin/xcrun --sdk macosx --show-sdk-path)}"
  export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
  export PATH="/opt/homebrew/opt/bison/bin:$MINGW_ROOT/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
  export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-26.0}"
  DEP_PREFIX="$BASE_ROOT/deps/macports/opt/local"
  if [ -d "$BASE_ROOT/deps/gstreamer/sdk/GStreamer.framework/Versions/1.0" ]; then
    GSTREAMER_ROOT="$BASE_ROOT/deps/gstreamer/sdk/GStreamer.framework/Versions/1.0"
  else
    GSTREAMER_ROOT="$BASE_ROOT/deps/gstreamer"
  fi
  export DEP_PREFIX GSTREAMER_ROOT
  PC_PATHS=
  append_pc_dir "$DEP_PREFIX/lib/pkgconfig"
  append_pc_dir "$DEP_PREFIX/share/pkgconfig"
  append_pc_dir "$GSTREAMER_ROOT/lib/pkgconfig"
  append_pc_dir "$GSTREAMER_ROOT/libdata/pkgconfig"
  export PKG_CONFIG="$PKG_CONFIG_BIN"
  export PKG_CONFIG_PATH="$PC_PATHS"
  export PKG_CONFIG_LIBDIR="$PC_PATHS"
  unset PKG_CONFIG_SYSROOT_DIR 2>/dev/null || true
  export CC="/usr/bin/clang -arch x86_64"
  export CXX="/usr/bin/clang++ -arch x86_64"
  export CFLAGS="${CFLAGS:--O2 -g} -mmacosx-version-min=$MACOSX_DEPLOYMENT_TARGET"
  export CXXFLAGS="${CXXFLAGS:--O2 -g} -mmacosx-version-min=$MACOSX_DEPLOYMENT_TARGET"
  export CROSSCFLAGS="${CROSSCFLAGS:--O2}"
  export CPPFLAGS="-I$DEP_PREFIX/include ${CPPFLAGS:-}"
  export LDFLAGS="-Wl,-headerpad_max_install_names -L$DEP_PREFIX/lib ${LDFLAGS:-} -mmacosx-version-min=$MACOSX_DEPLOYMENT_TARGET"
  if "$PKG_CONFIG_BIN" --exists gstreamer-1.0 gstreamer-video-1.0; then
    export GSTREAMER_CFLAGS="${GSTREAMER_CFLAGS:-$($PKG_CONFIG_BIN --cflags gstreamer-1.0 gstreamer-video-1.0 gstreamer-audio-1.0 gstreamer-tag-1.0)}"
    export GSTREAMER_LIBS="${GSTREAMER_LIBS:-$($PKG_CONFIG_BIN --libs gstreamer-1.0 gstreamer-video-1.0 gstreamer-audio-1.0 gstreamer-tag-1.0)}"
  fi
  if "$PKG_CONFIG_BIN" --exists libavutil libavformat libavcodec; then
    export FFMPEG_CFLAGS="${FFMPEG_CFLAGS:-$($PKG_CONFIG_BIN --cflags libavutil libavformat libavcodec)}"
    export FFMPEG_LIBS="${FFMPEG_LIBS:-$($PKG_CONFIG_BIN --libs libavutil libavformat libavcodec)}"
  fi
}

setup_arm64_env() {
  # Do not expose x86_64 MacPorts/GStreamer link flags to the native server.
  export PATH="/opt/homebrew/opt/bison/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
  export SDKROOT="${SDKROOT:-$(/usr/bin/xcrun --sdk macosx --show-sdk-path)}"
  export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
  export CC="/usr/bin/clang -arch arm64"
  export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-26.0}"
  export CXX="/usr/bin/clang++ -arch arm64"
  export CFLAGS="-O2 -g -DWINE_TUNED_X86_SERVER -mmacosx-version-min=$MACOSX_DEPLOYMENT_TARGET"
  export CXXFLAGS="-O2 -g -DWINE_TUNED_X86_SERVER -mmacosx-version-min=$MACOSX_DEPLOYMENT_TARGET"
  export CPPFLAGS=
  export LDFLAGS="-Wl,-headerpad_max_install_names -mmacosx-version-min=$MACOSX_DEPLOYMENT_TARGET"
  export PKG_CONFIG=/usr/bin/false
  export PKG_CONFIG_PATH=
  export PKG_CONFIG_LIBDIR=
  unset CROSSCFLAGS CROSSLDFLAGS GSTREAMER_CFLAGS GSTREAMER_LIBS FFMPEG_CFLAGS FFMPEG_LIBS \
    DYLD_FALLBACK_FRAMEWORK_PATH GST_PLUGIN_SYSTEM_PATH_1_0 GST_PLUGIN_SCANNER GI_TYPELIB_PATH \
    WINE_P3_MACPORTS_ROOT WINE_P3_MACPORTS_PREFIX WINE_P3_MACPORTS_PKGCONFIG \
    GSTREAMER_DEPS_ROOT GSTREAMER_FRAMEWORK_ROOT GSTREAMER_ROOT DEP_PREFIX 2>/dev/null || true
}

write_x64_args() {
  output=$1
  /usr/bin/python3 - "$BASE_PROVENANCE" "$HOST_DIR" > "$output" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
for arg in payload["configureArgs"]:
    if arg.startswith("--prefix="):
        arg = "--prefix=" + sys.argv[2]
    print(arg)
PY
}

write_arm64_args() {
  cat > "$1" <<EOF
--prefix=$HOST_DIR
--disable-tests
--enable-archs=none
--without-mingw
--without-alsa
--without-capi
--without-coreaudio
--without-cups
--without-dbus
--without-ffmpeg
--without-fontconfig
--without-freetype
--without-gettext
--without-gettextpo
--without-gnutls
--without-gphoto
--without-gssapi
--without-gstreamer
--without-inotify
--without-krb5
--without-netapi
--without-opencl
--without-opengl
--without-oss
--without-pcap
--with-pthread
--without-pcsclite
--without-pulse
--without-sane
--without-sdl
--without-udev
--without-usb
--without-v4l2
--without-vulkan
--without-wayland
--without-x
--disable-winebth_sys
EOF
}

write_configure_state() {
  wcs_build_dir=$1
  wcs_args_file=$2
  wcs_arch_name=$3
  wcs_state_file=$4
  wcs_tmp="$wcs_state_file.tmp.$$"
  ARGS_FILE_VALUE=$wcs_args_file CONFIG_STATUS_VALUE=$wcs_build_dir/config.status \
  ARCH_VALUE=$wcs_arch_name /usr/bin/python3 - "$wcs_tmp" <<'PY'
import hashlib, json, os, sys

def file_sha(path):
    with open(path, "rb") as stream:
        return hashlib.sha256(stream.read()).hexdigest()

keys = (
    "CC", "CXX", "CFLAGS", "CXXFLAGS", "CROSSCFLAGS", "CROSSLDFLAGS",
    "CPPFLAGS", "LDFLAGS", "PKG_CONFIG", "PKG_CONFIG_PATH",
    "PKG_CONFIG_LIBDIR", "PKG_CONFIG_SYSROOT_DIR", "SDKROOT",
    "DEVELOPER_DIR", "MACOSX_DEPLOYMENT_TARGET", "GSTREAMER_CFLAGS",
    "GSTREAMER_LIBS", "FFMPEG_CFLAGS", "FFMPEG_LIBS",
)
payload = {
    "schemaVersion": 1,
    "architecture": os.environ["ARCH_VALUE"],
    "configureArgsSha256": file_sha(os.environ["ARGS_FILE_VALUE"]),
    "configStatusSha256": file_sha(os.environ["CONFIG_STATUS_VALUE"]),
    "environment": {key: os.environ.get(key) for key in keys},
}
with open(sys.argv[1], "w", encoding="utf-8") as stream:
    json.dump(payload, stream, indent=2, sort_keys=True)
    stream.write("\n")
PY
  /bin/mv "$wcs_tmp" "$wcs_state_file"
}

require_configure_state() {
  rcs_build_dir=$1
  rcs_args_file=$2
  rcs_arch_name=$3
  rcs_state_file=$4
  require_file "$rcs_build_dir/Makefile"
  require_exec "$rcs_build_dir/config.status"
  require_file "$rcs_state_file"
  rcs_expected="$rcs_state_file.expected.$$"
  write_configure_state "$rcs_build_dir" "$rcs_args_file" "$rcs_arch_name" "$rcs_expected"
  if ! /usr/bin/cmp -s "$rcs_state_file" "$rcs_expected"; then
    /bin/rm -f "$rcs_expected"
    die "configured $rcs_arch_name tree does not match current arguments, environment, or config.status; remove $rcs_build_dir and rerun configure"
  fi
  /bin/rm -f "$rcs_expected"
}

run_configure() {
  build_dir=$1
  args_file=$2
  arch_name=$3
  state_file=$4
  if [ -f "$build_dir/Makefile" ]; then
    require_configure_state "$build_dir" "$args_file" "$arch_name" "$state_file"
    info "reusing verified configured $arch_name tree: $build_dir"
    return
  fi
  [ ! -e "$build_dir" ] || die "partial configure directory exists without Makefile: $build_dir"
  /bin/mkdir -p "$build_dir"
  (
    CDPATH= cd -- "$build_dir"
    set --
    while IFS= read -r arg; do
      [ -n "$arg" ] && set -- "$@" "$arg"
    done < "$args_file"
    if [ "$arch_name" = x86_64 ]; then
      /usr/bin/arch -x86_64 "$SOURCE_DIR/configure" "$@"
    else
      "$SOURCE_DIR/configure" "$@"
    fi
  ) || die "$arch_name configure failed"
  write_configure_state "$build_dir" "$args_file" "$arch_name" "$state_file"
}

cmd_configure() {
  cmd_preflight
  require_prepared
  /bin/mkdir -p "$ROOT"
  x64_args="$ROOT/configure-x64.args"
  arm64_args="$ROOT/configure-arm64.args"
  write_x64_args "$x64_args"
  write_arm64_args "$arm64_args"
  setup_x64_env
  run_configure "$BUILD_X64" "$x64_args" x86_64 "$CONFIG_X64_STATE_FILE"
  setup_arm64_env
  run_configure "$BUILD_ARM64" "$arm64_args" arm64 "$CONFIG_ARM64_STATE_FILE"
  info "isolated configure trees ready"
}

write_build_state() {
  tmp="$BUILD_STATE_FILE.tmp.$$"
  PREPARED_VALUE=$PREPARED_FILE X64_ARGS_VALUE=$ROOT/configure-x64.args \
  ARM64_ARGS_VALUE=$ROOT/configure-arm64.args X64_CONFIG_STATE_VALUE=$CONFIG_X64_STATE_FILE \
  ARM64_CONFIG_STATE_VALUE=$CONFIG_ARM64_STATE_FILE BUILD_X64_VALUE=$BUILD_X64 \
  BUILD_ARM64_VALUE=$BUILD_ARM64 ARTIFACT_INVENTORY_VALUE=$ARTIFACT_INVENTORY \
  /usr/bin/python3 - "$tmp" <<'PY'
import hashlib, json, os, pathlib, sys

def sha(path): return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
build_dirs = {"x86_64": pathlib.Path(os.environ["BUILD_X64_VALUE"]), "arm64": pathlib.Path(os.environ["BUILD_ARM64_VALUE"])}
artifacts = {}
for line in os.environ["ARTIFACT_INVENTORY_VALUE"].splitlines():
    key, build, target, installed, arch, fmt, sources = line.split("|", 6)
    artifacts[key] = {"buildTree": build, "makeTarget": target, "installedPath": installed,
                      "architecture": arch, "format": fmt, "sha256": sha(build_dirs[build] / target)}
payload = {
    "schemaVersion": 2,
    "preparedInputsSha256": sha(os.environ["PREPARED_VALUE"]),
    "configureArgsSha256": {"x86_64": sha(os.environ["X64_ARGS_VALUE"]), "arm64": sha(os.environ["ARM64_ARGS_VALUE"])},
    "configureStateSha256": {"x86_64": sha(os.environ["X64_CONFIG_STATE_VALUE"]), "arm64": sha(os.environ["ARM64_CONFIG_STATE_VALUE"])},
    "artifacts": artifacts,
}
with open(sys.argv[1], "w", encoding="utf-8") as stream:
    json.dump(payload, stream, indent=2)
    stream.write("\n")
PY
  /bin/mv "$tmp" "$BUILD_STATE_FILE"
}

require_successful_build() {
  require_file "$BUILD_STATE_FILE"
  ARTIFACT_INVENTORY_VALUE=$ARTIFACT_INVENTORY BUILD_X64_VALUE=$BUILD_X64 BUILD_ARM64_VALUE=$BUILD_ARM64 \
  /usr/bin/python3 - "$BUILD_STATE_FILE" "$PREPARED_FILE" "$ROOT/configure-x64.args" \
    "$ROOT/configure-arm64.args" "$CONFIG_X64_STATE_FILE" "$CONFIG_ARM64_STATE_FILE" <<'PY'
import hashlib, json, os, pathlib, sys

def sha(path):
    try: return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
    except OSError as error: raise SystemExit(f"successful build state is stale: {path}: {error}")
state = json.load(open(sys.argv[1], encoding="utf-8"))
if state.get("schemaVersion") != 2: raise SystemExit("successful build state schema is stale; run build again")
if state.get("preparedInputsSha256") != sha(sys.argv[2]): raise SystemExit("successful build state has stale prepared inputs; run build again")
if state.get("configureArgsSha256") != {"x86_64": sha(sys.argv[3]), "arm64": sha(sys.argv[4])}: raise SystemExit("successful build state has stale configure arguments; run build again")
if state.get("configureStateSha256") != {"x86_64": sha(sys.argv[5]), "arm64": sha(sys.argv[6])}: raise SystemExit("successful build state has stale configure state; run build again")
build_dirs = {"x86_64": pathlib.Path(os.environ["BUILD_X64_VALUE"]), "arm64": pathlib.Path(os.environ["BUILD_ARM64_VALUE"])}
expected = {}
for line in os.environ["ARTIFACT_INVENTORY_VALUE"].splitlines():
    key, build, target, installed, arch, fmt, sources = line.split("|", 6)
    expected[key] = {"buildTree": build, "makeTarget": target, "installedPath": installed,
                     "architecture": arch, "format": fmt, "sha256": sha(build_dirs[build] / target)}
if state.get("artifacts") != expected: raise SystemExit("successful build state does not match current artifact inventory or outputs; run build again")
PY
}

regenerate_configured_tree() {
  build_dir=$1
  args_file=$2
  arch_name=$3
  state_file=$4
  require_configure_state "$build_dir" "$args_file" "$arch_name" "$state_file"
  info "regenerating verified $arch_name configure outputs: $build_dir"
  (
    CDPATH= cd -- "$build_dir"
    if [ "$arch_name" = x86_64 ]; then
      /usr/bin/arch -x86_64 ./config.status
    else
      ./config.status
    fi
  ) || die "$arch_name configure-output regeneration failed"
  write_configure_state "$build_dir" "$args_file" "$arch_name" "$state_file"
}

cmd_build() {
  cmd_preflight
  require_prepared
  require_file "$BUILD_X64/Makefile"
  require_file "$BUILD_ARM64/Makefile"
  require_file "$ROOT/configure-x64.args"
  require_file "$ROOT/configure-arm64.args"
  /bin/rm -f "$BUILD_STATE_FILE"
  setup_x64_env
  regenerate_configured_tree "$BUILD_X64" "$ROOT/configure-x64.args" x86_64 "$CONFIG_X64_STATE_FILE"
  info "refreshing x86_64 WoW64 generated dependencies"
  /usr/bin/arch -x86_64 /usr/bin/make -C "$BUILD_X64" depend
  artifact_inventory | while IFS='|' read -r key build target installed artifact_arch format sources; do
    [ "$build" = x86_64 ] || continue
    info "building $artifact_arch $format $target"
    /usr/bin/arch -x86_64 /usr/bin/make -C "$BUILD_X64" -j"$JOBS" "$target"
  done
  setup_arm64_env
  regenerate_configured_tree "$BUILD_ARM64" "$ROOT/configure-arm64.args" arm64 "$CONFIG_ARM64_STATE_FILE"
  info "refreshing arm64 generated dependencies"
  /usr/bin/make -C "$BUILD_ARM64" depend
  artifact_inventory | while IFS='|' read -r key build target installed artifact_arch format sources; do
    [ "$build" = arm64 ] || continue
    info "building arm64 native $target"
    /usr/bin/make -C "$BUILD_ARM64" -j"$JOBS" "$target"
  done
  artifact_inventory | while IFS='|' read -r key build target installed artifact_arch format sources; do
    case "$build" in x86_64) output="$BUILD_X64/$target" ;; arm64) output="$BUILD_ARM64/$target" ;; esac
    require_file "$output"
  done
  write_build_state
  info "closed incremental artifact build complete; successful build state recorded"
}

write_provenance() {
  base_tree_sha=$1
  inherited_tree_sha=$2
  tmp="$PROVENANCE_FILE.tmp.$$"
  BASE_PROVENANCE_SHA=$(sha256_file "$BASE_PROVENANCE") BASE_TREE_SHA=$base_tree_sha \
  INHERITED_TREE_SHA=$inherited_tree_sha SOURCE_DIR_VALUE=$SOURCE_DIR BASE_HOST_VALUE=$BASE_HOST \
  HOST_DIR_VALUE=$HOST_DIR BASE_PROVENANCE_VALUE=$BASE_PROVENANCE BUILD_X64_VALUE=$BUILD_X64 \
  BUILD_ARM64_VALUE=$BUILD_ARM64 PREPARED_VALUE=$PREPARED_FILE \
  X64_ARGS_VALUE=$ROOT/configure-x64.args ARM64_ARGS_VALUE=$ROOT/configure-arm64.args \
  ARTIFACT_INVENTORY_VALUE=$ARTIFACT_INVENTORY INSPECTED_UPSTREAM_TIP_VALUE=$INSPECTED_UPSTREAM_TIP \
  BACKPORTED_UPSTREAM_COMMITS_VALUE=$BACKPORTED_UPSTREAM_COMMITS PROVENANCE_NAME_VALUE=$PROVENANCE_NAME RUNTIME_ID_VALUE=$RUNTIME_ID BUILD_PROFILE_VALUE=$WINE_BUILD_PROFILE PACKAGE_COMMAND_VALUE=$PACKAGE_COMMAND ROOT_REL_VALUE=$ROOT_REL \
  /usr/bin/python3 - "$tmp" <<'PY'
import datetime, hashlib, json, os, pathlib, sys

def sha(path): return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
def args(path): return [line.rstrip("\n") for line in open(path, encoding="utf-8") if line.rstrip("\n")]
source = pathlib.Path(os.environ["SOURCE_DIR_VALUE"])
base = pathlib.Path(os.environ["BASE_HOST_VALUE"])
host = pathlib.Path(os.environ["HOST_DIR_VALUE"])
prepared = json.load(open(os.environ["PREPARED_VALUE"], encoding="utf-8"))
rebuilt = []
replaced = {}
inherited_except = []
for line in os.environ["ARTIFACT_INVENTORY_VALUE"].splitlines():
    key, build, target, installed, arch, fmt, sources = line.split("|", 6)
    inherited_except.append(installed)
    replaced[installed] = sha(base / installed)
    item = {"path": installed, "architecture": arch, "format": fmt, "makeTarget": target,
            "buildTree": build, "sha256": sha(host / installed),
            "changedSourceInputs": [{"path": path, "sha256": sha(source / path)} for path in sources.split(",")]}
    if key == "wineserver":
        item.update({"compileDefine": "WINE_TUNED_X86_SERVER", "advertisedWindowsMachines": ["AMD64", "I386"]})
    rebuilt.append(item)
payload = {
    "schemaVersion": 2,
    "name": os.environ["PROVENANCE_NAME_VALUE"],
    "runtimeId": os.environ["RUNTIME_ID_VALUE"],
    "profile": os.environ["BUILD_PROFILE_VALUE"],
    "wineVersion": "11.17",
    "upstreamCommit": "913e31f201d344223bdf3d13a50a41af35893d12",
    "sourcePin": "913e31f201d344223bdf3d13a50a41af35893d12",
    "inspectedUpstreamTip": os.environ["INSPECTED_UPSTREAM_TIP_VALUE"],
    "backportedUpstreamCommits": os.environ["BACKPORTED_UPSTREAM_COMMITS_VALUE"].split(),
    "builtAt": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "buildKind": "incremental-module-overlay", "fullCleanBuild": False,
    "patches": prepared["overlayPatches"], "sourceDir": str(source), "prefix": str(host),
    "clientHostArch": "x86_64", "serverHostArch": "arm64",
    "buildDirs": {"x86_64": os.environ["BUILD_X64_VALUE"], "arm64": os.environ["BUILD_ARM64_VALUE"]},
    "baseline": {"prefix": str(base), "provenance": os.environ["BASE_PROVENANCE_VALUE"],
        "provenanceSha256": os.environ["BASE_PROVENANCE_SHA"],
        "inheritedTreeSha256": os.environ["BASE_TREE_SHA"],
        "copiedTreeSha256BeforeOverlay": os.environ["INHERITED_TREE_SHA"],
        "inheritedExcept": inherited_except, "replacedArtifactSha256": replaced},
    "rebuiltArtifacts": rebuilt,
    "configureArgs": {"x86_64": args(os.environ["X64_ARGS_VALUE"]), "arm64": args(os.environ["ARM64_ARGS_VALUE"])},
    "preparedManifest": os.environ["PREPARED_VALUE"],
    "appleComponentsDownloaded": False, "systemPrefixWritten": False,
    "baselineMutated": False,
    "packagingHandoff": {"command": os.environ["PACKAGE_COMMAND_VALUE"],
        "hostPrefix": os.environ["ROOT_REL_VALUE"] + "/host",
        "provenance": os.environ["ROOT_REL_VALUE"] + "/provenance.json",
        "defaultOutputDir": os.environ["ROOT_REL_VALUE"] + "/package"},
}
with open(sys.argv[1], "w", encoding="utf-8") as stream:
    json.dump(payload, stream, indent=2)
    stream.write("\n")
PY
  /bin/mv "$tmp" "$PROVENANCE_FILE"
}

cmd_install() {
  cmd_preflight
  require_prepared
  require_successful_build
  [ ! -e "$HOST_DIR" ] || die "refusing to overwrite profile host: $HOST_DIR"
  artifact_inventory | while IFS='|' read -r key build target installed artifact_arch format sources; do
    case "$build" in x86_64) output="$BUILD_X64/$target" ;; arm64) output="$BUILD_ARM64/$target" ;; esac
    require_file "$output"
    case "$format:$artifact_arch" in
      macho:x86_64) /usr/bin/lipo -verify_arch x86_64 "$output" >/dev/null || die "$target lacks x86_64" ;;
      macho:arm64)
        /usr/bin/lipo -verify_arch arm64 "$output" >/dev/null || die "$target lacks arm64"
        if /usr/bin/lipo -verify_arch x86_64 "$output" >/dev/null 2>&1; then die "$target unexpectedly contains x86_64"; fi
        ;;
      pe:x86_64) validate_pe_machine "$output" 0x8664 ;;
      pe:i386) validate_pe_machine "$output" 0x014c ;;
      *) die "unsupported artifact identity: $format/$artifact_arch for $target" ;;
    esac
  done

  tmp_host="$ROOT/.host.tmp.$$"
  [ ! -e "$tmp_host" ] || die "temporary host already exists: $tmp_host"
  cleanup_tmp() { /bin/rm -rf "$tmp_host"; }
  trap cleanup_tmp EXIT HUP INT TERM
  /usr/bin/ditto "$BASE_HOST" "$tmp_host"
  base_tree_sha=$(host_tree_digest "$BASE_HOST")
  inherited_tree_sha=$(host_tree_digest "$tmp_host")
  [ "$base_tree_sha" = "$inherited_tree_sha" ] || die "baseline host copy differs before module overlay"
  artifact_inventory | while IFS='|' read -r key build target installed artifact_arch format sources; do
    case "$build" in x86_64) output="$BUILD_X64/$target" ;; arm64) output="$BUILD_ARM64/$target" ;; esac
    /bin/mkdir -p "$(dirname -- "$tmp_host/$installed")"
    /bin/cp "$output" "$tmp_host/$installed"
    case "$format" in macho) /bin/chmod 755 "$tmp_host/$installed" ;; esac
  done
  /bin/mv "$tmp_host" "$HOST_DIR"
  trap - EXIT HUP INT TERM
  write_provenance "$base_tree_sha" "$inherited_tree_sha"
  info "installed mixed-architecture $WINE_BUILD_PROFILE host with the complete rebuilt artifact inventory: $HOST_DIR"
  info "packaging: $PACKAGE_COMMAND $HOST_DIR $PROVENANCE_FILE $ROOT/package"
}

cmd_all() {
  cmd_prepare
  cmd_configure
  cmd_build
  cmd_install
}

[ "$#" -eq 1 ] || { usage >&2; exit 2; }
ACTION=$1
case "$ACTION" in
  -h|--help) usage ;;
  preflight) cmd_preflight ;;
  prepare) cmd_prepare ;;
  configure) cmd_configure ;;
  build) cmd_build ;;
  install) cmd_install ;;
  all) cmd_all ;;
  *) die "unknown action: $ACTION" ;;
esac
