# CX 26.3 D3DMetal runtime (GPTK 4.0b2)

[한국어](README.ko.md)

A clean CrossOver 26.3 / Wine 11.0 build path for Yaagl.
Targets Apple Silicon, macOS 26+, and Rosetta 2; macOS 26 hardware requires separate validation.
The Wine loader and Unix modules are x86_64, Windows modules are i386/x86_64, and wineserver is ARM64.

## Selected features

- Yaagl MF video and Timeout compatibility, preserving work-queue release, bounded heap access, and explicit Timeout OFF.
- ARM64 wineserver, retaining CX's MSync implementation and server protocol.
- FSR API translation to MetalFX SR/FG with native FG fallback.
  Valid MetalFX/automatic-provider FFX memory-usage queries (V1/V2) return success with both byte counts set to zero, matching D3DMetal DLSS compatibility behavior. Zero means unreported usage, not a measured footprint; native FG/swapchain queries retain their native results. No allocation tracking or query-time GPU allocation is added.
- GPTK 4.0b2 FP64 codec, stage-lock, native PSO/function/RT caches, and required display/resource lifetime handling.
- Per-game GPU identity: RX 9070 for ZZZ; RTX 5060 for other launches.
- Metal HUD Frame Interpolator row cleanup when FG stops.

Custom MSync tuning, cursor ownership/RawInput/warp changes, generic Wine 11.17 backports, cache warmup, idle MetalFX SR backend pooling, and historical RT diagnostic/translation tools are excluded.
Cursor changes remain a separate future A/B candidate, not part of this build.

## Source and input boundaries

`scripts/build-wine-crossover.sh` verifies the pinned official `crossover-sources-26.3.0.tar.gz` and prepares `build/cx26.3/source-root/sources/wine`.
Only three patches in `patches/wine-cx/` and explicitly enumerated new FSR sources are applied.
The repository root contains that CX 26.3 / Wine 11.0 source with the selected patches and new FSR modules already applied.
The build reconstructs a separate verified source tree from the pinned archive and the same patches; it does not inherit an older Wine core or build tree.

`scripts/wine-crossover-inputs.json` allows only external GPTK bridge modules and non-Wine dependency libraries from the sealed local input runtime.
No old Wine loader, server, ordinary PE/Unix module, NLS, or font is inherited.
The donor's full file inventory and selected inputs are verified; changed inputs are rejected.
Yaagl's default CrossOver distribution is a compatibility reference, not a substitute for the pinned source archive.

## Build

Requirements: Xcode, Node.js, Python 3, llvm-mingw, and x86_64 MacPorts/GStreamer dependency SDKs.
The full build requires at least 20 GiB free at startup; packaging requires at least 8 GiB free.

```sh
scripts/build-wine-crossover.sh fetch
scripts/build-wine-crossover.sh preflight
node scripts/build-d3dmetal-autopatch.mjs build/cx26.3/autopatch
scripts/build-wine-crossover.sh all
```

Individual actions are `prepare`, `configure`, `build`, `install`, and `package`.
Existing configure trees and package outputs are not overwritten. Resolve the cause of an interrupted step before resuming that step.

Defaults reflect the local build environment. Override paths with `WINE_CX_ROOT`, `WINE_CX_MINGW`, `WINE_CX_DEPS_PREFIX`, `WINE_CX_GSTREAMER_ROOT`, and `WINE_CX_DONOR`.
Changing `WINE_CX_DONOR` does not bypass the pinned donor inventory.

Outputs:

- `build/cx26.3/host/`: Wine installed from the clean source build.
- `build/cx26.3/package/wine/`: runtime with external bridges, dependencies, and native autopatcher.
- `build/cx26.3/package/wine-cx26.3-d3dmetal-gptk4.0b2-macos26.tar.xz`: distributable archive rooted at `wine/`.
- `build/cx26.3/package/SHA256SUMS`: archive checksum.

## D3DMetal preparation and verification

Neither Git nor the runtime archive contains Apple's `D3DMetal.framework`.
Yaagl must obtain Apple license consent before passing `--accept-apple-license` to `wine/libexec/yaagl-d3dmetal/prepare-d3dmetal-runtime` and preparing the framework in `wine/lib/external/` before Wine loads it.
Exact download, pre/post-patch, and signature checks remain fail-closed; other GPTK revisions are rejected.

Focused boundary checks:

```sh
node --test scripts/prepare-d3dmetal-runtime.test.mjs scripts/metalir-fp64-codec-patch.test.mjs
python3 scripts/test-resource-map.py
python3 scripts/test-fg-normalization.py
python3 scripts/test-fsr-memory-query.py
python3 scripts/test-wine-launch-wrapper.py
```

A successful build is not gameplay verification. Before distribution, exercise 32/64-bit programs with the ARM64 server, actual D3D12/FSR GPU work, and MF video playback in an isolated prefix.
Visual output, long gameplay sessions, and interpolation quality require separate in-game validation.
Build operations do not modify existing apps or game prefixes.

## Licenses

See [LICENSE](LICENSE), [COPYING.LIB](COPYING.LIB), and the notices bundled with each third-party source. Apple GPTK has separate license terms.
