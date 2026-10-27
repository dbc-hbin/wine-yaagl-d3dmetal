#include "../d3dmetal-sidecar/low-latency-state.hpp"

#include <cassert>
#include <chrono>
#include <future>

using yaagl::latency::Limiter;
using namespace std::chrono_literals;

static void ready(std::future<bool>& result) {
    assert(result.wait_for(5s) == std::future_status::ready);
}

struct Fixture {
    Limiter limiter;
    int device = 0;
    int chain = 0;

    explicit Fixture(bool force = false) : limiter(force) {
        if (!force) limiter.configure(&device, true);
        assert(!limiter.enabled(&device));
        assert(!limiter.sleep(&device)); // No chain: do not stall a non-presenting device.
        auto warmup = limiter.begin(&device, &chain);
        limiter.finish(warmup, true);
        limiter.complete(&chain);
        assert(limiter.enabled(&device));
    }

    std::future<bool> blocked() {
        auto result = std::async(std::launch::async, [&] { return limiter.sleep(&device); });
        assert(limiter.waitForWaiters(&device, 1, 5s));
        assert(result.wait_for(0s) == std::future_status::timeout);
        return result;
    }

    std::future<bool> blockedPresent() {
        auto first = limiter.begin(&device, &chain);
        limiter.finish(first, true);
        auto second = limiter.begin(&device, &chain);
        auto result = std::async(std::launch::async, [&, second] {
            limiter.finish(second, true);
            return true;
        });
        assert(limiter.waitForWaiters(&device, 1, 5s));
        assert(result.wait_for(0s) == std::future_status::timeout);
        return result;
    }
};

static void capAndCompletion() {
    Fixture f;
    assert(f.limiter.sleep(&f.device));
    assert(f.limiter.sleep(&f.device));
    auto waiting = f.blocked();
    // Reservations consume capacity even when the render thread has not submitted.
    assert(f.limiter.snapshot(&f.device).reservations == 2);
    auto a = f.limiter.begin(&f.device, &f.chain);
    f.limiter.finish(a, true);
    assert(waiting.wait_for(0s) == std::future_status::timeout);
    auto b = f.limiter.begin(&f.device, &f.chain);
    f.limiter.finish(b, true);
    assert(f.limiter.snapshot(&f.device).pending == 2);
    assert(waiting.wait_for(0s) == std::future_status::timeout);
    f.limiter.complete(&f.chain);
    ready(waiting);
    assert(waiting.get());
    auto state = f.limiter.snapshot(&f.device);
    assert(state.pending == 1 && state.reservations == 1);
    f.limiter.complete(&f.chain);
}

static void modeAndDestructionWake() {
    {
        Fixture f;
        assert(f.limiter.sleep(&f.device));
        assert(f.limiter.sleep(&f.device));
        auto a = f.limiter.begin(&f.device, &f.chain);
        f.limiter.finish(a, true);
        auto waiting = f.blocked();
        f.limiter.configure(&f.device, false);
        ready(waiting);
        assert(!waiting.get());
        assert(!f.limiter.enabled(&f.device));
        assert(f.limiter.snapshot(&f.device).pending == 1);
        f.limiter.configure(&f.device, true);
        assert(f.limiter.snapshot(&f.device).pending == 1);
        f.limiter.complete(&f.chain);
    }
    {
        Fixture f;
        assert(f.limiter.sleep(&f.device));
        assert(f.limiter.sleep(&f.device));
        auto waiting = f.blocked();
        f.limiter.destroy(&f.device);
        ready(waiting);
        assert(!waiting.get());
    }
    {
        Fixture f;
        assert(f.limiter.sleep(&f.device));
        assert(f.limiter.sleep(&f.device));
        auto waiting = f.blocked();
        f.limiter.retire(&f.chain);
        ready(waiting);
        assert(!waiting.get());
    }
}

static void callbacksAndRollback() {
    Fixture f;
    assert(f.limiter.sleep(&f.device));
    auto a = f.limiter.begin(&f.device, &f.chain);
    f.limiter.complete(&f.chain); // Completion can precede native Present's return.
    f.limiter.finish(a, true);
    assert(f.limiter.snapshot(&f.device).pending == 0);

    assert(f.limiter.sleep(&f.device));
    assert(f.limiter.sleep(&f.device));
    auto successful = f.limiter.begin(&f.device, &f.chain);
    f.limiter.finish(successful, true);
    auto failed = f.limiter.begin(&f.device, &f.chain);
    auto waiting = f.blocked();
    f.limiter.complete(&f.chain); // Other Present completes before failure returns.
    f.limiter.finish(failed, false);
    f.limiter.finish(failed, false); // Idempotent even if a token is shared.
    ready(waiting);
    assert(waiting.get());
    assert(f.limiter.snapshot(&f.device).pending == 0);
}

static void ambiguousPresent() {
    {
        Fixture f;
        assert(f.limiter.sleep(&f.device));
        assert(f.limiter.sleep(&f.device));
        auto waiting = f.blocked();
        auto generated = f.limiter.begin(&f.device, &f.chain, false);
        ready(waiting);
        assert(!waiting.get());
        assert(!f.limiter.enabled(&f.device));
        assert(f.limiter.snapshot(&f.device).reservations == 0);
        f.limiter.finish(generated, true);
        f.limiter.complete(&f.chain);
    }
    {
        Fixture f;
        assert(f.limiter.sleep(&f.device));
        auto a = f.limiter.begin(&f.device, &f.chain);
        f.limiter.finish(a, true);
        auto unmatched = f.limiter.begin(&f.device, &f.chain);
        assert(!f.limiter.enabled(&f.device));
        assert(!f.limiter.sleep(&f.device));
        f.limiter.finish(unmatched, true);
        f.limiter.complete(&f.chain);
        f.limiter.complete(&f.chain);
    }
    {
        Fixture f;
        int other = 0;
        assert(f.limiter.sleep(&f.device));
        assert(f.limiter.sleep(&f.device));
        auto waiting = f.blocked();
        auto second = f.limiter.begin(&f.device, &other);
        ready(waiting);
        assert(!waiting.get());
        assert(!f.limiter.enabled(&f.device));
        f.limiter.finish(second, true);
        f.limiter.complete(&other);
        f.limiter.retire(&other);
        assert(f.limiter.enabled(&f.device));
    }
    {
        Fixture f;
        assert(f.limiter.sleep(&f.device));
        assert(f.limiter.sleep(&f.device));
        auto waiting = f.blocked();
        auto first = f.limiter.begin(&f.device, &f.chain);
        auto overlapping = f.limiter.begin(&f.device, &f.chain);
        ready(waiting);
        assert(!waiting.get());
        assert(!f.limiter.enabled(&f.device));
        f.limiter.finish(first, true);
        f.limiter.finish(overlapping, false);
        f.limiter.complete(&f.chain);
        f.limiter.retire(&f.chain);
        auto replacement = f.limiter.begin(&f.device, &f.chain);
        assert(!f.limiter.enabled(&f.device));
        f.limiter.finish(replacement, true);
        f.limiter.complete(&f.chain);
    }
}

static void testPresentAdmission() {
    {
        Fixture f;
        f.limiter.testPresent(&f.device); // A probe before Sleep owns no admission.
        assert(f.limiter.sleep(&f.device));
        f.limiter.testPresent(&f.device);
        auto real = f.limiter.begin(&f.device, &f.chain);
        f.limiter.finish(real, true);
        f.limiter.complete(&f.chain);
        assert(f.limiter.enabled(&f.device));
        assert(f.limiter.sleep(&f.device)); // TEST followed by real preserves pacing.
    }
    {
        Fixture f;
        assert(f.limiter.sleep(&f.device));
        f.limiter.testPresent(&f.device);
        auto next = std::async(std::launch::async, [&] { return f.limiter.sleep(&f.device); });
        ready(next);
        assert(!next.get());
        assert(!f.limiter.enabled(&f.device));
        assert(f.limiter.snapshot(&f.device).reservations == 0);
    }
    {
        Fixture f;
        assert(f.limiter.sleep(&f.device));
        auto submitted = f.limiter.begin(&f.device, &f.chain);
        f.limiter.finish(submitted, true);
        assert(f.limiter.sleep(&f.device));
        auto waiting = f.blocked();
        f.limiter.testPresent(&f.device);
        ready(waiting);
        assert(!waiting.get());
        assert(f.limiter.snapshot(&f.device).pending == 1);
        f.limiter.complete(&f.chain);
    }
    {
        Fixture f;
        assert(f.limiter.sleep(&f.device));
        assert(f.limiter.sleep(&f.device));
        auto waiting = f.blocked();
        f.limiter.testPresent(&f.device);
        ready(waiting);
        assert(!waiting.get());
        assert(!f.limiter.enabled(&f.device));
        assert(f.limiter.snapshot(&f.device).reservations == 0);
    }
}

static void isolationAndReuse() {
    Fixture f;
    int device2 = 0, chain2 = 0;
    f.limiter.configure(&device2, true);
    auto warmup = f.limiter.begin(&device2, &chain2);
    f.limiter.finish(warmup, true);
    f.limiter.complete(&chain2);
    assert(f.limiter.sleep(&f.device));
    assert(f.limiter.sleep(&f.device));
    auto waiting = f.blocked();
    assert(f.limiter.sleep(&device2));
    auto other = f.limiter.begin(&device2, &chain2);
    f.limiter.finish(other, true);
    f.limiter.complete(&chain2);
    assert(waiting.wait_for(0s) == std::future_status::timeout);
    f.limiter.configure(&f.device, false);
    ready(waiting);
    assert(!waiting.get());

    f.limiter.configure(&f.device, true);
    assert(f.limiter.sleep(&f.device));
    auto old = f.limiter.begin(&f.device, &f.chain);
    f.limiter.retire(&f.chain);
    auto replacement = f.limiter.begin(&f.device, &f.chain);
    assert(f.limiter.snapshot(&f.device).pending == 1);
    f.limiter.finish(old, false);
    assert(f.limiter.snapshot(&f.device).pending == 1);
    f.limiter.finish(replacement, true);
    f.limiter.complete(&f.chain);

    assert(f.limiter.sleep(&f.device));
    old = f.limiter.begin(&f.device, &f.chain);
    f.limiter.destroy(&f.device);
    f.limiter.configure(&f.device, true);
    replacement = f.limiter.begin(&f.device, &f.chain);
    f.limiter.finish(old, false);
    assert(f.limiter.snapshot(&f.device).pending == 1);
    f.limiter.finish(replacement, true);
    f.limiter.complete(&f.chain);
}

static void forcedAdmission() {
    Fixture f(true); // No configure or Reflex call is required.
    auto waiting = f.blockedPresent();
    assert(f.limiter.snapshot(&f.device).pending == 2);
    assert(f.limiter.snapshot(&f.device).reservations == 0);
    assert(!f.limiter.sleep(&f.device)); // Off-mode Sleep cannot add a second wait.
    f.limiter.complete(&f.chain);
    ready(waiting);
    assert(waiting.get());
    assert(f.limiter.snapshot(&f.device).pending == 1);
    f.limiter.complete(&f.chain);

    for (unsigned i = 0; i < 4; ++i) {
        f.limiter.testPresent(&f.device);
        assert(!f.limiter.sleep(&f.device));
    }
    assert(f.limiter.enabled(&f.device));
    assert(f.limiter.snapshot(&f.device).pending == 0);

    auto a = f.limiter.begin(&f.device, &f.chain);
    f.limiter.finish(a, true);
    auto b = f.limiter.begin(&f.device, &f.chain);
    f.limiter.finish(b, true, false); // DXGI_PRESENT_DO_NOT_WAIT.
    assert(f.limiter.snapshot(&f.device).pending == 2);
    auto failed = f.limiter.begin(&f.device, &f.chain);
    f.limiter.finish(failed, false); // Nonadvancing calls must not wait either.
    assert(f.limiter.snapshot(&f.device).pending == 2);
    f.limiter.complete(&f.chain);
    f.limiter.complete(&f.chain);
    auto early = f.limiter.begin(&f.device, &f.chain);
    f.limiter.complete(&f.chain);
    f.limiter.finish(early, true);
    assert(f.limiter.snapshot(&f.device).pending == 0);
}

static void forcedReflexHandoff() {
    Fixture f(true);
    auto present = f.blockedPresent();
    f.limiter.configure(&f.device, true);
    assert(present.wait_for(0s) == std::future_status::timeout); // Setting is not Sleep.
    auto input = std::async(std::launch::async, [&] { return f.limiter.sleep(&f.device); });
    ready(present); // Sleep takes over without waiting for another GPU signal.
    assert(present.get());
    assert(f.limiter.waitForWaiters(&f.device, 1, 5s));
    assert(input.wait_for(0s) == std::future_status::timeout);
    f.limiter.complete(&f.chain);
    ready(input);
    assert(input.get());
    assert(f.limiter.snapshot(&f.device).reservations == 1);
    auto next = f.limiter.begin(&f.device, &f.chain);
    auto finished = std::async(std::launch::async, [&] {
        f.limiter.finish(next, true);
        return true;
    });
    ready(finished); // Cooperative Present must not duplicate the Sleep wait.
    assert(finished.get());
    f.limiter.complete(&f.chain);
    f.limiter.complete(&f.chain);

    // A stale enabled setting cannot suppress forced pacing after Sleep stops.
    auto resumed = f.blockedPresent();
    assert(f.limiter.enabled(&f.device));
    f.limiter.complete(&f.chain);
    ready(resumed);
    assert(resumed.get());
    f.limiter.complete(&f.chain);
    assert(f.limiter.sleep(&f.device)); // A later valid call can take over again.
    f.limiter.configure(&f.device, false);
    assert(f.limiter.snapshot(&f.device).reservations == 0);
    auto off = f.blockedPresent(); // Runtime force remains selected when Reflex is off.
    f.limiter.complete(&f.chain);
    ready(off);
    assert(off.get());
    f.limiter.complete(&f.chain);
}

static void forcedInitialHandoffWithoutReservation() {
    Fixture f(true);
    auto previous = f.blockedPresent();
    f.limiter.configure(&f.device, true);
    auto input = std::async(std::launch::async, [&] { return f.limiter.sleep(&f.device); });
    ready(previous);
    assert(previous.get());
    assert(f.limiter.waitForWaiters(&f.device, 1, 5s));
    assert(input.wait_for(0s) == std::future_status::timeout);
    // A serial render thread can get here before the first Sleep owns a slot.
    auto next = f.limiter.begin(&f.device, &f.chain);
    auto rendering = std::async(std::launch::async, [&] {
        f.limiter.finish(next, true);
        return true;
    });
    ready(input); // Missing admission cancels the initial handoff, not GPU work.
    assert(!input.get());
    assert(f.limiter.waitForWaiters(&f.device, 1, 5s));
    assert(f.limiter.snapshot(&f.device).pending == 3);
    assert(f.limiter.snapshot(&f.device).reservations == 0);
    f.limiter.complete(&f.chain);
    assert(rendering.wait_for(0s) == std::future_status::timeout);
    f.limiter.complete(&f.chain);
    ready(rendering);
    assert(rendering.get());
    assert(f.limiter.snapshot(&f.device).pending == 1);
    f.limiter.complete(&f.chain);
}

static void forcedCancellationAndIsolation() {
    for (unsigned action = 0; action < 5; ++action) {
        Fixture f(true);
        auto waiting = f.blockedPresent();
        int other = 0;
        if (action == 0) f.limiter.exclude(&f.device);
        if (action == 1) f.limiter.retire(&f.chain);
        if (action == 2) f.limiter.destroy(&f.device);
        if (action == 3) {
            auto second = f.limiter.begin(&f.device, &other);
            f.limiter.finish(second, true);
        }
        if (action == 4) {
            auto overlapping = f.limiter.begin(&f.device, &f.chain);
            f.limiter.finish(overlapping, false);
        }
        ready(waiting);
        assert(waiting.get());
        assert(!f.limiter.enabled(&f.device));
    }
    {
        Fixture f(true);
        auto waiting = f.blockedPresent();
        int device = 0, chain = 0;
        auto independent = f.limiter.begin(&device, &chain);
        f.limiter.finish(independent, true);
        f.limiter.complete(&chain);
        assert(waiting.wait_for(0s) == std::future_status::timeout);
        f.limiter.complete(&f.chain);
        ready(waiting);
        assert(waiting.get());
        f.limiter.complete(&f.chain);
        auto generated = f.limiter.begin(&f.device, &f.chain, false);
        f.limiter.finish(generated, true);
        assert(!f.limiter.enabled(&f.device)); // Force does not override known FG exclusion.
        f.limiter.complete(&f.chain);
    }
}

int main() {
    capAndCompletion();
    modeAndDestructionWake();
    callbacksAndRollback();
    ambiguousPresent();
    testPresentAdmission();
    isolationAndReuse();
    forcedAdmission();
    forcedReflexHandoff();
    forcedInitialHandoffWithoutReservation();
    forcedCancellationAndIsolation();
}
