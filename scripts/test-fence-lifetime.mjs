import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import { applyD3DMetalSidecarPatch } from "./d3dmetal-sidecar-patch.mjs";

const root = fileURLToPath(new URL("..", import.meta.url));
const layout = JSON.parse(readFileSync(join(root, "d3dmetal-sidecar/layout.json"), "utf8"));
const pristine = readFileSync(process.env.YAAGL_PRISTINE_D3DMETAL ?? join(root,
  "build/wine-11.17-gptk4b3-rx4060-clean/lib/external/D3DMetal.framework/Versions/A/D3DMetal"));

test("native pending fence blocks own duplicated events and concurrent multi counters", () => {
  const output = mkdtempSync(join(tmpdir(), "yaagl-fence-lifetime-"));
  try {
    const patched = applyD3DMetalSidecarPatch(pristine);
    const single = layout.hooks.findIndex(hook => hook.id === "SingleFenceCompletion");
    const multiple = layout.hooks.findIndex(hook => hook.id === "MultipleFenceCompletion");
    assert.equal(single, 29);
    assert.equal(multiple, 30);
    const imports = [
      ["BlockCopy", 0x39134c], ["BlockRelease", 0x39135e],
      ["BlockAssign", 0x391352], ["BlockDispose", 0x391358],
      ["New", 0x391694], ["Delete", 0x39167c], ["Bzero", 0x3916a0],
      ["Memcpy", 0x391844], ["Lock", 0x3918da], ["Unlock", 0x3918e0],
      ["SemaphoreCreate", 0x391784], ["SemaphoreWait", 0x391790],
      ["SemaphoreSignal", 0x39178a], ["DispatchRelease", 0x391778],
    ];
    for (const [, stub] of imports) assert.equal(pristine.readUInt16LE(stub), 0x25ff);
    const bridge = readFileSync(join(root, "d3dmetal-sidecar/bridge.mm"), "utf8");
    const imageGate = bridge.match(/^bool matchesImage\([\s\S]*?^\}/m)?.[0];
    assert.ok(imageGate);
    writeFileSync(join(output, "fence-lifetime-image-gate.inc"),
      `namespace yaagl::pso {\n${imageGate}\n}\n`);
    const spans = [
      { offset: layout.dependency.commandOffset, hex: layout.dependency.commandHex },
      { offset: layout.constructorVerification.markerOffset, hex: layout.constructorVerification.markerHex },
      ...layout.verificationSpans.map(span => ({ offset: span.offset, hex: span.expectedHex })),
      ...layout.hooks.flatMap(hook => [
        { offset: hook.entryOffset, hex: hook.entryPatchHex },
        { offset: hook.gateOffset, hex: hook.gateHex },
        { offset: hook.trampolineOffset, hex: hook.trampolineHex },
      ]),
    ];
    const bytes = hex => [...Buffer.from(hex, "hex")].join(", ");
    writeFileSync(join(output, "layout.hpp"), [
      "#pragma once", "#include <array>", "#include <cstddef>", "#include <cstdint>",
      "namespace yaagl::pso::layout {",
      `constexpr std::uint32_t kCommandCount = ${layout.dependency.patchedCommandCount};`,
      `constexpr std::uint32_t kCommandsSize = ${layout.dependency.patchedCommandsSize};`,
      `constexpr std::uintptr_t kTextSize = ${layout.textCave.endOffset};`,
      `constexpr std::uintptr_t kFirstTextOffset = ${layout.dependency.firstTextOffset};`,
      `constexpr std::uintptr_t kDataSlot = ${layout.dispatch.dataSlotVMAddr};`,
      `constexpr std::uintptr_t kCommonSectionOffset = ${layout.dispatch.commonSectionCommandOffset};`,
      `constexpr std::uint64_t kCommonAddress = ${layout.dispatch.commonSectionOldEndVMAddr - layout.dispatch.commonSectionOldSize};`,
      `constexpr std::uint64_t kCommonSize = ${layout.dispatch.commonSectionNewSize};`,
      `constexpr std::uint8_t kUuid[] = {${bytes(layout.source.machUuid)}};`,
      "namespace fence {",
      ...Object.entries(layout.fenceAbi).map(([name, value]) =>
        `constexpr std::uintptr_t k${name} = ${value};`),
      "}",
      ...spans.map((span, index) => `constexpr std::uint8_t kSpan${index}[] = {${bytes(span.hex)}};`),
      "struct VerificationSpan { std::uintptr_t offset; const std::uint8_t* bytes; std::size_t size; };",
      `constexpr std::array<VerificationSpan, ${spans.length}> kVerificationSpans = {{`,
      ...spans.map((span, index) => `  {${span.offset}, kSpan${index}, sizeof(kSpan${index})},`),
      "}};", "}",
      `constexpr std::size_t kImageSize = ${layout.dispatch.segmentEndVMAddr};`,
      `constexpr std::size_t kHookCount = ${layout.hooks.length};`,
      `constexpr std::size_t kSingleIndex = ${single};`,
      `constexpr std::size_t kMultipleIndex = ${multiple};`,
      `constexpr std::size_t kSingleOriginal = ${layout.hooks[single].trampolineOffset};`,
      `constexpr std::size_t kMultipleOriginal = ${layout.hooks[multiple].trampolineOffset};`,
      ...layout.hooks.slice(31).map(hook =>
        `constexpr std::size_t k${hook.id}Original = ${hook.trampolineOffset};`),
      ...[0x4eea98, 0x4eeaa0].map((offset, index) =>
        `constexpr std::size_t kWait${index ? "Dispose" : "Copy"} = ${
          Number(pristine.readBigUInt64LE(offset) & ((1n << 36n) - 1n))};`),
      ...imports.map(([name, stub]) =>
        `constexpr std::size_t k${name}Slot = ${stub + 6 + pristine.readInt32LE(stub + 2)};`),
      "",
    ].join("\n"));
    const fixture = join(output, "fence-image.bin");
    const mapped = Buffer.alloc(layout.dispatch.segmentEndVMAddr);
    for (let cursor = 32; cursor < 32 + patched.readUInt32LE(20);
      cursor += patched.readUInt32LE(cursor + 4)) {
      if (patched.readUInt32LE(cursor) !== 0x19) continue;
      const address = Number(patched.readBigUInt64LE(cursor + 24));
      const offset = Number(patched.readBigUInt64LE(cursor + 40));
      const size = Number(patched.readBigUInt64LE(cursor + 48));
      if (address + size <= mapped.length) patched.copy(mapped, address, offset, offset + size);
    }
    writeFileSync(fixture, mapped);
    const executable = join(output, "fence-lifetime");
    const build = spawnSync("xcrun", ["clang++", "-arch", "x86_64", "-std=c++20",
      "-fno-objc-arc", "-fobjc-exceptions", "-fblocks",
      "-Wall", "-Wextra", "-Werror", "-pthread", "-framework", "Foundation",
      "-Wl,-adhoc_codesign", "-I", output, "-I", join(root, "d3dmetal-sidecar"),
      join(root, "scripts/test-fence-lifetime.mm"),
      join(root, "d3dmetal-sidecar/fence-lifetime.mm"), "-o", executable],
    { encoding: "utf8", timeout: 120000 });
    assert.equal(build.status, 0, build.stdout + build.stderr || build.error?.message);
    const run = spawnSync(executable, [fixture],
      { encoding: "utf8", timeout: 30000 });
    assert.equal(run.status, 0, run.stdout + run.stderr || run.error?.message);
    console.log(run.stdout.trim());
  } finally {
    rmSync(output, { recursive: true, force: true });
  }
});
