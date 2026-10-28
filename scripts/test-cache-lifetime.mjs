import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import { applyD3DMetalSidecarPatch, inspectD3DMetalSidecarPatch } from "./d3dmetal-sidecar-patch.mjs";

const root = fileURLToPath(new URL("..", import.meta.url));
const layout = JSON.parse(readFileSync(join(root, "d3dmetal-sidecar/layout.json"), "utf8"));
const pristine = readFileSync(process.env.YAAGL_PRISTINE_D3DMETAL ?? join(root,
  "build/wine-11.17-gptk4b3-rx4060-clean/lib/external/D3DMetal.framework/Versions/A/D3DMetal"));
const patched = applyD3DMetalSidecarPatch(pristine);

function exercise(image) {
  const output = mkdtempSync(join(tmpdir(), "yaagl-cache-lifetime-"));
  try {
    const openIndex = layout.hooks.findIndex(hook => hook.id === "CacheFileOpen");
    const closeIndex = layout.hooks.findIndex(hook => hook.id === "CacheFileClose");
    assert.ok(openIndex >= 0 && closeIndex >= 0);
    const open = layout.hooks[openIndex];
    const close = layout.hooks[closeIndex];
    const view = layout.verificationSpans.find(span => span.id === "CacheFileViewBody");
    assert.ok(view);
    // Rebind only the native cache bodies' real imported C functions. Their
    // complete instructions, gates and original trampolines are unmodified.
    const imports = [
      ["Fopen", 0x3917f0], ["Fseek", 0x391802], ["Ftell", 0x391808],
      ["Fileno", 0x3917e4], ["Ftruncate", 0x39180e], ["Mmap", 0x39185c],
      ["Munmap", 0x391862], ["Fclose", 0x3917d2],
    ];
    for (const [, stub] of imports) assert.equal(pristine.readUInt16LE(stub), 0x25ff);
    const bridge = readFileSync(join(root, "d3dmetal-sidecar/bridge.mm"), "utf8");
    const imageGate = bridge.match(/^bool matchesImage\([\s\S]*?^\}/m)?.[0];
    assert.ok(imageGate, "Cannot extract production constructor image gate");
    writeFileSync(join(output, "cache-lifetime-image-gate.inc"),
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
    writeFileSync(join(output, "cache-lifetime-layout.hpp"), [
      "#pragma once",
      `constexpr std::size_t kImageSize = ${layout.dispatch.segmentEndVMAddr};`,
      `constexpr std::size_t kTextSize = ${layout.textCave.endOffset};`,
      `constexpr std::size_t kDispatchSlot = ${layout.dispatch.dataSlotVMAddr};`,
      `constexpr std::size_t kHookCount = ${layout.hooks.length};`,
      `constexpr std::size_t kOpenIndex = ${openIndex};`,
      `constexpr std::size_t kCloseIndex = ${closeIndex};`,
      `constexpr std::size_t kOpen = ${open.entryOffset};`,
      `constexpr std::size_t kClose = ${close.entryOffset};`,
      `constexpr std::size_t kView = ${view.offset};`,
      `constexpr std::size_t kOpenOriginal = ${open.trampolineOffset};`,
      `constexpr std::size_t kCloseOriginal = ${close.trampolineOffset};`,
      ...imports.map(([name, stub]) =>
        `constexpr std::size_t k${name}Slot = ${stub + 6 + pristine.readInt32LE(stub + 2)};`),
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
      ...spans.map((span, index) => `constexpr std::uint8_t kSpan${index}[] = {${bytes(span.hex)}};`),
      "struct VerificationSpan { std::uintptr_t offset; const std::uint8_t* bytes; std::size_t size; };",
      `constexpr std::array<VerificationSpan, ${spans.length}> kVerificationSpans = {{`,
      ...spans.map((span, index) => `  {${span.offset}, kSpan${index}, sizeof(kSpan${index})},`),
      "}};",
      "}",
      "",
    ].join("\n"));
    const fixture = join(output, "patched-cache-image.bin");
    writeFileSync(fixture, patched.subarray(0, layout.textCave.endOffset));
    const executable = join(output, "cache-lifetime");
    const build = spawnSync("xcrun", ["clang++", "-arch", "x86_64", "-std=c++20",
      "-Wall", "-Wextra", "-Werror", "-Wl,-adhoc_codesign",
      "-I", output, "-I", join(root, "d3dmetal-sidecar"),
      join(root, "scripts/test-cache-lifetime.cpp"),
      join(root, "d3dmetal-sidecar/cache-lifetime.mm"), "-o", executable],
    { encoding: "utf8", timeout: 120000 });
    assert.equal(build.status, 0, build.stdout + build.stderr || build.error?.message);
    let args = [fixture, join(output, "cache")];
    let command = executable;
    let options = { encoding: "utf8", timeout: 30000 };
    if (image) {
      const loadImage = realpathSync(image);
      const ownedRun = process.env.RUN_TMP ? realpathSync(process.env.RUN_TMP) : null;
      assert.ok(loadImage.startsWith(realpathSync(root) + "/") ||
        (ownedRun && ownedRun !== "/" && loadImage.startsWith(ownedRun + "/")),
      "native cache execution requires a worktree or RUN_TMP copy, never an installed runtime");
      assert.equal(inspectD3DMetalSidecarPatch(readFileSync(loadImage)).mode, "patched");
      const profile = `(version 1) (allow default) (deny file-write*) ` +
        `(allow file-write* (subpath ${JSON.stringify(realpathSync(output))}) (literal "/dev/null"))`;
      command = "/usr/bin/sandbox-exec";
      args = ["-p", profile, executable, "--image", loadImage, join(output, "cache")];
      options = { ...options, cwd: output, env: { ...process.env,
        HOME: output, CFFIXED_USER_HOME: output, RUN_TMP: output, TMPDIR: output,
        NODE_COMPILE_CACHE: join(output, "node-cache"),
        BUN_RUNTIME_TRANSPILER_CACHE_PATH: join(output, "bun-cache"),
        MTL_SHADER_CACHE_DIR: join(output, "metal-cache") } };
    }
    const run = spawnSync(command, args, options);
    assert.equal(run.status, 0, run.stdout + run.stderr || run.error?.message);
    console.log(run.stdout.trim());
  } finally {
    rmSync(output, { recursive: true, force: true });
  }
}

test("actual beta3 cache bodies and dispatch gates repair payload and failed-Open ownership", () => {
  exercise();
});

test("final native sidecar cache gates clean owned files",
  { skip: process.env.YAAGL_NATIVE_CACHE_D3DMETAL ? false :
    "set YAAGL_NATIVE_CACHE_D3DMETAL to a final signed worktree artifact" }, () => {
    exercise(process.env.YAAGL_NATIVE_CACHE_D3DMETAL);
  });
