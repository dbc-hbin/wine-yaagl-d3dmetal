# Wine 11.17 D3DMetal (GPTK 4.0b2, experimental)

[한국어](README.ko.md)

Wine 11.17 runtime source for Yaagl, using GPTK 4.0b2 D3DMetal and MetalFX.
Targets Apple Silicon, macOS 26+, and Rosetta 2; macOS 26 hardware has not been tested. No standalone installer or Apple framework is bundled.

Current release: [experimental 4](https://github.com/dbc-hbin/wine-yaagl-d3dmetal/releases/tag/wine-11.17-gptk4.0b2-4).

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
`scripts/wine-artifacts.json` is the shared artifact catalog for the tuned/safe-msync source builds and the Yaagl overlay builder/packager.
Legacy P3/split packagers and their staging helpers are removed. `build-wine-tuned.sh` remains source-build tooling; it no longer advertises a legacy packaging handoff.
Its `install` action finishes after publishing the profile host and provenance; release packaging is a separate operation through the commands above.

## Runtime internals

The MSync patch stack is consolidated in `patches/wine-tuned/0001-msync-tuned.patch`, without follow-up overrides. Apply it through the source builder’s `patch` flow.
SR/FG resource mapping acquires the native resource owner once, checks output UAV access before extracting its Metal texture, and balances the owner and retained texture references on success and failure. Descriptor-layout byte checks remain fail-closed.
FG Prepare/Generate descriptors are normalized and validated once into a stack packet. Provider eligibility is rechecked under the context lock; native extensions/fallback, V1/V2 reset semantics, and callback-produced descriptors retain their existing handling.
Valid MetalFX/automatic-provider FFX memory-usage queries (V1/V2) return success with both byte counts set to zero, matching D3DMetal DLSS compatibility behavior. Zero means unreported usage, not a measured footprint; native FG/swapchain queries retain their native results. FFX SR/FG exports use the SDK cdecl ABI.

Focused checks: `python3 scripts/test-msync-message-dispatch.py`, `python3 scripts/test-resource-map.py`, `python3 scripts/test-fg-normalization.py`, `python3 scripts/test-fsr-memory-query.py`, and `python3 scripts/test-wine-artifact-catalog.py`.

## Cursor handling

The tuned cursor/RawInput changes are consolidated in `patches/wine-tuned/0004-macdrv-reset-rawinput-baseline.patch`; the cursor protocol remains 966.
Programmatic cursor updates and native owner queries share strict active-window hit-testing; AppKit-routed cursor updates retain their separate window check.
Direct `CGWarpMouseCursorPosition` moves record actual displacement. A later nonzero mouse-move delta consumes matching records in one pass and subtracts only that displacement before legacy/RawInput processing. Zero-delta notifications leave records pending; activation changes clear them. EventTap clipping retains its separate correction path.
Run `python3 scripts/test-cursor-warp-correction.py` for the input-preservation regressions.

## Licenses

See [COPYING.LIB](COPYING.LIB) and [NOTICES.md](NOTICES.md). Apple GPTK has separate license terms.
