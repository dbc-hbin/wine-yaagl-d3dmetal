#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  chmod, copyFile, lstat, mkdir, mkdtemp, readFile, rename, rm, writeFile,
} from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { D3DMETAL_SIDECAR_PATCH_SITES, applyD3DMetalSidecarPatch } from "./d3dmetal-sidecar-patch.mjs";
import { D3DMETAL_RELEASE, D3DMETAL_RUNTIME_HASHES, D3DMETAL_FRAMEWORK_RESOURCE_HASHES } from "./d3dmetal-runtime-spec.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const sourcePaths = [
  "scripts/build-d3dmetal-autopatch.mjs",
  "scripts/d3dmetal-runtime-spec.mjs",
  "scripts/prepare-d3dmetal-runtime.swift",
  "scripts/d3dmetal-sidecar-patch.mjs",
  "scripts/build-d3dmetal-sidecar.mjs",
  "d3dmetal-sidecar/layout.json",
];
const helperName = "prepare-d3dmetal-runtime";
const sidecarName = "libD3DMetalSidecar.dylib";

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
  const usage = "usage: node scripts/build-d3dmetal-autopatch.mjs OUTPUT_DIR --framework D3DMetal.framework --accept-apple-license [--sidecar-module PATH --sidecar-manifest PATH]";
  if (!argv[0]) throw new Error(usage);
  const options = { output: resolve(argv[0]), sidecarModule: null, sidecarManifest: null, framework: null, accepted: false };
  const keys = { "--framework": "framework", "--sidecar-module": "sidecarModule", "--sidecar-manifest": "sidecarManifest" };
  for (let index = 1; index < argv.length; index++) {
    if (argv[index] === "--accept-apple-license") { options.accepted = true; continue; }
    const key = keys[argv[index]];
    if (!key || !argv[index + 1] || options[key]) throw new Error(usage);
    options[key] = resolve(argv[++index]);
  }
  if (!options.framework || !options.accepted || Boolean(options.sidecarModule) !== Boolean(options.sidecarManifest)) throw new Error(usage);
  return options;
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

export function createAutopatchRecipe(pristineBinary, rawSidecar, signedSidecar) {
  if (sha256(pristineBinary) !== D3DMETAL_RUNTIME_HASHES.pristineD3DMetal) throw new Error("expected pristine beta3 D3DMetal");
  for (const hash of [rawSidecar, signedSidecar]) {
    if (!/^[a-f0-9]{64}$/.test(hash)) throw new Error("invalid built sidecar hash");
  }
  const patched = applyD3DMetalSidecarPatch(pristineBinary);
  return {
    schema: 2,
    release: D3DMETAL_RELEASE,
    frameworkResources: D3DMETAL_FRAMEWORK_RESOURCE_HASHES,
    hashes: { ...D3DMETAL_RUNTIME_HASHES, compositePreSign: sha256(patched), rawSidecar, signedSidecar },
    patches: {
      nativeSidecar: patchRecipe(D3DMETAL_SIDECAR_PATCH_SITES),
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
  if (options.sidecarModule) await assertRegularFile(options.sidecarModule);
  const pristineBinary = await readFile(join(options.framework, "Versions/A/D3DMetal"));
  if (sha256(pristineBinary) !== D3DMETAL_RUNTIME_HASHES.pristineD3DMetal ||
      await sha256File(join(options.framework, "Versions/A/Resources/libmetalirconverter.dylib")) !== D3DMETAL_RUNTIME_HASHES.pristineConverter) {
    throw new Error("framework is not pristine Apple GPTK beta3");
  }
  for (const [name, hash] of Object.entries(D3DMETAL_FRAMEWORK_RESOURCE_HASHES)) {
    if (await sha256File(join(options.framework, "Versions/A/Resources", name)) !== hash) {
      throw new Error(`framework resource is not pristine Apple GPTK beta3: ${name}`);
    }
  }
  const sidecarInputHash = options.sidecarModule ? await sha256File(options.sidecarModule) : null;
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
    let sourceSidecar = options.sidecarModule;
    if (!sourceSidecar) {
      run(process.execPath, [resolve(root, "scripts/build-d3dmetal-sidecar.mjs"), nativeBuild]);
      sourceSidecar = join(nativeBuild, sidecarName);
      await assertRegularFile(sourceSidecar);
    }
    const actualInputHash = await sha256File(sourceSidecar);
    const nativeManifest = JSON.parse(await readFile(options.sidecarManifest ?? join(nativeBuild, "build-manifest.json"), "utf8"));
    if (nativeManifest.schemaVersion !== 1 || nativeManifest.architecture !== "x86_64" ||
        nativeManifest.testControls !== false || nativeManifest.module?.sha256 !== actualInputHash ||
        !nativeManifest.sources?.length) throw new Error("native sidecar module differs from its build manifest");
    for (const source of nativeManifest.sources) {
      if (!source.path || resolve(root, source.path).startsWith(`${root}/`) === false ||
          await sha256File(resolve(root, source.path)) !== source.sha256) {
        throw new Error(`native sidecar source mismatch: ${source.path}`);
      }
    }
    const sidecar = join(bundle, sidecarName);
    await copyFile(sourceSidecar, sidecar);
    await chmod(sidecar, 0o755);
    run("/usr/bin/codesign", ["--force", "--sign", "-", sidecar]);
    run("/usr/bin/codesign", ["--verify", "--strict", sidecar]);
    const signedSidecarHash = await sha256File(sidecar);
    const recipe = JSON.stringify(createAutopatchRecipe(pristineBinary, actualInputHash, signedSidecarHash));
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
      schema: 2,
      gptkVersion: D3DMETAL_RELEASE.version,
      target: "arm64-apple-macos26.0",
      sources,
      compiler: { command: "/usr/bin/xcrun swiftc", version: swiftVersion },
      recipe: { schema: 2, sha256: sha256(Buffer.from(recipe)), generatedSourceSha256: await sha256File(recipeSource) },
      nativeBuildManifest: nativeManifest,
      sidecarInput: { sha256: actualInputHash, mode: options.sidecarModule ? "provided" : "built" },
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
    if (options.sidecarModule && await sha256File(options.sidecarModule) !== sidecarInputHash) {
      throw new Error("native sidecar module changed during build");
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
