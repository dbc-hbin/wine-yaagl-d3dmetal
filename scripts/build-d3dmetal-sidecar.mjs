#!/usr/bin/env node

import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { D3DMETAL_SIDECAR_PATCHED_PAYLOAD_SHA256 } from "./d3dmetal-sidecar-patch.mjs";
import { buildFsrKernels } from "./build-fsr-kernels.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const output = process.argv[2];
if (!output || process.argv.length !== 3 || output === "--test-controls") {
  throw new Error("usage: node scripts/build-d3dmetal-sidecar.mjs <output-dir>");
}
const outputDirectory = resolve(output);
const sourceDirectory = resolve(root, "d3dmetal-sidecar");
const sourcePaths = [
  "d3dmetal-sidecar/async-pipeline.hpp", "d3dmetal-sidecar/async-pipeline-state.hpp",
  "d3dmetal-sidecar/async-pipeline.mm",
  "d3dmetal-sidecar/low-latency.hpp", "d3dmetal-sidecar/low-latency-state.hpp",
  "d3dmetal-sidecar/low-latency.mm", "include/yaagl_nvapi_low_latency.hpp",
  "d3dmetal-sidecar/metalfx-contract.hpp",
  "d3dmetal-sidecar/metalfx-backend.hpp", "d3dmetal-sidecar/metalfx-backend.mm",
  "d3dmetal-sidecar/d3dmetal-transport.hpp", "d3dmetal-sidecar/d3dmetal-transport.mm",
  "d3dmetal-sidecar/ngx-hooks.hpp", "d3dmetal-sidecar/ngx-hooks.mm",
  "d3dmetal-sidecar/display-routing.hpp", "d3dmetal-sidecar/display-routing.mm",
  "include/yaagl_d3dmetal_display.h",
  "include/yaagl_d3dmetal_abi.hpp", "include/yaagl_d3dmetal_native.hpp",
  "include/yaagl_macho_symbols.hpp",
  "d3dmetal-sidecar/fsr-contract.hpp", "d3dmetal-sidecar/fsr-contract.cpp",
  "d3dmetal-sidecar/fsr-translator.hpp", "d3dmetal-sidecar/fsr-translator.mm",
  "d3dmetal-sidecar/fsr-framegeneration.hpp", "d3dmetal-sidecar/fsr-framegeneration.mm",
  "include/yaagl_fsr_bridge.h", "include/yaagl_fsr_fg_bridge.h", "d3dmetal-sidecar/fsr-kernels.metal",
  "d3dmetal-sidecar/third-party/fidelityfx/Kits/FidelityFX/api/include/ffx_api.h",
  "d3dmetal-sidecar/third-party/fidelityfx/Kits/FidelityFX/api/include/ffx_api_types.h",
  "d3dmetal-sidecar/third-party/fidelityfx/Kits/FidelityFX/api/include/dx12/ffx_api_dx12.h",
  "d3dmetal-sidecar/third-party/fidelityfx/Kits/FidelityFX/upscalers/include/ffx_upscale.h",
  "d3dmetal-sidecar/video-memory.hpp", "d3dmetal-sidecar/video-memory.mm",
  "d3dmetal-sidecar/bridge.mm", "d3dmetal-sidecar/layout.json",
  "scripts/build-d3dmetal-sidecar.mjs", "scripts/d3dmetal-sidecar-patch.mjs",
  "scripts/generate-b3-sidecar-layout.mjs",
  "scripts/build-fsr-kernels.mjs",
];
const sources = sourcePaths.map((path) => ({
  path, sha256: createHash("sha256").update(readFileSync(resolve(root, path))).digest("hex"),
}));
const layout = JSON.parse(readFileSync(resolve(sourceDirectory, "layout.json"), "utf8"));
const hookNames = layout.hooks.map(hook => hook.id);
if (layout.formatVersion !== 32 || new Set(hookNames).size !== hookNames.length ||
    layout.hooks.some((hook, index) => !/^[A-Za-z][A-Za-z0-9]*$/.test(hook.id) ||
        hook.dispatchFieldOffset !== index * layout.dispatch.pointerSize)) {
  throw new Error("unsupported beta3 sidecar dispatch layout");
}
const spans = [
  { offset: layout.dependency.commandOffset, hex: layout.dependency.commandHex },
  { offset: layout.constructorVerification.markerOffset, hex: layout.constructorVerification.markerHex },
  ...layout.verificationSpans.map((span) => ({ offset: span.offset, hex: span.expectedHex })),
  ...layout.hooks.flatMap((hook) => [
    { offset: hook.entryOffset, hex: hook.entryPatchHex },
    { offset: hook.gateOffset, hex: hook.gateHex },
    { offset: hook.trampolineOffset, hex: hook.trampolineHex },
  ]),
  ...layout.binaryPatches.map((patch) => ({ offset: patch.offset, hex: patch.patchedHex })),
];
for (const span of spans) {
  if (!Number.isSafeInteger(span.offset) || span.offset < 0 || !/^(?:[0-9a-f]{2})+$/.test(span.hex)) {
    throw new Error("invalid sidecar verification span");
  }
}
const bytes = (hex) => Array.from(Buffer.from(hex, "hex"), (byte) => `0x${byte.toString(16).padStart(2, "0")}`).join(", ");
const header = [
  "#pragma once", "#include <array>", "#include <cstddef>", "#include <cstdint>",
  "namespace yaagl::pso::layout {",
  `enum class Hook : std::size_t { ${hookNames.join(", ")}, Count };`,
  `inline constexpr std::uint32_t kCommandCount = ${layout.dependency.patchedCommandCount};`,
  `inline constexpr std::uint32_t kCommandsSize = ${layout.dependency.patchedCommandsSize};`,
  `inline constexpr std::uintptr_t kTextSize = ${layout.textCave.endOffset};`,
  `inline constexpr std::uintptr_t kFirstTextOffset = ${layout.dependency.firstTextOffset};`,
  `inline constexpr std::uintptr_t kDataSlot = ${layout.dispatch.dataSlotVMAddr};`,
  `inline constexpr std::uintptr_t kCommonSectionOffset = ${layout.dispatch.commonSectionCommandOffset};`,
  `inline constexpr std::uint64_t kCommonAddress = ${layout.dispatch.commonSectionOldEndVMAddr - layout.dispatch.commonSectionOldSize};`,
  `inline constexpr std::uint64_t kCommonSize = ${layout.dispatch.commonSectionNewSize};`,
  `inline constexpr std::uint8_t kUuid[] = { ${bytes(layout.source.machUuid)} };`,
  `inline constexpr std::array<std::uintptr_t, ${hookNames.length}> kTrampolines = { ${layout.hooks.map((hook) => hook.trampolineOffset).join(", ")} };`,
  ...spans.map((span, index) => `inline constexpr std::uint8_t kSpan${index}[] = { ${bytes(span.hex)} };`),
  "struct VerificationSpan { std::uintptr_t offset; const std::uint8_t* bytes; std::size_t size; };",
  `inline constexpr std::array<VerificationSpan, ${spans.length}> kVerificationSpans = {{`,
  ...spans.map((span, index) => `  { ${span.offset}, kSpan${index}, sizeof(kSpan${index}) },`),
  "}};", "} // namespace yaagl::pso::layout", "",
].join("\n");
mkdirSync(outputDirectory, { recursive: true });
writeFileSync(resolve(outputDirectory, "layout.hpp"), header);
const metalLibrary = buildFsrKernels(outputDirectory);
const compilerLookup = spawnSync("xcrun", ["--find", "clang++"], { encoding: "utf8" });
if (compilerLookup.status !== 0) throw new Error(compilerLookup.stderr || "clang++ unavailable");
const compiler = compilerLookup.stdout.trim();
const compilerVersion = spawnSync(compiler, ["--version"], { encoding: "utf8" });
if (compilerVersion.status !== 0) throw new Error("unable to identify clang++");
const sdkLookup = spawnSync("xcrun", ["--sdk", "macosx", "--show-sdk-path"], { encoding: "utf8" });
if (sdkLookup.status !== 0) throw new Error(sdkLookup.stderr || "macOS SDK unavailable");
const sdkPath = sdkLookup.stdout.trim();
const modulePath = resolve(outputDirectory, "libD3DMetalSidecar.dylib");
const compileArgs = [
  "-arch", "x86_64", "-std=c++20", "-fno-objc-arc", "-fobjc-exceptions", "-fblocks",
  "-isysroot", sdkPath,
  "-mmacosx-version-min=14.0", "-O2", "-Wall", "-Wextra", "-Werror",
  "-dynamiclib", "-pthread", "-framework", "Foundation", "-framework", "Metal", "-framework", "QuartzCore", "-framework", "MetalFX",
  "-I", sourceDirectory, "-I", outputDirectory,
  ...sourcePaths.filter(path => /\.(?:mm|cpp)$/.test(path)).map(path => resolve(root, path)),
  "-Wl,-install_name,@rpath/libD3DMetalSidecar.dylib", "-o", modulePath,
];
const compiled = spawnSync(compiler, compileArgs, { cwd: root, stdio: "inherit" });
if (compiled.error) throw compiled.error;
if (compiled.status !== 0) process.exit(compiled.status ?? 1);

const moduleBytes = readFileSync(modulePath);
if (!moduleBytes.includes(Buffer.from("yaagl_fsr_api"))) {
  throw new Error("native sidecar is missing the FSR API export");
}
if (!moduleBytes.includes(Buffer.from("yaagl_fsr_fg_api"))) {
  throw new Error("native sidecar is missing the FSR frame-generation API export");
}
if (moduleBytes.includes(Buffer.from("YAAGL_METALFX_RENDER_PRESET"))) {
  throw new Error("native sidecar still contains the removed render-size override");
}
const forbiddenMarkers = [
  "YAAGL_NATIVE_PSO_CACHE_PROBE",
  "YAAGL_NATIVE_PSO_CACHE_PROBE_BYPASS",
  "YAAGL_D3DMETAL_CACHE_ROOT",
  "YAAGL_D3DMETAL_CACHE_EXECUTABLE",
  "setFdopendirFailureForTest",
  "lastFdopendirFdForTest",
  "_NSGetArgv",
];
for (const marker of forbiddenMarkers) {
  if (moduleBytes.includes(Buffer.from(marker))) {
    throw new Error(`production native sidecar contains test control: ${marker}`);
  }
}

for (const source of sources) {
  const current = createHash("sha256").update(readFileSync(resolve(root, source.path))).digest("hex");
  if (current !== source.sha256) throw new Error(`native sidecar source changed during compilation: ${source.path}`);
}
const manifest = {
  schemaVersion: 1,
  architecture: "x86_64",
  deploymentTarget: "14.0",
  testControls: false,
  d3dmetal: {
    version: "4.0b3",
    layoutFormat: layout.formatVersion,
    hookCount: layout.hooks.length,
    pristineSha256: layout.source.sha256,
    patchedPayloadSha256: D3DMETAL_SIDECAR_PATCHED_PAYLOAD_SHA256,
  },
  sources,
  compiler: { path: compiler, version: compilerVersion.stdout.trim(), sdkPath },
  compileArgs,
  metalLibrary,
  module: {
    file: "libD3DMetalSidecar.dylib",
    sha256: createHash("sha256").update(readFileSync(modulePath)).digest("hex"),
  },
};
writeFileSync(resolve(outputDirectory, "build-manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`);
console.log(JSON.stringify({ module: modulePath, manifest: resolve(outputDirectory, "build-manifest.json") }, null, 2));
