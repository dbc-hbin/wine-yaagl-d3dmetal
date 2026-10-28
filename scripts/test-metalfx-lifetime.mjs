import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const receiptRoot = join(root, 'build/hud-memory-accounting/native-leak-fix/metalfx');

const cases = [
  ['fg', 'factory'], ['fg', 'encode'], ['fg', 'external-history'],
  ['fg', 'multi-queue-replay'], ['fg', 'fail-owner'], ['fg', 'fail-implementation'],
  ['bbr', 'factory'], ['bbr', 'encode'], ['bbr', 'async-drop'],
  ['bbr', 'prewarm-group'], ['bbr', 'multi-queue-replay'], ['bbr', 'release-before-submit'],
  ['bbr', 'fail-owner'], ['bbr', 'fail-bbr-nil'], ['bbr', 'fail-bbr-throw'],
];
const requested = process.argv.slice(2);
assert(requested.length === 0 || requested.length === 2,
  'Usage: node scripts/test-metalfx-lifetime.mjs [fg|bbr scenario]');
const selected = requested.length ? cases.filter(value => value.every((part, n) => part === requested[n])) : cases;
assert(selected.length > 0, 'Unknown effect/scenario pair');
assert.equal(process.platform, 'darwin', 'Native MetalFX verification requires macOS');

mkdirSync(receiptRoot, { recursive: true });
const temporary = mkdtempSync(join(tmpdir(), 'yaagl-metalfx-lifetime-test-'));
const executable = join(temporary, 'test-metalfx-lifetime');
const env = { ...process.env, RUN_TMP: temporary, TMPDIR: temporary,
  NODE_COMPILE_CACHE: join(temporary, 'node-cache'),
  BUN_RUNTIME_TRANSPILER_CACHE_PATH: join(temporary, 'bun-cache') };
const results = [];
const receipt = { schema: 1, architecture: 'x86_64/Rosetta',
  authentication: 'Production exact-image and ownership-contract checks execute before every repair scenario',
  temporaryRoot: temporary, results, cleaned: false };

function run(label, command, args, timeout) {
  const result = spawnSync(command, args, { cwd: root, env, encoding: 'utf8', timeout, maxBuffer: 16 * 1024 * 1024 });
  writeFileSync(join(receiptRoot, `${label}.stdout`), result.stdout ?? '');
  writeFileSync(join(receiptRoot, `${label}.stderr`), result.stderr ?? '');
  assert.ifError(result.error);
  assert.equal(result.status, 0, `${label}: ${result.signal ?? result.stderr}`);
  return result.stdout;
}

try {
  run('compile', 'xcrun', ['clang++', '-arch', 'x86_64', '-std=c++20', '-fblocks', '-fno-objc-arc',
    '-mmacosx-version-min=27.0', '-Wall', '-Wextra', '-Werror', '-O1', '-g', '-framework', 'Foundation', '-framework', 'Metal',
    '-framework', 'MetalFX', join(root, 'scripts/test-metalfx-lifetime.mm'), '-o', executable], 180_000);
  const gate = JSON.parse(run('gate-rejection', executable, ['reject'], 30_000).trim());
  assert.equal(gate.gateRejected, true);
  assert.equal(gate.unmodifiedOtherMethods, true);
  receipt.gateRejected = true;
  for (const [effect, scenario] of selected) {
    const pair = [];
    for (const mode of ['baseline', 'repair']) {
      const stdout = run(`${effect}-${scenario}-${mode}`, executable, [mode, effect, scenario], 180_000);
      const records = stdout.split('\n').filter(line => line.startsWith('{')).map(line => JSON.parse(line));
      const record = records.find(value => value.mode === mode && value.scenario === scenario);
      assert(record, `Missing result for ${mode} ${effect} ${scenario}`);
      results.push(record); pair.push(record);
      console.log(JSON.stringify(record));
    }
    assert.equal(pair[0].effectsCreated, pair[1].effectsCreated, 'Factory coverage changed');
    assert.equal(pair[0].residenciesCreated, pair[1].residenciesCreated, 'Native factory allocation coverage changed');
    assert.equal(pair[0].outputSha256, pair[1].outputSha256, `${effect} ${scenario}: output changed under ownership repair`);
  }
} finally {
  rmSync(temporary, { recursive: true, force: false });
  receipt.cleaned = true;
  writeFileSync(join(receiptRoot, requested.length ? `${requested.join('-')}-results.json` : 'native-results.json'),
    `${JSON.stringify(receipt, null, 2)}\n`);
}
