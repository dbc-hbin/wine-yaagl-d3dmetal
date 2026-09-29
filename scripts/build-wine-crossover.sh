#!/bin/sh
# Clean CodeWeavers 26.3 Wine build. No baseline Wine object or binary enters host/.
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
root=${WINE_CX_ROOT:-"$repo/build/cx26.3"}
source="$root/source-root/sources/wine"
build="$root/build-x64"
armbuild="$root/build-arm64"
prefix="$root/host"
archive="$root/source-cache/crossover-sources-26.3.0.tar.gz"
expected=ac99c8ca4b3848f3e81784135f023df266b61c2345726ea55a50b3e030dd6872
url=https://media.codeweavers.com/pub/crossover/source/crossover-sources-26.3.0.tar.gz
mingw=${WINE_CX_MINGW:-/opt/llvm-mingw-20260616-ucrt-macos-universal}
macports=${WINE_CX_DEPS_PREFIX:-/Users/hanbinnoh/Documents/yaagl-dx12/build/wine-p3/deps/macports/opt/local}
gst=${WINE_CX_GSTREAMER_ROOT:-/Users/hanbinnoh/Documents/yaagl-dx12/naposdx12/wine/lib/GStreamer.framework/Versions/1.0}
pkgconfig=${WINE_CX_PKG_CONFIG:-/opt/homebrew/bin/pkg-config}
deployment=${MACOSX_DEPLOYMENT_TARGET:-26.0}
jobs=${WINE_CX_JOBS:-$(/usr/sbin/sysctl -n hw.ncpu)}
minimum_space() {
    available=$(df -Pk "$root" | /usr/bin/tail -n 1 | /usr/bin/tr -s ' ' | /usr/bin/cut -d ' ' -f 4)
    [ "$available" -ge "$1" ] || { echo "insufficient disk: need $1 KiB free, have $available KiB" >&2; exit 1; }
}
verify_archive() {
    [ -f "$archive" ] || { echo "missing verified CX archive: $archive" >&2; exit 1; }
    actual=$(/usr/bin/shasum -a 256 "$archive" | /usr/bin/cut -d ' ' -f 1)
    [ "$actual" = "$expected" ] || { echo "CX archive checksum mismatch: $actual" >&2; exit 1; }
}
fetch() {
    /bin/mkdir -p "$root/source-cache"
    if [ ! -e "$archive" ]; then
        /usr/bin/curl -fL --retry 3 -o "$archive.tmp" "$url"
        actual=$(/usr/bin/shasum -a 256 "$archive.tmp" | /usr/bin/cut -d ' ' -f 1)
        [ "$actual" = "$expected" ] || { echo 'downloaded CX source checksum mismatch' >&2; exit 1; }
        /bin/mv "$archive.tmp" "$archive"
    fi
    verify_archive
}
prepare() {
    fetch
    python3 "$repo/scripts/package-wine-crossover.py" prepare "$repo" "$root"
}
common() {
    [ -x "$mingw/bin/clang" ] || { echo "missing llvm-mingw: $mingw" >&2; exit 1; }
    [ -f "$macports/lib/pkgconfig/gnutls.pc" ] || { echo "missing MacPorts dependency SDK: $macports" >&2; exit 1; }
    [ -f "$gst/lib/pkgconfig/gstreamer-1.0.pc" ] || { echo "missing GStreamer SDK: $gst" >&2; exit 1; }
    [ -x "$pkgconfig" ] || { echo "missing pkg-config: $pkgconfig" >&2; exit 1; }
    [ "$deployment" = 26.0 ] || { echo 'CX MetalFX runtime requires macOS deployment 26.0' >&2; exit 1; }
    export SDKROOT=${SDKROOT:-$(/usr/bin/xcrun --sdk macosx --show-sdk-path)}
    export DEVELOPER_DIR=${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}
    export MACOSX_DEPLOYMENT_TARGET="$deployment"
    export PATH="/opt/homebrew/opt/bison/bin:$mingw/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    export PKG_CONFIG="$pkgconfig"
    export PKG_CONFIG_PATH="$macports/lib/pkgconfig:$macports/share/pkgconfig:$gst/lib/pkgconfig"
    export PKG_CONFIG_LIBDIR="$PKG_CONFIG_PATH"
    unset PKG_CONFIG_SYSROOT_DIR || :
    export GSTREAMER_CFLAGS="$($pkgconfig --cflags gstreamer-1.0 gstreamer-video-1.0 gstreamer-audio-1.0 gstreamer-tag-1.0)"
    export GSTREAMER_LIBS="$($pkgconfig --libs gstreamer-1.0 gstreamer-video-1.0 gstreamer-audio-1.0 gstreamer-tag-1.0)"
    export FFMPEG_CFLAGS="$($pkgconfig --cflags libavutil libavformat libavcodec)"
    export FFMPEG_LIBS="$($pkgconfig --libs libavutil libavformat libavcodec)"
}
configure_x64() {
    common
    [ ! -e "$build/config.status" ] || { echo "refusing existing configure tree: $build" >&2; exit 1; }
    /bin/mkdir -p "$build"
    export CC='/usr/bin/clang -arch x86_64' CXX='/usr/bin/clang++ -arch x86_64'
    export CFLAGS='-O2 -g0 -mmacosx-version-min=26.0' CROSSCFLAGS='-O2 -g0'
    export CXXFLAGS="$CFLAGS" CPPFLAGS="-I$macports/include"
    export LDFLAGS="-Wl,-headerpad_max_install_names -L$macports/lib -L$gst/lib -mmacosx-version-min=26.0"
    export CROSSLDFLAGS='-Wl,--no-insert-timestamp'
    (cd "$build" && /usr/bin/arch -x86_64 "$source/configure" \
        --prefix="$prefix" --disable-tests --enable-win64 --enable-archs=i386,x86_64 \
        --with-mingw="$mingw/bin/clang" --with-coreaudio --with-cups --with-freetype \
        --with-gettext --with-gnutls --with-gstreamer --with-ffmpeg --with-sdl \
        --with-pthread --with-pcsclite --with-opencl --without-pcap --without-inotify \
        --without-vulkan --without-alsa --without-capi --without-dbus --without-fontconfig \
        --without-gettextpo --without-gphoto --without-gssapi --without-krb5 \
        --without-netapi --without-opengl --without-oss --without-pulse --without-sane \
        --without-udev --without-usb --without-v4l2 --without-wayland --without-x \
        --disable-winebth_sys)
}
configure_arm64() {
    [ ! -e "$armbuild/config.status" ] || { echo "refusing existing ARM configure tree: $armbuild" >&2; exit 1; }
    /bin/mkdir -p "$armbuild"
    export PATH='/opt/homebrew/opt/bison/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin'
    export CC='/usr/bin/clang -arch arm64' CXX='/usr/bin/clang++ -arch arm64'
    export CFLAGS='-O2 -g0 -DWINE_TUNED_X86_SERVER -mmacosx-version-min=26.0'
    export CXXFLAGS="$CFLAGS" CPPFLAGS= LDFLAGS='-Wl,-headerpad_max_install_names -mmacosx-version-min=26.0'
    export PKG_CONFIG=/usr/bin/false PKG_CONFIG_PATH= PKG_CONFIG_LIBDIR=
    unset CROSSCFLAGS CROSSLDFLAGS GSTREAMER_CFLAGS GSTREAMER_LIBS FFMPEG_CFLAGS FFMPEG_LIBS || :
    (cd "$armbuild" && "$source/configure" --prefix="$prefix" --disable-tests \
        --enable-archs=none --without-mingw --without-x --without-opengl --without-vulkan --without-gstreamer \
        --without-ffmpeg --without-gnutls --without-cups --without-freetype \
        --without-fontconfig --without-gettext --without-sdl --without-opencl \
        --without-pcsclite --without-pcap --without-inotify)
}
configured() {
    python3 "$repo/scripts/package-wine-crossover.py" verify-prepared "$repo" "$root"
    [ -f "$build/config.status" ] && [ -f "$armbuild/config.status" ] || {
        echo 'missing both configured build trees' >&2; exit 1;
    }
}
case ${1:-all} in
    fetch) fetch ;;
    prepare) prepare ;;
    preflight) /bin/mkdir -p "$root"; verify_archive; common; python3 "$repo/scripts/package-wine-crossover.py" check-inputs "$repo" "$root" ;;
    configure) /bin/mkdir -p "$root"; minimum_space 2097152; prepare; common; configure_x64; configure_arm64 ;;
    build) minimum_space 20971520; configured; common; /usr/bin/arch -x86_64 /usr/bin/make -C "$build" -j "$jobs"; /usr/bin/make -C "$armbuild" -j "$jobs" server/wineserver ;;
    install) configured; [ -f "$armbuild/server/wineserver" ] || { echo 'ARM64 server missing' >&2; exit 1; }; [ ! -e "$prefix" ] || { echo "refusing existing host: $prefix" >&2; exit 1; }; /usr/bin/arch -x86_64 /usr/bin/make -C "$build" install; /bin/cp "$armbuild/server/wineserver" "$prefix/bin/wineserver"; /usr/bin/lipo -verify_arch arm64 "$prefix/bin/wineserver" ;;
    package) python3 "$repo/scripts/package-wine-crossover.py" package "$repo" "$root" ;;
    all) /bin/mkdir -p "$root"; minimum_space 20971520; prepare; common; configure_x64; configure_arm64; /usr/bin/arch -x86_64 /usr/bin/make -C "$build" -j "$jobs"; /usr/bin/make -C "$armbuild" -j "$jobs" server/wineserver; [ ! -e "$prefix" ] || { echo "refusing existing host: $prefix" >&2; exit 1; }; /usr/bin/arch -x86_64 /usr/bin/make -C "$build" install; /bin/cp "$armbuild/server/wineserver" "$prefix/bin/wineserver"; /usr/bin/lipo -verify_arch arm64 "$prefix/bin/wineserver"; python3 "$repo/scripts/package-wine-crossover.py" package "$repo" "$root" ;;
    *) echo "usage: $0 {fetch|prepare|preflight|configure|build|install|package|all}" >&2; exit 2 ;;
esac
