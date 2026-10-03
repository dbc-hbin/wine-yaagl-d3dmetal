#pragma once

#import <Foundation/Foundation.h>

#include <cstdint>
#include <memory>
#include <span>

namespace yaagl::pso {

// functions is adopted at +1. A reusable result is recorded by the cache as
// weak references only; the original caller still receives its original
// mutable container.
class FunctionResult final {
public:
    FunctionResult() noexcept = default;
    FunctionResult(NSMutableArray* functions, bool reusable) noexcept;
    FunctionResult(const FunctionResult&) = delete;
    FunctionResult& operator=(const FunctionResult&) = delete;
    FunctionResult(FunctionResult&& other) noexcept;
    FunctionResult& operator=(FunctionResult&& other) noexcept;
    ~FunctionResult();

    [[nodiscard]] NSMutableArray* functions() const noexcept { return functions_; }
    [[nodiscard]] bool reusable() const noexcept { return reusable_; }
    [[nodiscard]] NSMutableArray* takeFunctions() noexcept;

private:
    NSMutableArray* functions_ = nil;
    bool reusable_ = false;
};

using FunctionCreate = FunctionResult (*)(void* context);

class FunctionCache final {
public:
    FunctionCache();
    FunctionCache(const FunctionCache&) = delete;
    FunctionCache& operator=(const FunctionCache&) = delete;
    ~FunctionCache();

    // library is retained only while a newly admitted entry is in flight. A
    // completed entry keeps weak references to the library and each function,
    // and a hit returns a new array of those objects only while all are alive.
    // Key bytes embed the library address, but a reused address can only hit an
    // entry whose weak library slot is nil, so it is discarded and recreated.
    [[nodiscard]] FunctionResult getOrCreate(
        const void* device,
        std::span<const std::uint8_t> key,
        id library,
        FunctionCreate create,
        void* context);

    // Cache admission for device is blocked until detached entries have been
    // released and action returns. The same address starts fresh afterward.
    void withDeviceRetired(
        const void* device,
        void (*action)(const void*, const void*),
        const void* context);

private:
    class Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace yaagl::pso
