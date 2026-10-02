import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { describe, it } from "node:test";

const layout = JSON.parse(await readFile(
  new URL("../d3dmetal-pso-cache/layout.json", import.meta.url), "utf8"
));

// Independent GPTK 4.0b2 disassembly audit: standalone End, two pipeline
// writers, twelve SerializeKey stages, bytecode, and root signature. Keeping
// this inventory outside layout prevents an omitted inline writer passing.
const writerAddresses = [
  0x83e29, 0x879ad, 0x8844d, 0x88ef5, 0x8deed, 0x8e3bd,
  0x8eae3, 0x8f228, 0x8fad2, 0x90154, 0x907b7, 0x90e1b,
  0x91592, 0x91d6e, 0x93437, 0x9a6d4, 0x9e05b,
];
const wrappedWriters = new Set([0x879ad, 0x88ef5, 0x8deed, 0x93437]);
const patches = layout.binaryPatches.filter(patch =>
  patch.id.startsWith("CacheFileEndPublicationOrder")
);
const hashTarget = 0x83e68;
const registerNames = [
  "rax", "rcx", "rdx", "rbx", "rsp", "rbp", "rsi", "rdi",
  "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15",
];

function prepareRecord(offset, payloadLength) {
  const memory = Buffer.alloc(0x2000 + payloadLength, 0xa7);
  const object = 0x200;
  const mapping = 0x1000;
  const oldEnd = 0x80n;
  const record = mapping + Number(oldEnd);
  // Native Begin at 0x83dde: (payload length + 0x27) & -8.
  const recordLength = (BigInt(payloadLength) + 39n) & ~7n;
  const newEnd = oldEnd + recordLength;
  memory.writeBigUInt64LE(BigInt(mapping), object);
  memory.writeBigUInt64LE(newEnd, mapping + 16);
  memory.writeBigUInt64LE(BigInt(record), object + 32);
  memory.writeBigUInt64LE(recordLength, object + 40);
  memory.writeBigUInt64LE(oldEnd, mapping + 8);
  memory.writeBigUInt64LE(recordLength, record);
  memory.writeBigUInt64LE(BigInt(payloadLength), record + 8);
  memory.writeBigUInt64LE(0n, record + 16);
  for (let index = 0; index < payloadLength; index += 1) {
    memory[record + 24 + index] = (index * 31 + payloadLength) & 0xff;
  }
  const payload = Buffer.from(memory.subarray(record + 24, record + 24 + payloadLength));
  // Opaque deterministic stand-in for XXH3_64bits: these tests establish call ABI,
  // write ordering, and interruption behavior, not the hash implementation.
  const checksum = createHash("sha256").update(payload).digest().readBigUInt64LE();
  const registers = new Map(registerNames.map((name, index) =>
    [name, BigInt(0x400 + index * 0x10)]
  ));
  if (wrappedWriters.has(offset)) {
    registers.set("r14", BigInt(object - 8));
    registers.set("r15", 0n);
  } else {
    registers.set("rbx", BigInt(object));
    if (offset === 0x83e29) registers.set("rdi", BigInt(object));
  }
  return {
    memory, registers, before: Buffer.from(memory), payload, checksum,
    object, mapping, record, recordLength, oldEnd, newEnd,
    footer: record + Number(recordLength) - 8,
  };
}

// Only the audited End spans' mov, lea, neg, and relative call instructions
// are supported. Decode real REX/ModRM/SIB operands; unexpected instructions
// fail instead of silently gaining convenient interpreter behavior.
function executeSpan(patch, field, state, { interruptAt, hashThrows = false } = {}) {
  // Inline relocation ends immediately before the original pending reset.
  // Execute that unchanged, independently audited next instruction as well
  // to check the relocated register values still make its store correct.
  const continuation = patch.offset === 0x83e29 ? ""
    : patch.offset === 0x8deed ? "49c7462800000000"
    : wrappedWriters.has(patch.offset) ? "4d897e28"
    : "48c7432000000000";
  const bytes = Buffer.from(patch[field] + continuation, "hex");
  const { memory, registers } = state;
  const observations = [];
  const calls = [];
  let cursor = 0;
  let steps = 0;
  const readOperand = operand => operand.register
    ? registers.get(operand.register)
    : memory.readBigUInt64LE(operand.address);
  const writeOperand = (operand, value) => {
    value = BigInt.asUintN(64, value);
    if (operand.register) registers.set(operand.register, value);
    else memory.writeBigUInt64LE(value, operand.address);
  };
  const observe = () => observations.push({
    pc: patch.offset + cursor,
    committedEnd: memory.readBigUInt64LE(state.mapping + 8),
    checksum: memory.readBigUInt64LE(state.record + 16),
    footer: memory.readBigUInt64LE(state.footer),
    pending: memory.readBigUInt64LE(state.object + 32),
  });
  observe();
  while (cursor < bytes.length) {
    if (steps === interruptAt) return { observations, calls, interrupted: true };
    const start = cursor;
    const first = bytes[cursor++];
    if (first === 0xe8) {
      const displacement = bytes.readInt32LE(cursor);
      cursor += 4;
      const target = patch.offset + cursor + displacement;
      calls.push(target);
      assert.equal(target, hashTarget, "must preserve the native XXH3_64bits call target");
      assert.equal(registers.get("rdi"), BigInt(state.record + 24), "hash payload pointer");
      assert.equal(registers.get("rsi"), BigInt(state.payload.length), "hash payload length");
      assert.deepEqual(memory.subarray(state.record + 24, state.record + 24 + state.payload.length), state.payload);
      if (hashThrows) throw new Error("checksum interrupted");
      // SysV caller-saved registers are unspecified after the native call.
      // Poison them to expose patches accidentally retaining values across it.
      for (const name of ["rcx", "rdx", "rsi", "rdi", "r8", "r9", "r10", "r11"]) {
        registers.set(name, 0xdedede00n + BigInt(registerNames.indexOf(name)));
      }
      registers.set("rax", state.checksum);
    } else {
      assert.ok(first >= 0x40 && first <= 0x4f, `unsupported prefix at 0x${(patch.offset + start).toString(16)}`);
      const rex = first;
      assert.ok(rex & 8, "only 64-bit End operands are supported");
      const opcode = bytes[cursor++];
      assert.ok([0x8b, 0x89, 0x8d, 0xf7, 0xc7].includes(opcode), "unsupported End instruction");
      const modrm = bytes[cursor++];
      const mode = modrm >>> 6;
      const extension = (modrm >>> 3) & 7;
      const register = registerNames[extension + ((rex & 4) ? 8 : 0)];
      const rm = modrm & 7;
      let operand;
      if (mode === 3) {
        operand = { register: registerNames[rm + ((rex & 1) ? 8 : 0)] };
      } else {
        let address;
        if (rm === 4) {
          const sib = bytes[cursor++];
          const scale = 1n << BigInt(sib >>> 6);
          const index = (sib >>> 3) & 7;
          const base = sib & 7;
          assert.ok(mode !== 0 || base !== 5, "unsupported base-less SIB");
          address = registers.get(registerNames[base + ((rex & 1) ? 8 : 0)]);
          if (index !== 4 || (rex & 2)) {
            address += registers.get(registerNames[index + ((rex & 2) ? 8 : 0)]) * scale;
          }
        } else {
          assert.ok(mode !== 0 || rm !== 5, "unsupported RIP-relative operand");
          address = registers.get(registerNames[rm + ((rex & 1) ? 8 : 0)]);
        }
        if (mode === 1) address += BigInt(bytes.readInt8(cursor++));
        if (mode === 2) {
          address += BigInt(bytes.readInt32LE(cursor));
          cursor += 4;
        }
        operand = { address: Number(address) };
      }
      if (opcode === 0x8b) registers.set(register, readOperand(operand));
      if (opcode === 0x89) writeOperand(operand, registers.get(register));
      if (opcode === 0x8d) {
        assert.ok("address" in operand, "lea must use memory addressing");
        registers.set(register, BigInt(operand.address));
      }
      if (opcode === 0xf7) {
        assert.equal(extension, 3, "only neg is supported");
        writeOperand(operand, -readOperand(operand));
      }
      if (opcode === 0xc7) {
        assert.equal(extension, 0, "only immediate mov is supported");
        const immediate = BigInt(bytes.readInt32LE(cursor));
        cursor += 4;
        writeOperand(operand, immediate);
      }
    }
    assert.ok(cursor <= bytes.length, "instruction must fit the audited patch span");
    steps += 1;
    observe();
  }
  return { observations, calls, interrupted: false };
}

function assertPublicationSafe(state, observations) {
  for (const observation of observations) {
    assert.ok(observation.committedEnd === state.oldEnd || observation.committedEnd === state.newEnd);
    if (observation.committedEnd === state.newEnd) {
      assert.equal(observation.checksum, state.checksum, `checksum before publication at 0x${observation.pc.toString(16)}`);
      assert.equal(observation.footer, BigInt.asUintN(64, -state.recordLength), `footer before publication at 0x${observation.pc.toString(16)}`);
    }
    assert.ok(observation.pending === BigInt(state.record) || observation.pending === 0n);
    if (observation.pending === 0n) {
      assert.equal(observation.committedEnd, state.newEnd, "reset must follow publication");
    }
  }
}

function assertUnrelatedMemoryPreserved(state) {
  const actual = Buffer.from(state.memory);
  for (const address of [state.mapping + 8, state.record + 16, state.footer, state.object + 32]) {
    state.before.copy(actual, address, address, address + 8);
  }
  assert.deepEqual(actual, state.before, "payload, prior records, object members, and surrounding mapping must remain unchanged");
}

describe("D3DMetal cache record publication", () => {
  it("covers every independently audited standalone and inline writer", () => {
    assert.deepEqual(patches.map(patch => patch.offset).sort((a, b) => a - b), writerAddresses);
  });

  for (const patch of patches) {
    describe(`writer 0x${patch.offset.toString(16)}`, () => {
      it("exposes the original publication-before-checksum bug", () => {
        const state = prepareRecord(patch.offset, 17);
        const result = executeSpan(patch, "originalHex", state);
        assert.deepEqual(result.calls, [hashTarget]);
        assert.equal(state.memory.readBigUInt64LE(state.mapping + 8), state.newEnd);
        assert.throws(() => assertPublicationSafe(state, result.observations), /checksum before publication/);
        const failed = prepareRecord(patch.offset, 17);
        assert.throws(() => executeSpan(patch, "originalHex", failed, { hashThrows: true }), /checksum interrupted/);
        assert.equal(failed.memory.readBigUInt64LE(failed.mapping + 8), failed.newEnd);
      });

      it("publishes complete records at empty, word, and page boundaries without corrupting neighbors", () => {
        for (const size of [0, 1, 7, 8, 9, 4095, 4096, 4097]) {
          const state = prepareRecord(patch.offset, size);
          const result = executeSpan(patch, "patchedHex", state);
          assertPublicationSafe(state, result.observations);
          assert.deepEqual(result.calls, [hashTarget]);
          assert.equal(state.memory.readBigUInt64LE(state.mapping + 8), state.newEnd);
          assert.equal(state.memory.readBigUInt64LE(state.object + 32), 0n, "pending record reset");
          assertUnrelatedMemoryPreserved(state);
        }
      });

      it("never exposes incomplete data at any instruction interruption or checksum exception", () => {
        const complete = executeSpan(patch, "patchedHex", prepareRecord(patch.offset, 33));
        for (let boundary = 0; boundary < complete.observations.length; boundary += 1) {
          const state = prepareRecord(patch.offset, 33);
          const result = executeSpan(patch, "patchedHex", state, { interruptAt: boundary });
          assertPublicationSafe(state, result.observations);
          assertUnrelatedMemoryPreserved(state);
        }
        const state = prepareRecord(patch.offset, 33);
        assert.throws(() => executeSpan(patch, "patchedHex", state, { hashThrows: true }), /checksum interrupted/);
        assert.equal(state.memory.readBigUInt64LE(state.mapping + 8), state.oldEnd);
        assert.equal(state.memory.readBigUInt64LE(state.object + 32), BigInt(state.record));
        assert.deepEqual(state.memory, state.before, "failed hash must not publish or mutate a record");
      });
    });
  }
});
