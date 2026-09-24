#include "display-routing.mm"

#include <array>
#include <cassert>
#include <cstdint>
#include <cstdio>

using namespace yaagl::pso::display;

namespace {

struct FakeOutput;
struct FakeOwner {
    void** methods = nullptr;
    FakeOutput* output = nullptr;
    void* unknown[1]{};
    void* external[2]{};
    int releases = 0;
};
struct FakeOutput {
    alignas(8) std::array<std::byte, 0x1a0> native{};
    std::array<void*, 4> swapchains{};
    FakeOwner owner;
    void* address() { return native.data(); }
    void* external() { return &owner.external[1]; }
};
struct FakeSwapchain {
    alignas(8) std::array<std::byte, 0x140> native{};
    void* address() { return native.data(); }
};
struct FakeAdapter {
    FakeOutput* outputs[2]{};
};

FakeAdapter adapter;
std::uintptr_t windowMonitor = 2;
std::uint32_t currentHz[3] = {0, 120, 144};
int flushes = 0;
int additions = 0;
std::uintptr_t modeMonitor = 0;

void releaseUnknown(void* unknown) {
    auto* owner = reinterpret_cast<FakeOwner*>(static_cast<char*>(unknown) - offsetof(FakeOwner, unknown));
    ++owner->releases;
}
void* asInterface(void* owner, const void* id) {
    auto* fake = static_cast<FakeOwner*>(owner);
    const auto* guid = static_cast<const Guid*>(id);
    if (guid->first == kNativeOutput.first) return fake->output->address();
    if (guid->first == kReleaseInterface.first) return fake->unknown;
    return nullptr;
}
std::int32_t externalGetDesc(void*, void*) {
    // Native routing uses the output object, not a call back into the PE vtable.
    return kInvalidCall;
}
void flush(void*) { ++flushes; }
int queryDisplay(std::uintptr_t, std::uintptr_t monitor, yaagl_d3dmetal_display* result) {
    if (!monitor) monitor = windowMonitor;
    if (monitor == 0 || monitor > 2) return 0;
    result->monitor = monitor;
    result->refresh_rate = currentHz[monitor];
    return 1;
}
std::int32_t enumerate(void*, std::uint32_t index, void** result) {
    if (index >= 2) { *result = nullptr; return static_cast<std::int32_t>(0x887a0002u); }
    *result = adapter.outputs[index]->external();
    return 0;
}
void addSwapchain(void* output, void* swapchain) {
    ++additions;
    auto* end = load<void**>(output, kOutputSwapchainsEnd);
    *end++ = swapchain;
    store(output, kOutputSwapchainsEnd, end);
}
std::int32_t originalGet(void* swapchain, void** result) {
    void* output = load<void*>(swapchain, kOutput);
    for (FakeOutput* candidate : adapter.outputs) {
        if (!output || candidate->address() == output) {
            *result = candidate->external();
            return 0;
        }
    }
    return -1;
}
std::int32_t originalSet(void* swapchain, int fullscreen, void*) {
    modeMonitor = load<std::uintptr_t>(load<void*>(swapchain, kOutput), kOutputMonitor);
    store(swapchain, kFullscreen, static_cast<std::uint8_t>(fullscreen != 0));
    return 0;
}

void initOutput(FakeOutput& output, std::uintptr_t monitor) {
    static void* ownerMethods[4] = {nullptr, nullptr, nullptr, reinterpret_cast<void*>(&asInterface)};
    static void* unknownMethods[6] = {nullptr, nullptr, nullptr, nullptr, nullptr,
                                       reinterpret_cast<void*>(&releaseUnknown)};
    static void* externalMethods[8] = {nullptr, nullptr, nullptr, nullptr, nullptr,
                                        nullptr, nullptr, reinterpret_cast<void*>(&externalGetDesc)};
    output.owner.methods = ownerMethods;
    output.owner.output = &output;
    output.owner.unknown[0] = unknownMethods;
    output.owner.external[0] = &output.owner;
    output.owner.external[1] = externalMethods;
    store(output.address(), kOutputAdapter, &adapter);
    store(output.address(), kOutputMonitor, monitor);
    store(output.address(), kOutputRefresh, 60u); // Registry-saved, not current.
    store(output.address(), kOutputSwapchains, output.swapchains.data());
    store(output.address(), kOutputSwapchainsEnd, output.swapchains.data());
    store(output.address(), kOutputReferences, 0x200000002ull);
}

void exerciseOutputRouting() {
    FakeOutput first, second;
    FakeSwapchain chain;
    initOutput(first, 1);
    initOutput(second, 2);
    adapter.outputs[0] = &first;
    adapter.outputs[1] = &second;
    static void* queueMethods[3] = {nullptr, nullptr, reinterpret_cast<void*>(&flush)};
    void* queue[1] = {queueMethods};
    store(chain.address(), kQueue, queue);
    store(chain.address(), kHwnd, reinterpret_cast<void*>(0x1234));
    runtime.query = &queryDisplay;
    runtime.originalGet = &originalGet;
    runtime.originalSet = &originalSet;
    runtime.enumerate = &enumerate;
    runtime.addSwapChain = &addSwapchain;

    void* containing = nullptr;
    assert(getContainingOutput(chain.address(), &containing) == 0);
    assert(containing == second.external()); // Constructor: not output 0.
    store(chain.address(), kOutput, second.address());
    addReference(second.address()); // Native constructor owns one output reference.
    addSwapchain(second.address(), chain.address()); // Native constructor's membership.
    assert(load<std::uint32_t>(second.address(), kOutputRefresh) == 144);

    windowMonitor = 1;
    {
        Locked guard(lockAt(chain.address(), kSwapchainLock));
        presentFlush(chain.address());
    }
    assert(load<void*>(chain.address(), kOutput) == first.address());
    assert(load<void**>(second.address(), kOutputSwapchainsEnd) == second.swapchains.data());
    assert(load<void**>(first.address(), kOutputSwapchainsEnd) == first.swapchains.data() + 1);
    assert(load<std::uint64_t>(second.address(), kOutputReferences) == 0x200000002ull);
    assert(load<std::uint64_t>(first.address(), kOutputReferences) == 0x300000003ull);
    assert(load<std::uint32_t>(first.address(), kOutputRefresh) == 120);
    assert(flushes == 1);
    const int before = additions;
    currentHz[1] = 90;
    {
        Locked guard(lockAt(chain.address(), kSwapchainLock));
        presentFlush(chain.address());
    }
    assert(additions == before); // No vector growth/work on steady-state frames.
    assert(load<std::uint32_t>(first.address(), kOutputRefresh) == 90);
    assert(flushes == 2);
    containing = nullptr;
    assert(getContainingOutput(chain.address(), &containing) == 0);
    assert(containing == first.external());

    assert(setFullscreenState(chain.address(), 1, second.external()) == 0);
    assert(modeMonitor == 2);
    assert(load<std::uint8_t>(chain.address(), kFullscreen) == 1);
    assert(load<void*>(chain.address(), kOutput) == second.address());
    windowMonitor = 1;
    {
        Locked guard(lockAt(chain.address(), kSwapchainLock));
        presentFlush(chain.address());
    }
    assert(load<void*>(chain.address(), kOutput) == second.address());
    assert(load<std::uint32_t>(second.address(), kOutputRefresh) == 144);
    assert(setFullscreenState(chain.address(), 0, nullptr) == 0);
    assert(modeMonitor == 2); // Native exit runs against old fullscreen target.
    assert(load<void*>(chain.address(), kOutput) == first.address());
    assert(load<std::uint8_t>(chain.address(), kFullscreen) == 0);
}

} // namespace

int main() {
    exerciseOutputRouting();
    std::puts("native output routing: passed");
}
