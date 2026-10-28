import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
assert.equal(process.platform, 'darwin', 'Native Metal queue verification requires macOS');
const temporary = mkdtempSync(join(tmpdir(), 'yaagl-metal-queue-lifetime-'));
const executable = join(temporary, 'queue-lifetime');
const env = { ...process.env, RUN_TMP: temporary, TMPDIR: temporary,
  NODE_COMPILE_CACHE: join(temporary, 'node-cache'),
  BUN_RUNTIME_TRANSPILER_CACHE_PATH: join(temporary, 'bun-cache') };
function run(command, args) {
  const result = spawnSync(command, args, { cwd: root, env, encoding: 'utf8', timeout: 180_000 });
  if (result.stdout) process.stdout.write(result.stdout);
  if (result.stderr) process.stderr.write(result.stderr);
  assert.ifError(result.error);
  assert.equal(result.status, 0, result.signal ?? command);
}
try {
  run('xcrun', ['clang++', '-arch', 'x86_64', '-std=c++20', '-fblocks', '-fno-objc-arc',
    '-mmacosx-version-min=27.0', '-Wall', '-Wextra', '-Werror', '-O1', '-g',
    '-I', 'd3dmetal-sidecar', 'scripts/test-metal-queue-lifetime.mm',
    'd3dmetal-sidecar/metalfx-lifetime.mm', '-framework', 'Foundation',
    '-framework', 'Metal', '-framework', 'MetalFX', '-o', executable]);
  run(executable, ['reject']);
  run(executable, ['repair']);
} finally {
  rmSync(temporary, { recursive: true, force: false });
}
