# Wine 11.20 runtime port

## Source identities

This tree starts at the official `wine-11.20` tag, commit `7298a818b79446bae153b8d5aa27235ab3703bf9`. The selected runtime source is `1846c16674bb50a11cf81fba67b884e537f3d075`, whose original Wine baseline is `913e31f201d344223bdf3d13a50a41af35893d12`. The baseline object is available and is an ancestor of Wine 11.20. The runtime source has imported-root history, so it is not rebased by replaying that root over upstream. Its actual baseline-to-source changes are reconciled with the 11.20 tree.

The source inventory contains 201 paths: 111 added and 90 modified. These include tests, vendored headers, documentation, four generated files and 21 historical patch-reference files, not 201 independent runtime features. The old target build outputs were discarded before implementation; no 11.17 host, server, sidecar or generated header is reused as a 11.20 build result.

Historical `patches/wine-p3/` and `patches/wine-tuned/` remain available in the selected source commit. They are not copied or applied here: they describe intermediate states and are not a complete current patch queue. Generated `configure`, `include/wine/server_protocol.h`, `server/request_handlers.h` and `server/request_trace.h` come from this target's inputs and generators.

## Retained contracts

### Host, Rosetta and synchronization

- Retain the current paired client/server MSync implementation, not stock CX or the separate CX 26.3 / Wine 11.0 A/B branch. The reference metadata attributes the original machinery to CX FOSS 26.3.0 and the pinned public carry; this is not a claim of independent authentication of the original archive during this port.
- Shared mapping uses wire version 2, fixed 16 KiB pages and 16-byte objects regardless of the client's native page size. Keep generation-bearing registration/cancellation, 128 registration spins, thread cleanup before tid reuse, WaitAll abandoned-owner/rollback/deadline/APC behavior and shared-index versus Unix-fd ownership.
- Preserve the user-requested selective rollback: no per-process `export_id` ledger, no extra final-release server RPC and no SIGKILL-on-exit path. Client final release sends the header-only Mach close message. An unclosed object can retain one 16-byte slot until wineserver exits; this is the existing accepted tradeoff, not a new cleanup guarantee.
- Keep the x86-client support in the optional ARM64 wineserver. Rebuild both sides together.
- Preserve the Apple pthread GS overlay with TEB Self/TLS/PEB mirrors, dynamic TLS updates, localtime TLS protection and thread teardown. Replacing it with GSBASE=TEB previously broke native GPTK worker initialization.
- Retain the PE `__wine_unix_call` export, native-code region registration, `WINEDLLPATH_PREPEND`, Rosetta instruction/fault/code-invalidation and WoW64 behavior, `WINENCPU`, macOS `pipe2` availability handling and Darwin MEM_RESET discard semantics.
- Integrate these into 11.20's startup, late PEB initialization and virtual-memory reservation implementation. Do not restore old ntdll files wholesale.

### ForgePlay optimization comparison

The dynamic unwind registration cache in `dlls/ntdll/unwind.c` is adapted from
`wine-11.12-dynamic-unwind-registration-cache.patch` in the
[ForgePlay v2.4.1 build 12 source release](https://github.com/Facta-Leopard/ForgePlay/releases/tag/v2.4.1).
The source archive SHA-256 is
`fdda327e07852186f96d88de8f60ae90ba3b6f2625f8c255aca2eeb558161394`.
This port keeps one live non-callback registration for an exact PC under the
existing critical section. Successful registration, growth and deletion
invalidate it before releasing the lock. The first matching registration still
wins; callbacks run on every lookup and caller-owned function data is searched
afresh. The ForgePlay-specific environment switch is not imported.

An x86_64/Rosetta `-O2` comparison used the actual before/after registration and
function-search code with a pthread critical-section substitute, 100,000 lookups
per thread, and four runs per variant in ABBA/BAAB order. At 256 registrations,
repeated lookup of the last registration fell from median 223.35 to 10.145 ns/op
with one thread and from 542.29 to 35.035 ns/op with four threads. Mixed-PC and
miss cases do not receive this benefit; the 1,024-registration, four-thread miss
case was 5.01% slower in this sample. These are registry microbenchmarks, not
public-API latency, frame-time or game-FPS measurements. Raw results are retained
locally in `build/forgeplay-candidates/unwind/results.json`.

Both x86 and x64 PE ntdll builds passed. The x64 public unwind suite ran against
the rebuilt ntdll in a separate prefix: 455,171 checks, no failures or skips.
Extended dynamic-table tests cover repeated-PC caller-data changes, deletion
after lookup, and repeated callback invocation. The native fixture additionally
exercises overlapping registrations and concurrent registration/deletion.

Other candidates are deliberately not duplicate implementations:

- FG already reuses descriptor-matched textures, execution scratch and MetalFX
  configurations with lease-based retirement. Real private R32Float texture
  acquire/retire measurements at 1920x1080 and 3840x2160 were respectively
  3,822.8/4,023.5 ns for new allocations versus 76.2/75.4 ns for existing reuse.
  This measures CPU allocation overhead, not GPU time or a new improvement.
  The seven production FG normalization/native Metal regression tests passed.
- ForgePlay owns presentation deadlines for its over-budget frame rejection.
  Our AMD presenter callback records work before later Metal replay, and the
  bridge has no remaining presentation deadline. `frame_time_delta_ms` is not
  that deadline; no guessed threshold or late replay-only rejection is added.
- ForgePlay's frame-pair optical-flow cache is not directly transferable to the
  MetalFX path using game-provided motion/depth and one generated output.
- The Apple pthread GS overlay and Game Mode support already exist. ForgePlay's
  optional diagnostic-file optimization has no matching normal per-frame write
  here. The policy supervisor's `fsync` and locks protect recovery/coordination,
  not diagnostics, and remain intact. Raw-device preflight is a stall-prevention
  change without a reproduced matching stall, not a demonstrated speed gain.

### Server protocol, surfaces, graphics and input

- Preserve upstream `unmount_device` and process/group-affinity protocol changes. The selected source adds only the native cursor window, `SET_CURSOR_OWNER`, and the `get_inproc_sync_fd` shared-memory index. Regenerate the target protocol tables; do not copy old request IDs or tables.
- Preserve shared-window-surface registration, dirty generation, clipping, lifetime and message handling together with the macdrv producer/consumer paths.
- The external D3DMetal bridge retains its 192-byte function table and 120-byte window-data ABI. Native helpers and Windows x64 DXGI callbacks must keep their distinct calling conventions. Do not change external layout assertions to conceal a merge error.
- Keep monitor/fullscreen routing, `macdrv_query_d3dmetal_display`, native cursor ownership, raw-input baseline and warp-consumption behavior while preserving 11.20 Objective-C types, IME interfaces and WineMetalSwapChain autorelease handling.
- Do not restore Game Mode's automatic native-fullscreen selection or transitions. Keep explicit fullscreen geometry/cursor handling separate from Game Mode. The signed host's `game-policy-supervisor` directly executes its bundled Apple-signed `gamepolicyctl` for a global override, validates its ANSI-colored status, and restores the previous policy after the last monitored game exits. Preserve the tool's original bytes/signature and privileged entitlement checks; never relocate or re-sign it, or reintroduce runtime Xcode/xcrun discovery. Preserve process-exit registration before mutation, same-user coordination and external-policy/recovery handling together.

### FSR and the native sidecar

Carry both `dlls/amd_fidelityfx_*_dx12` modules, their exports and Unix bridges, the `include/yaagl_*` contracts, `d3dmetal-sidecar/` and its vendored FidelityFX headers as one source contract. Preserve current SR/FG jitter, motion, depth, camera, color, UI composition, barriers and resource-lifetime behavior. MetalFX frame history and interpolator reuse remain; they are not the removed GPTK caches.

The SR and FG backends are Metal4-only; their factories require the matching device/compiler and have no Legacy mode or encode branch. FG retains provider-selection state and same-device validation without a command-mode enum. A composition-only R8Unorm mask may bind directly as the reactive mask only when its active rectangle and texture match the scaler input capacity and required usage. Keep reactive-mask enablement, format, composition offsets, residency and recorded ownership intact. Two masks, float masks, descriptor mismatches and reduced active input still use the max/clamp-and-pad pass. The native SR regression compares direct and combined masks through changing image history and capacity transitions.

Full-display FG motion shares the public generation rectangle's color crop without scaling its pixel displacement. Packed motion extents and independent subrects retain their own sampling domains; snapshot copies already consume input resource offsets. The rectangle regression covers nonzero origins with position-dependent camera reconstruction in both R32 fused and R16 split paths. Infinite-depth camera normalization ignores the unused far plane but still validates the near plane. SR jitter queries reject phase counts outside `int32_t`, normalize negative indices without signed overflow, and write either optional output independently.

FG Prepare reuses a cached same-device, same-depth-format configuration for snapshot requirements. The first eligible Prepare and a new depth format still validate native construction; changing render size alone must not fill the interpolation cache or reset output-sized history. Internal copy/compute helpers use typed Metal4 protocols and direct calls with macOS 26 availability, preserving shared-encoder barriers and lease ownership. Keep the generated-output baseline copy: it preserves present pixels when a later prepass fails, even when successful scatter would overwrite the whole output. The existing injected-failure regression covers this contract.

Copy-padded SR inputs opt out of GPU-optimized contents. Repeated extent changes exposed GPU-visible corruption during same-texture edge blits before MetalFX execution; broader barriers and `ShaderWrite` alone did not prevent it. Derive the opt-out from whether the copied input needs edge expansion, not its format or label. Full-capacity crops, compute-generated inputs and output scratch remain optimized. Include `allowGPUOptimizedContents` in scratch-pool matching so a full-capacity allocation cannot reenter a padded frame with the wrong policy. The native copied-input regression checks complete color, depth, motion and reactive extents through shrink/restore, independent-axis and odd-size transitions.

SR finish specializes sharpening with a Metal function constant and lazily retains the two pipeline variants per feature. Select the variant from the prepared frame's operations, not the latest configuration. Generated linear-color padding dispatches only the disjoint right and bottom strips; their immutable parameter records follow the linearization record in the same lease-owned buffer. Preserve the producer visibility barrier, omit empty strips, and never write active source pixels during padding.

SR preserves native MetalFX history across same-aspect active-input size changes within one scaler generation. Caller resets, generation reactivation and input-aspect changes still reset history. The native SR regression compares moving, jittered resize sequences with an independently configured MetalFX scaler, including explicit resets and aspect transitions. Do not confuse this history policy with input padding: direct smaller-active inputs on the tested macOS 27 implementation produced different output when only inactive texture contents changed, including nonzero offsets.

SR and FG helper shaders, including numerical exposure conversion, are compiled by `scripts/build-fsr-kernels.mjs` into an embedded metallib. The sidecar build records the Metal compiler, explicit MSL/deployment options and library digest; fixtures use the same generator. The dylib contains the bytes, so runtime packaging needs no loose shader file and no runtime Xcode/xcrun lookup or source compilation. Preserve lazy pipeline creation and finish-function specialization: metallib loading does not eliminate GPU-specific pipeline creation or MetalFX initialization.

Selected MetalFX contexts notify the bridge on an OFF-to-ON transition, not on every enabled configuration. Per-frame snapshots and native swapchain configuration still run, and repeated OFF still drains and clears state. Failed initial activation or snapshot publication retains its rollback. Disabled Prepare validates input and initial provider eligibility without allocating a prepared-frame implementation.

`d3dmetal-sidecar/layout.json` is beta3-specific format 34 with marker `YAAGLB3SIDEcar\x22`, no additional `binaryPatches`, and 38 dispatch slots. The original four retain their order:

1. `GetContainingOutput`
2. `SetFullscreenState`
3. `QueryVideoMemoryInfo`
4. `ReplayTemporalScale`

`CacheFileOpen` and `CacheFileClose` follow the existing latency gates. Their native SysV wrappers authenticate all Open/Close/View bytes, fix the zero-length payload unmap, normalize closed header/FILE fields, and retire both failed acquisitions and successful siblings before compiler retry or disabling. Preserve the original empty-path success and cache record/publication policy.

Nine fence gates follow the original 29 slots. Preserve copied-block completion ownership, close-only cancellation through Wine's Event provider, all-input registration commit, NULL-event semaphore ownership, public/unpack HRESULT propagation, native allocator pairing and both CPU lock order and shared-event notification semantics. The authenticated Metal4 queue descriptor setter separately balances feedbackQueue ownership; its copy/destructor methods remain native.

Carry SR/FG temporary-owner exception cleanup and publish-after-record Prepare semantics. Wine failed-view rollback must drop HWND ownership only after removing its own array entry, with releases outside the window lock. Keep the exact-provider NEW/HWND/WRAP allocation adapter separate from source-owned FSR3 effect initialization; never pass a partial source context to the native destructor.

Carry `fsr3-lifetime.cpp`, `yaagl_fsr3_lifetime.h`, `build-fsr3-lifetime.sh` and the pinned FidelityFX CPU source subset together with the PE loader and build/staging manifest changes. The helper has its own static C++ runtime and explicit Microsoft x64 provider table; do not rely on a MinGW class vtable matching the native compiler. Authenticate the native image before using shader/config/ABI helpers or replacing the FSR3 Create slot. Preserve original provider selection and the normal caller contract to retire callbacks/GPU work before destruction. Backend callback snapshots, partial-construction ledgers, upload ownership and transaction rollback are one ownership contract, not independent optional patches. Validate real native-shader output and failure-stage owner balances after a source or native-image update.

The CPU subset comes from AMD FidelityFX-SDK commit `0836aa7f24058b3f8d035a88c0774457d78fc98e`, with its original license notices retained. Shader-permutation implementations and the upstream provider selector are excluded. Debug-message dispatch also stays in the native DLL so its global debug configuration remains effective for both implementations.

The sidecar initializes exact-image MetalFX lifetime hooks before creating effects. FG uses the native owning-slot destructor after original object cleanup. BBR uses paired init/destructor ownership conversion, detaches its filter, and waits through notifications for both native prewarm-group and final internal graph-event completion. Keep failure paths, typed-delete pairing, image/class/ivar/IMP checks and recorded ownership together; neither a dealloc-only cycle fix nor an unconditional GPU wait is sufficient. The x86_64 repair leaves unsupported images and ARM untouched.

Four appended gates implement opt-in fixed-two latency admission: `NvapiQueryInterface`, `CompletePresent`, `LatencySwapchainDestroy` and `LatencyDeviceDestroy`. Keep them together with `low-latency.mm`, its admission state and the NVAPI wire definitions. Native resolver and public Present use Microsoft x64 ABI; completion blocks and destructors use System V, including the base device destructor's hidden construction-vtable argument. The device-destructor hook is not a restored PSO cache. Default-off dispatch goes directly to the original functions. Do not patch the three-byte native Sleep stub or replace the donor NVAPI DLL. The existing Present wrapper is shared with async: latency observes the FFX annotation first, then the async wrapper consumes it even when async compilation is disabled. Only async frame-history updates are conditional; leaving a pending annotation behind can falsely exclude a new device when a swapchain address is reused on the same thread.

`CompleteExecute` follows the async gates and is independent of the async option. It retires FSR execution leases at native Metal4 GPU completion, before the original completion routine notifies fences and returns the buffer to its pool. Preserve recorded input ownership and unsubmitted/partial-work fallback ownership. Do not regress to allocator-reset-only retirement: the beta3 migration removed the former completion integration, allowing completed replay resources to accumulate in long-lived allocators.

`PresentResidencyAdd` follows `CompleteExecute` and patches only the indirect call at `0x16d242`, not a function prologue. Its fallback trampoline tail-jumps through the original imported `objc_msgSend` slot, preserving the call site's return address and stack alignment. The serialized native worker registers one current layer set per Metal4 queue and replaces it on a set change. Queue-associated state releases the final set on queue destruction; device/baseline registrations are not removed. The macOS 27 IOGPU implementation uses a retaining `NSCountedSet`, so duplicate adds increment a count and distinct stale layer sets consume the documented 32-set budget. This capacity/lifetime correction does not establish a whole-game memory improvement.

The async suffix is `GraphicsCtor`, `GetRenderPipelineState`, `GraphicsDestroy`, `MPLDraw`, `MPLDrawIndexed`, `RootCtor`, `RootDestroy`, `MPLReset`, `MPLBeginQuery`, `MPLEndQuery`, `MPLDestroy`, `Present1`, `DX11Draw`, `DX11DrawIndexed`, `DX11Begin`, `DX11End`, `DX11Destroy`. It is disabled unless `YAAGL_D3DMETAL_ASYNC=1`. Preserve the mixed ABI, root-signature classification, query tracking, texture/subresource recurrence and destructor drain together; never change only the getter to return null while a draw continues. DX11 uses the native compiler's pending-byte and destructor-wait protocol, with the original context lock and binding/draw/resource-merge order. Its omission point also covers deferred replay and must not treat an unscheduled dynamic variant as perpetually pending.

The module is `libD3DMetalSidecar.dylib`. Keep `--sidecar-module` / `--sidecar-manifest`, `nativeSidecarBuild` / `nativeSidecar`, `patches.nativeSidecar` and the preparation pipeline names consistent. The former custom caches remain absent; the graphics getter wrapper schedules Apple's original compilation and retains native result ownership. Non-FSR replay uses Apple's original implementation. The converter and stock NGX/DLSS pair remain pristine; experimental DLSS/Streamline/nameplate work is excluded.

Async draw eligibility releases the registry mutex before acquiring texture-history locks; retain the pipeline owner and registry decision first, and keep DX11's native context lock around the whole draw. Zero-count or zero-instance DX11 draws omit attachment resolution only: state publication, native emission and resource merging remain ordered. The native regression fixtures explicitly ad-hoc sign generated x86_64 executables for Rosetta execution.

Async hook initialization uses named `Hooks` fields; the bridge must still preserve the dispatch order in `layout.json`. The DX12 safety fixture keeps cold compilation queued explicitly and checks direct/indexed draw admission without relying on compiler timing.

Pending DX12 draws collect attachment histories and subresource keys once and reuse that draw-local batch for observation only after the request returns pending. Do not retain those raw history owners across draws or observe an ineligible target before the request decision. Keep ready-cache hits ahead of root/list classification.

### Media, audio, devices, networking and resources

Retain local MF video-allocator policy with `MFUnlockWorkQueue`, asynchronous error/lifetime handling, GStreamer parser/seek/finalize cleanup, resampler buffer reuse, CoreAudio stream resources, XAudio callback synchronization, HID/SDL/IOHID/XInput ownership, guarded game timeout behavior, WinHTTP/WinINET TLS and pool cleanup, and server socket-poll ownership.

Upstream behavior must survive overlapping optimizations: DirectInput cached value objects retain link-collection selection; Winebus report delivery retains overlong-report truncation using the wire size without the synthetic report-ID byte, rejects short reports, and keeps upstream device identities. Device hardware behavior requires appropriate hardware or simulated-device tests; compilation is not a claim of physical-controller validation.

### Audio lifetime correction found during verification

The real XAudio lifecycle probe exposed an existing FAudio callback-list leak shared by the selected source and upstream 11.20. Each engine registered an internal callback node, but final `FAudio_Release` freed the engine without freeing that list. Direct allocator tracing confirmed one retained 16-byte node per engine on both runtimes. The old runtime's total-heap assertion could pass because `HeapWalk` reports an entire LFH group rather than its individual occupied slots.

In addition to the source-delta port, `libs/faudio/src/FAudio.c` now removes callback nodes after stopping the final engine and before destroying its callback lock. The existing XAudio runtime regression retains its heap-total check and also counts outstanding real native allocator calls, so allocator grouping cannot hide this leak. This is a verification-discovered correction, not an imported historical patch.

## Already-upstream backports

These nine source-reference fixes are ancestors of Wine 11.20. Keep their upstream implementations and tests; do not apply the old backport again or drop other local changes in the same files.

| Upstream commit | Behavior |
|---|---|
| `91fabe0d1dc0f766df0c54809b0b2811f28bcf1d` | resampledmo time denominator |
| `5b7db73f174be3ef98fd80e2a7ccf608f357fb7b` | GStreamer input chunks |
| `f360ce3e9bf650bc41abf0cf26d420b2180add93` | MF ALL_STREAMS end-of-segment |
| `2f73d9efc2a060b56cde465e3d3cac523823ef5d` | MF async-command release |
| `7c48881f4fe2edba88f70b2e1b0892d3d98c8b19` | amstream flush wakeup |
| `5960f8049bb185b1a789b4ee8efaedbe1feb8b6f` | imm32 link order |
| `6850fba76d365e8855b5a81432d32781693cce2f` | latched macdrv surface toplevel |
| `d119a0c94a0a9cdabc42b28c607584e357295413` | ntdll debug exception space |
| `0e36c06949adbd1a409990fce171060968c6935b` | active VAO for glDrawArrays |

## Build and verification boundary

Use a fresh `WINE_P3_ROOT` with `WINE_NATIVE_SERVER=yes` and the explicit Xcode/LLVM MinGW/Bison/x86_64 dependency environment. The full builder includes both FSR modules and creates its own Wine tools and headers. `sourceTreeSha256` covers tracked and nonignored untracked source bytes; the upstream HEAD alone is not the identity of an uncommitted port. Do not edit manifests to relabel old binaries or hide source changes.

Pristine GPTK 4.0b3, stock graphics/NGX modules, license resources, Steam helpers, TLS/font/media dependencies and the original game FG fallback remain separate inputs. Rebuild the native sidecar and validate its source manifest, then stage with `stage-full-wine-runtime.py`. The staged runtime must pass `stage-runtime.py --verify-runtime ... --current-sources` before packaging.

Public FSR upscaler and framegeneration DLLs come from the full Wine host, not the native sidecar or graphics donor. Both `x86_64-windows` and `i386-windows` DLLs are required, checked for architecture and exports, and matched to the full build's `hostArtifacts` hashes at staging and verification. A sidecar-only rebuild does not update these DLLs. After producer changes, rebuild and install the full host with `build-wine-p3.sh` into a fresh build root, then restage and package; the x86_64-only `build-fsr-translator.sh` output is not a full-host packaging input. Do not refresh inventories around old DLLs to claim a current build.

Run the existing MSync message, cursor, FSR contract/memory/resource/motion/GPU, native ABI/layout, preparation, launcher and staging checks against the final inputs. Supplement the focused MSync harness with real Wine wait/APC/child-process and shared-memory scenarios using the paired ARM64 server and Rosetta clients. Run the existing real-runtime memory-reset and XAudio callback regressions on both guest architectures. Finally exercise the new runtime in an isolated prefix and observe actual loaded modules and game rendering. No source-level check proves universal image correctness, game performance parity, physical-device coverage or absence of rare concurrency failures.

## Complete source-path disposition

This table accounts for every path in the 201-path source delta. `Carried` means the selected source bytes were retained; `Reconciled` means its local intent was combined with upstream changes; `Upstream` means no local file change is needed; `Generated` uses target generators; `Reference` stays only in the original source commit. Documentation is updated for this base. These are source dispositions, not independent runtime test results.

| Source path | Disposition | Owner |
|---|---|---|
| `.gitignore` | Carried | Root |
| `LICENSE.MIT` | Carried | Root |
| `README.ko.md` | Documentation | Root |
| `README.md` | Adapted | Root |
| `configure` | Generated | Root |
| `configure.ac` | Adapted | Root |
| `d3dmetal-sidecar/bridge.mm` | Carried | Root |
| `d3dmetal-sidecar/d3dmetal-transport.hpp` | Carried | Root |
| `d3dmetal-sidecar/d3dmetal-transport.mm` | Carried | Root |
| `d3dmetal-sidecar/display-routing.hpp` | Carried | Root |
| `d3dmetal-sidecar/display-routing.mm` | Carried | Root |
| `d3dmetal-sidecar/fsr-contract.cpp` | Carried | Root |
| `d3dmetal-sidecar/fsr-contract.hpp` | Carried | Root |
| `d3dmetal-sidecar/fsr-framegeneration.hpp` | Carried | Root |
| `d3dmetal-sidecar/fsr-framegeneration.mm` | Carried | Root |
| `d3dmetal-sidecar/fsr-kernels.metal` | Carried | Root |
| `d3dmetal-sidecar/fsr-translator.hpp` | Carried | Root |
| `d3dmetal-sidecar/fsr-translator.mm` | Carried | Root |
| `d3dmetal-sidecar/layout.json` | Carried | Root |
| `d3dmetal-sidecar/metalfx-backend.hpp` | Carried | Root |
| `d3dmetal-sidecar/metalfx-backend.mm` | Carried | Root |
| `d3dmetal-sidecar/metalfx-contract.hpp` | Carried | Root |
| `d3dmetal-sidecar/ngx-hooks.hpp` | Carried | Root |
| `d3dmetal-sidecar/ngx-hooks.mm` | Carried | Root |
| `d3dmetal-sidecar/third-party/fidelityfx/Kits/FidelityFX/api/include/dx12/ffx_api_dx12.h` | Carried | Root |
| `d3dmetal-sidecar/third-party/fidelityfx/Kits/FidelityFX/api/include/ffx_api.h` | Carried | Root |
| `d3dmetal-sidecar/third-party/fidelityfx/Kits/FidelityFX/api/include/ffx_api_types.h` | Carried | Root |
| `d3dmetal-sidecar/third-party/fidelityfx/Kits/FidelityFX/framegeneration/include/dx12/ffx_api_framegeneration_dx12.h` | Carried | Root |
| `d3dmetal-sidecar/third-party/fidelityfx/Kits/FidelityFX/framegeneration/include/ffx_framegeneration.h` | Carried | Root |
| `d3dmetal-sidecar/third-party/fidelityfx/Kits/FidelityFX/framegeneration/include/ffx_framegeneration_api_types.h` | Carried | Root |
| `d3dmetal-sidecar/third-party/fidelityfx/Kits/FidelityFX/upscalers/include/ffx_upscale.h` | Carried | Root |
| `d3dmetal-sidecar/video-memory.hpp` | Carried | Root |
| `d3dmetal-sidecar/video-memory.mm` | Carried | Root |
| `dlls/amd_fidelityfx_framegeneration_dx12/Makefile.in` | Carried | Root |
| `dlls/amd_fidelityfx_framegeneration_dx12/amd_fidelityfx_framegeneration_dx12.spec` | Carried | Root |
| `dlls/amd_fidelityfx_framegeneration_dx12/main.c` | Carried | Root |
| `dlls/amd_fidelityfx_framegeneration_dx12/unixlib.c` | Carried | Root |
| `dlls/amd_fidelityfx_framegeneration_dx12/unixlib.h` | Carried | Root |
| `dlls/amd_fidelityfx_upscaler_dx12/Makefile.in` | Carried | Root |
| `dlls/amd_fidelityfx_upscaler_dx12/amd_fidelityfx_upscaler_dx12.spec` | Carried | Root |
| `dlls/amd_fidelityfx_upscaler_dx12/main.c` | Carried | Root |
| `dlls/amd_fidelityfx_upscaler_dx12/unixlib.c` | Carried | Root |
| `dlls/amd_fidelityfx_upscaler_dx12/unixlib.h` | Carried | Root |
| `dlls/amstream/ddrawstream.c` | Upstream | Compatibility |
| `dlls/dinput/joystick_hid.c` | Reconciled | Compatibility |
| `dlls/hidclass.sys/pnp.c` | Reconciled | Compatibility |
| `dlls/imm32/Makefile.in` | Upstream | Compatibility |
| `dlls/mfreadwrite/reader.c` | Reconciled | Compatibility |
| `dlls/mfreadwrite/writer.c` | Reconciled | Compatibility |
| `dlls/ntdll/Makefile.in` | Reconciled | HostSync |
| `dlls/ntdll/loader.c` | Reconciled | HostSync |
| `dlls/ntdll/ntdll.spec` | Reconciled | HostSync |
| `dlls/ntdll/tests/sync.c` | Reconciled | HostSync |
| `dlls/ntdll/unix/loader.c` | Reconciled | HostSync |
| `dlls/ntdll/unix/msync.c` | Carried | HostSync |
| `dlls/ntdll/unix/msync.h` | Carried | HostSync |
| `dlls/ntdll/unix/process.c` | Reconciled | HostSync |
| `dlls/ntdll/unix/server.c` | Reconciled | HostSync |
| `dlls/ntdll/unix/signal_arm.c` | Upstream | HostSync |
| `dlls/ntdll/unix/signal_arm64.c` | Upstream | HostSync |
| `dlls/ntdll/unix/signal_i386.c` | Upstream | HostSync |
| `dlls/ntdll/unix/signal_x86_64.c` | Reconciled | HostSync |
| `dlls/ntdll/unix/sync.c` | Reconciled | HostSync |
| `dlls/ntdll/unix/system.c` | Reconciled | HostSync |
| `dlls/ntdll/unix/thread.c` | Reconciled | HostSync |
| `dlls/ntdll/unix/unix_private.h` | Upstream | HostSync |
| `dlls/ntdll/unix/virtual.c` | Reconciled | HostSync |
| `dlls/ntdll/unixlib.h` | Reconciled | HostSync |
| `dlls/resampledmo/resampler.c` | Reconciled | Compatibility |
| `dlls/win32u/d3dkmt.c` | Reconciled | GraphicsInput |
| `dlls/win32u/dce.c` | Reconciled | GraphicsInput |
| `dlls/win32u/message.c` | Reconciled | GraphicsInput |
| `dlls/win32u/opengl.c` | Upstream | GraphicsInput |
| `dlls/win32u/vulkan.c` | Reconciled | GraphicsInput |
| `dlls/win32u/win32u_private.h` | Reconciled | GraphicsInput |
| `dlls/win32u/window.c` | Reconciled | GraphicsInput |
| `dlls/winebus.sys/bus_iohid.c` | Reconciled | Compatibility |
| `dlls/winebus.sys/bus_sdl.c` | Reconciled | Compatibility |
| `dlls/winebus.sys/hid.c` | Reconciled | Compatibility |
| `dlls/winebus.sys/main.c` | Reconciled | Compatibility |
| `dlls/winecoreaudio.drv/coreaudio.c` | Reconciled | Compatibility |
| `dlls/winegstreamer/gst_private.h` | Reconciled | Compatibility |
| `dlls/winegstreamer/main.c` | Reconciled | Compatibility |
| `dlls/winegstreamer/media_sink.c` | Reconciled | Compatibility |
| `dlls/winegstreamer/media_source.c` | Reconciled | Compatibility |
| `dlls/winegstreamer/quartz_parser.c` | Reconciled | Compatibility |
| `dlls/winegstreamer/wg_parser.c` | Reconciled | Compatibility |
| `dlls/winegstreamer/wg_transform.c` | Reconciled | Compatibility |
| `dlls/winegstreamer/wm_reader.c` | Reconciled | Compatibility |
| `dlls/winemac.drv/Makefile.in` | Reconciled | GraphicsInput |
| `dlls/winemac.drv/cocoa_app.h` | Reconciled | GraphicsInput |
| `dlls/winemac.drv/cocoa_app.m` | Reconciled | GraphicsInput |
| `dlls/winemac.drv/cocoa_event.h` | Reconciled | GraphicsInput |
| `dlls/winemac.drv/cocoa_event.m` | Reconciled | GraphicsInput |
| `dlls/winemac.drv/cocoa_warpconsumption.h` | Reconciled | GraphicsInput |
| `dlls/winemac.drv/cocoa_window.h` | Reconciled | GraphicsInput |
| `dlls/winemac.drv/cocoa_window.m` | Reconciled | GraphicsInput |
| `dlls/winemac.drv/d3dmetal.c` | Reconciled | GraphicsInput |
| `dlls/winemac.drv/d3dmetal_objc.h` | Reconciled | GraphicsInput |
| `dlls/winemac.drv/d3dmetal_objc.m` | Reconciled | GraphicsInput |
| `dlls/winemac.drv/dllmain.c` | Reconciled | GraphicsInput |
| `dlls/winemac.drv/event.c` | Reconciled | GraphicsInput |
| `dlls/winemac.drv/macdrv.h` | Reconciled | GraphicsInput |
| `dlls/winemac.drv/macdrv_cocoa.h` | Reconciled | GraphicsInput |
| `dlls/winemac.drv/macdrv_main.c` | Reconciled | GraphicsInput |
| `dlls/winemac.drv/mouse.c` | Reconciled | GraphicsInput |
| `dlls/winemac.drv/surface.c` | Reconciled | GraphicsInput |
| `dlls/winemac.drv/unixlib.h` | Reconciled | GraphicsInput |
| `dlls/winemac.drv/window.c` | Reconciled | GraphicsInput |
| `dlls/winhttp/net.c` | Reconciled | Compatibility |
| `dlls/wininet/http.c` | Reconciled | Compatibility |
| `dlls/wininet/netconnection.c` | Reconciled | Compatibility |
| `dlls/wow64cpu/cpu.c` | Reconciled | HostSync |
| `dlls/ws2_32/socket.c` | Reconciled | Compatibility |
| `dlls/xaudio2_7/xaudio_dll.c` | Reconciled | Compatibility |
| `dlls/xaudio2_7/xaudio_private.h` | Reconciled | Compatibility |
| `dlls/xinput1_3/main.c` | Reconciled | Compatibility |
| `documentation/WINE-REBASE.md` | Documentation | Root |
| `include/Makefile.in` | Adapted | Root |
| `include/ntuser.h` | Reconciled | GraphicsInput |
| `include/wine/gdi_driver.h` | Reconciled | GraphicsInput |
| `include/wine/msync.h` | Carried | HostSync |
| `include/wine/server_protocol.h` | Generated | Root |
| `include/yaagl_d3dmetal_abi.hpp` | Carried | Root |
| `include/yaagl_d3dmetal_display.h` | Carried | Root |
| `include/yaagl_d3dmetal_native.hpp` | Carried | Root |
| `include/yaagl_fsr_bridge.h` | Carried | Root |
| `include/yaagl_fsr_fg_bridge.h` | Carried | Root |
| `include/yaagl_macho_symbols.hpp` | Carried | Root |
| `patches/wine-p3/0001-host-msync.patch` | Reference | Root |
| `patches/wine-p3/0002-d3dmetal-bridge.patch` | Reference | Root |
| `patches/wine-p3/0003-zzz-compatibility.patch` | Reference | Root |
| `patches/wine-p3/manifest.json` | Reference | Root |
| `patches/wine-p3/series` | Reference | Root |
| `patches/wine-tuned/0001-msync-tuned.patch` | Reference | Root |
| `patches/wine-tuned/0002-native-x86-server.patch` | Reference | Root |
| `patches/wine-tuned/0003-msync-reliability.patch` | Reference | Root |
| `patches/wine-tuned/0004-macdrv-reset-rawinput-baseline.patch` | Reference | Root |
| `patches/wine-tuned/0005-upstream-media-fixes.patch` | Reference | Root |
| `patches/wine-tuned/0006-upstream-macos-fixes.patch` | Reference | Root |
| `patches/wine-tuned/0007-msync-resource-reuse.patch` | Reference | Root |
| `patches/wine-tuned/0008-media-resources.patch` | Reference | Root |
| `patches/wine-tuned/0009-device-resources.patch` | Reference | Root |
| `patches/wine-tuned/0010-network-resources.patch` | Reference | Root |
| `patches/wine-tuned/0011-audio-resources.patch` | Reference | Root |
| `patches/wine-tuned/0012-msync-shared-pages.patch` | Reference | Root |
| `patches/wine-tuned/0013-window-resources.patch` | Reference | Root |
| `patches/wine-tuned/0014-core-resources.patch` | Reference | Root |
| `patches/wine-tuned/0017-cursor-reconciliation-coalescing.patch` | Reference | Root |
| `patches/wine-tuned/0018-confinement-warp-correction.patch` | Reference | Root |
| `scripts/build-d3dmetal-autopatch.mjs` | Carried | Root |
| `scripts/build-d3dmetal-sidecar.mjs` | Carried | Root |
| `scripts/build-fsr-translator.sh` | Carried | Root |
| `scripts/build-wine-p3.sh` | Carried | Root |
| `scripts/build-wine-tuned.sh` | Carried | Root |
| `scripts/d3dmetal-runtime-spec.mjs` | Carried | Root |
| `scripts/d3dmetal-sidecar-patch.mjs` | Carried | Root |
| `scripts/fsr-public-smoke.cpp` | Carried | Root |
| `scripts/generate-b3-sidecar-layout.mjs` | Carried | Root |
| `scripts/native-contract-test.mm` | Carried | Root |
| `scripts/package-wine-p3-runtime.sh` | Carried | Root |
| `scripts/package-wine-runtime-split.sh` | Carried | Root |
| `scripts/package-yaagl-runtime.py` | Carried | Root |
| `scripts/prepare-d3dmetal-runtime.swift` | Carried | Root |
| `scripts/prepare-d3dmetal-runtime.test.mjs` | Carried | Root |
| `scripts/refresh-staged-runtime-metadata.py` | Carried | Root |
| `scripts/stage-full-wine-runtime.py` | Carried | Root |
| `scripts/stage-runtime.py` | Carried | Root |
| `scripts/test-b3-sidecar-layout.mjs` | Carried | Root |
| `scripts/test-cursor-warp-correction.py` | Carried | Root |
| `scripts/test-fg-normalization.py` | Carried | Root |
| `scripts/test-fsr-contract.py` | Carried | Root |
| `scripts/test-fsr-memory-query.py` | Carried | Root |
| `scripts/test-fsr-native.py` | Carried | Root |
| `scripts/test-memory-reset-runtime.py` | Carried | Root |
| `scripts/test-motion-view.py` | Carried | Root |
| `scripts/test-msync-message-dispatch.py` | Carried | Root |
| `scripts/test-native-contract.mjs` | Carried | Root |
| `scripts/test-resource-map.py` | Carried | Root |
| `scripts/test-staged-runtime-integrity.py` | Carried | Root |
| `scripts/test-wine-launch-wrapper.py` | Carried | Root |
| `scripts/test-xaudio-callback-runtime.py` | Adapted: native allocation lifetime regression | Root |
| `scripts/wine-launch-wrapper.sh` | Carried | Root |
| `scripts/write-wine-runtime-manifest.py` | Carried | Root |
| `server/Makefile.in` | Reconciled | HostSync |
| `server/inproc_sync.c` | Reconciled | HostSync |
| `server/mach.c` | Reconciled | HostSync |
| `server/main.c` | Reconciled | HostSync |
| `server/msync.c` | Carried | HostSync |
| `server/msync.h` | Carried | HostSync |
| `server/protocol.def` | Adapted | Root |
| `server/queue.c` | Reconciled | GraphicsInput |
| `server/registry.c` | Reconciled | HostSync |
| `server/request.c` | Reconciled | HostSync |
| `server/request_handlers.h` | Generated | Root |
| `server/request_trace.h` | Generated | Root |
| `server/sock.c` | Reconciled | Compatibility |
| `server/thread.c` | Reconciled | HostSync |
| `server/thread.h` | Reconciled | HostSync |
| `server/user.h` | Reconciled | GraphicsInput |
| `server/window.c` | Reconciled | GraphicsInput |
