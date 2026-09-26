import assert from "node:assert/strict";
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
});
