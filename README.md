# Wine 11.17 D3DMetal (GPTK 4.0b2, experimental)

[한국어](README.ko.md)

Wine 11.17 runtime source for Yaagl, using GPTK 4.0b2 D3DMetal and MetalFX.
Targets Apple Silicon, macOS 26+, and Rosetta 2; macOS 26 hardware has not been tested. No standalone installer or Apple framework is bundled.

Current release: [experimental 6](https://github.com/dbc-hbin/wine-yaagl-d3dmetal/releases/tag/wine-11.17-gptk4.0b2-6).

## D3DMetal autopatch

Installation uses a native helper and bundled sidecar; Node.js, Python, and Xcode tools are not required.
Yaagl must obtain Apple license consent in its UI before invoking the helper with `--accept-apple-license`.
The helper resolves its sidecar beside the real executable, including PATH/symlink launches, and verifies downloads, patches, and signatures before publishing the framework.

## Build the autopatch bundle

Maintainers need Node.js and the Apple toolchain:

```sh
node scripts/build-d3dmetal-autopatch.mjs build/d3dmetal-autopatch
```

Output: `build/d3dmetal-autopatch/` and `build/d3dmetal-autopatch.tar.gz`.

## Package the Wine runtime

```sh
scripts/build-yaagl-overlay.sh BASE_WINE_ROOT build/yaagl-overlay
python3 scripts/package-yaagl-runtime.py BASE_WINE_ROOT build/yaagl-overlay build/d3dmetal-autopatch build/yaagl-release
```

The archive excludes Apple’s `D3DMetal.framework`; prepare it in `wine/lib/external/` before loading Wine.
Metal HUD hides the MetalFX “Frame Interpolator” row about 0.5 s after frame generation stops.

`BASE_WINE_ROOT` must match the pinned `f163d14` baseline and its full-tree manifest.
`scripts/wine-artifacts.json` lists the modules the overlay builder rebuilds and the packager replaces.

## Runtime internals

SR/FG resource mapping acquires the native resource owner once, checks output UAV access before extracting its Metal texture, and balances the owner and retained texture references on success and failure. Descriptor-layout byte checks remain fail-closed.
FG Prepare/Generate descriptors are normalized and validated once into a stack packet. Provider eligibility is rechecked under the context lock; native extensions/fallback, V1/V2 reset semantics, and callback-produced descriptors retain their existing handling.
Valid MetalFX/automatic-provider FFX memory-usage queries (V1/V2) return success with both byte counts set to zero, matching D3DMetal DLSS compatibility behavior. Zero means unreported usage, not a measured footprint; native FG/swapchain queries retain their native results. FFX SR/FG exports use the SDK cdecl ABI.
Native PSO and function caches coalesce concurrent creation and reuse live results only. Completed entries hold weak references, so a pipeline, its reflection, and extracted functions are freed when D3DMetal releases them; later requests are served by D3DMetal's and Metal's disk caches.
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
