#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import {
  chmod,
  copyFile,
  lstat,
  mkdir,
  readFile,
  readlink,
  rename,
  rm,
  stat,
  writeFile,
} from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  METAL_IR_CONVERTER_4_0_BETA_2_FP64_PATCHED_SHA256,
  METAL_IR_CONVERTER_4_0_BETA_2_SHA256,
  applyFP64CodecPatch,
  inspectFP64CodecPatch,
} from "./metalir-fp64-codec-patch.mjs";
import {
  D3DMETAL_PSO_CACHE_PATCHED_PAYLOAD_SHA256,
  applyD3DMetalPsoCachePatch,
  inspectD3DMetalPsoCachePatch,
} from "./d3dmetal-pso-cache-patch.mjs";
import { D3DMETAL_STAGE_LOCK_SOURCE_SHA256 } from "./d3dmetal-stage-lock-patch.mjs";

export const D3DMETAL_RELEASE = Object.freeze({
  repository: "https://github.com/dbc-hbin/d3dmetal-redistributable",
  tag: "gptk-4.0b2",
  assets: Object.freeze({
    framework: Object.freeze({
      name: "D3DMetal.framework-4.0b2.zip",
      sha256: "61ff2bb920376ce58709cabb081a5b5e7948c778939dbe66bfa0edca7c2f0d68",
    }),
    license: Object.freeze({
      name: "License.rtf",
      sha256: "5abb2d059be217663b00e8fd37e14411d374e11d17e3b744eebd49b8d17118c8",
    }),
    acknowledgements: Object.freeze({
      name: "Acknowledgements.rtf",
      sha256: "6f3aa835f6d0d06f89997d0a346a209e39a8105521fd939e096c5b24dc0cb0a6",
    }),
    sums: Object.freeze({
      name: "SHA256SUMS",
      sha256: "0b86038435fcb90a4bc1b27ab7c02fc02ad245365134731cbe6a5aeea2f1a973",
    }),
  }),
});

export const D3DMETAL_4_0_BETA_2_SHA256 =
  "f5b56df1b8fe8b364dd9530651a3769c8aed948bd343be3b4510604d503e2bad";

export const D3DMETAL_4_0_BETA_2_COMPOSITE_INPUT_SHA256 =
  D3DMETAL_STAGE_LOCK_SOURCE_SHA256;

export const D3DMETAL_4_0_BETA_2_COMPOSITE_PRE_SIGN_SHA256 =
  "b7b06c767d4ec71ea76a3fd8918a79db0e7fdbb30a555062adff61bfd5f4e1b0";

export const D3DMETAL_4_0_BETA_2_UNIFIED_FINAL_SHA256 =
  "ebd6be389eb34576b4158accf9e785ad58f9a2ef3c4ca03f4d188a85c1338637";

export const D3DMETAL_PSO_MODULE_RAW_SHA256 =
  "a2fe5f470854d3c350be3c4b6d8e4ffe67d15768db66114d5a82a8e6d0c8fbed";

export const D3DMETAL_PSO_MODULE_SIGNED_SHA256 =
  "e5e69f05c069bafd242759e86c64332de005be8bccdd6d9c882f379ece16f75f";

// Exact output of applyFP64CodecPatch() before the invalidated Apple signature
// is replaced by the deterministic ad-hoc signature below.
export const METAL_IR_CONVERTER_4_0_BETA_2_FP64_UNSIGNED_SHA256 =
  "c4c5265e355c59b93e4684de79289ff2b7606756b70c25258dd8d29f59c3ea04";

const releaseBase =
  `${D3DMETAL_RELEASE.repository}/releases/download/${D3DMETAL_RELEASE.tag}`;

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

async function sha256File(path) {
  return sha256(await readFile(path));
}

function fail(message) {
  throw new Error(message);
}

async function replacePreservingMode(path, bytes) {
  const mode = (await stat(path)).mode;
  const temporary = path + ".tmp-" + randomUUID();
  await writeFile(temporary, bytes, { flag: "wx" });
  await chmod(temporary, mode);
  await rename(temporary, path);
}

function run(command, args, { capture = false } = {}) {
  const result = spawnSync(command, args, {
    encoding: "utf8",
    stdio: capture ? "pipe" : "inherit",
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    const detail = [result.stdout, result.stderr].filter(Boolean).join("").trim();
    fail(`${command} exited ${result.status}${detail ? `: ${detail}` : ""}`);
  }
  return capture ? `${result.stdout ?? ""}${result.stderr ?? ""}`.trim() : "";
}

function parseArgs(argv) {
  const options = {
    output: null,
    cache: null,
    archive: null,
    license: null,
    acknowledgements: null,
    sums: null,
    psoModule: null,
    acceptAppleLicense: false,
    force: false,
  };

  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    const value = () => {
      if (index + 1 >= argv.length) fail(`missing value for ${argument}`);
      index += 1;
      return resolve(argv[index]);
    };
    if (argument === "--output") options.output = value();
    else if (argument === "--cache") options.cache = value();
    else if (argument === "--archive") options.archive = value();
    else if (argument === "--license") options.license = value();
    else if (argument === "--acknowledgements") options.acknowledgements = value();
    else if (argument === "--sums") options.sums = value();
    else if (argument === "--pso-module") options.psoModule = value();
    else if (argument === "--accept-apple-license") options.acceptAppleLicense = true;
    else if (argument === "--force") options.force = true;
    else fail(`unknown argument: ${argument}`);
  }
  if (!options.output) fail("--output DIR is required");
  if (!options.acceptAppleLicense) {
    fail(
      "refusing to prepare Apple software without --accept-apple-license; " +
        "review the pinned License.rtf first",
    );
  }
  options.cache ??= join(dirname(options.output), ".d3dmetal-download-cache");
  return options;
}

async function download(url, destination, expectedSha256) {
  const current = await sha256File(destination).catch(() => null);
  if (current === expectedSha256) return;

  const response = await fetch(url, { redirect: "follow" });
  if (!response.ok) fail(`download failed (${response.status}) for ${url}`);
  const bytes = Buffer.from(await response.arrayBuffer());
  const actual = sha256(bytes);
  if (actual !== expectedSha256) {
    fail(`download hash mismatch for ${url}: expected ${expectedSha256}, got ${actual}`);
  }

  const temporary = `${destination}.tmp-${randomUUID()}`;
  await writeFile(temporary, bytes, { flag: "wx", mode: 0o600 });
  await rename(temporary, destination);
}

async function expectSymlink(path, target) {
  const info = await lstat(path);
  if (!info.isSymbolicLink()) fail(`expected symlink: ${path}`);
  const actual = await readlink(path);
  if (actual !== target) fail(`unexpected symlink target for ${path}: ${actual}`);
}

async function assertRegularFile(path) {
  const info = await lstat(path);
  if (!info.isFile() || info.isSymbolicLink()) fail(`expected regular file: ${path}`);
}

async function ensureAssets(options) {
  await mkdir(options.cache, { recursive: true, mode: 0o700 });
  const assets = {};
  for (const [key, metadata] of Object.entries(D3DMETAL_RELEASE.assets)) {
    const destination = join(options.cache, metadata.name);
    const override =
      key === "framework" ? options.archive :
      key === "license" ? options.license :
      key === "acknowledgements" ? options.acknowledgements :
      options.sums;

    if (override) {
      const actual = await sha256File(override);
      if (actual !== metadata.sha256) {
        fail(`local ${key} hash mismatch: expected ${metadata.sha256}, got ${actual}`);
      }
      await copyFile(override, destination);
      await chmod(destination, 0o600);
    } else {
      await download(`${releaseBase}/${metadata.name}`, destination, metadata.sha256);
    }
    assets[key] = destination;
  }

  const sums = (await readFile(assets.sums, "utf8")).trim();
  const expectedLine =
    `${D3DMETAL_RELEASE.assets.framework.sha256}  ${D3DMETAL_RELEASE.assets.framework.name}`;
  if (sums !== expectedLine) fail("gptk-4.0b2 SHA256SUMS does not match the pinned framework asset");
  return assets;
}

async function prepare(options) {
  if (process.platform !== "darwin") fail("D3DMetal preparation requires macOS");
  run("/usr/bin/xcrun", ["--find", "codesign"], { capture: true });
  run("/usr/bin/which", ["ditto"], { capture: true });

  const existing = await lstat(options.output).catch(() => null);
  if (existing && !options.force) {
    fail(`output already exists: ${options.output}; pass --force to replace it`);
  }
  if (existing && options.force) await rm(options.output, { recursive: true, force: true });

  const assets = await ensureAssets(options);
  await mkdir(dirname(options.output), { recursive: true });
  const temporary = join(
    dirname(options.output),
    `.d3dmetal-prepare-${process.pid}-${randomUUID()}`,
  );
  const extracted = join(temporary, "extracted");
  const resultRoot = join(temporary, "result");
  const framework = join(resultRoot, "D3DMetal.framework");

  try {
    await mkdir(extracted, { recursive: true, mode: 0o700 });
    await mkdir(resultRoot, { recursive: true, mode: 0o700 });
    run("/usr/bin/ditto", ["-x", "-k", assets.framework, extracted]);
    await rm(join(extracted, "__MACOSX"), { recursive: true, force: true });

    const sourceFramework = join(extracted, "D3DMetal.framework");
    await expectSymlink(join(sourceFramework, "D3DMetal"), "Versions/Current/D3DMetal");
    await expectSymlink(join(sourceFramework, "Resources"), "Versions/Current/Resources");
    await expectSymlink(join(sourceFramework, "Versions", "Current"), "A");
    run("/usr/bin/ditto", [sourceFramework, framework]);

    const d3dmetal = join(framework, "Versions", "A", "D3DMetal");
    const converter = join(
      framework,
      "Versions",
      "A",
      "Resources",
      "libmetalirconverter.dylib",
    );
    const infoPlist = join(framework, "Versions", "A", "Resources", "Info.plist");
    await assertRegularFile(d3dmetal);
    await assertRegularFile(converter);
    await assertRegularFile(infoPlist);

    const version = run(
      "/usr/libexec/PlistBuddy",
      ["-c", "Print :CFBundleShortVersionString", infoPlist],
      { capture: true },
    );
    if (version !== "4.0b2") fail(`unexpected D3DMetal version: ${version}`);

    const pristineD3DMetalHash = await sha256File(d3dmetal);
    if (pristineD3DMetalHash !== D3DMETAL_4_0_BETA_2_SHA256) {
      fail(
        `unexpected D3DMetal binary: expected ${D3DMETAL_4_0_BETA_2_SHA256}, ` +
          `got ${pristineD3DMetalHash}`,
      );
    }
    const pristineConverterHash = await sha256File(converter);
    if (pristineConverterHash !== METAL_IR_CONVERTER_4_0_BETA_2_SHA256) {
      fail(
        `unexpected Metal IR converter: expected ${METAL_IR_CONVERTER_4_0_BETA_2_SHA256}, ` +
          `got ${pristineConverterHash}`,
      );
    }
    run("/usr/bin/codesign", ["--verify", "--deep", "--strict", framework]);

    const fp64Patched = applyFP64CodecPatch(await readFile(converter));
    const unsignedHash = sha256(fp64Patched);
    if (unsignedHash !== METAL_IR_CONVERTER_4_0_BETA_2_FP64_UNSIGNED_SHA256) {
      fail(
        "unexpected unsigned FP64 patch result: expected " +
          METAL_IR_CONVERTER_4_0_BETA_2_FP64_UNSIGNED_SHA256 +
          ", got " +
          unsignedHash,
      );
    }
    await replacePreservingMode(converter, fp64Patched);
    if (inspectFP64CodecPatch(await readFile(converter)).mode !== "patched") {
      fail("FP64 patch inspection failed before signing");
    }

    // Repair FP64 first, then reseal the framework. That FP64-only identity is
    // the source locked by the stage/PSO patchers.
    run("/usr/bin/codesign", ["--force", "--sign", "-", converter]);
    run("/usr/bin/codesign", ["--force", "--deep", "--sign", "-", framework]);

    const signedConverterHash = await sha256File(converter);
    if (signedConverterHash !== METAL_IR_CONVERTER_4_0_BETA_2_FP64_PATCHED_SHA256) {
      fail(
        "unexpected signed FP64 converter: expected " +
          METAL_IR_CONVERTER_4_0_BETA_2_FP64_PATCHED_SHA256 +
          ", got " +
          signedConverterHash,
      );
    }
    if (inspectFP64CodecPatch(await readFile(converter)).mode !== "patched") {
      fail("FP64 patch inspection failed after signing");
    }

    const compositeInputHash = await sha256File(d3dmetal);
    if (compositeInputHash !== D3DMETAL_4_0_BETA_2_COMPOSITE_INPUT_SHA256) {
      fail(
        "unexpected stage-lock/native-PSO input: expected " +
          D3DMETAL_4_0_BETA_2_COMPOSITE_INPUT_SHA256 +
          ", got " +
          compositeInputHash,
      );
    }
    const compositeInputInspection = inspectD3DMetalPsoCachePatch(
      await readFile(d3dmetal),
    );
    if (compositeInputInspection.mode !== "original") {
      fail(
        "stage-lock/native-PSO input inspection failed: " +
          compositeInputInspection.mode,
      );
    }

    const nativeBuild = join(temporary, "native-pso");
    let psoSource;
    let psoSourceHash;
    if (options.psoModule) {
      await assertRegularFile(options.psoModule);
      psoSource = options.psoModule;
      psoSourceHash = await sha256File(psoSource);
      if (
        psoSourceHash !== D3DMETAL_PSO_MODULE_RAW_SHA256 &&
        psoSourceHash !== D3DMETAL_PSO_MODULE_SIGNED_SHA256
      ) {
        fail(
          "unexpected native PSO module: expected " +
            D3DMETAL_PSO_MODULE_RAW_SHA256 +
            " or " +
            D3DMETAL_PSO_MODULE_SIGNED_SHA256 +
            ", got " +
            psoSourceHash,
        );
      }
    } else {
      run(process.execPath, [
        join(dirname(fileURLToPath(import.meta.url)), "build-d3dmetal-pso-cache.mjs"),
        nativeBuild,
      ]);
      psoSource = join(nativeBuild, "libYaaglNativePsoCache.dylib");
      psoSourceHash = await sha256File(psoSource);
      if (psoSourceHash !== D3DMETAL_PSO_MODULE_RAW_SHA256) {
        fail(
          "native PSO build is not reproducible: expected " +
            D3DMETAL_PSO_MODULE_RAW_SHA256 +
            ", got " +
            psoSourceHash,
        );
      }
    }

    const psoModule = join(
      framework,
      "Versions",
      "A",
      "Resources",
      "libYaaglNativePsoCache.dylib",
    );
    await copyFile(psoSource, psoModule);
    await chmod(psoModule, 0o755);

    const compositePatched = applyD3DMetalPsoCachePatch(await readFile(d3dmetal));
    const compositePreSignHash = sha256(compositePatched);
    if (compositePreSignHash !== D3DMETAL_4_0_BETA_2_COMPOSITE_PRE_SIGN_SHA256) {
      fail(
        "unexpected composite patch result: expected " +
          D3DMETAL_4_0_BETA_2_COMPOSITE_PRE_SIGN_SHA256 +
          ", got " +
          compositePreSignHash,
      );
    }
    await replacePreservingMode(d3dmetal, compositePatched);
    const compositePreSignInspection = inspectD3DMetalPsoCachePatch(
      await readFile(d3dmetal),
    );
    if (
      compositePreSignInspection.mode !== "patched" ||
      compositePreSignInspection.payloadSha256 !==
        D3DMETAL_PSO_CACHE_PATCHED_PAYLOAD_SHA256
    ) {
      fail("stage-lock/native-PSO composite verification failed before signing");
    }

    // codesign --deep does not reliably replace arbitrary Resources dylibs.
    // Sign both nested dylibs explicitly, then reseal the complete framework.
    run("/usr/bin/codesign", ["--force", "--sign", "-", psoModule]);
    run("/usr/bin/codesign", ["--force", "--sign", "-", converter]);
    run("/usr/bin/codesign", ["--force", "--deep", "--sign", "-", framework]);
    run("/usr/bin/codesign", ["--verify", "--strict", psoModule]);
    run("/usr/bin/codesign", ["--verify", "--strict", converter]);
    run("/usr/bin/codesign", ["--verify", "--strict", d3dmetal]);
    run("/usr/bin/codesign", ["--verify", "--deep", "--strict", framework]);

    const signedPsoHash = await sha256File(psoModule);
    if (signedPsoHash !== D3DMETAL_PSO_MODULE_SIGNED_SHA256) {
      fail(
        "unexpected signed native PSO module: expected " +
          D3DMETAL_PSO_MODULE_SIGNED_SHA256 +
          ", got " +
          signedPsoHash,
      );
    }
    const finalD3DMetalHash = await sha256File(d3dmetal);
    if (finalD3DMetalHash !== D3DMETAL_4_0_BETA_2_UNIFIED_FINAL_SHA256) {
      fail(
        "unexpected final D3DMetal: expected " +
          D3DMETAL_4_0_BETA_2_UNIFIED_FINAL_SHA256 +
          ", got " +
          finalD3DMetalHash,
      );
    }
    const finalCompositeInspection = inspectD3DMetalPsoCachePatch(
      await readFile(d3dmetal),
    );
    if (
      finalCompositeInspection.mode !== "patched-signed" ||
      finalCompositeInspection.payloadSha256 !==
        D3DMETAL_PSO_CACHE_PATCHED_PAYLOAD_SHA256
    ) {
      fail("stage-lock/native-PSO composite verification failed after signing");
    }
    const dependencies = run("/usr/bin/otool", ["-L", d3dmetal], {
      capture: true,
    })
      .split("\n")
      .filter(line =>
        line.includes("@loader_path/Resources/libYaaglNativePsoCache.dylib"),
      );
    if (dependencies.length !== 1) {
      fail("final D3DMetal does not contain exactly one native PSO sidecar dependency");
    }

    const converterSignature = run(
      "/usr/bin/codesign",
      ["-d", "--verbose=4", converter],
      { capture: true },
    );
    if (!converterSignature.includes("Signature=adhoc")) {
      fail("patched Metal IR converter is not ad-hoc signed");
    }
    const d3dmetalSignature = run(
      "/usr/bin/codesign",
      ["-d", "--verbose=4", d3dmetal],
      { capture: true },
    );
    if (!d3dmetalSignature.includes("Signature=adhoc")) {
      fail("resealed D3DMetal binary is not ad-hoc signed");
    }
    const psoSignature = run(
      "/usr/bin/codesign",
      ["-d", "--verbose=4", psoModule],
      { capture: true },
    );
    if (!psoSignature.includes("Signature=adhoc")) {
      fail("native PSO sidecar is not ad-hoc signed");
    }

    await copyFile(assets.license, join(resultRoot, "License.rtf"));
    await copyFile(assets.acknowledgements, join(resultRoot, "Acknowledgements.rtf"));
    await copyFile(assets.sums, join(resultRoot, "SHA256SUMS"));

    const manifest = {
      schema: 2,
      source: {
        repository: D3DMETAL_RELEASE.repository,
        tag: D3DMETAL_RELEASE.tag,
        frameworkAsset: D3DMETAL_RELEASE.assets.framework,
        licenseAsset: D3DMETAL_RELEASE.assets.license,
        acknowledgementsAsset: D3DMETAL_RELEASE.assets.acknowledgements,
      },
      licenseAcceptance: "explicit-cli-flag",
      framework: {
        version: "4.0b2",
        pristineD3DMetalSha256: pristineD3DMetalHash,
        compositeInputSha256: compositeInputHash,
        compositePreSignSha256: compositePreSignHash,
        compositePayloadSha256: finalCompositeInspection.payloadSha256,
        finalD3DMetalSha256: finalD3DMetalHash,
        compositeMode: finalCompositeInspection.mode,
        signature: "adhoc",
      },
      metalIrConverter: {
        pristineSha256: pristineConverterHash,
        fp64UnsignedSha256: unsignedHash,
        fp64SignedSha256: signedConverterHash,
        patchMode: "patched",
        signature: "adhoc",
      },
      nativePsoSidecar: {
        sourceSha256: psoSourceHash,
        signedSha256: signedPsoHash,
        dependency: "@loader_path/Resources/libYaaglNativePsoCache.dylib",
        signature: "adhoc",
      },
      patchPipeline: [
        "fp64-codec",
        "framework-reseal-to-composite-input",
        "stage-lock",
        "native-pso-dxil-composite",
        "native-pso-sidecar",
        "final-framework-reseal",
      ],
    };
    await writeFile(
      join(resultRoot, "prepared-d3dmetal.json"),
      `${JSON.stringify(manifest, null, 2)}\n`,
      { mode: 0o600 },
    );

    await rename(resultRoot, options.output);
    return manifest;
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
}

async function main(argv) {
  const options = parseArgs(argv);
  const manifest = await prepare(options);
  console.log(JSON.stringify({ output: options.output, ...manifest }, null, 2));
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2)).catch(error => {
    console.error(`prepare-d3dmetal-runtime: ${error.message}`);
    process.exitCode = 1;
  });
}
