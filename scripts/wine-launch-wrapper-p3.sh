#!/bin/sh
set -u

wrapper_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
real_wine="$wrapper_dir/wine.real"
if [ ! -x "$real_wine" ]; then
  echo "YAAGL local D3DMetal runtime: missing $real_wine" >&2
  exit 126
fi

wine_root=$(CDPATH= cd -- "$wrapper_dir/.." && pwd)

# GPTK D3DMetal with a fixed per-game adapter identity: Zenless Zone Zero sees
# AMD Radeon RX 9070, every other launch sees NVIDIA GeForce RTX 5060.
# Default the timeout fix without overriding an explicit launcher selection.
export WINE_ENABLE_TIMEOUT_FIX=${WINE_ENABLE_TIMEOUT_FIX-1}
export CX_ACTIVE_GRAPHICS_BACKEND=d3dmetal
export D3DM_MTL4=1
export D3DM_ENABLE_METALFX=1
export D3DM_SUPPORT_DXR=1
# The identity is derived from the launch below; never let an inherited or
# stale manual selection reach wine.real or the game.
unset YAAGL_GPU_IDENTITY
export WINEMSYNC=1
unset WINEDLLOVERRIDES WINEDLLPATH_PREPEND DXMT_CONFIG DXMT_CONFIG_FILE
unset DXVK_CONFIG_FILE DXVK_STATE_CACHE_PATH VK_ICD_FILENAMES VK_DRIVER_FILES
unset DYLD_INSERT_LIBRARIES

# The baseline FSR bridge is installed only by stage-runtime.py; the P3 marker
# alone must not enable loader overrides before those modules exist.
# The launcher owns MTL_HUD_ENABLED; never override its selection.
if [ -f "$wine_root/zzz-frame-probe-stage.json" ]; then
  case "${YAAGL_FSR_UPSCALER:-metalfx}" in
    metalfx) export WINEDLLOVERRIDES=amd_fidelityfx_upscaler_dx12,amd_fidelityfx_framegeneration_dx12=b ;;
    native) export WINEDLLOVERRIDES="amd_fidelityfx_upscaler_dx12=n;amd_fidelityfx_framegeneration_dx12=b" ;;
    *) echo "YAAGL_FSR_UPSCALER must be metalfx or native" >&2; exit 64 ;;
  esac
  export MTL_CAPTURE_ENABLED=0
  export YAAGL_FSR_FG_NATIVE_DLL="Z:$wine_root/lib/wine/x86_64-windows/amd_fidelityfx_framegeneration_dx12_native.dll"
fi

# The P3 baseline carries bundled MacDeps/GStreamer before FSR staging.
if [ -f "$wine_root/yaagl-wine-p3-runtime.txt" ]; then
  wine_lib="$wine_root/lib"
  export CX_APPLEGPTK_LIBD3DSHARED_PATH="$wine_lib/external/libd3dshared.dylib"
  gst_root="$wine_lib/GStreamer.framework/Versions/1.0"
  dyld_fallback="$wine_lib"
  if [ -d "$gst_root/lib" ]; then
    dyld_fallback="$gst_root/lib:$dyld_fallback"
  fi
  if [ -n "${DYLD_FALLBACK_LIBRARY_PATH:-}" ]; then
    export DYLD_FALLBACK_LIBRARY_PATH="$dyld_fallback:$DYLD_FALLBACK_LIBRARY_PATH"
  else
    export DYLD_FALLBACK_LIBRARY_PATH="$dyld_fallback"
  fi
  if [ -d "$gst_root/lib/gstreamer-1.0" ]; then
    export GST_PLUGIN_SYSTEM_PATH_1_0="$gst_root/lib/gstreamer-1.0"
  fi
  if [ -x "$gst_root/libexec/gstreamer-1.0/gst-plugin-scanner" ]; then
    export GST_PLUGIN_SCANNER="$gst_root/libexec/gstreamer-1.0/gst-plugin-scanner"
  fi
fi

# Classify this launch. Yaagl passes the game executable directly or through the
# Steam wrapper, and its normal launch runs `cmd /c "Z:\...\config.bat"`; match
# only a standalone, case-insensitive file-name token so related files such as
# NotZenlessZoneZero.exe or ZenlessZoneZero.exe.bak never select the game.
launch_has_zzz_token() {
  LC_ALL=C grep -qiE '(^"?|[\\/])zenlesszonezero[.]exe"?[[:space:]]*$'
}

has_zzz=0
if printf '%s\n' "$@" | launch_has_zzz_token; then
  has_zzz=1
fi

# The generated game batch is consulted only when config.bat itself is in the
# invocation, so any other program keeps the default identity even while a
# stale game batch from an earlier run is still on disk.
if [ "$has_zzz" -eq 0 ] && [ -n "${WINEPREFIX:-}" ] &&
    printf '%s\n' "$@" | LC_ALL=C grep -qiE '(^"?|[\\/])config[.]bat"?[[:space:]]*$'; then
  game_batch="$(dirname -- "$WINEPREFIX")/config.bat"
  # Yaagl quotes the full game executable, with optional arguments after it.
  if [ -f "$game_batch" ] && {
    LC_ALL=C grep -qiE '"([^"]*[\\/])?zenlesszonezero[.]exe"([[:space:]]|$)' "$game_batch" ||
      launch_has_zzz_token <"$game_batch"
  }; then
    has_zzz=1
  fi
fi

# Fixed automatic identity: the game keeps the AMD adapter, every other program
# sees the NVIDIA adapter. This does not change renderer capabilities.
if [ "$has_zzz" -eq 1 ]; then
  export D3DM_VENDOR_ID=0x1002
  export D3DM_DEVICE_ID=0x7550
  export D3DM_DEVICE_DESCRIPTION="AMD Radeon RX 9070"
else
  export D3DM_VENDOR_ID=0x10de
  export D3DM_DEVICE_ID=0x2d05
  export D3DM_DEVICE_DESCRIPTION="NVIDIA GeForce RTX 5060"
fi

# Original Yaagl temporarily moves the packaged module to .bak and installs
# DXMT before launching. Copy the saved D3DMetal module back for this run;
# keep .bak so Yaagl's normal patchRevertProgram remains valid afterwards.
restore_d3dmetal_modules() {
  module_dir="$wrapper_dir/../lib/wine/x86_64-windows"
  for module in d3d10core.dll d3d11.dll dxgi.dll; do
    if [ -f "$module_dir/$module.bak" ]; then
      cp "$module_dir/$module.bak" "$module_dir/$module" || return 1
    fi
  done
}

restore_d3dmetal_modules || exit 124
exec "$real_wine" "$@"
