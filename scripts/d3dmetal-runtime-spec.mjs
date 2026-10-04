import { METAL_IR_CONVERTER_4_0_BETA_2_SHA256 } from "./metalir-fp64-codec-patch.mjs";

// Build-host pins. The native installer receives these embedded in its recipe.
export const D3DMETAL_RELEASE = Object.freeze({
  repository: "https://github.com/dbc-hbin/d3dmetal-redistributable",
  tag: "gptk-4.0b2",
  assets: Object.freeze({
    framework: Object.freeze({
      name: "D3DMetal.framework-4.0b2.zip",
      sha256: "61ff2bb920376ce58709cabb081a5b5e7948c778939dbe66bfa0edca7c2f0d68",
    }),
    license: Object.freeze({
      name: "License.rtf",
      sha256: "5abb2d059be217663b00e8fd37e14411d374e11d17e3b744eebd49b8d17118c8",
    }),
    acknowledgements: Object.freeze({
      name: "Acknowledgements.rtf",
      sha256: "6f3aa835f6d0d06f89997d0a346a209e39a8105521fd939e096c5b24dc0cb0a6",
    }),
    sums: Object.freeze({
      name: "SHA256SUMS",
      sha256: "0b86038435fcb90a4bc1b27ab7c02fc02ad245365134731cbe6a5aeea2f1a973",
    }),
  }),
});

export const D3DMETAL_RUNTIME_HASHES = Object.freeze({
  pristineD3DMetal: "f5b56df1b8fe8b364dd9530651a3769c8aed948bd343be3b4510604d503e2bad",
  pristineConverter: METAL_IR_CONVERTER_4_0_BETA_2_SHA256,
  fp64Unsigned: "c4c5265e355c59b93e4684de79289ff2b7606756b70c25258dd8d29f59c3ea04",
  stagePatched: "40f495987a9f5acce9c602578dbef78d2b3999db90edba10e4192c033b736ec2",
  compositePreSign: "674e5aa6f6e5356fc2573d9ddaa56f9fa35b2a38f26e9e6ed31512b88c63650b",
  // Excludes the signature blob/range and normalizes __LINKEDIT signature-dependent sizes.
  compositePayload: "27b0e24395cf3e3f8816d183cd9a4ec5ff7db28e732c664656891c594ba6b070",
  // Accepted original builder inputs only; local signing results are never pinned.
  rawSidecar: "619c47060643287b3254c9ae3a97621a6e4a2df31cbb7be1d4b0c3edd0c03bfb",
  signedSidecar: "b254eb0f48faadf3e3b46a25e2e7537850c84ccc8a8fbe255e26d0b97f92e270",
});
