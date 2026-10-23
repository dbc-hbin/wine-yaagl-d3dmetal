#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include "d3dmetal-transport.hpp"
#include <array>
#include <cassert>
#include <optional>
#include <stdexcept>

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
    unsigned calls = 0;
    unsigned leasesDestroyed = 0;
    unsigned framesDestroyed = 0;
    void* commandBuffer = nullptr;
    void* fence = nullptr;
};

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
    if (control.mode == EncodeMode::throwAfter) throw std::runtime_error("after publish");
    return control.mode == EncodeMode::success;
}

namespace yaagl::pso::metalfx {
struct Error {};
class ExecutionLease {
public:
    explicit ExecutionLease(BackendControl& control) : control_(control) {}
    ~ExecutionLease() { ++control_.leasesDestroyed; }
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
    ~ExecutionLease() { ++control_.leasesDestroyed; }
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

template<class Frame, class Lease>
static void replayLifetime(EncodeMode mode) {
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
    assert(!state.firstLease && state.leases.capacity() == 0);
    assert(state.encode(&commandBuffer, &fence) == (mode == EncodeMode::success));
    assert(control.calls == 1);
    assert(state.firstLease);
    assert(state.leases.empty() && state.leases.capacity() == 0);
    const bool published = mode == EncodeMode::success ||
        mode == EncodeMode::publishedFalse || mode == EncodeMode::throwAfter;
    const auto& first = std::get<std::shared_ptr<const Lease>>(*state.firstLease);
    assert(static_cast<bool>(first) == published);
    std::weak_ptr<const Lease> weakFirst = first;
    const auto* firstAddress = first.get();

    control.mode = EncodeMode::success;
    assert(state.encode(&commandBuffer, &fence));
    assert(control.calls == 2);
    assert(state.leases.size() == 1);
    assert(std::get<std::shared_ptr<const Lease>>(*state.firstLease).get() == firstAddress);
    const auto& second = std::get<std::shared_ptr<const Lease>>(state.leases.front());
    assert(second && second.get() != firstAddress);
    std::weak_ptr<const Lease> weakSecond = second;
    assert(!weakSecond.expired());
    assert(weakFirst.expired() == !published);
    assert(!weakFrame.expired());
    assert(control.leasesDestroyed == 0 && control.framesDestroyed == 0);
    assert(textureCounts.destroyed == 0 && bufferCounts.destroyed == 0);
    allocator.reset();
    assert(weakFirst.expired() && weakSecond.expired() && weakFrame.expired());
    assert(control.leasesDestroyed == (published ? 2u : 1u));
    assert(control.framesDestroyed == 1);
    assert(textureCounts.releases == 2 && bufferCounts.releases == 2);
    assert(textureCounts.destroyed == 1 && bufferCounts.destroyed == 1);
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
        auto* empty = [[YAAGLMetalFXRecordedOwner alloc] init];
        assert(!empty->_state);
        [empty release];
        for (const auto mode : {EncodeMode::success, EncodeMode::emptyFalse,
                                EncodeMode::publishedFalse, EncodeMode::throwBefore,
                                EncodeMode::throwAfter}) {
            replayLifetime<yaagl::pso::metalfx::PreparedFrame,
                           yaagl::pso::metalfx::ExecutionLease>(mode);
            replayLifetime<yaagl::pso::fsr::framegeneration::PreparedFrame,
                           yaagl::pso::fsr::framegeneration::ExecutionLease>(mode);
        }
        for (bool texture : {false, true}) {
            constructionFailure<yaagl::pso::metalfx::PreparedFrame>(texture);
            constructionFailure<yaagl::pso::fsr::framegeneration::PreparedFrame>(texture);
        }
    }
}
