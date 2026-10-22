# Wine 11.20 D3DMetal (GPTK 4.0b3)

[한국어](README.ko.md)

Yaagl runtime source for Apple Silicon and Rosetta 2, targeting GPTK 4.0 beta 3 exclusively. Wine host, msync, input, media, audio, window and resource fixes are retained. The native sidecar supplies display routing, video-memory policy and FSR-to-MetalFX translation. GPTK's native caches remain responsible for shader, library, PSO and RT function reuse.

DLSS uses Apple's stock `nvngx-on-metalfx.dll` and Unix module. This tree does not include the experimental DLSS translator, Streamline interception or NPC-nameplate composition. No nameplate fix is claimed.

## Current source and patch policy

This tree starts at the official `wine-11.20` tag and ports the selected runtime changes from verified source `1846c16674b`. The Wine sources in `dlls/`, `server/` and `include/` are the build inputs. See [the migration record](documentation/WINE-REBASE.md) for retained contracts and upstream overlap decisions. Historical P3/tuned patch queues remain in the source commit as reference material; they are not reapplied or copied into this tree.

`d3dmetal-sidecar/layout.json` defines the beta3 hook contract: format 27, marker `YAAGLB3SIDEcar\x1b`, four compact dispatch slots and no additional `binaryPatches`. The entry hooks, in dispatch order, are `GetContainingOutput`, `SetFullscreenState`, `QueryVideoMemoryInfo` and `ReplayTemporalScale`. They cover display routing, video-memory queries and FSR replay only. `ngx-hooks.mm` dispatches recorded FSR commands through `ReplayTemporalScale`; other commands use Apple's original replay. FSR SR/FG uses the Metal4/MPL transport.

All 13 custom cache-related hooks are removed, including PSO/function caching, RT provenance tracking, cache-context/lifetime hooks and library singleflight. Their native entry bytes stay pristine; no forwarding wrappers replace the removed hooks. Native GPTK shader/stage and disk caches, library publication/reuse, owner-local graphics/compute PSOs and RT function reuse remain in place. This does not claim cross-owner reuse equivalent to the removed custom caches or measured performance parity.

DXGI video-memory queries, containing-output queries, fullscreen entry points and output enumeration use the Windows x64 calling convention, including calls back to the original methods. Native helper methods retain the host ABI. Mixing these conventions causes Unity startup crashes before rendering.

The converter stays pristine. The preparation pipeline runs `native-sidecar-patch`, `native-sidecar-module`, then `final-framework-reseal`. The four-hook sidecar lives in `d3dmetal-sidecar/`, is built by `scripts/build-d3dmetal-sidecar.mjs` as `libD3DMetalSidecar.dylib`, and uses `scripts/d3dmetal-sidecar-patch.mjs` for patching. Packaging uses `--sidecar-module` and `--sidecar-manifest`, manifest fields `nativeSidecarBuild` and `nativeSidecar`, and recipe field `patches.nativeSidecar`. These names replace the cache-era interfaces without compatibility aliases; the internal C++ namespace `yaagl::pso` and public FSR C APIs remain unchanged. Excluded patches have different reasons:

| Excluded patch | Reason for the current policy |
|---|---|
| FP64 codec recipe | The known failing codec fixture compiles with the stock beta3 converter, and its cast implementation differs from the old recipe. This does not establish correctness for every FP64/constrained-cast input. |
| Stage-lock and compute-loser cleanup | Beta3 `CompileCompute` already holds the stage-cache lock and releases a duplicate allocation. |
| CacheFile close/munmap | Beta3 closes the file, unmaps the payload and header, and resets its state. |
| CacheFile publication-order | The reader rejects incomplete records and closes or reopens the cache. No reproduced correctness or cache-reuse failure justifies changing the writer order in the default configuration. |
| Legacy fullscreen null/forward call sites | Beta3 forwards the native output itself. Yaagl monitor routing remains a separate sidecar responsibility. |
| Scoped present-residency cleanup | This shortens post-submit association lifetime, rather than repairing submission. It is excluded without measured residency growth or a driver-memory failure. |
| Persistent-cache read-ahead | File warmup is not shader compilation. The game and D3DMetal own shader/PSO precompilation and their on-disk caches. |
| Custom DLSS/Streamline processing | This runtime uses the stock NGX pair; experimental nameplate processing is outside its scope. |

Cursor handling uses the current `cocoa_app.m` and `cocoa_warpconsumption.h` implementation. These policies should not be described as identical to every change in a separate runtime branch.

## Build Wine and the sidecar

Maintainers need Xcode, Node.js, Python, LLVM MinGW, Bison and x86_64 TLS/font/media dependencies. Set `WINE_DEPS_ENV` to the dependency environment and `WINE_MINGW_ROOT` to the installed LLVM MinGW toolchain. The build consumes this worktree, including local changes, rather than fetching a different source checkout.

```sh
WINE_NATIVE_SERVER=yes scripts/build-wine-p3.sh all
node scripts/build-d3dmetal-sidecar.mjs build/native-sidecar
```

`WINE_P3_ROOT` selects a fresh build directory; the default is `build/wine-p3-current`. Its `host/` and provenance describe the actual Wine source. Sidecar output includes the dylib and a source-hash manifest. Existing build directories are not evidence that current sources were built.

## Prepare the Apple framework locally

No remote redistributable is assumed. Supply the pristine beta3 framework when building the native preparation bundle:

```sh
node scripts/build-d3dmetal-autopatch.mjs build/d3dmetal-autopatch \
  --framework "$GPTK_FRAMEWORK" --accept-apple-license \
  --sidecar-module build/native-sidecar/libD3DMetalSidecar.dylib \
  --sidecar-manifest build/native-sidecar/build-manifest.json
```

The resulting helper uses a local Apple beta3 disk image and the bundled sidecar:

```sh
build/d3dmetal-autopatch/prepare-d3dmetal-runtime \
  --dmg "$GPTK_DMG" --output build/prepared-d3dmetal \
  --accept-apple-license
```

The helper verifies the input, patch and signatures before publishing output. Disk images are mounted read-only. Direct framework input is also supported with explicit `--framework`, `--license` and `--acknowledgements` paths. Yaagl must obtain Apple license consent before passing the acceptance flag. Preparing local artifacts does not install or replace a live Yaagl runtime.

## Stage a complete local runtime

`GPTK_RUNTIME` is a pristine beta3 Wine graphics distribution containing `lib/external` and the stock PE/Unix graphics modules. It is separate from the Wine host just built. Supply the dependency library directory, GStreamer framework and original native FSR frame-generation DLL:

```sh
python3 scripts/stage-full-wine-runtime.py \
  build/wine-p3-current/host "$GPTK_RUNTIME" build/native-sidecar build/staged/wine \
  --dependency-lib "$DEPENDENCY_LIB" \
  --gstreamer-framework "$GSTREAMER_FRAMEWORK" \
  --native-fg "$FSR_NATIVE_FG_DLL" --accept-apple-license
python3 scripts/stage-runtime.py --verify-runtime build/staged/wine --current-sources
```

The staged launcher uses `yaagl-d3dmetal-runtime.json`, enables Metal4/MetalFX, and selects the RTX 4060 adapter identity. `YAAGL_FSR_UPSCALER=metalfx` selects the translated upscaler; `native` retains the native upscaler while using the packaged frame-generation bridge. The stock NGX pair remains the DLSS route.

`package-wine-p3-runtime.sh` packages the complete local runtime. `package-yaagl-runtime.py STAGED_WINE_ROOT AUTOPATCH_DIR OUTPUT_DIR` instead packages a verified staged runtime with the native preparation helper and omits the Apple framework. The split-package entrypoint uses the same beta3 contracts. Apple framework redistribution remains subject to Apple's license.

## Verification

Run the owner tests for the changed native transport, patcher, preparation helper, staging and launcher. A successful build or CPU fixture does not prove game image quality or FSR frame-generation behavior. Report the exact runtime scenario separately from static and unit checks.

`python3 scripts/test-fg-normalization.py` checks FG dispatch and ownership, plus actual Metal4 motion-texture readback for jitter cancellation. The translator keeps per-frame normalized jitter history so omitted motion dimensions, render-size changes, reset boundaries and queued snapshots use the correct motion-vector domain.

Synthesized FG camera projections use MetalFX's reversed working-depth coordinates for both normal and reversed raw depth, including finite and infinite far planes. The raw-depth flag and explicitly supplied projection matrices are preserved. The camera regression exercises both public camera-packet producers over 56 native frames, checking emitted matrices, GPU-normalized inputs and final pixels. `FG_CAMERA_NATIVE_MASK=1` additionally reads the actual mover mask, gated to the reviewed MetalFX binary UUID. The comparison keeps its textured object within the view; a separate full-screen-plane experiment found native normal/reversed-depth differences at the outer image boundary, which this camera fix does not address.

`python3 scripts/test-fsr-native.py` exercises production SR and reads its intermediate and final GPU textures. Nonlinear color conversion uses floating-point working textures without reducing existing higher precision; hardware sRGB decoding remains owned by the texture format. When the requested output exceeds the temporal scaler's limit, display-resolution motion is resampled over its entire active region and its scale is converted per axis to the actual temporal output size. Render-resolution motion, uncapped motion, camera jitter and centered output placement keep their existing conventions. Tests cover dark sRGB ramps, hardware sRGB, BGRA, PQ, nonuniform motion, subrects, history and capped/uncapped transitions.

Depth and render-resolution motion are also sampled at `outputUV + currentJitter/renderSize` to align their jittered grids with stabilized color. Display-resolution motion remains on its stabilized grid. This spatial correction is independent of the flag describing jitter in motion-vector values. Nonuniform GPU readbacks cover both signs and clamped edges; the same suite reads final MetalFX output to check explicit UI composition, inferred HUDless/present composition, and no-UI color. Explicit premultiplied UI is composed once, after color encoding. These controlled inputs do not establish whole-game image quality.

History continuity also requires the same MetalFX interpolator instance: replacing an evicted instance resets history and the GPU-written interpolation-disable hint. GPU regressions cover that cache boundary, transparent/translucent alpha-mask reconstruction with independent origins, and generated output placement without touching surrounding pixels. Resource aliases share one transition/restoration pair; contradictory initial states or access roles are rejected before recording barriers.

The same suite checks SDK-enum-based FFX-to-D3D12 state conversion, native-provider passthrough, and forward/reverse barriers from the production recorder. Barrier capture exercises rejected transport; it is not a successful Wine/GPU submission test.

## Licenses

See [COPYING.LIB](COPYING.LIB) and [NOTICES.md](NOTICES.md). Apple GPTK has separate license terms.
