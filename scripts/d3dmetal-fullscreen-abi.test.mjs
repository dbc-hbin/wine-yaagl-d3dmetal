import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { D3DMETAL_PSO_CACHE_PATCH_SITES } from "./d3dmetal-pso-cache-patch.mjs";

const callsite = 0x105568;
const fullscreenEntry = 0x1146fe;
const callPatch = D3DMETAL_PSO_CACHE_PATCH_SITES.find(site => site.offset === callsite);
assert.ok(callPatch);

// Decode the actual fixed-width patch instructions rather than pinning their
// spelling. Stop at the native entry to observe the ABI and its return stack.
function executeFullscreenCall(original, initialRdx) {
  const instructions = new Map(D3DMETAL_PSO_CACHE_PATCH_SITES.map(site =>
    [site.offset, original ? site.expectedOriginal : site.patched]
  ));
  const registers = new Map([
    ["rax", 0x1010101010101010n], ["rbx", 0x2020202020202020n],
    ["rcx", 0x3030303030303030n], ["rdx", initialRdx],
    ["rsi", 1n], ["rdi", 0x1234567890n], ["rbp", 0x4000n],
    ["rsp", 0x3000n], ["r8", 0xabcdef0000n], ["r9", 0x5050505050505050n],
    ["r10", 0x6060606060606060n], ["r11", 0x7070707070707070n],
    ["r12", 0x8080808080808080n], ["r13", 0x9090909090909090n],
    ["r14", 0xa0a0a0a0a0a0a0a0n], ["r15", 0xb0b0b0b0b0b0b0b0n],
  ]);
  const before = new Map(registers);
  const stack = new Map();
  let pc = callsite;
  let steps = 0;
  while (pc !== fullscreenEntry) {
    assert.ok(++steps <= 3, "must reach fullscreen through one call and a tail jump");
    const site = [...instructions.keys()].find(offset =>
      pc >= offset && pc < offset + instructions.get(offset).length
    );
    assert.notEqual(site, undefined, "control flow stays within verified patch spans");
    const bytes = instructions.get(site);
    const cursor = pc - site;
    const opcode = bytes[cursor];
    if (opcode === 0xe8 || opcode === 0xe9) {
      assert.ok(cursor + 5 <= bytes.length, "complete relative branch");
      const next = pc + 5;
      if (opcode === 0xe8) {
        registers.set("rsp", registers.get("rsp") - 8n);
        stack.set(registers.get("rsp"), next);
      }
      pc = next + bytes.readInt32LE(cursor + 1);
    } else if (opcode === 0x31) {
      const modrm = bytes[cursor + 1];
      assert.equal(modrm >>> 6, 3, "xor operands must be registers");
      assert.equal((modrm >>> 3) & 7, 2, "xor source is edx");
      assert.equal(modrm & 7, 2, "xor destination is edx");
      // An x86-64 32-bit register write zero-extends the entire 64-bit RDX.
      registers.set("rdx", 0n);
      pc += 2;
    } else {
      assert.fail(`unsupported fullscreen instruction 0x${opcode.toString(16)}`);
    }
  }
  const targetArguments = new Map(registers);
  const returnAddress = stack.get(registers.get("rsp"));
  registers.set("rsp", registers.get("rsp") + 8n);
  return { before, targetArguments, registers, returnAddress, stack };
}

describe("legacy CreateSwapChain fullscreen ABI", () => {
  it("removes the native AsInterface UUID residue before the target-consuming hook", () => {
    const original = executeFullscreenCall(true, 0x1401n);
    assert.equal(original.targetArguments.get("rdx"), 0x1401n,
      "the original call exposes the invalid IDXGIOutput pointer");
    const patched = executeFullscreenCall(false, 0x1401n);
    assert.equal(patched.targetArguments.get("rdx"), 0n);
    assert.equal(patched.targetArguments.get("rdi"), patched.before.get("rdi"));
    assert.equal(patched.targetArguments.get("rsi"), 1n);
    assert.equal(patched.returnAddress, callsite + 5);
    assert.equal(patched.stack.size, 1, "tail jump cannot add a second return frame");
    for (const [name, value] of patched.before) {
      if (name !== "rdx") assert.equal(patched.registers.get(name), value, name);
    }
  });

  it("clears high RDX bits as well as the low DWORD without changing other arguments", () => {
    for (const residue of [0n, 0xffffffffffffffffn, 0xdeadbeef00001401n]) {
      const patched = executeFullscreenCall(false, residue);
      assert.equal(patched.targetArguments.get("rdx"), 0n);
      assert.equal(patched.targetArguments.get("rsp"), patched.before.get("rsp") - 8n);
      assert.equal(patched.returnAddress, callsite + 5);
      for (const [name, value] of patched.before) {
        if (name !== "rdx") assert.equal(patched.registers.get(name), value, name);
      }
    }
  });
});
