import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import { applyD3DMetalSidecarPatch } from "./d3dmetal-sidecar-patch.mjs";

const root = fileURLToPath(new URL("..", import.meta.url));
test("production Present registration bounds counted residency across layer and queue lifetimes", () => {
  const bridge = readFileSync(join(root, "d3dmetal-sidecar/bridge.mm"), "utf8");
  const production = bridge.match(/^char presentResidencyKey;[\s\S]*?^void addPresentResidency\([\s\S]*?^\}/m)?.[0];
  assert.ok(production, "Cannot extract production Present registration");
  const layout = JSON.parse(readFileSync(join(root, "d3dmetal-sidecar/layout.json"), "utf8"));
  const index = layout.hooks.findIndex(hook => hook.id === "PresentResidencyAdd");
  assert.ok(index >= 0);
  const hook = layout.hooks[index];
  const pristine = readFileSync(process.env.YAAGL_PRISTINE_D3DMETAL ?? join(root,
    "build/wine-11.17-gptk4b3-rx4060-clean/lib/external/D3DMetal.framework/Versions/A/D3DMetal"));
  const patched = applyD3DMetalSidecarPatch(pristine);
  const bytes = (offset, count) => [...patched.subarray(offset, offset + count)].join(", ");
  const output = mkdtempSync(join(tmpdir(), "yaagl-present-residency-"));
  try {
    writeFileSync(join(output, "present-residency-production.inc"),
      `namespace yaagl::pso {\n${production}\n}\n`);
    writeFileSync(join(output, "present-residency-layout.hpp"), [
      "#pragma once",
      `constexpr std::size_t kImageSize = ${layout.dispatch.segmentEndVMAddr};`,
      `constexpr std::size_t kDispatchSlot = ${layout.dispatch.dataSlotVMAddr};`,
      `constexpr std::size_t kCallSite = ${hook.entryOffset};`,
      `constexpr std::size_t kGate = ${hook.gateOffset};`,
      `constexpr std::size_t kTrampoline = ${hook.trampolineOffset};`,
      `constexpr std::size_t kMessageSlot = ${hook.entryOffset + 6 + pristine.readInt32LE(hook.entryOffset + 2)};`,
      `constexpr std::size_t kHookCount = ${layout.hooks.length};`,
      `constexpr std::size_t kPresentHook = ${index};`,
      `constexpr unsigned char kCallBytes[] = {${bytes(hook.entryOffset, hook.originalHex.length / 2)}};`,
      `constexpr unsigned char kGateBytes[] = {${bytes(hook.gateOffset, hook.gateHex.length / 2)}};`,
      `constexpr unsigned char kTrampolineBytes[] = {${bytes(hook.trampolineOffset, hook.trampolineHex.length / 2)}};`,
      "",
    ].join("\n"));
    const executable = join(output, "present-residency");
    const build = spawnSync("xcrun", ["clang++", "-arch", "x86_64", "-std=c++20",
      "-fno-objc-arc", "-fobjc-exceptions", "-Wall", "-Wextra", "-Werror",
      "-framework", "Foundation", "-I", output,
      join(root, "scripts/native-present-residency-test.mm"), "-o", executable],
      { encoding: "utf8", timeout: 120000 });
    assert.equal(build.status, 0, build.stderr || build.error?.message);
    const run = spawnSync(executable, [], { encoding: "utf8", timeout: 30000 });
    assert.equal(run.status, 0, run.stdout + run.stderr || run.error?.message);
    console.log(run.stdout.trim());
  } finally { rmSync(output, { recursive: true, force: true }); }
});
