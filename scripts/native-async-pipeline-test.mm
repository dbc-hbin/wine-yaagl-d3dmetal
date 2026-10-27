#import <Foundation/Foundation.h>
#import <objc/runtime.h>
#include <future>
#include <utility>

static thread_local std::promise<void>* historyLookupSignal;
static id observedAssociatedObject(id object, const void* key) {
    id value = objc_getAssociatedObject(object, key);
    if (auto* signal = std::exchange(historyLookupSignal, nullptr))
        signal->set_value();
    return value;
}
#define objc_getAssociatedObject observedAssociatedObject
#include "../d3dmetal-sidecar/async-pipeline.mm"
#undef objc_getAssociatedObject
#include "../d3dmetal-sidecar/low-latency-state.hpp"

#include <atomic>
#include <barrier>
#include <cassert>
#include <chrono>
#include <future>
#include <thread>
#include <vector>

using namespace yaagl::pso::async_pipeline;
using namespace std::chrono_literals;

template<class T> void ready(std::future<T>& future) {
    assert(future.wait_for(5s) == std::future_status::ready);
}

template<class Draw>
void registryProgressWhileHistoryBlocked(std::mutex& mutex, Draw draw) {
    assert(runtime().managedPipelinePublished.load());
    std::unique_lock historyLock(mutex);
    std::promise<void> entered;
    auto enteredSignal = entered.get_future();
    auto drawing = std::async(std::launch::async, [&] {
        @autoreleasepool {
            historyLookupSignal = &entered;
            return draw();
        }
    });
    // The association lookup is inside the old global critical section.
    // Holding the real history mutex prevents the draw from releasing it.
    ready(enteredSignal);
    enteredSignal.get();
    auto unrelated = std::async(std::launch::async, [] { return stateFor(nullptr); });
    ready(unrelated);
    assert(!unrelated.get());
    historyLock.unlock();
    ready(drawing);
    assert(drawing.get());
}

void __attribute__((ms_abi)) dx11QueryFixture(void*, void*) {}
std::atomic<unsigned> dx12Emitted{0};
std::vector<char> dx11Order;
void __attribute__((ms_abi)) dx12DrawFixture(
    void*, std::uint32_t, std::uint32_t, std::uint32_t, std::uint32_t) { ++dx12Emitted; }
void __attribute__((ms_abi)) dx12DrawIndexedFixture(
    void*, std::uint32_t, std::uint32_t, std::uint32_t, std::int32_t, std::uint32_t) {
    ++dx12Emitted;
}
std::int32_t dx12ResetResult = 0;
std::int32_t __attribute__((ms_abi)) dx12ResetFixture(void*, void*, void*) {
    return dx12ResetResult;
}
void __attribute__((ms_abi)) dx12QueryFixture(void*, void*, std::uint32_t, std::uint32_t) {}

struct PresentFixtureControl {
    std::int32_t result = 0;
    bool advance = true;
    std::uint32_t flags = 0;
    void* markedSwapchain = nullptr;
    unsigned calls = 0;
};

std::int32_t __attribute__((ms_abi)) nativePresentFixture(
    void* swapchain, std::uint32_t interval, std::uint32_t flags, const void* parameters) {
    auto& control = *static_cast<PresentFixtureControl*>(const_cast<void*>(parameters));
    assert(interval == 2 && flags == control.flags);
    ++control.calls;
    // The hook must consume even a mismatched marker before entering native code.
    if (control.markedSwapchain)
        assert(!presentationHistory().take(control.markedSwapchain).swapchain);
    auto* counterAddress = static_cast<std::uint8_t*>(swapchain) + 0x120;
    std::uint64_t counter;
    std::memcpy(&counter, counterAddress, sizeof(counter));
    if (control.advance) ++counter;
    std::memcpy(counterAddress, &counter, sizeof(counter));
    return control.result;
}

void presentEpochFixture() {
    auto& rt = runtime();
    const auto savedFrame = rt.frame.load();
    const auto savedOriginal = original.present;
    const auto savedEnabled = enabled;
    std::array<std::uint8_t, 0x128> native{}, other{};
    void* external = native.data() + 0x10;
    void* otherExternal = other.data() + 0x10;
    original.present = reinterpret_cast<std::uintptr_t>(&nativePresentFixture);
    enabled = true;
    rt.frame = 100;
    const auto call = [&](void* object, PresentFrame marker, bool counts,
                          std::int32_t result = 0, bool advance = true, std::uint32_t flags = 0) {
        if (marker.swapchain) presentationHistory().mark(marker);
        PresentFixtureControl control{result, advance, flags, marker.swapchain};
        const auto before = rt.frame.load();
        const auto returned = present(object, 2, flags, &control);
        assert(returned == result && control.calls == 1);
        assert(rt.frame.load() == before + (counts ? 1 : 0));
    };
    const auto member = [&](std::uint64_t frame, bool generated, bool counts,
                            std::uint64_t source = 1) {
        call(native.data(), {external, source, frame, generated}, counts);
    };

    TargetHistory<std::uint64_t> targets;
    for (std::uint64_t frame = 1; frame <= 5; ++frame) {
        targets.use(7, rt.frame.load(), true);
        assert(targets.use(7, rt.frame.load(), false) == (frame == 5));
        member(frame, true, true);
        member(frame, false, false);
        assert(rt.frame.load() == 100 + frame);
        assert(targets.use(7, rt.frame.load(), false) == (frame == 5));
    }
    // Skip observing one actual app frame, not just its generated member.
    member(6, true, true);
    member(6, false, false);
    assert(!targets.use(7, rt.frame.load(), false));
    targets.use(7, rt.frame.load(), true);
    assert(!targets.use(7, rt.frame.load(), false));

    // Repeated IDs in generated-only and real-only streams still advance.
    member(7, true, true);
    member(7, true, true);
    member(8, false, true);
    member(8, false, true);
    member(8, true, true);
    member(8, false, false);
    member(8, false, true); // A pair suppresses exactly one real member.
    call(native.data(), {external, 1, 0, true, true}, true);
    member(0, false, true); // Generated-only packet, then reset ID with no generated output.

    call(native.data(), {external, 1, 9, true}, false, -1);
    member(9, false, true); // Failed generated output cannot consume this frame.
    member(10, true, true);
    call(native.data(), {external, 1, 10, false}, false, -1);
    member(10, false, true); // Failure also breaks the previous successful pair.
    call(native.data(), {external, 1, 11, true}, false, 0, true, 1);
    member(11, false, true); // TEST counter movement is not a generated success.
    call(native.data(), {external, 1, 12, true}, false, 0, false);
    member(12, false, true);
    call(native.data(), {}, true, 1); // Any nonnegative HRESULT qualifies.

    member(100, true, true);
    member(100, false, true, 2); // Same address/ID, different source lifetime.
    member(0, true, true, 2);
    member(0, false, false, 2); // Frame IDs can reset; zero is not a sentinel.
    member(0, true, true, 2);
    member(0, false, false, 2);
    member(1, true, true, 2);
    call(other.data(), {otherExternal, 2, 1, false}, true);
    member(1, false, true, 2); // Another swapchain breaks adjacency.

    member(20, true, true);
    call(other.data(), {external, 1, 20, false}, true);
    call(native.data(), {}, true); // Mismatched marker cannot leak to this call.
    member(20, false, true);
    call(native.data(), {external, 1, 21, true}, false, -1);
    call(native.data(), {}, true);
    member(21, false, true); // The failed call's marker cannot be reused.

    // Complete worker calls before resuming the main thread: no timing assumptions.
    presentationHistory().mark({external, 1, 30, true});
    auto unmarkedWorker = std::async(std::launch::async, [&] {
        call(native.data(), {}, true);
    });
    ready(unmarkedWorker);
    unmarkedWorker.get();
    call(native.data(), {}, true); // Main-thread pending marker survived the worker.
    member(30, false, false);
    member(31, true, true);
    auto realWorker = std::async(std::launch::async, [&] {
        member(31, false, true); // Cannot pair with another thread's generated call.
    });
    ready(realWorker);
    realWorker.get();
    member(31, false, false); // Worker completion cannot clear this thread's pair.

    enabled = false;
    for (bool generated : {false, true}) {
        yaagl::latency::Limiter limiter(true);
        int device;
        presentationHistory().mark({external, 1, 32, generated});
        auto old = limiter.begin(&device, native.data(),
            !presentationHistory().hasFrameGeneration(external));
        assert(!limiter.enabled(&device)); // Actual FG remains excluded.
        call(native.data(), {}, false);
        limiter.finish(old, true);
        limiter.complete(native.data());
        limiter.retire(native.data());
        limiter.destroy(&device);

        // Reuse both native addresses without FG on the same rendering thread.
        auto reused = limiter.begin(&device, native.data(),
            !presentationHistory().hasFrameGeneration(external));
        assert(limiter.enabled(&device));
        limiter.finish(reused, true);
        limiter.complete(native.data());
        limiter.retire(native.data());
        limiter.destroy(&device);
    }
    call(native.data(), {external, 1, 33, true}, false, -1, false);
    call(native.data(), {external, 1, 34, true}, false, 0, false, 1);
    call(native.data(), {external, 1, 35, false}, false, 1, false);
    call(native.data(), {otherExternal, 1, 36, true}, false);

    original.present = savedOriginal;
    enabled = savedEnabled;
    rt.frame = savedFrame;
}

void dx12SafetyFixture() {
    @autoreleasepool {
        auto& rt = runtime();
        std::array<std::uint8_t, 0xe50> list{};
        std::array<std::uint8_t, 0x710> native{};
        int root, device, otherDevice, heapA, heapB, pipeline;
        auto texture = [[NSObject alloc] init];
        auto fresh = [[NSObject alloc] init];
        const auto put = []<class T>(auto& object, std::size_t offset, T value) {
            std::memcpy(object.data() + offset, &value, sizeof(value));
        };
        put(list, 0x100, native.data());
        put(list, 0x130, std::uint64_t{0x46});
        put(list, 0x138, std::uint8_t{1});
        put(list, 0x13c, std::uint32_t{1});
        put(list, 0x1d0, texture);
        put(list, 0x3f4, std::uint32_t{1});
        put(list, 0xe48, &root);
        std::vector<std::function<void()>> queued;
        unsigned compiled = 0, rejected = 0;
        auto owner = std::make_shared<State>(
            [&](Key) -> void* { ++compiled; return &pipeline; },
            [&](std::function<void()> job) { queued.push_back(std::move(job)); });
        rt.pipelines.emplace(native.data(), Pipeline{owner, &device, 1});
        rt.roots.emplace(&root, Root{true, &device, 1});
        rt.lists.emplace(list.data(), List{true, {}});
        original.draw = reinterpret_cast<std::uintptr_t>(&dx12DrawFixture);
        original.drawIndexed = reinterpret_cast<std::uintptr_t>(&dx12DrawIndexedFixture);
        original.reset = reinterpret_cast<std::uintptr_t>(&dx12ResetFixture);
        original.beginQuery = original.endQuery = reinterpret_cast<std::uintptr_t>(&dx12QueryFixture);
        original.listDestroy = reinterpret_cast<std::uintptr_t>(+[](void*) {});
        enabled = true;
        for (std::uint64_t frame = 1; frame <= 5; ++frame) {
            rt.frame = frame;
            targets(list.data(), true);
        }
        const auto reject = [&](const char* name) {
            const auto before = dx12Emitted.load();
            draw(list.data(), 3, 1, 0, 0);
            drawIndexed(list.data(), 3, 1, 0, 0, 0);
            if (dx12Emitted != before + 2 || !queued.empty())
                std::fprintf(stderr, "DX12 unsafe omission: %s\n", name);
            assert(dx12Emitted == before + 2 && queued.empty() && compiled == 0);
            ++rejected;
        };
        rt.roots.erase(&root);
        reject("missing root");
        rt.roots.emplace(&root, Root{false, &device, 1});
        reject("UAV-capable root");
        rt.roots.at(&root).safe = true;
        rt.roots.at(&root).device = &otherDevice;
        reject("different root device");
        rt.roots.at(&root).device = &device;
        rt.roots.at(&root).id = 2;
        reject("different root identity");
        rt.roots.at(&root).id = 1;
        rt.lists.erase(list.data());
        reject("unregistered command list");
        rt.lists.emplace(list.data(), List{false, {}});
        reject("uninitialized command list");
        dx12ResetResult = -1;
        assert(reset(list.data(), nullptr, nullptr) == -1);
        reject("failed initial reset");
        dx12ResetResult = 0;
        assert(reset(list.data(), nullptr, nullptr) == 0);
        beginQuery(list.data(), &heapA, 0, 0);
        beginQuery(list.data(), &heapA, 0, 1);
        reject("active queries");
        endQuery(list.data(), &heapA, 2, 0);
        reject("end-only timestamp preserves active queries");
        endQuery(list.data(), &heapA, 0, 0);
        reject("different query index remains active");
        endQuery(list.data(), &heapB, 0, 1);
        reject("different query heap cannot end the remaining query");
        dx12ResetResult = -1;
        assert(reset(list.data(), nullptr, nullptr) == -1);
        reject("failed reset preserves active query");
        dx12ResetResult = 0;
        assert(reset(list.data(), nullptr, nullptr) == 0);
        assert(rt.lists.at(list.data()).queries.empty());
        put(list, 0x70, std::uint32_t{1});
        reject("bundle");
        put(list, 0x70, std::uint32_t{0});
        put(native, 0x44, std::uint32_t{1});
        reject("native pipeline exclusion");
        put(native, 0x44, std::uint32_t{0});
        put(native, 0x32e, std::uint8_t{1});
        reject("native stream-output exclusion");
        put(native, 0x32e, std::uint8_t{0});
        put(list, 0x428, std::uint8_t{1});
        reject("native draw-state exclusion");
        put(list, 0x428, std::uint8_t{0});
        put(list, 0x3f4, std::uint32_t{9});
        reject("unsupported attachment count");
        put(list, 0x3f4, std::uint32_t{1});
        put(list, 0x1d0, static_cast<id>(nil));
        reject("no target");
        put(list, 0x1d0, fresh);
        reject("first-use target");
        put(list, 0x1d0, texture);
        listDestroy(list.data());
        reject("retired command list");
        assert(reset(list.data(), nullptr, nullptr) == 0);

        // Empty direct/indexed draws must neither enqueue a cold variant nor
        // keep previously recurrent attachments eligible in later epochs.
        for (std::uint64_t frame = 6; frame <= 7; ++frame) {
            rt.frame = frame;
            for (const auto size : {std::pair{0u, 1u}, std::pair{3u, 0u}}) {
                const auto before = dx12Emitted.load();
                draw(list.data(), size.first, size.second, 0, 0);
                drawIndexed(list.data(), size.first, size.second, 0, 0, 0);
                assert(dx12Emitted == before + 2 && queued.empty() && compiled == 0);
            }
        }
        assert(!targets(list.data(), false));
        for (std::uint64_t frame = 10; frame <= 14; ++frame) {
            rt.frame = frame;
            targets(list.data(), true);
        }

        // The same cold key must be skippable with every guard restored. The
        // scheduler retains the job, so compiler speed cannot mask a bad gate.
        const auto before = dx12Emitted.load();
        draw(list.data(), 3, 1, 0, 0);
        drawIndexed(list.data(), 3, 1, 0, 0, 0);
        assert(dx12Emitted == before && queued.size() == 1 && compiled == 0);
        rt.frame = 15;
        draw(list.data(), 3, 1, 0, 0);
        drawIndexed(list.data(), 3, 1, 0, 0, 0);
        assert(dx12Emitted == before && queued.size() == 1 && compiled == 0);
        rt.frame = 16;
        assert(!targets(list.data(), false)); // Omitted frames cannot renew history.
        // Expiry makes this draw non-skippable. It must execute the queued
        // compile itself rather than wait for our deliberately idle scheduler.
        draw(list.data(), 3, 1, 0, 0);
        assert(dx12Emitted == before + 1 && compiled == 1);
        queued.front()(); // The late callback cannot compile a second time.
        draw(list.data(), 3, 1, 0, 0);
        drawIndexed(list.data(), 3, 1, 0, 0, 0);
        assert(dx12Emitted == before + 3 && compiled == 1);
        owner->stop();
        owner->wait();
        rt.pipelines.erase(native.data());
        rt.roots.erase(&root);
        rt.lists.erase(list.data());
        enabled = false;
        [fresh release]; [texture release];
        std::printf("DX12 safety gates: %u rejected conditions, direct/indexed and pending/ready controls passed\n",
                    rejected);
    }
}

int main() {
    presentEpochFixture();
    const Key key{0x102, 0x123456789abcdef0ULL};
    int nativePipeline = 1;
    std::vector<std::function<void()>> queued;
    std::mutex queueMutex;
    std::atomic<unsigned> compiled{0};
    std::promise<void> entered, release;
    auto enteredSignal = entered.get_future();
    auto releaseSignal = release.get_future().share();
    State state([&](Key observed) -> void* {
        assert(observed.flags == key.flags && observed.formats == key.formats);
        assert(++compiled == 1);
        entered.set_value();
        releaseSignal.wait();
        return &nativePipeline;
    }, [&](std::function<void()> job) {
        std::lock_guard lock(queueMutex);
        queued.push_back(std::move(job));
    });

    // Concurrent submissions must share one job before the compiler even starts.
    std::vector<std::thread> callers;
    for (unsigned i = 0; i != 8; ++i) callers.emplace_back([&] {
        assert(state.request(key, true).status == Status::Pending);
    });
    for (auto& caller : callers) caller.join();
    assert(queued.size() == 1 && compiled == 0);
    assert(!state.readyRevision(key));
    assert(!state.readyRevision({7, 8}));
    assert(queued.size() == 1); // A readiness miss must not admit work.
    auto worker = std::async(std::launch::async, std::move(queued.front()));
    ready(enteredSignal);
    enteredSignal.get();
    // These calls execute while compilation is deterministically held. A hidden
    // wait deadlocks the bounded test instead of passing by compiler timing luck.
    for (unsigned i = 0; i != 100; ++i)
        assert(state.request(key, true).status == Status::Pending);
    auto synchronous = std::async(std::launch::async, [&] { return state.request(key, false); });
    release.set_value();
    ready(worker);
    worker.get();
    ready(synchronous);
    const auto result = synchronous.get();
    assert(result.status == Status::Ready && result.pipeline == &nativePipeline);
    const auto revision = state.readyRevision(key);
    assert(revision && state.matchesRevision(revision));
    assert(!state.readyRevision({key.flags, key.formats + 1}));
    assert(!state.readyRevision({key.flags + 1, key.formats}));
    assert(state.request(key, true).pipeline == &nativePipeline);
    assert(compiled == 1 && queued.size() == 1);
    state.stop();
    state.wait();
    assert(!state.readyRevision(key) && !state.matchesRevision(revision));
    assert(state.request({7, 8}, true).status == Status::Failed);

    unsigned failures = 0;
    State failed([&](Key) -> void* { ++failures; return nullptr; },
                 [](std::function<void()> job) { job(); });
    failed.request(key, true);
    assert(!failed.readyRevision(key));
    assert(failed.request(key, true).status == Status::Failed);
    assert(failed.request(key, false).status == Status::Failed);
    assert(failures == 1);
    failed.stop();
    failed.wait();

    // Both words of the native dynamic key participate in single flight.
    unsigned variants = 0;
    State keyed([&](Key) -> void* { ++variants; return &nativePipeline; },
                [](std::function<void()> job) { job(); });
    keyed.request({1, 0x100000000ULL}, false);
    keyed.request({1, 0x200000000ULL}, false);
    keyed.request({2, 0x100000000ULL}, false);
    keyed.request({1, 0x100000000ULL}, false);
    assert(variants == 3);
    keyed.stop();
    keyed.wait();

    std::function<void()> otherVariant;
    State concurrentOwner([&](Key candidate) -> void* {
        return candidate.flags == key.flags + 2 ? nullptr : &nativePipeline;
    },
        [&](std::function<void()> work) { otherVariant = std::move(work); });
    concurrentOwner.request(key, false);
    const auto originalRevision = concurrentOwner.readyRevision(key);
    assert(originalRevision && concurrentOwner.matchesRevision(originalRevision));
    concurrentOwner.request({key.flags + 1, key.formats}, true);
    assert(!concurrentOwner.readyRevision(key)); // Ready key, outstanding owner work.
    assert(!concurrentOwner.matchesRevision(originalRevision));
    otherVariant();
    assert(!concurrentOwner.matchesRevision(originalRevision)); // No ABA after completion.
    const auto afterAsync = concurrentOwner.readyRevision(key);
    assert(afterAsync);
    assert(concurrentOwner.request({key.flags + 2, key.formats}, false).status == Status::Failed);
    assert(!concurrentOwner.matchesRevision(afterAsync));
    const auto beforeStop = concurrentOwner.readyRevision(key);
    assert(beforeStop);
    concurrentOwner.request({key.flags + 3, key.formats}, true);
    concurrentOwner.stop();
    otherVariant();
    concurrentOwner.wait();
    assert(!concurrentOwner.readyRevision(key) && !concurrentOwner.matchesRevision(beforeStop));

    // Retirement stops admission before draining queued work. The compiler is
    // allowed to touch its native owner until the exact completion notification.
    std::function<void()> outstanding;
    bool ownerAlive = true;
    State retiring([&](Key) -> void* { assert(ownerAlive); return &nativePipeline; },
                   [&](std::function<void()> job) { outstanding = std::move(job); });
    retiring.request(key, true);
    retiring.stop();
    assert(retiring.request({9, 10}, true).status == Status::Failed);
    auto retireWorker = std::async(std::launch::async, std::move(outstanding));
    retiring.wait();
    ownerAlive = false;
    ready(retireWorker);
    retireWorker.get();

    // Synchronous consumers and retirement can take work from an idle queue.
    // Retained callbacks must also be safe after both state and owner are gone.
    std::vector<std::function<void()>> late;
    unsigned stolenCompiles = 0;
    bool stolenOwnerAlive = true;
    {
        State stolen([&](Key candidate) -> void* {
            assert(stolenOwnerAlive);
            ++stolenCompiles;
            return candidate.flags == 3 ? nullptr : &nativePipeline;
        }, [&](std::function<void()> job) { late.push_back(std::move(job)); });
        stolen.request({1, 1}, true);
        assert(stolen.request({1, 1}, false).pipeline == &nativePipeline);
        stolen.request({2, 2}, true);
        stolen.request({3, 3}, true);
        stolen.wait();
        assert(stolenCompiles == 3);
        assert(stolen.request({3, 3}, false).status == Status::Failed);
        stolen.request({4, 4}, true);
        stolen.stop();
        stolen.wait();
        assert(stolenCompiles == 4);
        assert(stolen.request({5, 5}, true).status == Status::Failed);
        stolenOwnerAlive = false;
    }
    for (auto& callback : late) callback();
    assert(stolenCompiles == 4);

    // Admission precedes enqueue. Retirement can finish the admitted job even
    // while the submitter is suspended in its scheduler callback.
    std::function<void()> delayed;
    std::promise<void> enqueueEntered, enqueueRelease;
    auto enqueueEnteredSignal = enqueueEntered.get_future();
    auto enqueueReleaseSignal = enqueueRelease.get_future();
    unsigned delayedCompiles = 0;
    {
        State submitting([&](Key) -> void* { ++delayedCompiles; return &nativePipeline; },
            [&](std::function<void()> job) {
                enqueueEntered.set_value();
                enqueueReleaseSignal.wait();
                delayed = std::move(job);
            });
        auto submitter = std::async(std::launch::async, [&] { submitting.request(key, true); });
        ready(enqueueEnteredSignal);
        submitting.stop();
        submitting.wait();
        assert(delayedCompiles == 1);
        enqueueRelease.set_value();
        ready(submitter);
        submitter.get();
    }
    delayed();
    assert(delayedCompiles == 1);

    // Owner-wide draining assists an unstarted variant while another variant
    // is already compiling, then joins that running job before retirement.
    std::vector<std::function<void()>> mixedJobs;
    std::promise<void> runningEntered, runningRelease, queuedCompleted;
    auto runningEnteredSignal = runningEntered.get_future();
    auto runningReleaseSignal = runningRelease.get_future();
    auto queuedCompletedSignal = queuedCompleted.get_future();
    std::atomic<bool> drained{false};
    std::atomic<unsigned> mixedCompiles{0};
    State mixed([&](Key candidate) -> void* {
        ++mixedCompiles;
        if (candidate.flags == 1) {
            runningEntered.set_value();
            runningReleaseSignal.wait();
        } else {
            queuedCompleted.set_value();
        }
        return &nativePipeline;
    }, [&](std::function<void()> job) { mixedJobs.push_back(std::move(job)); });
    mixed.request({1, 1}, true);
    mixed.request({2, 2}, true);
    auto runningJob = std::async(std::launch::async, mixedJobs.front());
    ready(runningEnteredSignal);
    mixed.stop();
    auto draining = std::async(std::launch::async, [&] { mixed.wait(); drained = true; });
    ready(queuedCompletedSignal);
    assert(!drained && mixedCompiles == 2);
    runningRelease.set_value();
    ready(runningJob); runningJob.get();
    ready(draining); draining.get();
    mixedJobs.back()();
    assert(drained && mixedCompiles == 2);

    // The callback and two synchronous consumers race for one claim.
    std::function<void()> racingCallback;
    std::atomic<unsigned> racingCompiles{0};
    State racing([&](Key) -> void* { ++racingCompiles; return &nativePipeline; },
        [&](std::function<void()> job) { racingCallback = std::move(job); });
    racing.request(key, true);
    std::barrier claimStart(4);
    auto queuedCaller = std::async(std::launch::async, [&] {
        claimStart.arrive_and_wait();
        racingCallback();
    });
    const auto synchronousClaim = [&] {
        claimStart.arrive_and_wait();
        return racing.request(key, false);
    };
    auto firstClaim = std::async(std::launch::async, synchronousClaim);
    auto secondClaim = std::async(std::launch::async, synchronousClaim);
    claimStart.arrive_and_wait();
    ready(queuedCaller); queuedCaller.get();
    ready(firstClaim); ready(secondClaim);
    assert(firstClaim.get().pipeline == &nativePipeline);
    assert(secondClaim.get().pipeline == &nativePipeline && racingCompiles == 1);
    racing.stop(); racing.wait();

    {
        auto& rt = runtime();
        std::array<std::uint8_t, 0x710> native{};
        std::array<std::uint8_t, 0x60> device{};
        std::array<std::uint8_t, 0x10> description{};
        original.getRenderPipelineState = reinterpret_cast<std::uintptr_t>(+[](void*, std::uint32_t, std::uint64_t) -> void* {
            static int result;
            return &result;
        });
        enabled = true;
        {
            std::lock_guard lock(rt.mutex);
            auto legacy = std::async(std::launch::async, [&] {
                return getRenderPipeline(native.data(), 2, 0x46);
            });
            // Before any managed owner exists, legacy lookup must not wait on
            // the owner-map mutex. The future is the bounded completion signal.
            ready(legacy);
            assert(legacy.get());
        }
        const std::uint32_t mpl = 2;
        std::memcpy(device.data() + 0x58, &mpl, sizeof(mpl));
        original.graphicsCtor = reinterpret_cast<std::uintptr_t>(+[](void* owner, void*, const void*) {
            getRenderPipeline(owner, 2, 0x46);
        });
        original.graphicsDestroy = reinterpret_cast<std::uintptr_t>(+[](void*) {});
        rt.queue = dispatch_queue_create("yaagl.async-publication-test", DISPATCH_QUEUE_SERIAL);
        graphicsCtor(native.data(), device.data(), description.data());
        auto published = stateFor(native.data());
        assert(published);
        published->wait();
        assert(published->readyRevision({2, 0x46}));
        graphicsDestroy(native.data());
        assert(!stateFor(native.data()));
        assert(rt.managedPipelinePublished.load());
        dispatch_sync_f(rt.queue, nullptr, [](void*) {});
        dispatch_release(rt.queue);
        rt.queue = nullptr;
        enabled = false;
    }

    dx12SafetyFixture();
    Recurrence history;
    for (unsigned i = 0; i != 100; ++i) history.observe(1);
    assert(!history.eligible(1));
    for (unsigned frame = 2; frame <= 5; ++frame) history.observe(frame);
    assert(history.eligible(5) && history.eligible(6));
    assert(!history.eligible(7));
    history.observe(8);
    assert(!history.eligible(8));

    @autoreleasepool {
        auto& rt = runtime();
        std::array<std::uint8_t, 0xe50> list{};
        std::array<std::uint8_t, 0x710> warmPSO{}, coldPSO{};
        int root, device;
        NSObject* texture = [[NSObject alloc] init];
        const auto put = [&]<class T>(std::size_t offset, T value) {
            std::memcpy(list.data() + offset, &value, sizeof(value));
        };
        put(0x100, warmPSO.data());
        put(0x130, std::uint64_t{0x46});
        put(0x138, std::uint8_t{1});
        put(0x13c, std::uint32_t{1});
        put(0x1d0, texture);
        put(0x3f4, std::uint32_t{1});
        put(0xe48, &root);
        auto warm = std::make_shared<State>(
            [&](Key) -> void* { return &nativePipeline; }, [](std::function<void()> job) { job(); });
        std::function<void()> compileCold;
        auto cold = std::make_shared<State>([&](Key) -> void* { return &nativePipeline; },
            [&](std::function<void()> job) { compileCold = std::move(job); });
        warm->request({2, 0x46}, false);
        cold->request({2, 0x46}, true);
        rt.pipelines.emplace(warmPSO.data(), Pipeline{warm, &device, 1});
        rt.pipelines.emplace(coldPSO.data(), Pipeline{cold, &device, 1});
        rt.roots.emplace(&root, Root{true, &device, 1});
        rt.lists.emplace(list.data(), List{true, {}});
        original.draw = reinterpret_cast<std::uintptr_t>(&dx12DrawFixture);
        enabled = true;
        for (std::uint64_t frame = 1; frame <= 5; ++frame) {
            rt.frame = frame;
            draw(list.data(), 3, 1, 0, 0);
        }
        std::promise<void> primed, proceed;
        auto primedSignal = primed.get_future();
        auto proceedSignal = proceed.get_future();
        const auto emittedBefore = dx12Emitted.load();
        auto warmDraws = std::async(std::launch::async, [&] {
            draw(list.data(), 3, 1, 0, 0);
            primed.set_value();
            proceedSignal.wait();
            for (unsigned i = 0; i < 5; ++i) draw(list.data(), 3, 1, 0, 0);
        });
        ready(primedSignal);
        primedSignal.get();
        {
            std::lock_guard lock(rt.mutex);
            proceed.set_value();
            ready(warmDraws); // A hot draw must progress without this unrelated lock.
            warmDraws.get();
        }
        assert(dx12Emitted == emittedBefore + 6);

        graphicsDestroy(warmPSO.data());
        std::function<void()> compileReplacement;
        auto replacement = std::make_shared<State>([&](Key) -> void* { return &nativePipeline; },
            [&](std::function<void()> job) { compileReplacement = std::move(job); });
        replacement->request({2, 0x46}, true);
        rt.pipelines.emplace(warmPSO.data(), Pipeline{replacement, &device, 1});
        // The same native address now names a pending owner, not cached readiness.
        assert(skipDraw(list.data()));
        compileReplacement();
        assert(!skipDraw(list.data()));
        graphicsDestroy(warmPSO.data());
        put(0x100, coldPSO.data());
        // Ready draws must still establish history for a later cold owner.
        auto* target = static_cast<YAAGLAsyncTargetHistory*>(
            objc_getAssociatedObject(texture, &historyKey));
        assert(target);
        registryProgressWhileHistoryBlocked(target->mutex, [&] { return skipDraw(list.data()); });
        assert(skipDraw(list.data()));
        compileCold();
        assert(!skipDraw(list.data()));
        cold->stop();
        cold->wait();
        rt.pipelines.erase(warmPSO.data());
        rt.pipelines.erase(coldPSO.data());
        rt.roots.erase(&root);
        rt.lists.erase(list.data());
        enabled = false;
        [texture release];
    }

    @autoreleasepool {
        auto& rt = runtime();
        NSObject* textures[]{[[NSObject alloc] init], [[NSObject alloc] init]};
        std::atomic<unsigned> created{0};
        std::array<id, 8> identities{};
        std::barrier start(9);
        std::vector<std::future<void>> observers;
        rt.frame = 40;
        for (std::size_t i = 0; i < identities.size(); ++i) {
            observers.push_back(std::async(std::launch::async, [&, i] {
                start.arrive_and_wait();
                identities[i] = targetHistory(textures[0], &historyKey, true, [&] {
                    ++created;
                    return [[YAAGLAsyncTargetHistory alloc] init];
                });
            }));
        }
        start.arrive_and_wait();
        for (auto& observer : observers) { ready(observer); observer.get(); }
        assert(created == 1);
        for (id identity : identities) assert(identity == identities.front());

        std::array<std::array<std::uint8_t, 0x400>, 2> lists{};
        for (unsigned i = 0; i < lists.size(); ++i) {
            const std::uint32_t count = 2;
            std::memcpy(lists[i].data() + 0x3f4, &count, sizeof(count));
            for (unsigned attachment = 0; attachment < 2; ++attachment) {
                id texture = textures[(i + attachment) % 2];
                std::memcpy(lists[i].data() + 0x1d0 + attachment * 0x38, &texture, sizeof(texture));
            }
        }
        for (std::uint64_t frame = 40; frame < 45; ++frame) {
            rt.frame = frame;
            std::barrier together(3);
            observers.clear();
            for (unsigned i = 0; i < lists.size(); ++i)
                observers.push_back(std::async(std::launch::async, [&, i] {
                    together.arrive_and_wait();
                    for (unsigned draw = 0; draw < 20; ++draw) targets(lists[i].data(), true);
                }));
            together.arrive_and_wait();
            for (auto& observer : observers) { ready(observer); observer.get(); }
            assert(targets(lists[0].data(), false) == (frame == 44));
            assert(targets(lists[1].data(), false) == (frame == 44));
        }
        for (id texture : textures) [texture release];
    }

    @autoreleasepool {
        auto& rt = runtime();
        std::lock_guard lock(rt.mutex);
        std::array<std::uint8_t, 0x400> list{};
        const std::uint32_t count = 1;
        std::memcpy(list.data() + 0x3f4, &count, sizeof(count));
        NSObject* texture = [[NSObject alloc] init];
        std::memcpy(list.data() + 0x1d0, &texture, sizeof(texture));
        const auto select = [&](std::uint32_t mip, std::uint32_t slice = 0) {
            std::memcpy(list.data() + 0x1d8, &mip, sizeof(mip));
            std::memcpy(list.data() + 0x1dc, &slice, sizeof(slice));
        };
        // Repeated draws cannot turn one frame into five observations. Mips and
        // slices must become recurrent independently, even within one texture.
        for (std::uint64_t frame = 1; frame <= 5; ++frame) {
            rt.frame = frame;
            select(0);
            for (unsigned draw = 0; draw != 20; ++draw) targets(list.data(), true);
            assert(targets(list.data(), false) == (frame == 5));
        }
        select(0, 1);
        assert(!targets(list.data(), false));
        targets(list.data(), true);
        select(1);
        assert(!targets(list.data(), false));
        targets(list.data(), true);
        select(0);
        assert(targets(list.data(), false));
        rt.frame = 7;
        select(1);
        targets(list.data(), true);
        select(0);
        assert(!targets(list.data(), false));

        [texture release];
        texture = [[NSObject alloc] init];
        std::memcpy(list.data() + 0x1d0, &texture, sizeof(texture));
        select(0);
        assert(!targets(list.data(), false)); // A new texture owns no old history.
        // Fill the bounded history. Reusing an existing key at capacity must
        // preserve other keys; adding a new key at capacity resets recurrence.
        for (std::uint64_t frame = 10; frame <= 14; ++frame) {
            rt.frame = frame;
            for (std::uint32_t mip = 0; mip < 64; ++mip) {
                select(mip);
                targets(list.data(), true);
            }
        }
        select(0);
        targets(list.data(), true);
        select(63);
        assert(targets(list.data(), false));
        select(64);
        auto resetList = list;
        auto overflow = std::async(std::launch::async, [&] { targets(resetList.data(), true); });
        ready(overflow);
        overflow.get();
        assert(!targets(list.data(), false));
        select(0);
        assert(!targets(list.data(), false));
        targets(list.data(), true);
        for (std::uint64_t frame = 15; frame <= 18; ++frame) {
            rt.frame = frame;
            targets(list.data(), true);
        }
        // Re-observation after a same-epoch capacity reset starts fresh and
        // must not be lost to a memo of the discarded entry.
        assert(targets(list.data(), false));

        // Expiry must run before capacity handling in each new epoch. Keep one
        // recurrent key alive while the other 63 expire, then insert a new key.
        for (std::uint64_t frame = 20; frame <= 24; ++frame) {
            rt.frame = frame;
            for (std::uint32_t mip = 0; mip < 64; ++mip) {
                select(mip);
                targets(list.data(), true);
            }
        }
        rt.frame = 25;
        select(0);
        targets(list.data(), true);
        rt.frame = 26;
        select(64);
        targets(list.data(), true);
        select(65);
        targets(list.data(), true);
        select(0);
        assert(targets(list.data(), false));
        [texture release];
    }

    {
        struct Owner {
            std::mutex mutex;
            TargetHistory<std::uint64_t> history;
        };
        alignas(Owner) std::array<std::byte, sizeof(Owner)> storage;
        auto* owner = new (storage.data()) Owner;
        std::array<std::pair<Owner*, std::uint64_t>, 1> entry{{{owner, 0}}};
        runtime().frame = 50;
        assert(useTargets(entry, entry.size(), true));
        const auto oldIdentity = owner->history.identity();
        const auto oldRevision = owner->history.observationRevision();
        owner->~Owner();
        owner = new (storage.data()) Owner;
        entry[0].first = owner;
        assert(owner->history.identity() != oldIdentity);
        {
            std::lock_guard lock(owner->mutex);
            owner->history.use(1, 50, true);
        }
        assert(owner->history.observationRevision() == oldRevision);
        // Same address and revision, but this new history has never seen key 0.
        assert(useTargets(entry, entry.size(), true));
        for (std::uint64_t frame = 51; frame <= 54; ++frame) {
            runtime().frame = frame;
            assert(useTargets(entry, entry.size(), true));
        }
        assert(useTargets(entry, entry.size(), false));
        owner->~Owner();

        Owner shared;
        runtime().frame = 60;
        {
            std::lock_guard lock(shared.mutex);
            for (std::uint64_t key = 2; key < 65; ++key) shared.history.use(key, 60, true);
        }
        std::array<std::pair<Owner*, std::uint64_t>, 2> pair{{{&shared, 0}, {&shared, 1}}};
        // Inserting the second key resets capacity and discards the first.
        // The next identical batch must re-observe that first key.
        assert(useTargets(pair, pair.size(), true));
        assert(useTargets(pair, pair.size(), true));
        for (std::uint64_t frame = 61; frame <= 64; ++frame) {
            runtime().frame = frame;
            assert(useTargets(pair, pair.size(), true));
        }
        assert(useTargets(pair, pair.size(), false));
    }

    @autoreleasepool {
        auto& rt = runtime();
        std::array<std::uint8_t, 0x9d90> context{};
        std::array<std::uint8_t, 0x710> pipeline{};
        std::array<std::uint8_t, 0x200> commands{};
        NSObject* texture = [[NSObject alloc] init];
        NSObject* fresh = [[NSObject alloc] init];
        DX11Attachment view{texture, {}}, freshView{fresh, {}};
        const auto put = [&]<class T>(std::size_t offset, T value) {
            std::memcpy(context.data() + offset, &value, sizeof(value));
        };
        put(0x1be8, pipeline.data());
        put(0x1c08, pipeline.data()); // A bound vertex shader, never dereferenced.
        put(0x1a90, commands.data());
        put(0x1ad0, std::uint8_t{1});
        put(0x9588, std::uint32_t{1});
        put(0x95e0, &view);
        dx11GetRTV = [](DX11Attachment* output, void* value) {
            dx11Order.push_back('A');
            *output = *static_cast<DX11Attachment*>(value);
        };
        dx11GetDSV = dx11GetRTV;
        dx11PushState = [](void* value) {
            dx11Order.push_back('P');
            // Native preparation publishes pending attachments before the
            // omission decision. Using the old applied RTV would lose a first draw.
            void* pending = field<void*>(value, 0x95e0);
            std::memcpy(static_cast<std::uint8_t*>(value) + 0x9590, &pending, sizeof(pending));
        };
        dx11Lock = [](void*, std::uint32_t) { dx11Order.push_back('L'); };
        dx11Unlock = [](void*) { dx11Order.push_back('U'); };
        dx11MergeResources = [](void*) { dx11Order.push_back('M'); };
        original.dx11Begin = original.dx11End = reinterpret_cast<std::uintptr_t>(&dx11QueryFixture);
        original.dx11Destroy = reinterpret_cast<std::uintptr_t>(+[](void*, void*) {});
        enabled = true;
        unsigned emitted = 0;
        const auto draw = [&] {
            const unsigned before = emitted;
            dx11Order.clear();
            dx11Draw(context.data(), 3, 1, [&](void*) { ++emitted; dx11Order.push_back('E'); });
            const bool rendered = emitted != before;
            std::vector<char> expected{'L', 'P', 'A'};
            if (field<void*>(context.data(), 0x95d0)) expected.push_back('A');
            if (rendered) expected.push_back('E');
            expected.insert(expected.end(), {'M', 'U'});
            assert(dx11Order == expected);
            return rendered;
        };
        for (std::uint64_t frame = 1; frame <= 5; ++frame) {
            rt.frame = frame;
            assert(draw());
        }
        {
            std::lock_guard lock(rt.mutex);
            auto warmDraw = std::async(std::launch::async, draw);
            ready(warmDraw);
            assert(warmDraw.get());
        }
        std::atomic_ref<std::uint8_t>(pipeline[0x38]).store(1);
        auto* target = static_cast<YAAGLDX11TargetHistory*>(
            objc_getAssociatedObject(texture, &dx11HistoryKey));
        assert(target);
        registryProgressWhileHistoryBlocked(target->mutex, [&] { return !draw(); });
        for (unsigned repeat = 0; repeat < 20; ++repeat) assert(!draw());
        rt.frame = 6;
        assert(!draw()); // The last emitted draw was in epoch 5.
        rt.frame = 7;
        assert(draw()); // A skipped epoch does not renew eligibility.
        std::atomic_ref<std::uint8_t>(pipeline[0x38]).store(0);
        assert(draw()); // Completion, or an unscheduled variant, must make progress.
        for (std::uint64_t frame = 8; frame <= 12; ++frame) {
            rt.frame = frame;
            assert(draw());
        }
        std::atomic_ref<std::uint8_t>(pipeline[0x38]).store(1);
        for (const auto offset : {0x1c10, 0x1c18, 0x1c20, 0x9d30}) {
            put(offset, pipeline.data());
            assert(draw());
            put(offset, static_cast<void*>(nullptr));
        }
        put(0x9d3c, std::uint8_t{1});
        assert(draw());
        put(0x9d3c, std::uint8_t{0});
        for (std::size_t stage = 0; stage < 5; ++stage) {
            put(0x94f8 + stage * 8, std::uint64_t{1});
            assert(draw());
            put(0x94f8 + stage * 8, std::uint64_t{0});
        }
        int query1, query2, timestamp;
        dx11Begin(context.data(), &query1);
        dx11Begin(context.data(), &query2);
        dx11End(context.data(), &timestamp);
        assert(draw()); // End-only queries cannot clear another active query.
        dx11End(context.data(), &query1);
        assert(draw());
        dx11End(context.data(), &query2);
        assert(!draw());
        // Every native descriptor byte participates, including its final plane
        // bytes; a recurrent slice must not promote a newly selected subresource.
        for (auto& byte : view.subresource) {
            byte = 1;
            assert(draw());
            byte = 0;
        }
        put(0x95d0, &freshView);
        assert(draw()); // A recurrent color target cannot promote a new depth target.
        put(0x95d0, static_cast<void*>(nullptr));
        put(0x95e0, &freshView);
        assert(draw());
        assert(draw()); // Repeats in one epoch cannot promote the fresh target.
        put(0x95e0, &view);
        assert(!draw());
        rt.frame = 14;
        assert(draw()); // A missing presentation epoch expires recurrence.
        for (std::uint64_t frame = 15; frame <= 18; ++frame) {
            rt.frame = frame;
            assert(draw());
        }
        assert(!draw());
        dx11Begin(context.data(), &query1);
        assert(draw());
        dx11Destroy(context.data(), nullptr);
        assert(!draw()); // A retired context cannot leave a stale active query.
        put(0x95e0, &freshView);
        put(0x95d0, &freshView);
        for (std::uint64_t frame = 20; frame <= 24; ++frame) {
            rt.frame = frame;
            for (const auto size : {std::pair{0u, 1u}, std::pair{3u, 0u}}) {
                dx11Order.clear();
                dx11Draw(context.data(), size.first, size.second,
                         [](void*) { dx11Order.push_back('E'); });
                assert((dx11Order == std::vector<char>{'L', 'P', 'E', 'M', 'U'}));
            }
        }
        assert(draw()); // Zero-count draws cannot establish target recurrence.
        enabled = false;
        [fresh release];
        [texture release];
    }

    RootDescription root{};
    root.version = 2;
    assert(rootHasNoUAV(&root));
    Parameter parameters[2]{};
    parameters[0].type = 1;
    parameters[1].type = 3;
    root.count = 2;
    root.parameters = parameters;
    root.samplerCount = 1; // Static samplers cannot grant UAV access.
    assert(rootHasNoUAV(&root));
    for (const auto flag : {0x80u, 0x400u, 0x800u, 0x1000u}) {
        root.flags = flag;
        assert(!rootHasNoUAV(&root));
    }
    root.flags = 0;
    for (const auto type : {4u, 5u}) {
        parameters[1].type = type;
        assert(!rootHasNoUAV(&root));
    }
    Range ranges[3]{};
    ranges[0].type = 0;
    ranges[1].type = 2;
    ranges[2].type = 3;
    parameters[1].type = 0;
    parameters[1].data.table = {3, 0, ranges};
    assert(rootHasNoUAV(&root));
    ranges[2].type = 1;
    assert(!rootHasNoUAV(&root));
    ranges[2].type = 4;
    assert(!rootHasNoUAV(&root));
    ranges[2].type = 3;
    parameters[1].data.table.ranges = nullptr;
    assert(!rootHasNoUAV(&root));
    parameters[1].data.table.ranges = ranges;
    root.version = 1;
    assert(!rootHasNoUAV(&root));
    root.version = 2;
    root.count = 65;
    assert(!rootHasNoUAV(&root));
    root.count = 1;
    root.parameters = nullptr;
    assert(!rootHasNoUAV(&root));
    assert(!rootHasNoUAV(nullptr));
}
