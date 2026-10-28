#import <Foundation/Foundation.h>

#include <Block.h>
#include <dispatch/dispatch.h>
#include <os/lock.h>

#include <atomic>
#include <cstddef>
#include <cstring>
#include <limits>
#include <memory>
#include <new>

#include "fence-lifetime.hpp"
#include "layout.hpp"
#include "../include/yaagl_d3dmetal_abi.hpp"

namespace yaagl::pso::fence_lifetime {
namespace {

namespace abi = layout::fence;
using Handler = void (^)(id, std::uint64_t);
using PublicSingle = std::int32_t (__attribute__((ms_abi)) *)(void*, std::uint64_t, void*);
using Multiple = std::int32_t (__attribute__((ms_abi)) *)(
    void*, void* const*, const std::uint64_t*, std::uint32_t, std::uint32_t, void*);
using AddHandler = void (*)(void*, std::uint64_t, Handler);
using EventFunction = void (*)(const void*);

const std::uint8_t* imageBase = nullptr;
Hooks original{};
constexpr std::int32_t kOutOfMemory = static_cast<std::int32_t>(0x8007000e);
constexpr std::int32_t kFailure = static_cast<std::int32_t>(0x80004005);

template<class T> T imageFunction(std::uintptr_t offset) noexcept {
    return reinterpret_cast<T>(const_cast<std::uint8_t*>(imageBase) + offset);
}

template<class T> T eventFunction(void* provider, std::size_t offset) noexcept {
    const auto* table = native::readObject<const std::uint8_t*>(provider);
    return native::readObject<T>(table + offset);
}

// Both the native pending vector and allocate_shared's control block use the
// authenticated image's operator new/operator delete pair. No process allocator
// interception or allocation-size classification is involved.
template<class T> struct NativeAllocator {
    using value_type = T;
    NativeAllocator() = default;
    template<class U> NativeAllocator(const NativeAllocator<U>&) noexcept {}
    T* allocate(std::size_t count) {
        if (count > std::numeric_limits<std::size_t>::max() / sizeof(T))
            throw std::bad_alloc();
        return static_cast<T*>(imageFunction<void* (*)(std::size_t)>(abi::kAllocate)(
            count * sizeof(T)));
    }
    void deallocate(T* pointer, std::size_t) noexcept {
        imageFunction<void (*)(void*)>(abi::kDeallocate)(pointer);
    }
    template<class U> bool operator==(const NativeAllocator<U>&) const noexcept { return true; }
};

struct EventPayload {
    void* provider;
    void* handle;
};
static_assert(sizeof(EventPayload) == 16);

void signalAndClose(const void* data) {
    const auto payload = native::readObject<EventPayload>(data);
    eventFunction<void (*)(void*, void*)>(payload.provider, abi::kSetEvent)(
        payload.provider, payload.handle);
    eventFunction<void (*)(void*, void*)>(payload.provider, abi::kCloseEvent)(
        payload.provider, payload.handle);
}

void closeOnly(const void* data) {
    const auto payload = native::readObject<EventPayload>(data);
    eventFunction<void (*)(void*, void*)>(payload.provider, abi::kCloseEvent)(
        payload.provider, payload.handle);
}

struct Completion {
    // Low 32 bits count successful inputs, including repeated fence inputs.
    // Commit prevents an immediate/ANY callback from signaling before a later
    // registration failure has had an opportunity to cancel the whole request.
    static constexpr std::uint64_t kCommitted = 1ull << 32;
    static constexpr std::uint64_t kCancelled = 1ull << 33;
    static constexpr std::uint64_t kFinished = 1ull << 34;
    std::atomic<std::uint64_t> progress{0};
    const std::uint32_t threshold;
    EventPayload payload{};
    bool duplicated = false;
    dispatch_semaphore_t semaphore = nullptr;

    Completion(std::uint32_t target, void* handle) : threshold(target) {
        if (!handle) {
            semaphore = imageFunction<dispatch_semaphore_t (*)(long)>(
                abi::kSemaphoreCreate)(0);
            if (!semaphore) throw std::bad_alloc();
            return;
        }
        payload.provider = native::readObject<void*>(imageBase + abi::kEventInterface);
        payload.handle = handle;
        const auto version = eventFunction<std::uint64_t (*)(void*)>(
            payload.provider, abi::kEventVersion)(payload.provider);
        if (version >= 3) {
            payload.handle = eventFunction<void* (*)(void*, void*)>(
                payload.provider, abi::kDuplicateEvent)(payload.provider, handle);
            if (!payload.handle || payload.handle == reinterpret_cast<void*>(-1))
                throw std::bad_alloc();
            duplicated = true;
        }
    }

    ~Completion() {
        cancel();
        if (semaphore) imageFunction<void (*)(dispatch_semaphore_t)>(
            abi::kDispatchRelease)(semaphore);
    }

    void consume(bool success) noexcept {
        if (semaphore) {
            if (success) imageFunction<long (*)(dispatch_semaphore_t)>(
                abi::kSemaphoreSignal)(semaphore);
        } else if (duplicated) {
            // Event v3 copies these 16 bytes before returning (the original
            // callbacks marshal an 8-byte handle the same way). Both success
            // and disposal therefore run on the provider's supported thread.
            eventFunction<void (*)(void*, EventFunction, bool, const void*, std::uint32_t)>(
                payload.provider, abi::kDispatchFunction)(
                payload.provider, success ? &signalAndClose : &closeOnly,
                false, &payload, sizeof(payload));
        } else if (success) {
            // The pre-v3 contract borrows the caller's event; its SetEvent
            // implementation owns any marshalling it requires.
            eventFunction<void (*)(void*, void*)>(payload.provider, abi::kSetEvent)(
                payload.provider, payload.handle);
        }
    }

    void finish(std::uint64_t observed) noexcept {
        while ((observed & kCommitted) && !(observed & (kCancelled | kFinished)) &&
               static_cast<std::uint32_t>(observed) >= threshold) {
            if (progress.compare_exchange_weak(observed, observed | kFinished,
                    std::memory_order_acq_rel, std::memory_order_acquire)) {
                consume(true);
                return;
            }
        }
    }

    void arrive() noexcept {
        finish(progress.fetch_add(1, std::memory_order_acq_rel) + 1);
    }

    void commit() noexcept {
        finish(progress.fetch_or(kCommitted, std::memory_order_acq_rel) | kCommitted);
    }

    void cancel() noexcept {
        const auto previous = progress.fetch_or(kCancelled | kFinished, std::memory_order_acq_rel);
        if (!(previous & kFinished)) consume(false);
    }
};

struct PendingCPUEvent {
    std::uint64_t value;
    Handler handler;
};
struct PendingVector {
    PendingCPUEvent* begin;
    PendingCPUEvent* end;
    PendingCPUEvent* capacity;
};
static_assert(sizeof(PendingCPUEvent) == 16);
static_assert(offsetof(PendingCPUEvent, handler) == 8);
static_assert(sizeof(PendingVector) == 24);
static_assert(sizeof(os_unfair_lock) == 4);
static_assert(sizeof(void*) == 8);

class FenceLock {
    os_unfair_lock* lock;
public:
    explicit FenceLock(std::uint8_t* fence, std::size_t offset)
        : lock(reinterpret_cast<os_unfair_lock*>(fence + offset)) {
        imageFunction<void (*)(os_unfair_lock*, std::uint32_t)>(abi::kLock)(
            lock, abi::kLockOptions);
    }
    ~FenceLock() { imageFunction<void (*)(os_unfair_lock*)>(abi::kUnlock)(lock); }
    FenceLock(const FenceLock&) = delete;
    FenceLock& operator=(const FenceLock&) = delete;
};

void addHandler(void* self, std::uint64_t value, Handler handler) {
    if (native::readObject<std::uint8_t>(imageBase + abi::kUseSharedEvents) == 1) {
        // Metal owns the copied notification block in this branch. Preserve
        // its listener/queue ordering and its event lifetime without a map.
        imageFunction<AddHandler>(abi::kAddHandler)(self, value, handler);
        return;
    }
    auto* fence = static_cast<std::uint8_t*>(self);
    auto& completed = *reinterpret_cast<std::uint64_t*>(fence + abi::kCompletedValue);
    if (std::atomic_ref<std::uint64_t>(completed).load(std::memory_order_acquire) >= value) {
        handler(nil, value);
        return;
    }
    FenceLock signalLock(fence, abi::kSignalLock);
    FenceLock handlerLock(fence, abi::kHandlerLock);
    if (std::atomic_ref<std::uint64_t>(completed).load(std::memory_order_acquire) >= value) {
        handler(nil, value);
        return;
    }
    auto pending = native::readObject<PendingVector>(fence + abi::kPendingVector);
    if (pending.end == pending.capacity) {
        const auto count = pending.begin ? static_cast<std::size_t>(pending.end - pending.begin) : 0;
        const auto capacity = pending.begin ?
            static_cast<std::size_t>(pending.capacity - pending.begin) : 0;
        const auto maximum = static_cast<std::size_t>(PTRDIFF_MAX) / sizeof(PendingCPUEvent);
        if (count == maximum) throw std::bad_alloc();
        const auto grown = capacity > maximum / 2 ? maximum : capacity ? capacity * 2 : 1;
        auto* allocation = NativeAllocator<PendingCPUEvent>{}.allocate(grown);
        if (count) std::memcpy(allocation, pending.begin, count * sizeof(PendingCPUEvent));
        auto* previous = pending.begin;
        pending = {allocation, allocation + count, allocation + grown};
        std::memcpy(fence + abi::kPendingVector, &pending, sizeof(pending));
        if (previous) NativeAllocator<PendingCPUEvent>{}.deallocate(previous, capacity);
    }
    // Reserve before copying. The original copies first, so a vector growth
    // exception loses that copy while also leaving both native locks held.
    Handler copy = Block_copy(handler);
    if (!copy) throw std::bad_alloc();
    *pending.end++ = {value, copy};
    std::memcpy(fence + abi::kPendingVector, &pending, sizeof(pending));
    auto maximum = native::readObject<std::uint64_t>(fence + abi::kMaximumPendingValue);
    if (maximum < value) std::memcpy(fence + abi::kMaximumPendingValue, &value, sizeof(value));
}

std::int32_t single(void* self, std::uint64_t value, void* event) {
    std::shared_ptr<Completion> completion;
    try {
        @try {
            const auto completed = imageFunction<std::uint64_t (*)(void*)>(
                abi::kGetCompletedValue)(self);
            if (completed >= value) {
                void* provider = native::readObject<void*>(imageBase + abi::kEventInterface);
                eventFunction<void (*)(void*, void*)>(provider, abi::kSetEvent)(provider, event);
                return 0;
            }
            completion = std::allocate_shared<Completion>(NativeAllocator<Completion>{}, 1, event);
            addHandler(self, value, ^(id, std::uint64_t) { completion->arrive(); });
            completion->commit();
            return 0;
        } @catch (id exception) {
            static_cast<void>(exception);
            if (completion) completion->cancel();
            return kFailure;
        }
    } catch (const std::bad_alloc&) {
        if (completion) completion->cancel();
        return kOutOfMemory;
    } catch (...) {
        if (completion) completion->cancel();
        return kFailure;
    }
}

template<std::uintptr_t Hooks::* Original, std::size_t Offset>
std::int32_t __attribute__((ms_abi)) publicSingle(void* self, std::uint64_t value, void* event) {
    if (!event) return reinterpret_cast<PublicSingle>(original.*Original)(self, value, event);
    return single(static_cast<std::uint8_t*>(self) + Offset, value, event);
}

struct CompletionParams {
    void* self;
    std::uint64_t value;
    void* event;
    std::int32_t result;
};
static_assert(offsetof(CompletionParams, value) == 8);
static_assert(offsetof(CompletionParams, event) == 16);
static_assert(offsetof(CompletionParams, result) == 24);
static_assert(sizeof(CompletionParams) == 32);

template<std::uintptr_t Hooks::* Original, std::size_t Offset>
std::int32_t unpackSingle(void* parameters) {
    const auto params = native::readObject<CompletionParams>(parameters);
    if (!params.event) return reinterpret_cast<std::int32_t (*)(void*)>(
        original.*Original)(parameters);
    const auto result = single(static_cast<std::uint8_t*>(params.self) + Offset,
        params.value, params.event);
    std::memcpy(static_cast<std::uint8_t*>(parameters) + offsetof(CompletionParams, result),
        &result, sizeof(result));
    // Unpack returns transport status separately from the API's HRESULT.
    return 0;
}

std::int32_t __attribute__((ms_abi)) multiple(void* device, void* const* fences,
    const std::uint64_t* values, std::uint32_t count, std::uint32_t flags, void* event) {
    // Zero inputs remain the native boundary's out-of-contract case.
    if (!count) return reinterpret_cast<Multiple>(original.multiple)(
        device, fences, values, count, flags, event);
    std::shared_ptr<Completion> completion;
    try {
        @try {
            completion = std::allocate_shared<Completion>(NativeAllocator<Completion>{},
                flags == 1 ? 1 : count, event);
            const void* fenceInterface = native::readObject<void*>(imageBase + abi::kFenceInterface);
            for (std::uint32_t index = 0; index != count; ++index) {
                // This is the complete native multiple-fence body's SysV
                // QueryInterface conversion, not an ID3D12Fence object cast.
                void* unknown = native::readObject<void*>(
                    static_cast<std::uint8_t*>(fences[index]) - abi::kUnknownPrefix);
                const auto* table = native::readObject<const std::uint8_t*>(unknown);
                auto query = native::readObject<void* (*)(void*, const void*)>(
                    table + abi::kQueryInterface);
                auto* wrapper = static_cast<std::uint8_t*>(query(unknown, fenceInterface));
                addHandler(wrapper + abi::kNativeFence, values[index],
                    ^(id, std::uint64_t) { completion->arrive(); });
            }
            completion->commit();
            if (!event) imageFunction<long (*)(dispatch_semaphore_t, dispatch_time_t)>(
                abi::kSemaphoreWait)(completion->semaphore, DISPATCH_TIME_FOREVER);
            return 0;
        } @catch (id exception) {
            static_cast<void>(exception);
            if (completion) completion->cancel();
            return kFailure;
        }
    } catch (const std::bad_alloc&) {
        if (completion) completion->cancel();
        return kOutOfMemory;
    } catch (...) {
        if (completion) completion->cancel();
        return kFailure;
    }
}

struct MultipleParams {
    void* device;
    void* const* fences;
    const std::uint64_t* values;
    std::uint32_t count;
    std::uint32_t flags;
    void* event;
    std::int32_t result;
};
static_assert(offsetof(MultipleParams, fences) == 8);
static_assert(offsetof(MultipleParams, values) == 16);
static_assert(offsetof(MultipleParams, count) == 24);
static_assert(offsetof(MultipleParams, flags) == 28);
static_assert(offsetof(MultipleParams, event) == 32);
static_assert(offsetof(MultipleParams, result) == 40);
static_assert(sizeof(MultipleParams) == 48);

std::int32_t unpackMultiple(void* parameters) {
    const auto params = native::readObject<MultipleParams>(parameters);
    const auto result = multiple(params.device, params.fences, params.values,
        params.count, params.flags, params.event);
    std::memcpy(static_cast<std::uint8_t*>(parameters) + offsetof(MultipleParams, result),
        &result, sizeof(result));
    return 0;
}

} // namespace

Hooks initialize(const std::uint8_t* image, Hooks originals) noexcept {
    imageBase = image;
    original = originals;
    return {
        reinterpret_cast<std::uintptr_t>(&publicSingle<&Hooks::single, abi::kDX12PublicFence>),
        reinterpret_cast<std::uintptr_t>(&multiple),
        reinterpret_cast<std::uintptr_t>(&publicSingle<&Hooks::dx12Native, abi::kNativeFence>),
        reinterpret_cast<std::uintptr_t>(&unpackSingle<&Hooks::dx12Unpack, abi::kDX12PublicFence>),
        reinterpret_cast<std::uintptr_t>(&publicSingle<&Hooks::dx11Public, abi::kDX11PublicFence>),
        reinterpret_cast<std::uintptr_t>(&publicSingle<&Hooks::dx11Native, abi::kDX11NativeFence>),
        reinterpret_cast<std::uintptr_t>(&unpackSingle<&Hooks::dx11Unpack, abi::kDX11PublicFence>),
        reinterpret_cast<std::uintptr_t>(&multiple),
        reinterpret_cast<std::uintptr_t>(&unpackMultiple),
    };
}

} // namespace yaagl::pso::fence_lifetime
