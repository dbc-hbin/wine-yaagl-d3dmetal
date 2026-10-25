#pragma once

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <map>
#include <mutex>
#include <utility>

namespace yaagl::pso::async_pipeline {

struct Key {
    std::uint32_t flags;
    std::uint64_t formats;
    bool operator==(const Key&) const noexcept = default;
    bool operator<(const Key& other) const noexcept {
        return flags < other.flags || (flags == other.flags && formats < other.formats);
    }
};

enum class Status { Pending, Ready, Failed };
struct Result {
    Status status = Status::Pending;
    void* pipeline = nullptr; // Borrowed from the native cache, never retained here.
};

// One owner per native graphics PSO. No compilation runs under the state mutex.
// Native destruction stops admission and drains this owner before freeing the PSO.
class State final {
public:
    using Compile = std::function<void*(Key)>;
    using Enqueue = std::function<void(std::function<void()>)>;

    State(Compile compile, Enqueue enqueue)
        : compile_(std::move(compile)), enqueue_(std::move(enqueue)) {}
    State(const State&) = delete;
    State& operator=(const State&) = delete;

    std::uint64_t readyRevision(Key key) {
        // A negative precheck avoids an extra mutex acquisition on pending
        // draws. A zero observation is never sufficient: recheck under the lock.
        if (active_.load(std::memory_order_relaxed)) return 0;
        std::lock_guard lock(mutex_);
        // Another variant can publish owner-wide native failure. Do not bypass
        // the existing unsafe-draw drain while any owner job is outstanding.
        if (stopped_ || active_.load(std::memory_order_relaxed)) return 0;
        const auto entry = results_.find(key);
        return entry != results_.end() && entry->second.status == Status::Ready
            ? revision_.load(std::memory_order_relaxed) : 0;
    }

    bool matchesRevision(std::uint64_t revision) const noexcept {
        return revision && revision_.load(std::memory_order_acquire) == revision;
    }

    Result request(Key key, bool asynchronous) {
        {
            std::unique_lock lock(mutex_);
            if (stopped_) return {Status::Failed, nullptr};
            auto [entry, inserted] = results_.try_emplace(key);
            if (!inserted) {
                if (!asynchronous) {
                    completed_.wait(lock, [&] { return entry->second.status != Status::Pending; });
                }
                return entry->second;
            }
            // Invalidate cached readiness before any new owner work can run,
            // including a variant that can publish owner-wide native failure.
            revision_.fetch_add(1, std::memory_order_release);
            active_.fetch_add(1, std::memory_order_relaxed);
        }
        const auto work = [this, key] {
            void* pipeline = compile_(key);
            {
                std::lock_guard lock(mutex_);
                results_.at(key) = {pipeline ? Status::Ready : Status::Failed, pipeline};
                active_.fetch_sub(1, std::memory_order_relaxed);
                completed_.notify_all();
            }
        };
        if (asynchronous) {
            enqueue_(work);
            // A very fast compiler may already have completed. The next draw
            // observes it; this draw is allowed to be skipped without waiting.
            return {Status::Pending, nullptr};
        }
        work();
        std::lock_guard lock(mutex_);
        return results_.at(key);
    }

    void stop() {
        std::lock_guard lock(mutex_);
        stopped_ = true;
        revision_.fetch_add(1, std::memory_order_release);
    }

    void wait() {
        std::unique_lock lock(mutex_);
        completed_.wait(lock, [this] { return active_.load(std::memory_order_relaxed) == 0; });
    }

private:
    Compile compile_;
    Enqueue enqueue_;
    std::mutex mutex_;
    std::condition_variable completed_;
    std::map<Key, Result> results_;
    std::atomic<std::size_t> active_{0};
    std::atomic<std::uint64_t> revision_{1};
    bool stopped_ = false;
};

struct PresentFrame {
    void* swapchain = nullptr;
    std::uint64_t source = 0;
    std::uint64_t frame = 0;
    bool generated = false;
    bool onlyGenerated = false;
};

// The pinned FFX presenter processes each packet on one thread: generated
// member, then real member, with a composition callback before each Present.
// Match that explicit pair, not presentation parity or a numeric frame high-water.
class PresentationHistory final {
public:
    void mark(PresentFrame frame) noexcept { pending_ = frame; }

    PresentFrame take(void* swapchain) noexcept {
        const auto frame = std::exchange(pending_, {});
        return frame.swapchain == swapchain ? frame : PresentFrame{};
    }

    bool complete(const PresentFrame& frame, bool advanced) noexcept {
        const auto previous = std::exchange(generated_, {});
        if (!advanced) return false;
        if (!frame.source) return true;
        if (frame.generated) {
            if (!frame.onlyGenerated) generated_ = frame;
            return true;
        }
        return previous.source != frame.source || previous.frame != frame.frame ||
               previous.swapchain != frame.swapchain;
    }

private:
    PresentFrame pending_{};
    PresentFrame generated_{};
};

inline PresentationHistory& presentationHistory() noexcept {
    static thread_local PresentationHistory history;
    return history;
}

struct Recurrence {
    std::uint64_t last = 0;
    unsigned consecutive = 0;

    void observe(std::uint64_t frame) noexcept {
        if (consecutive && last == frame) return;
        consecutive = consecutive && last + 1 == frame ? consecutive + 1 : 1;
        if (consecutive > 5) consecutive = 5;
        last = frame;
    }

    bool eligible(std::uint64_t frame) const noexcept {
        return consecutive >= 5 && (last == frame || last + 1 == frame);
    }
};

template<class Key>
class TargetHistory final {
public:
    std::uint64_t identity() const noexcept { return identity_; }
    std::uint64_t observationRevision() const noexcept {
        return observationRevision_.load(std::memory_order_acquire);
    }

    // The associated history mutex serializes access. Memoize only the last observed
    // key: intervening observations, including capacity resets, replace it.
    bool use(const Key& key, std::uint64_t frame, bool observe) {
        if (!observe) {
            const auto found = uses_.find(key);
            return found != uses_.end() && found->second.eligible(frame);
        }
        if (observed_ && observedFrame_ == frame && observedKey_ == key) return true;
        // Positive observation tickets are issued only after use() completes
        // under the history lock. Invalidate them before any key/epoch/capacity
        // mutation, even if another thread completes it before the next draw.
        observationRevision_.fetch_add(1, std::memory_order_release);
        if (cleanedFrame_ != frame) {
            for (auto entry = uses_.begin(); entry != uses_.end();) {
                if (entry->second.last + 1 < frame) entry = uses_.erase(entry);
                else ++entry;
            }
            cleanedFrame_ = frame;
        }
        auto entry = uses_.lower_bound(key);
        if (entry == uses_.end() || entry->first != key) {
            if (uses_.size() >= 64) {
                uses_.clear();
                entry = uses_.end();
            }
            entry = uses_.try_emplace(entry, key);
        }
        entry->second.observe(frame);
        observedKey_ = key;
        observedFrame_ = frame;
        observed_ = true;
        return true;
    }

private:
    inline static std::atomic<std::uint64_t> nextIdentity_{0};
    const std::uint64_t identity_ = nextIdentity_.fetch_add(1, std::memory_order_relaxed) + 1;
    std::atomic<std::uint64_t> observationRevision_{1};
    std::map<Key, Recurrence> uses_;
    std::uint64_t cleanedFrame_ = 0;
    Key observedKey_{};
    std::uint64_t observedFrame_ = 0;
    bool observed_ = false;
};

// Layout returned by the native converter's versioned root-signature parser.
// The parser normalizes both serialized versions to D3D12_ROOT_SIGNATURE_DESC1.
struct Range {
    std::uint32_t type, count, shaderRegister, space, flags, tableOffset;
};
struct Parameter {
    std::uint32_t type, padding;
    union {
        struct { std::uint32_t count, padding; const Range* ranges; } table;
        std::uint32_t values[4];
    } data;
    std::uint32_t visibility, tailPadding;
};
struct RootDescription {
    std::uint32_t version, padding;
    std::uint32_t count, countPadding;
    const Parameter* parameters;
    std::uint32_t samplerCount, samplerPadding;
    const void* samplers;
    std::uint32_t flags, tailPadding;
};
static_assert(sizeof(Range) == 0x18 && sizeof(Parameter) == 0x20);
static_assert(sizeof(RootDescription) == 0x30);

inline bool rootHasNoUAV(const RootDescription* root) noexcept {
    // Known stage-denial and IA/SO flags cannot grant UAV access. Local roots,
    // directly indexed heaps and future flags do not have an established proof.
    constexpr std::uint32_t allowedFlags = 0x37f;
    if (!root || root->version != 2 || (root->flags & ~allowedFlags) ||
        root->count > 64 || (root->count && !root->parameters)) return false;
    for (std::uint32_t i = 0; i < root->count; ++i) {
        const auto& parameter = root->parameters[i];
        switch (parameter.type) {
        case 0:
            if (parameter.data.table.count && !parameter.data.table.ranges) return false;
            for (std::uint32_t j = 0; j < parameter.data.table.count; ++j) {
                const auto type = parameter.data.table.ranges[j].type;
                if (type != 0 && type != 2 && type != 3) return false;
            }
            break;
        case 1: // Root constants.
        case 2: // Root CBV.
        case 3: // Root SRV.
            break;
        default: // Root UAV or an unknown parameter kind.
            return false;
        }
    }
    return true;
}

} // namespace yaagl::pso::async_pipeline
