#pragma once

#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <utility>

namespace yaagl::latency {

// The native owner must retire a chain only after its completion callbacks drain.
// A non-advancing Present must not produce its own completion callback. Callbacks
// for other Presents can race with begin/finish and can arrive out of order.
class Limiter final {
    struct Device;
    struct Chain {
        std::shared_ptr<Device> device;
        std::size_t pending = 0;
        std::size_t inProgress = 0;
        bool alive = true;
    };
    struct Device {
        bool requested = false;
        bool alive = true;
        bool invalid = false;
        bool synchronized = false;
        bool reflex = false;
        bool testedAdmission = false;
        std::size_t chains = 0;
        std::size_t pending = 0;
        std::size_t reservations = 0;
        std::size_t waiters = 0;
        std::size_t epoch = 0;
        bool sleepCalled = false;
        std::uint32_t sleepIntervalUs = 0;
    };

public:
    struct Present {
    private:
        friend class Limiter;
        explicit Present(std::shared_ptr<Chain> owner) : chain(std::move(owner)) {}
        std::shared_ptr<Chain> chain;
        bool finished = false;
    };
    using Token = std::shared_ptr<Present>;

    struct Snapshot {
        bool active = false;
        std::size_t pending = 0;
        std::size_t reservations = 0;
        std::size_t waiters = 0;
        std::size_t chains = 0;
        bool sleepCalled = false;
        std::uint32_t sleepIntervalUs = 0;
    };

    explicit Limiter(bool force = false) : force_(force) {}
    Limiter(const Limiter&) = delete;
    Limiter& operator=(const Limiter&) = delete;

    void configure(void* device, bool enabled) {
        if (!device) return;
        std::lock_guard lock(mutex_);
        auto d = deviceFor(device);
        if (d->requested == enabled) return;
        d->requested = enabled;
        cancel(*d);
        changed_.notify_all();
    }

    bool enabled(void* device) {
        std::lock_guard lock(mutex_);
        auto it = devices_.find(device);
        return it != devices_.end() && active(*it->second);
    }

    bool sleep(void* device) {
        const auto started = std::chrono::steady_clock::now();
        std::unique_lock lock(mutex_);
        auto it = devices_.find(device);
        if (it == devices_.end()) return false;
        auto d = it->second;
        const auto recorded = [&](bool reserved) {
            const auto elapsed = std::chrono::duration_cast<std::chrono::microseconds>(
                std::chrono::steady_clock::now() - started).count();
            d->sleepCalled = true;
            d->sleepIntervalUs = elapsed > std::numeric_limits<std::uint32_t>::max()
                ? std::numeric_limits<std::uint32_t>::max() : static_cast<std::uint32_t>(elapsed);
            return reserved;
        };
        if (!active(*d) || !d->requested) return recorded(false);
        if (force_ && !d->reflex) {
            // A setting alone does not prove that the game calls Sleep.
            // Transfer ownership only at the actual input admission call.
            cancel(*d);
            d->reflex = true;
            changed_.notify_all();
        }
        const auto epoch = d->epoch;
        if (d->pending + d->reservations >= 2 && !d->testedAdmission) {
            ++d->waiters;
            changed_.notify_all();
            changed_.wait(lock, [&] {
                return d->epoch != epoch || !active(*d) || d->testedAdmission ||
                    d->pending + d->reservations < 2;
            });
            --d->waiters;
            changed_.notify_all();
        }
        if (d->epoch != epoch || !active(*d)) return recorded(false);
        if (d->testedAdmission) {
            // A new input iteration followed TEST without a real Present.
            // There will be no GPU completion to retire that reservation.
            invalidate(*d);
            changed_.notify_all();
            return recorded(false);
        }
        ++d->reservations;
        d->synchronized = true;
        return recorded(true);
    }

    void exclude(void* device) {
        if (!device) return;
        std::lock_guard lock(mutex_);
        invalidate(*deviceFor(device));
        changed_.notify_all();
    }

    void testPresent(void* device) {
        std::lock_guard lock(mutex_);
        const auto it = devices_.find(device);
        if (it == devices_.end() || !active(*it->second)) return;
        auto& d = *it->second;
        if (d.reservations > 1) {
            // Without frame IDs, multiple outstanding CPU admissions cannot
            // identify which TEST may be followed by a real Present.
            invalidate(d);
        } else if (d.reservations) {
            d.testedAdmission = true;
        }
        changed_.notify_all();
    }

    Token begin(void* device, void* swapchain, bool eligible = true) {
        if (!device || !swapchain) return {};
        std::lock_guard lock(mutex_);
        auto d = deviceFor(device);
        auto it = chains_.find(swapchain);
        std::shared_ptr<Chain> chain;
        if (it == chains_.end()) {
            chain = std::make_shared<Chain>();
            chain->device = d;
            chains_.emplace(swapchain, chain);
            ++d->chains;
            cancel(*d);
        } else {
            chain = it->second;
            if (chain->device != d) {
                invalidate(*d);
                invalidate(*chain->device);
                changed_.notify_all();
                return {};
            }
        }
        // A shared native counter cannot attribute advancement across
        // overlapping Present calls, including failed/empty calls.
        if (chain->inProgress || !eligible) invalidate(*d);
        ++chain->inProgress;
        if (active(*d) && eligible && (!force_ || d->reflex)) {
            if (d->reservations) {
                --d->reservations;
                d->testedAdmission = false;
            }
            else if (force_ || d->synchronized) {
                if (force_) {
                    // No reservation, including during the first Sleep's wait:
                    // restore Present pacing and cancel that input admission.
                    cancel(*d);
                } else invalidate(*d);
            }
        }
        ++chain->pending;
        ++d->pending;
        changed_.notify_all();
        return Token(new Present(std::move(chain)));
    }

    void finish(const Token& token, bool advanced, bool allowWait = true) {
        if (!token) return;
        std::unique_lock lock(mutex_);
        if (token->finished) return;
        token->finished = true;
        auto& chain = *token->chain;
        auto& d = *chain.device;
        if (!advanced && chain.alive && d.alive) {
            if (chain.pending) {
                --chain.pending;
                --d.pending;
            } else {
                // Violated completion contract: do not debit another generation.
                invalidate(d);
            }
            changed_.notify_all();
        }
        if (advanced && allowWait && chain.alive && forced(d) && d.pending >= 2) {
            // Leave one slot for the next frame's input/render work. Native
            // Present has returned and released its own lock before this wait.
            ++d.waiters;
            changed_.notify_all();
            changed_.wait(lock, [&] {
                return !chain.alive || !forced(d) || d.pending < 2;
            });
            --d.waiters;
            changed_.notify_all();
        }
        --chain.inProgress;
    }

    void complete(void* swapchain) {
        std::lock_guard lock(mutex_);
        auto it = chains_.find(swapchain);
        if (it == chains_.end()) return;
        auto& chain = *it->second;
        auto& d = *chain.device;
        if (chain.pending) {
            --chain.pending;
            --d.pending;
        } else {
            invalidate(d);
        }
        changed_.notify_all();
    }

    void retire(void* swapchain) {
        std::lock_guard lock(mutex_);
        auto it = chains_.find(swapchain);
        if (it == chains_.end()) return;
        auto chain = it->second;
        auto& d = *chain->device;
        chain->alive = false;
        d.pending -= chain->pending;
        --d.chains;
        chains_.erase(it);
        cancel(d);
        changed_.notify_all();
    }

    void destroy(void* device) {
        std::lock_guard lock(mutex_);
        auto it = devices_.find(device);
        if (it == devices_.end()) return;
        auto d = it->second;
        d->alive = false;
        cancel(*d);
        for (auto chain = chains_.begin(); chain != chains_.end();) {
            if (chain->second->device == d) {
                chain->second->alive = false;
                chain = chains_.erase(chain);
            } else ++chain;
        }
        devices_.erase(it);
        changed_.notify_all();
    }

    Snapshot snapshot(void* device) {
        std::lock_guard lock(mutex_);
        auto it = devices_.find(device);
        if (it == devices_.end()) return {};
        auto& d = *it->second;
        return {active(d), d.pending, d.reservations, d.waiters, d.chains,
            d.sleepCalled, d.sleepIntervalUs};
    }

    // Allows tests/diagnostics to subscribe before triggering a wakeup.
    bool waitForWaiters(void* device, std::size_t count, std::chrono::milliseconds timeout) {
        std::unique_lock lock(mutex_);
        auto it = devices_.find(device);
        if (it == devices_.end()) return false;
        auto d = it->second;
        return changed_.wait_for(lock, timeout, [&] { return !d->alive || d->waiters >= count; }) &&
            d->alive && d->waiters >= count;
    }

private:
    bool active(const Device& d) const {
        return d.alive && (d.requested || force_) && !d.invalid && d.chains == 1;
    }
    bool forced(const Device& d) const {
        return force_ && !d.reflex && active(d);
    }
    static void cancel(Device& d) {
        ++d.epoch;
        d.reservations = 0;
        d.synchronized = false;
        d.reflex = false;
        d.testedAdmission = false;
    }
    static void invalidate(Device& d) {
        d.invalid = true;
        cancel(d);
    }
    std::shared_ptr<Device> deviceFor(void* key) {
        auto& d = devices_[key];
        if (!d) d = std::make_shared<Device>();
        return d;
    }

    const bool force_;
    std::mutex mutex_;
    std::condition_variable changed_;
    std::map<void*, std::shared_ptr<Device>> devices_;
    std::map<void*, std::shared_ptr<Chain>> chains_;
};

} // namespace yaagl::latency
