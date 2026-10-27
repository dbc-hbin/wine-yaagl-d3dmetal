import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const root = fileURLToPath(new URL("..", import.meta.url));
test("native low-latency admission state", () => {
  const directory = mkdtempSync(join(tmpdir(), "yaagl-low-latency-"));
  try {
    const executable = join(directory, "low-latency-test");
    const build = spawnSync("xcrun", ["clang++", "-arch", "x86_64", "-std=c++20",
      "-pthread", "-Wall", "-Wextra", "-Werror", "-Wl,-adhoc_codesign",
      join(root, "scripts/native-low-latency-test.cpp"), "-o", executable],
    { encoding: "utf8", timeout: 120000 });
    assert.equal(build.status, 0, build.stderr || build.error?.message);
    const run = spawnSync(executable, [], { encoding: "utf8", timeout: 30000 });
    assert.equal(run.status, 0, run.stdout + run.stderr || run.error?.message);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
