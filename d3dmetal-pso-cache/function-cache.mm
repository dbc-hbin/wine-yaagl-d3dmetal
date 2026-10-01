#import "function-cache.hpp"

#if __has_feature(objc_arc)
#error "d3dmetal-pso-cache must be compiled with Objective-C automatic reference counting disabled"
#endif

#import <objc/runtime.h>

#include <algorithm>
#include <condition_variable>
#include <exception>
#include <mutex>
#include <optional>
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

FunctionResult::FunctionResult(NSMutableArray* functions, const bool reusable) noexcept
    : functions_(functions), reusable_(reusable) {}

FunctionResult::FunctionResult(FunctionResult&& other) noexcept
    : functions_(std::exchange(other.functions_, nil)),
      reusable_(std::exchange(other.reusable_, false)) {}

FunctionResult& FunctionResult::operator=(FunctionResult&& other) noexcept {
    if (this == &other) {
        return *this;
    }

    [functions_ release];
    functions_ = std::exchange(other.functions_, nil);
    reusable_ = std::exchange(other.reusable_, false);
    return *this;
}

FunctionResult::~FunctionResult() {
    [functions_ release];
}

NSMutableArray* FunctionResult::takeFunctions() noexcept {
    reusable_ = false;
    return std::exchange(functions_, nil);
}

namespace {

using Key = std::vector<std::uint8_t>;
using KeyView = std::span<const std::uint8_t>;

struct KeyHash final {
    using is_transparent = void;

    std::size_t operator()(const KeyView key) const noexcept {
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
    ~Entry() {
        for (std::size_t index = 0; index < weakFunctionCount; ++index) {
            objc_destroyWeak(&weakFunctions[index]);
        }
        objc_destroyWeak(&weakLibrary);
        [library release];
        [objcException release];
    }

    Entry(const Entry&) = delete;
    Entry& operator=(const Entry&) = delete;
    Entry() = default;

    // Strong only while the producer runs; released once the entry completes.
    id library = nil;
    bool complete = false;
    // Set once weak slots are published. They are never written again, so
    // holders of the entry may load them without the scope mutex.
    bool cached = false;
    // Weak slots live in the entry or in a fixed heap block so their addresses
    // stay stable from objc_storeWeak until objc_destroyWeak.
    id weakLibrary = nil;
    std::unique_ptr<id[]> weakFunctions;
    std::size_t weakFunctionCount = 0;
    std::exception_ptr cppException;
    NSException* objcException = nil;
    std::condition_variable ready;
};

constexpr std::size_t kMinimumSweepSize = 64;

struct DeviceScope final {
    std::mutex mutex;
    std::unordered_map<Key, std::shared_ptr<Entry>, KeyHash, KeyEqual> entries;
    std::size_t sweepAt = kMinimumSweepSize;
};

// Records weak references to the library and every extracted function. Any
// failure leaves the entry uncacheable; the producer keeps its own result.
bool publishWeak(Entry& entry, NSArray* functions) noexcept {
    try {
        @try {
            const NSUInteger count = [functions count];
            entry.weakFunctions.reset(new id[count]());
            entry.weakFunctionCount = count;
            for (NSUInteger index = 0; index < count; ++index) {
                id function = [functions objectAtIndex:index];
                if (objc_storeWeakOrNil(&entry.weakFunctions[index], function) == nil) {
                    return false;
                }
            }
            return objc_storeWeakOrNil(&entry.weakLibrary, entry.library) != nil;
        } @catch (NSException*) {
            return false;
        }
    } catch (...) {
        return false;
    }
}

// Returns false if the library or any function has been deallocated. On live
// entries, output receives a new +1 array of the loaded functions. Allocation
// exceptions propagate after releasing every temporary.
bool loadWeak(Entry& entry, NSMutableArray*& output) {
    id library = objc_loadWeakRetained(&entry.weakLibrary);
    if (library == nil) {
        return false;
    }
    [library release];

    NSMutableArray* functions = nil;
    @try {
        functions = [[NSMutableArray alloc] initWithCapacity:entry.weakFunctionCount];
        for (std::size_t index = 0; index < entry.weakFunctionCount; ++index) {
            id function = objc_loadWeakRetained(&entry.weakFunctions[index]);
            if (function == nil) {
                [functions release];
                return false;
            }
            @try {
                [functions addObject:function];
            } @finally {
                [function release];
            }
        }
    } @catch (NSException*) {
        [functions release];
        @throw;
    }
    output = functions;
    return true;
}

// Drops completed entries whose library or any function died. Runs under the
// scope mutex and only performs weak loads.
void sweep(DeviceScope& scope) noexcept {
    const auto dead = [](id& slot) {
        id object = objc_loadWeakRetained(&slot);
        [object release];
        return object == nil;
    };
    for (auto current = scope.entries.begin(); current != scope.entries.end();) {
        Entry& entry = *current->second;
        if (entry.complete && entry.cached
            && (dead(entry.weakLibrary)
                || std::any_of(entry.weakFunctions.get(),
                    entry.weakFunctions.get() + entry.weakFunctionCount, dead))) {
            current = scope.entries.erase(current);
            continue;
        }
        ++current;
    }
    scope.sweepAt = std::max(kMinimumSweepSize, scope.entries.size() * 2);
}

thread_local unsigned producerDepth = 0;

class ProducerGuard final {
public:
    ProducerGuard() noexcept { ++producerDepth; }
    ~ProducerGuard() { --producerDepth; }

    ProducerGuard(const ProducerGuard&) = delete;
    ProducerGuard& operator=(const ProducerGuard&) = delete;
};

FunctionResult invokeCreate(const FunctionCreate create, void* const context) {
    ProducerGuard guard;
    return create(context);
}

} // namespace

class FunctionCache::Impl final {
public:
    FunctionResult getOrCreate(
        const void* const device,
        const KeyView key,
        id library,
        const FunctionCreate create,
        void* const context) {
        std::shared_ptr<DeviceScope> scope;
        try {
            scope = scopeFor(device);
        } catch (const std::bad_alloc&) {
            return invokeCreate(create, context);
        }

        if (!scope) {
            return invokeCreate(create, context);
        }

        for (;;) {
            std::shared_ptr<Entry> entry;
            bool producer = false;
            {
                std::unique_lock lock(scope->mutex);
                const auto found = scope->entries.find(key);
                if (found == scope->entries.end()) {
                    if (scope->entries.size() >= scope->sweepAt) {
                        sweep(*scope);
                    }
                    try {
                        entry = std::make_shared<Entry>();
                        scope->entries.emplace(Key(key.begin(), key.end()), entry);
                        producer = true;
                    } catch (const std::bad_alloc&) {
                        lock.unlock();
                        entry.reset();
                        return invokeCreate(create, context);
                    }
                } else {
                    entry = found->second;
                    if (!entry->complete && producerDepth != 0) {
                        lock.unlock();
                        return invokeCreate(create, context);
                    }
                    entry->ready.wait(lock, [&entry] { return entry->complete; });

                    if (entry->objcException != nil) {
                        @throw entry->objcException;
                    }
                    if (entry->cppException) {
                        std::rethrow_exception(entry->cppException);
                    }
                    if (!entry->cached) {
                        continue;
                    }
                }
            }

            if (!producer) {
                // Weak slots are immutable once cached, so they are loaded (and
                // any temporaries released) without holding the scope mutex.
                ProducerGuard hitGuard;
                bool live = false;
                NSMutableArray* functions = nil;
                NSException* objcException = nil;
                try {
                    @try {
                        live = loadWeak(*entry, functions);
                    } @catch (NSException* exception) {
                        if ([[exception name] isEqualToString:NSMallocException]) {
                            live = true;
                        } else {
                            objcException = exception;
                        }
                    }
                } catch (const std::bad_alloc&) {
                    live = true;
                }
                if (objcException != nil) {
                    [functions release];
                    @throw objcException;
                }
                if (!live) {
                    {
                        std::lock_guard lock(scope->mutex);
                        eraseIfCurrent(*scope, key, entry);
                    }
                    continue;
                }
                if (functions != nil) {
                    return FunctionResult(functions, true);
                }
                return invokeCreate(create, context);
            }

            // The entry is visible from admission onward. Retain, native
            // creation, snapshotting, and publication can all invoke user or
            // Objective-C code, so nested flights must bypass waits throughout.
            ProducerGuard producerGuard;
            bool libraryRetained = false;
            NSException* retainObjcException = nil;
            std::exception_ptr retainCppException;
            try {
                @try {
                    entry->library = [library retain];
                    libraryRetained = true;
                } @catch (NSException* exception) {
                    if ([[exception name] isEqualToString:NSMallocException]) {
                        libraryRetained = false;
                    } else {
                        retainObjcException = [exception retain];
                    }
                }
            } catch (const std::bad_alloc&) {
                libraryRetained = false;
            } catch (...) {
                retainCppException = std::current_exception();
            }
            if (retainObjcException != nil) {
                publishException(*scope, key, entry, retainObjcException, {});
                @throw retainObjcException;
            }
            if (retainCppException) {
                publishException(*scope, key, entry, nil, retainCppException);
                std::rethrow_exception(retainCppException);
            }
            if (!libraryRetained) {
                finishWithoutValue(*scope, key, entry);
                return invokeCreate(create, context);
            }

            std::optional<FunctionResult> produced;
            std::exception_ptr cppException;
            NSException* objcException = nil;
            try {
                @try {
                    produced.emplace(invokeCreate(create, context));
                } @catch (NSException* exception) {
                    objcException = [exception retain];
                }
            } catch (...) {
                cppException = std::current_exception();
            }

            if (objcException != nil) {
                publishException(*scope, key, entry, objcException, {});
                @throw objcException;
            }
            if (cppException) {
                publishException(*scope, key, entry, nil, cppException);
                std::rethrow_exception(cppException);
            }

            // Waiters only read the entry after complete is set under the
            // mutex, so the weak slots can be published before locking.
            const bool cached = produced->functions() != nil && produced->reusable()
                && publishWeak(*entry, produced->functions());
            id retainedLibrary = nil;
            {
                std::lock_guard lock(scope->mutex);
                entry->complete = true;
                entry->cached = cached;
                retainedLibrary = std::exchange(entry->library, nil);
                if (!cached) {
                    eraseIfCurrent(*scope, key, entry);
                }
            }
            entry->ready.notify_all();
            [retainedLibrary release];
            return std::move(*produced);
        }
    }

    void withDeviceRetired(
        const void* const device,
        void (*action)(const void*, const void*),
        const void* const context) {
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

    std::shared_ptr<DeviceScope> scopeFor(const void* const device) {
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

    static void finishWithoutValue(
        DeviceScope& scope,
        const KeyView key,
        const std::shared_ptr<Entry>& entry) {
        {
            std::lock_guard lock(scope.mutex);
            entry->complete = true;
            eraseIfCurrent(scope, key, entry);
        }
        entry->ready.notify_all();
    }

    static void publishException(
        DeviceScope& scope,
        const KeyView key,
        const std::shared_ptr<Entry>& entry,
        NSException* const objcException,
        const std::exception_ptr& cppException) {
        {
            std::lock_guard lock(scope.mutex);
            entry->objcException = objcException;
            entry->cppException = cppException;
            entry->complete = true;
            eraseIfCurrent(scope, key, entry);
        }
        entry->ready.notify_all();
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

FunctionCache::FunctionCache() : impl_(std::make_unique<Impl>()) {}

FunctionCache::~FunctionCache() = default;

FunctionResult FunctionCache::getOrCreate(
    const void* const device,
    const std::span<const std::uint8_t> key,
    id library,
    const FunctionCreate create,
    void* const context) {
    return impl_->getOrCreate(device, key, library, create, context);
}

void FunctionCache::withDeviceRetired(
    const void* const device,
    void (*action)(const void*, const void*),
    const void* const context) {
    impl_->withDeviceRetired(device, action, context);
}

} // namespace yaagl::pso
