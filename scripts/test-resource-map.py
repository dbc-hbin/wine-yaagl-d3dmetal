#!/usr/bin/env python3
"""Compile and exercise the production resource-map orchestration with a
scripted native boundary; no D3DMetal framework, GPU, or installed runtime.
"""

import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HARNESS = r'''
#include "d3dmetal-transport.hpp"
#include <cassert>
#include <cstdint>
#include <initializer_list>
#include <stdexcept>

using namespace yaagl::pso::d3dmetal;

struct Native {
    enum class Failure { none, acquireEmpty, acquireThrow, flagsThrow,
                         textureEmpty, textureThrow, viewInvalid, viewThrow,
                         retainInvalid, retainThrow };
    Failure failure = Failure::none;
    std::uint32_t resourceFlags = 4;
    int acquisitions = 0;
    int ownerReleases = 0;
    int textureQueries = 0;
    int viewQueries = 0;
    int retains = 0;
    int metalReleases = 0;
    int metalReferences = 0;
    int bufferQueries = 0;
    int bufferRetains = 0;
    int bufferReleases = 0;
    std::uint8_t ownerObject = 0;
    std::uint8_t metalObject = 0;
    std::uint8_t bufferObject = 0;
    bool bufferMode = false;

    bool acquire(void*, void*& owner) {
        ++acquisitions;
        if (failure == Failure::acquireEmpty) return false;
        owner = &ownerObject;
        if (failure == Failure::acquireThrow) throw std::runtime_error("owner acquired");
        return true;
    }
    std::uint32_t flags(void* owner) {
        assert(owner == &ownerObject);
        if (failure == Failure::flagsThrow) throw std::runtime_error("descriptor");
        return resourceFlags;
    }
    void* texture(void* owner) {
        assert(owner == &ownerObject);
        ++textureQueries;
        if (failure == Failure::textureThrow) throw std::runtime_error("metal extraction");
        if (bufferMode) return nullptr;
        return failure == Failure::textureEmpty ? nullptr : &metalObject;
    }
    void* buffer(void* owner, std::size_t& offset) {
        assert(owner == &ownerObject);
        ++bufferQueries;
        offset = 4;
        return bufferMode ? &bufferObject : nullptr;
    }
    bool view(void* metal, TextureView& out) {
        assert(metal == &metalObject);
        ++viewQueries;
        if (failure == Failure::viewThrow) throw std::runtime_error("view");
        out.firstMip = 2;
        out.planes = 2;
        return failure != Failure::viewInvalid;
    }
    bool retain(void* metal) {
        assert(metal == &metalObject);
        if (failure == Failure::retainThrow) throw std::runtime_error("retain");
        if (failure == Failure::retainInvalid) return false;
        ++retains;
        ++metalReferences;
        return true;
    }
    void releaseInternal(void* owner) noexcept {
        assert(owner == &ownerObject);
        ++ownerReleases;
    }
    void releaseMetal(void* metal) noexcept {
        assert(metal == &metalObject);
        ++metalReleases;
        assert(--metalReferences >= 0);
    }
    bool retainBuffer(void* buffer) {
        assert(buffer == &bufferObject);
        ++bufferRetains;
        return true;
    }
    void releaseBuffer(void* buffer) noexcept {
        assert(buffer == &bufferObject);
        ++bufferReleases;
    }
};

int main() {
    Native native;
    MetalResource out{};
    auto map = [&](void* resource, ResourceAccess access) {
        return detail::mapResourceNative(resource, out, access, native);
    };
    const void* resource = &native.ownerObject;

    assert(map(nullptr, ResourceAccess::read) == ResourceMapResult::metadataUnavailable);
    assert(native.acquisitions == 0 && out.texture == nullptr);
    native.failure = Native::Failure::acquireEmpty;
    assert(map(const_cast<void*>(resource), ResourceAccess::write) == ResourceMapResult::metadataUnavailable);
    assert(native.acquisitions == 1 && native.ownerReleases == 0);
    native.failure = Native::Failure::acquireThrow;
    assert(map(const_cast<void*>(resource), ResourceAccess::write) == ResourceMapResult::metadataUnavailable);
    assert(native.acquisitions == 2 && native.ownerReleases == 1 && out.texture == nullptr);
    native.failure = Native::Failure::flagsThrow;
    assert(map(const_cast<void*>(resource), ResourceAccess::write) == ResourceMapResult::metadataUnavailable);
    assert(native.ownerReleases == 2 && native.textureQueries == 0);

    native.failure = Native::Failure::textureEmpty;
    native.resourceFlags = 0;
    assert(map(const_cast<void*>(resource), ResourceAccess::write) == ResourceMapResult::notUnorderedAccess);
    assert(native.ownerReleases == 3 && native.textureQueries == 0 && out.texture == nullptr);
    native.failure = Native::Failure::none;
    assert(map(const_cast<void*>(resource), ResourceAccess::read) == ResourceMapResult::mapped);
    assert(native.acquisitions == 5 && native.ownerReleases == 4 && native.textureQueries == 1);
    assert(out.texture == &native.metalObject && out.view.firstMip == 2 && out.view.planes == 2);
    assert(native.retains == 1 && native.metalReferences == 1);

    // A later map releases its prior retained view before a rejected output.
    assert(map(const_cast<void*>(resource), ResourceAccess::write) == ResourceMapResult::notUnorderedAccess);
    assert(native.metalReleases == 1 && native.metalReferences == 0 && out.texture == nullptr);
    assert(out.view.firstMip == 0 && native.textureQueries == 1 && native.ownerReleases == 5);

    native.resourceFlags = 4;
    for (auto failure : {Native::Failure::textureEmpty, Native::Failure::textureThrow,
                         Native::Failure::viewInvalid, Native::Failure::viewThrow,
                         Native::Failure::retainInvalid, Native::Failure::retainThrow}) {
        native.failure = failure;
        const int releases = native.ownerReleases;
        const int retains = native.retains;
        assert(map(const_cast<void*>(resource), ResourceAccess::write) == ResourceMapResult::mapFailed);
        assert(native.ownerReleases == releases + 1 && native.retains == retains);
        assert(native.metalReferences == 0 && out.texture == nullptr);
    }
    native.failure = Native::Failure::none;
    assert(map(const_cast<void*>(resource), ResourceAccess::write) == ResourceMapResult::mapped);
    assert(native.metalReferences == 1 && native.retains == 2);
    native.releaseMetal(out.texture);
    assert(native.metalReferences == 0 && native.metalReleases == 2);
    out = {};

    native.bufferQueries = native.bufferRetains = native.bufferReleases = 0;
    native.bufferMode = true;
    assert(map(const_cast<void*>(resource), ResourceAccess::write) == ResourceMapResult::mapped);
    assert(out.texture == nullptr && out.buffer == &native.bufferObject &&
           out.bufferOffset == 4 && native.bufferQueries == 1 &&
           native.bufferRetains == 1);
    native.bufferMode = false;
    assert(map(const_cast<void*>(resource), ResourceAccess::read) == ResourceMapResult::mapped);
    assert(out.buffer == nullptr && native.bufferReleases == 1 &&
           native.metalReferences == 1);
    native.releaseMetal(out.texture);
}
'''


class ResourceMapTest(unittest.TestCase):
    def test_native_mapping(self):
        with tempfile.TemporaryDirectory(prefix="yaagl-resource-map-") as directory:
            source = pathlib.Path(directory) / "resource-map.cpp"
            executable = pathlib.Path(directory) / "resource-map"
            source.write_text(HARNESS)
            compiler = subprocess.check_output(["xcrun", "--find", "clang++"], text=True).strip()
            sdk = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
            subprocess.run([compiler, "-std=c++20", "-isysroot", sdk, "-Wall", "-Wextra", "-Werror",
                            "-I", str(ROOT / "d3dmetal-sidecar"), str(source),
                            "-o", str(executable)], check=True)
            subprocess.run([str(executable)], check=True)


if __name__ == "__main__":
    unittest.main()
