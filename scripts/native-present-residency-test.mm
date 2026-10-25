#import <Foundation/Foundation.h>
#import <objc/message.h>
#import <objc/runtime.h>

#include <array>
#include <atomic>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <sys/mman.h>
#include <unistd.h>

#include "present-residency-production.inc"
#include "present-residency-layout.hpp"

static std::atomic<unsigned> liveTokens;

@interface ResidencyToken : NSObject
@end
@implementation ResidencyToken
- (id)init {
    self = [super init];
    if (self) ++liveTokens;
    return self;
}
- (void)dealloc {
    --liveTokens;
    [super dealloc];
}
@end

// The inspected IOGPUMetal4CommandQueue stores its registrations in an
// NSCountedSet. Keep those real Foundation ownership/counting semantics.
@interface ResidencyQueue : NSObject {
@public
    NSCountedSet *sets;
    NSUInteger additions;
    NSUInteger removals;
}
- (void)addResidencySet:(id)set;
- (void)removeResidencySet:(id)set;
@end
@implementation ResidencyQueue
- (id)init {
    self = [super init];
    if (self) sets = [[NSCountedSet alloc] init];
    return self;
}
- (void)addResidencySet:(id)set {
    [sets addObject:set];
    ++additions;
}
- (void)removeResidencySet:(id)set {
    [sets removeObject:set];
    ++removals;
}
- (void)dealloc {
    [sets release];
    [super dealloc];
}
@end

int main() {
    @autoreleasepool {
        auto *image = static_cast<unsigned char *>(mmap(nullptr, kImageSize,
            PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0));
        assert(image != MAP_FAILED);
        std::memcpy(image + kCallSite, kCallBytes, sizeof(kCallBytes));
        std::memcpy(image + kGate, kGateBytes, sizeof(kGateBytes));
        std::memcpy(image + kTrampoline, kTrampolineBytes, sizeof(kTrampolineBytes));
        const auto message = reinterpret_cast<std::uintptr_t>(objc_msgSend);
        std::memcpy(image + kMessageSlot, &message, sizeof(message));

        // Reproduce the native call site's aligned stack and continuation,
        // without loading D3DMetal or executing any GPU/window operations.
        constexpr unsigned char enter[] = {0x48, 0x83, 0xec, 0x08};
        constexpr unsigned char leave[] = {0x48, 0x83, 0xc4, 0x08, 0xc3};
        std::memcpy(image + kCallSite - sizeof(enter), enter, sizeof(enter));
        std::memcpy(image + kCallSite + sizeof(kCallBytes), leave, sizeof(leave));
        const auto pageSize = static_cast<std::size_t>(getpagesize());
        assert(mprotect(image + kCallSite / pageSize * pageSize, pageSize, PROT_READ | PROT_EXEC) == 0);
        assert(mprotect(image + kGate / pageSize * pageSize, pageSize, PROT_READ | PROT_EXEC) == 0);
        using Add = void (*)(id, SEL, id);
        const auto add = reinterpret_cast<Add>(image + kCallSite - sizeof(enter));
        auto &dispatch = *reinterpret_cast<std::uintptr_t *>(image + kDispatchSlot);

        // A constructor that does not publish the sidecar table must retain
        // the original objc_msgSend call and its return/stack behavior.
        auto *fallback = [[ResidencyQueue alloc] init];
        auto *fallbackToken = [[ResidencyToken alloc] init];
        add(fallback, @selector(addResidencySet:), fallbackToken);
        add(fallback, @selector(addResidencySet:), fallbackToken);
        assert([fallback->sets countForObject:fallbackToken] == 2);
        [fallbackToken release];
        [fallback release];
        assert(liveTokens == 0);

        std::array<std::uintptr_t, kHookCount> table{};
        table[kPresentHook] = reinterpret_cast<std::uintptr_t>(&yaagl::pso::addPresentResidency);
        dispatch = reinterpret_cast<std::uintptr_t>(table.data());
        auto *first = [[ResidencyQueue alloc] init];
        auto *second = [[ResidencyQueue alloc] init];
        id baseline = [[NSObject alloc] init];
        [first addResidencySet:baseline];
        [second addResidencySet:baseline];
        auto *shared = [[ResidencyToken alloc] init];
        add(first, @selector(addResidencySet:), shared);
        add(second, @selector(addResidencySet:), shared);
        [shared release];
        for (unsigned frame = 0; frame < 128; ++frame) {
            add(first, @selector(addResidencySet:), shared);
            add(second, @selector(addResidencySet:), shared);
        }
        assert(first->additions == 2 && second->additions == 2);
        assert([first->sets countForObject:shared] == 1);
        assert([second->sets countForObject:shared] == 1);

        for (unsigned generation = 0; generation < 128; ++generation) {
            auto *current = [[ResidencyToken alloc] init];
            add(first, @selector(addResidencySet:), current);
            add(first, @selector(addResidencySet:), current);
            assert([first->sets count] == 2);
            assert([first->sets countForObject:current] == 1);
            assert([first->sets countForObject:baseline] == 1);
            assert([second->sets countForObject:shared] == 1);
            assert(first->removals == generation + 1);
            [current release];
            // One token in each queue, even after more than 32 layer changes.
            assert(liveTokens == 2);
        }
        add(first, @selector(addResidencySet:), shared);
        assert([first->sets countForObject:shared] == 1);
        assert(liveTokens == 1);
        [first release];
        assert(liveTokens == 1);
        [second release];
        assert(liveTokens == 0);
        [baseline release];
        assert(munmap(image, kImageSize) == 0);
        std::puts("PRESENT_RESIDENCY_CPU_GATE_AND_LIFETIME_PASS");
    }
}
