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
  compositePreSign: "ae2acc2fda582574d5010ef5855fde8c6363779236f94e0bc7892ee21a003ce4",
  compositePayload: D3DMETAL_PSO_CACHE_PATCHED_PAYLOAD_SHA256,
  finalD3DMetal: "0ae07b7e37404c9f8b409c77c46f3ac8a25223b7ea764339214be0d08d9c97d4",
  rawSidecar: "5bedd9bbb76e76ff4ae96bf03a8937d979069c10cb603255bd11194e95a1a0a4",
  signedSidecar: "bdbcf4657193391b2e4a090a215801a53a6e73b89b3ed851490e6e3f9458b6b4",
});
