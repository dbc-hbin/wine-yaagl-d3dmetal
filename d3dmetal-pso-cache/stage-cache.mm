#import "stage-cache.hpp"

#if __has_feature(objc_arc)
#error "d3dmetal-pso-cache must be compiled with Objective-C automatic reference counting disabled"
#endif

#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <mutex>
#include <thread>
#include <utility>
#include <vector>

namespace {

struct Flight final {
    std::condition_variable completed;
    std::thread::id owner;
    std::size_t waiters = 0;
    bool running = true;
    bool succeeded = false;
};

struct LibraryFlight final {
    void* stageResult;
    id metalDevice;
    std::shared_ptr<Flight> state;
};

struct LibraryCallScope final {
    LibraryCallScope() noexcept { ++depth; }
    ~LibraryCallScope() { --depth; }

    static thread_local unsigned depth;
};

thread_local unsigned LibraryCallScope::depth = 0;

std::mutex gLibraryMutex;
std::vector<LibraryFlight> gLibraryFlights;

std::vector<LibraryFlight>::iterator findLibraryFlight(void* stageResult, id metalDevice) {
    for (auto entry = gLibraryFlights.begin(); entry != gLibraryFlights.end(); ++entry) {
        if (entry->stageResult == stageResult && entry->metalDevice == metalDevice) {
            return entry;
        }
    }
    return gLibraryFlights.end();
}

void eraseLibraryFlight(const std::shared_ptr<Flight>& state) {
    for (auto entry = gLibraryFlights.begin(); entry != gLibraryFlights.end(); ++entry) {
        if (entry->state == state) {
            gLibraryFlights.erase(entry);
            return;
        }
    }
}

} // namespace

namespace yaagl::pso {

id getAndRetainLibrarySingleFlight(
    void* stageResult,
    id metalDevice,
    StageGetAndRetainLibraryEntry original) {
    if (stageResult == nullptr || original == nullptr) {
        return original == nullptr ? nil : original(stageResult, metalDevice);
    }

    const auto* librarySlot = reinterpret_cast<const std::uintptr_t*>(
        static_cast<std::uint8_t*>(stageResult) + 0x178);
    id completed = reinterpret_cast<id>(__atomic_load_n(librarySlot, __ATOMIC_ACQUIRE));
    if (completed != nil) {
        return original(stageResult, metalDevice);
    }

    std::shared_ptr<Flight> owned;
    {
        std::unique_lock lock(gLibraryMutex);
        for (;;) {
            auto entry = findLibraryFlight(stageResult, metalDevice);
            if (entry == gLibraryFlights.end()) {
                owned = std::make_shared<Flight>();
                owned->owner = std::this_thread::get_id();
                gLibraryFlights.push_back({stageResult, metalDevice, owned});
                break;
            }
            std::shared_ptr<Flight> state = entry->state;
            // Nested library creation can cross-call a flight whose owner is
            // waiting on this thread. Let native code resolve that rare cycle.
            if (state->running && (state->owner == std::this_thread::get_id()
                                   || LibraryCallScope::depth != 0)) {
                lock.unlock();
                return original(stageResult, metalDevice);
            }
            ++state->waiters;
            state->completed.wait(lock, [&] { return !state->running; });
            --state->waiters;
            if (state->succeeded) {
                lock.unlock();
                return original(stageResult, metalDevice);
            }
            if (!state->running) {
                state->running = true;
                state->owner = std::this_thread::get_id();
                state->succeeded = false;
                owned = std::move(state);
                break;
            }
        }
    }

    LibraryCallScope callScope;
    id result = nil;
    bool returned = false;
    try {
        @try {
            result = original(stageResult, metalDevice);
            returned = true;
        } @catch (...) {
            std::lock_guard lock(gLibraryMutex);
            owned->running = false;
            owned->succeeded = false;
            if (owned->waiters == 0) eraseLibraryFlight(owned);
            owned->completed.notify_all();
            @throw;
        }
    } catch (...) {
        if (!returned) {
            // Objective-C exceptions have already completed the flight.
            throw;
        }
        std::lock_guard lock(gLibraryMutex);
        owned->running = false;
        owned->succeeded = false;
        if (owned->waiters == 0) eraseLibraryFlight(owned);
        owned->completed.notify_all();
        throw;
    }

    {
        std::lock_guard lock(gLibraryMutex);
        owned->running = false;
        owned->succeeded = result != nil;
        if (result != nil || owned->waiters == 0) eraseLibraryFlight(owned);
        owned->completed.notify_all();
    }
    return result;
}

} // namespace yaagl::pso
