#pragma once

#import <Foundation/Foundation.h>

#include <cstdint>
#include <functional>
#include <memory>
#include <span>

namespace yaagl::pso {

// The state argument is adopted at +1. Reflection and error are borrowed and
// retained by NativeResult. Copies own independent references to all objects.
class NativeResult final {
public:
    NativeResult() noexcept = default;
    NativeResult(id state, id reflection, NSError* error) noexcept;
    NativeResult(const NativeResult& other) noexcept;
    NativeResult(NativeResult&& other) noexcept;
    NativeResult& operator=(NativeResult&& other) noexcept;
    ~NativeResult();

    [[nodiscard]] id state() const noexcept { return state_; }
    [[nodiscard]] id reflection() const noexcept { return reflection_; }
    [[nodiscard]] NSError* error() const noexcept { return error_; }
    [[nodiscard]] bool hasError() const noexcept { return error_ != nil; }

    // Transfer this result's owned reference to the caller.
    [[nodiscard]] id takeState() noexcept;
    [[nodiscard]] id takeReflection() noexcept;
    [[nodiscard]] NSError* takeError() noexcept;

private:
    id state_ = nil;
    id reflection_ = nil;
    NSError* error_ = nil;
};

using CreateFunction = std::function<NativeResult()>;

class Cache final {
public:
    Cache();
    Cache(const Cache&) = delete;
    Cache& operator=(const Cache&) = delete;
    ~Cache();

    // keyResources is retained only when a new entry is created; its objects
    // stay alive with that in-flight or completed entry.
    [[nodiscard]] NativeResult getOrCreate(
        const void* device,
        std::span<const std::uint8_t> key,
        NSArray* keyResources,
        const CreateFunction& create);

    // Prevents cache admission for this address while releasing its current
    // scope and running action. The address can form a fresh scope afterward.
    void withDeviceRetired(
        const void* device,
        void (*action)(const void*, const void*),
        const void* context);

private:
    class Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace yaagl::pso
