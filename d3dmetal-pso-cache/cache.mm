#import "cache.hpp"

#if __has_feature(objc_arc)
#error "d3dmetal-pso-cache must be compiled with Objective-C automatic reference counting disabled"
#endif

#import <objc/runtime.h>

#include <algorithm>
#include <condition_variable>
#include <exception>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <unordered_map>
#include <utility>
#include <vector>

// Exported by libobjc (the __weak ABI) but only declared in private headers.
// objc_storeWeakOrNil stores nil instead of aborting when an object refuses
// weak references, which makes such a result uncacheable instead of fatal.
extern "C" id objc_storeWeakOrNil(id* location, id object);
extern "C" id objc_loadWeakRetained(id* location);
extern "C" void objc_destroyWeak(id* location);

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

const char kReflectionAssociation = 0;

struct Entry final {
    explicit Entry(NSArray* resources) : keyResources([resources retain]) {}
    ~Entry() {
        for (std::size_t index = 0; index < weakResourceCount; ++index) {
            objc_destroyWeak(&weakResources[index]);
        }
        objc_destroyWeak(&weakState);
        objc_destroyWeak(&weakReflection);
        [keyResources release];
        [objcException release];
    }

    Entry(const Entry&) = delete;
    Entry& operator=(const Entry&) = delete;

    // Strong only while the producer runs; released once the entry completes.
    NSArray* keyResources;
    bool complete = false;
    // Set once weak slots are published. They are never written again, so
    // holders of the entry may load them without the scope mutex.
    bool cached = false;
    bool hasReflection = false;
    // Weak slots live in the entry or in a fixed heap block so their addresses
    // stay stable from objc_storeWeak until objc_destroyWeak.
    std::unique_ptr<id[]> weakResources;
    std::size_t weakResourceCount = 0;
    id weakState = nil;
    id weakReflection = nil;
    // Only uncached completions (failures) keep a strong result for waiters
    // that already hold this erased entry.
    std::optional<NativeResult> result;
    std::exception_ptr cppException;
    NSException* objcException = nil;
    std::condition_variable ready;
    Entry* waitingOn = nullptr;
};

constexpr std::size_t kMinimumSweepSize = 64;

struct DeviceScope final {
    std::mutex mutex;
    std::unordered_map<Key, std::shared_ptr<Entry>, KeyHash, KeyEqual> entries;
    std::size_t sweepAt = kMinimumSweepSize;
};

// Converts a successful result into weak slots. Any failure leaves the entry
// uncacheable; the producer still returns its strong result.
bool publishWeak(Entry& entry, const NativeResult& result) noexcept {
    try {
        @try {
            const NSUInteger count = [entry.keyResources count];
            entry.weakResources.reset(new id[count]());
            entry.weakResourceCount = count;
            for (NSUInteger index = 0; index < count; ++index) {
                id resource = [entry.keyResources objectAtIndex:index];
                if (objc_storeWeakOrNil(&entry.weakResources[index], resource) == nil) {
                    return false;
                }
            }
            id reflection = result.reflection();
            if (reflection != nil) {
                // The reflection has no other owner once D3DMetal drops it;
                // tie its lifetime to the state it describes.
                objc_setAssociatedObject(result.state(), &kReflectionAssociation,
                    reflection, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
                if (objc_storeWeakOrNil(&entry.weakReflection, reflection) == nil) {
                    return false;
                }
                entry.hasReflection = true;
            }
            return objc_storeWeakOrNil(&entry.weakState, result.state()) != nil;
        } @catch (NSException*) {
            return false;
        }
    } catch (...) {
        return false;
    }
}

// Returns a live result with fresh owned references, or nothing if any object
// the entry refers to has been deallocated.
std::optional<NativeResult> loadWeak(Entry& entry) noexcept {
    for (std::size_t index = 0; index < entry.weakResourceCount; ++index) {
        id resource = objc_loadWeakRetained(&entry.weakResources[index]);
        if (resource == nil) {
            return std::nullopt;
        }
        [resource release];
    }
    id state = objc_loadWeakRetained(&entry.weakState);
    if (state == nil) {
        return std::nullopt;
    }
    id reflection = nil;
    if (entry.hasReflection) {
        reflection = objc_loadWeakRetained(&entry.weakReflection);
        if (reflection == nil) {
            [state release];
            return std::nullopt;
        }
    }
    std::optional<NativeResult> result(std::in_place, state, reflection, nil);
    [reflection release];
    return result;
}

// Drops completed entries whose state, reflection, or any key resource died.
// Runs under the scope mutex and only performs weak loads; each release
// returns an object someone else still owns unless it raced to zero.
void sweep(DeviceScope& scope) noexcept {
    const auto dead = [](id& slot) {
        id object = objc_loadWeakRetained(&slot);
        [object release];
        return object == nil;
    };
    for (auto current = scope.entries.begin(); current != scope.entries.end();) {
        Entry& entry = *current->second;
        if (entry.complete && entry.cached
            && (dead(entry.weakState)
                || (entry.hasReflection && dead(entry.weakReflection))
                || std::any_of(entry.weakResources.get(),
                    entry.weakResources.get() + entry.weakResourceCount, dead))) {
            current = scope.entries.erase(current);
            continue;
        }
        ++current;
    }
    scope.sweepAt = std::max(kMinimumSweepSize, scope.entries.size() * 2);
}

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

        for (;;) {
            std::shared_ptr<Entry> entry;
            std::optional<ProducerGuard> producer;
            {
                std::unique_lock lock(scope->mutex);
                const auto found = scope->entries.find(key);
                if (found == scope->entries.end()) {
                    if (scope->entries.size() >= scope->sweepAt) {
                        sweep(*scope);
                    }
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
                    if (!entry->cached) {
                        if (!entry->result) {
                            throw std::logic_error("completed pipeline cache entry has no result");
                        }
                        return *entry->result;
                    }
                }
            }

            if (!producer) {
                // Weak slots are immutable once cached, so they are loaded (and
                // any temporaries released) without holding the scope mutex.
                std::optional<NativeResult> live = loadWeak(*entry);
                if (live) {
                    return std::move(*live);
                }
                {
                    std::lock_guard lock(scope->mutex);
                    eraseIfCurrent(*scope, key, entry);
                }
                continue;
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

            // Waiters only read the entry after complete is set under the
            // mutex, so the weak slots can be published before locking.
            const bool cached = produced->state() != nil && !produced->hasError()
                && publishWeak(*entry, *produced);
            NSArray* resources = nil;
            {
                std::lock_guard lock(scope->mutex);
                if (!cached) {
                    entry->result.emplace(*produced);
                }
                entry->cached = cached;
                entry->complete = true;
                resources = std::exchange(entry->keyResources, nil);
                if (!cached) {
                    eraseIfCurrent(*scope, key, entry);
                }
            }
            entry->ready.notify_all();
            [resources release];
            return std::move(*produced);
        }
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
