#!/usr/bin/env node

import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");

export function buildFsrKernels(outputDirectory) {
  const run = (args) => {
    const result = spawnSync("xcrun", ["--sdk", "macosx", ...args], { encoding: "utf8" });
    if (result.error) throw result.error;
    if (result.status !== 0) throw new Error(result.stderr || `xcrun ${args.join(" ")} failed`);
    return result.stdout.trim();
  };
  const compiler = run(["--find", "metal"]);
  const version = run(["metal", "--version"]);
  const sdkPath = run(["--show-sdk-path"]);
  const temporary = mkdtempSync(join(tmpdir(), "yaagl-fsr-metallib-"));
  try {
    // MSL 3.1 is available on macOS 14 and remains usable by Metal 4 pipelines.
    const compileArgs = ["-std=metal3.1", "-mmacosx-version-min=14.0", "-isysroot", sdkPath,
      "-c", resolve(root, "d3dmetal-sidecar/fsr-kernels.metal"),
      "-o", join(temporary, "fsr-kernels.air")];
    const linkArgs = [join(temporary, "fsr-kernels.air"), "-o", join(temporary, "fsr-kernels.metallib")];
    run(["metal", ...compileArgs]);
    run(["metallib", ...linkArgs]);
    const library = readFileSync(join(temporary, "fsr-kernels.metallib"));
    const rows = [];
    for (let offset = 0; offset < library.length; offset += 16) {
      rows.push(`  ${Array.from(library.subarray(offset, offset + 16),
        byte => `0x${byte.toString(16).padStart(2, "0")}`).join(", ")},`);
    }
    writeFileSync(resolve(outputDirectory, "fsr-kernels.inc"),
      `#pragma once\ninline constexpr unsigned char kFsrKernelsMetallib[] = {\n${rows.join("\n")}\n};\n`);
    return { compiler: { path: compiler, version, sdkPath }, deploymentTarget: "14.0",
      languageVersion: "metal3.1", compileArgs, linkArgs,
      sha256: createHash("sha256").update(library).digest("hex"), size: library.length };
  } finally {
    rmSync(temporary, { recursive: true, force: true });
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  if (process.argv.length !== 3) throw new Error("usage: node scripts/build-fsr-kernels.mjs <output-dir>");
  console.log(JSON.stringify(buildFsrKernels(resolve(process.argv[2])), null, 2));
}
