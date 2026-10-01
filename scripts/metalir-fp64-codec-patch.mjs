#!/usr/bin/env node

import { createHash, randomUUID } from "node:crypto";
import { chmod, readFile, rename, rm, stat, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const METAL_IR_CONVERTER_4_0_BETA_2_SHA256 =
  "75974d49ad4dd1bdf17ab3cd666ae7cac43e7f7a5760237699ab33ecd3d31daf";
// Pinned output of FP64_CODEC_PATCH_SITES applied to the 4.0b2 converter.
export const METAL_IR_CONVERTER_4_0_BETA_2_FP64_PATCHED_SHA256 =
  "5c5619ef17a7d62e84db0a7f5181d746623b47364379271fd5827e6bd961ba34";

/**
 * Experimental narrow fix for the codec-shaped FP64 islands observed in the
 * twelve captured ZZZ shaders. This changes AIRBuilder::patchFP64Operations
 * in GPTK 4.0 beta 2 and adds an i32 guard in executable __TEXT padding.
 *
 * This deliberately does not claim to be a general FP64 lowering fix. In
 * particular, the constrained-SIToFP branch still uses the converter's
 * existing constrained-UIToFP intrinsic. Captured shaders use the normal
 * non-constrained path; product validation must keep that limitation visible.
 */
export const FP64_CODEC_PATCH_SITES = [
  {
    name: "preserve-i1-and-route-other-integer-sources",
    offset: 0xa19f88,
    // Keep cmp 0x7d8(%r9), %rax: i1 still falls through to 0xa19f95.
    // Only jne changes, from error 0xa1b3d6 to the i32 guard 0x135a4f0.
    expectedOriginal: Buffer.from("493b81d80700000f8541140000", "hex"),
    patched: Buffer.from("493b81d80700000f855b059400", "hex"),
  },
  {
    name: "guard-i32-in-executable-padding",
    offset: 0x135a4f0,
    // Pristine SHA-locked Mach-O: __TEXT has fileoff/vmaddr 0, end
    // 0x135b000, maxprot/initprot RX (5). Its last section, __eh_frame,
    // ends at 0x135a4f0. All 0xb10 remaining bytes are zero and belong to
    // no section. Pin the entire padding, including the unused tail.
    expectedOriginal: Buffer.alloc(0xb10),
    // cmp 0x7f0(%r9), %rax; je 0xa19f95; jmp 0xa1b3d6.
    // No stack/register writes or calls; only flags from the comparison.
    patched: Buffer.concat([
      Buffer.from("493b81f00700000f8498fa6bffe9d40e6cff", "hex"),
      Buffer.alloc(0xb10 - 18),
    ]),
  },
  {
    name: "route-si-to-fp-to-integer-cast-handler",
    offset: 0xa1b900,
    expectedOriginal: Buffer.from([0xf7, 0xfa, 0xff, 0xff]),
    patched: Buffer.from([0xb5, 0xe1, 0xff, 0xff]),
  },
  {
    name: "select-signed-or-unsigned-fp-cast-opcode",
    offset: 0xa1a147,
    expectedOriginal: Buffer.from([0xbe, 0x2b, 0x00, 0x00, 0x00]),
    // lea -0x1c(%rbp), %esi; nop; nop
    // ValueID 71 (UIToFP) -> opcode 43, ValueID 72 (SIToFP) -> opcode 44.
    patched: Buffer.from([0x8d, 0x75, 0xe4, 0x90, 0x90]),
  },
];

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function readMachOName(bytes, offset) {
  return bytes.subarray(offset, offset + 16).toString("ascii").split("\0", 1)[0];
}

export function signatureNormalizedSha256(bytes) {
  if (bytes.length < 32 || bytes.readUInt32LE(0) !== 0xfeedfacf) {
    throw new Error("unsupported binary: expected a little-endian 64-bit Mach-O");
  }
  const commandCount = bytes.readUInt32LE(16);
  const commandsSize = bytes.readUInt32LE(20);
  if (32 + commandsSize > bytes.length) {
    throw new Error("unsupported binary: truncated Mach-O load commands");
  }

  let cursor = 32;
  let linkEdit;
  let codeSignature;
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
    if (command === 0x19) {
      if (commandSize < 72) {
        throw new Error("unsupported binary: invalid segment command size");
      }
      const sectionCount = bytes.readUInt32LE(cursor + 64);
      if (sectionCount > Math.floor((commandSize - 72) / 80)) {
        throw new Error("unsupported binary: truncated Mach-O sections");
      }
      const segment = {
        fileOffset: Number(bytes.readBigUInt64LE(cursor + 40)),
        fileSize: Number(bytes.readBigUInt64LE(cursor + 48)),
        initialProtection: bytes.readInt32LE(cursor + 60),
      };
      if (
        !Number.isSafeInteger(segment.fileOffset) ||
        !Number.isSafeInteger(segment.fileSize) ||
        segment.fileOffset + segment.fileSize > bytes.length
      ) {
        throw new Error("unsupported binary: invalid Mach-O segment file range");
      }
      if (readMachOName(bytes, cursor + 8) === "__LINKEDIT") {
        if (linkEdit) throw new Error("unsupported binary: duplicate __LINKEDIT segment");
        linkEdit = segment;
      }
      if ((segment.initialProtection & 4) !== 0) {
        for (let section = 0; section < sectionCount; section += 1) {
          const sectionOffset = cursor + 72 + section * 80;
          const offset = bytes.readUInt32LE(sectionOffset + 48);
          const size = Number(bytes.readBigUInt64LE(sectionOffset + 40));
          if (!Number.isSafeInteger(size) || offset + size > bytes.length) {
            throw new Error("unsupported binary: invalid executable section file range");
          }
          executableSections.push({ offset, size });
        }
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
  if (!codeSignature || codeSignature.dataSize === 0 || !linkEdit) {
    throw new Error("unsupported binary: expected one code signature in __LINKEDIT");
  }
  const signatureEnd = codeSignature.dataOffset + codeSignature.dataSize;
  if (
    signatureEnd > bytes.length ||
    codeSignature.dataOffset < linkEdit.fileOffset ||
    signatureEnd > linkEdit.fileOffset + linkEdit.fileSize
  ) {
    throw new Error("unsupported binary: invalid LC_CODE_SIGNATURE file range");
  }
  for (const section of executableSections) {
    if (
      codeSignature.dataOffset < section.offset + section.size &&
      signatureEnd > section.offset
    ) {
      throw new Error("unsupported binary: code signature overlaps executable section data");
    }
  }
  return createHash("sha256")
    .update(bytes.subarray(0, codeSignature.dataOffset))
    .update(Buffer.alloc(codeSignature.dataSize))
    .update(bytes.subarray(signatureEnd))
    .digest("hex");
}

function bytesAt(bytes, site) {
  const length = Math.max(site.expectedOriginal.length, site.patched.length);
  if (site.offset + length > bytes.length) return null;
  return bytes.subarray(site.offset, site.offset + length);
}

export function inspectFP64CodecPatch(bytes) {
  const sites = FP64_CODEC_PATCH_SITES.map(site => {
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
  return {
    sha256: sha256(bytes),
    mode:
      states.size === 1 && states.has("original")
        ? "original"
        : states.size === 1 && states.has("patched")
          ? "patched"
          : "unknown-or-partial",
    sites,
  };
}

export function applyFP64CodecPatch(bytes) {
  const inspection = inspectFP64CodecPatch(bytes);
  if (inspection.mode !== "original") {
    throw new Error(
      `refusing patch: expected all original sites, got ${inspection.mode}: ` +
        inspection.sites.map(site => `${site.name}=${site.value}`).join(", ")
    );
  }
  const output = Buffer.from(bytes);
  for (const site of FP64_CODEC_PATCH_SITES) {
    site.patched.copy(output, site.offset);
  }
  return output;
}

async function patchFile(inputPath, outputPath) {
  const input = await readFile(inputPath);
  const inputHash = sha256(input);
  if (inputHash !== METAL_IR_CONVERTER_4_0_BETA_2_SHA256) {
    throw new Error(
      `refusing patch: expected SHA-256 ${METAL_IR_CONVERTER_4_0_BETA_2_SHA256}, got ${inputHash}`
    );
  }

  const output = applyFP64CodecPatch(input);
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
  return inspectFP64CodecPatch(output);
}

async function main(argv) {
  const [command, ...args] = argv;
  if (command === "inspect" && args.length === 1) {
    console.log(JSON.stringify(inspectFP64CodecPatch(await readFile(args[0])), null, 2));
    return;
  }
  if (command === "patch" && args.length === 2) {
    console.log(JSON.stringify(await patchFile(args[0], args[1]), null, 2));
    return;
  }
  if (command === "signature-normalized-sha256" && args.length === 1) {
    console.log(signatureNormalizedSha256(await readFile(args[0])));
    return;
  }
  throw new Error(
    "Usage: metalir-fp64-codec-patch.mjs inspect <binary> | patch <pristine-binary> <output> | signature-normalized-sha256 <binary>"
  );
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2)).catch(error => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
