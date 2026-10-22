import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const root = fileURLToPath(new URL("..", import.meta.url));
test("beta3 symbols, mixed ABI, native getters and allocator ownership", async () => {
  const output = mkdtempSync(join(tmpdir(), "yaagl-native-contract-"));
  try {
    const executable = join(output, "native-contract-test");
    const build = spawnSync("xcrun", ["clang++", "-arch", "x86_64", "-std=c++20",
      "-fno-objc-arc", "-fobjc-exceptions", "-fblocks", "-Wall", "-Wextra", "-Werror",
      "-framework", "Foundation", "-framework", "Metal",
      join(root, "scripts/native-contract-test.mm"), "-o", executable],
      { encoding: "utf8", timeout: 120000 });
    assert.equal(build.status, 0, build.stderr || build.error?.message);
    const image = process.env.YAAGL_D3DMETAL_IMAGE;
    const run = spawnSync(executable, image ? [image] : [], { encoding: "utf8", timeout: 30000 });
    assert.equal(run.status, 0, run.stdout + run.stderr);
    console.log(run.stdout.trim());
    if (process.env.YAAGL_NATIVE_LOAD_D3DMETAL) {
      const loadImage = realpathSync(process.env.YAAGL_NATIVE_LOAD_D3DMETAL);
      const ownedRun = process.env.RUN_TMP ? realpathSync(process.env.RUN_TMP) : null;
      assert.ok(loadImage.startsWith(realpathSync(root) + "/") ||
        (ownedRun && ownedRun !== "/" && loadImage.startsWith(ownedRun + "/")),
      "native execution is restricted to this worktree or explicit RUN_TMP, never an installed runtime");
      const pristineImage = realpathSync(process.env.YAAGL_PRISTINE_D3DMETAL ??
        join(root, "build/pristine-gptk4b3/lib/external/D3DMetal.framework/Versions/A/D3DMetal"));
      const { inspectD3DMetalSidecarPatch } = await import("./d3dmetal-sidecar-patch.mjs");
      assert.equal(inspectD3DMetalSidecarPatch(readFileSync(loadImage)).mode, "patched");
      assert.equal(inspectD3DMetalSidecarPatch(readFileSync(pristineImage)).mode, "original");
      const scratch = realpathSync(output);
      const home = join(scratch, "home");
      mkdirSync(home);
      // Native GetUserCachePath calls confstr, not getenv(TMPDIR). Confining
      // writes is required even with isolated HOME and compiler-cache variables.
      const profile = `(version 1) (allow default) (deny file-write*) ` +
        `(allow file-write* (subpath ${JSON.stringify(scratch)}) (literal "/dev/null"))`;
      const native = spawnSync("/usr/bin/sandbox-exec",
        ["-p", profile, executable, "--load", loadImage, pristineImage],
        { encoding: "utf8", timeout: 60000, cwd: scratch,
          env: { ...process.env, HOME: home, CFFIXED_USER_HOME: home,
            RUN_TMP: scratch, TMPDIR: scratch,
            NODE_COMPILE_CACHE: join(scratch, "node-cache"),
            BUN_RUNTIME_TRANSPILER_CACHE_PATH: join(scratch, "bun-cache"),
            MTL_SHADER_CACHE_DIR: join(scratch, "metal-cache") } });
      assert.equal(native.status, 0, native.stdout + native.stderr || native.error?.message);
      console.log(native.stdout.trim());
    } else {
      console.log("Native dylib execution not requested; set YAAGL_NATIVE_LOAD_D3DMETAL for the isolated constructor probe.");
    }
  } finally { rmSync(output, { recursive: true, force: true }); }
});

test("production video-memory hook preserves the erased Windows x64 ABI", () => {
  const bridge = readFileSync(join(root, "d3dmetal-sidecar/bridge.mm"), "utf8");
  const hook = bridge.match(/^std::int32_t\b[^\n]*\bqueryVideoMemoryInfo\([\s\S]*?^\}/m)?.[0];
  if (!hook) throw new Error("Cannot extract production queryVideoMemoryInfo");
  const output = mkdtempSync(join(tmpdir(), "yaagl-video-memory-abi-"));
  try {
    const source = join(output, "video-memory-abi.mm");
    const executable = join(output, "video-memory-abi");
    writeFileSync(source, `
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include "video-memory.hpp"
#include <algorithm>
#include <cassert>
#include <cstring>
#include <sys/sysctl.h>
#include <type_traits>

namespace yaagl::pso {
${hook}
}

using Info = yaagl::pso::video_memory::QueryVideoMemoryInfo;
using Query = std::int32_t (__attribute__((ms_abi)) *)(
    void*, std::uint32_t, std::uint32_t, Info*) noexcept;
// Type-check before erasing: the pre-fix SysV entry must fail deterministically.
static_assert(std::is_same_v<decltype(&yaagl::pso::queryVideoMemoryInfo), Query>);

int main() {
    @autoreleasepool {
        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        assert(device != nil);
        alignas(void*) unsigned char adapter[0x1f8]{};
        const std::uint64_t reservation = 0x12345678;
        std::memcpy(adapter + 0x18, &device, sizeof(device));
        std::memcpy(adapter + 0x1f0, &reservation, sizeof(reservation));

        std::uint64_t ram = 0;
        std::size_t size = sizeof(ram);
        assert(sysctlbyname("hw.memsize", &ram, &size, nullptr, 0) == 0);
        constexpr std::uint64_t reserve = 16ull << 30;
        const std::uint64_t graphics = std::min(
            ram / 5 * 4, std::max(ram > reserve ? ram - reserve : 0, ram / 2));
        const std::uint64_t recommended = [device recommendedMaxWorkingSetSize];
        const std::uint64_t budget = graphics ? std::min(recommended, graphics) : recommended;
        assert(budget > 0);
        const std::uint64_t usage = [device currentAllocatedSize];

        // Match the production hook table's erased address, retaining an indirect call.
        volatile std::uintptr_t entry =
            reinterpret_cast<std::uintptr_t>(&yaagl::pso::queryVideoMemoryInfo);
        const auto query = reinterpret_cast<Query>(entry);
        Info info;
        std::memset(&info, 0xa5, sizeof(info));
        assert(query(adapter, 0, 0, &info) == 0);
        assert(info.budget == budget);
        assert(info.currentUsage == usage);
        assert(info.availableForReservation == budget / 2);
        assert(info.currentReservation == reservation);
        std::uint64_t description[3]{};
        std::memcpy(description, adapter + 0x1c0, sizeof(description));
        assert(description[0] == budget && description[1] == 0);
        assert(description[2] == (ram > budget ? ram - budget : 0));
        std::uint64_t storedReservation = 0;
        std::memcpy(&storedReservation, adapter + 0x1f0, sizeof(storedReservation));
        assert(storedReservation == reservation);

        std::memset(&info, 0xa5, sizeof(info));
        assert(query(adapter, 0, 1, &info) == 0);
        assert(info.budget == 0 && info.currentUsage == 0);
        assert(info.availableForReservation == 0 && info.currentReservation == 0);
        assert(static_cast<std::uint32_t>(query(adapter, 0, 0, nullptr)) == 0x80070057u);
        [device release];
    }
}
`);
    const build = spawnSync("xcrun", ["clang++", "-arch", "x86_64", "-std=c++20",
      "-fno-objc-arc", "-fobjc-exceptions", "-fblocks", "-Wall", "-Wextra", "-Werror",
      "-framework", "Foundation", "-framework", "Metal",
      "-I", join(root, "d3dmetal-sidecar"),
      source, join(root, "d3dmetal-sidecar/video-memory.mm"), "-o", executable],
      { encoding: "utf8", timeout: 120000 });
    assert.equal(build.status, 0, build.stderr || build.error?.message);
    const run = spawnSync(executable, [], { encoding: "utf8", timeout: 30000 });
    assert.equal(run.status, 0, run.stdout + run.stderr || run.error?.message);
  } finally { rmSync(output, { recursive: true, force: true }); }
});
