# Low-latency scheduling for Wine / GPTK 4 beta 3

Status: cooperative and forced fixed-two modes implemented; adaptive controller and AMD adapters remain proposals. Investigation date: 2026-10-27.
Target: this Wine 11.20 tree, Apple Silicon/Rosetta, pinned GPTK 4 beta 3.
The investigation and implementation validation use isolated runtime copies and prefixes. No installed runtime or game prefix is modified.

## 1. Decision

The implementation provides opt-in cooperative input admission and forced Present pacing in the existing native sidecar. It uses the stock NVAPI -> Unix bridge -> D3DMetal path, rather than replacing nvapi64.dll, Streamline, or the DLSS translator, and preserves native DXGI frame-latency settings. The wider adaptive/LatencyFleX and AMD adapter design below is not the current implementation.


Do not equate this with porting NVIDIA's or AMD's closed driver algorithm. Do not force FG into unsupported games. Do not implement clock boosting, speculative draw skipping, arbitrary input hooks, or a second presenter. Markerless games keep native scheduling by default; forced pacing is a separate explicit option. A callback added at Present cannot establish that the game's input sample was delayed to the right place.

The strongest immediate opportunity is concrete: stock beta3 accepts Reflex calls but does not implement its Sleep controller.

### Implemented fixed-two boundary

`YAAGL_D3DMETAL_LOW_LATENCY=1` selects the mode; the game must request low latency and call `NvAPI_D3D_Sleep` before input. Sleep reserves capacity before returning, including frames not yet submitted by a separate render thread. Pending native Presents plus reservations may not exceed two during synchronized admission. Before a swapchain is observed, Sleep passes through. This is independent of async compilation and does not alter swapchain flags or inject a Present wait.

`YAAGL_D3DMETAL_LOW_LATENCY=force` additionally supports games with no Reflex calls. The existing native Present returns before the sidecar waits for fewer than two unfinished native Presents, leaving room for the next frame. The wait releases the limiter mutex and holds no native Present lock. TEST, nonadvancing and DO_NOT_WAIT calls bypass it. Native completion, exclusions and lifetime retirement release waiters; a second live chain or overlapping Present cannot leave the first caller blocked.

Each device has one pacing owner. Forced pacing remains active after SetSleepMode alone. A requested, eligible Sleep call transfers ownership to cooperative admission and releases a blocked forced Present before waiting on the same pending GPU count. A missing admission at the next Present returns force mode to Present pacing, while cooperative-only mode retains its prior invalidation policy. Reflex OFF also restores force mode, rather than overriding the explicit runtime option. Cancelling or changing chain generations clears stale ownership and reservations, not real pending work.

The native resolver replaces only Sleep, SetSleepMode, GetSleepStatus and SetLatencyMarker pointers; all other resolutions, including GetLatency, retain their stock implementation. Original resolver calls occur on API use, not during the sidecar dependency constructor. GetSleepStatus reports effective eligibility, observed Sleep calls and duration. Boost does not change GPU clocks; nonzero minimumIntervalUs is explicitly unsupported. Known FFX FG annotations and Reflex out-of-band markers exclude both modes. Multiple live swapchains suspend them, while overlapping Presents invalidate pacing for the device lifetime.

Native lifetime evidence is retained in `build/low-latency-limit2/native-contract.json` and `native-lifecycle.json`. Present completion at `0x12b610` captures the swapchain at block + `0x20`, then releases its reserved lifetime reference. The sidecar observes completion before that release. The actual swapchain destructor at `0x12ac64` therefore cannot precede its outstanding native callbacks. Device destruction at `0x60f54` retires the canonical RTTI-resolved D3DMDevice key and forwards the hidden construction-vtable argument. A nonadvancing Present has not enqueued its own completion. These contracts permit aggregate counts without assigning unordered callbacks to individual frames; shared begin tokens isolate late rollback from a replacement object. They do not provide the per-frame identity needed by the proposed adaptive controller.

TEST followed by real Present consumes the same admission. TEST without real Present before the next Sleep invalidates pacing, as does TEST when multiple CPU reservations make attribution ambiguous. This wakes an already blocked Sleep without decrementing real native work. The independent review found the original TEST-only reservation leak; it was reproduced through public DX11/NVAPI before adding this event-driven regression.

`scripts/test-low-latency.mjs` exercises the production admission state with event-driven tests. The actual stock PE/Unix/native route is additionally exercised by public D3D11 and D3D12 workloads in the isolated runtime. Measurement records belong under `build/low-latency-limit2/`; GPU feedback is a GPU-work endpoint, not physical scanout or input-to-photon latency.

The balanced normal-Present benchmark ran four OFF and four ON trials at each of two workloads, plus four uninstrumented controls: 20 runs total, each with 90 warmup and 360 requested frames at 1280x720. The final readback frame is excluded from timing. On the GPU-bound workload, four-run medians were:

| Measurement | OFF | Fixed two |
|---|---:|---:|
| Native GPU render throughput | 51.661 FPS | 51.650 FPS |
| Synthetic input marker to render GPU completion | 76.90 ms | 38.36 ms |
| Submit to GPU start | 57.63 ms | 19.12 ms |
| Input-before-render wait | 0.0004 ms | 19.16 ms |
| Present CPU wait | 19.17 ms | 0.0053 ms |

The throughput change was -0.021%, with overlapping run ranges. All 5,744 instrumented measured frames had valid timing/mapping and no reported GPU errors. At the input marker, earlier unfinished GPU Presents fell from at most three to at most one, leaving room for the newly admitted frame. Light-workload throughput ranges also overlapped; no FPS improvement is claimed. Timing uses actual Metal4 feedback and the paired mach-continuous/CACurrentMediaTime offset, not the runtime's zero D3D12 timestamp-query values.

After the TEST-only fix, the final 450-frame GPU-bound confirmation gave 51.636 FPS and 38.35 ms marker-to-GPU completion, with valid mapping and zero reported GPU errors. Public DX11 checks cover exact pixels, TEST-then-real Present, TEST-only wakeup, IUnknown alias identity, resize, mode/error handling and repeated device teardown. OOB exclusion and combined async/latency passed public D3D12 smoke runs. Actual FFX game presentation and physical input-to-photon improvement have not been established.

## 2. Evidence and current behavior

### 2.1 Public implementations

- [AMD Anti-Lag 2 SDK v2.0.0a](https://github.com/GPUOpen-LibrariesAndSDKs/AntiLag2-SDK/tree/390aa4a8c8655d0ae6e90079db2c85e103a96da3): headers implement binding, state packets and call ordering, not the pacing algorithm. DX12 obtains `IAmdExtAntiLagApi` from the already loaded `amdxc64.dll!AmdExtD3DCreateInterface`; DX11 uses `amdxx64.dll!AmdDxExtCreate11`. `Update` runs immediately before input sampling. A non-null packet updates options; the null packet requests the actual delay. DX12 FG adds end-of-render and interpolated-frame identity. Driver internals were not recovered.
- [NVIDIA NVAPI headers](https://github.com/NVIDIA/nvapi/blob/main/nvapi.h), observed SDK version 61683: `SetSleepMode` persists independent low-latency, boost and minimum-interval settings. `Sleep` is called exactly once per frame, preferably before input. `SetLatencyMarker` and `GetLatency` provide attribution/measurement; an accepted marker or successful SetSleepMode is not proof of pacing.
- [Streamline Reflex guide v2.14.16](https://github.com/NVIDIA-RTX/Streamline/blob/main/docs/ProgrammingGuideReflex.md): `slReflexSleep` maps to the sleep contract; PCL markers are distinct. Off mode does not remove all calls. Frame limiting and low-latency scheduling are separate behaviors. Stock plugin capability checks still matter; this design does not promise to bypass game/vendor checks.
- [VK_NV_low_latency2](https://registry.khronos.org/vulkan/specs/latest/man/html/VK_NV_low_latency2.html): asynchronous sleep signals a timeline semaphore; the application waits on it. Explicit frame IDs and out-of-band presentation distinguish application frames from extra presents. [VK_AMD_anti_lag](https://registry.khronos.org/vulkan/specs/latest/man/html/VK_AMD_anti_lag.html) instead permits its update call to block. These are host-driver contracts, not algorithms that become available just because Wine has thunk declarations.
- [dxvk-nvapi](https://github.com/jp7677/dxvk-nvapi/blob/master/src/nvapi_d3d.cpp) forwards to a low-latency device interface. [DXVK's Reflex tracker](https://github.com/doitsujin/dxvk/blob/master/src/dxvk/dxvk_latency_reflex.cpp) maps application/internal frame IDs, records timings, and calls the presenter/driver sleep path. Its markerless path is not a universal implementation of Reflex.
- [vkd3d-proton swapchain](https://github.com/HansKristian-Work/vkd3d-proton/blob/master/libs/vkd3d/swapchain.c) is a useful reference for driver sleep integration, multiple swapchains, and avoiding competing latency waits. These moving branches are comparative references, not pinned code selected for import.
- [LatencyFleX core at 17c061c32594f24be225e69a2aeadf16f959a6fb](https://github.com/ishitatsuyuki/LatencyFleX/blob/17c061c32594f24be225e69a2aeadf16f959a6fb/latencyflex.h) is an actual open, Apache-2.0 controller: completion-throughput and latency EWMAs, alternating rate probes, prediction-error compensation, and explicit GetWaitTarget/BeginFrame/EndFrame. Its controller has not yet been measured on this Metal runtime. Preserve its attribution and constants if imported; do not claim them to be Metal-optimal.

### 2.2 REA findings

Stock D3DMetal SHA-256:
`a64950e8324cec32c9a442daa1fafd03823bd199fdd84fee5c73679b5fe86660`.
Installed stock nvapi64.dll SHA-256:
`de4872e71e6b3f54775b3e564cc487d984c52fec6711bfee250228843ba762d0`.
Addresses below belong only to this pinned x86_64 image, not arbitrary GPTK builds.

| Native location | Observed behavior | Design consequence |
|---|---|---|
| NVAPI PE QueryInterface `0x180001120` | Optional GFXT override; otherwise resolves through Unix opcode 0x2b and returns a PE wrapper | Keep both ABI sides; hook the native path rather than replacing the PE API surface |
| PE Sleep `0x1800014c0`, SetSleepMode `0x180002080` | Unwrap Streamline base interface when supplied; forward through Unix call 0x2d/0x2e | The bridge already reaches the native device; no new broad NVAPI proxy is necessary |
| Native Sleep `0x1407f7` | Exactly three bytes: `XOR EAX,EAX; RET` | No pacing; a five-byte entry detour would overwrite bytes outside this function |
| Native SetSleepMode `0x14049d` | Checks version 0x1002c; no settings persistence or controller | Needs real per-device options and admission behavior |
| Native GetSleepStatus `0x14047a` | For version 0x10088, clears enabled/interval/status fields | Replace with truthful effective-state reporting |
| Native GetLatency `0x1404ab` | For version 0x13c28, zeroes report area | Do not copy the empty-success pattern into a new feature |
| Native SetLatencyMarker `0x140560`, update `0xe7c92` | Records frame ID/type, host timestamp, last two samples per type under a mutex | Preserve original recording while collecting scheduler-owned frame records |
| Swapchain SetMaximumFrameLatency `0x12a59a` | Validates range/event presence; updates state and semaphore/event behavior | Existing real DXGI mechanism, not a missing Wine feature to replace |
| Device SetMaximumFrameLatency `0x1ab28c` | Stores requested value; zero maps to three | Distinct from swapchain configuration; do not conflate with drawable count |
| GetFrameLatencyWaitableObject `0x12a6d0` | Returns event or converted handle depending on host event interface | Handle duplication/CloseHandle behavior needs live tests; one legacy branch logs unsupported CloseHandle |
| Present1 `0x129a78` | Queues native Present, may wait on a semaphore, then advances its counter; TEST/empty dimensions do not advance | CPU Present return is admission/backpressure evidence, not actual display completion |
| DoPresent `0x16cc46` | Metal4 presentation blit has commit feedback; then queue signalDrawable and drawable present/minimum-duration presentation | Hook workload completion and actual presentation separately |
| Metal4 Present feedback `0x16e669` | Handles errors and invokes a copied completion block | A lifetime-owned place to observe completion; must not break native callback execution |
| CompleteExecute `0x16f0d0` | Retires an Execute submission, signals fences, updates queue completion state and destroys the submission | Not one application frame and not a scanout notification |
| Present completion `0x12b610` | Signals the latency event and semaphore, releases the semaphore reference, then updates lifetime bookkeeping | Native frame-latency credits are returned on GPU completion, not physical display |
| Execute feedback `0x16eccb` / `0x16eecc` | Metal4 feedback / legacy command-buffer error observation followed by CompleteExecute | Hardware timing must be read before collapsing into the shared retirement callback |
| Native resolver `0x17d757` | Reads ID from ECX, maps it through the interface table and locked entrypoint store, returns function pointer in RAX; 643-byte body | One Windows-x64 resolver gate can return typed replacements without touching the three-byte Sleep body |

Evidence bundles under `build/low-latency-design/`: `nvapi-evidence.json`, `d3dmetal-evidence.json`, `d3dmetal-queue-evidence.json`, `d3dmetal-feedback-evidence.json`, and `d3dmetal-resolver-evidence.json`. Primary dossier IDs include `ev_2abe11095d1c40cea60df38746831680926320292433ab73d8839a8a20f5b94e`, `ev_e826140626c0b3bee280f2133b7e192abeb45a55668845a8b4c044fd83885462`, `ev_7a882653a3c4addbe25f09dac91aef8132615ade77b11508994b00f9108dbda0`, and resolver dossier `ev_ed0855c95b96a0ce305ca02d503c62d40339c0ac31503b8c464c1d75ed249e6e`.

Ghidra misidentifies some Windows-x64 arguments as host arguments and gives unreliable Objective-C floating-point prototypes. Function names and decompiled logic are evidence, not a callable ABI declaration. Verify registers/instructions and real call traces before encoding a new hook.

### 2.3 Actual installed-Wine probe

An isolated x64 Windows executable created a real D3D12 device, resolved all three NVAPI functions, and initialized NVAPI successfully. Results:

| Requested mode | Minimum interval | Set/Get/Sleep result | Reported enabled/interval | Sleep call wall time |
|---|---:|---|---|---:|
| Off | 0 us | 0 / 0 / 0 | 0 / 0 | 0.9 us |
| On | 10,000 us | 0 / 0 / 0 | 0 / 0 | 0.4 us |
| On + Boost | 10,000 us | 0 / 0 / 0 | 0 / 0 | 0.2 us |

This confirms the callable path and inactive state observed statically. It is a CPU-only API probe without rendered frames: it is not a latency benchmark or proof about real-game frame limiting. Results: `build/low-latency-design/runtime-probe.txt`.

## 3. Integration boundaries

The active D3DMetal renderer uses donor DXGI/D3D modules, not Wine's `dlls/dxgi/swapchain.c`. Keep donor module hashes in `scripts/d3dmetal-runtime-spec.mjs:30-39` unchanged. Do not edit `libd3dshared.dylib`, nvapi64.dll or the stock NGX pair for this feature.

Add `d3dmetal-sidecar/low-latency.hpp/.mm` as the owner of the new state. Integrate through `bridge.mm`, the existing native patch layout, and the native API loader. This module is independent of async PSO compilation: enabling/disabling `YAAGL_D3DMETAL_ASYNC` must not change latency behavior.

Use one process-start option, `YAAGL_D3DMETAL_LOW_LATENCY`, default off. Value `1` selects game-cooperative admission; `force` selects Present pacing with cooperative handoff as described above. Off preserves original resolver pointers and the existing Present fast path. Export bounded in-memory diagnostics for measurement; do not add synchronous per-frame log or file writes.

The current Present observer in `async-pipeline.mm:293-306` returns immediately when async is disabled and consumes presentation history when enabled. Refactor only this dispatch boundary so one observer consumes the existing generated/real annotation once, then distributes the immutable observation to async recurrence and latency state. Preserve the existing async epoch rule exactly. Never use the global async epoch as an application frame ID.

Use the existing CompleteExecute hook only for submission/resource retirement and error propagation. It does not provide whole-frame attribution by itself. The selected controller endpoint is completion of a Present blit proven ordered after that frame's source-producing GPU work, not an arbitrary Present callback. Preserve the existing FSR execution leases and original completion order.

Patch layout work includes `layout.json`, generated `layout.hpp`, layout/payload hashes, dispatch order in `bridge.mm`, patcher acceptance hash, and independent inventory in `scripts/test-b3-sidecar-layout.mjs`. Direct Sleep prologue patching is rejected: REA confirms its body is only three bytes. Use typed replacement of the five Reflex function pointers at the native NVAPI resolver boundary, preserving all other resolutions and caching original pointers for marker forwarding. `_nvapi_QueryInterface` at `0x17d757` is confirmed: ID in ECX, pointer return in RAX, first three instructions are two-byte PUSH R15/R14/R13. A six-byte gate with a trampoline for those complete instructions is the selected entry strategy; actual bytes, relocations and ABI execution remain patch-test obligations. The resolver and returned callbacks use Windows-x64 ABI, not the host default. Call the original resolver outside the scheduler lock and never intercept its internal store recursively. Install at sidecar startup before the PE shim caches pointers; never retroactively replace pointers held by running games. No global Objective-C swizzle and no process-wide NVAPI override.

## 4. State and frame attribution

A scheduler context belongs to a canonical native D3D device plus lifetime generation. It owns settings, the controller, marker records and associated swapchain generations. Queue and swapchain pointers alone are not durable identities: retirement and pointer reuse must create a new generation.

A frame record contains:

- Internal monotonic simulation sequence; optional game frame ID plus its mapping generation.
- CPU Sleep entry/return, simulation and render-submit markers, input marker when actually supplied.
- Swapchain generation, real/generated/OOB classification, native Present command identity and queue submission boundary.
- Workload submission/start/end when observable, CPU completion-receipt time, drawable presentation time, and validity/error flags for each field.

NVAPI Sleep has no frame-ID argument. Allocate a provisional internal sequence on the single admission call; bind the next matching simulation-start marker to it. Explicit Present markers link the simulation record to a swapchain. Never associate asynchronous submissions to whichever TLS marker was most recent on an unrelated thread. Use the native queued command identity for transfer to the worker and callbacks.

Adaptive admission requires one unambiguous simulation-owner thread per context and a validated Sleep -> simulation-marker -> Present stream. A second concurrent simulation owner disables adaptation for that generation. Ownership can migrate only after pending associations are retired and tracking is restarted. A repeated Sleep before the expected simulation marker does not create a second frame or second adaptive wait; mark the stream invalid and preserve only the independent frame-limit contract. Before the first valid stream, pass through without adaptive delay and report warmup/inactive, not full support.

The per-record state machine is `Reserved -> Begun -> Identified -> PresentBound -> Completed`, with `Invalidated` terminal from any state:

- `Reserved` is owned by one admission transaction and simulation thread. Cancellation before Begin discards the entire estimator generation.
- `Begun -> Identified` requires a unique SIMULATION_START on that owner thread after its Sleep returns, with a game ID not already bound in this generation. Duplicate/out-of-order/reset/wrapped IDs invalidate rather than overwrite the map. Missing markers leave a bounded provisional record; they do not authorize a guessed match.
- Rendering on another thread can reference only an already identified game ID. `PRESENT_START(gameID)` establishes a same-thread, same-device pending binding that the ensuing native Present gate consumes once. A Present on another thread without a carried explicit token does not consume it.
- While the original Present executes synchronously, the native queue-enqueue observation captures that pending token into a sidecar record keyed by the actual queued PresentCommand identity. The worker uses that identity, not producer TLS. Retries/TEST/failure do not consume a successful-frame credit.
- GPU completion resolves the exact command record and generation once. Completion delivery can be reordered; feed the controller only the contiguous completed sequence, buffering at most the imported core's in-flight capacity. A gap, failed present, missing marker or capacity overrun invalidates tracking; no waiter depends indefinitely on the gap closing.

For the initial supported topology, all frame-producing work must be on the swapchain's native graphics queue with a verified terminal ordering point. Validate the queued Present ticket -> DoPresent wait relationship using a delayed upstream GPU workload and multiple Execute submissions. For multiple contributing queues, record their actual signal/wait dependencies and admit the frame only if the Present boundary is proven downstream of each contributor; do not join unrelated background work. Missing dependency attribution disables adaptation for that topology until its explicit join is implemented and verified. Present-blit GPUStartTime alone is not the start of application rendering.

If more than one swapchain could own a device-level frame and there is no unambiguous mapping, disable adaptive pacing for that device rather than guessing by window size or the last presenter. Keep native DXGI semantics. Settings and valid measurements remain available. A restored unique association starts a fresh estimator epoch.

FG creates extra presentation records, not extra simulation/admission calls. Reuse existing source/frame/generated annotations and preserve only-generated operation. In only-generated mode the original simulation still exists: retire it from its real rendering/accepted output boundary, never wait for a nonexistent original Present. If that boundary cannot be associated, adaptive pacing stays disabled for that mode. Native DLSS FG and unknown third-party FG must not be assumed to provide our FSR annotation; use verified OOB markers, or disable adaptation for ambiguous pairs.

## 5. Controller and wait contract

The selected baseline is the pinned LatencyFleX core, not a newly invented fixed delay:

1. Serialize controller updates under the context mutex. Before each admitted simulation call, get exactly one `GetWaitTarget(sequence)`.
2. Merge that absolute target with an explicit API minimum-frame-interval target using `max`, not two sequential sleeps. Low-latency Off with a nonzero interval still supports the requested limiter.
3. Release the state mutex before waiting. Wait on a monotonic deadline with a cancellation condition, never while holding the native device/queue/swapchain lock, async runtime lock, or FSR lease lock.
4. Recheck generation/settings/cancellation after waking. Record actual sleep duration separately. Call `BeginFrame` once, following the imported core's target-versus-actual timestamp contract; do not call `GetWaitTarget` twice for the same sequence.
5. On accepted original-frame workload completion, feed `EndFrame` once in sequence order. Duplicate or out-of-order receipts cannot advance a different frame. A failed/missing association invalidates the sample, not the native rendering operation.
6. Do not wait for the current frame to complete before the game is allowed to submit it. Warmup without valid history returns without adaptive delay. Ring-capacity exhaustion invalidates/reinitializes tracking instead of indexing over live records or introducing an indefinite GPU wait.

Start with one consistent host monotonic clock for controller begin and CPU-observed completion receipts. Label receipt latency as such: callback dispatch can lag hardware completion. Metal4 `MTL4CommitFeedback` exposes GPUStartTime/GPUEndTime, and legacy `MTLCommandBuffer` has corresponding timing properties. Calibrate their host-seconds domain before mixing them with controller nanoseconds or Windows QPC. Keep hardware timestamps as separate measurements until calibration is verified, including sleep/wake. Do not silently substitute callback time for a GPU timestamp in API reports.

The existing native DXGI semaphore/waitable handle remains the resource-capacity mechanism. The new controller only chooses pre-input admission timing; it must account for backpressure reflected in measured starts/completions rather than add another unconditional previous-frame fence wait. Existing application frame limiters remain application-owned. Do not modify VSync, drawable count, display mode or Game Mode automatically.

Lifecycle events (device/swapchain destruction, resize, mode change, occlusion, device loss, OFF transition and clock discontinuity) cancel waiters and invalidate the estimator generation. Completion observers carry weak references and immutable identity, not an unretained native object. They record/notify and return; they never wait for the simulation thread or call back into a native method while holding the scheduler mutex. Resize may preserve user settings, but not old frame associations.

The admission transaction is concrete: reserve `{generation, sequence, ownerThread}` under the mutex, calculate the adaptive target once, release for the condition/deadline wait, and commit Begin only if the same ticket is still current. A second concurrent admission never enters the controller. Cancellation after GetWaitTarget retires/resets that controller generation, since target calculation mutates it; no partially mutated estimator is reused. Previous-frame completion updates take the mutex briefly and may proceed while the caller is asleep.

Let `A` be the imported core's adaptive target, `L = previousActualAdmission + requestedInterval`, and `D = max(A, L)`. With no requested interval, omit L. If waiting is needed, retry spurious wakes until D or cancellation, then call `BeginFrame(sequence, A, D)`; if no wait is needed, use `BeginFrame(sequence, A, now)`. Keep actual wake as a separate measurement and use it for the next limiter anchor. This lets the core account for limiter-dominated delay without treating it as adaptive-target time. Settings changes cancel the pending ticket and restart estimation; do not carry an old cap's deadline into a new mode.

For lifetime cancellation, use a private-data lifetime sentinel on each canonical D3D device and DXGI swapchain, via the documented SetPrivateDataInterface contract, rather than retaining native objects indefinitely. The native private-data store is the sole long-lived COM owner of this sentinel; the scheduler must not AddRef the native owner or sentinel and form a cycle. The sentinel owns only a weak context reference. Its final Release removes the registry identity under the registry lock, marks that context generation closed under its mutex, then notifies waiters after unlocking. Real SetPrivateDataInterface/release behavior must be verified on both donor device interfaces before activation; if unavailable, that device remains unsupported rather than silently omitting destruction handling.

Resize/loss observations cancel the generation before old frame associations are reused. Callback observers capture weak context references plus immutable generation/sequence values, not native pointers or strong ownership of an entire session. On delivery, lock the weak reference, reject closed/stale generations, and drop it. Native copied blocks keep their original copy/dispose behavior and original callback execution exactly once. GPU completion retires the pacing record, while a separate bounded diagnostic ring retains its timing fields for a later presentation observation. New records evict completed old slots with a missing-display flag, and cancellation clears the generation; a late handler never writes into a recycled slot. No pacing wait depends on presentation observation. Limit outstanding observation registrations independently across generations, releasing the reservation on callback or native block disposal, so a stuck drawable cannot cause unbounded registration.

## 6. API semantics

### Reflex path

- Preserve the stock PE/Unix unwrapping ABI. Use official NVAPI struct definitions and version macros, not literals copied from decompiler output. Validate only supported public versions at the boundary; leave caller-owned reserved input bytes alone and fill defined output fields deterministically.
- `SetSleepMode`: persist settings per device; Off cancels adaptive waits, but retain the independent interval contract.
- `Sleep`: block only at this game-supplied admission point, once per simulation. No sleep in a marker callback, GPU completion callback, or presentation worker.
- `SetLatencyMarker`: call original recording, then record the same marker in our context. Unsupported/out-of-band markers must not invent simulation work. Missing/late IDs degrade measurement validity, not rendering correctness.
- `GetSleepStatus`: report effective activation and observed sleep, not merely requested settings. Mapping/feedback unavailability reports adaptive mode inactive. Boost has no verified Apple clock-control counterpart: accept the low-latency component, report locally that boost is unsupported, and do not claim Game Mode is equivalent.
- `GetLatency`: populate only SDK-compatible reports whose required timing semantics are actually known. Keep unsupported fields in their SDK-defined unavailable form; if the SDK version has no truthful partial-report encoding, return its documented unsupported/error status rather than fabricate timestamps. Maintain a separate versioned native diagnostic export with an explicit validity mask for richer partial observations.
- DLL/plugin resolution does not imply a particular game calls Sleep. Trace real game activation before claiming support for that title.

Initial report policy is deliberately explicit. GetSleepStatus supports the verified V1 ABI: `bLowLatencyMode` is effective controller activity; `sleepIntervalUs` is the latest completed Sleep duration in microseconds; `bUseGameSleep` reflects actual calls. NVIDIA control-panel overrides and Windows exclusive/iFlip state are not implemented and remain false, not inferred from CoreAnimation. Newer fields occupying formerly reserved bytes are not advertised without a verified version contract. GetLatency returns `NVAPI_NOT_SUPPORTED` until the required report semantics can all be populated truthfully; partial timings are available only through the validity-bearing native diagnostic export. Test Streamline's reaction to this status explicitly: if a title/plugin requires successful complete reports to activate pacing, that title remains unsupported until the full report mapping is implemented. Do not return a fabricated zero-filled success to bypass it.

### AMD Anti-Lag 2 adapter

After Reflex and attribution are correct, add minimal driver-extension adapters for the documented DX11/DX12 anti-lag interfaces, backed by the same controller. DX12 uses the SDK's IUnknown-derived IAmdExtAntiLagApi and exact IID. DX11 uses IAmdDxExtInterface's AddRef/Release and protected virtual-destructor ABI, followed by UpdateAntiLagStateDx11; it is not an IUnknown/QueryInterface vtable. Compile each adapter against the pinned SDK declarations with the matching Windows C++ ABI. Decode the SDK request identifier supplied through the DX11 output slot before overwriting it. Return unsupported for unrelated requests; do not emulate an entire AMD driver or change the advertised GPU identity.

The SDK uses GetModuleHandle, not LoadLibrary: simply packaging a DLL will not activate it. Choose an opt-in loader-owned preload, not an arbitrary LoadLibrary call from DllMain. In `dlls/ntdll/loader.c:4489-4629`, `loader_init` maps imports before `process_attach(node_ntdll/node_kernel32)`, application dependency attachment and EXE TLS callbacks. Map the two builtin adapters after successful import fixup, skipping a name already mapped to a non-owned vendor module; explicitly attach their dependency nodes after kernel32 and before application initialization. Use the loader's normal graph/refcount sequencing under its own loader lock. Adapter initialization must be inert: no Metal work, native device probing, sleeps or cross-thread waits from DllMain. Native association is lazy on the first explicit API call. Keep the modules mapped for process lifetime. Verify GetModuleHandle from an application's early TLS/static initializer as well as from main. Do not alter the initial loader sequence when the opt-in is off.

DX12 initialization supplies a device; DX11 creation supplies none. Bind DX11 only when exactly one supported native D3D11 context exists; otherwise return E_NOINTERFACE. AMD's ID-less input update is initially eligible for adaptive scheduling only with a proven single-thread Update -> Present sequence on that context. A multithreaded AMD rendering path requires a verified frame-index/end-of-render handoff; neither FIFO timing guesses nor the next arbitrary Present establish it. Handle the pinned SDK's v1 state/null update and v2 end-render/frame-type annotations. Unknown v2 query flags, including unproven index-query semantics, return E_INVALIDARG rather than invented output; document that extension limit.

Exactly one API frontend owns admission for a device at a time. Ownership is acquired atomically by the first accepted enabled-or-nonzero-limiter settings request. A competing frontend receives NVAPI_NOT_SUPPORTED or E_NOINTERFACE and does not change settings or wait; never acknowledge both controllers and guess whether their ID-less calls are the same frame. Off plus zero interval releases ownership only after cancelling the pending admission transaction and advancing the generation. Off with a nonzero limiter retains ownership. Owner disappearance is not inferred from a timer: release comes from explicit disabling, adapter final release or native device teardown. Test both initialization orders, concurrent settings, differing intervals, Off-with-calls and ownership changes. This conservative exclusion avoids lost frame limits and double sleeps.

## 7. Native timing and display observations

Use [MTL4CommitFeedback](https://developer.apple.com/documentation/metal/mtl4commitfeedback) and per-commit options to attach bounded observations to owned workloads. Commit options are not thread-safe; do not mutate a shared options instance or overwrite Apple's handler. Keep the original callback and resource retirement intact.

Register [MTLDrawable.addPresentedHandler](https://developer.apple.com/documentation/metal/mtldrawable/addpresentedhandler(_:)) before the original present scheduling call. Its presentedTime is a display-system observation, not physical input-to-photon measurement. Track dropped/never-presented records as unavailable and release them through independent lifecycle completion; no scheduler wait depends on that handler arriving.

[CAMetalDisplayLink](https://developer.apple.com/documentation/quartzcore/cametaldisplaylink) is useful background/reference for native engines, but this design does not add one beside D3DMetal's existing presenter. Owning another display loop or setting CAMetalLayer.maximumDrawableCount to one is not the chosen implementation.

## 8. Ordered implementation increments and acceptance

### A. ABI and attribution foundation

- Verify typed native dispatch interception, short-function patchability, device ownership and lifecycle boundaries. Preserve source hash rejection and unknown-build refusal.
- Extend the existing native contract/layout tests, not source-text tests. Execute the actual generated gates under x86_64/Rosetta.
- Add observation-only frame records at native queue acceptance, GPU completion and drawable presentation; separate async and low-latency enablement.
- Acceptance: every controlled successful original Present maps to exactly one frame; TEST/failed/nonadvancing Present maps to none. Multi-submit frames are not counted as multiple frames. Generated/real/only-generated, resize and pointer reuse cannot cross generations. Native output and completion/fence behavior remain identical with observation enabled.

### B. Real Reflex scheduling

- Import the pinned controller with license attribution. Implement settings, sleep admission, cancellation, effective-status readback and truthful reports.
- Deterministic tests use injected monotonic time and explicit completion signals: no fixed sleeps to make races pass. Cover GPU/CPU-bound phases, sudden long GPU work, oversleep, no completion, duplicate markers, multiple simulation threads, resize/destruction while waiting, and native callback reentrancy.
- Extend the public D3D11/D3D12 smoke style with real NVAPI + rendered frames, loaded from an isolated candidate runtime. The existing investigation probe is a baseline, not the implementation test.
- Acceptance: requested interval is honored over actual frames; Off releases waiters; adaptive On changes admission under a real GPU backlog; no double waits, deadlocks, resource corruption or false active status. Marker-only mode cannot be reported as completed low-latency support.

### C. AMD adapters and FG compatibility

- Run the unmodified AMD SDK against the new adapters. Cover module-loaded discovery and all documented packet/FG behaviors before advertising them.
- Verify shared-controller arbitration and generated/OOB frame attribution. Keep stock native FG paths intact.
- Acceptance: D3D11 and D3D12 SDK integration succeeds only for a supported device mapping; state/null packet ordering produces exactly one admission; arbitrary extension requests do not succeed as no-ops.

### D. Performance and latency qualification

Compare original runtime, observation-only candidate, and active scheduler in counterbalanced order with identical game/settings/window/VRR/VSync/FG/async state. Include CPU-bound and GPU-bound scenes, limiter on/off, supported/unsupported marker streams, alt-tab/resize and long shader compilation. Warm caches consistently; include cold compilation separately.

Report distributions, not only average FPS: original rendered frame interval, CPU submit-to-GPU start queue time, GPU execution time, completion callback lag, simulation/input-marker-to-GPU completion, GPU-end-to-presentedTime, requested/actual wait, outstanding application frames, and invalid/missing observation counts. A marker timestamp is not the electrical mouse event. Use a high-speed camera or hardware latency measurement for input-to-photon claims; otherwise label results marker-to-presentation.

Adoption requires a repeatable reduction in the targeted latency distribution beyond run-to-run noise, no reproducible steady-throughput regression, and no new missed-display-deadline tail. Report any tradeoff instead of hiding it under higher generated/display FPS. Numeric performance targets must be set from the measured baseline, not invented here.

Related existing checks to run once after affected edits: native contract/layout, async pipeline (epoch behavior), present residency, FSR lifecycle/normalization, and staged-runtime integrity. Baseline investigation did not run these unchanged suites. Full game input latency, DX11 API activation, physical display latency and AMD adapter execution remain unmeasured.

## 9. Explicit unknowns and stop rules

- Vendor driver internals and Reflex 2 frame-warp algorithms were not recovered. The plan implements compatible admission behavior using an open controller, not their proprietary algorithms.
- Native resolver Windows-x64 ABI and a safe six-byte instruction boundary were observed; callback capture layouts, private-data lifetime behavior and generated trampoline execution still need implementation-time verification. Never patch the three-byte Sleep function.
- A real game may suppress Reflex/Anti-Lag calls based on plugin, GPU or driver checks. Do not globally spoof capabilities to obtain calls.
- Multiple devices/swapchains, separate render threads and native FG may prevent unambiguous attribution. Adaptive mode is unavailable in those unresolved states; native rendering continues. These exclusions must be visible, not presented as universal low-latency support.
- Closed D3DMetal cannot be made correct by changing Wine's inactive DXGI implementation. Any native scheduling behavior change remains pinned to the authenticated image and is subject to the existing packaging/reseal checks.

The deliverable of this task is this design plus evidence. Product implementation and game-runtime installation require a subsequent implementation request; no performance improvement is claimed yet.

## 10. Independent review disposition

One independent read-only design review found seven blocking ambiguities in the first draft, not a code defect verdict. The final design addresses them with: dependency-proven frame completion; an explicit admission/marker/PresentCommand state machine; one cancellable controller transaction per generation; a defined merged-deadline timestamp formula; private-data lifetime sentinels and weak callback records; mutually exclusive frontend ownership; and separate AMD DX11/DX12 ABIs plus a concrete pre-entry bootstrap. The original review is retained in the evidence directory. These are design resolutions, not claims that the corresponding implementation tests have passed.
