#!/usr/bin/env python3
"""Exercise the production weak function cache/keys and execute cached Metal functions.

Native-owner byte records encode only the pinned fields read by function-hooks.mm;
functions, libraries, constant values, exceptions and GPU execution are real objects.
"""

import pathlib
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "d3dmetal-pso-cache"

NATIVE = r'''
#import "function-cache.hpp"
#import "function-hooks.hpp"
#import <mach/mach.h>
#include <algorithm>
#include <array>
#include <atomic>
#include <cassert>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <limits>
#include <mutex>
#include <pthread.h>
#include <stdexcept>
#include <thread>

using namespace yaagl::pso;
extern "C" id objc_storeWeakOrNil(id*, id);
extern "C" id objc_loadWeakRetained(id*);
extern "C" void objc_destroyWeak(id*);

struct Record {
    std::array<std::uint8_t, 0x300> bytes{};
    template<class T> void put(std::size_t offset, T value) {
        std::memcpy(bytes.data() + offset, &value, sizeof(value));
    }
    const void* data() const { return bytes.data(); }
};

struct Create {
    std::atomic<unsigned> calls{0};
    FunctionCache* cache = nullptr;
    const void* device = nullptr;
    KeyBytes* key = nullptr;
    id library = nil;
    MTLFunctionConstantValues* constants = nil;
    bool reenter = false;
    int failure = 0;
    std::mutex mutex;
    std::condition_variable ready;
    bool blocked = false;
    bool entered = false;
    bool released = false;

    static FunctionResult run(void* opaque) {
        auto& c = *static_cast<Create*>(opaque);
        ++c.calls;
        if (c.reenter) {
            c.reenter = false;
            auto nested = c.cache->getOrCreate(c.device, *c.key, c.library, run, &c);
            assert(nested.functions() != nil);
        }
        if (c.blocked) {
            std::unique_lock lock(c.mutex);
            c.entered = true;
            c.ready.notify_all();
            c.ready.wait(lock, [&] { return c.released; });
        }
        if (c.failure == 1) return FunctionResult(nil, false);
        if (c.failure == 2) throw std::runtime_error("native producer failure");
        if (c.failure == 3) @throw [NSException exceptionWithName:@"ExtractionFailure"
            reason:@"native producer failure" userInfo:nil];
        id function = nil;
        if ([c.library conformsToProtocol:@protocol(MTLLibrary)]) {
            NSError* error = nil;
            function = [c.library newFunctionWithName:@"cache_probe"
                constantValues:c.constants error:&error];
            assert(function != nil && error == nil);
        } else {
            function = [[NSObject alloc] init];
        }
        auto* functions = [[NSMutableArray alloc] initWithObjects:function, nil];
        [function release];
        return FunctionResult(functions, true);
    }
};

// Waiting state is observed only after each thread announces it is about to
// enter getOrCreate. It has no other blocking operation before that call.
// This proves overlap with the producer instead of depending on scheduling/sleeps.
void awaitWait(std::thread& thread, std::atomic<bool>& entered) {
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
    while (!entered.load(std::memory_order_acquire)) {
        assert(std::chrono::steady_clock::now() < deadline);
        std::this_thread::yield();
    }
    for (;;) {
        thread_basic_info_data_t info{};
        mach_msg_type_number_t count = THREAD_BASIC_INFO_COUNT;
        assert(thread_info(pthread_mach_thread_np(thread.native_handle()),
            THREAD_BASIC_INFO, reinterpret_cast<thread_info_t>(&info), &count) == KERN_SUCCESS);
        if (info.run_state == TH_STATE_WAITING) return;
        assert(std::chrono::steady_clock::now() < deadline);
        std::this_thread::yield();
    }
}

void concurrency(FunctionCache& cache, const void* device, KeyBytes& key, id library, int failure) {
    Create create;
    create.blocked = true;
    create.failure = failure;
    std::array<NSMutableArray*, 3> results{};
    std::array<int, 3> errors{};
    auto call = [&](int index) {
        @autoreleasepool {
            try {
                @try {
                    auto result = cache.getOrCreate(device, key, library, Create::run, &create);
                    results[index] = result.takeFunctions();
                } @catch (NSException* exception) {
                    assert([exception.name isEqualToString:@"ExtractionFailure"]);
                    errors[index] = 3;
                }
            } catch (const std::runtime_error& error) {
                assert(std::strcmp(error.what(), "native producer failure") == 0);
                errors[index] = 2;
            }
        }
    };
    std::thread producer([&] { call(0); });
    {
        std::unique_lock lock(create.mutex);
        create.ready.wait(lock, [&] { return create.entered; });
    }
    std::array<std::atomic<bool>, 2> entered{};
    std::thread first([&] { entered[0].store(true, std::memory_order_release); call(1); });
    std::thread second([&] { entered[1].store(true, std::memory_order_release); call(2); });
    awaitWait(first, entered[0]);
    awaitWait(second, entered[1]);
    assert(create.calls == 1);
    {
        std::lock_guard lock(create.mutex);
        create.released = true;
    }
    create.ready.notify_all();
    producer.join(); first.join(); second.join();
    assert(create.calls == 1);
    if (failure) {
        for (int error : errors) assert(error == failure);
        create.failure = 0;
        auto retry = cache.getOrCreate(device, key, library, Create::run, &create);
        assert(retry.functions() != nil && create.calls == 2);
    } else {
        assert(results[0] != results[1] && results[1] != results[2]);
        assert([results[0] objectAtIndex:0] == [results[1] objectAtIndex:0]);
        assert([results[0] objectAtIndex:0] == [results[2] objectAtIndex:0]);
        [results[1] removeAllObjects];
        assert([results[0] count] == 1 && [results[2] count] == 1);
    }
    for (auto result : results) [result release];
}

void cacheLifetime() {
    FunctionCache cache;
    NSObject* library = [[NSObject alloc] init];
    int device = 0;
    Record owner;
    owner.put(0x8, static_cast<const void*>(&device));
    KeyBytes key;
    const void* actualDevice = nullptr;
    assert(makeFunctionExtractionKey(library, 0, nullptr, nil, 0x10f0ed,
        {ContextKind::Compute, owner.data(), nullptr}, key, actualDevice));
    assert(actualDevice == &device);
    Create create;
    {
        auto first = cache.getOrCreate(&device, key, library, Create::run, &create);
        auto second = cache.getOrCreate(&device, key, library, Create::run, &create);
        assert(create.calls == 1 && first.functions() != second.functions());
        assert([first.functions() objectAtIndex:0] == [second.functions() objectAtIndex:0]);
        id weak = nil;
        assert(objc_storeWeakOrNil(&weak, [first.functions() objectAtIndex:0]) != nil);
        first = {}; second = {};
        assert(objc_loadWeakRetained(&weak) == nil);
        objc_destroyWeak(&weak);
    }
    auto recreated = cache.getOrCreate(&device, key, library, Create::run, &create);
    assert(create.calls == 2);
    struct Retirement { FunctionCache* cache; KeyBytes* key; id library; Create* create; } retirement{
        &cache, &key, library, &create};
    cache.withDeviceRetired(&device, [](const void* device, const void* opaque) {
        const auto& r = *static_cast<const Retirement*>(opaque);
        auto first = r.cache->getOrCreate(device, *r.key, r.library, Create::run, r.create);
        auto second = r.cache->getOrCreate(device, *r.key, r.library, Create::run, r.create);
        assert([first.functions() objectAtIndex:0] != [second.functions() objectAtIndex:0]);
    }, &retirement);
    auto fresh = cache.getOrCreate(&device, key, library, Create::run, &create);
    assert(create.calls == 5);
    assert([fresh.functions() objectAtIndex:0] != [recreated.functions() objectAtIndex:0]);
    fresh = {}; recreated = {};
    create.failure = 1;
    for (int i = 0; i < 2; ++i) {
        auto nilResult = cache.getOrCreate(&device, key, library, Create::run, &create);
        assert(nilResult.functions() == nil);
    }
    assert(create.calls == 7);
    create.failure = 0; create.reenter = true;
    create.cache = &cache; create.device = &device; create.key = &key; create.library = library;
    auto recursive = cache.getOrCreate(&device, key, library, Create::run, &create);
    assert(create.calls == 9 && recursive.functions() != nil);
    recursive = {};
    for (int failure : {0, 2, 3}) concurrency(cache, &device, key, library, failure);
    [library release];
}

void gpuKeys() {
    id<MTLDevice> device = MTLCreateSystemDefaultDevice();
    assert(device != nil);
    NSString* shader = @"#include <metal_stdlib>\nusing namespace metal;\n"
        "constant uint vertexOutputSize [[function_constant(0)]];\n"
        "constant float maxTessellationFactor [[function_constant(1)]];\n"
        "kernel void cache_probe(device uint* out [[buffer(0)]]) {"
        "out[0] = vertexOutputSize + uint(maxTessellationFactor); }";
    NSError* error = nil;
    id<MTLLibrary> library = [device newLibraryWithSource:shader options:nil error:&error];
    assert(library != nil && error == nil);
    id<MTLLibrary> otherLibrary = [device newLibraryWithSource:shader options:nil error:&error];
    assert(otherLibrary != nil && library != otherLibrary && error == nil);
    Record owner, otherOwner, stages, vertex, hull, domain;
    owner.put(0x8, static_cast<const void*>(device));
    otherOwner.put(0x8, static_cast<const void*>(device));
    owner.put(0x30, stages.data()); otherOwner.put(0x30, stages.data());
    stages.put(0x28, vertex.data()); stages.put(0x10, hull.data()); stages.put(0x18, domain.data());
    vertex.put(0x168, std::uint32_t(1)); vertex.put(0x128, std::uint32_t(7));
    hull.put(0x178, reinterpret_cast<std::uintptr_t>(library));
    float factor = 3.0f;
    owner.put(0x2b9, factor); otherOwner.put(0x2b9, factor);
    auto* constants = [[MTLFunctionConstantValues alloc] init];
    auto* equalConstants = [[MTLFunctionConstantValues alloc] init];
    std::uint32_t outputSize = 7;
    for (auto* values : {constants, equalConstants}) {
        [values setConstantValue:&outputSize type:MTLDataTypeUInt atIndex:0];
        [values setConstantValue:&factor type:MTLDataTypeFloat atIndex:1];
    }
    const void* reflection = hull.bytes.data() + 0x20;
    auto make = [&](id lib, const Record& own, MTLFunctionConstantValues* values, std::uintptr_t caller,
                    KeyBytes& key) {
        const void* actual = nullptr;
        return makeFunctionExtractionKey(lib, 0x100, reflection, values, caller,
            {ContextKind::Graphics, own.data(), stages.data()}, key, actual);
    };
    KeyBytes key, equalKey, changedKey, libraryKey, rejected;
    assert(make(library, owner, constants, 0x110987, key));
    assert(make(library, otherOwner, equalConstants, 0x110987, equalKey));
    assert(std::equal(key.begin(), key.end(), equalKey.begin(), equalKey.end()));
    assert(!make(library, owner, constants, 0x123f7d, rejected)); // RT provenance is never admitted.
    assert(!make(library, owner, constants, 0x1107d5, rejected)); // Unsupported constants site.
    FunctionCache cache;
    Create create;
    create.library = library; create.constants = constants;
    auto first = cache.getOrCreate(device, key, library, Create::run, &create);
    create.constants = equalConstants;
    auto equal = cache.getOrCreate(device, equalKey, library, Create::run, &create);
    assert(create.calls == 1);
    assert([first.functions() objectAtIndex:0] == [equal.functions() objectAtIndex:0]);
    factor = 4.0f; otherOwner.put(0x2b9, factor);
    [equalConstants setConstantValue:&factor type:MTLDataTypeFloat atIndex:1];
    assert(make(library, otherOwner, equalConstants, 0x110987, changedKey));
    assert(!std::equal(key.begin(), key.end(), changedKey.begin(), changedKey.end()));
    auto changed = cache.getOrCreate(device, changedKey, library, Create::run, &create);
    assert(create.calls == 2);
    assert([first.functions() objectAtIndex:0] != [changed.functions() objectAtIndex:0]);
    hull.put(0x178, reinterpret_cast<std::uintptr_t>(otherLibrary));
    assert(make(otherLibrary, otherOwner, equalConstants, 0x110987, libraryKey));
    create.library = otherLibrary;
    auto distinct = cache.getOrCreate(device, libraryKey, otherLibrary, Create::run, &create);
    assert(create.calls == 3);
    assert([distinct.functions() objectAtIndex:0] != [changed.functions() objectAtIndex:0]);
    id<MTLCommandQueue> queue = [device newCommandQueue];
    id<MTLBuffer> output = [device newBufferWithLength:sizeof(std::uint32_t) options:MTLResourceStorageModeShared];
    unsigned expected = 10;
    for (NSMutableArray* functions : {first.functions(), equal.functions(), changed.functions(), distinct.functions()}) {
        id<MTLComputePipelineState> pipeline = [device newComputePipelineStateWithFunction:
            [functions objectAtIndex:0] error:&error];
        assert(pipeline != nil && error == nil);
        id<MTLCommandBuffer> commands = [queue commandBuffer];
        id<MTLComputeCommandEncoder> encoder = [commands computeCommandEncoder];
        [encoder setComputePipelineState:pipeline];
        [encoder setBuffer:output offset:0 atIndex:0];
        [encoder dispatchThreads:MTLSizeMake(1, 1, 1) threadsPerThreadgroup:MTLSizeMake(1, 1, 1)];
        [encoder endEncoding]; [commands commit]; [commands waitUntilCompleted];
        assert(commands.status == MTLCommandBufferStatusCompleted && commands.error == nil);
        assert(*static_cast<std::uint32_t*>(output.contents) == expected);
        if (functions == equal.functions()) expected = 11;
        [pipeline release];
    }
    [output release]; [queue release];
    [constants release]; [equalConstants release];
    [otherLibrary release]; [library release]; [device release];
}

int main() {
    @autoreleasepool {
        cacheLifetime();
        gpuKeys();
        puts("function cache lifetime/key/failure/reentry/concurrency and real Metal GPU smoke passed");
    }
}
'''


def main():
    # Link the exact production append body without pulling in unrelated PSO
    # provenance hooks or requiring the generated sidecar layout header.
    key_source = (SOURCE / "key.mm").read_text()
    start = key_source.index("void KeyBytes::append(")
    append = key_source[start:key_source.index("\nKey::~Key()", start)]
    with tempfile.TemporaryDirectory(prefix="function-cache-test-") as directory:
        source = pathlib.Path(directory) / "test.mm"
        binary = pathlib.Path(directory) / "test"
        source.write_text(NATIVE + "\nnamespace yaagl::pso {\n" + append + "\n}\n")
        subprocess.run(("xcrun", "clang++", "-std=c++20", "-fno-objc-arc", "-pthread",
                        "-I", str(SOURCE), str(source), str(SOURCE / "function-cache.mm"),
                        str(SOURCE / "function-hooks.mm"), "-framework", "Foundation",
                        "-framework", "Metal", "-o", str(binary)), check=True, timeout=60)
        subprocess.run((str(binary),), check=True, timeout=30)


if __name__ == "__main__":
    main()
