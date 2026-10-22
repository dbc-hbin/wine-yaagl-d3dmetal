import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
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
import { fileURLToPath } from "node:url";
import { after, before, describe, test } from "node:test";

const builder = fileURLToPath(new URL("./build-d3dmetal-autopatch.mjs", import.meta.url));

describe("native D3DMetal preparation safety", { skip: process.platform !== "darwin" }, () => {
  let root;
  let executable;
  const nativeEnvironment = {
    PATH: "/nonexistent",
    DEVELOPER_DIR: "/nonexistent",
  };

  before(() => {
    root = mkdtempSync(join(tmpdir(), "native-d3dmetal-test-"));
    const bundle = process.env.YAAGL_AUTOPATCH_TEST_BUNDLE
      ? resolve(process.env.YAAGL_AUTOPATCH_TEST_BUNDLE)
      : join(root, "autopatch");
    if (!process.env.YAAGL_AUTOPATCH_TEST_BUNDLE) {
      assert.ok(process.env.YAAGL_AUTOPATCH_TEST_FRAMEWORK,
        "set YAAGL_AUTOPATCH_TEST_BUNDLE or YAAGL_AUTOPATCH_TEST_FRAMEWORK to the pristine beta3 framework");
      const built = spawnSync(process.execPath, [builder, bundle,
        "--framework", process.env.YAAGL_AUTOPATCH_TEST_FRAMEWORK, "--accept-apple-license"], {
        encoding: "utf8",
      });
      assert.equal(built.status, 0, built.stderr || built.stdout);
    }
    executable = join(bundle, "prepare-d3dmetal-runtime");
  });

  after(() => {
    if (root) rmSync(root, { recursive: true, force: true });
  });

  test("requires explicit license acceptance before creating output", () => {
    const directory = mkdtempSync(join(root, "license-"));
    const output = join(directory, "D3DMetal");
    const result = spawnSync(executable, ["--output", output, "--dmg", join(directory, "input.dmg")], {
      encoding: "utf8",
      env: nativeEnvironment,
    });
    assert.equal(result.status, 1);
    assert.equal(existsSync(output), false);
  });

  test("refuses an existing output without force and preserves its contents", () => {
    const directory = mkdtempSync(join(root, "existing-"));
    const output = join(directory, "D3DMetal");
    mkdirSync(output);
    writeFileSync(join(output, "previous-runtime"), "keep this runtime");
    const result = spawnSync(executable, [
      "--accept-apple-license", "--output", output, "--dmg", join(directory, "input.dmg"),
    ], { encoding: "utf8", env: nativeEnvironment });
    assert.equal(result.status, 1);
    assert.equal(readFileSync(join(output, "previous-runtime"), "utf8"), "keep this runtime");
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
      "--dmg", corruptAsset,
    ], { encoding: "utf8", env: nativeEnvironment });
    assert.equal(result.status, 1);
    assert.equal(readFileSync(join(output, "previous-runtime"), "utf8"), "keep this runtime");
    assert.equal(existsSync(join(output, "prepared-d3dmetal.json")), false);
  });

  test("requires exactly one local source, without remote fallback", () => {
    for (const inputs of [[], ["--dmg", "/missing.dmg", "--framework", "/missing.framework"]]) {
      const output = join(root, "no-source-output");
      const result = spawnSync(executable, [
        "--accept-apple-license", "--output", output, ...inputs,
      ], { encoding: "utf8", env: nativeEnvironment });
      assert.equal(result.status, 1);
      assert.equal(existsSync(output), false);
    }
  });

  test("framework input requires its license documents", () => {
    const output = join(root, "no-license-documents");
    const result = spawnSync(executable, [
      "--accept-apple-license", "--output", output, "--framework", "/missing.framework",
    ], { encoding: "utf8", env: nativeEnvironment });
    assert.equal(result.status, 1);
    assert.equal(existsSync(output), false);
  });

  test("prepares the local beta3 image with pristine converter and preserves its input", {
    skip: !process.env.YAAGL_AUTOPATCH_TEST_DMG,
  }, () => {
    const output = join(root, "prepared");
    const imageHash = () => createHash("sha256").update(readFileSync(process.env.YAAGL_AUTOPATCH_TEST_DMG)).digest("hex");
    const originalImageHash = imageHash();
    const result = spawnSync(executable, [
      "--accept-apple-license", "--output", output, "--dmg", process.env.YAAGL_AUTOPATCH_TEST_DMG,
    ], { encoding: "utf8", env: nativeEnvironment });
    assert.equal(result.status, 0, result.stderr || result.stdout);
    assert.equal(imageHash(), originalImageHash);
    const manifest = JSON.parse(readFileSync(join(output, "prepared-d3dmetal.json"), "utf8"));
    assert.equal(manifest.schema, 3);
    assert.equal(manifest.framework.version, "4.0b3");
    assert.equal(manifest.source.kind, "local-apple-dmg");
    assert.equal(manifest.metalIrConverter.patchMode, "pristine");
    assert.deepEqual(manifest.patchPipeline, ["native-sidecar-patch", "native-sidecar-module", "final-framework-reseal"]);
    const converter = join(output, "D3DMetal.framework/Versions/A/Resources/libmetalirconverter.dylib");
    const hashed = spawnSync("/usr/bin/shasum", ["-a", "256", converter], { encoding: "utf8" });
    assert.equal(hashed.status, 0, hashed.stderr);
    assert.equal(hashed.stdout.split(" ")[0], manifest.metalIrConverter.pristineSha256);
    assert.equal(Object.keys(manifest.frameworkResources).length, 8);
    for (const [name, expected] of Object.entries(manifest.frameworkResources)) {
      const resource = join(output, "D3DMetal.framework/Versions/A/Resources", name);
      assert.equal(createHash("sha256").update(readFileSync(resource)).digest("hex"), expected, name);
    }
  });

  test("accepts the pristine local framework with explicit beta3 license documents", {
    skip: !process.env.YAAGL_AUTOPATCH_TEST_FRAMEWORK ||
      !process.env.YAAGL_AUTOPATCH_TEST_LICENSE ||
      !process.env.YAAGL_AUTOPATCH_TEST_ACKNOWLEDGEMENTS,
  }, () => {
    const source = process.env.YAAGL_AUTOPATCH_TEST_FRAMEWORK;
    const binary = join(source, "Versions/A/D3DMetal");
    const originalHash = createHash("sha256").update(readFileSync(binary)).digest("hex");
    const output = join(root, "prepared-framework");
    const result = spawnSync(executable, [
      "--accept-apple-license", "--output", output, "--framework", source,
      "--license", process.env.YAAGL_AUTOPATCH_TEST_LICENSE,
      "--acknowledgements", process.env.YAAGL_AUTOPATCH_TEST_ACKNOWLEDGEMENTS,
    ], { encoding: "utf8", env: nativeEnvironment });
    assert.equal(result.status, 0, result.stderr || result.stdout);
    assert.equal(createHash("sha256").update(readFileSync(binary)).digest("hex"), originalHash);
    const manifest = JSON.parse(readFileSync(join(output, "prepared-d3dmetal.json"), "utf8"));
    assert.equal(manifest.source.kind, "local-framework");
    assert.equal(manifest.framework.pristineD3DMetalSha256, originalHash);
    assert.equal(manifest.licenseAcceptance, "explicit-cli-flag");
  });
});
