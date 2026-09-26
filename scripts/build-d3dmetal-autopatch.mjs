#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  chmod, copyFile, lstat, mkdir, mkdtemp, readFile, rename, rm, writeFile,
} from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { FP64_CODEC_PATCH_SITES } from "./metalir-fp64-codec-patch.mjs";
import { D3DMETAL_STAGE_LOCK_PATCH_SITES } from "./d3dmetal-stage-lock-patch.mjs";
import { D3DMETAL_PSO_CACHE_PATCH_SITES } from "./d3dmetal-pso-cache-patch.mjs";
import { D3DMETAL_RELEASE, D3DMETAL_RUNTIME_HASHES } from "./d3dmetal-runtime-spec.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const sourcePaths = [
  "scripts/build-d3dmetal-autopatch.mjs",
  "scripts/d3dmetal-runtime-spec.mjs",
  "scripts/prepare-d3dmetal-runtime.swift",
  "scripts/metalir-fp64-codec-patch.mjs",
  "scripts/d3dmetal-stage-lock-patch.mjs",
  "scripts/d3dmetal-pso-cache-patch.mjs",
  "scripts/build-d3dmetal-pso-cache.mjs",
  "d3dmetal-pso-cache/layout.json",
];
const helperName = "prepare-d3dmetal-runtime";
const sidecarName = "libYaaglNativePsoCache.dylib";

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

async function sha256File(path) {
  return sha256(await readFile(path));
}

function run(command, args, capture = false) {
  const result = spawnSync(command, args, {
    cwd: root,
    env: { ...process.env, COPYFILE_DISABLE: "1" },
    encoding: "utf8",
    stdio: capture ? "pipe" : "inherit",
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(`${command} exited ${result.status}: ${[result.stdout, result.stderr].filter(Boolean).join("").trim()}`);
  }
  return capture ? `${result.stdout ?? ""}${result.stderr ?? ""}`.trim() : "";
}

function parseArgs(argv) {
  if (argv.length !== 1 && argv.length !== 3) {
    throw new Error("usage: node scripts/build-d3dmetal-autopatch.mjs OUTPUT_DIR [--pso-module PATH]");
  }
  if (argv.length === 3 && argv[1] !== "--pso-module") {
    throw new Error("expected --pso-module PATH after OUTPUT_DIR");
  }
  return { output: resolve(argv[0]), psoModule: argv[2] ? resolve(argv[2]) : null };
}

async function assertAbsent(path) {
  try {
    await lstat(path);
  } catch (error) {
    if (error.code === "ENOENT") return;
    throw error;
  }
  throw new Error(`refusing to replace existing output: ${path}`);
}

async function assertRegularFile(path) {
  const entry = await lstat(path);
  if (!entry.isFile() || entry.isSymbolicLink()) {
    throw new Error(`expected a regular file: ${path}`);
  }
}

// Preserve the site ordering from the existing patchers; the native installer
// applies each fixed-width replacement only after checking the full input hash.
function patchRecipe(sites) {
  return sites.map(site => {
    if (!Number.isSafeInteger(site.offset) || site.offset < 0 ||
        site.expectedOriginal.length === 0 ||
        site.expectedOriginal.length !== site.patched.length) {
      throw new Error(`invalid fixed-width patch site: ${site.name}`);
    }
    return { offset: site.offset, bytes: site.patched.toString("base64") };
  });
}

export function createAutopatchRecipe() {
  return {
    schema: 1,
    release: D3DMETAL_RELEASE,
    hashes: D3DMETAL_RUNTIME_HASHES,
    patches: {
      fp64: patchRecipe(FP64_CODEC_PATCH_SITES),
      stageLock: patchRecipe(D3DMETAL_STAGE_LOCK_PATCH_SITES),
      nativePso: patchRecipe(D3DMETAL_PSO_CACHE_PATCH_SITES),
    },
  };
}

async function build(options) {
  if (process.platform !== "darwin" || process.arch !== "arm64") {
    throw new Error("the bundle must be built on arm64 macOS");
  }
  const tarball = `${options.output}.tar.gz`;
  await assertAbsent(options.output);
  await assertAbsent(tarball);
  if (options.psoModule) await assertRegularFile(options.psoModule);
  const sidecarInputHash = options.psoModule ? await sha256File(options.psoModule) : null;
  if (sidecarInputHash &&
      sidecarInputHash !== D3DMETAL_RUNTIME_HASHES.rawSidecar &&
      sidecarInputHash !== D3DMETAL_RUNTIME_HASHES.signedSidecar) {
    throw new Error(`unexpected native PSO module hash: ${sidecarInputHash}`);
  }
  const sources = await Promise.all(sourcePaths.map(async path => ({
    path,
    sha256: await sha256File(resolve(root, path)),
  })));

  await mkdir(dirname(options.output), { recursive: true });
  const temporary = await mkdtemp(join(dirname(options.output), ".d3dmetal-autopatch-build-"));
  const bundle = join(temporary, "bundle");
  const nativeBuild = join(temporary, "native-build");
  const recipeSource = join(temporary, "AutopatchRecipe.swift");
  const temporaryTarball = join(temporary, "bundle.tar.gz");
  let publishedTarball = false;
  try {
    await mkdir(bundle);
    let sourceSidecar = options.psoModule;
    if (!sourceSidecar) {
      run(process.execPath, [resolve(root, "scripts/build-d3dmetal-pso-cache.mjs"), nativeBuild]);
      sourceSidecar = join(nativeBuild, sidecarName);
      await assertRegularFile(sourceSidecar);
      if (await sha256File(sourceSidecar) !== D3DMETAL_RUNTIME_HASHES.rawSidecar) {
        throw new Error("native PSO build differs from the pinned raw sidecar");
      }
    }
    const actualInputHash = await sha256File(sourceSidecar);
    if (actualInputHash !== D3DMETAL_RUNTIME_HASHES.rawSidecar &&
        actualInputHash !== D3DMETAL_RUNTIME_HASHES.signedSidecar) {
      throw new Error(`native PSO module changed or differs from pins: ${actualInputHash}`);
    }
    const sidecar = join(bundle, sidecarName);
    await copyFile(sourceSidecar, sidecar);
    await chmod(sidecar, 0o755);
    if (actualInputHash === D3DMETAL_RUNTIME_HASHES.rawSidecar) {
      run("/usr/bin/codesign", ["--force", "--sign", "-", sidecar]);
    }
    run("/usr/bin/codesign", ["--verify", "--strict", sidecar]);
    const signedSidecarHash = await sha256File(sidecar);
    if (signedSidecarHash !== D3DMETAL_RUNTIME_HASHES.signedSidecar) {
      throw new Error(`signed native PSO module differs from pin: ${signedSidecarHash}`);
    }

    const recipe = JSON.stringify(createAutopatchRecipe());
    const recipeBase64 = Buffer.from(recipe, "utf8").toString("base64");
    await writeFile(recipeSource, `enum AutopatchRecipe {\n    static let base64 = "${recipeBase64}"\n}\n`, { flag: "wx" });
    const swiftVersion = run("/usr/bin/xcrun", ["swiftc", "--version"], true);
    const helper = join(bundle, helperName);
    run("/usr/bin/xcrun", [
      "swiftc", "-O", "-target", "arm64-apple-macos26.0",
      resolve(root, "scripts/prepare-d3dmetal-runtime.swift"), recipeSource,
      "-o", helper,
    ]);
    run("/usr/bin/codesign", ["--force", "--sign", "-", helper]);
    run("/usr/bin/codesign", ["--verify", "--strict", helper]);

    const manifest = {
      schema: 1,
      target: "arm64-apple-macos26.0",
      sources,
      compiler: { command: "/usr/bin/xcrun swiftc", version: swiftVersion },
      recipe: { schema: 1, sha256: sha256(Buffer.from(recipe)), generatedSourceSha256: await sha256File(recipeSource) },
      sidecarInput: { sha256: actualInputHash, mode: options.psoModule ? "provided" : "built" },
      artifacts: {
        helper: { file: helperName, sha256: await sha256File(helper) },
        sidecar: { file: sidecarName, sha256: signedSidecarHash },
      },
    };
    await writeFile(join(bundle, "build-manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`, { flag: "wx" });
    run("/usr/bin/tar", ["-czf", temporaryTarball, "-C", bundle,
      helperName, sidecarName, "build-manifest.json"]);
    const currentSources = await Promise.all(sourcePaths.map(async path => ({
      path,
      sha256: await sha256File(resolve(root, path)),
    })));
    for (let index = 0; index < sources.length; index += 1) {
      if (sources[index].sha256 !== currentSources[index].sha256) {
        throw new Error(`source changed during build: ${sources[index].path}`);
      }
    }
    if (options.psoModule && await sha256File(options.psoModule) !== sidecarInputHash) {
      throw new Error("native PSO module changed during build");
    }
    await assertAbsent(options.output);
    await assertAbsent(tarball);
    await rename(temporaryTarball, tarball);
    publishedTarball = true;
    await rename(bundle, options.output);
    publishedTarball = false;
    console.log(JSON.stringify({ output: options.output, tarball, manifest }, null, 2));
  } finally {
    if (publishedTarball) await rm(tarball, { force: true });
    await rm(temporary, { recursive: true, force: true });
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  build(parseArgs(process.argv.slice(2))).catch(error => {
    console.error(`build-d3dmetal-autopatch: ${error.message}`);
    process.exitCode = 1;
  });
}
