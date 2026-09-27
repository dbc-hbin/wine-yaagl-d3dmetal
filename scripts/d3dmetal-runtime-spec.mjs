import {
  METAL_IR_CONVERTER_4_0_BETA_2_FP64_PATCHED_SHA256,
  METAL_IR_CONVERTER_4_0_BETA_2_SHA256,
} from "./metalir-fp64-codec-patch.mjs";
import {
  D3DMETAL_STAGE_LOCK_PATCHED_SHA256,
  D3DMETAL_STAGE_LOCK_SOURCE_SHA256,
} from "./d3dmetal-stage-lock-patch.mjs";
import { D3DMETAL_PSO_CACHE_PATCHED_PAYLOAD_SHA256 } from "./d3dmetal-pso-cache-patch.mjs";

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
  fp64Signed: METAL_IR_CONVERTER_4_0_BETA_2_FP64_PATCHED_SHA256,
  compositeInput: D3DMETAL_STAGE_LOCK_SOURCE_SHA256,
  stagePatched: D3DMETAL_STAGE_LOCK_PATCHED_SHA256,
  compositePreSign: "b7b06c767d4ec71ea76a3fd8918a79db0e7fdbb30a555062adff61bfd5f4e1b0",
  compositePayload: D3DMETAL_PSO_CACHE_PATCHED_PAYLOAD_SHA256,
  finalD3DMetal: "38268233fac2f176436237415772dd4cf628376158c5f95d44138f3b76402664",
  rawSidecar: "31c10c5f81b91e7bddb2036511752c170c5b47a1331e0b99c3ad54957e9dfa26",
  signedSidecar: "656d6e0001c5102c49534d7cee5cc1b1ae2819c74aeee0e9c1c7a9c6adcbb196",
});
