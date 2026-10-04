# Wine 11.17 D3DMetal (GPTK 4.0b2, experimental)

[한국어](README.ko.md)

Wine 11.17 runtime source for Yaagl, using GPTK 4.0b2 D3DMetal and MetalFX.
Targets Apple Silicon, macOS 26+, and Rosetta 2; macOS 26 hardware has not been tested. No standalone installer or Apple framework is bundled.

Current release: [experimental 9](https://github.com/dbc-hbin/wine-yaagl-d3dmetal/releases/tag/wine-11.17-gptk4.0b2-9).

## D3DMetal autopatch

Installation uses a native helper and bundled sidecar; Node.js, Python, and Xcode tools are not required.
Yaagl must obtain Apple license consent in its UI before invoking the helper with `--accept-apple-license`.
The helper resolves its sidecar beside the real executable, including PATH/symlink launches, and verifies downloads, patches, and signatures before publishing the framework.
Original assets and patch results remain SHA-256 pinned. All patches are applied before final signing; locally re-signed files are checked with `codesign` rather than fixed whole-file hashes, while the signature-independent D3DMetal payload hash remains enforced. The prepared manifest records the actual final file hashes.

## Build the autopatch bundle

Maintainers need Node.js and the Apple toolchain:

```sh
node scripts/build-d3dmetal-autopatch.mjs build/d3dmetal-autopatch
```

Output: `build/d3dmetal-autopatch/` and `build/d3dmetal-autopatch.tar.gz`.

## Package the Wine runtime

```sh
export GSTREAMER_ROOT=/path/to/development/GStreamer.framework/Versions/1.0
export WINE_DEPS_ROOT=/path/to/development/opt/local
scripts/build-yaagl-overlay.sh BASE_WINE_ROOT build/yaagl-overlay
python3 scripts/package-yaagl-runtime.py BASE_WINE_ROOT build/yaagl-overlay build/d3dmetal-autopatch build/yaagl-release
```

The archive excludes Apple’s `D3DMetal.framework`; prepare it in `wine/lib/external/` before loading Wine.
Metal HUD hides the MetalFX “Frame Interpolator” row about 0.5 s after frame generation stops.

`BASE_WINE_ROOT` must match the pinned `f163d14` baseline and its full-tree manifest.
The overlay uses `GSTREAMER_ROOT` for GStreamer/FFmpeg development headers and pkg-config metadata. It defaults to the framework inside `BASE_WINE_ROOT` only when its development files are present; the pruned release baseline needs a separate development framework. Optional `WINE_DEPS_ROOT` supplies an external development prefix (`include/`, `lib/`, `lib/pkgconfig/`) for required dependencies such as GnuTLS and SDL. pkg-config relocates each package to its own SDK prefix; these inputs apply only to x86_64, not the ARM64 server build. Do not add files to the pinned baseline or disable its required configure features.
`scripts/wine-artifacts.json` lists the modules the overlay builder rebuilds and the packager replaces.

## Runtime fixes

XAudio2 uses a separate registry lock, stable slots and registration generations. Neither external callbacks nor allocation/free run under that lock. Foreign-thread unregister drains only matching active callbacks; self/next removal and registration during dispatch are safe, with new registrations deferred to a later dispatch. All 10 scenarios passed for versions 0–9 on x64/i386 (200 cases), plus version 9 with tracing enabled on both architectures (20 cases): first-registration interleave, allocation failure/recovery, Start/End reentrancy, unrelated worker/voice progress, synchronous unregister, allocation-gated growth and teardown. CriticalError shares the dispatcher but actual device-loss notification was not exercised.

Unregister remains synchronous: a callback must not wait for a worker unregistering that same callback. Final Release/master-voice destruction from the callback thread and recovery from client exceptions escaping into FAudio are not supported. The registry removes its own stack record during exception unwind; it does not claim to repair FAudio exception handling.

Audio checks: `python3 scripts/test-xaudio-callback-runtime.py RUNTIME --arch x86_64` and `--arch i386`; add `--version 9 --trace` for diagnostic-on scenarios. These launch synthetic silent-audio PE guests, never the game.

Final XAudio2 Release now clears and deletes the embedded mastering-voice critical section, matching the engine, callback-registry and pooled-voice locks. Previously its debug-info allocation survived even when no mastering voice was created. The new `--case lock-lifetime` regression warms up once, then checks process-heap live-byte balance across eight public engine creation/Release cycles, without source, RVA, label or private-layout assertions. It passes for versions 0–9 on x64/i386 with tracing off and for version 9 on both architectures with tracing on; version 9 foreign-thread audio teardown also passes with tracing on. The isolated candidate is `build/xaudio-lock-fixed/wine`; no installed runtime was changed and no game was launched. Closed synthetic x64 traces show nine 48-byte mastering-lock allocations (warmup plus eight cycles): the previous runtime freed none, while the fixed candidate freed all nine. Exact birth/free evidence is in `build/xaudio-lock-fixed/mastering-lock-lifetimes.json`, with the raw before/after traces in `build/xaudio-engine-heap-before/` and `build/xaudio-engine-heap-after/`. This proves a small per-engine leak, not the cause of megabytes of game-memory growth or any game crash.

On macOS, `MEM_RESET` uses `MADV_FREE` rather than `MADV_DONTNEED`, so abandoned dirty anonymous pages can be discarded without paging their old contents. Commitment, protection and subsequent writes remain valid; immediate RSS reduction is not required. `python3 scripts/test-memory-reset-runtime.py RUNTIME` (also `--arch i386`) observes the real native call across the full advised span and checks the Windows API contract. The isolated candidate `build/memory-reset-reclaim-20261004/wine` passed both architectures; the baseline failed the dirty-page check. In the game capture, the two RESET spans were fully decommitted 0.307 s and 0.078 s later, so this fix does not explain sustained growth.

## Runtime internals

Private FSR JSON diagnostics (`YAAGL_FSR_LOG`) and their diagnostic-only counters/metadata are removed. The internal MetalFX contract stores values directly without unused presence flags; defaults, validation, SDK debug callbacks, error returns and rendering behavior are unchanged. Other custom Wine tuning is retained.

Fullscreen target forwarding covers direct COM calls, Wine unixcalls, and the legacy factory creation path. The legacy implicit-target call explicitly passes null instead of treating a leftover register value as an output interface; explicit targets and HRESULT propagation are preserved.
Metal4 presentation keeps scoped layer-residency registration: the set is detached after submission/presentation scheduling, without removing residency from already-submitted work. Isolated real-GPU layer-churn comparisons found that stock queue associations retained empty sets until queue destruction; scoped registration did not. Drawable-size changes reused the same set. This justifies registration cleanup, not a claim of leaked GPU allocations or improved FPS.

SR/FG resource mapping acquires the native resource owner once, checks output UAV access before extracting its Metal texture, and balances the owner and retained texture references on success and failure. Descriptor-layout byte checks remain fail-closed.
Legacy SR/FG replay rejects foreign command tags before copying the payload. The final pre-scale blit encoder is closed by the pinned native `GetExternalCommandBuffer` call, without a duplicate explicit flush. The earlier all-encoder flush, fence updates/waits, synchronization and completion-owned resource lifetimes are unchanged.
FG Prepare/Generate descriptors are normalized and validated once into a stack packet. Provider eligibility is rechecked under the context lock; native extensions/fallback, V1/V2 reset semantics, and callback-produced descriptors retain their existing handling.
Direct FG context destruction releases cached MetalFX configurations and history even while recorded frames retain the context. Those frames keep their own required resources; replay after destruction resets history without repopulating retired state. The active eight-entry cache is unchanged. `python3 scripts/test-fg-normalization.py` exercises destruction and queued replay through real GPU completion. This fixes excessive teardown lifetime, not a demonstrated continuous per-frame leak.
Valid MetalFX/automatic-provider FFX memory-usage queries (V1/V2) return success with both byte counts set to zero, matching D3DMetal DLSS compatibility behavior. Zero means unreported usage, not a measured footprint; native FG/swapchain queries retain their native results. FFX SR/FG exports use the SDK cdecl ABI.
The sidecar coalesces equivalent PSO creation across native pipeline owners and reuses only live results. Completed entries hold weak references to PSOs and key resources; reflection is associated with its PSO and lives until that PSO is released. Only the `ExtractFunctions` and `LoadGraphicsFunctions` hooks are restored for function caching: verified callers and effective specialization constants scope reuse to the same device and live library. Equivalent requests share one producer, completed functions remain weakly held, and device retirement clears their cache. Misses and ineligible requests retain native extraction. Run `python3 scripts/test-function-cache.py` for lifetime, concurrency, key separation and real Metal GPU checks.
Outer compute/graphics stage compilation and stage-key creation are not hooked: D3DMetal retains its native stage caches and pending-result synchronization. A direct native patch frees only the losing 88-byte compute stage table after duplicate insertion, preserving the canonical table and borrowed stage results; `node --test scripts/d3dmetal-compute-loser.test.mjs` covers its cleanup paths. The separate stage-ID lookup patch still releases the outer lock before waiting. Library creation singleflight and cross-pipeline live PSO reuse remain; dispatch layout 21 contains 18 generic and five special entries. The native compiler, disk caches and exp 7 correctness fixes are retained. These changes do not establish a game-FPS improvement.
Cache records publish their committed end only after writing the payload checksum and negative-length footer. Exact-byte patches cover standalone `D3DMCacheFile::End` and all 16 audited inline copies: twelve shader-stage serializers, compute/graphics pipeline-stage records, bytecode, and root signatures. The record format is unchanged; this fixes publication order, not power-loss durability or disk flushing. Run `node --test scripts/d3dmetal-cache-publication.test.mjs` for instruction-level visibility and interrupted-checksum regressions.
`QueryVideoMemoryInfo` reports a fixed budget: the local segment group gets `recommendedMaxWorkingSetSize`, capped by the memory Windows grants graphics on the same RAM (`min(RAM × 80%, max(RAM − 16 GB, RAM × 50%))`, e.g. 8 GB on 16 GB and 12 GB on 24 GB), as its budget and the full 64-bit `currentAllocatedSize` as usage. The non-local group is all zero, as D3D12 specifies for UMA adapters. Stock D3DMetal reports twice the recommended size and truncates usage to 32 bits. The budget does not follow system memory because D3DMetal's `Evict` and `MakeResident` are no-ops; a shrinking budget would only make the game lower texture detail without freeing memory. Reservations remain unimplemented and are reported as zero.

An ordinary C++ function-entry hook runs the original adapter constructor, then sets `DedicatedVideoMemory` to that budget, `DedicatedSystemMemory` to zero, and `SharedSystemMemory` to physical RAM minus dedicated video memory, without another cap. For example, 32 GiB RAM with 16 GiB dedicated reports 16 GiB shared; 64 GiB with 48 GiB dedicated reports 16 GiB shared. If physical RAM cannot be read, shared memory is zero; subtraction never underflows. This updates the stored description once, covering all four `GetDesc` versions, including COM and Wine unixcall paths that inline the copy. The three memory fields override `gpuinfo` values; identity, LUID, and other descriptor fields are unchanged. This is a UMA compatibility reporting policy, not an exact reproduction of a Windows driver, an additional allocation, or a memory-use reduction.

Focused checks: `python3 scripts/test-msync-message-dispatch.py`, `python3 scripts/test-resource-map.py`, `python3 scripts/test-fg-normalization.py`, `python3 scripts/test-fsr-memory-query.py`, and `python3 scripts/test-wine-artifact-catalog.py`.

## Cursor handling

The cursor/RawInput server protocol is 966.
Programmatic cursor updates and native owner queries share strict active-window hit-testing; AppKit-routed cursor updates retain their separate window check.
Direct `CGWarpMouseCursorPosition` moves record actual displacement. A later nonzero mouse-move delta consumes matching records in one pass and subtracts only that displacement before legacy/RawInput processing. Zero-delta notifications leave records pending; activation changes clear them. EventTap clipping retains its separate correction path.
Run `python3 scripts/test-cursor-warp-correction.py` for the input-preservation regressions.

## Licenses

See [COPYING.LIB](COPYING.LIB) and [NOTICES.md](NOTICES.md). Apple GPTK has separate license terms.
