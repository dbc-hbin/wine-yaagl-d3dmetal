import { D3DMETAL_SIDECAR_PATCHED_PAYLOAD_SHA256 } from "./d3dmetal-sidecar-patch.mjs";

// Measured from Apple's local GPTK 4.0 beta 3 distribution, not a remote release.
export const D3DMETAL_RELEASE = Object.freeze({
  version: "4.0b3",
  dmg: Object.freeze({ name: "Game_Porting_Toolkit_4.0_beta_3.dmg", sha256: "56a7d0bd3d5375267d2d58e7f947c53fbf2fa636a824d2cd68ac2bf8c0c11e65" }),
  evaluationDmg: Object.freeze({ name: "Evaluation environment for Windows games 4.0 beta 3.dmg", sha256: "afa311da371f5b6dd970b45378a3d3df567cc9e535818f56f9b3f9d4c9537c1d" }),
  frameworkPath: "redist/lib/external/D3DMetal.framework",
  license: Object.freeze({ name: "License.rtf", sha256: "5abb2d059be217663b00e8fd37e14411d374e11d17e3b744eebd49b8d17118c8" }),
  acknowledgements: Object.freeze({ name: "Acknowledgements.rtf", sha256: "6f3aa835f6d0d06f89997d0a346a209e39a8105521fd939e096c5b24dc0cb0a6" }),
});

export const D3DMETAL_RUNTIME_HASHES = Object.freeze({
  pristineD3DMetal: "a64950e8324cec32c9a442daa1fafd03823bd199fdd84fee5c73679b5fe86660",
  pristineConverter: "7a5ddc8081054b2c09ede8df1a9a7190fa7c00f3c219232f6e269b4ce02f6ca7",
  compositePayload: D3DMETAL_SIDECAR_PATCHED_PAYLOAD_SHA256,
});

export const D3DMETAL_FRAMEWORK_RESOURCE_HASHES = Object.freeze({
  "Info.plist": "26e3166820a15850f9977e2336ee7f4d1237f402c8520a1bd30716fc2655ea18",
  "LICENSE": "553d0035773ddd1590045f8fdc3a4c6ead31e36336721aeca8421e88ed1c9f80",
  "default.metallib": "c1ea924b9818099d60c861eedb23266632a7db39240b2412a7d405c3a7695408",
  "libdxccontainer.dylib": "62e2faa5bf5a7d6f2bf5accae75fe5b0f2c84e261e1b4b011ff947ed48ee245c",
  "libdxcompiler.dylib": "eaa33a6476ea5310261b55f800907c40b99f9dc91e1b0f2cb40bc3cd190f4f09",
  "libdxilconv.dylib": "568c7006672394e9d077a0a8de2e464a2471a77bcefe9f1b79e5396d03c24472",
  "libmetalirconverter.dylib": D3DMETAL_RUNTIME_HASHES.pristineConverter,
  "version.plist": "a1989325e56ef9a28b17d7320d6ce713693aa44f50fe309d33263a9a99918b3e",
});

export const D3DMETAL_STOCK_MODULE_HASHES = Object.freeze({
  "lib/external/libd3dshared.dylib": "a70ba718a06c1372e0c7fe747dcd4c3ba6262791ccf0dd5cdebe6a8921c378ef",
  "lib/wine/x86_64-windows/d3d10.dll": "78c2cd82697dde7081bae72c0ebd82eb41131e7c80b1f99d16ca5f54e5e962e1",
  "lib/wine/x86_64-windows/d3d11.dll": "39b05a887598697d46225f998354c41582d12b3d35fceb4509df9b2386a10b2e",
  "lib/wine/x86_64-windows/d3d12.dll": "3174fac027d3ff10971645ac41b665cd53949c87d6d2d7d53b0073bf4bb36981",
  "lib/wine/x86_64-windows/d3d12core.dll": "8fee579ce538f5a56de72d8ff64dec9d2f018496358069a0216f29a9330ff7fd",
  "lib/wine/x86_64-windows/dxgi.dll": "d53d9ea91935f0de126ba528264fc4c9b4dfea8bfe43f3f1256bd3e8963d67ee",
  "lib/wine/x86_64-windows/nvapi64.dll": "de4872e71e6b3f54775b3e564cc487d984c52fec6711bfee250228843ba762d0",
  "lib/wine/x86_64-windows/nvngx-on-metalfx.dll": "2e522873d2dec412c29f73096f1c1dc6bd8016dc40fd76fbeddd2eb7e226d3cb",
});
