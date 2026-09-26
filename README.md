# Wine 11.17 D3DMetal (GPTK 4.0b2, experimental)

[한국어](README.ko.md)

Wine 11.17 runtime source for Yaagl, using GPTK 4.0b2 D3DMetal and MetalFX.
Targets Apple Silicon, macOS 26+, and Rosetta 2; macOS 26 hardware has not been tested. No standalone installer or Apple framework is bundled.

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

On macOS with Wine/P3 compiler dependencies and a verified `f163d14` staged Wine tree:

```sh
scripts/build-yaagl-overlay.sh BASE_WINE_ROOT build/yaagl-overlay
python3 scripts/package-yaagl-runtime.py BASE_WINE_ROOT build/yaagl-overlay build/d3dmetal-autopatch build/yaagl-release
```

The `wine/` archive includes the native autopatcher and GPTK companion modules, but excludes Apple’s `D3DMetal.framework`; prepare it in `wine/lib/external/` before loading Wine. `wine/yaagl-d3dmetal-runtime.json` inventories the package and activates its launch policy, which preserves explicit HUD/Timeout Fix settings. Deploy the rebuilt ntdll and wineserver together.

## Licenses

See [COPYING.LIB](COPYING.LIB) and [NOTICES.md](NOTICES.md). Apple GPTK has separate license terms.
