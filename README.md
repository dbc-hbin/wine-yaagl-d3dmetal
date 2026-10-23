# Wine 11.20 D3DMetal (GPTK 4.0b3)

[한국어](README.ko.md)

Yaagl runtime source for Apple Silicon and Rosetta 2, targeting GPTK 4.0 beta 3 exclusively. Wine host, msync, input, media, audio, window and resource fixes are retained. The native sidecar supplies display routing, video-memory policy and FSR-to-MetalFX translation. GPTK's native caches remain responsible for shader, library, PSO and RT function reuse.

DLSS uses Apple's stock `nvngx-on-metalfx.dll` and Unix module. This tree does not include the experimental DLSS translator, Streamline interception or NPC-nameplate composition. No nameplate fix is claimed.

## Current source and patch policy

This tree starts at the official `wine-11.20` tag and ports the selected runtime changes from verified source `1846c16674b`. The Wine sources in `dlls/`, `server/` and `include/` are the build inputs. See [the migration record](documentation/WINE-REBASE.md) for retained contracts and upstream overlap decisions. Historical P3/tuned patch queues remain in the source commit as reference material; they are not reapplied or copied into this tree.

`d3dmetal-sidecar/layout.json` defines the beta3 hook contract: format 27, marker `YAAGLB3SIDEcar\x1b`, four compact dispatch slots and no additional `binaryPatches`. The entry hooks, in dispatch order, are `GetContainingOutput`, `SetFullscreenState`, `QueryVideoMemoryInfo` and `ReplayTemporalScale`. They cover display routing, video-memory queries and FSR replay only. `ngx-hooks.mm` dispatches recorded FSR commands through `ReplayTemporalScale`; other commands use Apple's original replay. FSR SR/FG uses the Metal4/MPL transport.

The current Wine FSR entrypoints and native regression callers select Metal4; no in-repository caller selects the backend's Legacy command mode. The SR C++ interface still accepts Legacy with an ordinary `MTLCommandBuffer`, and the sidecar exports its C++ factory/encode symbols, so repository usage alone does not prove external callers absent. Legacy is retained pending an explicit interface cutover. It is separate from AMD native-provider passthrough: `YAAGL_FSR_UPSCALER=native` and FG native fallback do not select MetalFX Legacy.

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

### ZZZ Game Mode host

The full-source build also links `libexec/yaagl-game-mode/ZZZ.app/Contents/MacOS/wine` from that build's existing `loader/main.o`. `scripts/game-mode-host.py` reuses the generated loader link recipe, changing only the output and embedded plist; it does not copy a loader implementation or assume an older Wine address-reservation layout. Wine 11.20's low-8GB reservation and configured flags remain authoritative. The loader resolves this fixed bundle location to the same runtime's `lib/wine/x86_64-unix/ntdll.so`, without an environment-selected library or an external symlink inside the signed app.

The embedded and bundle plists are identical: `io.github.yaagl.zzz-game-host`, Games category, `LSSupportsGameMode=true`, `WineApplication`, and no `LSUIElement`. Install and staging sign the bundle, and staging checks its executable, both plists, relative ntdll resolution and signature before inventorying final bytes. Both local and framework-free package manifests advertise `gameMode: {"version":1,"host":"libexec/yaagl-game-mode/ZZZ.app/Contents/MacOS/wine","target":"zzz"}`; their existing hashes and symlink inventories remain authoritative.

The launcher opt-in is for ZZZ on Apple Silicon/macOS 26 or later. `YAAGL_GAME_MODE_TARGET` is the absolute Unix path of the selected game executable; only the matching opened Windows image is routed to the host. `YAAGL_GAME_MODE_ACTIVE=1` is internal to that selected process, not a launcher-wide switch. The normal renderer wrapper still runs first. Off uses ordinary Wine without forcing macOS policy off, and explicit windowed settings are preserved. Host entry is not proof that macOS activated Game Mode: macOS retains that decision. No global `gamepolicyctl`, ForgePlay code, or additional Apple framework distribution is involved.

Native fullscreen preserves the latest Wine frame request across transitions. A different destination monitor causes an exit, applies the requested frame through the existing exit callback, and re-enters fullscreen; a user-requested exit still suppresses automatic re-entry. The existing display-change reassertion hook also discards stale frame events while application is deferred. `python3 scripts/test-game-mode-fullscreen.py` executes the production Objective-C methods against synthetic screens without changing desktop Spaces. It covers transition ordering and failure paths, but does not replace physical multi-monitor QA.

After a complete build/stage, maintainers can check the host with `python3 scripts/game-mode-host.py validate build/staged/wine` and `codesign --verify --strict build/staged/wine/libexec/yaagl-game-mode/ZZZ.app`. Runtime verification below includes the same metadata and signature checks. Actual Game Mode activation still requires the Steam → ZZZ fullscreen scenario and observation of macOS's Game Mode UI.

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

Explicit `wineboot` and `wineboot.exe` launches skip optional Mono/Gecko installer dialogs so unattended prefix initialization can finish. This applies only to that process tree, preserves the FSR loader policy, and writes no persistent DLL-disable settings. Ordinary application launches do not inherit these bootstrap-only disables.

`package-wine-p3-runtime.sh` packages the complete local runtime. `package-yaagl-runtime.py STAGED_WINE_ROOT AUTOPATCH_DIR OUTPUT_DIR` instead packages a verified staged runtime with the native preparation helper and omits the Apple framework. The split-package entrypoint uses the same beta3 contracts. Apple framework redistribution remains subject to Apple's license.

## Verification

Run the owner tests for the changed native transport, patcher, preparation helper, staging and launcher. A successful build or CPU fixture does not prove game image quality or FSR frame-generation behavior. Report the exact runtime scenario separately from static and unit checks.

FSR's overhead reductions preserve translation inputs, precision, ownership and history/reset decisions. Pass consolidation uses explicit Metal4 visibility barriers and format-specific GPU comparisons. SR shares its embedded shader library within a feature; FG shares its library and immutable compute pipelines across configurations on the same device within a context, while keeping interpolators and history identities separate. Successful Unix-side API resolution uses acquire/release publication without taking the loader mutex again, while failed resolution remains retryable. FG wakes destruction waiters only on the final closing-context reference and reserves retained-object storage before GPU encoding.

Temporary COM device references use scoped ownership without a shared control block; persistent device ownership is retained. SR reuses the validated output layout. FG reuses mode reads within the configure lock, refreshing after native selection, and preserves an identical HUD-less reference while updating its metadata. Identical scene/present textures share one raw view within an execution. FG packs its constants into one immutable execution-owned buffer, including the offset used for the GPU-written disable hint. SR argument tables travel with execution leases and become reusable only after lease retirement; bounded idle pools support different frames, optional bindings are cleared, and dormant features cannot refill the pools. No live lease shares mutable tables or constant storage.

FG uses one execution-local argument table for all translation passes. Each dispatch snapshots fully refreshed bindings, and simultaneous executions retain separate tables. SR creates only pipelines required by the current operations and depth format, adding others when later frames need them; debug output does not allocate unused normal-finish constants. The transport stores its first replay lease inline, including empty or failed attempts, and retains overflow leases for subsequent replays. Its C++ owner state is embedded in the Objective-C recorded owner; automatic destruction preserves resource and lease ownership until allocator retirement.

Metal4 SR batches independent exposure, motion, color and mask preprocessing in one encoder. Generated color padding copies already-converted active edges in that encoder after a dispatch visibility barrier, rather than repeating nonlinear transfer math. Mask generation fills its clamped padding directly. Independent source origins, floating-point precision, high-water input capacity and Legacy padding remain intact.

Metal4 FG batches baseline copies and preprocessing, with blit-to-dispatch visibility and a fence before MetalFX. R32Float depth/motion normalization shares a dispatch while retaining separate sampling grids, jitter corrections and parameter blocks; other depth formats keep the stored-depth path. Inferred scene/UI decode shares a dispatch with independent origins. Explicit UI composition and final output scatter remain separate.

Each FG context can retain one retired scratch bundle: normalized inputs, generated output, optional inferred UI/distortion, constants, argument table and an emptied residency set. Input snapshots and current/previous scene history are excluded. A live lease owns its bundle exclusively; OFF/DESTROY clears the idle slot, and epochs prevent late retirement or stale replay from refilling it. Native pending-frame metadata and PE configuration snapshots use bounded 64-entry storage without per-frame container allocations. Full IDs, backpressure, newest-preceding configuration lookup and out-of-order retirement are preserved; PE resource releases remain outside the dispatch lock. Texture ancestry is reused only within one dispatch, retaining role-specific mapping and alias rejection.

GPU regressions include 33 bitwise split/fused preprocessing comparisons, generated-padding readback, overlapping leases, configuration changes and OFF/DESTROY retirement. Registry tests cover 64/65 capacity, sparse IDs, failed recording and resource aliases. Generated-output baseline copies, direct-output policy, SR high-water generations and Configure synchronization are intentionally retained: removing them changes failure results, pixels or synchronization semantics.

The native FG regression injects exceptions at the first prepass pipeline binding and at fence finalization on real Metal4 encoders. It submits the partial command buffers and checks baseline pixels, unchanged history, lease retention, scratch retirement and successful retry of the same prepared frame.

FG Prepare and Generate share the input snapshot and its production state; each recorded execution owns its configuration, avoiding a snapshot clone and a separate production-state allocation. R32Float requires only the fused depth/motion pipeline, while other depth formats require the split pipelines. Missing required shaders still reject configuration and can be retried. Named compute texture bindings preserve the existing Metal4 slot clearing and Legacy bindings.

Metal4 FG uses 16x8 threadgroups only for the measured 1920x1080 render to 2560x1440 output profile; other profiles and Legacy retain 8x8. Separate per-context idle slots retain at most one depth snapshot, motion snapshot and scene texture each, with exact device/format/extent/usage matching. Shared owners keep snapshots alive through recorded producers, consumers and execution leases, and keep scene history alive through every writer and previous-history reader. Only the final owner can return a texture; OFF/DESTROY invalidates late returns by epoch.

SR RCAS reuses the center color already loaded by the finish pass instead of reading it again. Neighbor edge clamping, exposure multiplication, sharpening arithmetic, transfer conversion and output alpha are preserved. Native coverage includes translated active crops and centered padding with float32 output, exposure, all transfer functions and sharpness endpoints.

SR returns a completed bundle's scratch resources under one pool lock, after clearing residency, with the same byte cap and eviction order. Within a dispatch, resources whose device pointer matches the validated command device reuse its adapter check; different pointers still undergo the original LUID validation. Regressions cover delayed Prepare execution, shader failures and depth-format transitions, pool caps and dormancy, and device ownership and rejection boundaries.

`python3 scripts/test-fsr-lifecycle.py` exercises concurrent loader publication, failure/retry, the warm fast path, destruction wakeups, temporary/persistent COM ownership and HUD-less replacement/disable behavior. The native SR regression checks retained-lease isolation, retired-table reuse, exposure rebinding, dormant teardown and operation changes against fresh-feature output; the FG regression checks same-texture sRGB output, shared-pipeline lifetime through context destruction, and table isolation across repeated execution with UI and distortion. `node --test scripts/test-native-contract.mjs` exercises the production recorded-owner definitions with controlled backend boundaries, including retain failures, partial/empty encode failures and allocator-retirement teardown. These checks do not establish a measured game-performance gain or whole-game image equivalence.

FG checks Metal4 support once per Prepare/Dispatch invocation before configuration lookup or creation; configuration reuses that same validated command without a second capability query. The support check remains outside the configuration cache, including cache hits. The producer/native regression rejects a missing compiler before and after cache population without consuming pending frames or changing history, then verifies successful retry.

The FG callback-wait guard checks the descriptor type before reading TLS, so ordinary Prepare/Generate calls skip that lookup. WAIT_FOR_PRESENTS remains rejected inside callbacks, and the separate context-specific callback check remains in place. Dispatch regressions cover waiting outside callbacks and rejection inside them, plus ordinary callback Prepare/Generate.

`python3 scripts/test-fg-normalization.py` checks FG dispatch and ownership, plus actual Metal4 motion-texture readback for jitter cancellation. The translator keeps per-frame normalized jitter history so omitted motion dimensions, render-size changes, reset boundaries and queued snapshots use the correct motion-vector domain.

Generation uses the selected frame's Prepare snapshot and actual generated-history continuity; a newer pending Prepare does not itself reset an older valid frame. A producer-to-native regression feeds direct and callback packets through the native frame registry and real Metal4 encoding/readback, checking explicit resets, future-frame isolation and missing/consumed snapshot rejection. COM/D3DMetal transport and context setup are substituted; this is not a live Wine/game test.

Synthesized FG camera projections use MetalFX's reversed working-depth coordinates for both normal and reversed raw depth, including finite and infinite far planes. The raw-depth flag and explicitly supplied projection matrices are preserved. The camera regression exercises both public camera-packet producers over 56 native frames, checking emitted matrices, GPU-normalized inputs and final pixels. `FG_CAMERA_NATIVE_MASK=1` additionally reads the actual mover mask, gated to the reviewed MetalFX binary UUID. The comparison keeps its textured object within the view; a separate full-screen-plane experiment found native normal/reversed-depth differences at the outer image boundary, which this camera fix does not address.

`python3 scripts/test-fsr-native.py` exercises production SR and reads its intermediate and final GPU textures. Nonlinear color conversion uses floating-point working textures without reducing existing higher precision; hardware sRGB decoding remains owned by the texture format. When the requested output exceeds the temporal scaler's limit, display-resolution motion is resampled over its entire active region and its scale is converted per axis to the actual temporal output size. Render-resolution motion, uncapped motion, camera jitter and centered output placement keep their existing conventions. Tests cover dark sRGB ramps, hardware sRGB, BGRA, PQ, nonuniform motion, subrects, history and capped/uncapped transitions.

Depth and render-resolution motion are also sampled at `outputUV + currentJitter/renderSize` to align their jittered grids with stabilized color. Display-resolution motion remains on its stabilized grid. This spatial correction is independent of the flag describing jitter in motion-vector values. Nonuniform GPU readbacks cover both signs and clamped edges; the same suite reads final MetalFX output to check explicit UI composition, inferred HUDless/present composition, and no-UI color. Explicit premultiplied UI is composed once, after color encoding. These controlled inputs do not establish whole-game image quality.

History continuity also requires the same MetalFX interpolator instance: replacing an evicted instance resets history and the GPU-written interpolation-disable hint. GPU regressions cover that cache boundary, transparent/translucent alpha-mask reconstruction with independent origins, and generated output placement without touching surrounding pixels. Resource aliases share one transition/restoration pair; contradictory initial states or access roles are rejected before recording barriers.

The same suite checks SDK-enum-based FFX-to-D3D12 state conversion, native-provider passthrough, and forward/reverse barriers from the production recorder. Barrier capture exercises rejected transport; it is not a successful Wine/GPU submission test.

## Licenses

See [COPYING.LIB](COPYING.LIB) and [NOTICES.md](NOTICES.md). Apple GPTK has separate license terms.
