import assert from "node:assert/strict";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import test from "node:test";

const script = fileURLToPath(new URL("./prepare-d3dmetal-runtime.mjs", import.meta.url));

test("requires explicit Apple license acceptance before creating output", () => {
  const root = mkdtempSync(join(tmpdir(), "prepare-d3dmetal-license-"));
  const output = join(root, "D3DMetal");
  try {
    const result = spawnSync(process.execPath, [script, "--output", output], {
      encoding: "utf8",
    });
    assert.equal(result.status, 1);
    assert.equal(existsSync(output), false);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
