import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { D3DMETAL_PSO_CACHE_PATCH_SITES } from "./d3dmetal-pso-cache-patch.mjs";

const loserJump = 0x871f3;
const helper = 0x4adc40;
const cleanup = 0x86f5e;
const unlock = 0x370876;
const free = 0x37079e;
const names = ["rax", "rcx", "rdx", "rbx", "rsp", "rbp", "rsi", "rdi",
  "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15"];

// Audited pristine native spans: canonical selection/unlock, key cleanup,
// failure cleanup and return. The jump and helper always come from production.
const native = new Map([
  [0x871e5, Buffer.from("498b6d20488b7c2418e883962e00", "hex")],
  [cleanup, Buffer.from("4885db0f846c0200004889dfe82f982e00e95f020000", "hex")],
  [0x86f54, Buffer.from("4c89efe842982e0031ed", "hex")],
  [0x86ec5, Buffer.from("488b68204c89e7e8a5992e004885ed740de983000000", "hex")],
  [0x8717a, Buffer.from("488b6d20488b7c2418e8ee962e00498b3e4885ff7443", "hex")],
  [0x871d3, Buffer.from("4889e84883c4685b415c415d415e415f5dc3", "hex")],
]);

function execute(route, original, keyPresent = true) {
  const spans = new Map(native);
  for (const offset of [loserJump, helper]) {
    const patch = D3DMETAL_PSO_CACHE_PATCH_SITES.find(site => site.offset === offset);
    assert.ok(patch, `production patch at 0x${offset.toString(16)}`);
    spans.set(offset, original ? patch.expectedOriginal : patch.patched);
  }
  const registers = names.map((_, index) => 0x10000 + index * 0x100);
  const stackBase = 0x8000;
  const candidate = 0x2000;
  const canonical = route === "winner" ? candidate : 0x3000;
  const key = 0x4000;
  const lock = 0x5000;
  const node = 0x6000;
  const stage = 0x7000;
  const cache = 0x9000;
  registers[4] = stackBase;
  registers[3] = keyPresent ? key : 0;
  registers[5] = canonical;
  if (route === "winner") registers[5] = node;
  registers[0] = node;
  registers[12] = lock;
  registers[14] = cache;
  registers[13] = route === "failure" ? candidate : node;
  const before = [...registers];
  const memory = new Map([[stackBase + 8, candidate], [stackBase + 24, lock],
    [node + 32, canonical], [candidate + 56, stage], [canonical + 56, stage], [cache, 0]]);
  const saved = [3, 12, 13, 14, 15, 5];
  for (const [index, register] of saved.entries()) {
    memory.set(stackBase + 104 + index * 8, before[register]);
  }
  memory.set(stackBase + 152, 0xdead);
  let locked = route !== "failure";
  let zero = false;
  let pc = route === "loser" ? 0x871e5 : route === "failure" ? 0x86f54
    : route === "winner" ? 0x8717a : 0x86ec5;
  const events = [];
  const freed = new Set();
  const pop = () => {
    const value = memory.get(registers[4]);
    assert.notEqual(value, undefined, "valid return/saved-register stack slot");
    registers[4] += 8;
    return value;
  };
  for (let steps = 0; pc !== 0xdead; steps += 1) {
    assert.ok(steps < 80, "bounded native cleanup path");
    if (pc === unlock || pc === free) {
      assert.equal(registers[4] % 16, 8, "SysV callee stack alignment");
      if (pc === unlock) {
        assert.equal(registers[7], lock);
        assert.equal(locked, true);
        locked = false;
        events.push("unlock");
      } else {
        const pointer = registers[7];
        assert.equal(locked, false, "allocator runs only after native unlock");
        assert.ok(pointer === candidate || pointer === key, "never free canonical table, node or borrowed stage");
        assert.equal(freed.has(pointer), false, "each owned allocation freed once");
        freed.add(pointer);
        events.push(pointer === candidate ? "candidate" : "key");
      }
      // Real calls may destroy every caller-saved register. RBP/RBX and the
      // candidate stack slot must not depend on their convenient preservation.
      for (const register of [0, 1, 2, 6, 7, 8, 9, 10, 11]) registers[register] = 0xbad;
      zero = false;
      pc = pop();
      continue;
    }
    const start = [...spans.keys()].find(offset => pc >= offset && pc < offset + spans.get(offset).length);
    assert.notEqual(start, undefined, `verified instruction at 0x${pc.toString(16)}`);
    const bytes = spans.get(start);
    let cursor = pc - start;
    const begin = cursor;
    let rex = 0;
    if ((bytes[cursor] & 0xf0) === 0x40) rex = bytes[cursor++];
    const opcode = bytes[cursor++];
    if (opcode === 0xe8 || opcode === 0xe9) {
      const next = pc + 5;
      if (opcode === 0xe8) {
        registers[4] -= 8;
        memory.set(registers[4], next);
      }
      pc = next + bytes.readInt32LE(cursor);
      continue;
    }
    if (opcode === 0x0f) {
      assert.equal(bytes[cursor++], 0x84);
      const next = pc + 6;
      pc = zero ? next + bytes.readInt32LE(cursor) : next;
      continue;
    }
    if (opcode === 0x74) {
      const next = pc + 2;
      pc = zero ? next + bytes.readInt8(cursor) : next;
      continue;
    }
    if (opcode === 0x89 || opcode === 0x8b || opcode === 0x85 || opcode === 0x31) {
      const modrm = bytes[cursor++];
      const mode = modrm >>> 6;
      const register = ((modrm >>> 3) & 7) + ((rex & 4) ? 8 : 0);
      const rm = (modrm & 7) + ((rex & 1) ? 8 : 0);
      if (mode === 3) {
        if (opcode === 0x89) registers[rm] = registers[register];
        else if (opcode === 0x85) zero = (registers[rm] & registers[register]) === 0;
        else if (opcode === 0x31) {
          registers[rm] = (registers[rm] ^ registers[register]) >>> 0;
          zero = registers[rm] === 0;
        } else assert.fail("unsupported register move");
      } else {
        assert.equal(opcode, 0x8b);
        assert.ok(mode === 0 || mode === 1);
        if ((modrm & 7) === 4) assert.equal(bytes[cursor++], 0x24, "rsp-only SIB");
        const address = registers[rm] + (mode === 1 ? bytes.readInt8(cursor++) : 0);
        assert.ok(memory.has(address), "candidate comes from real caller frame, not return address");
        registers[register] = memory.get(address);
      }
    } else if (opcode === 0x83) {
      assert.equal(bytes[cursor++], 0xc4);
      registers[4] += bytes.readInt8(cursor++);
    } else if (opcode >= 0x58 && opcode <= 0x5f) {
      registers[opcode - 0x58 + ((rex & 1) ? 8 : 0)] = pop();
    } else if (opcode === 0xc3) {
      pc = pop();
      continue;
    } else assert.fail(`unsupported instruction 0x${opcode.toString(16)}`);
    pc += cursor - begin;
  }
  assert.equal(registers[4], stackBase + 160, "unchanged outer return frame");
  for (const register of saved) assert.equal(registers[register], before[register], names[register]);
  assert.equal(memory.get(candidate + 56), stage, "table cleanup must not destroy borrowed stage");
  assert.equal(memory.get(canonical + 56), stage);
  return { events, freed, result: registers[0], candidate, canonical, key };
}

describe("native outer compute losing table lifetime", () => {
  it("reproduces the original leaked 88-byte candidate, then frees it once after unlock", () => {
    for (const keyPresent of [true, false]) {
      const original = execute("loser", true, keyPresent);
      assert.equal(original.freed.has(original.candidate), false, "original free0");
      const patched = execute("loser", false, keyPresent);
      assert.equal(patched.freed.has(patched.candidate), true, "patched free1");
      assert.deepEqual(patched.events, keyPresent ? ["unlock", "candidate", "key"] : ["unlock", "candidate"]);
      assert.equal(patched.result, patched.canonical);
      assert.equal(original.result, original.canonical);
    }
  });

  it("leaves winner/hit ownership and native failure cleanup unchanged", () => {
    for (const original of [true, false]) {
      for (const route of ["winner", "hit", "failure"]) {
        const state = execute(route, original);
        assert.deepEqual(state.events, route === "failure" ? ["candidate", "key"]
          : route === "winner" ? ["unlock"] : ["unlock", "key"]);
        assert.equal(state.result, route === "failure" ? 0 : state.canonical);
      }
    }
  });
});
