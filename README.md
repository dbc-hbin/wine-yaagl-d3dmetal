# zzz-wine-d3dmetal-dx12

**English** | [한국어 (Korean)](README.ko.md)

Wine 11.17 runtime source and a one-click GUI installer for playing **Zenless Zone Zero (ZZZ)** through **Direct3D 12 (Apple GPTK 4.0b2)** on Apple Silicon with **Yaagl ZZZ OS**.

The v1.1.0 public runtime identifies its graphics adapter as **AMD Radeon RX 9070** (`0x1002:0x7550`) and translates the game's FSR upscaling API to MetalFX. It does not ship a DLSS or NVIDIA NGX translation path. The installer registers **`Wine 11.17 ZZZ DX12 (GPTK4.0b2)`** in Yaagl's Wine menu.

**Requirements: macOS 26.0 or later on Apple Silicon and Rosetta 2.** Temporal upscaling uses the system-default MetalFX model on every Mac; the runtime does not force BBR or a private model version.

## Quick start

1. Download [ZZZWineDX12Installer.zip](https://github.com/dbc-hbin/zzz-wine-d3dmetal-dx12/releases/latest/download/ZZZWineDX12Installer.zip).
2. Extract it and open **`ZZZ Wine DX12 Installer.app`**.
3. Quit Yaagl and its Wine processes. Choose your launcher under **`Yaagl Target`**, then select **`Install Wine 11.17 ZZZ DX12`**. If the same runtime is already selected, the button reads **`Reinstall / Update Wine`**.
4. Start the selected Yaagl launcher and select **`Wine 11.17 ZZZ DX12 (GPTK4.0b2)`** from its Wine menu.

The installer detects the Yaagl application and support directories, installs its bundled archive, registers the runtime, and backs up Yaagl resources, the selected Wine, and the previous runtime directory. The archive remains in Yaagl's local runtime storage for offline selection, and the installer does not require Node.js.

A same-name runtime can be reinstalled. The bundled archive replaces both the cached archive and runtime directory instead of treating the matching name as proof that the files are current. If final Wine-selection activation fails, the installer restores the runtime and selection from immediately before that attempt without consuming the original restore backup. “Update” means replacing the runtime with the build bundled in the installer being run; it is not an online update check.

The current installer source (not yet included in existing release ZIPs) preserves a provable v1.0.5 forced-DX12 default on upgrade: it saves DX12 ON only when the old forced-launch rule is present, the same target D3DMetal runtime is selected and supports DX12, and no DX12 preference was stored. A saved OFF is never overwritten. Fresh and already settings-driven launchers retain their settings-based behavior; an ambiguous v1.1.x preference is not guessed from its value.

### Terminal installer

The target picker supports **Yaagl ZZZ OS**, **Yaagl ZZZ OS DX12 Beta** (global), and **Yaagl ZZZ DX12 Beta** (CN), including the [DX12 beta release](https://github.com/dbc-hbin/yaagl-ZZZ-DX12/releases). Each target uses its own matching `~/Library/Application Support/<launcher name>` directory; installing into a beta does not replace the stable launcher's Wine. Launch a newly installed Yaagl once to create its support directory, then quit it before using the installer. Stable is selected by default when installed; otherwise an installed beta is detected.

For a beta CLI install, pass `--app-path "/Applications/Yaagl ZZZ OS DX12 Beta.app"` (global) or `--app-path "/Applications/Yaagl ZZZ DX12 Beta.app"` (CN). Recognized app names infer the matching support directory; an explicit `--support-path` takes precedence for custom installations.

```bash
./installer/zzz-wine-installer --install \
  --app-path "/Applications/Yaagl ZZZ OS.app" \
  --support-path "$HOME/Library/Application Support/Yaagl ZZZ OS"

./installer/zzz-wine-installer --restore \
  --app-path "/Applications/Yaagl ZZZ OS.app" \
  --support-path "$HOME/Library/Application Support/Yaagl ZZZ OS"
```

## v1.1.0 public runtime

### FSR upscaling to MetalFX

- The published v1.1.0 wrapper fixes the graphics identity to AMD Radeon RX 9070 (`0x1002:0x7550`); it has no NVIDIA option. The **unreleased current-source** staged wrapper defaults to that same AMD identity and offers explicit `YAAGL_GPU_IDENTITY=rtx5060` (`0x10de:0x2d05`) for NVIDIA/NGX comparison. It applies FSR selection without a second helper and preserves Yaagl's `MTL_HUD_ENABLED` choice (including unset or empty). Published v1.1.x archives still contain the older helper that forces the HUD on; they must be replaced to gain this behavior.
- The builtin `amd_fidelityfx_upscaler_dx12` module implements the public FSR API boundary and translates accepted temporal-upscaling work to MetalFX. It does not execute AMD's FSR4 neural network.
- In newly staged runtimes, `YAAGL_FSR_UPSCALER=metalfx` (also the unset/empty default) selects that builtin upscaler; `YAAGL_FSR_UPSCALER=native` selects the game's original canonical upscaler DLL without a builtin fallback. This explicit SR comparison option survives the launch wrapper; it does not change the frame-generation provider policy. Other values stop before Wine launches. Existing release archives do not gain this option until rebuilt.
- Native AA and the Quality, Balanced, Performance, and Ultra Performance modes remain explicit game/provider choices. The translator does not silently select a quality mode. When a request exceeds MetalFX's maximum temporal scale, the MetalFX output is capped to a single uniform scale, centered in the caller's own output texture, and the surrounding texels are preserved.
- The game remains the source of truth for an explicit OFF selection. The runtime does not auto-enable upscaling or frame generation.
- Newly staged runtimes retain at most three inactive temporal scalers per FSR context for repeated output-size changes. Switching back resets temporal history; previously unseen sizes still create a scaler and may increase MetalFX/driver-accounted memory.
- Unreleased source reuses completed, descriptor-matched SR scratch textures and parameter buffers across dispatches. Each MetalFX feature retains at most 128 MiB of idle textures; caller-bound residency and argument tables are rebuilt per frame. This trades bounded idle memory for fewer allocations, not a demonstrated FPS improvement. Existing release archives do not include it.
- Unreleased SR also retains at most two descriptor-compatible reactive-mask-presence scaler variants per MetalFX feature. Returning to a cached variant resets history; format/layout/input-capacity changes evict incompatible variants, while in-flight frames and leases retain their resources. ARM64 Metal4 checks passed mask alternation, composition masks, input growth and output readback; this is not a measured game-FPS improvement.
- All Macs use the system-default MetalFX temporal model. There is no hardware-name heuristic, mandatory BBR policy, or private model-version override.
- FSR exposure, reactive/composition masks, transfer functions, sharpening, reset, jitter, motion-vector scale, and active input/output extents are translated explicitly. Invalid or unsupported contracts return an error instead of becoming successful no-ops.

### Frame generation and native fallback

- The automatic frame-generation provider selects MetalFX interpolation only when the actual command-buffer mode and Apple's matching support checks accept the request.
- The original FSR provider continues to own swapchain creation and wrapping, presentation timing, pacing, registered UI resources, custom present callbacks, and delegated swapchain queries.
- Explicit native-provider selection stays native. Unsupported translation contracts fall back to the original provider before MetalFX records work. Once MetalFX has recorded work for a frame, an error does not run a second native interpolation pass.
- The packaged runtime binds its private, read-only native fallback by absolute path, preventing recursive canonical DLL loading. The original loader and native fallback are not overridden.
- An explicit OFF state remains off. A lingering HUD label is not evidence that frame generation continued.
- Newly staged runtimes clear unconsumed frame-generation metadata when presentation is disabled. Recorded GPU work keeps its own resources until completion. With generation enabled, 64 distinct pending frame configurations are allowed; further configurations return a runtime error until a completion retires them, rather than retaining HUD-less resources without a bound.

### Translation contract details

- A logical D3D12 device is identified by `ID3D12Device::GetAdapterLuid`, not raw COM tear-off pointer equality. The command list and every resource must report the same adapter LUID; this validates the single-adapter runtime, not physical multi-adapter hardware.
- Backing textures larger than the current active input/output are accepted. MetalFX receives exact active-sized resources, GPU staging is used only when required, and output writes preserve texels outside the caller's active rectangle. Both low-resolution and display-resolution motion vectors follow this rule.
- Prepare V1 may omit camera information or supply its single optional camera extension; Prepare V2 carries camera data directly. Non-consecutive frame IDs and explicit resets reset history. Generation rectangles keep signed coordinates: only width and height both zero select the full display, and a partial rectangle maps its top-left to depth/motion coordinate (0,0).
- Frame-generation Prepare follows the pinned SDK camera defaults. Finite nonpositive `viewSpaceToMetersFactor` values use a scale of `1.0`, and `cameraFar` is ignored when infinite depth was selected at context creation. Finite-depth planes must be positive, finite, and distinct; their order is normalized with min/max. Reversed depth stays a separate flag, so inputs such as `cameraNear=5000`, `cameraFar=0.1` are valid without inverting the depth texture. Non-finite scales remain invalid.
- sRGB, PQ, and scRGB transfers preserve their defined luminance conversions and reject non-finite or invalid ranges. Disabled sharpening accepts an unused finite sharpness in `[0,1]` without running RCAS. Jitter phase queries use the pinned SDK's truncation (1600→2000 yields 12), and a null dispatch descriptor returns `FFX_API_RETURN_ERROR_PARAMETER`.
- Distortion fields, AMD debug shader views and tear/reset overlays, custom DX12 backend allocation callbacks, and more than one generated output per frame remain unsupported and return an error.
- Metal4 compute parameter buffers are included in residency before encoding. The configuration cache holds at most eight variants; luminance and rectangle-origin changes reset history without creating new factories, and evicted configurations are retained until in-flight work completes.
- Configure calls that only notify the swapchain reuse the immutable binding while the application callbacks and user contexts are unchanged, so they avoid unnecessary presenter drains; real callback changes retire the old binding through the native swapchain, and pending per-frame HUD-less snapshots survive ordinary Configure calls. This is a pacing/lifetime correction, not a measured FPS or image-quality improvement.

### Frame-generation verification boundaries

These items remain unverified risks; the path carries no FPS or image-quality guarantee until they are closed.

- Earlier synthetic checks used uniform depth and a single global motion vector. They did not exercise disocclusion or mixed foreground/background motion, and they did not reproduce the game's observed 2256×1272 render-resolution motion vectors feeding a 3840×2160 output.
- Depth and motion vectors are currently expanded with nearest sampling before interpolation. Whether this is better or worse than giving MetalFX its native low-resolution inputs requires an A/B comparison; it is not a known quality bug.
- The descriptor's nullable `scaler` is not linked. Apple's WWDC25 session 211 sample links a scaler, but that is an architectural difference, not proof of a quality defect here.
- Jitter units remain unresolved when the input color is already temporally upscaled. Do not blindly rescale the jitter or replace it with zero; first capture the producer's actual convention and compare temporally stable scenes.
- Logical scratch allocated per Generate is approximately 190 MiB without UI and 253 MiB with UI at 4K, based on the RGBA16F, R32F, and RG16F texture dimensions. These are logical allocation sizes, not measured resident memory, bandwidth, latency, or frame-time cost.
- Generation and generated-frame present logs each stop after 120 callbacks, not 120 game frames. They do not record the OFF transition or prove that generation stopped after it.

Next verification should use game captures with disocclusion and mixed motion at 2256×1272→3840×2160, A/B nearest-expanded versus native low-resolution depth/MV inputs, record the actual jitter convention, profile resident memory and GPU time, and explicitly observe OFF transitions and subsequent generation activity beyond the callback log limit.

### Optional, bounded logging

`YAAGL_FSR_LOG` is optional and must name an absolute path.

- Upscaling logs lifecycle/query/error events and successful dispatch metadata only for global dispatch IDs 1 through 120. Failed-frame detail has an independent cap of 120.
- Frame generation writes one `first_encode` JSON record to `YAAGL_FSR_LOG`. Frame-generation failures written to stderr stop after 120 records.
- The opt-in `WINEDEBUG=trace+yaagl_fsr_fg` channel records generation and generated-frame present callback results, each stopping after 120 entries.
- There is no unbounded normal per-frame log.
- Log entries report API, encode, or callback progress. They do not prove GPU completion, image quality, FPS, or an OFF transition.

### Unreleased opt-in cursor diagnostics

Build both `winemac.so` and `win32u.so` from current source; existing releases do not contain this feature. Recording requires `YAAGL_CURSOR_TRACE` in the Wine process environment. Cursor correction, clipping, and RawInput delivery policy remain unchanged.

```sh
trace_dir="$(mktemp -d /tmp/yaagl-cursor.XXXXXX)"
YAAGL_CURSOR_TRACE="$trace_dir" /path/to/diagnostic/wine /path/to/application.exe
python3 scripts/decode-cursor-trace.py "$trace_dir" --summary
python3 scripts/decode-cursor-trace.py "$trace_dir" --last-seconds 5 > cursor.jsonl
# Select a window around an event's clock_ns:
python3 scripts/decode-cursor-trace.py "$trace_dir" --around-ns 123456789000 --before 2 --after 1
```

- The output directory must already exist and be absolute. Files are created with mode `0600`. Configuration is read at process startup; omit the variable on the next launch to disable recording.
- Records cover the actual Confinement/EventTap backend, clip/focus/Retina transitions, warps and no-ops, EventTap correction, Cocoa deltas and timestamp filtering, queue merge/discard, Wine delivery, and application RawInput/position reads. Keyboard contents and game camera angles are not captured.
- Each loaded DLL in each process retains only its latest 65,536 records in **8 MiB + 128 bytes**. Files survive exit. There are no explicit file writes, additional threads, or allocations on the input path; diagnostic work and OS writeback of mapped pages still have a cost.
- Read from a separate terminal immediately after the symptom; older records roll out. Live reads are not globally atomic snapshots. The decoder reports contention drops, sequences outside the retained window, and unstable slots.
- `clock_ns` uses the common monotonic clock; source-event clock units are separately labeled. Pointer identities can be reused, so interpret them with sequence/lifetime context, not as automatic cross-process causal links. Repeated reads of a RawInput handle are not additional physical input. Confinement does not execute EventTap warp correction.
- Collector/decoder checks for disabled recording, wraparound, concurrent producers, fork isolation, and malformed files: `python3 scripts/test-cursor-trace.py`.

### Unreleased NGX restoration in current source

Shared Metal4 replay and legacy encode handling are restored directly inside `ngx-hooks.mm`, rather than retained as an outer compatibility wrapper. `bridge.mm` publishes the NGX replay/encode entrypoints directly in slots 17/18; they recognize and execute FSR-recorded commands before interpreting any NGX descriptor. The separate `d3dmetal-replay-hooks.{hpp,mm}` layer and its build entries have been removed.

The published v1.1.0/v1.1.1 runtime remains FSR-only. The **unreleased schema-5 candidate**, built additively on the current Wine source rather than rolling back to v1.0.5, restores the pinned stock GPTK `nvngx.dll` (SHA-256 `f6bc9d77fd1e898fec8c6339d367bd8e0f338992c9c0c66d59b30c6e9e0743e4`) and `nvngx.so` → `../../external/libd3dshared.dylib` (pinned target SHA-256 `d932330841e77682d47688641e0ac17049a2aff498deafac88921983dc16eedb`). The stage record includes their provenance, symlink target, and signed artifact inventory. No existing installer archive or game/prefix is changed.

Native layout **v14** publishes 25 dispatch entries (21 normal hooks plus four special hooks). The only NGX wrappers are shared replay/encode, retaining FSR-first routing and forwarding ordinary NGX commands directly to the original D3DMetal replay/encode. The NGX private-output shadow/copy adapter has been removed; FSR backend output handling is unchanged. NGX Evaluate/record observation detours, diagnostic controls, and optional exposure/temporal corrections have been removed; stock NGX handles those calls directly. Current FSR, display routing, GPU-completion/resource-lifetime and Wine MSync fixes remain, as do the system-default MetalFX model and RX 9070 default. The old frame-probe capture layer remains absent.

Earlier isolated runs exercised stock NGX in Metal4 and legacy modes with Metal API Validation disabled. Those synthetic harnesses have been removed; their reused output did not establish fresh pixels for every NGX evaluation. With validation enabled, the shared-output NGX fixture previously asserted `outputTexture must have private storage mode`; that known limitation remains. Production validation settings are unchanged, and neither validation compliance nor game stability, FPS or image-quality improvement is claimed. The matching format-14 D3DMetal patch and native sidecar are required; published release archives remain unchanged.

The current source retires completed execution leases without waiting for allocator Reset, while retaining owner-based fallback for unsubmitted work or failed callback registration. SR also reuses compatible scaler capacity for smaller active inputs, with history resets and synchronized edge staging. [Measured memory results and limits](docs/screenshot-sr-analysis-2026-09-23.ko.md) distinguish bounded allocation reuse from immediate physical release and from the unverified game-wide memory difference.

### Unreleased lifecycle corrections

- MSync leaves the borrowed alert index alone at thread exit. Cache exports have process-owned, one-shot IDs and are reclaimed after native process death. Rebuild `ntdll` and `wineserver` together: server protocol **968**, MSync Mach wire **3**; existing request numbers are preserved.
- macdrv detaches window state under the window-data lock, then closes the Cocoa window and releases queued surface events outside that lock, for both destruction and top-level-to-child reparenting.
- Native FG callbacks enter their owning context's callback scope, avoiding configure/dispatch lock inversion. Unchanged callbacks reuse a binding without allocation or presenter waits; obsolete bindings are reclaimed only after successful replacement and the required drain.
- FG bridge **v4** retires the exact completed frame after failed/skipped generation too. Older pending frames and recorded commands keep their snapshots. Rebuild the FG PE/Unix modules and native sidecar together.
- SR prepared frames retain their scaler activation number, preserving reactive-variant resets across delayed encodes and dropped activation frames.
- MF async commands start with an owned reference; source errors complete pending reads/seeks with failure. WM parser reinitialization disconnects before joining the reader thread on failure. IOHID startup publishes its state atomically without waiting for the run-loop mutex.

These current-source corrections do not update installed Beta or existing archives and do not establish a game-FPS improvement.

### Historical verification status

The following records earlier release checks, not current game acceptance. The synthetic harnesses described here have since been removed.

Verified on macOS 27 / Apple M5 Pro:

- **v1.1.1 fixes a registration failure** where a Yaagl frontend this installer had already registered, but whose hash-keyed restore backup was missing, was rejected with “changed without a matching backup”. Registration now recovers a marker-free restore baseline from that frontend by removing only this installer's own updater hook and catalog entry; unrelated catalog entries, the frontend version, and the local-archive install path are preserved. Unrecognized or modified hooks still fail closed without touching the frontend. The Wine runtime bytes are unchanged from v1.1.0.
- Reproduction on a copy of the reported state: the v1.1.0 helper exited 1 with the reported message and changed nothing; the v1.1.1 helper completed, recorded the recovered baseline, and left the registered frontend byte-identical. Re-registering published bytes is idempotent; a mutated hook argument still exits 1 with the frontend preserved.
- The re-extracted full runtime passed FSR upscaling and frame-generation GPU checks in both Metal4 and legacy command-buffer modes, with Metal API Validation enabled.
- Normal-production DX12 graphics, compute, and ray-tracing GPU readbacks passed, including direct/indirect draws, blending, logic operations, MSAA, and duplicate/distinct D3D12 objects.
- MetalFX backend, quality, transport, and legacy-transport native suites passed. Native cache/stage-cache/key tests verified single-flight and object reuse, including graphics, compute, and RT key paths; production hit counters are not exposed or claimed measured. Actual DXGI enumeration of the production launcher reported `0x1002:0x7550`, **AMD Radeon RX 9070**.
- Core/backend archive reassembly matched the staged file inventory, bytes, modes, and symlinks. Signatures and isolated Wine initialization passed. The 45 declared tuned Wine core artifacts remain byte-identical to the verified v1.0.5 base; the FSR/native overlay was rebuilt.
- The re-extracted v1.1.0 installer ZIP passed deep/strict signature checks and all nine installer/update/restore/activation-failure scenarios using the actual preserved v1.0.5 archive. Three DX12 launch regressions and the relocated FSR launcher regression passed.
- The v1.1.1 installer ZIP passed deep/strict signature checks, bundles the unchanged v1.1.0 runtime, and passed all ten resource lifecycle scenarios, including the new lost-backup recovery scenario.
- The cursor ownership/RawInput source harness and isolated Win32 cold-start/layered-window cursor metadata checks passed. These are not native cursor-pixel or physical RawInput measurements.

**Limits:** Physical macOS 26 execution, native cursor pixels, and the first physical RawInput delta remain unverified. Apple’s original `libdxccontainer.dylib` is unchanged and still records minimum macOS 26.4. With optional Metal API Validation enabled, the generic MSAA resolve control triggers the same render-target-usage assertion in both untouched v1.0.5 and v1.1.0; this baseline validation limitation is not claimed fixed. Normal-production GPU readbacks pass. The frame-generation quality/performance limits above still apply.

## Historical release notes

### v1.0.5: rebuilt cursor/RawInput runtime and same-name upgrades

- v1.0.5 rebuilt 45 Wine artifacts from fresh build directories, including the cursor ownership/RawInput separation change. Seven rebuilt native modules targeted macOS 26.0 using SDK 26.5; the remaining Wine files came from the pinned P3 package. Other tuned patches, native PSO caching, and `D3DM_MTL4=1` were unchanged.
- Apple’s original `libdxccontainer.dylib` was retained byte-for-byte for D3DMetal DXIL container parsing and DXBC/HLSL conversion, including its recorded minimum version of 26.4. No 26.4-only imported API was identified. Wine configuration and isolated DX12 graphics, compute, and ray-tracing GPU readbacks passed on macOS 27; execution on macOS 26 hardware was not verified.
- The bundled Wine included `db45a95`: cursor ownership synchronization no longer changed pointer coordinates, and corrected RawInput deltas traveled independently. Matching Wine client/server modules were rebuilt together for server protocol **966**.
- The Wine menu name and runtime ID stayed unchanged. The v1.0.5 installer replaced an existing same-name runtime and cached archive with the new build while preserving other Wine catalog entries.
- Failed final activation restored the runtime and selection from immediately before the attempt without consuming the original restore backup.
- The extracted release ZIP passed nine installer/update/restore scenarios, three DX12 launch regression checks, and an upgrade from the previous protocol-965 archive that replaced all four coupled native modules (`wineserver`, `ntdll`, `winemac`, `win32u`).
- Cold-start cursor requests, layered-window ownership, and the post-Escape capture transition were exercised in isolated Wine windows. Those captures did not establish macOS native activation or custom-cursor pixels. Synthetic pointer input produced no physical RawInput callback, so native cursor pixels and the first physical mouse delta remained unverified.
- The earlier game-cursor fix remained present. The previously reported native-overlay P2 classification was withdrawn because source-level arrow-setter calls alone did not establish an unintended native cursor overwrite.

### v1.0.4: DX12 launch argument delivery

- v1.0.4 preserved the selected distribution identity in the actual Wine runner and added `-use-d3d12` only for **`Wine 11.17 ZZZ DX12 (GPTK4.0b2)`**. Earlier installers checked an absent runner `id`, so a successfully applied patch could still omit the argument.
- Game arguments were forwarded through both normal and Steam-patch launches. Other D3DMetal Wine distributions were not forced to DX12.
- The old ID guard and legacy backend-wide guard were replaced with the scoped condition; repeated installation did not duplicate the argument.
- These launcher fixes required the installer/update helper, not only the Wine archive. v1.0.4 retained the v1.0.2/v1.0.3 archive; v1.0.5 later replaced it with the rebuilt cursor/RawInput runtime. Physical Tahoe DX12 execution was not verified for those releases.

### v1.0.3: launcher updates and restore

v1.0.3 changed the installer only; its macOS 26 Wine archive and tuning were unchanged from v1.0.2.

- Registration patched the active `resources.neu` in Yaagl's data directory, not the app bundle. App resources and legacy app backups remained untouched.
- A native helper in `.zzz-wine-registration` registered the Wine in downloaded in-app updates before they replaced the active frontend. It ran only during installation or an in-app update; there was no background service and Node.js was not required.
- Restore used the pristine resource matching the currently registered generation instead of an older whole-resource backup.

If an older installer downgraded Yaagl, update Yaagl to the desired version, quit it, and run the current installer. Full app replacements or externally replaced resources can bypass the in-app hook, so run the installer again after those changes.

## Key runtime components

1. **Direct3D 12 and GPTK 4.0b2** — D3DMetal and Metal IR translate Direct3D 12 rendering to Metal.
2. **Native ARM64 wineserver** — avoids running the server through Rosetta and reduces synchronization/IPC overhead.
3. **MSync fast paths** — map Windows synchronization primitives to lower-overhead macOS mechanisms.
4. **Native PSO cache** — deduplicates shader compilation and retains compiled pipeline state objects across the device lifetime.
5. **Cursor ownership and RawInput separation** — keeps ownership synchronization independent from pointer coordinates and carries corrected motion deltas separately.
6. **Media, audio, window, and resource tuning** — retains the repository's GStreamer, Media Foundation, CoreAudio, window, and network patches.

Unreleased MSync fixes abandoned-mutex `WaitAll` nontermination and transactional rollback: pre-owned recursive mutexes remain owned, abandoned state is restored, and waiters are notified when acquired objects are returned. The 128-spin registration budget is unchanged. Deterministic race tests cover rollback and real pthread waiter wakeups; an isolated Wine API run passed abandoned status, finite recursive timeout/ownership and consume-once behavior. Both defects also exist in the directly inspected official CrossOver 26.3.0 FOSS source (Wine 11.0); this is not a claim about tested commercial CrossOver binaries.

## Repository structure

```text
zzz-wine-d3dmetal-dx12/
├── dlls/                   # Wine sources, including FSR upscaler/FG builtins
├── d3dmetal-pso-cache/     # Native PSO cache and FSR → MetalFX backend
├── external/               # Local GPTK framework input
├── include/                # Shared Wine/FSR bridge headers
├── installer/              # SwiftUI installer and CLI source
├── patches/                # Wine tuned and P3 patch series
├── scripts/                # Build, verification, staging, and packaging tools
└── server/                 # Native wineserver and MSync implementation
```

## Building from source

### Prerequisites

- macOS 26.0 or later on Apple Silicon, Rosetta 2, and a macOS 26 SDK
- Xcode Command Line Tools
- LLVM MinGW toolchain
- Bison, pkg-config, and GStreamer dependencies
- Prepared P3 source/host/dependency/provenance inputs, a local GPTK overlay, and the Steam helper payload; this repository does not download those external inputs

### Runtime and installer

```bash
export WINE_P3_ROOT="/absolute/path/to/prepared/wine-p3"
export YAAGL_STEAM_HELPER_DIR="/absolute/path/to/protonextras"
export GPTK_SOURCE="/absolute/path/to/gptk-overlay/wine"
export MACOSX_DEPLOYMENT_TARGET=26.0
export SDKROOT="/path/to/MacOSX26.sdk"
export WINE_PACKAGE_NAME=wine-11.17-zzz-dx12-gptk4b2-macos26
export WINE_RUNTIME_ID=11.17-zzz-dx12-tuned-stage-parallel-cache-warmup-cursor-rollback-gptk4b2-arm64server

./scripts/build-wine-tuned.sh all
./scripts/package-wine-p3-runtime.sh build/wine-tuned/host "$GPTK_SOURCE" \
  build/wine-tuned/provenance.json build/wine-tuned/package
./installer/build.sh
```

### Upstream Yaagl integration

The [Yaagl PR #759](https://github.com/yaagl/yet-another-anime-game-launcher/pull/759) integration consumes the split pair: Yaagl downloads and caches the backend separately and installs it into the extracted core `wine/` directory before Wine initialization. The pair is matched; it is not a general Wine/backend compatibility guarantee. The all-in-one archive and GUI installer remain the supported self-contained path. In the upstream integration, ZZZ's DirectX 12 option is off by default and is enabled only for a distribution that declares `supportsD3d12`.

### v1.1.0 release assets

v1.1.1 republishes the v1.1.0 runtime unchanged; only `ZZZWineDX12Installer.zip` differs. The runtime archive names below are therefore still used in the v1.1.1 release.

|Asset|Contents|
|---|---|
|`ZZZWineDX12Installer.zip`|GUI installer and the full runtime archive below|
|`wine-11.17-zzz-dx12-gptk4b2-macos26.tar.xz`|All-in-one runtime (`wine/` root)|
|`wine-11.17-zzz-core-macos26.tar.xz`|Split core archive (`wine/` root)|
|`d3dmetal-gptk4b2-zzz-v1.1.0.tar.xz`|Split backend overlay (relative `lib/`)|

Each archive has a `.sha256` sidecar. By default, `installer/build.sh` reads the full runtime archive at `build/release-v1.1.1/wine-11.17-zzz-dx12-gptk4b2-macos26.tar.xz` (override with `RUNTIME_ARCHIVE_SOURCE`).

```bash
# Use a verified current schema-4 source runtime and pinned external inputs.
# This creates a new candidate; do not stage over an installed runtime or game prefix.
WINE_ROOT=/absolute/path/to/new-candidate/wine
WINE_SOURCE=/absolute/path/to/current-schema4-runtime/wine
D3DMETAL_INPUT=/absolute/path/to/verified-stage-locked-D3DMetal
NGX_DLL=/absolute/path/to/pinned-gptk/nvngx-on-metalfx.dll
NATIVE_BUILD=/absolute/path/to/native-build
OUTPUT_DIR=/absolute/path/to/split-output
(
  set -e
  # The input must pass the current pinned D3DMetal layout inspection.
  # An exact pristine input is accepted by this option as well.
  python3 scripts/stage-runtime.py --wine-source "$WINE_SOURCE" --wine-dest "$WINE_ROOT" \
    --pristine-d3dmetal "$D3DMETAL_INPUT" --ngx-dll "$NGX_DLL" \
    --build-dir "$NATIVE_BUILD" --play --fsr-translator

  # Refresh inherited P3 metadata from the read-only current source runtime.
  python3 scripts/refresh-staged-runtime-metadata.py \
    --tree "$WINE_ROOT" --base "$WINE_SOURCE" \
    --native-manifest "$NATIVE_BUILD/build-manifest.json"

  # Split packaging verifies schema 5 against current sources, then reassembles
  # and smoke-tests the pair in a private Wine prefix.
  sh scripts/package-wine-runtime-split.sh "$WINE_ROOT" "$OUTPUT_DIR"
)
```

The exact archive hashes belong to the parent release notes and are not asserted here.

### Display output and refresh routing

The earlier native patch format 12 introduced output selection by swapchain HWND rather than adapter output 0; current format 14 retains that routing. Windowed routing follows monitor moves; an explicit fullscreen target takes precedence until returning to windowed mode. Output migration preserves swapchain registration and reference ownership. Before Present pacing, the Wine bridge queries `ENUM_CURRENT_SETTINGS`, not the saved registry mode. `SyncInterval=0` and an explicit `D3DM_MAX_FPS` retain their existing behavior; no refresh rate or frame cap is forced.

Rebuild Wine with the current P3 patch before staging. `stage-runtime.py` requires the separate `macdrv_query_d3dmetal_display` export in `winemac.so` and records/verifies that binary's identity. Replacing only the native sidecar in an old runtime is not sufficient. The fixed 192-byte Wine callback table is unchanged.

The fullscreen repair also requires repatching D3DMetal, not just rebuilding the sidecar. Both its direct Windows-ABI vtable thunk and unixcall unpacker now forward the explicit output argument and preserve the native HRESULT. Routing resolves the native output interface instead of calling back through the PE `GetDesc` vtable. An earlier D3D12 regression exercised explicit fullscreen, state/output queries, and windowed return even on a single monitor; baseline and saved/current-refresh split runs passed without changing the physical display mode.

The isolated D3D12 regression reproduced CURRENT=120 Hz / saved=60 Hz: the old swapchain reported 60 Hz; the corrected one reported 120 Hz and requested an 8.333 ms minimum Present duration instead of 16.667 ms. SyncInterval 0 requested no minimum; an explicit 30 FPS cap still requested 33.333 ms. GPU pixel readback passed. The paired format-12 runtime additionally passed real Wine movement and explicit fullscreen between reported 60/120 Hz outputs, plus a CURRENT=60 Hz / saved=50 Hz split with Metal API/GPU validation. This is not a game-FPS improvement claim.

Window surface arrays own their references explicitly, without CFArray release callbacks. Removing an entry transfers its reference; individual release and window-destruction draining happen after unlocking `win_data_mutex`, avoiding the reverse acquisition of `surfaces_lock`. An earlier focused regression covered opposing lock acquisitions and reference balance through view-creation failure and window destruction. A real D3D12 smoke completed 128 swapchain lifetimes with 128 concurrent window resizes, verified GPU pixels, and released a retained view after destroying its window. That earlier lock-lifetime smoke used a separate queue per surface.

The earlier format-12 patch also scoped drawable residency registration to the final Metal4 commit/signal/present operations in `DoPresent`, then removes only that registration; existing D3DMetal resource owners and baseline queue registrations remain unchanged. A pinned real-Wine probe passed 132 Present lifetimes plus four no-Present controls on one queue with 136 valid pixel readbacks. All 132 late presentation callbacks still exposed the original layer/residency set and a live drawable texture after registration removal. This fixes queue-registration accumulation, not a measured game-FPS or memory improvement, and requires a matching repatched D3DMetal/sidecar pair. An earlier same-queue regression also passed 128 swapchain lifetimes with resize coverage and 256 valid pixel readbacks under Metal API/GPU validation.

### Essential safeguards and real-game validation

Current source keeps only two standalone project checks:

- Binary patch safety: reject partial, corrupted, unsupported, or already patched inputs and preserve unrelated bytes.
- Installer safety: preserve user files and restore the prior runtime/selection after failed installation or activation.

```bash
node --test scripts/metalir-fp64-codec-patch.test.mjs
# Requires a built installer with its runtime archive and an explicit stock resource fixture.
python3 scripts/test-resource-lifecycle.py --stock-resource /path/to/stock/resources.neu
```

The installer check uses temporary app/support copies, not the installed game prefix. Exact binary identity, layout, signature, ABI/export, source/provenance and archive-reassembly checks remain in the build/staging/packaging tools. Split packaging retains its private-prefix Wine initialization check. These are packaging safety checks, not graphics acceptance.

Project-specific NGX/FSR/MetalFX rendering, image-quality, cache, cursor, synchronization and launch-profile test harnesses and their test-only build controls have been removed. Upstream Wine tests/CI and historical evidence under `docs/evidence/` are unchanged.

Accept graphics changes only in the actual game: compare the same scene and settings for frame-time stability, memory and image quality; exercise FSR and FG OFF→ON→OFF, cursor/menu-to-camera transitions, fullscreen and monitor changes. Use an isolated candidate. Synthetic PASS logs, API success and successful builds do not establish game correctness or performance.

## License

- Wine source code is licensed under the **GNU Lesser General Public License (LGPL v2.1+)**.
- D3DMetal bridge components and installer tools use the terms included in this repository.
- The vendored FidelityFX SDK headers under `d3dmetal-pso-cache/third-party/fidelityfx/` retain AMD's MIT license text and copyright notice.
