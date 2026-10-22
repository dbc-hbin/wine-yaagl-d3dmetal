#!/usr/bin/env node

import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

const root = resolve(new URL("..", import.meta.url).pathname);
const input = resolve(process.argv[2] ?? resolve(root, "build/wine-11.17-gptk4b3-rx4060-clean/lib/external/D3DMetal.framework/Versions/A/D3DMetal"));
const output = resolve(process.argv[3] ?? resolve(root, "d3dmetal-sidecar/layout.json"));
if (process.argv.length > 4) throw new Error("usage: node scripts/generate-b3-sidecar-layout.mjs [D3DMETAL] [LAYOUT]");

const bytes = await readFile(input);
const sha256 = createHash("sha256").update(bytes).digest("hex");
if (sha256 !== "a64950e8324cec32c9a442daa1fafd03823bd199fdd84fee5c73679b5fe86660") {
  throw new Error(`unexpected beta3 D3DMetal SHA-256: ${sha256}`);
}
if (bytes.readUInt32LE(0) !== 0xfeedfacf || bytes.readUInt32LE(16) !== 33 || bytes.readUInt32LE(20) !== 4576) {
  throw new Error("unexpected beta3 Mach-O header");
}
let commonSectionCommandOffset;
for (let cursor = 32; cursor < 32 + bytes.readUInt32LE(20);) {
  const command = bytes.readUInt32LE(cursor);
  const commandSize = bytes.readUInt32LE(cursor + 4);
  if (command === 0x19) {
    const segmentName = bytes.subarray(cursor + 8, cursor + 24).toString("ascii").split("\0", 1)[0];
    const sectionCount = bytes.readUInt32LE(cursor + 64);
    for (let index = 0; index < sectionCount; index += 1) {
      const sectionOffset = cursor + 72 + index * 80;
      const sectionName = bytes.subarray(sectionOffset, sectionOffset + 16).toString("ascii").split("\0", 1)[0];
      const sectionSegment = bytes.subarray(sectionOffset + 16, sectionOffset + 32).toString("ascii").split("\0", 1)[0];
      if (segmentName === "__DATA" && sectionName === "__common" && sectionSegment === "__DATA") {
        commonSectionCommandOffset = sectionOffset;
      }
    }
  }
  cursor += commandSize;
}
if (!Number.isSafeInteger(commonSectionCommandOffset)) throw new Error("beta3 __common section command not found");

const hooks = [
  ["GetContainingOutput", 0x129760, "415741565657"],
  ["SetFullscreenState", 0x128fe4, "4156565755534881ec30010000"],
  ["QueryVideoMemoryInfo", 0x1678ae, "415741564154"],
  ["ReplayTemporalScale", 0x417e0, "554889e5415741564154"],
];
const formatVersion = 27;
const gateStart = 0x4e0040;
const slot = 0x563f18;
const markerOffset = gateStart + 0x780;
const markerHex = Buffer.concat([
  Buffer.from("YAAGLB3SIDEcar", "ascii"), Buffer.from([formatVersion]),
]).toString("hex");
const dependency = "@loader_path/Resources/libD3DMetalSidecar.dylib";
const dependencyCommand = Buffer.alloc(80);
dependencyCommand.writeUInt32LE(0x0c, 0);
dependencyCommand.writeUInt32LE(80, 4);
dependencyCommand.writeUInt32LE(24, 8);
dependencyCommand.write(dependency, 24, "utf8");
const rel32 = (from, target) => {
  const value = target - (from + 5);
  const buffer = Buffer.alloc(4);
  buffer.writeInt32LE(value, 0);
  return buffer.toString("hex");
};
const gateHex = (gate, dispatchOffset) => {
  const compare = Buffer.alloc(4);
  compare.writeInt32LE(slot - (gate + 8), 0);
  const load = Buffer.alloc(4);
  load.writeInt32LE(slot - (gate + 17), 0);
  const jump = Buffer.alloc(4);
  jump.writeInt32LE(dispatchOffset, 0);
  return Buffer.concat([
    Buffer.from("48833d", "hex"), compare, Buffer.from("00740e4c8b1d", "hex"), load,
    Buffer.from("41ffa3", "hex"), jump,
  ]).toString("hex");
};
const hooksOut = hooks.map(([id, entryOffset, originalHex], index) => {
  const original = Buffer.from(originalHex, "hex");
  const observed = bytes.subarray(entryOffset, entryOffset + original.length).toString("hex");
  if (observed !== originalHex) throw new Error(`${id} prologue mismatch at 0x${entryOffset.toString(16)}: ${observed}`);
  const gateOffset = gateStart + index * 0x40;
  const trampolineOffset = gateOffset + 0x18;
  const dispatchFieldOffset = index * 8;
  const entryPatch = Buffer.concat([
    Buffer.from("e9", "hex"), Buffer.from(rel32(entryOffset, gateOffset), "hex"),
    Buffer.alloc(original.length - 5),
  ]).toString("hex");
  const trampoline = Buffer.concat([
    original, Buffer.from("e9", "hex"), Buffer.from(rel32(trampolineOffset + original.length, entryOffset + original.length), "hex"),
  ]).toString("hex");
  return {
    id,
    entryOffset,
    gateOffset,
    trampolineOffset,
    continuationOffset: entryOffset + original.length,
    originalHex,
    entryPatchHex: entryPatch,
    gateHex: gateHex(gateOffset, dispatchFieldOffset),
    trampolineHex: trampoline,
    dispatchFieldOffset,
  };
});

const layout = {
  formatVersion,
  source: {
    sha256,
    machUuid: "4418ec47c3f03285a05615d87d785ffe",
    cpuType: 16777223,
    fileType: 6,
  },
  dependency: {
    path: dependency,
    commandHex: dependencyCommand.toString("hex"),
    commandOffset: 4608,
    commandSize: 80,
    originalCommandCount: 33,
    patchedCommandCount: 34,
    originalCommandsSize: 4576,
    patchedCommandsSize: 4656,
    firstTextOffset: 4720,
  },
  constructorVerification: {
    markerOffset,
    markerHex,
    description: "GPTK4b3 feature-only dispatch; native PSO, function, RT, and library caching remains unmodified.",
  },
  textCave: {
    offset: gateStart,
    endOffset: 0x4e1000,
    ehFrameEndOffset: 0x4e0040,
  },
  dispatch: {
    dataSlotOffset: slot,
    dataSlotVMAddr: slot,
    commonSectionCommandOffset,
    commonSectionOldSize: 0x4430,
    commonSectionNewSize: 0x4438,
    commonSectionOldEndVMAddr: slot,
    segmentEndVMAddr: 0x564000,
    pointerSize: 8,
    initialValue: 0,
  },
  hooks: hooksOut,
  binaryPatches: [],
  verificationSpans: [
    { id: "B3TextMarker", offset: markerOffset, expectedHex: markerHex },
  ],
};
// Derive the signature-independent output identity from the same recipe that
// produces the gates. The patcher pins this complete layout, not a second list
// of hook counts or a separately maintained payload hash.
const patched = Buffer.from(bytes);
patched.writeUInt32LE(layout.dependency.patchedCommandCount, 16);
patched.writeUInt32LE(layout.dependency.patchedCommandsSize, 20);
dependencyCommand.copy(patched, layout.dependency.commandOffset);
patched.writeBigUInt64LE(BigInt(layout.dispatch.commonSectionNewSize),
  commonSectionCommandOffset + 40);
Buffer.from(markerHex, "hex").copy(patched, markerOffset);
for (const hook of hooksOut) {
  Buffer.from(hook.entryPatchHex, "hex").copy(patched, hook.entryOffset);
  Buffer.from(hook.gateHex, "hex").copy(patched, hook.gateOffset);
  Buffer.from(hook.trampolineHex, "hex").copy(patched, hook.trampolineOffset);
}
let signatureOffset;
for (let cursor = 32; cursor < 32 + patched.readUInt32LE(20);) {
  const command = patched.readUInt32LE(cursor);
  const size = patched.readUInt32LE(cursor + 4);
  if (command === 0x1d) {
    signatureOffset = patched.readUInt32LE(cursor + 8);
    patched.fill(0, cursor + 8, cursor + 16);
  }
  if (command === 0x19 &&
      patched.subarray(cursor + 8, cursor + 24).toString("ascii").split("\0", 1)[0] === "__LINKEDIT") {
    patched.fill(0, cursor + 32, cursor + 40);
    patched.fill(0, cursor + 48, cursor + 56);
  }
  cursor += size;
}
if (!Number.isSafeInteger(signatureOffset)) throw new Error("beta3 code signature not found");
layout.patchedPayloadSha256 = createHash("sha256")
  .update(patched.subarray(0, signatureOffset)).digest("hex");
await writeFile(output, `${JSON.stringify(layout, null, 2)}\n`);
console.log(JSON.stringify({
  input, output, sha256, hookCount: hooksOut.length,
  layoutSha256: createHash("sha256").update(`${JSON.stringify(layout, null, 2)}\n`).digest("hex"),
  patchedPayloadSha256: layout.patchedPayloadSha256,
  markerOffset: `0x${markerOffset.toString(16)}`, dispatchSlot: `0x${slot.toString(16)}`,
}, null, 2));
