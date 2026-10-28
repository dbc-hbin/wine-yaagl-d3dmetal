#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <objc/runtime.h>
#include "metalfx-lifetime.hpp"
#include <atomic>
#include <cassert>
#include <cstdio>
#include <cstring>

struct Finalization {
    dispatch_semaphore_t signal = dispatch_semaphore_create(0);
    std::atomic<unsigned> count{0};
    ~Finalization() { dispatch_release(signal); }
    void wait() {
        assert(dispatch_semaphore_wait(signal, dispatch_time(DISPATCH_TIME_NOW, 30 * NSEC_PER_SEC)) == 0);
        assert(count.load() == 1);
    }
};
static dispatch_queue_t makeQueue(Finalization& finalization) {
    auto queue = dispatch_queue_create("yaagl.queue-lifetime", DISPATCH_QUEUE_SERIAL);
    dispatch_set_context(queue, &finalization);
    dispatch_set_finalizer_f(queue, [](void* context) {
        auto& state = *static_cast<Finalization*>(context);
        ++state.count;
        dispatch_semaphore_signal(state.signal);
    });
    return queue;
}
static id rejectedGetter(id, SEL) { return nil; }

int main(int argc, char** argv) { @autoreleasepool {
    assert(argc == 2);
    if (!std::strcmp(argv[1], "reject")) {
        Class cls = objc_lookUpClass("MTL4CommandQueueDescriptor");
        auto getter = class_getInstanceMethod(cls, @selector(feedbackQueue));
        auto setter = class_getInstanceMethod(cls, @selector(setFeedbackQueue:));
        assert(getter && setter);
        auto originalSetter = method_getImplementation(setter);
        auto originalGetter = method_setImplementation(getter, reinterpret_cast<IMP>(&rejectedGetter));
        assert(!yaagl::pso::metalfx_lifetime::initializeQueueDescriptor());
        assert(method_getImplementation(setter) == originalSetter);
        assert(method_getImplementation(getter) == reinterpret_cast<IMP>(&rejectedGetter));
        method_setImplementation(getter, originalGetter);
        std::puts("METAL_QUEUE_IDENTITY_REJECTION_PASS");
        return 0;
    }
    assert(!std::strcmp(argv[1], "repair"));
    assert(yaagl::pso::metalfx_lifetime::initializeQueueDescriptor());
    for (unsigned i = 0; i < 16; ++i) { @autoreleasepool {
        Finalization first, second;
        auto a = makeQueue(first), b = makeQueue(second);
        auto descriptor = [MTL4CommandQueueDescriptor new];
        descriptor.feedbackQueue = a;
        dispatch_release(a);
        auto copy = static_cast<MTL4CommandQueueDescriptor*>([descriptor copy]);
        [descriptor release];
        dispatch_sync(copy.feedbackQueue, ^{});
        copy.feedbackQueue = b;
        first.wait();
        dispatch_release(b);
        copy.feedbackQueue = copy.feedbackQueue;
        dispatch_sync(copy.feedbackQueue, ^{});
        copy.feedbackQueue = nil;
        second.wait();
        [copy release];
    }}
    auto device = MTLCreateSystemDefaultDevice();
    assert(device);
    Finalization feedback;
    auto callbackQueue = makeQueue(feedback);
    for (unsigned i = 0; i < 32; ++i) { @autoreleasepool {
        auto descriptor = [MTL4CommandQueueDescriptor new];
        descriptor.feedbackQueue = callbackQueue;
        NSError* error = nil;
        auto queue = [device newMTL4CommandQueueWithDescriptor:descriptor error:&error];
        [descriptor release];
        assert(queue && !error);
        auto allocator = [device newCommandAllocator];
        auto buffer = [device newCommandBuffer];
        constexpr NSUInteger length = 256;
        auto source = [device newBufferWithLength:length options:MTLResourceStorageModeShared];
        auto destination = [device newBufferWithLength:length options:MTLResourceStorageModeShared];
        assert(source && destination);
        std::memset(source.contents, i + 1, length);
        std::memset(destination.contents, 0, length);
        auto residencyDescriptor = [MTLResidencySetDescriptor new];
        auto residency = [device newResidencySetWithDescriptor:residencyDescriptor error:&error];
        [residencyDescriptor release];
        assert(residency && !error);
        id<MTLAllocation> allocations[] = {source, destination};
        [residency addAllocations:allocations count:2];
        [residency commit];
        [queue addResidencySet:residency];
        [buffer beginCommandBufferWithAllocator:allocator];
        auto encoder = [buffer computeCommandEncoder];
        assert(encoder);
        [encoder copyFromBuffer:source sourceOffset:0 toBuffer:destination destinationOffset:0 size:length];
        [encoder endEncoding];
        [buffer endCommandBuffer];
        auto done = dispatch_semaphore_create(0);
        auto options = [MTL4CommitOptions new];
        [options addFeedbackHandler:^(id<MTL4CommitFeedback> result) {
            assert(!result.error);
            dispatch_semaphore_signal(done);
        }];
        [queue commit:&buffer count:1 options:options];
        assert(dispatch_semaphore_wait(done, dispatch_time(DISPATCH_TIME_NOW, 30 * NSEC_PER_SEC)) == 0);
        assert(std::memcmp(source.contents, destination.contents, length) == 0);
        [options release];
        dispatch_release(done);
        [source release]; [destination release]; [residency release];
        [buffer release]; [allocator release]; [queue release];
    }}
    dispatch_sync(callbackQueue, ^{});
    dispatch_release(callbackQueue);
    feedback.wait();
    [device release];
    std::puts("METAL_QUEUE_LIFETIME_PASS copies=16 replacements=16 readbacks=32 finalized=33");
}}
