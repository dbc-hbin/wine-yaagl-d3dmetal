#!/bin/sh
# Rebuild changed Wine modules from this source against the verified f163d14 runtime.
# Usage: build-yaagl-overlay.sh BASE_WINE_ROOT OUTPUT_DIR
set -eu
repo=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
base=$(CDPATH= cd -- "$1" && pwd)
out=$2
[ ! -e "$out" ] || { echo "refusing existing output: $out" >&2; exit 1; }
mkdir -p "$out/x64" "$out/arm64"
out=$(CDPATH= cd -- "$out" && pwd)
provenance="$base/yaagl-wine-p3-provenance.json"
[ -f "$provenance" ] || { echo "missing baseline provenance" >&2; exit 1; }
macports=/Users/hanbinnoh/Documents/yaagl-dx12/build/wine-p3/deps/macports/opt/local
gstreamer=/Users/hanbinnoh/Documents/yaagl-dx12/naposdx12/wine/lib/GStreamer.framework/Versions/1.0
mingw=/opt/llvm-mingw-20260616-ucrt-macos-universal
export PATH="/opt/homebrew/opt/bison/bin:$mingw/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export SDKROOT=$(/usr/bin/xcrun --sdk macosx --show-sdk-path)
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
export MACOSX_DEPLOYMENT_TARGET=26.0
export CC='/usr/bin/clang -arch x86_64' CXX='/usr/bin/clang++ -arch x86_64'
export CFLAGS='-O2 -g -mmacosx-version-min=26.0' CROSSCFLAGS=-O2
export CXXFLAGS="$CFLAGS"
export CPPFLAGS="-I$macports/include"
export LDFLAGS="-Wl,-headerpad_max_install_names -L$macports/lib -mmacosx-version-min=26.0"
export PKG_CONFIG=/opt/homebrew/bin/pkg-config
export PKG_CONFIG_PATH="$macports/lib/pkgconfig:$macports/share/pkgconfig:$gstreamer/lib/pkgconfig"
export PKG_CONFIG_LIBDIR="$PKG_CONFIG_PATH"
unset PKG_CONFIG_SYSROOT_DIR || :
export GSTREAMER_CFLAGS="$($PKG_CONFIG --cflags gstreamer-1.0 gstreamer-video-1.0 gstreamer-audio-1.0 gstreamer-tag-1.0)"
export GSTREAMER_LIBS="$($PKG_CONFIG --libs gstreamer-1.0 gstreamer-video-1.0 gstreamer-audio-1.0 gstreamer-tag-1.0)"
export FFMPEG_CFLAGS="$($PKG_CONFIG --cflags libavutil libavformat libavcodec)"
export FFMPEG_LIBS="$($PKG_CONFIG --libs libavutil libavformat libavcodec)"
python3 - "$provenance" "$repo/configure" "$out/x64" "$out/host" <<'PY'
import json, os, subprocess, sys
provenance, configure, build, prefix = sys.argv[1:]
args = [('--prefix=' + prefix if arg.startswith('--prefix=') else arg)
        for arg in json.load(open(provenance))['configureArgs']['x86_64']]
args += ['--build=x86_64-apple-darwin', '--host=x86_64-apple-darwin']
subprocess.run([configure, *args], cwd=build, check=True)
PY
# Wine's version.c is generated from git metadata; this source branch still pins Wine 11.17.
(cd "$out/x64" && /usr/bin/arch -x86_64 make -f Makefile -f "$repo/scripts/wine-version.mk" -j 8 \
  dlls/ntdll/ntdll.so dlls/win32u/win32u.so dlls/winemac.drv/winemac.so \
  dlls/amd_fidelityfx_framegeneration_dx12/x86_64-windows/amd_fidelityfx_framegeneration_dx12.dll)
export PATH='/opt/homebrew/opt/bison/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin'
export CC='/usr/bin/clang -arch arm64' CXX='/usr/bin/clang++ -arch arm64'
export CFLAGS='-O2 -g -DWINE_TUNED_X86_SERVER -mmacosx-version-min=26.0'
export CXXFLAGS="$CFLAGS"
export CPPFLAGS= LDFLAGS='-Wl,-headerpad_max_install_names -mmacosx-version-min=26.0'
export PKG_CONFIG=/usr/bin/false PKG_CONFIG_PATH= PKG_CONFIG_LIBDIR=
unset CROSSCFLAGS CROSSLDFLAGS GSTREAMER_CFLAGS GSTREAMER_LIBS FFMPEG_CFLAGS FFMPEG_LIBS || :
python3 - "$provenance" "$repo/configure" "$out/arm64" "$out/host" <<'PY'
import json, subprocess, sys
provenance, configure, build, prefix = sys.argv[1:]
args = [('--prefix=' + prefix if arg.startswith('--prefix=') else arg)
        for arg in json.load(open(provenance))['configureArgs']['arm64']]
subprocess.run([configure, *args], cwd=build, check=True)
PY
make -C "$out/arm64" -j 8 server/wineserver
printf 'overlay built from %s at %s\n' "$(git -C "$repo" rev-parse HEAD)" "$out"
