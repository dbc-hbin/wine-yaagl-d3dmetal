#!/bin/sh
# Full current-worktree build. GPTK/D3DMetal remains a separate packaging input.
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
source_dir=${WINE_SOURCE_DIR:-$repo_dir}
root=${WINE_P3_ROOT:-$repo_dir/build/wine-p3-current}
case "$source_dir" in /*) ;; *) source_dir=$PWD/$source_dir ;; esac
case "$root" in /*) ;; *) root=$PWD/$root ;; esac
build_dir=$root/build-x64
native_dir=$root/build-arm64
host_dir=$root/host
lifetime_dir=$root/fsr3-lifetime
native_server=${WINE_NATIVE_SERVER:-no}
jobs=${WINE_JOBS:-$(/usr/sbin/sysctl -n hw.ncpu 2>/dev/null || echo 4)}

die() { printf '%s\n' "build-wine-p3: $*" >&2; exit 1; }
require_file() { [ -f "$1" ] || die "missing file: $1"; }

usage() {
    cat <<'EOF'
Usage: scripts/build-wine-p3.sh {preflight|configure|build|install|all}
Builds CURRENT source, including local changes, not a fetched patch baseline.
Use a fresh WINE_P3_ROOT for a clean build. No source checkout or deletion occurs.
WINE_SOURCE_DIR defaults to this worktree. WINE_DEPS_ENV, when supplied, must
exist and supplies x86_64 dependency/compiler environment. TLS (GnuTLS), fonts
(FreeType), GStreamer and FFmpeg are required, never automatically disabled.
WINE_MINGW_CLANG (or WINE_MINGW_ROOT), WINE_BISON and WINE_PKG_CONFIG select tools;
otherwise tools come from PATH. Build tools are built by Wine in this build tree.
WINE_NATIVE_SERVER=yes additionally builds the arm64 x86-client wineserver.
WINE_JOBS, SDKROOT and MACOSX_DEPLOYMENT_TARGET are honored.
GPTK and launcher packaging are separate; this script never touches a live prefix.
EOF
}

load_deps() {
    if [ -n "${WINE_DEPS_ENV:-}" ]; then
        require_file "$WINE_DEPS_ENV"
        . "$WINE_DEPS_ENV"
    fi
    if [ -n "${WINE_MINGW_ROOT:-}" ]; then
        PATH=$WINE_MINGW_ROOT/bin:$PATH
    fi
    mingw_clang=${WINE_MINGW_CLANG:-$(command -v x86_64-w64-mingw32-clang || true)}
    bison=${WINE_BISON:-$(command -v bison || true)}
    pkg_config=${WINE_PKG_CONFIG:-${PKG_CONFIG:-$(command -v pkg-config || true)}}
    [ -n "$mingw_clang" ] && [ -x "$mingw_clang" ] || die 'set WINE_MINGW_CLANG to LLVM MinGW clang'
    # configure accepts */clang, not a target-prefixed compiler path.
    mingw_clang=$(dirname "$mingw_clang")/clang
    [ -x "$mingw_clang" ] || die 'LLVM MinGW clang driver is missing'
    [ -n "$bison" ] && [ -x "$bison" ] || die 'set WINE_BISON'
    [ -n "$pkg_config" ] && [ -x "$pkg_config" ] || die 'set WINE_PKG_CONFIG'
    export PATH="$(dirname "$bison"):$(dirname "$mingw_clang"):$PATH"
    export BISON="$bison" PKG_CONFIG="$pkg_config" WINE_MINGW_CLANG="$mingw_clang"
    export PKG_CONFIG_PATH=${WINE_PKG_CONFIG_PATH:-${PKG_CONFIG_PATH:-}}
    export PKG_CONFIG_LIBDIR=${WINE_PKG_CONFIG_LIBDIR:-${PKG_CONFIG_LIBDIR:-/nonexistent}}
    export SDKROOT=${SDKROOT:-$(/usr/bin/xcrun --sdk macosx --show-sdk-path)}
    export MACOSX_DEPLOYMENT_TARGET=${MACOSX_DEPLOYMENT_TARGET:-14.0}
    export CC="${CC:-/usr/bin/clang -arch x86_64}"
    export CXX="${CXX:-/usr/bin/clang++ -arch x86_64}"
    export CFLAGS="${CFLAGS:--O2 -g}" CROSSCFLAGS="${CROSSCFLAGS:--O2}"
    export LDFLAGS="${LDFLAGS:-} -Wl,-headerpad_max_install_names"
}

preflight() {
    case "$native_server" in yes|no) ;; *) die 'WINE_NATIVE_SERVER must be yes or no' ;; esac
    [ -z "${WINE_TOOLS_ROOT:-}${WINE_TUNED_WINE_TOOLS:-}" ] || die 'external Wine tools are not supported; unset WINE_TOOLS_ROOT/WINE_TUNED_WINE_TOOLS'
    require_file "$source_dir/configure"
    require_file "$source_dir/dlls/amd_fidelityfx_upscaler_dx12/Makefile.in"
    require_file "$source_dir/dlls/amd_fidelityfx_framegeneration_dx12/Makefile.in"
    require_file "$source_dir/scripts/build-fsr3-lifetime.sh"
    /usr/bin/git -C "$source_dir" rev-parse --show-toplevel >/dev/null
    load_deps
    "$pkg_config" --exists gnutls freetype2 gstreamer-1.0 gstreamer-video-1.0 \
        gstreamer-audio-1.0 gstreamer-tag-1.0 libavcodec libavformat libavutil \
        || die 'required x86_64 TLS/font/media dependencies missing; supply WINE_DEPS_ENV'
    printf 'Building current source: %s\nBuild root: %s\n' "$source_dir" "$root"
}

native_env() {
    export CC='/usr/bin/clang -arch arm64' CXX='/usr/bin/clang++ -arch arm64'
    export CFLAGS='-O2 -g -DWINE_TUNED_X86_SERVER' CXXFLAGS='-O2 -g -DWINE_TUNED_X86_SERVER'
    export CPPFLAGS= LDFLAGS='-Wl,-headerpad_max_install_names'
    export PKG_CONFIG=/usr/bin/false PKG_CONFIG_PATH= PKG_CONFIG_LIBDIR=/nonexistent
    unset CPATH C_INCLUDE_PATH CPLUS_INCLUDE_PATH LIBRARY_PATH \
        GSTREAMER_CFLAGS GSTREAMER_LIBS FFMPEG_CFLAGS FFMPEG_LIBS \
        GNUTLS_CFLAGS GNUTLS_LIBS FREETYPE_CFLAGS FREETYPE_LIBS
}

run_configure() {
    target=$1
    architecture=$2
    shift 2
    mkdir -p "$target"
    /usr/bin/python3 - "$target/configure.args.json" "$@" <<'PY'
import json, sys
with open(sys.argv[1], 'w') as stream:
    json.dump(sys.argv[2:], stream, indent=2)
    stream.write('\n')
PY
    (cd "$target" && /usr/bin/arch "-$architecture" "$source_dir/configure" "$@")
}

configure() {
    [ ! -e "$build_dir" ] && [ ! -e "$native_dir" ] && [ ! -e "$host_dir" ] \
        || die "use a fresh WINE_P3_ROOT; refusing existing build/install directories in $root"
    run_configure "$build_dir" x86_64 \
        "--prefix=$host_dir" --disable-tests --enable-win64 --enable-archs=i386,x86_64 \
        "--with-mingw=$mingw_clang" --with-coreaudio --with-cups --with-pthread \
        --with-freetype --with-gnutls --with-gstreamer --with-ffmpeg \
        --without-alsa --without-dbus --without-fontconfig --without-gphoto \
        --without-gssapi --without-krb5 --without-netapi --without-opengl \
        --without-oss --without-pulse --without-sane --without-udev \
        --without-usb --without-v4l2 --without-wayland --without-x
    if [ "$native_server" = yes ]; then
        (native_env
         run_configure "$native_dir" arm64 \
            "--prefix=$host_dir" --disable-tests --enable-archs=none --without-mingw \
            --without-alsa --without-capi --without-coreaudio --without-cups \
            --without-dbus --without-ffmpeg --without-fontconfig --without-freetype \
            --without-gettext --without-gettextpo --without-gnutls --without-gphoto \
            --without-gssapi --without-gstreamer --without-inotify --without-krb5 \
            --without-netapi --without-opencl --without-opengl --without-oss \
            --without-pcap --with-pthread --without-pcsclite --without-pulse \
            --without-sane --without-sdl --without-udev --without-usb --without-v4l2 \
            --without-vulkan --without-wayland --without-x --disable-winebth_sys)
    fi
    printf '%s\n' "$native_server" > "$root/native-server.mode"
    printf '%s\n' "$source_dir" > "$root/source-directory"
}

# Fingerprint actual tracked and nonignored source bytes, not a claimed upstream pin.
source_identity() {
    /usr/bin/python3 - "$source_dir" <<'PY'
import hashlib, json, os, pathlib, subprocess, sys
root = pathlib.Path(sys.argv[1])
def git(*args):
    return subprocess.check_output(['git', '-C', str(root), *args])
digest = hashlib.sha256()
for name in sorted(set(git('ls-files', '-z', '--cached', '--others', '--exclude-standard').split(b'\0')) - {b''}):
    path = root / os.fsdecode(name)
    digest.update(name + b'\0')
    if path.is_symlink():
        digest.update(b'L' + os.fsencode(os.readlink(path)))
    elif path.is_file():
        digest.update(b'F' + path.read_bytes())
    else:
        digest.update(b'DELETED')
    digest.update(b'\0')
print(json.dumps({'sourceDirectory': str(root), 'sourceCommit': git('rev-parse', 'HEAD').decode().strip(),
                  'sourceTreeSha256': digest.hexdigest(),
                  'sourceStatus': git('status', '--porcelain', '--untracked-files=normal').decode()}))
PY
}

build() {
    require_file "$build_dir/Makefile"
    require_file "$root/native-server.mode"
    require_file "$root/source-directory"
    [ "$(cat "$root/source-directory")" = "$source_dir" ] || die 'WINE_SOURCE_DIR differs from configure'
    [ "$(cat "$root/native-server.mode")" = "$native_server" ] || die 'WINE_NATIVE_SERVER differs from configure'
    rm -f "$root/build-complete.marker"
    source_identity > "$root/build-source.json"
    /usr/bin/arch -x86_64 /usr/bin/make -C "$build_dir" -j"$jobs"
    /usr/bin/python3 "$source_dir/scripts/game-mode.py" build "$build_dir"
    sh "$source_dir/scripts/build-fsr3-lifetime.sh" "$lifetime_dir"
    if [ "$native_server" = yes ]; then
        require_file "$native_dir/Makefile"
        (native_env; /usr/bin/arch -arm64 /usr/bin/make -C "$native_dir" -j"$jobs" server/wineserver)
    fi
    source_identity > "$root/build-source-after.json"
    cmp -s "$root/build-source.json" "$root/build-source-after.json" \
        || die 'source changed during build; rerun build before installing'
    printf '%s\n' "$native_server" > "$root/build-complete.marker"
}

install_host() {
    require_file "$root/build-source.json"
    require_file "$root/build-complete.marker"
    require_file "$root/source-directory"
    [ "$(cat "$root/source-directory")" = "$source_dir" ] || die 'WINE_SOURCE_DIR differs from configure'
    [ "$(cat "$root/build-complete.marker")" = "$native_server" ] || die 'WINE_NATIVE_SERVER differs from build'
    require_file "$lifetime_dir/lib/wine/x86_64-windows/yaagl_fsr3_lifetime.dll"
    require_file "$lifetime_dir/build-manifest.json"
    [ ! -e "$host_dir" ] || die "refusing to overwrite $host_dir"
    source_identity > "$root/install-source.json"
    cmp -s "$root/build-source.json" "$root/install-source.json" \
        || die 'source changed since build; rerun build before installing'
    /usr/bin/arch -x86_64 /usr/bin/make -C "$build_dir" install
    if [ "$native_server" = yes ]; then
        require_file "$native_dir/server/wineserver"
        cp "$native_dir/server/wineserver" "$host_dir/bin/wineserver"
    fi
    [ -x "$host_dir/bin/wine" ] || die 'Wine install failed'
    /usr/bin/python3 "$source_dir/scripts/game-mode.py" install "$build_dir" --tree "$host_dir"
    cp "$lifetime_dir/lib/wine/x86_64-windows/yaagl_fsr3_lifetime.dll" \
        "$host_dir/lib/wine/x86_64-windows/yaagl_fsr3_lifetime.dll"
    /usr/bin/python3 - "$root" "$source_dir" "$native_server" <<'PY'
import datetime, hashlib, json, os, pathlib, subprocess, sys
root, source = map(pathlib.Path, sys.argv[1:3])
data = json.loads((root / 'build-source.json').read_text())
version = (source / 'VERSION').read_text().strip().removeprefix('Wine version ')
data.update(schemaVersion=3, buildKind='full-current-source', wineVersion=version,
            name=os.environ.get('WINE_PACKAGE_NAME', 'wine-' + version + '-gptk4b3-current'),
            runtimeId=os.environ.get('WINE_RUNTIME_ID', version + '-gptk4b3-current'),
            host=str(root / 'host'), windowsArchitectures=['i386', 'x86_64'],
            wineserverArchitecture='arm64' if sys.argv[3] == 'yes' else 'x86_64',
            configureArgs=json.loads((root / 'build-x64/configure.args.json').read_text()),
            builtAt=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            depsEnvironment=os.environ.get('WINE_DEPS_ENV'), graphicsOverlayIncluded=False,
            graphicsTarget='gptk-4.0b3')
def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
data['fsr3LifetimeBuildManifest'] = json.loads((root / 'fsr3-lifetime/build-manifest.json').read_text())
lifetime = root / 'host/lib/wine/x86_64-windows/yaagl_fsr3_lifetime.dll'
if sha(lifetime) != data['fsr3LifetimeBuildManifest']['artifact']['sha256']:
    raise SystemExit('installed private FSR3 lifetime DLL differs from its build manifest')
data['hostArtifacts'] = {str(p.relative_to(root / 'host')): sha(p)
                         for p in sorted((root / 'host').rglob('*')) if p.is_file()}
data['hostSymlinks'] = {str(p.relative_to(root / 'host')): os.readlink(p)
                        for p in sorted((root / 'host').rglob('*')) if p.is_symlink()}
data['buildEnvironment'] = {key: os.environ.get(key) for key in (
    'CC', 'CXX', 'CFLAGS', 'CXXFLAGS', 'CPPFLAGS', 'LDFLAGS', 'CROSSCFLAGS',
    'SDKROOT', 'MACOSX_DEPLOYMENT_TARGET', 'PKG_CONFIG', 'PKG_CONFIG_PATH',
    'PKG_CONFIG_LIBDIR', 'WINE_MINGW_ROOT', 'WINE_MINGW_CLANG', 'BISON')}
data['configureLogSha256'] = sha(root / 'build-x64/config.log')
if os.environ.get('WINE_DEPS_ENV'):
    data['depsEnvironmentSha256'] = sha(pathlib.Path(os.environ['WINE_DEPS_ENV']))
data['toolVersions'] = {tool: subprocess.check_output(command, text=True).strip() for tool, command in {
    'clang': ['/usr/bin/clang', '--version'], 'bison': [os.environ['BISON'], '--version'],
    'mingw': [os.environ['WINE_MINGW_CLANG'], '--version'],
    'pkg-config': [os.environ['PKG_CONFIG'], '--version'],
    'make': ['/usr/bin/make', '--version'], 'sdk': ['/usr/bin/xcrun', '--sdk', 'macosx', '--show-sdk-version']}.items()}
if sys.argv[3] == 'yes':
    data['nativeServerConfigureArgs'] = json.loads((root / 'build-arm64/configure.args.json').read_text())
with (root / 'provenance.json').open('w') as stream:
    json.dump(data, stream, indent=2)
    stream.write('\n')
PY
}

case "${1:-}" in
    preflight) preflight ;;
    configure) preflight; configure ;;
    build) preflight; build ;;
    install) preflight; install_host ;;
    all) preflight; configure; build; install_host ;;
    *) usage >&2; exit 2 ;;
esac
