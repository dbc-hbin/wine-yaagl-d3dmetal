#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include "d3dmetal-transport.hpp"
#include <array>
#include <atomic>
#include <cassert>
#include <chrono>
#include <cstring>
#include <optional>
#include <semaphore>
#include <stdexcept>
#include <thread>
#include <unordered_map>

struct ObjectCounts {
    unsigned retains = 0;
    unsigned releases = 0;
    unsigned destroyed = 0;
};

@interface OwnerTestResource : NSObject {
@public
    ObjectCounts* counts;
    bool failRetain;
}
@end
@implementation OwnerTestResource
- (id)retain {
    if (failRetain)
        @throw [NSException exceptionWithName:@"InjectedRetainFailure" reason:nil userInfo:nil];
    ++counts->retains;
    return [super retain];
}
- (oneway void)release {
    ++counts->releases;
    [super release];
}
- (void)dealloc {
    ++counts->destroyed;
    [super dealloc];
}
@end

static OwnerTestResource* resource(ObjectCounts& counts) {
    auto* value = [[OwnerTestResource alloc] init];
    value->counts = &counts;
    return value;
}

enum class EncodeMode { success, emptyFalse, publishedFalse, throwBefore, throwAfter };
struct BackendControl {
    EncodeMode mode = EncodeMode::success;
    std::atomic<unsigned> calls{0};
    std::atomic<unsigned> leasesDestroyed{0};
    std::atomic<unsigned> framesDestroyed{0};
    void* commandBuffer = nullptr;
    void* fence = nullptr;
    std::weak_ptr<const void> lastLease;
    std::binary_semaphore* destructionEntered = nullptr;
    std::binary_semaphore* finishDestruction = nullptr;
};

static void destroyLease(BackendControl& control) {
    if (control.destructionEntered) {
        control.destructionEntered->release();
        assert(control.finishDestruction->try_acquire_for(std::chrono::seconds(5)));
    }
    ++control.leasesDestroyed;
}

// Only backend boundaries are replaced. Throws deliberately stress the
// transport's catch path; real backend encode declarations are noexcept.
template<class Lease>
static bool encodeFixture(BackendControl& control, void* commandBuffer, void* fence,
                          std::shared_ptr<const Lease>& lease) {
    assert(commandBuffer == control.commandBuffer && fence == control.fence);
    ++control.calls;
    if (control.mode == EncodeMode::throwBefore) throw std::runtime_error("before publish");
    if (control.mode == EncodeMode::emptyFalse) return false;
    lease = std::make_shared<Lease>(control);
    control.lastLease = lease;
    if (control.mode == EncodeMode::throwAfter) throw std::runtime_error("after publish");
    return control.mode == EncodeMode::success;
}

namespace yaagl::pso::metalfx {
struct Error {};
class ExecutionLease {
public:
    explicit ExecutionLease(BackendControl& control) : control_(control) {}
    ~ExecutionLease() { destroyLease(control_); }
private:
    BackendControl& control_;
};
class PreparedFrame {
public:
    explicit PreparedFrame(BackendControl& control) : control_(control) {}
    ~PreparedFrame() { ++control_.framesDestroyed; }
    bool encode(void* buffer, void* fence, std::shared_ptr<const ExecutionLease>& lease,
                Error*) const { return encodeFixture(control_, buffer, fence, lease); }
private:
    BackendControl& control_;
};
}
namespace yaagl::pso::fsr::framegeneration {
class ExecutionLease {
public:
    explicit ExecutionLease(BackendControl& control) : control_(control) {}
    ~ExecutionLease() { destroyLease(control_); }
private:
    BackendControl& control_;
};
class PreparedFrame {
public:
    explicit PreparedFrame(BackendControl& control) : control_(control) {}
    ~PreparedFrame() { ++control_.framesDestroyed; }
    bool encode(void* buffer, void* fence, std::shared_ptr<const ExecutionLease>& lease) const {
        return encodeFixture(control_, buffer, fence, lease);
    }
private:
    BackendControl& control_;
};
}

#include "transport-owner-production.inc"

using namespace yaagl::pso::d3dmetal;

// Native beta3 capture layout, independent of the production reader. The final
// fixture pointer is test-only; the actual hook reads only worker and command.
struct CompletionFixture {
    struct Capture {
        void* worker;
        void* reserved;
        void* command;
        CompletionFixture* fixture;
    };
    std::array<unsigned char, 0x20> worker{};
    std::array<unsigned char, 0xb0> command{};
    Capture capture{worker.data(), nullptr, command.data(), this};
    BackendControl& control;
    unsigned expectedDestroyed;
    unsigned forwarded = 0;

    CompletionFixture(BackendControl& value, void* buffer, unsigned expected, bool metal4 = true)
        : control(value), expectedDestroyed(expected) {
        worker[0x1a] = metal4;
        std::memcpy(command.data() + 0xa8, &buffer, sizeof(buffer));
    }
};

static void originalCompletion(void* opaque) {
    auto& fixture = *static_cast<CompletionFixture::Capture*>(opaque)->fixture;
    // Cleanup must precede native fence notifications and command-buffer reuse.
    assert(fixture.control.leasesDestroyed == fixture.expectedDestroyed);
    ++fixture.forwarded;
}

// Models the native allocator's +1 transfer (no retain), followed by Reset.
struct FakeAllocator {
    YAAGLMetalFXRecordedOwner* owner = nil;
    void take(YAAGLMetalFXRecordedOwner* value) {
        assert(!owner);
        owner = value;
    }
    void reset() {
        [owner release];
        owner = nil;
    }
    ~FakeAllocator() { assert(!owner); }
};

template<class Frame>
static void replayLifetime(EncodeMode mode, bool complete) {
    BackendControl control;
    control.mode = mode;
    int commandBuffer = 0, fence = 0;
    control.commandBuffer = &commandBuffer;
    control.fence = &fence;
    ObjectCounts textureCounts, bufferCounts;
    auto* texture = resource(textureCounts);
    auto* buffer = resource(bufferCounts);
    std::array<UseEntry, kMaxResources> uses{};
    uses[0].texture = reinterpret_cast<id<MTLTexture>>(texture);
    uses[1].buffer = reinterpret_cast<id<MTLBuffer>>(buffer);

    auto* owner = [[YAAGLMetalFXRecordedOwner alloc] init];
    assert(!owner->_state);
    std::shared_ptr<const Frame> frame = std::make_shared<Frame>(control);
    std::weak_ptr<const Frame> weakFrame = frame;
    owner->_state.emplace(PreparedWork(frame), uses, 2);
    assert(textureCounts.retains == 1 && bufferCounts.retains == 1);
    assert(textureCounts.releases == 0 && bufferCounts.releases == 0);
    frame.reset();
    [texture release];
    [buffer release];
    FakeAllocator allocator;
    allocator.take(owner);
    auto& state = *owner->_state;
    assert(state.encode(&commandBuffer, &fence) == (mode == EncodeMode::success));
    assert(control.calls == 1);
    const bool published = mode == EncodeMode::success ||
        mode == EncodeMode::publishedFalse || mode == EncodeMode::throwAfter;
    const auto weakFirst = control.lastLease;

    control.mode = EncodeMode::success;
    assert(state.encode(&commandBuffer, &fence));
    assert(control.calls == 2);
    const auto weakSecond = control.lastLease;
    assert(weakFirst.lock().get() != weakSecond.lock().get());
    assert(!weakSecond.expired());
    assert(weakFirst.expired() == !published);
    assert(!weakFrame.expired());
    assert(control.leasesDestroyed == 0 && control.framesDestroyed == 0);
    assert(textureCounts.destroyed == 0 && bufferCounts.destroyed == 0);
    unsigned expectedLeases = published ? 2u : 1u;
    if (complete) {
        int unrelatedBuffer = 0;
        CompletionFixture unrelated(control, &unrelatedBuffer, 0);
        completeExecute(&unrelated.capture);
        assert(unrelated.forwarded == 1 && !weakSecond.expired());
        CompletionFixture legacy(control, &commandBuffer, 0, false);
        completeExecute(&legacy.capture);
        assert(legacy.forwarded == 1 && !weakSecond.expired());

        CompletionFixture completion(control, &commandBuffer, expectedLeases);
        completeExecute(&completion.capture);
        assert(completion.forwarded == 1);
        assert(weakFirst.expired() && weakSecond.expired());
        assert(!weakFrame.expired() && control.framesDestroyed == 0);
        assert(textureCounts.destroyed == 0 && bufferCounts.destroyed == 0);

        // Reusing both the recorded owner and native buffer needs a fresh lease.
        assert(state.encode(&commandBuffer, &fence));
        assert(!control.lastLease.expired());
        assert(control.leasesDestroyed == expectedLeases);
        completion.expectedDestroyed = ++expectedLeases;
        completeExecute(&completion.capture);
        assert(completion.forwarded == 2 && control.lastLease.expired());
    }
    allocator.reset();
    assert(weakFirst.expired() && weakSecond.expired() && weakFrame.expired());
    assert(control.leasesDestroyed == expectedLeases);
    assert(control.framesDestroyed == 1);
    assert(textureCounts.releases == 2 && bufferCounts.releases == 2);
    assert(textureCounts.destroyed == 1 && bufferCounts.destroyed == 1);
}

static void completionAndOwnerTeardown() {
    BackendControl control;
    int buffer = 0, fence = 0;
    control.commandBuffer = &buffer;
    control.fence = &fence;
    std::array<UseEntry, kMaxResources> uses{};
    auto* owner = [[YAAGLMetalFXRecordedOwner alloc] init];
    owner->_state.emplace(PreparedWork(
        std::make_shared<const yaagl::pso::metalfx::PreparedFrame>(control)), uses, 0);
    FakeAllocator allocator;
    allocator.take(owner);
    assert(owner->_state->encode(&buffer, &fence));
    std::binary_semaphore entered(0), finish(0);
    control.destructionEntered = &entered;
    control.finishDestruction = &finish;
    CompletionFixture completion(control, &buffer, 1);
    std::thread callback([&] { completeExecute(&completion.capture); });
    assert(entered.try_acquire_for(std::chrono::seconds(5)));
    allocator.reset();
    assert(control.framesDestroyed == 1);
    finish.release();
    callback.join();
    assert(control.lastLease.expired() && control.leasesDestroyed == 1);
    assert(completion.forwarded == 1);
}

static void independentCompletedBuffers() {
    BackendControl first, second;
    int firstBuffer = 0, secondBuffer = 0, fence = 0;
    first.commandBuffer = &firstBuffer;
    second.commandBuffer = &secondBuffer;
    first.fence = second.fence = &fence;
    std::array<UseEntry, kMaxResources> uses{};
    auto* firstOwner = [[YAAGLMetalFXRecordedOwner alloc] init];
    auto* secondOwner = [[YAAGLMetalFXRecordedOwner alloc] init];
    firstOwner->_state.emplace(PreparedWork(
        std::make_shared<const yaagl::pso::metalfx::PreparedFrame>(first)), uses, 0);
    secondOwner->_state.emplace(PreparedWork(
        std::make_shared<const yaagl::pso::fsr::framegeneration::PreparedFrame>(second)), uses, 0);
    assert(firstOwner->_state->encode(&firstBuffer, &fence));
    assert(secondOwner->_state->encode(&secondBuffer, &fence));
    CompletionFixture completed(second, &secondBuffer, 1);
    completeExecute(&completed.capture);
    assert(completed.forwarded == 1 && second.lastLease.expired());
    assert(!first.lastLease.expired() && first.leasesDestroyed == 0);
    [secondOwner release];
    // Unsubmitted work is still reclaimed by allocator/owner teardown.
    [firstOwner release];
    assert(first.lastLease.expired() && first.leasesDestroyed == 1);
}

template<class Frame>
static void constructionFailure(bool failingTexture) {
    BackendControl control;
    ObjectCounts firstCounts, failingCounts;
    auto* first = resource(firstCounts);
    auto* failing = resource(failingCounts);
    failing->failRetain = true;
    std::array<UseEntry, kMaxResources> uses{};
    // Cover rollback of either kind, followed by either kind of failed retain.
    if (failingTexture) {
        uses[0].buffer = reinterpret_cast<id<MTLBuffer>>(first);
        uses[1].texture = reinterpret_cast<id<MTLTexture>>(failing);
    } else {
        uses[0].texture = reinterpret_cast<id<MTLTexture>>(first);
        uses[1].buffer = reinterpret_cast<id<MTLBuffer>>(failing);
    }
    auto* owner = [[YAAGLMetalFXRecordedOwner alloc] init];
    assert(!owner->_state);
    bool caught = false;
    @try {
        std::shared_ptr<const Frame> frame = std::make_shared<Frame>(control);
        owner->_state.emplace(PreparedWork(std::move(frame)), uses, 2);
    } @catch (NSException* exception) {
        assert([exception.name isEqualToString:@"InjectedRetainFailure"]);
        caught = true;
    }
    assert(caught && !owner->_state);
    assert(firstCounts.retains == 1 && firstCounts.releases == 1);
    assert(failingCounts.retains == 0 && failingCounts.releases == 0);
    assert(control.framesDestroyed == 1);
    [owner release];
    assert(firstCounts.releases == 1 && failingCounts.releases == 0);
    assert(control.framesDestroyed == 1);
    [first release];
    [failing release];
    assert(firstCounts.destroyed == 1 && failingCounts.destroyed == 1);
}

int main() {
    @autoreleasepool {
        gCompleteExecute.store(&originalCompletion);
        auto* empty = [[YAAGLMetalFXRecordedOwner alloc] init];
        assert(!empty->_state);
        [empty release];
        for (const auto mode : {EncodeMode::success, EncodeMode::emptyFalse,
                                EncodeMode::publishedFalse, EncodeMode::throwBefore,
                                EncodeMode::throwAfter}) {
            for (bool complete : {false, true}) {
                replayLifetime<yaagl::pso::metalfx::PreparedFrame>(mode, complete);
                replayLifetime<yaagl::pso::fsr::framegeneration::PreparedFrame>(mode, complete);
            }
        }
        completionAndOwnerTeardown();
        independentCompletedBuffers();
        for (bool texture : {false, true}) {
            constructionFailure<yaagl::pso::metalfx::PreparedFrame>(texture);
            constructionFailure<yaagl::pso::fsr::framegeneration::PreparedFrame>(texture);
        }
    }
}
