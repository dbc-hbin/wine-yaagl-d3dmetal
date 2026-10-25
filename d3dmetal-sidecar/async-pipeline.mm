#import <Foundation/Foundation.h>
#include <dispatch/dispatch.h>
#include <objc/runtime.h>

#include <algorithm>
#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <set>
#include <tuple>
#include <unordered_map>
#include <vector>

#include "async-pipeline.hpp"
#include "async-pipeline-state.hpp"
#include "../include/yaagl_macho_symbols.hpp"

@interface YAAGLAsyncTargetHistory : NSObject {
@public
    std::mutex mutex;
    yaagl::pso::async_pipeline::TargetHistory<std::uint64_t> history;
}
@end
@implementation YAAGLAsyncTargetHistory
@end

@interface YAAGLDX11TargetHistory : NSObject {
@public
    std::mutex mutex;
    yaagl::pso::async_pipeline::TargetHistory<std::array<std::uint8_t, 10>> history;
}
@end
@implementation YAAGLDX11TargetHistory
@end

namespace yaagl::pso::async_pipeline {
namespace {

using GraphicsCtor = void (*)(void*, void*, const void*);
using Getter = void* (*)(void*, std::uint32_t, std::uint64_t);
using Destroy = void (*)(void*);
using Draw = void (__attribute__((ms_abi)) *)(void*, std::uint32_t, std::uint32_t,
                                             std::uint32_t, std::uint32_t);
using DrawIndexed = void (__attribute__((ms_abi)) *)(void*, std::uint32_t, std::uint32_t,
                                                    std::uint32_t, std::int32_t, std::uint32_t);
using RootCtor = void (*)(void*, void*, const void*, std::uint32_t);
using Reset = std::int32_t (__attribute__((ms_abi)) *)(void*, void*, void*);
using Query = void (__attribute__((ms_abi)) *)(void*, void*, std::uint32_t, std::uint32_t);
using Present = std::int32_t (__attribute__((ms_abi)) *)(void*, std::uint32_t, std::uint32_t, const void*);
using ParseRoot = RootDescription* (*)(const void*, std::uint32_t, void*);
using ReleaseRoot = void (*)(RootDescription*);

std::array<std::uintptr_t, kHookCount> original{};
ParseRoot parseRoot = nullptr;
ReleaseRoot releaseRoot = nullptr;
const std::uint64_t* dxgiInfo = nullptr;
bool enabled = false;
bool logging = false;
char historyKey;
char dx11HistoryKey;
std::mutex historyCreationMutex;

// The native getters return an 18-byte packed D3DMTextureViewDesc through the
// SysV hidden result pointer. Keep every subresource/plane byte in its identity.
struct DX11Attachment {
    id texture = nil;
    std::array<std::uint8_t, 10> subresource{};
};
static_assert(offsetof(DX11Attachment, subresource) == 8 && sizeof(DX11Attachment) == 24);
using DX11Attachments = std::array<DX11Attachment, 9>;
using NativeDraw = void (*)(void*, std::uint32_t, std::uint32_t, std::uint32_t, std::uint32_t);
using NativeDrawIndexed = void (*)(void*, std::uint32_t, std::uint32_t, std::uint32_t,
                                   std::int32_t, std::uint32_t);
using DX11Query = void (__attribute__((ms_abi)) *)(void*, void*);
using DX11Destroy = void (*)(void*, void*);
void (*dx11PushState)(void*) = nullptr;
void (*dx11MergeResources)(void*) = nullptr;
void (*dx11GetRTV)(DX11Attachment*, void*) = nullptr;
void (*dx11GetDSV)(DX11Attachment*, void*) = nullptr;
void (*dx11Lock)(void*, std::uint32_t) = nullptr;
void (*dx11Unlock)(void*) = nullptr;
NativeDraw dx11EmitDraw = nullptr;
NativeDrawIndexed dx11EmitDrawIndexed = nullptr;

struct Pipeline {
    std::shared_ptr<State> state;
    void* device;
    std::uint32_t rootId;
};
struct Root {
    bool safe;
    void* device;
    std::uint32_t id;
};
struct List {
    bool initialized = false;
    std::set<std::tuple<std::uintptr_t, std::uint32_t, std::uint32_t>> queries;
};
struct Runtime {
    std::mutex mutex;
    std::atomic<bool> managedPipelinePublished{false};
    std::unordered_map<void*, Pipeline> pipelines;
    std::unordered_map<void*, Root> roots;
    std::unordered_map<void*, List> lists;
    std::unordered_map<void*, std::set<void*>> dx11Queries;
    std::atomic<std::uint64_t> frame{1};
    std::atomic<std::uint64_t> queued{0}, ready{0}, failed{0}, skipped{0}, synchronous{0};
    dispatch_queue_t queue = nullptr;
};
Runtime& runtime() {
    // The loaded framework owns the sidecar for process lifetime. Individual
    // native owners retire every map entry; shutdown must not destroy locks
    // before the framework's own global destructors finish.
    static Runtime* value = new Runtime;
    return *value;
}

template<class T> T field(const void* object, std::size_t offset) noexcept {
    T value;
    std::memcpy(&value, static_cast<const std::uint8_t*>(object) + offset, sizeof(value));
    return value;
}

std::shared_ptr<State> stateFor(void* native) {
    auto& rt = runtime();
    // Monotonic: a DX11-only process never needs this DX12 owner map. Publish
    // before any managed PSO/job escapes; never race retirement by resetting it.
    if (!rt.managedPipelinePublished.load(std::memory_order_acquire)) return nullptr;
    std::lock_guard lock(rt.mutex);
    const auto found = rt.pipelines.find(native);
    return found == rt.pipelines.end() ? nullptr : found->second.state;
}

void recordSynchronous(void* native) {
    const auto count = ++runtime().synchronous;
    if (logging && (count & (count - 1)) == 0)
        std::fprintf(stderr, "[D3DMetal async] synchronous=%llu pso=%p\n",
                     static_cast<unsigned long long>(count), native);
}

struct Construction;
thread_local Construction* construction = nullptr;
struct Construction {
    void* native;
    std::vector<Key> keys;
    Construction* previous;
    explicit Construction(void* value) : native(value), previous(construction) { construction = this; }
    ~Construction() { construction = previous; }
};

void* getRenderPipeline(void* native, std::uint32_t flags, std::uint64_t formats) {
    if (enabled && construction && construction->native == native) {
        // The only ctor callsite ignores this return. DXIL translation, function
        // loading, reflection and static-state setup have already run natively.
        construction->keys.push_back({flags, formats});
        return nullptr;
    }
    if (enabled) {
        if (auto state = stateFor(native)) {
            recordSynchronous(native);
            return state->request({flags, formats}, false).pipeline;
        }
    }
    return reinterpret_cast<Getter>(original[1])(native, flags, formats);
}

void graphicsCtor(void* native, void* device, const void* description) {
    if (!enabled || field<std::uint32_t>(device, 0x58) != 2) {
        reinterpret_cast<GraphicsCtor>(original[0])(native, device, description);
        return;
    }
    std::vector<Key> keys;
    {
        Construction context(native);
        reinterpret_cast<GraphicsCtor>(original[0])(native, device, description);
        keys = std::move(context.keys);
    }
    if (keys.empty() || field<std::uint8_t>(native, 0x28)) return;
    auto state = std::make_shared<State>(
        [native](Key key) -> void* {
            @autoreleasepool {
                void* result = reinterpret_cast<Getter>(original[1])(native, key.flags, key.formats);
                auto& rt = runtime();
                if (result) ++rt.ready; else ++rt.failed;
                if (logging) std::fprintf(stderr,
                    "[D3DMetal async] %s pso=%p flags=%x formats=%llx\n",
                    result ? "ready" : "failed", native, key.flags,
                    static_cast<unsigned long long>(key.formats));
                return result;
            }
        },
        [native](std::function<void()> work) {
            auto& rt = runtime();
            auto* owned = new std::function<void()>(std::move(work));
            ++rt.queued;
            if (logging) std::fprintf(stderr, "[D3DMetal async] queued pso=%p\n", native);
            dispatch_async_f(rt.queue, owned, [](void* context) {
                std::unique_ptr<std::function<void()>> work(static_cast<std::function<void()>*>(context));
                (*work)();
            });
        });
    {
        auto& rt = runtime();
        std::lock_guard lock(rt.mutex);
        rt.pipelines.emplace(native, Pipeline{state, device, field<std::uint32_t>(description, 8)});
        rt.managedPipelinePublished.store(true, std::memory_order_release);
    }
    // Publish all static constructor writes before admitting any worker.
    for (const auto key : keys) state->request(key, true);
}

void graphicsDestroy(void* native) {
    if (enabled) {
        if (auto state = stateFor(native)) {
            state->stop();
            state->wait();
            auto& rt = runtime();
            std::lock_guard lock(rt.mutex);
            rt.pipelines.erase(native);
        }
    }
    reinterpret_cast<Destroy>(original[2])(native);
}

void rootCtor(void* root, void* device, const void* bytes, std::uint32_t length) {
    reinterpret_cast<RootCtor>(original[5])(root, device, bytes, length);
    if (!enabled) return;
    bool safe = false;
    if (field<std::int32_t>(root, 0x638) == 0) {
        RootDescription* parsed = parseRoot(bytes, length, nullptr);
        safe = rootHasNoUAV(parsed);
        if (parsed) releaseRoot(parsed);
    }
    auto& rt = runtime();
    std::lock_guard lock(rt.mutex);
    rt.roots.insert_or_assign(root, Root{safe, static_cast<std::uint8_t*>(device) + 0x18,
                                      field<std::uint32_t>(root, 0x610)});
}

void rootDestroy(void* root) {
    if (enabled) {
        auto& rt = runtime();
        std::lock_guard lock(rt.mutex);
        rt.roots.erase(root);
    }
    reinterpret_cast<Destroy>(original[6])(root);
}

std::int32_t __attribute__((ms_abi)) reset(void* list, void* allocator, void* initialPipeline) {
    const auto result = reinterpret_cast<Reset>(original[7])(list, allocator, initialPipeline);
    if (enabled && result >= 0) {
        auto& rt = runtime();
        std::lock_guard lock(rt.mutex);
        rt.lists.insert_or_assign(list, List{true, {}});
    }
    return result;
}

void __attribute__((ms_abi)) beginQuery(void* list, void* heap, std::uint32_t type, std::uint32_t index) {
    if (enabled) {
        auto& rt = runtime();
        std::lock_guard lock(rt.mutex);
        rt.lists[list].queries.emplace(reinterpret_cast<std::uintptr_t>(heap), type, index);
    }
    reinterpret_cast<Query>(original[8])(list, heap, type, index);
}

void __attribute__((ms_abi)) endQuery(void* list, void* heap, std::uint32_t type, std::uint32_t index) {
    reinterpret_cast<Query>(original[9])(list, heap, type, index);
    if (enabled) {
        auto& rt = runtime();
        std::lock_guard lock(rt.mutex);
        const auto found = rt.lists.find(list);
        // Timestamp EndQuery without BeginQuery cannot decrement another query.
        if (found != rt.lists.end())
            found->second.queries.erase({reinterpret_cast<std::uintptr_t>(heap), type, index});
    }
}

void listDestroy(void* list) {
    if (enabled) {
        auto& rt = runtime();
        std::lock_guard lock(rt.mutex);
        rt.lists.erase(list);
    }
    reinterpret_cast<Destroy>(original[10])(list);
}

std::int32_t __attribute__((ms_abi)) present(void* swapchain, std::uint32_t interval,
                                            std::uint32_t flags, const void* parameters) {
    if (!enabled)
        return reinterpret_cast<Present>(original[11])(swapchain, interval, flags, parameters);
    // The pinned IDXGISwapChain4 Present thunk subtracts 0x10 from the public
    // interface before entering this native object (also used by the unpack bridge).
    const auto frame = presentationHistory().take(static_cast<std::uint8_t*>(swapchain) + 0x10);
    const auto before = field<std::uint64_t>(swapchain, 0x120);
    const auto result = reinterpret_cast<Present>(original[11])(swapchain, interval, flags, parameters);
    const bool advanced = result >= 0 && !(flags & 1) &&
        field<std::uint64_t>(swapchain, 0x120) != before;
    if (presentationHistory().complete(frame, advanced)) ++runtime().frame;
    return result;
}

template<class Create>
id targetHistory(id texture, const void* key, bool observe, Create create) {
    id history = objc_getAssociatedObject(texture, key);
    if (!history && observe) {
        std::lock_guard lock(historyCreationMutex);
        history = objc_getAssociatedObject(texture, key);
        if (!history) {
            history = create();
            objc_setAssociatedObject(texture, key, history, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
            [history release];
        }
    }
    return history;
}

template<class History, class Key, std::size_t N>
bool useTargets(const std::array<std::pair<History*, Key>, N>& targets, std::size_t count,
                bool observe, bool requireEligible = false) {
    if (!count) return false;
    struct Observation {
        std::uint64_t identity = 0;
        Key key{};
        std::uint64_t revision = 0;
    };
    struct Observed {
        std::array<Observation, N> entries{};
        std::size_t count = 0;
        std::uint64_t frame = 0;
    };
    static thread_local Observed observed;
    auto frame = runtime().frame.load();
    const bool remember = observe && !requireEligible;
    if (remember && observed.count == count && observed.frame == frame) {
        bool unchanged = true;
        for (std::size_t i = 0; i < count; ++i) {
            const auto* owner = targets[i].first;
            const auto& entry = observed.entries[i];
            if (!owner || owner->history.identity() != entry.identity ||
                targets[i].second != entry.key ||
                owner->history.observationRevision() != entry.revision) {
                unchanged = false;
                break;
            }
        }
        // Tickets only describe completed observations, never eligibility.
        // Unique history identities prevent reuse even if an address and its
        // initial revision match a destroyed texture's last observation.
        if (unchanged) return true;
    }
    std::array<History*, N> owners{};
    for (std::size_t i = 0; i < count; ++i) {
        if (!targets[i].first) return false;
        owners[i] = targets[i].first;
    }
    std::sort(owners.begin(), owners.begin() + count, std::less<History*>{});
    const auto end = std::unique(owners.begin(), owners.begin() + count);
    std::array<std::unique_lock<std::mutex>, N> locks;
    for (auto owner = owners.begin(); owner != end; ++owner)
        locks[owner - owners.begin()] = std::unique_lock((*owner)->mutex);

    // Preserve a single multi-attachment decision, including DX11's conditional
    // observation, without serializing unrelated textures. Never acquire the
    // runtime/creation mutex while holding these ordered history locks.
    frame = runtime().frame.load();
    bool recurrent = true;
    if (!observe || requireEligible) {
        for (std::size_t i = 0; i < count; ++i)
            if (!targets[i].first->history.use(targets[i].second, frame, false)) recurrent = false;
    }
    if (observe && (!requireEligible || recurrent)) {
        if (remember) observed.count = 0;
        for (std::size_t i = 0; i < count; ++i) {
            auto& history = targets[i].first->history;
            history.use(targets[i].second, frame, true);
            if (remember)
                observed.entries[i] = {history.identity(), targets[i].second,
                                       history.observationRevision()};
        }
        // A later subresource observation can reset an earlier entry of the
        // same history. Its earlier ticket must then miss, not inherit the
        // final revision of that reset history.
        if (remember) {
            observed.frame = frame;
            observed.count = count;
        }
    }
    return recurrent;
}

// Associations remain the lifetime boundary; no texture address/history pair
// survives a draw. Each texture retains the same bounded subresource history.
bool targets(void* list, bool observe) {
    const auto count = field<std::uint32_t>(list, 0x3f4);
    if (count > 8) return false;
    std::array<std::pair<YAAGLAsyncTargetHistory*, std::uint64_t>, 8 + 2> entries{};
    std::size_t used = 0;
    const auto visit = [&](std::size_t offset) {
        id texture = field<id>(list, offset);
        if (!texture) return;
        const auto mip = field<std::uint32_t>(list, offset + 8);
        const auto slice = field<std::uint32_t>(list, offset + 12);
        const auto key = (std::uint64_t(mip) << 32) | slice;
        auto* history = static_cast<YAAGLAsyncTargetHistory*>(
            targetHistory(texture, &historyKey, observe,
                          [] { return [[YAAGLAsyncTargetHistory alloc] init]; }));
        entries[used++] = {history, key};
    };
    for (std::uint32_t i = 0; i < count; ++i) visit(0x1d0 + i * 0x38);
    visit(0x390);
    visit(0x3b0);
    return useTargets(entries, used, observe);
}

Key dynamicKey(void* list, void* native) {
    std::uint64_t formats = field<std::uint64_t>(list, 0x130);
    if (!field<std::uint8_t>(list, 0x138)) {
        formats = 0;
        const auto count = field<std::uint32_t>(list, 0x3f4);
        for (std::uint32_t i = 0; i < count; ++i) {
            auto format = field<std::uint32_t>(list, 0x3d0 + i * 4);
            if (format > 0xbf) format = 0;
            formats |= dxgiInfo[format * 11] << (i * 8);
        }
    }
    auto samples = field<std::uint32_t>(list, 0x13c);
    if (!samples) samples = field<std::uint8_t>(native, 0x326);
    return {std::uint32_t(field<std::uint8_t>(list, 0x428)) | ((samples & 0x1f) << 1) |
            ((field<std::uint32_t>(list, 0x3f0) & 0xffff) << 6), formats};
}

bool skipDraw(void* list) {
    if (!enabled) return false;
    void* native = field<void*>(list, 0x100);
    if (!native) return false;
    std::shared_ptr<State> state;
    Key key{};
    bool eligible = false;
    struct Ready {
        void* native = nullptr;
        Key key{};
        std::shared_ptr<State> state;
        std::uint64_t revision = 0;
    };
    static thread_local Ready ready;
    const bool validKey = field<std::uint32_t>(list, 0x3f4) <= 8;
    if (validKey) {
        key = dynamicKey(list, native);
        if (ready.native == native && ready.key == key && ready.state &&
            ready.state->matchesRevision(ready.revision)) return false;
    }
    {
        auto& rt = runtime();
        std::lock_guard lock(rt.mutex);
        const auto pipeline = rt.pipelines.find(native);
        if (pipeline == rt.pipelines.end()) return false;
        // Retain the owner in the same lookup used for eligibility. No native
        // compilation or waiting may run while the runtime mutex is held.
        state = pipeline->second.state;
        if (validKey) {
            if (const auto revision = state->readyRevision(key)) {
                // Only positive registry-backed readiness is cached. stop()
                // invalidates it before native destruction/address reuse.
                ready = {native, key, state, revision};
                return false;
            }
        }
        const auto root = rt.roots.find(field<void*>(list, 0xe48));
        const auto commands = rt.lists.find(list);
        eligible = root != rt.roots.end() && root->second.safe &&
            root->second.device == pipeline->second.device &&
            root->second.id == pipeline->second.rootId &&
            commands != rt.lists.end() && commands->second.initialized && commands->second.queries.empty() &&
            field<std::uint32_t>(list, 0x70) == 0 && // Direct lists, not bundles.
            field<std::uint32_t>(native, 0x44) == 0 &&
            !field<std::uint8_t>(native, 0x32e) && !field<std::uint8_t>(list, 0x428) &&
            targets(list, false);
    }
    if (eligible && state->request(key, true).status == Status::Pending) {
        auto& rt = runtime();
        const auto skipped = ++rt.skipped;
        // Keep an already established recurring target alive across pending
        // frames, without promoting any previously ineligible target.
        targets(list, true);
        if (logging && (skipped == 1 || (skipped & (skipped - 1)) == 0))
            std::fprintf(stderr, "[D3DMetal async] skipped=%llu pso=%p\n",
                         static_cast<unsigned long long>(skipped), native);
        return true;
    }
    if (!eligible) {
        recordSynchronous(native);
        state->wait();
    }
    return false;
}

void recordDraw(void* list, std::uint32_t count, std::uint32_t instances) {
    if (!enabled || !count || !instances) return;
    void* native = field<void*>(list, 0x100);
    if (!native || field<std::uint8_t>(native, 0x28)) return;
    targets(list, true);
}

void __attribute__((ms_abi)) draw(void* list, std::uint32_t count, std::uint32_t instances,
                                std::uint32_t start, std::uint32_t baseInstance) {
    if (skipDraw(list)) return;
    reinterpret_cast<Draw>(original[3])(list, count, instances, start, baseInstance);
    recordDraw(list, count, instances);
}

void __attribute__((ms_abi)) drawIndexed(void* list, std::uint32_t count, std::uint32_t instances,
                                       std::uint32_t start, std::int32_t baseVertex,
                                       std::uint32_t baseInstance) {
    if (skipDraw(list)) return;
    reinterpret_cast<DrawIndexed>(original[4])(list, count, instances, start, baseVertex, baseInstance);
    recordDraw(list, count, instances);
}

class DX11ContextLock {
    void* lock_;
public:
    explicit DX11ContextLock(void* context)
        : lock_(field<std::uint8_t>(context, 0x1ad0) == 1
                    ? static_cast<std::uint8_t*>(context) + 0x1ad4 : nullptr) {
        if (lock_) dx11Lock(lock_, 0);
    }
    ~DX11ContextLock() { if (lock_) dx11Unlock(lock_); }
};

DX11Attachments dx11Attachments(void* context) {
    DX11Attachments result{};
    const auto count = field<std::uint32_t>(context, 0x9588);
    if (count > 8) return result;
    for (std::uint32_t i = 0; i < count; ++i) {
        if (void* view = field<void*>(context, 0x9590 + i * 8)) dx11GetRTV(&result[i], view);
    }
    if (void* view = field<void*>(context, 0x95d0)) dx11GetDSV(&result.back(), view);
    return result;
}

// Native descriptor resolution happens under the context lock, before taking
// the runtime mutex. Histories are attached to the returned Metal texture views.
bool dx11Targets(const DX11Attachments& attachments, bool observe, bool requireEligible = false) {
    std::array<std::pair<YAAGLDX11TargetHistory*, decltype(DX11Attachment::subresource)>,
               std::tuple_size_v<DX11Attachments>> entries{};
    std::size_t used = 0;
    for (const auto& attachment : attachments) {
        if (!attachment.texture) continue;
        auto* history = static_cast<YAAGLDX11TargetHistory*>(
            targetHistory(attachment.texture, &dx11HistoryKey, observe && !requireEligible,
                          [] { return [[YAAGLDX11TargetHistory alloc] init]; }));
        entries[used++] = {history, attachment.subresource};
    }
    return useTargets(entries, used, observe, requireEligible);
}

bool dx11SkipDraw(void* context, const DX11Attachments& attachments) {
    void* native = field<void*>(context, 0x1be8);
    if (!native) return false;
    // Legacy beta3 already schedules compilation. Its callback clears this
    // atomic byte only after GetRenderPipelineState returns; its destructor
    // waits on the same byte. A clear byte is NOT proof that every dynamic
    // variant exists: those misses must still reach the original native path.
    auto& pending = *reinterpret_cast<std::uint8_t*>(static_cast<std::uint8_t*>(native) + 0x38);
    if (!std::atomic_ref<std::uint8_t>(pending).load(std::memory_order_acquire)) return false;
    if (!field<void*>(context, 0x1c08) || field<void*>(context, 0x1c10) ||
        field<void*>(context, 0x1c18) || field<void*>(context, 0x1c20) ||
        field<void*>(context, 0x9d30) || field<std::uint8_t>(context, 0x9d3c)) return false;
    // Native occupied-UAV masks, including the mirrored VS/PS bindings.
    for (std::size_t stage = 0; stage < 5; ++stage)
        if (field<std::uint64_t>(context, 0x94f8 + stage * 8)) return false;
    auto& rt = runtime();
    std::uint64_t skipped;
    {
        std::lock_guard lock(rt.mutex);
        const auto queries = rt.dx11Queries.find(context);
        if ((queries != rt.dx11Queries.end() && !queries->second.empty()) ||
            !dx11Targets(attachments, true, true)) return false;
        skipped = ++rt.skipped;
    }
    if (logging && (skipped == 1 || (skipped & (skipped - 1)) == 0))
        std::fprintf(stderr, "[D3DMetal async] dx11-skipped=%llu pso=%p\n",
                     static_cast<unsigned long long>(skipped), native);
    return true;
}

template<class Emit>
void dx11Draw(void* context, std::uint32_t count, std::uint32_t instances, Emit emit) {
    DX11ContextLock lock(context);
    // Preserve the native sequence: publish pending bindings, encode the draw,
    // merge resource usage. SetGraphicsPipelineState replay only marks state
    // dirty; compilation is awaited by the later draw, not by that binding.
    dx11PushState(context);
    const auto attachments = dx11Attachments(context);
    if (!count || !instances || !dx11SkipDraw(context, attachments)) {
        emit(static_cast<std::uint8_t*>(field<void*>(context, 0x1a90)) + 0x58);
        if (count && instances && field<void*>(context, 0x1be8))
            dx11Targets(attachments, true);
    }
    dx11MergeResources(context);
}

void __attribute__((ms_abi)) dx11DrawInstanced(void* context, std::uint32_t count,
    std::uint32_t instances, std::uint32_t start, std::uint32_t baseInstance) {
    if (!enabled) {
        reinterpret_cast<Draw>(original[12])(context, count, instances, start, baseInstance);
        return;
    }
    dx11Draw(context, count, instances, [&](void* list) {
        dx11EmitDraw(list, count, instances, start, baseInstance);
    });
}

void __attribute__((ms_abi)) dx11DrawIndexedInstanced(void* context, std::uint32_t count,
    std::uint32_t instances, std::uint32_t start, std::int32_t baseVertex, std::uint32_t baseInstance) {
    if (!enabled) {
        reinterpret_cast<DrawIndexed>(original[13])(context, count, instances, start, baseVertex, baseInstance);
        return;
    }
    dx11Draw(context, count, instances, [&](void* list) {
        dx11EmitDrawIndexed(list, count, instances, start, baseVertex, baseInstance);
    });
}

void __attribute__((ms_abi)) dx11Begin(void* context, void* query) {
    if (!enabled) { reinterpret_cast<DX11Query>(original[14])(context, query); return; }
    DX11ContextLock lock(context);
    {
        auto& rt = runtime();
        std::lock_guard historyLock(rt.mutex);
        rt.dx11Queries[context].insert(query);
    }
    reinterpret_cast<DX11Query>(original[14])(context, query);
}

void __attribute__((ms_abi)) dx11End(void* context, void* query) {
    if (!enabled) { reinterpret_cast<DX11Query>(original[15])(context, query); return; }
    DX11ContextLock lock(context);
    reinterpret_cast<DX11Query>(original[15])(context, query);
    auto& rt = runtime();
    std::lock_guard historyLock(rt.mutex);
    const auto found = rt.dx11Queries.find(context);
    if (found != rt.dx11Queries.end()) {
        found->second.erase(query);
        if (found->second.empty()) rt.dx11Queries.erase(found);
    }
}

void dx11Destroy(void* context, void* vtt) {
    if (enabled) {
        auto& rt = runtime();
        std::lock_guard lock(rt.mutex);
        rt.dx11Queries.erase(context);
    }
    reinterpret_cast<DX11Destroy>(original[16])(context, vtt);
}

} // namespace

std::array<std::uintptr_t, kHookCount> initialize(
    const std::uint8_t* image, const std::array<std::uintptr_t, kHookCount>& originals) {
    original = originals;
    const std::array<std::uintptr_t, kHookCount> hooks = {
        reinterpret_cast<std::uintptr_t>(&graphicsCtor), reinterpret_cast<std::uintptr_t>(&getRenderPipeline),
        reinterpret_cast<std::uintptr_t>(&graphicsDestroy), reinterpret_cast<std::uintptr_t>(&draw),
        reinterpret_cast<std::uintptr_t>(&drawIndexed), reinterpret_cast<std::uintptr_t>(&rootCtor),
        reinterpret_cast<std::uintptr_t>(&rootDestroy), reinterpret_cast<std::uintptr_t>(&reset),
        reinterpret_cast<std::uintptr_t>(&beginQuery), reinterpret_cast<std::uintptr_t>(&endQuery),
        reinterpret_cast<std::uintptr_t>(&listDestroy), reinterpret_cast<std::uintptr_t>(&present),
        reinterpret_cast<std::uintptr_t>(&dx11DrawInstanced),
        reinterpret_cast<std::uintptr_t>(&dx11DrawIndexedInstanced),
        reinterpret_cast<std::uintptr_t>(&dx11Begin), reinterpret_cast<std::uintptr_t>(&dx11End),
        reinterpret_cast<std::uintptr_t>(&dx11Destroy),
    };
    const char* option = std::getenv("YAAGL_D3DMETAL_ASYNC");
    if (!option || std::strcmp(option, "1") != 0) return hooks;
    const macho::Symbols symbols(reinterpret_cast<const mach_header_64*>(image));
    dxgiInfo = static_cast<const std::uint64_t*>(symbols.find("_dxgi_info", false));
    if (!dxgiInfo) {
        std::fprintf(stderr, "[D3DMetal async] disabled: native DXGI format table not found\n");
        return hooks;
    }
    // These import stubs are part of the exact b3 image verified by bridge.mm.
    parseRoot = reinterpret_cast<ParseRoot>(const_cast<std::uint8_t*>(image) + 0x39130a);
    releaseRoot = reinterpret_cast<ReleaseRoot>(const_cast<std::uint8_t*>(image) + 0x391310);
    dx11PushState = reinterpret_cast<decltype(dx11PushState)>(const_cast<std::uint8_t*>(image) + 0xe13d4);
    dx11MergeResources = reinterpret_cast<decltype(dx11MergeResources)>(const_cast<std::uint8_t*>(image) + 0xdf5bc);
    dx11GetRTV = reinterpret_cast<decltype(dx11GetRTV)>(const_cast<std::uint8_t*>(image) + 0xd0310);
    dx11GetDSV = reinterpret_cast<decltype(dx11GetDSV)>(const_cast<std::uint8_t*>(image) + 0xd0368);
    dx11Lock = reinterpret_cast<decltype(dx11Lock)>(const_cast<std::uint8_t*>(image) + 0x3918e6);
    dx11Unlock = reinterpret_cast<decltype(dx11Unlock)>(const_cast<std::uint8_t*>(image) + 0x3918ec);
    dx11EmitDraw = reinterpret_cast<NativeDraw>(const_cast<std::uint8_t*>(image) + 0x5d02a);
    dx11EmitDrawIndexed = reinterpret_cast<NativeDrawIndexed>(const_cast<std::uint8_t*>(image) + 0x5d280);
    option = std::getenv("YAAGL_D3DMETAL_ASYNC_LOG");
    logging = option && std::strcmp(option, "1") == 0;
    runtime().queue = dispatch_queue_create("yaagl.d3dmetal.graphics-pipelines", DISPATCH_QUEUE_CONCURRENT);
    enabled = true;
    if (logging) std::fprintf(stderr, "[D3DMetal async] enabled: DX12 MPL and DX11 direct/indexed, no UAVs, 5-frame target history\n");
    return hooks;
}
} // namespace yaagl::pso::async_pipeline

extern "C" bool yaagl_d3dmetal_async_stats(
    yaagl::pso::async_pipeline::Statistics* output, std::uint32_t size) noexcept {
    using namespace yaagl::pso::async_pipeline;
    if (!output || size != sizeof(Statistics)) return false;
    auto& rt = runtime();
    *output = {1, sizeof(Statistics), rt.queued.load(), rt.ready.load(), rt.failed.load(),
               rt.skipped.load(), rt.synchronous.load()};
    return true;
}
