import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { copyFile, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { test } from "node:test";

import {
  applyD3DMetalSidecarPatch,
  D3DMETAL_SIDECAR_PATCHED_PAYLOAD_SHA256,
  D3DMETAL_SIDECAR_PATCH_SITES,
  inspectD3DMetalSidecarPatch,
} from "./d3dmetal-sidecar-patch.mjs";

const root = resolve(new URL("..", import.meta.url).pathname);
const inputPath = process.env.YAAGL_PRISTINE_D3DMETAL ?? resolve(root,
  "build/wine-11.17-gptk4b3-rx4060-clean/lib/external/D3DMetal.framework/Versions/A/D3DMetal");
const pristine = await readFile(inputPath);
const layout = JSON.parse(await readFile(new URL("../d3dmetal-sidecar/layout.json", import.meta.url), "utf8"));
const sites = D3DMETAL_SIDECAR_PATCH_SITES;

// Independent acceptance inventory: deriving this from layout.hooks would let
// deleting a retained hook or replacing a native compiler change the test oracle.
const retained = [
  ["GetContainingOutput", 0x129760, "415741565657"],
  ["SetFullscreenState", 0x128fe4, "4156565755534881ec30010000"],
  ["QueryVideoMemoryInfo", 0x1678ae, "415741564154"],
  ["ReplayTemporalScale", 0x417e0, "554889e5415741564154"],
];
const asynchronous = [
  ["GraphicsCtor", 0x123ee8, "5541574156"],
  ["GetRenderPipelineState", 0x126576, "5541574156"],
  ["GraphicsDestroy", 0x121502, "415741564154"],
  ["MPLDraw", 0xea208, "4157415656"],
  ["MPLDrawIndexed", 0xea0a8, "4157415656"],
  ["RootCtor", 0x879f6, "5541574156"],
  ["RootDestroy", 0x85486, "415741564154"],
  ["MPLReset", 0xeb8bc, "415741564155"],
  ["MPLBeginQuery", 0xe856a, "415741564155"],
  ["MPLEndQuery", 0xea3f4, "415741564155"],
  ["MPLDestroy", 0xf2d6e, "415741564154"],
  ["Present1", 0x129a78, "415741564155"],
  ["DX11Draw", 0xe3788, "4157415656"],
  ["DX11DrawIndexed", 0xe38b4, "4157415656"],
  ["DX11Begin", 0xe34fa, "4157415656"],
  ["DX11End", 0xe338a, "4157415656"],
  ["DX11Destroy", 0xcd716, "4157415653"],
];
const completion = [
  ["CompleteExecute", 0x16f0d0, "5541574156"],
];
const presentation = [
  ["PresentResidencyAdd", 0x16d242, "ff15e0403700"],
];
const removed = [
  ["Metal4Render", 0x1842b9, "5541574156"],
  ["Render", 0x184353, "5541574156"],
  ["Mesh", 0x1843fc, "5541574156"],
  ["Compute", 0x1844a5, "5541574156"],
  ["CompileCompute", 0x96758, "5541574156"],
  ["DestroyDevice", 0x60f54, "5541574156"],
  ["CreateRTFunction", 0x138d28, "5541574156"],
  ["CreateRTCombined", 0x139418, "5541574156"],
  ["CreateRTIntersection", 0x13c544, "4156534883ec28"],
  ["GetAndRetainLibrary", 0x92d6a, "4157415653"],
  ["ExtractFunctions", 0x1863fe, "5541574156"],
  ["LoadGraphicsFunctions", 0x12507c, "5541574156"],
];

function rejects(bytes) {
  assert.equal(inspectD3DMetalSidecarPatch(bytes).mode, "unknown-or-partial");
  assert.throws(() => applyD3DMetalSidecarPatch(bytes), /refusing patch/);
}

function assertNativeEntries(bytes) {
  for (const [id, offset, hex] of removed) {
    assert.equal(pristine.subarray(offset, offset + hex.length / 2).toString("hex"), hex, id);
    assert.deepEqual(bytes.subarray(offset, offset + 64),
      pristine.subarray(offset, offset + 64), `${id}: native entry was modified`);
    assert.ok(!sites.some(site => site.offset < offset + 64 &&
      site.offset + site.patched.length > offset), `${id}: custom patch overlaps native entry`);
  }
}

function assertRetainedRoutes(patched) {
  for (const [index, hook] of layout.hooks.entries()) {
    assert.equal(patched[hook.entryOffset], hook.callSite ? 0xe8 : 0xe9);
    assert.equal(hook.entryOffset + 5 + patched.readInt32LE(hook.entryOffset + 1), hook.gateOffset);
    assert.equal(hook.gateOffset + 8 + patched.readInt32LE(hook.gateOffset + 3), layout.dispatch.dataSlotVMAddr);
    assert.equal(hook.gateOffset + 17 + patched.readInt32LE(hook.gateOffset + 13), layout.dispatch.dataSlotVMAddr);
    assert.equal(patched.readInt32LE(hook.gateOffset + 20), index * 8);
    assert.equal(hook.gateOffset + 10 + patched.readInt8(hook.gateOffset + 9), hook.trampolineOffset);
    const prologue = Buffer.from(hook.originalHex, "hex");
    if (hook.callSite) {
      assert.equal(hook.id, "PresentResidencyAdd");
      assert.equal(patched[hook.entryOffset + 5], 0x90);
      assert.equal(patched.readUInt16LE(hook.trampolineOffset), 0x25ff);
      assert.equal(hook.trampolineOffset + 6 + patched.readInt32LE(hook.trampolineOffset + 2),
        hook.entryOffset + 6 + pristine.readInt32LE(hook.entryOffset + 2));
      continue;
    }
    assert.deepEqual(patched.subarray(hook.trampolineOffset, hook.trampolineOffset + prologue.length), prologue);
    const jump = hook.trampolineOffset + prologue.length;
    assert.equal(patched[jump], 0xe9);
    assert.equal(jump + 5 + patched.readInt32LE(jump + 1), hook.continuationOffset);
  }
}

await test("format 31 retains feature, async and completion gates with scoped Present residency", () => {
  assert.equal(layout.formatVersion, 31);
  assert.equal(layout.source.sha256, "a64950e8324cec32c9a442daa1fafd03823bd199fdd84fee5c73679b5fe86660");
  assert.equal(layout.source.machUuid, "4418ec47c3f03285a05615d87d785ffe");
  assert.equal(layout.constructorVerification.markerHex,
    Buffer.from("YAAGLB3SIDEcar\x1f", "ascii").toString("hex"));
  assert.equal(layout.dispatch.pointerSize, 8);
  const expected = [...retained, ...asynchronous, ...completion, ...presentation];
  assert.deepEqual(layout.hooks.map(hook => [hook.id, hook.entryOffset, hook.originalHex]), expected);
  assert.deepEqual(layout.hooks.map(hook => hook.dispatchFieldOffset), expected.map((_, index) => index * 8));
  assert.deepEqual(layout.hooks.map(hook => hook.gateOffset), expected.map((_, index) => 0x4e0040 + index * 0x40));
  assert.deepEqual(layout.hooks.filter(hook => hook.callSite).map(hook => hook.id), ["PresentResidencyAdd"]);
  assert.deepEqual(layout.binaryPatches, []);
  assert.equal(sites.length, 5 + expected.length * 3);
});

await test("beta3 canonical gates preserve the input and every unpatched byte", () => {
  assert.equal(inspectD3DMetalSidecarPatch(pristine).mode, "original");
  const before = Buffer.from(pristine);
  const patched = applyD3DMetalSidecarPatch(pristine);
  assert.deepEqual(pristine, before);
  assert.equal(patched.length, pristine.length);
  const inspection = inspectD3DMetalSidecarPatch(patched);
  assert.equal(inspection.mode, "patched");
  assert.equal(inspection.payloadSha256, D3DMETAL_SIDECAR_PATCHED_PAYLOAD_SHA256);
  assert.ok(inspection.sites.every(site => site.state === "patched"));
  assert.deepEqual(applyD3DMetalSidecarPatch(patched), patched);

  let cursor = 0;
  for (const site of [...sites].sort((left, right) => left.offset - right.offset)) {
    assert.deepEqual(patched.subarray(cursor, site.offset), pristine.subarray(cursor, site.offset));
    assert.deepEqual(patched.subarray(site.offset, site.offset + site.patched.length), site.patched);
    cursor = site.offset + site.patched.length;
  }
  assert.deepEqual(patched.subarray(cursor), pristine.subarray(cursor));
});

await test("every generated gate routes to its dispatch slot and original continuation", () => {
  const patched = applyD3DMetalSidecarPatch(pristine);
  assertRetainedRoutes(patched);
  assertNativeEntries(patched);
});

await test("final emitted artifact preserves native cache entries and retained routing",
  { skip: process.env.YAAGL_PATCHED_D3DMETAL ? false : "set YAAGL_PATCHED_D3DMETAL to the final signed artifact" },
  async () => {
    const emitted = await readFile(process.env.YAAGL_PATCHED_D3DMETAL);
    assert.equal(inspectD3DMetalSidecarPatch(emitted).mode, "patched");
    assertNativeEntries(emitted);
    assertRetainedRoutes(emitted);
    // Signing may resize __LINKEDIT and the signature, but not executable code.
    assert.deepEqual(emitted.subarray(layout.dependency.firstTextOffset, layout.textCave.endOffset),
      applyD3DMetalSidecarPatch(pristine).subarray(layout.dependency.firstTextOffset, layout.textCave.endOffset));
  });

await test("every partial or corrupted patch span is rejected", () => {
  const patched = applyD3DMetalSidecarPatch(pristine);
  for (const site of sites) {
    const partial = Buffer.from(pristine);
    site.patched.copy(partial, site.offset);
    rejects(partial);
    const corrupt = Buffer.from(patched);
    corrupt[site.offset] ^= 1;
    rejects(corrupt);
    const missing = Buffer.from(patched);
    site.expectedOriginal.copy(missing, site.offset);
    rejects(missing);
  }
});

await test("legacy marker, cache entry jumps, and old dispatch slots fail closed", () => {
  const patched = applyD3DMetalSidecarPatch(pristine);
  const legacyMarker = Buffer.from(patched);
  Buffer.from("YAAGLB3SIDEcar\x1a", "ascii").copy(legacyMarker, layout.constructorVerification.markerOffset);
  rejects(legacyMarker);
  for (const [index, [id, entry]] of removed.entries()) {
    const reintroduced = Buffer.from(patched);
    reintroduced[entry] = 0xe9;
    reintroduced.writeInt32LE(0x4e0040 + index * 0x40 - entry - 5, entry + 1);
    assert.equal(inspectD3DMetalSidecarPatch(reintroduced).mode, "unknown-or-partial", id);
    rejects(reintroduced);
  }
  for (const [index, hook] of layout.hooks.entries()) {
    const oldSlot = Buffer.from(patched);
    oldSlot.writeInt32LE((index + 13) * 8, hook.gateOffset + 20);
    rejects(oldSlot);
  }
  const staleCave = Buffer.from(patched);
  staleCave[0x4e0040 + 4 * 0x40] = 0xe9;
  rejects(staleCave);
});

await test("the complete former seventeen-hook payload is rejected rather than migrated in place", () => {
  const legacy = applyD3DMetalSidecarPatch(pristine);
  // Reconstruct the former load command too, rather than a hybrid payload
  // with the current sidecar dependency and the old hooks.
  const legacyDependency = Buffer.alloc(80);
  legacyDependency.writeUInt32LE(0x0c, 0);
  legacyDependency.writeUInt32LE(80, 4);
  legacyDependency.writeUInt32LE(24, 8);
  legacyDependency.write("@loader_path/Resources/libYaaglNativePsoCache.dylib", 24, "utf8");
  legacyDependency.copy(legacy, layout.dependency.commandOffset);
  legacy.fill(0, 0x4e0040, 0x4e1000);
  const legacyHooks = [...removed.slice(0, 4), ["GetRender", 0x126576, "5541574156"],
    ...removed.slice(4), ...retained];
  for (const [index, [, entry, originalHex]] of legacyHooks.entries()) {
    const original = Buffer.from(originalHex, "hex");
    const gateOffset = 0x4e0040 + index * 0x40;
    const trampoline = gateOffset + 0x18;
    const gate = Buffer.from("48833d0000000000740e4c8b1d0000000041ffa300000000", "hex");
    gate.writeInt32LE(0x563f18 - gateOffset - 8, 3);
    gate.writeInt32LE(0x563f18 - gateOffset - 17, 13);
    gate.writeInt32LE(index * 8, 20);
    gate.copy(legacy, gateOffset);
    legacy.fill(0, entry, entry + original.length);
    legacy[entry] = 0xe9;
    legacy.writeInt32LE(gateOffset - entry - 5, entry + 1);
    original.copy(legacy, trampoline);
    legacy[trampoline + original.length] = 0xe9;
    legacy.writeInt32LE(entry - trampoline - 5, trampoline + original.length + 1);
  }
  Buffer.from("YAAGLB3SIDEcar\x1a", "ascii").copy(legacy, 0x4e07c0);
  rejects(legacy);
});

await test("layout identity rejects legacy and self-consistent partial recipes before import", async () => {
  const directory = await mkdtemp(resolve(tmpdir(), "yaagl-layout-identity-"));
  try {
    await mkdir(resolve(directory, "scripts"));
    await mkdir(resolve(directory, "d3dmetal-sidecar"));
    const patcher = resolve(directory, "scripts/d3dmetal-sidecar-patch.mjs");
    await copyFile(new URL("./d3dmetal-sidecar-patch.mjs", import.meta.url), patcher);
    const mutations = [
      value => { value.formatVersion = 26; },
      value => { value.hooks.pop(); },
      value => { value.hooks.reverse(); },
      value => { value.patchedPayloadSha256 = "0".repeat(64); },
      value => { value.hooks[0].dispatchFieldOffset = 104; },
    ];
    for (const mutate of mutations) {
      const changed = structuredClone(layout);
      mutate(changed);
      await writeFile(resolve(directory, "d3dmetal-sidecar/layout.json"), `${JSON.stringify(changed, null, 2)}\n`);
      const result = spawnSync(process.execPath,
        ["--input-type=module", "-e", "await import(process.argv[1])", patcher],
        { encoding: "utf8", timeout: 30000 });
      assert.equal(result.status, 1, result.error?.message || result.stderr);
      assert.match(result.stderr, /layout corruption: expected SHA-256/);
    }
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

await test("wrong identity, untouched-payload mutation, and malformed images fail closed", () => {
  const wrongUuid = Buffer.from(pristine);
  for (let cursor = 32; cursor < 32 + wrongUuid.readUInt32LE(20); cursor += wrongUuid.readUInt32LE(cursor + 4)) {
    if (wrongUuid.readUInt32LE(cursor) === 0x1b) {
      wrongUuid[cursor + 8] ^= 1;
      break;
    }
  }
  rejects(wrongUuid);
  const wrongCpu = Buffer.from(pristine);
  wrongCpu.writeUInt32LE(0x0100000c, 4);
  rejects(wrongCpu);
  for (const source of [pristine, applyD3DMetalSidecarPatch(pristine)]) {
    const mutation = Buffer.from(source);
    mutation[0x10000] ^= 1;
    rejects(mutation);
  }
  for (const size of [0, 4, 31, 32, 4096, pristine.length - 1]) rejects(pristine.subarray(0, size));
  const badCommand = Buffer.from(pristine);
  badCommand.writeUInt32LE(0, 36);
  rejects(badCommand);
  const badSignature = Buffer.from(pristine);
  for (let cursor = 32; cursor < 32 + badSignature.readUInt32LE(20); cursor += badSignature.readUInt32LE(cursor + 4)) {
    if (badSignature.readUInt32LE(cursor) === 0x1d) {
      badSignature.writeUInt32LE(0, cursor + 8);
      badSignature.writeUInt32LE(badSignature.length, cursor + 12);
      break;
    }
  }
  rejects(badSignature);
});
