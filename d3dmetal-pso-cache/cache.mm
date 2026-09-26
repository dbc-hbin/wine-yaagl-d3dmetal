#import "cache.hpp"

#if __has_feature(objc_arc)
#error "d3dmetal-pso-cache must be compiled with Objective-C automatic reference counting disabled"
#endif

#include <algorithm>
#include <condition_variable>
#include <exception>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <unordered_map>
#include <utility>

namespace yaagl::pso {

NativeResult::NativeResult(id state, id reflection, NSError* error) noexcept
    : state_(state), reflection_([reflection retain]), error_([error retain]) {}

NativeResult::NativeResult(const NativeResult& other) noexcept
    : state_([other.state_ retain]),
      reflection_([other.reflection_ retain]),
      error_([other.error_ retain]) {}

NativeResult::NativeResult(NativeResult&& other) noexcept
    : state_(std::exchange(other.state_, nil)),
      reflection_(std::exchange(other.reflection_, nil)),
      error_(std::exchange(other.error_, nil)) {}

NativeResult& NativeResult::operator=(NativeResult&& other) noexcept {
    if (this == &other) {
        return *this;
    }

    [state_ release];
    [reflection_ release];
    [error_ release];
    state_ = std::exchange(other.state_, nil);
    reflection_ = std::exchange(other.reflection_, nil);
    error_ = std::exchange(other.error_, nil);
    return *this;
}

NativeResult::~NativeResult() {
    [state_ release];
    [reflection_ release];
    [error_ release];
}

id NativeResult::takeState() noexcept {
    return std::exchange(state_, nil);
}

id NativeResult::takeReflection() noexcept {
    return std::exchange(reflection_, nil);
}

NSError* NativeResult::takeError() noexcept {
    return std::exchange(error_, nil);
}

namespace {

using Key = std::vector<std::uint8_t>;
using KeyView = std::span<const std::uint8_t>;

struct KeyHash final {
    using is_transparent = void;

    std::size_t operator()(const KeyView key) const noexcept {
        // FNV-1a is only a bucket selector. unordered_map still compares every
        // key byte, so a collision cannot alias native pipeline states.
        std::size_t hash = sizeof(std::size_t) == 8
            ? static_cast<std::size_t>(14695981039346656037ULL)
            : static_cast<std::size_t>(2166136261U);
        const std::size_t prime = sizeof(std::size_t) == 8
            ? static_cast<std::size_t>(1099511628211ULL)
            : static_cast<std::size_t>(16777619U);
        for (const std::uint8_t byte : key) {
            hash ^= byte;
            hash *= prime;
        }
        return hash;
    }

    std::size_t operator()(const Key& key) const noexcept {
        return (*this)(KeyView(key));
    }
};

struct KeyEqual final {
    using is_transparent = void;

    bool operator()(const KeyView left, const KeyView right) const noexcept {
        return left.size() == right.size()
            && std::equal(left.begin(), left.end(), right.begin());
    }
    bool operator()(const Key& left, const Key& right) const noexcept {
        return (*this)(KeyView(left), KeyView(right));
    }
    bool operator()(const Key& left, const KeyView right) const noexcept {
        return (*this)(KeyView(left), right);
    }
    bool operator()(const KeyView left, const Key& right) const noexcept {
        return (*this)(left, KeyView(right));
    }
};

struct Entry final {
    explicit Entry(NSArray* resources) : keyResources([resources retain]) {}
    ~Entry() {
        [keyResources release];
        [objcException release];
    }

    Entry(const Entry&) = delete;
    Entry& operator=(const Entry&) = delete;

    NSArray* keyResources;
    bool complete = false;
    std::optional<NativeResult> result;
    std::exception_ptr cppException;
    NSException* objcException = nil;
    std::condition_variable ready;
    Entry* waitingOn = nullptr;
};

struct DeviceScope final {
    std::mutex mutex;
    std::unordered_map<Key, std::shared_ptr<Entry>, KeyHash, KeyEqual> entries;
};

std::mutex dependencyMutex;
thread_local Entry* activeProducer = nullptr;

class ProducerGuard final {
public:
    explicit ProducerGuard(Entry* entry) noexcept
        : entry_(entry), previous_(activeProducer) {
        std::lock_guard lock(dependencyMutex);
        if (previous_ != nullptr) {
            previous_->waitingOn = entry_;
        }
        activeProducer = entry_;
    }

    ~ProducerGuard() {
        std::lock_guard lock(dependencyMutex);
        activeProducer = previous_;
        if (previous_ != nullptr && previous_->waitingOn == entry_) {
            previous_->waitingOn = nullptr;
        }
    }

    ProducerGuard(const ProducerGuard&) = delete;
    ProducerGuard& operator=(const ProducerGuard&) = delete;

private:
    Entry* entry_;
    Entry* previous_;
};

class WaitDependency final {
public:
    explicit WaitDependency(Entry* target) noexcept : waiter_(activeProducer) {
        if (waiter_ == nullptr) {
            return;
        }

        std::lock_guard lock(dependencyMutex);
        for (Entry* dependency = target; dependency != nullptr;
             dependency = dependency->waitingOn) {
            if (dependency == waiter_) {
                cyclic_ = true;
                return;
            }
        }
        waiter_->waitingOn = target;
    }

    ~WaitDependency() {
        if (waiter_ == nullptr || cyclic_) {
            return;
        }
        std::lock_guard lock(dependencyMutex);
        waiter_->waitingOn = nullptr;
    }

    WaitDependency(const WaitDependency&) = delete;
    WaitDependency& operator=(const WaitDependency&) = delete;

    bool cyclic() const noexcept { return cyclic_; }

private:
    Entry* waiter_;
    bool cyclic_ = false;
};

} // namespace

class Cache::Impl final {
public:
    NativeResult getOrCreate(
        const void* device,
        const KeyView key,
        NSArray* keyResources,
        const CreateFunction& create) {
        std::shared_ptr<DeviceScope> scope;
        try {
            scope = scopeFor(device);
        } catch (const std::bad_alloc&) {
            return create();
        }

        if (!scope) {
            return create();
        }

        std::shared_ptr<Entry> entry;
        std::optional<ProducerGuard> producer;
        {
            std::unique_lock lock(scope->mutex);
            const auto found = scope->entries.find(key);
            if (found == scope->entries.end()) {
                try {
                    entry = std::make_shared<Entry>(keyResources);
                    scope->entries.emplace(Key(key.begin(), key.end()), entry);
                    producer.emplace(entry.get());
                } catch (const std::bad_alloc&) {
                    lock.unlock();
                    return create();
                }
            } else {
                entry = found->second;
                if (!entry->complete) {
                    WaitDependency dependency(entry.get());
                    if (dependency.cyclic()) {
                        lock.unlock();
                        return create();
                    }
                    entry->ready.wait(lock, [&entry] { return entry->complete; });
                }
                if (entry->objcException != nil) {
                    @throw entry->objcException;
                }
                if (entry->cppException) {
                    std::rethrow_exception(entry->cppException);
                }
                if (!entry->result) {
                    throw std::logic_error("completed pipeline cache entry has no result");
                }

                return *entry->result;
            }
        }

        std::optional<NativeResult> produced;
        std::exception_ptr cppException;
        NSException* objcException = nil;
        try {
            @try {
                produced.emplace(create());
            } @catch (NSException* exception) {
                objcException = [exception retain];
            }
        } catch (...) {
            cppException = std::current_exception();
        }

        if (objcException != nil) {
            {
                std::lock_guard lock(scope->mutex);
                entry->objcException = objcException;
                entry->complete = true;
                eraseIfCurrent(*scope, key, entry);
            }
            entry->ready.notify_all();
            @throw objcException;
        }

        if (cppException) {
            {
                std::lock_guard lock(scope->mutex);
                entry->cppException = cppException;
                entry->complete = true;
                eraseIfCurrent(*scope, key, entry);
            }
            entry->ready.notify_all();
            std::rethrow_exception(cppException);
        }

        const bool successful = produced->state() != nil && !produced->hasError();
        {
            std::lock_guard lock(scope->mutex);
            entry->result.emplace(std::move(*produced));
            entry->complete = true;
            if (!successful) {
                eraseIfCurrent(*scope, key, entry);
            }
        }
        entry->ready.notify_all();
        return *entry->result;
    }

    void withDeviceRetired(
        const void* device,
        void (*action)(const void*, const void*),
        const void* context) {
        Retirement retirement{device, nullptr};
        std::shared_ptr<DeviceScope> forgotten;
        {
            std::lock_guard lock(scopesMutex_);
            retirement.next = retirements_;
            retirements_ = &retirement;
            const auto found = scopes_.find(device);
            if (found != scopes_.end()) {
                forgotten = std::move(found->second);
                scopes_.erase(found);
            }
        }

        RetirementGuard guard(*this, retirement);
        forgotten.reset();
        action(device, context);
    }

private:
    struct Retirement final {
        const void* device;
        Retirement* next;
    };

    class RetirementGuard final {
    public:
        RetirementGuard(Impl& owner, Retirement& retirement) noexcept
            : owner_(owner), retirement_(retirement) {}
        ~RetirementGuard() { owner_.endRetirement(retirement_); }

        RetirementGuard(const RetirementGuard&) = delete;
        RetirementGuard& operator=(const RetirementGuard&) = delete;

    private:
        Impl& owner_;
        Retirement& retirement_;
    };

    void endRetirement(Retirement& retirement) {
        std::lock_guard lock(scopesMutex_);
        Retirement** current = &retirements_;
        while (*current != &retirement) {
            current = &(*current)->next;
        }
        *current = retirement.next;
    }

    std::shared_ptr<DeviceScope> scopeFor(const void* device) {
        std::lock_guard lock(scopesMutex_);
        for (Retirement* retirement = retirements_; retirement != nullptr;
             retirement = retirement->next) {
            if (retirement->device == device) {
                return {};
            }
        }
        const auto found = scopes_.find(device);
        if (found != scopes_.end()) {
            return found->second;
        }
        auto scope = std::make_shared<DeviceScope>();
        scopes_.emplace(device, scope);
        return scope;
    }

    static void eraseIfCurrent(
        DeviceScope& scope,
        const KeyView key,
        const std::shared_ptr<Entry>& entry) {
        const auto current = scope.entries.find(key);
        if (current != scope.entries.end() && current->second == entry) {
            scope.entries.erase(current);
        }
    }

    std::mutex scopesMutex_;
    std::unordered_map<const void*, std::shared_ptr<DeviceScope>> scopes_;
    Retirement* retirements_ = nullptr;
};

Cache::Cache() : impl_(std::make_unique<Impl>()) {}

Cache::~Cache() = default;

NativeResult Cache::getOrCreate(
    const void* device,
    const std::span<const std::uint8_t> key,
    NSArray* keyResources,
    const CreateFunction& create) {
    return impl_->getOrCreate(device, key, keyResources, create);
}

void Cache::withDeviceRetired(
    const void* device,
    void (*action)(const void*, const void*),
    const void* context) {
    impl_->withDeviceRetired(device, action, context);
}

} // namespace yaagl::pso
