# Yaagl Wine DX12 runtime

[한국어](README.ko.md)

Wine 11.17 runtime source for Yaagl, using GPTK 4.0b2 D3DMetal and MetalFX.
Requires Apple Silicon, macOS 26+, and Rosetta 2. No standalone installer or Apple framework is bundled.

## D3DMetal autopatch

Installation uses a native helper and bundled sidecar; Node.js, Python, and Xcode tools are not required.
Yaagl must obtain Apple license consent in its UI before invoking the helper with `--accept-apple-license`.
The helper verifies pinned downloads, patches, and signatures before publishing the prepared framework.

## Build the autopatch bundle

Maintainers need Node.js and the Apple toolchain:

```sh
node scripts/build-d3dmetal-autopatch.mjs build/d3dmetal-autopatch
```

Output: `build/d3dmetal-autopatch/` and `build/d3dmetal-autopatch.tar.gz`.

With `MTL_HUD_ENABLED=1`, Metal HUD keeps its MetalFX “Frame Interpolator” row after the first interpolation. When no MetalFX frame-generation context is still interpolating, the sidecar removes that row about 0.5 s later; the next interpolation shows it again.

## Licenses

See [COPYING.LIB](COPYING.LIB) and [NOTICES.md](NOTICES.md). Apple GPTK has separate license terms.
