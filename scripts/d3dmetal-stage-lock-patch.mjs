#!/usr/bin/env node

import { createHash, randomUUID } from "node:crypto";
import { chmod, readFile, rename, rm, stat, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const D3DMETAL_STAGE_LOCK_SOURCE_SHA256 =
  "32f8adb414806e63dcb46c4260f399a36b85454ad255e40c3dc5d8e0dfad95eb";
export const D3DMETAL_STAGE_LOCK_PATCHED_SHA256 =
  "61e23a262c7f7df9a36a612990a13cffea5fce41888d0e31e323a73a9f34e791";
export const D3DMETAL_STAGE_LOCK_PATCHED_SIGNATURE_NORMALIZED_SHA256 =
  "4339cd347a3b75556b4e632bc895d75bc0ba663f9ed26a06fb471f8267a29b00";
export const D3DMETAL_STAGE_LOCK_MACH_UUID =
  "674e662b6b5c3fd99a8af415609d2f6a";

const TEXT_END = 0x4ae000;
const EH_FRAME_END = 0x4ad690;
const HIT_THUNK_OFFSET = EH_FRAME_END;
const MISS_THUNK_OFFSET = 0x4ad69f;
const UNFAIR_LOCK_LOCK = 0x370870;
const UNFAIR_LOCK_UNLOCK = 0x370876;
const UNWIND_RESUME = 0x37033c;
const CANONICAL_GET_VALUE = 0x86d54;

const LOOKUPS = [
  ["vertex", 0x860d8, "e83b0c0000", "e852a72e00", "e838a72e00"],
  ["geometry", 0x86146, "e8210b0000", "e8e4a62e00", "e8caa62e00"],
  ["hull", 0x861b4, "e8250a0000", "e876a62e00", "e85ca62e00"],
  ["domain", 0x86222, "e829090000", "e808a62e00", "e8eea52e00"],
  ["mesh", 0x86290, "e82d080000", "e89aa52e00", "e880a52e00"],
  ["amplification", 0x862fe, "e831070000", "e82ca52e00", "e812a52e00"],
  ["fragment", 0x8636c, "e835060000", "e8bea42e00", "e8a4a42e00"],
  ["compute", 0x863da, "e839050000", "e850a42e00", "e836a42e00"],
  ["stage-in", 0x86448, "e83d040000", "e8e2a32e00", "e8c8a32e00"],
  ["stream-out", 0x864b6, "e841030000", "e874a32e00", "e85aa32e00"],
  ["node", 0x86524, "e845020000", "e806a32e00", "e8eca22e00"],
];

const LOOKUP_PREFIX = Buffer.from(
  "41574156415453504189f74989fc41ffcf488d5f404531f64889df31f6",
  "hex"
);
const LOOKUP_MIDDLE = Buffer.from(
  "498b442428498b4c24304829c148c1f9034c39f9760c4a8b3cf8",
  "hex"
);
const LOOKUP_RETURN = Buffer.from(
  "4989c64889df",
  "hex"
);
const LOOKUP_EPILOGUE = Buffer.from(
  "4c89f04883c4085b415c415e415fc34989c64889df",
  "hex"
);
const LOOKUP_UNWIND = Buffer.from("4c89f7", "hex");

function relativeInstruction(opcode, from, target) {
  const instruction = Buffer.alloc(5);
  instruction[0] = opcode;
  instruction.writeInt32LE(target - (from + 5), 1);
  return instruction;
}

function originalLookup(start, getValueCall, unlockCall, unwindUnlockCall) {
  return Buffer.concat([
    LOOKUP_PREFIX,
    relativeInstruction(0xe8, start + 0x1d, UNFAIR_LOCK_LOCK),
    LOOKUP_MIDDLE,
    Buffer.from(getValueCall, "hex"),
    LOOKUP_RETURN,
    Buffer.from(unlockCall, "hex"),
    LOOKUP_EPILOGUE,
    Buffer.from(unwindUnlockCall, "hex"),
    LOOKUP_UNWIND,
    relativeInstruction(0xe8, start + 0x69, UNWIND_RESUME),
  ]);
}

function patchedLookup(original, start) {
  const patched = Buffer.from(original);
  relativeInstruction(0xe8, start + 0x3c, HIT_THUNK_OFFSET).copy(patched, 0x3c);
  Buffer.from("eb0c90", "hex").copy(patched, 0x41);
  relativeInstruction(0xe8, start + 0x47, MISS_THUNK_OFFSET).copy(patched, 0x47);
  Buffer.from("909090", "hex").copy(patched, 0x4c);
  Buffer.from("eb06909090909090", "hex").copy(patched, 0x5e);
  return patched;
}

const hitThunk = Buffer.concat([
  // Preserve the wrapper argument while calling unlock, then restore the
  // original stack and tail-call getValue. An exception therefore unwinds
  // through the lookup's original +0x41 call-site and LSDA entry.
  Buffer.from("574889df", "hex"),
  relativeInstruction(0xe8, HIT_THUNK_OFFSET + 4, UNFAIR_LOCK_UNLOCK),
  Buffer.from([0x5f]),
  relativeInstruction(0xe9, HIT_THUNK_OFFSET + 10, CANONICAL_GET_VALUE),
]);
const missThunk = Buffer.concat([
  // The original miss path has already placed the outer lock in RDI. Balance
  // the extra call frame for SysV alignment, unlock, and return a null result.
  Buffer.from([0x50]),
  relativeInstruction(0xe8, MISS_THUNK_OFFSET + 1, UNFAIR_LOCK_UNLOCK),
  Buffer.from("5931c0c3", "hex"),
]);
const cavePatched = Buffer.concat([
  hitThunk,
  missThunk,
  Buffer.alloc(TEXT_END - EH_FRAME_END - hitThunk.length - missThunk.length),
]);

export const D3DMETAL_STAGE_LOCK_PATCH_SITES = [
  ...LOOKUPS.map(([name, start, getValueCall, unlockCall, unwindUnlockCall]) => {
    const expectedOriginal = originalLookup(
      start,
      getValueCall,
      unlockCall,
      unwindUnlockCall
    );
    if (expectedOriginal.length !== 0x6e) {
      throw new Error(`internal patch specification error for ${name}`);
    }
    return {
      name: `${name}-stage-lookup`,
      offset: start,
      expectedOriginal,
      patched: patchedLookup(expectedOriginal, start),
    };
  }),
  {
    name: "stage-lookup-thunks-in-text-padding",
    offset: EH_FRAME_END,
    expectedOriginal: Buffer.alloc(TEXT_END - EH_FRAME_END),
    patched: cavePatched,
  },
];

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function readName(bytes, offset) {
  return bytes.subarray(offset, offset + 16).toString("ascii").split("\0", 1)[0];
}

function validateMachO(bytes) {
  if (bytes.length < 32 || bytes.readUInt32LE(0) !== 0xfeedfacf) {
    throw new Error("unsupported binary: expected a little-endian 64-bit Mach-O");
  }
  if (bytes.readInt32LE(4) !== 0x01000007 || bytes.readUInt32LE(12) !== 6) {
    throw new Error("unsupported binary: expected an x86-64 MH_DYLIB");
  }

  const commandCount = bytes.readUInt32LE(16);
  const commandsSize = bytes.readUInt32LE(20);
  if (32 + commandsSize > bytes.length) {
    throw new Error("unsupported binary: truncated Mach-O load commands");
  }

  let cursor = 32;
  let uuid;
  let text;
  let linkEdit;
  let codeSignature;
  const textSections = [];
  const executableSections = [];
  for (let index = 0; index < commandCount; index += 1) {
    if (cursor + 8 > 32 + commandsSize) {
      throw new Error("unsupported binary: truncated Mach-O load command");
    }
    const command = bytes.readUInt32LE(cursor);
    const commandSize = bytes.readUInt32LE(cursor + 4);
    if (commandSize < 8 || cursor + commandSize > 32 + commandsSize) {
      throw new Error("unsupported binary: invalid Mach-O load command size");
    }
    if (command === 0x1b && commandSize >= 24) {
      uuid = bytes.subarray(cursor + 8, cursor + 24).toString("hex");
    }
    if (command === 0x19 && commandSize >= 72) {
      const segmentName = readName(bytes, cursor + 8);
      const sectionCount = bytes.readUInt32LE(cursor + 64);
      if (sectionCount > Math.floor((commandSize - 72) / 80)) {
        throw new Error("unsupported binary: truncated Mach-O sections");
      }
      const segment = {
        fileOffset: Number(bytes.readBigUInt64LE(cursor + 40)),
        fileSize: Number(bytes.readBigUInt64LE(cursor + 48)),
        maxProtection: bytes.readInt32LE(cursor + 56),
        initialProtection: bytes.readInt32LE(cursor + 60),
      };
      if (
        !Number.isSafeInteger(segment.fileOffset) ||
        !Number.isSafeInteger(segment.fileSize) ||
        segment.fileOffset + segment.fileSize > bytes.length
      ) {
        throw new Error("unsupported binary: invalid Mach-O segment file range");
      }
      if (segmentName === "__TEXT") {
        if (text) throw new Error("unsupported binary: duplicate __TEXT segment");
        text = segment;
      }
      if (segmentName === "__LINKEDIT") {
        if (linkEdit) throw new Error("unsupported binary: duplicate __LINKEDIT segment");
        linkEdit = segment;
      }
      for (let section = 0; section < sectionCount; section += 1) {
        const sectionOffset = cursor + 72 + section * 80;
        const entry = {
          name: readName(bytes, sectionOffset),
          offset: bytes.readUInt32LE(sectionOffset + 48),
          size: Number(bytes.readBigUInt64LE(sectionOffset + 40)),
        };
        if (!Number.isSafeInteger(entry.size)) {
          throw new Error("unsupported binary: invalid Mach-O section size");
        }
        if (segmentName === "__TEXT") textSections.push(entry);
        if ((segment.initialProtection & 4) !== 0) executableSections.push(entry);
      }
    }
    if (command === 0x1d) {
      if (commandSize !== 16) {
        throw new Error("unsupported binary: invalid LC_CODE_SIGNATURE size");
      }
      if (codeSignature) {
        throw new Error("unsupported binary: multiple LC_CODE_SIGNATURE commands");
      }
      codeSignature = {
        dataOffset: bytes.readUInt32LE(cursor + 8),
        dataSize: bytes.readUInt32LE(cursor + 12),
      };
    }
    cursor += commandSize;
  }
  if (cursor !== 32 + commandsSize) {
    throw new Error("unsupported binary: load command count/size mismatch");
  }

  if (uuid !== D3DMETAL_STAGE_LOCK_MACH_UUID) {
    throw new Error(`unsupported binary: expected Mach-O UUID ${D3DMETAL_STAGE_LOCK_MACH_UUID}, got ${uuid ?? "none"}`);
  }
  if (
    !text ||
    text.fileOffset !== 0 ||
    text.fileSize !== TEXT_END ||
    text.maxProtection !== 5 ||
    text.initialProtection !== 5
  ) {
    throw new Error("unsupported binary: unexpected executable __TEXT segment");
  }
  const ehFrame = textSections.find(section => section.name === "__eh_frame");
  if (!ehFrame || ehFrame.offset + ehFrame.size !== EH_FRAME_END) {
    throw new Error("unsupported binary: executable cave does not follow __eh_frame");
  }
  if (
    textSections.some(section =>
      section.offset < TEXT_END && section.offset + section.size > EH_FRAME_END
    )
  ) {
    throw new Error("unsupported binary: executable cave overlaps live section data");
  }
  if (!codeSignature || codeSignature.dataSize === 0) {
    throw new Error("unsupported binary: expected one non-empty LC_CODE_SIGNATURE");
  }
  const signatureEnd = codeSignature.dataOffset + codeSignature.dataSize;
  if (
    signatureEnd > bytes.length ||
    !linkEdit ||
    codeSignature.dataOffset < linkEdit.fileOffset ||
    signatureEnd > linkEdit.fileOffset + linkEdit.fileSize
  ) {
    throw new Error("unsupported binary: invalid LC_CODE_SIGNATURE file range");
  }
  for (const section of executableSections) {
    const sectionEnd = section.offset + section.size;
    if (sectionEnd > bytes.length) {
      throw new Error("unsupported binary: invalid executable section file range");
    }
    if (
      codeSignature.dataOffset < sectionEnd &&
      signatureEnd > section.offset
    ) {
      throw new Error("unsupported binary: code signature overlaps executable section data");
    }
  }
  return { codeSignature };
}

function signatureNormalizedSha256(bytes, codeSignature) {
  return createHash("sha256")
    .update(bytes.subarray(0, codeSignature.dataOffset))
    .update(Buffer.alloc(codeSignature.dataSize))
    .update(bytes.subarray(codeSignature.dataOffset + codeSignature.dataSize))
    .digest("hex");
}

function bytesAt(bytes, site) {
  const length = Math.max(site.expectedOriginal.length, site.patched.length);
  if (site.offset + length > bytes.length) return null;
  return bytes.subarray(site.offset, site.offset + length);
}

export function inspectD3DMetalStageLockPatch(bytes) {
  let layout;
  let layoutError;
  try {
    layout = validateMachO(bytes);
  } catch (error) {
    layoutError = error.message;
  }
  const hash = sha256(bytes);
  const normalizedHash = layout
    ? signatureNormalizedSha256(bytes, layout.codeSignature)
    : undefined;
  const sites = D3DMETAL_STAGE_LOCK_PATCH_SITES.map(site => {
    const value = bytesAt(bytes, site);
    let state = "unknown";
    if (value?.equals(site.expectedOriginal)) state = "original";
    if (value?.equals(site.patched)) state = "patched";
    return {
      name: site.name,
      offset: site.offset,
      state,
      value: value?.toString("hex") ?? "truncated",
    };
  });
  const states = new Set(sites.map(site => site.state));
  const allOriginal = states.size === 1 && states.has("original");
  const allPatched = states.size === 1 && states.has("patched");
  return {
    sha256: hash,
    signatureNormalizedSha256: normalizedHash,
    mode:
      !layoutError && allOriginal && hash === D3DMETAL_STAGE_LOCK_SOURCE_SHA256
        ? "original"
        : !layoutError && allPatched && hash === D3DMETAL_STAGE_LOCK_PATCHED_SHA256
          ? "patched"
          : !layoutError &&
              allPatched &&
              normalizedHash === D3DMETAL_STAGE_LOCK_PATCHED_SIGNATURE_NORMALIZED_SHA256
            ? "patched-signed"
            : "unknown-or-partial",
    layoutError,
    codeSignature: layout?.codeSignature,
    sites,
  };
}

export function applyD3DMetalStageLockPatch(bytes) {
  const inspection = inspectD3DMetalStageLockPatch(bytes);
  if (inspection.mode === "patched") return Buffer.from(bytes);
  if (inspection.mode !== "original") {
    throw new Error(
      `refusing patch: expected exact source SHA-256 ${D3DMETAL_STAGE_LOCK_SOURCE_SHA256}, got ${inspection.sha256} (${inspection.layoutError ?? inspection.mode})`
    );
  }

  const output = Buffer.from(bytes);
  for (const site of D3DMETAL_STAGE_LOCK_PATCH_SITES) {
    site.patched.copy(output, site.offset);
  }
  const outputInspection = inspectD3DMetalStageLockPatch(output);
  if (outputInspection.mode !== "patched") {
    throw new Error(
      `internal patch verification failed: expected ${D3DMETAL_STAGE_LOCK_PATCHED_SHA256}, got ${outputInspection.sha256}`
    );
  }
  return output;
}

async function patchFile(inputPath, outputPath) {
  const input = await readFile(inputPath);
  const output = applyD3DMetalStageLockPatch(input);
  const inputStat = await stat(inputPath);
  const temporaryPath = `${outputPath}.tmp-${randomUUID()}`;
  let temporaryCreated = false;
  try {
    await writeFile(temporaryPath, output, { flag: "wx" });
    temporaryCreated = true;
    await chmod(temporaryPath, inputStat.mode);
    await rename(temporaryPath, outputPath);
  } finally {
    if (temporaryCreated) await rm(temporaryPath, { force: true });
  }
  return inspectD3DMetalStageLockPatch(output);
}

const DEFAULT_INPUT =
  "build/wine-p3/gptk-overlay/wine/lib/external/D3DMetal.framework/Versions/A/D3DMetal";
const DEFAULT_OUTPUT = "build/d3dmetal-stage-lock/D3DMetal";

async function main(argv) {
  const [command, ...args] = argv;
  if (command === "inspect" && args.length <= 1) {
    const inputPath = args[0] ?? DEFAULT_INPUT;
    console.log(
      JSON.stringify(inspectD3DMetalStageLockPatch(await readFile(inputPath)), null, 2)
    );
    return;
  }
  if (command === "patch" && args.length <= 2) {
    const inputPath = args[0] ?? DEFAULT_INPUT;
    const outputPath = args[1] ?? DEFAULT_OUTPUT;
    console.log(JSON.stringify(await patchFile(inputPath, outputPath), null, 2));
    return;
  }
  throw new Error(
    "Usage: d3dmetal-stage-lock-patch.mjs inspect [binary] | patch [pristine-binary] [output]"
  );
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2)).catch(error => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
