#import <Foundation/Foundation.h>
#import <objc/message.h>
#import <objc/runtime.h>

#include <Block.h>
#include <dispatch/dispatch.h>
#include <mach/mach.h>
#include <mach-o/loader.h>
#include <os/lock.h>
#include <sys/mman.h>

#include <array>
#include <atomic>
#include <barrier>
#include <cassert>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <future>
#include <map>
#include <mutex>
#include <new>
#include <set>
#include <thread>
#include <vector>

#include "fence-lifetime.hpp"
#include "layout.hpp"
#include "fence-lifetime-image-gate.inc"

namespace abi = yaagl::pso::layout::fence;
using Handler = void (^)(id, std::uint64_t);
using Single = std::int32_t (*)(void*, std::uint64_t, void*);
using PublicSingle = std::int32_t (__attribute__((ms_abi)) *)(void*, std::uint64_t, void*);
using Multiple = std::int32_t (__attribute__((ms_abi)) *)(
    void*, void* const*, const std::uint64_t*, std::uint32_t, std::uint32_t, void*);
using Function = void (*)(const void*);
extern "C" void* _NSConcreteStackBlock[32];
extern "C" void _Block_object_assign(void*, const void*, const int);
extern "C" void _Block_object_dispose(const void*, const int);

static std::uint8_t* image;
static std::mutex allocationMutex;
static std::set<void*> allocations;
static unsigned allocationCalls = 0;
static unsigned failAllocation = 0;
static std::mutex semaphoreMutex;
static std::set<dispatch_semaphore_t> semaphores;
static std::atomic<unsigned> semaphoreSignals{0};

static dispatch_semaphore_t createSemaphore(long value) {
    auto semaphore = dispatch_semaphore_create(value);
    assert(semaphore);
    std::lock_guard guard(semaphoreMutex);
    assert(semaphores.insert(semaphore).second);
    return semaphore;
}
static long signalSemaphore(dispatch_semaphore_t semaphore) {
    ++semaphoreSignals;
    return dispatch_semaphore_signal(semaphore);
}
static void releaseDispatch(dispatch_semaphore_t semaphore) {
    {
        std::lock_guard guard(semaphoreMutex);
        assert(semaphores.erase(semaphore) == 1);
    }
    dispatch_release(semaphore);
}

static void* allocate(std::size_t size) {
    std::lock_guard lock(allocationMutex);
    if (++allocationCalls == failAllocation) throw std::bad_alloc();
    void* pointer = ::operator new(size);
    assert(allocations.insert(pointer).second);
    return pointer;
}
static void deallocate(void* pointer) {
    if (!pointer) return;
    std::lock_guard lock(allocationMutex);
    assert(allocations.erase(pointer) == 1);
    ::operator delete(pointer);
}
static void balancedAllocations() {
    std::lock_guard lock(allocationMutex);
    assert(allocations.empty());
    std::lock_guard semaphoreLock(semaphoreMutex);
    assert(semaphores.empty());
}

static std::mutex registrationMutex;
static std::condition_variable registrationSignal;
static os_unfair_lock* watchedLock = nullptr;
static bool registered = false;
static void lockFence(os_unfair_lock* lock, std::uint32_t options) {
    assert(options == 0 || options == 0x40000);
    os_unfair_lock_lock(lock);
}
static void unlockFence(os_unfair_lock* lock) {
    os_unfair_lock_unlock(lock);
    std::lock_guard guard(registrationMutex);
    if (lock == watchedLock) {
        registered = true;
        registrationSignal.notify_one();
    }
}

struct Event { std::atomic<unsigned> signals{0}; };
struct EventHandle { Event* event; bool duplicate; };
struct Dispatch {
    Function function;
    std::array<std::uint8_t, 32> data{};
};
struct Provider {
    std::array<void*, 15>* table;
    std::uint64_t version = 3;
    std::mutex mutex;
    std::set<EventHandle*> handles;
    std::vector<Dispatch> queued;
    unsigned duplicates = 0;
    unsigned closes = 0;
    bool failDuplicate = false;
    explicit Provider(std::array<void*, 15>* functions) : table(functions) {}
};
static thread_local bool marshalled = false;
static std::uint64_t version(Provider* provider) { return provider->version; }
static void setEvent(Provider*, EventHandle* handle) {
    assert(handle);
    if (handle->duplicate) assert(marshalled);
    ++handle->event->signals;
}
static void closeEvent(Provider* provider, EventHandle* handle) {
    assert(marshalled && handle->duplicate);
    {
        std::lock_guard guard(provider->mutex);
        assert(provider->handles.erase(handle) == 1);
        ++provider->closes;
    }
    delete handle;
}
static void* duplicateEvent(Provider* provider, EventHandle* handle) {
    if (provider->failDuplicate) return reinterpret_cast<void*>(-1);
    auto* copy = new EventHandle{handle->event, true};
    std::lock_guard guard(provider->mutex);
    assert(provider->handles.insert(copy).second);
    ++provider->duplicates;
    return copy;
}
static void dispatch(Provider* provider, Function function, bool sync,
    const void* data, std::uint32_t size) {
    assert(!sync && size <= 32);
    Dispatch copy{function, {}};
    std::memcpy(copy.data.data(), data, size);
    std::lock_guard guard(provider->mutex);
    provider->queued.push_back(copy);
}
static void drain(Provider& provider) {
    std::vector<Dispatch> pending;
    {
        std::lock_guard guard(provider.mutex);
        pending.swap(provider.queued);
    }
    marshalled = true;
    for (const auto& item : pending) item.function(item.data.data());
    marshalled = false;
}
static void balanced(Provider& provider) {
    drain(provider);
    std::lock_guard guard(provider.mutex);
    assert(provider.handles.empty() && provider.duplicates == provider.closes);
    balancedAllocations();
}

// This receiver implements the actual native AddHandler shared-event message
// boundary. Its copied blocks have real Objective-C block copy/dispose helpers.
static unsigned sharedNotifications = 0;
static unsigned failSharedNotification = 0;
@interface FenceTestEvent : NSObject {
@public
    std::mutex mutex;
    std::vector<std::pair<std::uint64_t, Handler>> pending;
    std::uint64_t completed;
}
- (std::uint64_t)signaledValue;
- (void)notifyListener:(id)listener atValue:(std::uint64_t)value block:(Handler)block;
- (void)complete:(std::uint64_t)value;
@end
@implementation FenceTestEvent
- (std::uint64_t)signaledValue { return completed; }
- (void)notifyListener:(id)listener atValue:(std::uint64_t)value block:(Handler)block {
    static_cast<void>(listener);
    if (++sharedNotifications == failSharedNotification)
        @throw [NSException exceptionWithName:@"FenceFixtureFailure" reason:nil userInfo:nil];
    std::lock_guard guard(mutex);
    if (completed >= value) block(self, value);
    else pending.emplace_back(value, Block_copy(block));
}
- (void)complete:(std::uint64_t)value {
    std::vector<std::pair<std::uint64_t, Handler>> ready;
    {
        std::lock_guard guard(mutex);
        completed = value;
        for (auto it = pending.begin(); it != pending.end();) {
            if (it->first <= value) {
                ready.push_back(*it);
                it = pending.erase(it);
            } else ++it;
        }
    }
    for (const auto& item : ready) {
        item.second(self, value);
        Block_release(item.second);
    }
}
- (void)dealloc {
    for (const auto& item : pending) Block_release(item.second);
    [super dealloc];
}
@end
@interface FenceTestListener : NSObject
+ (id)sharedListener;
@end
@implementation FenceTestListener
+ (id)sharedListener { return nil; }
@end

struct Unknown { std::array<void*, 4>* table; void* wrapper; };
struct PublicFence { void* unknown; void* vtable; };
static void* query(Unknown* unknown, const void* iid) {
    assert(iid == reinterpret_cast<void*>(0x12345678));
    return unknown->wrapper;
}
static std::array<void*, 4> unknownTable{nullptr, nullptr, nullptr,
    reinterpret_cast<void*>(&query)};

struct Fence {
    std::array<std::uint64_t, 0x68 / 8> wrapper{};
    std::array<std::uint64_t, 0xa8 / 8> fields{};
    Unknown unknown{&unknownTable, this};
    PublicFence external{&unknown, nullptr};
    bool alive = true;

    explicit Fence(bool shared = false) {
        if (shared) fields[0x40 / 8] =
            reinterpret_cast<std::uintptr_t>([[FenceTestEvent alloc] init]);
    }
    void* native() { return fields.data(); }
    void* publicFence() { return &external.vtable; }
    void complete(std::uint64_t value) {
        if (image[abi::kUseSharedEvents] == 1) {
            [reinterpret_cast<FenceTestEvent*>(fields[0x40 / 8]) complete:value];
        } else {
            reinterpret_cast<void (*)(void*, std::uint64_t)>(image + 0x1608ee)(native(), value);
        }
    }
    void destroy() {
        assert(alive);
        // D2's VTT selects the virtual base at +0x88. No real fence destructor
        // instructions, CPU-vector releases, lock calls or frees are replaced.
        std::array<std::uint64_t, 4> table{0x88, 0, 0, 0};
        std::array<void*, 2> vtt{table.data() + 3, table.data() + 3};
        reinterpret_cast<void (*)(void*, void*)>(image + 0x1602d4)(native(), vtt.data());
        alive = false;
    }
    ~Fence() { if (alive) destroy(); }
};
static_assert(offsetof(Fence, fields) == 0x68);

struct SingleParams {
    void* self;
    std::uint64_t value;
    void* event;
    std::int32_t result;
};
struct MultiParams {
    void* device;
    void* const* fences;
    const std::uint64_t* values;
    std::uint32_t count;
    std::uint32_t flags;
    void* event;
    std::int32_t result;
};
struct SingleRoute { std::size_t entry; std::size_t nativeOffset; bool unpack; };
static constexpr std::array<SingleRoute, 6> singleRoutes{{
    {0xba259, 0x58, false}, {0xba6c4, 0x68, false}, {0xba99d, 0x58, true},
    {0x1ca86a, 0x48, false}, {0x1cad78, 0x58, false}, {0x1cafb9, 0x48, true},
}};
static unsigned singleRoute = 0;
static unsigned multiRoute = 0;
static std::int32_t routedSingle(void* fence, std::uint64_t value, void* event) {
    const auto route = singleRoutes[singleRoute];
    void* self = static_cast<std::uint8_t*>(fence) - route.nativeOffset;
    if (!route.unpack) return reinterpret_cast<PublicSingle>(
        image + route.entry)(self, value, event);
    SingleParams params{self, value, event, -1};
    assert(reinterpret_cast<std::int32_t (*)(void*)>(image + route.entry)(&params) == 0);
    return params.result;
}
static std::int32_t __attribute__((ms_abi)) routedMultiple(void* device, void* const* fences,
    const std::uint64_t* values, std::uint32_t count, std::uint32_t flags, void* event) {
    if (multiRoute != 2) return reinterpret_cast<Multiple>(
        image + (multiRoute ? 0x71380 : 0x7783e))(device, fences, values, count, flags, event);
    MultiParams params{device, fences, values, count, flags, event, -1};
    assert(reinterpret_cast<std::int32_t (*)(void*)>(image + 0x81349)(&params) == 0);
    return params.result;
}

template<class T> static void bind(std::size_t offset, T pointer) {
    const auto value = reinterpret_cast<std::uintptr_t>(pointer);
    std::memcpy(image + offset, &value, sizeof(value));
}
static void* copyBlock(const void* block) { return _Block_copy(block); }
static void releaseBlock(const void* block) { _Block_release(block); }
static void releaseObject(id object) { [object release]; }

static void imageGateControl() {
    const auto accepts = [] {
        return yaagl::pso::matchesImage(reinterpret_cast<const mach_header*>(image));
    };
    assert(accepts());
    auto* header = reinterpret_cast<mach_header*>(image);
    const auto cpu = header->cputype;
    header->cputype = CPU_TYPE_ARM64;
    assert(!accepts());
    header->cputype = cpu;
    for (const auto& span : yaagl::pso::layout::kVerificationSpans) {
        image[span.offset + span.size - 1] ^= 1;
        assert(!accepts());
        image[span.offset + span.size - 1] ^= 1;
    }
    bind(yaagl::pso::layout::kDataSlot, reinterpret_cast<void*>(1));
    assert(!accepts());
    bind(yaagl::pso::layout::kDataSlot, static_cast<void*>(nullptr));
    assert(accepts());
    std::puts("FENCE_IMAGE_GATE_PASS architecture complete-bodies ABI unpublished-slot");
}

static void beforeControl(Single single, Multiple multiple, Provider& provider) {
    Event event;
    EventHandle handle{&event, false};
    {
        Fence fence;
        assert(single(fence.native(), 1, &handle) == 0);
    }
    drain(provider);
    assert(event.signals == 0 && provider.handles.size() == 1);
    {
        marshalled = true;
        closeEvent(&provider, *provider.handles.begin());
        marshalled = false;
    }
    for (const auto flags : {0u, 1u}) {
        Fence first, second;
        std::array<void*, 2> inputs{first.publicFence(), second.publicFence()};
        const std::uint64_t values[]{1, 1};
        assert(multiple(nullptr, inputs.data(), values, 2, flags, &handle) == 0);
        if (flags == 1) first.complete(1);
        first.destroy();
        second.destroy();
        drain(provider);
        // After native fence/vector destruction, the only remaining native
        // allocation is the exact counter acquired by the original multi body.
        assert(allocations.size() == 1);
        void* orphan = *allocations.begin();
        deallocate(orphan);
        if (flags == 0) {
            assert(provider.handles.size() == 1);
            marshalled = true;
            closeEvent(&provider, *provider.handles.begin());
            marshalled = false;
        }
    }
    {
        Fence first, second;
        void* inputs[]{first.publicFence(), second.publicFence()};
        const std::uint64_t values[]{1, 1};
        {
            std::lock_guard guard(registrationMutex);
            watchedLock = reinterpret_cast<os_unfair_lock*>(
                reinterpret_cast<std::uint8_t*>(second.native()) + 0x7c);
            registered = false;
        }
        std::promise<std::int32_t> returned;
        auto result = returned.get_future();
        std::thread waiter([&] {
            returned.set_value(multiple(nullptr, inputs, values, 2, 1, nullptr));
        });
        {
            std::unique_lock lock(registrationMutex);
            assert(registrationSignal.wait_for(lock, std::chrono::seconds(5), [] { return registered; }));
            watchedLock = nullptr;
        }
        first.complete(1);
        assert(result.wait_for(std::chrono::seconds(5)) == std::future_status::ready);
        assert(result.get() == 0);
        waiter.join();
        first.destroy();
        second.destroy();
        assert(allocations.size() == 1);
        deallocate(*allocations.begin());
    }
    balanced(provider);
    std::puts("FENCE_NATIVE_BEFORE_PASS single-cancel ALL-cancel ANY-partial-counter ANY-null-counter");
}

static void fixedCases(Single single, Multiple multiple, Provider& provider, bool shared) {
    image[abi::kUseSharedEvents] = shared;
    for (unsigned iteration = 0; iteration != 32; ++iteration) {
        Event event;
        EventHandle handle{&event, false};
        {
            Fence fence(shared);
            assert(single(fence.native(), 1, &handle) == 0);
        }
        balanced(provider);
        assert(event.signals == 0);
        {
            Fence fence(shared);
            assert(single(fence.native(), 1, &handle) == 0);
            fence.complete(1);
            drain(provider);
            assert(event.signals == 1);
            const auto duplicates = provider.duplicates;
            assert(single(fence.native(), 1, &handle) == 0);
            assert(event.signals == 2 && provider.duplicates == duplicates);
        }
        balanced(provider);
        for (const auto flags : {0u, 1u}) {
            for (const auto successes : {0u, 1u, 2u}) {
                event.signals = 0;
                Fence first(shared), second(shared);
                std::array<void*, 2> inputs{first.publicFence(), second.publicFence()};
                const std::uint64_t values[]{1, 1};
                assert(multiple(nullptr, inputs.data(), values, 2, flags, &handle) == 0);
                if (successes >= 1) first.complete(1);
                if (successes == 2) second.complete(1);
                first.destroy();
                second.destroy();
                balanced(provider);
                assert(event.signals == (flags == 1 ? successes != 0 : successes == 2));
            }
            event.signals = 0;
            Fence fence(shared);
            void* repeated[]{fence.publicFence(), fence.publicFence()};
            const std::uint64_t repeatedValues[]{1, 2};
            assert(multiple(nullptr, repeated, repeatedValues, 2, flags, &handle) == 0);
            fence.complete(1);
            drain(provider);
            assert(event.signals == (flags == 1));
            fence.complete(2);
            fence.destroy();
            balanced(provider);
            assert(event.signals == 1);
            event.signals = 0;
            Fence equal(shared);
            void* equalInputs[]{equal.publicFence(), equal.publicFence()};
            const std::uint64_t equalValues[]{1, 1};
            assert(multiple(nullptr, equalInputs, equalValues, 2, flags, &handle) == 0);
            equal.complete(1);
            equal.destroy();
            balanced(provider);
            assert(event.signals == 1);
        }
    }
    std::puts(shared ? "FENCE_SHARED_PASS cancellation immediate ALL ANY duplicates" :
        "FENCE_CPU_PASS cancellation immediate ALL ANY duplicates");
}

static void concurrentCases(Multiple multiple, Provider& provider) {
    image[abi::kUseSharedEvents] = 0;
    for (const auto flags : {0u, 1u}) {
        for (unsigned iteration = 0; iteration != 32; ++iteration) {
            Event event;
            EventHandle handle{&event, false};
            Fence first, second;
            std::array<void*, 2> inputs{first.publicFence(), second.publicFence()};
            const std::uint64_t values[]{1, 1};
            assert(multiple(nullptr, inputs.data(), values, 2, flags, &handle) == 0);
            std::barrier ready(3);
            std::thread a([&] { ready.arrive_and_wait(); first.complete(1); });
            std::thread b([&] { ready.arrive_and_wait(); second.complete(1); });
            ready.arrive_and_wait();
            a.join();
            b.join();
            first.destroy();
            second.destroy();
            balanced(provider);
            assert(event.signals == 1);
        }
        Event event;
        EventHandle handle{&event, false};
        Fence first, second;
        void* inputs[]{first.publicFence(), second.publicFence()};
        const std::uint64_t values[]{1, 1};
        assert(multiple(nullptr, inputs, values, 2, flags, &handle) == 0);
        std::barrier ready(3);
        std::thread cancel([&] { ready.arrive_and_wait(); first.destroy(); });
        std::thread complete([&] { ready.arrive_and_wait(); second.complete(1); });
        ready.arrive_and_wait();
        cancel.join();
        complete.join();
        second.destroy();
        balanced(provider);
        assert(event.signals == (flags == 1));
    }
    std::puts("FENCE_CONCURRENT_PASS ALL ANY exactly-one-marshalled-signal-and-close");
}

static void failures(Single single, Multiple multiple, Provider& provider) {
    image[abi::kUseSharedEvents] = 0;
    for (const auto flags : {0u, 1u}) {
        for (const bool immediate : {false, true}) {
            Event event;
            EventHandle handle{&event, false};
            Fence first, second;
            if (immediate) first.complete(1);
            std::array<void*, 2> inputs{first.publicFence(), second.publicFence()};
            const std::uint64_t values[]{1, 1};
            failAllocation = allocationCalls + (immediate ? 2 : 3);
            assert(multiple(nullptr, inputs.data(), values, 2, flags, &handle) ==
                static_cast<std::int32_t>(0x8007000e));
            failAllocation = 0;
            // Previously registered callbacks may still arrive; cancellation
            // must be inert rather than reusing the success callback to free.
            first.complete(1);
            first.destroy();
            second.destroy();
            balanced(provider);
            assert(event.signals == 0);
        }
    }
    {
        Event event;
        EventHandle handle{&event, false};
        Fence fence;
        failAllocation = allocationCalls + 1;
        assert(single(fence.native(), 1, &handle) == static_cast<std::int32_t>(0x8007000e));
        failAllocation = 0;
        provider.failDuplicate = true;
        assert(single(fence.native(), 1, &handle) == static_cast<std::int32_t>(0x8007000e));
        provider.failDuplicate = false;
        failAllocation = allocationCalls + 2;
        assert(single(fence.native(), 1, &handle) == static_cast<std::int32_t>(0x8007000e));
        failAllocation = 0;
        // The exact native locks remain usable after a failed growth.
        assert(single(fence.native(), 1, &handle) == 0);
        fence.complete(1);
        fence.destroy();
        balanced(provider);
        assert(event.signals == 1);
    }
    image[abi::kUseSharedEvents] = 1;
    {
        Event event;
        EventHandle handle{&event, false};
        Fence first(true), second(true);
        void* inputs[]{first.publicFence(), second.publicFence()};
        const std::uint64_t values[]{1, 1};
        failSharedNotification = sharedNotifications + 2;
        assert(multiple(nullptr, inputs, values, 2, 1, &handle) ==
            static_cast<std::int32_t>(0x80004005));
        failSharedNotification = 0;
        first.complete(1);
        first.destroy();
        second.destroy();
        balanced(provider);
        assert(event.signals == 0);
    }
    image[abi::kUseSharedEvents] = 0;
    for (const auto flags : {0u, 1u}) {
        Fence first, second;
        void* inputs[]{first.publicFence(), second.publicFence()};
        const std::uint64_t values[]{1, 1};
        const auto signals = semaphoreSignals.load();
        failAllocation = allocationCalls + 3;
        assert(multiple(nullptr, inputs, values, 2, flags, nullptr) ==
            static_cast<std::int32_t>(0x8007000e));
        failAllocation = 0;
        first.complete(1);
        first.destroy();
        second.destroy();
        balanced(provider);
        assert(semaphoreSignals == signals);
    }
    std::puts("FENCE_FAILURE_PASS partial-registration immediate-ANY no-signal unlocked-retry");
}

static void synchronousCases(Multiple multiple, Provider& provider) {
    image[abi::kUseSharedEvents] = 0;
    for (const auto flags : {0u, 1u, 2u}) {
        Fence first, second;
        void* inputs[]{first.publicFence(), second.publicFence()};
        const std::uint64_t values[]{1, 1};
        {
            std::lock_guard guard(registrationMutex);
            watchedLock = reinterpret_cast<os_unfair_lock*>(
                reinterpret_cast<std::uint8_t*>(second.native()) + 0x7c);
            registered = false;
        }
        std::promise<std::int32_t> returned;
        auto result = returned.get_future();
        std::thread waiter([&] {
            returned.set_value(flags == 2 ?
                routedSingle(second.native(), 1, nullptr) :
                multiple(nullptr, inputs, values, 2, flags, nullptr));
        });
        {
            std::unique_lock lock(registrationMutex);
            assert(registrationSignal.wait_for(lock, std::chrono::seconds(5), [] { return registered; }));
            watchedLock = nullptr;
        }
        // Subscribe to insertion before starting the waiter; no sleeps or
        // repeated completion-value polling are used to make this race pass.
        first.complete(1);
        if (flags != 1) second.complete(1);
        assert(result.wait_for(std::chrono::seconds(5)) == std::future_status::ready);
        assert(result.get() == 0);
        waiter.join();
        first.destroy();
        second.destroy();
        balanced(provider);
    }
    std::puts("FENCE_SYNCHRONOUS_PASS single-null ALL-null ANY-null-first-return cancelled-rest balanced-semaphores");
}

int main(int argc, char** argv) {
    assert(argc == 2);
    @autoreleasepool {
        image = static_cast<std::uint8_t*>(mmap(nullptr, kImageSize,
            PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0));
        assert(image != MAP_FAILED);
        std::ifstream input(argv[1], std::ios::binary);
        input.read(reinterpret_cast<char*>(image), kImageSize);
        assert(input.gcount() == static_cast<std::streamsize>(kImageSize));
        imageGateControl();
        bind(kBlockCopySlot, &copyBlock);
        bind(kBlockReleaseSlot, &releaseBlock);
        bind(kBlockAssignSlot, &_Block_object_assign);
        bind(kBlockDisposeSlot, &_Block_object_dispose);
        bind(kNewSlot, &allocate);
        bind(kDeleteSlot, &deallocate);
        bind(kBzeroSlot, &bzero);
        bind(kMemcpySlot, &memcpy);
        bind(kLockSlot, &lockFence);
        bind(kUnlockSlot, &unlockFence);
        bind(kSemaphoreCreateSlot, &createSemaphore);
        bind(kSemaphoreWaitSlot, &dispatch_semaphore_wait);
        bind(kSemaphoreSignalSlot, &signalSemaphore);
        bind(kDispatchReleaseSlot, &releaseDispatch);
        bind(0x4e1360, &releaseObject);
        bind(0x4e1328, &objc_msgSend);
        bind(0x4e1768, _NSConcreteStackBlock);
        bind(0x4eea98, image + kWaitCopy);
        bind(0x4eeaa0, image + kWaitDispose);
        bind(0x4ff2f8, [FenceTestListener class]);
        bind(0x4fea90, @selector(sharedListener));
        bind(0x4fea98, @selector(notifyListener:atValue:block:));
        bind(0x4fea80, @selector(signaledValue));
        bind(abi::kFenceInterface, reinterpret_cast<void*>(0x12345678));
        image[0x550370] = 1;
        image[0x550368] = 1;
        std::array<void*, 15> eventTable{};
        eventTable[0] = reinterpret_cast<void*>(&version);
        eventTable[0x20 / 8] = reinterpret_cast<void*>(&setEvent);
        eventTable[0x38 / 8] = reinterpret_cast<void*>(&closeEvent);
        eventTable[0x60 / 8] = reinterpret_cast<void*>(&duplicateEvent);
        eventTable[0x70 / 8] = reinterpret_cast<void*>(&dispatch);
        Provider provider(&eventTable);
        bind(abi::kEventInterface, &provider);
        assert(mprotect(image, yaagl::pso::layout::kTextSize, PROT_READ | PROT_EXEC) == 0);
        const auto nativeSingle = reinterpret_cast<Single>(image + 0x161662);
        const auto nativeMultiple = reinterpret_cast<Multiple>(image + 0x7783e);
        beforeControl(nativeSingle, nativeMultiple, provider);
        const auto hooks = yaagl::pso::fence_lifetime::initialize(image, {
            reinterpret_cast<std::uintptr_t>(image + kSingleOriginal),
            reinterpret_cast<std::uintptr_t>(image + kMultipleOriginal),
            reinterpret_cast<std::uintptr_t>(image + kDX12NativeFenceCompletionOriginal),
            reinterpret_cast<std::uintptr_t>(image + kDX12UnpackFenceCompletionOriginal),
            reinterpret_cast<std::uintptr_t>(image + kDX11FenceCompletionOriginal),
            reinterpret_cast<std::uintptr_t>(image + kDX11NativeFenceCompletionOriginal),
            reinterpret_cast<std::uintptr_t>(image + kDX11UnpackFenceCompletionOriginal),
            reinterpret_cast<std::uintptr_t>(image + kPublicMultipleFenceCompletionOriginal),
            reinterpret_cast<std::uintptr_t>(image + kUnpackMultipleFenceCompletionOriginal),
        });
        std::array<std::uintptr_t, kHookCount> table{};
        table[kSingleIndex] = hooks.single;
        table[kMultipleIndex] = hooks.multiple;
        table[31] = hooks.dx12Native;
        table[32] = hooks.dx12Unpack;
        table[33] = hooks.dx11Public;
        table[34] = hooks.dx11Native;
        table[35] = hooks.dx11Unpack;
        table[36] = hooks.multiplePublic;
        table[37] = hooks.multipleUnpack;
        bind(yaagl::pso::layout::kDataSlot, table.data());
        fixedCases(&routedSingle, &routedMultiple, provider, false);
        fixedCases(&routedSingle, &routedMultiple, provider, true);
        concurrentCases(&routedMultiple, provider);
        for (singleRoute = 0; singleRoute != singleRoutes.size(); ++singleRoute) {
            failures(&routedSingle, &routedMultiple, provider);
            synchronousCases(&routedMultiple, provider);
        }
        singleRoute = 0;
        for (multiRoute = 1; multiRoute != 3; ++multiRoute) {
            failures(&routedSingle, &routedMultiple, provider);
            synchronousCases(&routedMultiple, provider);
        }
        multiRoute = 0;
        provider.version = 2;
        fixedCases(&routedSingle, &routedMultiple, provider, false);
        balanced(provider);
        assert(munmap(image, kImageSize) == 0);
        std::puts("FENCE_LIFETIME_PASS native-gates allocator-ledger copied-dispatch block-disposal");
    }
}
