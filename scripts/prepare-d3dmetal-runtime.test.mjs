import assert from "node:assert/strict";
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";
import { after, before, describe, test } from "node:test";
import { createAutopatchRecipe } from "./build-d3dmetal-autopatch.mjs";
import { D3DMETAL_RELEASE, D3DMETAL_RUNTIME_HASHES } from "./d3dmetal-runtime-spec.mjs";

const builder = fileURLToPath(new URL("./build-d3dmetal-autopatch.mjs", import.meta.url));

describe("native D3DMetal preparation safety", { skip: process.platform !== "darwin" }, () => {
  let root;
  let executable;
  let bundle;
  const nativeEnvironment = {
    PATH: "/nonexistent",
    DEVELOPER_DIR: "/nonexistent",
  };

  before(() => {
    root = mkdtempSync(join(tmpdir(), "native-d3dmetal-test-"));
    bundle = process.env.YAAGL_AUTOPATCH_TEST_BUNDLE
      ? resolve(process.env.YAAGL_AUTOPATCH_TEST_BUNDLE)
      : join(root, "autopatch");
    if (!process.env.YAAGL_AUTOPATCH_TEST_BUNDLE) {
      const built = spawnSync(process.execPath, [builder, bundle], {
        encoding: "utf8",
      });
      assert.equal(built.status, 0, built.stderr || built.stdout);
    }
    executable = join(bundle, "prepare-d3dmetal-runtime");
  });

  after(() => {
    if (root) rmSync(root, { recursive: true, force: true });
  });

  test("requires explicit license acceptance before creating output or cache", () => {
    const directory = mkdtempSync(join(root, "license-"));
    const output = join(directory, "D3DMetal");
    const cache = join(directory, "cache");
    const result = spawnSync(executable, ["--output", output, "--cache", cache], {
      encoding: "utf8",
      env: nativeEnvironment,
    });
    assert.equal(result.status, 1);
    assert.equal(existsSync(output), false);
    assert.equal(existsSync(cache), false);
  });

  test("refuses an existing output without force and preserves its contents", () => {
    const directory = mkdtempSync(join(root, "existing-"));
    const output = join(directory, "D3DMetal");
    const cache = join(directory, "cache");
    mkdirSync(output);
    writeFileSync(join(output, "previous-runtime"), "keep this runtime");
    const result = spawnSync(executable, [
      "--accept-apple-license", "--output", output, "--cache", cache,
    ], { encoding: "utf8", env: nativeEnvironment });
    assert.equal(result.status, 1);
    assert.equal(readFileSync(join(output, "previous-runtime"), "utf8"), "keep this runtime");
    assert.equal(existsSync(cache), false);
  });

  test("failed forced preparation preserves the previous runtime", () => {
    const directory = mkdtempSync(join(root, "failed-replacement-"));
    const output = join(directory, "D3DMetal");
    const corruptAsset = join(directory, "corrupt-local-asset");
    mkdirSync(output);
    writeFileSync(join(output, "previous-runtime"), "keep this runtime");
    writeFileSync(corruptAsset, "intentionally corrupt input, not an Apple asset");
    const result = spawnSync(executable, [
      "--accept-apple-license", "--force", "--output", output,
      "--cache", join(directory, "cache"),
      "--archive", corruptAsset, "--license", corruptAsset,
      "--acknowledgements", corruptAsset, "--sums", corruptAsset,
    ], { encoding: "utf8", env: nativeEnvironment });
    assert.equal(result.status, 1);
    assert.equal(readFileSync(join(output, "previous-runtime"), "utf8"), "keep this runtime");
    assert.equal(existsSync(join(output, "prepared-d3dmetal.json")), false);
  });

  test("offline preparation accepts real local signatures, records actual hashes, and rejects sidecar tampering", {
    skip: !process.env.YAAGL_D3DMETAL_TEST_ASSETS,
  }, () => {
    const directory = mkdtempSync(join(root, "offline-signing-"));
    const assets = resolve(process.env.YAAGL_D3DMETAL_TEST_ASSETS);
    const sidecar = join(directory, "libYaaglNativePsoCache.dylib");
    copyFileSync(join(bundle, "libYaaglNativePsoCache.dylib"), sidecar);
    const signed = spawnSync("/usr/bin/codesign", [
      "--force", "--sign", "-", "--identifier", "org.yaagl.native-pso.local-signature-regression", sidecar,
    ], { encoding: "utf8" });
    assert.equal(signed.status, 0, signed.stderr || signed.stdout);
    const sourceHash = createHash("sha256").update(readFileSync(sidecar)).digest("hex");
    assert.notEqual(sourceHash, D3DMETAL_RUNTIME_HASHES.signedSidecar);
    const recipeSource = join(directory, "AutopatchRecipe.swift");
    const recipe = Buffer.from(JSON.stringify(createAutopatchRecipe(sourceHash))).toString("base64");
    writeFileSync(recipeSource, `enum AutopatchRecipe { static let base64 = "${recipe}" }\n`);
    const localExecutable = join(directory, "prepare-d3dmetal-runtime");
    const compiled = spawnSync("/usr/bin/xcrun", [
      "swiftc", "-O", "-target", "arm64-apple-macos26.0",
      fileURLToPath(new URL("./prepare-d3dmetal-runtime.swift", import.meta.url)), recipeSource,
      "-o", localExecutable,
    ], { encoding: "utf8" });
    assert.equal(compiled.status, 0, compiled.stderr || compiled.stdout);
    const output = join(directory, "prepared");
    const argumentsForOutput = [
      "--accept-apple-license", "--output", output, "--cache", join(directory, "cache"),
      "--archive", join(assets, D3DMETAL_RELEASE.assets.framework.name),
      "--license", join(assets, D3DMETAL_RELEASE.assets.license.name),
      "--acknowledgements", join(assets, D3DMETAL_RELEASE.assets.acknowledgements.name),
      "--sums", join(assets, D3DMETAL_RELEASE.assets.sums.name), "--pso-module", sidecar,
    ];
    const prepared = spawnSync(localExecutable, argumentsForOutput, { encoding: "utf8", env: nativeEnvironment });
    assert.equal(prepared.status, 0, prepared.stderr || prepared.stdout);
    const manifestPath = join(output, "prepared-d3dmetal.json");
    const manifestBytes = readFileSync(manifestPath);
    const manifest = JSON.parse(manifestBytes);
    const version = join(output, "D3DMetal.framework", "Versions", "A");
    const paths = [
      [join(version, "D3DMetal"), manifest.framework.finalD3DMetalSha256],
      [join(version, "Resources", "libmetalirconverter.dylib"), manifest.metalIrConverter.fp64SignedSha256],
      [join(version, "Resources", "libYaaglNativePsoCache.dylib"), manifest.nativePsoSidecar.signedSha256],
    ];
    for (const [path, reportedHash] of paths) {
      assert.equal(reportedHash, createHash("sha256").update(readFileSync(path)).digest("hex"));
      const verified = spawnSync("/usr/bin/codesign", ["--verify", "--strict", path], { encoding: "utf8" });
      assert.equal(verified.status, 0, verified.stderr || verified.stdout);
    }
    assert.equal(manifest.nativePsoSidecar.sourceSha256, sourceHash);
    const tampered = readFileSync(sidecar);
    tampered[tampered.length - 1] ^= 1;
    writeFileSync(sidecar, tampered);
    const rejected = spawnSync(localExecutable, [...argumentsForOutput, "--force"], { encoding: "utf8", env: nativeEnvironment });
    assert.equal(rejected.status, 1, rejected.stderr || rejected.stdout);
    assert.deepEqual(readFileSync(manifestPath), manifestBytes);
    for (const [path, reportedHash] of paths) {
      assert.equal(createHash("sha256").update(readFileSync(path)).digest("hex"), reportedHash);
    }
  });
});
