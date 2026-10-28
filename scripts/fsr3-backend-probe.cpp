// Direct regression probe for the pinned source DX12 backend, not a replacement backend.
// Compile the production implementation here to observe its actual private cursors,
// job slots and upload identities without exporting test hooks or copying layouts.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_6.h>
#include <d3dcompiler.h>
#include <array>
#include <atomic>
#include <bit>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cwchar>
#include <limits>
#include <map>
#include <mutex>
#include <new>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>
#include "../include/yaagl_fsr3_lifetime.h"
#include "../d3dmetal-sidecar/third-party/fidelityfx/Kits/FidelityFX/framegeneration/fsr3/internal/ffx_opticalflow_shaderblobs.h"
HRESULT WINAPI backendProbeCreateDXGIFactory2(UINT flags, REFIID iid, void** output);
FARPROC WINAPI backendProbeGetProcAddress(HMODULE module, LPCSTR name);
#define CreateDXGIFactory2 backendProbeCreateDXGIFactory2
#define GetProcAddress backendProbeGetProcAddress
#include "../d3dmetal-sidecar/third-party/fidelityfx/Kits/FidelityFX/backend/dx12/ffx_dx12.cpp"
#undef GetProcAddress
#undef CreateDXGIFactory2

namespace backend_probe {
static const char* scenario = "startup";
[[noreturn]] static void fail(const char* assertion) {
    std::fprintf(stderr, "FAIL %s assertion=%s\n", scenario, assertion);
    std::fflush(nullptr);
    std::exit(1);
}
static void need(bool condition, const char* assertion) { if (!condition) fail(assertion); }
static void hr(HRESULT result, const char* assertion) {
    if (FAILED(result)) { std::fprintf(stderr, "HRESULT=0x%08lx\n", static_cast<unsigned long>(result)); fail(assertion); }
}
static void ok(FfxErrorCode result, const char* assertion) {
    if (result != FFX_OK) { std::fprintf(stderr, "FfxErrorCode=0x%08x\n", static_cast<unsigned>(result)); fail(assertion); }
}
enum class Stage : size_t { Descriptor, Root, Command, Pipeline, Resource, Heap, UploadMap, Name, Options, Factory, Serializer, Count };
struct Plan {
    std::array<uint64_t, static_cast<size_t>(Stage::Count)> calls{}, successes{};
    Stage fault = Stage::Count;
    uint64_t ordinal{}, hits{}, unmaps{};
    bool tierOne{};
    void arm(Stage stage, uint64_t at = 1) { *this = {}; fault = stage; ordinal = at; }
} plan;
static bool inject(Stage stage) {
    const auto n = ++plan.calls[static_cast<size_t>(stage)];
    if (plan.fault == stage && n == plan.ordinal) { ++plan.hits; return true; }
    return false;
}
static void succeeded(Stage stage) { ++plan.successes[static_cast<size_t>(stage)]; }
// Each wrapper adopts precisely one successful real COM output. Every visible
// AddRef/Release is mirrored on that same real interface. Identity is canonical
// for the complete wrapped interface hierarchy. Unsupported QIs use the real
// result; a successful extension without a wrapper stops the diagnostic rather
// than returning an interface with a different canonical IUnknown.
struct ComRecord { uint64_t id; std::string kind; void* real; ULONG refs; uint64_t adds{}, releases{}; };
struct ComLedger {
    std::mutex mutex;
    uint64_t next{};
    std::map<void*, ComRecord> live;
    std::vector<ComRecord> retired;
    std::atomic<unsigned> escaped{};
    void born(void* wrapper, void* real, const char* kind) {
        std::lock_guard lock(mutex);
        need(live.emplace(wrapper, ComRecord{++next, kind, real, 1}).second, "unique COM creator");
    }
    ULONG add(void* wrapper) {
        std::lock_guard lock(mutex);
        auto& record = live.at(wrapper); ++record.adds; return ++record.refs;
    }
    ULONG release(void* wrapper) {
        std::lock_guard lock(mutex);
        auto& record = live.at(wrapper);
        need(record.refs != 0, "COM no over-release");
        ++record.releases;
        ULONG refs = --record.refs;
        if (!refs) {
            need(record.releases == record.adds + 1, "COM creator/AddRef/Release exact balance");
            retired.push_back(record);
            live.erase(wrapper);
        }
        return refs;
    }
    template<class T> T* unwrap(T* value) {
        if (!value) return nullptr;
        std::lock_guard lock(mutex);
        auto found = live.find(const_cast<void*>(static_cast<const void*>(value)));
        return found == live.end() ? value : static_cast<T*>(found->second.real);
    }
    std::map<uint64_t, ULONG> snapshot() {
        std::lock_guard lock(mutex);
        std::map<uint64_t, ULONG> result;
        for (const auto& [pointer, record] : live) { (void)pointer; result.emplace(record.id, record.refs); }
        return result;
    }
    void baseline(const std::map<uint64_t, ULONG>& expected) {
        need(!escaped.load(), "no uninstrumented successful extension QI");
        if (snapshot() != expected) {
            std::lock_guard lock(mutex);
            for (const auto& [pointer, record] : live)
                std::fprintf(stderr, "LIVE_COM id=%llu kind=%s wrapper=%p real=%p refs=%lu add=%llu release=%llu\n",
                    static_cast<unsigned long long>(record.id), record.kind.c_str(), pointer, record.real, record.refs,
                    static_cast<unsigned long long>(record.adds), static_cast<unsigned long long>(record.releases));
            fail("COM exact owner/reference baseline");
        }
    }
} com;
static ID3D12Device* proxyDevice{};
static ID3D12Device* realDevice{};

template<class T> class Object : public T {
protected:
    T* real;
    std::mutex references;
    ULONG refs = 1;
public:
    Object(T* value, const char* kind) : real(value) { com.born(this, value, kind); }
    virtual ~Object() = default;
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** output) override {
        if (!output) return E_POINTER;
        *output = nullptr;
        void* accepted{};
        HRESULT result = real->QueryInterface(iid, &accepted);
        if (FAILED(result)) return result;
        const bool supported = iid == __uuidof(IUnknown) || iid == __uuidof(ID3D12Object) || iid == __uuidof(T)
            || (std::is_base_of_v<ID3D12DeviceChild, T> && iid == __uuidof(ID3D12DeviceChild))
            || (std::is_base_of_v<ID3D12Pageable, T> && iid == __uuidof(ID3D12Pageable));
        if (!supported) {
            ++com.escaped;
            std::fprintf(stderr, "UNWRAPPED_QI iid=%08lx\n", static_cast<unsigned long>(iid.Data1));
            static_cast<IUnknown*>(accepted)->Release();
            // Do not return an extension with a different canonical IUnknown,
            // or lie about real support by returning E_NOINTERFACE.
            fail("successful extension QI requires a corresponding transparent proxy");
        }
        static_cast<IUnknown*>(accepted)->Release();
        AddRef();
        *output = static_cast<T*>(this);
        return result;
    }
    ULONG STDMETHODCALLTYPE AddRef() override {
        std::lock_guard lock(references);
        real->AddRef();
        ++refs;
        return com.add(this);
    }
    ULONG STDMETHODCALLTYPE Release() override {
        ULONG remaining;
        {
            std::lock_guard lock(references);
            need(refs != 0, "COM valid Release");
            real->Release();
            remaining = --refs;
            need(com.release(this) == remaining, "COM reference mirror");
        }
        if (!remaining) delete this;
        return remaining;
    }
    HRESULT STDMETHODCALLTYPE GetPrivateData(REFGUID key, UINT* size, void* value) override { return real->GetPrivateData(key, size, value); }
    HRESULT STDMETHODCALLTYPE SetPrivateData(REFGUID key, UINT size, const void* value) override { return real->SetPrivateData(key, size, value); }
    HRESULT STDMETHODCALLTYPE SetPrivateDataInterface(REFGUID key, const IUnknown* value) override {
        return real->SetPrivateDataInterface(key, com.unwrap(value));
    }
    HRESULT STDMETHODCALLTYPE SetName(LPCWSTR name) override { if (inject(Stage::Name)) return E_FAIL; return real->SetName(name); }
};
template<class T> class Child : public Object<T> {
public:
    Child(T* value, const char* kind) : Object<T>(value, kind) {}
    HRESULT STDMETHODCALLTYPE GetDevice(REFIID iid, void** output) override {
        if (!output) return E_POINTER;
        *output = nullptr;
        ID3D12Device* actual{};
        HRESULT result = this->real->GetDevice(__uuidof(ID3D12Device), reinterpret_cast<void**>(&actual));
        if (FAILED(result)) return result;
        ID3D12Device* a{}; ID3D12Device* b{};
        hr(actual->QueryInterface(IID_PPV_ARGS(&a)), "real child device identity");
        hr(realDevice->QueryInterface(IID_PPV_ARGS(&b)), "real device identity");
        need(a == b, "resource belongs to selected real device");
        a->Release(); b->Release(); actual->Release();
        return proxyDevice->QueryInterface(iid, output);
    }
};
#ifdef WIDL_EXPLICIT_AGGREGATE_RETURNS
#define AGGREGATE0(type, name) type* STDMETHODCALLTYPE name(type* output) override { *output = this->real->name(); return output; }
#else
#define AGGREGATE0(type, name) type STDMETHODCALLTYPE name() override { return this->real->name(); }
#endif
class Resource final : public Child<ID3D12Resource> {
    bool upload;
public:
    Resource(ID3D12Resource* value, bool isUpload) : Child(value, "resource"), upload(isUpload) {}
    HRESULT STDMETHODCALLTYPE Map(UINT subresource, const D3D12_RANGE* range, void** output) override {
        if (upload && inject(Stage::UploadMap)) { if (output) *output = reinterpret_cast<void*>(uintptr_t{1}); return E_OUTOFMEMORY; }
        HRESULT result = real->Map(subresource, range, output);
        if (upload && SUCCEEDED(result)) succeeded(Stage::UploadMap);
        return result;
    }
    void STDMETHODCALLTYPE Unmap(UINT subresource, const D3D12_RANGE* range) override { if (upload) ++plan.unmaps; real->Unmap(subresource, range); }
    AGGREGATE0(D3D12_RESOURCE_DESC, GetDesc)
    D3D12_GPU_VIRTUAL_ADDRESS STDMETHODCALLTYPE GetGPUVirtualAddress() override { return real->GetGPUVirtualAddress(); }
    HRESULT STDMETHODCALLTYPE WriteToSubresource(UINT subresource, const D3D12_BOX* box, const void* source, UINT row, UINT slice) override {
        return real->WriteToSubresource(subresource, box, source, row, slice);
    }
    HRESULT STDMETHODCALLTYPE ReadFromSubresource(void* target, UINT row, UINT slice, UINT subresource, const D3D12_BOX* box) override {
        return real->ReadFromSubresource(target, row, slice, subresource, box);
    }
    HRESULT STDMETHODCALLTYPE GetHeapProperties(D3D12_HEAP_PROPERTIES* properties, D3D12_HEAP_FLAGS* flags) override {
        return real->GetHeapProperties(properties, flags);
    }
};
class Heap final : public Child<ID3D12Heap> {
public:
    explicit Heap(ID3D12Heap* value) : Child(value, "heap") {}
    AGGREGATE0(D3D12_HEAP_DESC, GetDesc)
};
class Descriptor final : public Child<ID3D12DescriptorHeap> {
public:
    explicit Descriptor(ID3D12DescriptorHeap* value) : Child(value, "descriptor") {}
    AGGREGATE0(D3D12_DESCRIPTOR_HEAP_DESC, GetDesc)
    AGGREGATE0(D3D12_CPU_DESCRIPTOR_HANDLE, GetCPUDescriptorHandleForHeapStart)
    AGGREGATE0(D3D12_GPU_DESCRIPTOR_HANDLE, GetGPUDescriptorHandleForHeapStart)
};
class Root final : public Child<ID3D12RootSignature> {
public: explicit Root(ID3D12RootSignature* value) : Child(value, "root") {}
};
class Command final : public Child<ID3D12CommandSignature> {
public: explicit Command(ID3D12CommandSignature* value) : Child(value, "command") {}
};
class Pipeline final : public Child<ID3D12PipelineState> {
public:
    explicit Pipeline(ID3D12PipelineState* value) : Child(value, "pso") {}
    HRESULT STDMETHODCALLTYPE GetCachedBlob(ID3DBlob** blob) override { return real->GetCachedBlob(blob); }
};
#undef AGGREGATE0

template<class T, class Wrapper> static HRESULT publish(HRESULT result, T* value, REFIID iid, void** output) {
    if (FAILED(result)) return result; // Failed outputs are not transferred to us.
    need(value != nullptr, "successful COM creation output");
    auto* wrapper = new Wrapper(value);
    HRESULT queried = wrapper->QueryInterface(iid, output);
    wrapper->Release();
    return queried;
}
static HRESULT publishResource(HRESULT result, ID3D12Resource* value, bool upload, REFIID iid, void** output) {
    if (FAILED(result)) return result;
    need(value != nullptr, "successful resource creation output");
    auto* wrapper = new Resource(value, upload);
    HRESULT queried = wrapper->QueryInterface(iid, output);
    wrapper->Release();
    return queried;
}
class Device final : public Object<ID3D12Device> {
public:
    explicit Device(ID3D12Device* value) : Object(value, "device") {}
    UINT STDMETHODCALLTYPE GetNodeCount() override { return real->GetNodeCount(); }
    HRESULT STDMETHODCALLTYPE CreateCommandQueue(const D3D12_COMMAND_QUEUE_DESC* desc, REFIID iid, void** output) override { return real->CreateCommandQueue(desc, iid, output); }
    HRESULT STDMETHODCALLTYPE CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE type, REFIID iid, void** output) override { return real->CreateCommandAllocator(type, iid, output); }
    HRESULT STDMETHODCALLTYPE CreateGraphicsPipelineState(const D3D12_GRAPHICS_PIPELINE_STATE_DESC* desc, REFIID iid, void** output) override {
        if (output) *output = nullptr;
        if (inject(Stage::Pipeline)) return E_OUTOFMEMORY;
        auto copy = *desc; copy.pRootSignature = com.unwrap(copy.pRootSignature);
        ID3D12PipelineState* value{};
        HRESULT result = real->CreateGraphicsPipelineState(&copy, IID_PPV_ARGS(&value));
        if (SUCCEEDED(result)) succeeded(Stage::Pipeline);
        return publish<ID3D12PipelineState, Pipeline>(result, value, iid, output);
    }
    HRESULT STDMETHODCALLTYPE CreateComputePipelineState(const D3D12_COMPUTE_PIPELINE_STATE_DESC* desc, REFIID iid, void** output) override {
        if (output) *output = nullptr;
        if (inject(Stage::Pipeline)) return E_OUTOFMEMORY;
        auto copy = *desc; copy.pRootSignature = com.unwrap(copy.pRootSignature);
        ID3D12PipelineState* value{};
        HRESULT result = real->CreateComputePipelineState(&copy, IID_PPV_ARGS(&value));
        if (SUCCEEDED(result)) succeeded(Stage::Pipeline);
        return publish<ID3D12PipelineState, Pipeline>(result, value, iid, output);
    }
    HRESULT STDMETHODCALLTYPE CreateCommandList(UINT node, D3D12_COMMAND_LIST_TYPE type, ID3D12CommandAllocator* allocator, ID3D12PipelineState* initial, REFIID iid, void** output) override {
        return real->CreateCommandList(node, type, com.unwrap(allocator), com.unwrap(initial), iid, output);
    }
    HRESULT STDMETHODCALLTYPE CheckFeatureSupport(D3D12_FEATURE feature, void* value, UINT size) override {
        if (feature == D3D12_FEATURE_D3D12_OPTIONS && inject(Stage::Options)) return E_FAIL;
        HRESULT result = real->CheckFeatureSupport(feature, value, size);
        if (SUCCEEDED(result) && feature == D3D12_FEATURE_D3D12_OPTIONS && plan.tierOne)
            static_cast<D3D12_FEATURE_DATA_D3D12_OPTIONS*>(value)->ResourceHeapTier = D3D12_RESOURCE_HEAP_TIER_1;
        return result;
    }
    HRESULT STDMETHODCALLTYPE CreateDescriptorHeap(const D3D12_DESCRIPTOR_HEAP_DESC* desc, REFIID iid, void** output) override {
        if (output) *output = nullptr;
        if (inject(Stage::Descriptor)) return E_OUTOFMEMORY;
        ID3D12DescriptorHeap* value{};
        HRESULT result = real->CreateDescriptorHeap(desc, IID_PPV_ARGS(&value));
        if (SUCCEEDED(result)) succeeded(Stage::Descriptor);
        return publish<ID3D12DescriptorHeap, Descriptor>(result, value, iid, output);
    }
    UINT STDMETHODCALLTYPE GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE type) override { return real->GetDescriptorHandleIncrementSize(type); }
    HRESULT STDMETHODCALLTYPE CreateRootSignature(UINT node, const void* bytes, SIZE_T size, REFIID iid, void** output) override {
        if (output) *output = nullptr;
        if (inject(Stage::Root)) return E_OUTOFMEMORY;
        ID3D12RootSignature* value{};
        HRESULT result = real->CreateRootSignature(node, bytes, size, IID_PPV_ARGS(&value));
        if (SUCCEEDED(result)) succeeded(Stage::Root);
        return publish<ID3D12RootSignature, Root>(result, value, iid, output);
    }
    void STDMETHODCALLTYPE CreateConstantBufferView(const D3D12_CONSTANT_BUFFER_VIEW_DESC* desc, D3D12_CPU_DESCRIPTOR_HANDLE handle) override { real->CreateConstantBufferView(desc, handle); }
    void STDMETHODCALLTYPE CreateShaderResourceView(ID3D12Resource* resource, const D3D12_SHADER_RESOURCE_VIEW_DESC* desc, D3D12_CPU_DESCRIPTOR_HANDLE handle) override { real->CreateShaderResourceView(com.unwrap(resource), desc, handle); }
    void STDMETHODCALLTYPE CreateUnorderedAccessView(ID3D12Resource* resource, ID3D12Resource* counter, const D3D12_UNORDERED_ACCESS_VIEW_DESC* desc, D3D12_CPU_DESCRIPTOR_HANDLE handle) override { real->CreateUnorderedAccessView(com.unwrap(resource), com.unwrap(counter), desc, handle); }
    void STDMETHODCALLTYPE CreateRenderTargetView(ID3D12Resource* resource, const D3D12_RENDER_TARGET_VIEW_DESC* desc, D3D12_CPU_DESCRIPTOR_HANDLE handle) override { real->CreateRenderTargetView(com.unwrap(resource), desc, handle); }
    void STDMETHODCALLTYPE CreateDepthStencilView(ID3D12Resource* resource, const D3D12_DEPTH_STENCIL_VIEW_DESC* desc, D3D12_CPU_DESCRIPTOR_HANDLE handle) override { real->CreateDepthStencilView(com.unwrap(resource), desc, handle); }
    void STDMETHODCALLTYPE CreateSampler(const D3D12_SAMPLER_DESC* desc, D3D12_CPU_DESCRIPTOR_HANDLE handle) override { real->CreateSampler(desc, handle); }
    void STDMETHODCALLTYPE CopyDescriptors(UINT dn, const D3D12_CPU_DESCRIPTOR_HANDLE* dh, const UINT* ds, UINT sn, const D3D12_CPU_DESCRIPTOR_HANDLE* sh, const UINT* ss, D3D12_DESCRIPTOR_HEAP_TYPE type) override { real->CopyDescriptors(dn, dh, ds, sn, sh, ss, type); }
    void STDMETHODCALLTYPE CopyDescriptorsSimple(UINT count, D3D12_CPU_DESCRIPTOR_HANDLE dst, D3D12_CPU_DESCRIPTOR_HANDLE src, D3D12_DESCRIPTOR_HEAP_TYPE type) override { real->CopyDescriptorsSimple(count, dst, src, type); }
#ifdef WIDL_EXPLICIT_AGGREGATE_RETURNS
    D3D12_RESOURCE_ALLOCATION_INFO* STDMETHODCALLTYPE GetResourceAllocationInfo(D3D12_RESOURCE_ALLOCATION_INFO* out, UINT mask, UINT count, const D3D12_RESOURCE_DESC* desc) override { *out = real->GetResourceAllocationInfo(mask, count, desc); return out; }
    D3D12_HEAP_PROPERTIES* STDMETHODCALLTYPE GetCustomHeapProperties(D3D12_HEAP_PROPERTIES* out, UINT node, D3D12_HEAP_TYPE type) override { *out = real->GetCustomHeapProperties(node, type); return out; }
    LUID* STDMETHODCALLTYPE GetAdapterLuid(LUID* out) override { *out = real->GetAdapterLuid(); return out; }
#else
    D3D12_RESOURCE_ALLOCATION_INFO STDMETHODCALLTYPE GetResourceAllocationInfo(UINT mask, UINT count, const D3D12_RESOURCE_DESC* desc) override { return real->GetResourceAllocationInfo(mask, count, desc); }
    D3D12_HEAP_PROPERTIES STDMETHODCALLTYPE GetCustomHeapProperties(UINT node, D3D12_HEAP_TYPE type) override { return real->GetCustomHeapProperties(node, type); }
    LUID STDMETHODCALLTYPE GetAdapterLuid() override { return real->GetAdapterLuid(); }
#endif
    HRESULT STDMETHODCALLTYPE CreateCommittedResource(const D3D12_HEAP_PROPERTIES* heap, D3D12_HEAP_FLAGS flags, const D3D12_RESOURCE_DESC* desc, D3D12_RESOURCE_STATES state, const D3D12_CLEAR_VALUE* clear, REFIID iid, void** output) override {
        if (output) *output = nullptr;
        if (inject(Stage::Resource)) return E_OUTOFMEMORY;
        ID3D12Resource* value{};
        HRESULT result = real->CreateCommittedResource(heap, flags, desc, state, clear, IID_PPV_ARGS(&value));
        if (SUCCEEDED(result)) succeeded(Stage::Resource);
        return publishResource(result, value, heap->Type == D3D12_HEAP_TYPE_UPLOAD, iid, output);
    }
    HRESULT STDMETHODCALLTYPE CreateHeap(const D3D12_HEAP_DESC* desc, REFIID iid, void** output) override {
        if (output) *output = nullptr;
        if (inject(Stage::Heap)) return E_OUTOFMEMORY;
        ID3D12Heap* value{};
        HRESULT result = real->CreateHeap(desc, IID_PPV_ARGS(&value));
        if (SUCCEEDED(result)) succeeded(Stage::Heap);
        return publish<ID3D12Heap, Heap>(result, value, iid, output);
    }
    HRESULT STDMETHODCALLTYPE CreatePlacedResource(ID3D12Heap* heap, UINT64 offset, const D3D12_RESOURCE_DESC* desc, D3D12_RESOURCE_STATES state, const D3D12_CLEAR_VALUE* clear, REFIID iid, void** output) override {
        if (output) *output = nullptr;
        if (inject(Stage::Resource)) return E_OUTOFMEMORY;
        auto* actual = com.unwrap(heap);
        ID3D12Resource* value{};
        HRESULT result = real->CreatePlacedResource(actual, offset, desc, state, clear, IID_PPV_ARGS(&value));
        if (SUCCEEDED(result)) succeeded(Stage::Resource);
        return publishResource(result, value, actual->GetDesc().Properties.Type == D3D12_HEAP_TYPE_UPLOAD, iid, output);
    }
    HRESULT STDMETHODCALLTYPE CreateReservedResource(const D3D12_RESOURCE_DESC* desc, D3D12_RESOURCE_STATES state, const D3D12_CLEAR_VALUE* clear, REFIID iid, void** output) override {
        if (output) *output = nullptr;
        if (inject(Stage::Resource)) return E_OUTOFMEMORY;
        ID3D12Resource* value{};
        HRESULT result = real->CreateReservedResource(desc, state, clear, IID_PPV_ARGS(&value));
        if (SUCCEEDED(result)) succeeded(Stage::Resource);
        return publishResource(result, value, false, iid, output);
    }
    HRESULT STDMETHODCALLTYPE CreateSharedHandle(ID3D12DeviceChild* object, const SECURITY_ATTRIBUTES* attributes, DWORD access, LPCWSTR name, HANDLE* output) override { return real->CreateSharedHandle(com.unwrap(object), attributes, access, name, output); }
    HRESULT STDMETHODCALLTYPE OpenSharedHandle(HANDLE handle, REFIID iid, void** output) override { return real->OpenSharedHandle(handle, iid, output); }
    HRESULT STDMETHODCALLTYPE OpenSharedHandleByName(LPCWSTR name, DWORD access, HANDLE* output) override { return real->OpenSharedHandleByName(name, access, output); }
    HRESULT STDMETHODCALLTYPE MakeResident(UINT count, ID3D12Pageable* const* values) override {
        std::vector<ID3D12Pageable*> actual(count);
        for (UINT i = 0; i < count; ++i) actual[i] = com.unwrap(values[i]);
        return real->MakeResident(count, actual.data());
    }
    HRESULT STDMETHODCALLTYPE Evict(UINT count, ID3D12Pageable* const* values) override {
        std::vector<ID3D12Pageable*> actual(count);
        for (UINT i = 0; i < count; ++i) actual[i] = com.unwrap(values[i]);
        return real->Evict(count, actual.data());
    }
    HRESULT STDMETHODCALLTYPE CreateFence(UINT64 value, D3D12_FENCE_FLAGS flags, REFIID iid, void** output) override { return real->CreateFence(value, flags, iid, output); }
    HRESULT STDMETHODCALLTYPE GetDeviceRemovedReason() override { return real->GetDeviceRemovedReason(); }
    void STDMETHODCALLTYPE GetCopyableFootprints(const D3D12_RESOURCE_DESC* desc, UINT first, UINT count, UINT64 offset, D3D12_PLACED_SUBRESOURCE_FOOTPRINT* layouts, UINT* rows, UINT64* rowSize, UINT64* total) override { real->GetCopyableFootprints(desc, first, count, offset, layouts, rows, rowSize, total); }
    HRESULT STDMETHODCALLTYPE CreateQueryHeap(const D3D12_QUERY_HEAP_DESC* desc, REFIID iid, void** output) override { return real->CreateQueryHeap(desc, iid, output); }
    HRESULT STDMETHODCALLTYPE SetStablePowerState(BOOL enabled) override { return real->SetStablePowerState(enabled); }
    HRESULT STDMETHODCALLTYPE CreateCommandSignature(const D3D12_COMMAND_SIGNATURE_DESC* desc, ID3D12RootSignature* root, REFIID iid, void** output) override {
        if (output) *output = nullptr;
        if (inject(Stage::Command)) return E_OUTOFMEMORY;
        ID3D12CommandSignature* value{};
        HRESULT result = real->CreateCommandSignature(desc, com.unwrap(root), IID_PPV_ARGS(&value));
        if (SUCCEEDED(result)) succeeded(Stage::Command);
        return publish<ID3D12CommandSignature, Command>(result, value, iid, output);
    }
    void STDMETHODCALLTYPE GetResourceTiling(ID3D12Resource* resource, UINT* total, D3D12_PACKED_MIP_INFO* packed, D3D12_TILE_SHAPE* shape, UINT* count, UINT first, D3D12_SUBRESOURCE_TILING* tilings) override { real->GetResourceTiling(com.unwrap(resource), total, packed, shape, count, first, tilings); }
};

// Only the test TU redirects these two imported calls. Every successful object
// remains a real DXGI factory or D3DCompiler blob, with a visible reference ledger.
class Factory final : public IDXGIFactory {
    IDXGIFactory* real;
    ULONG refs = 1;
public:
    explicit Factory(IDXGIFactory* value) : real(value) { com.born(this, value, "factory"); }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** out) override {
        if (!out) return E_POINTER;
        *out = nullptr;
        if (iid != __uuidof(IUnknown) && iid != __uuidof(IDXGIObject) && iid != __uuidof(IDXGIFactory))
            fail("factory extension QI needs a transparent proxy");
        AddRef(); *out = this; return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { real->AddRef(); ++refs; return com.add(this); }
    ULONG STDMETHODCALLTYPE Release() override {
        real->Release(); const ULONG left = --refs;
        need(com.release(this) == left, "factory exact reference mirror");
        if (!left) delete this;
        return left;
    }
    HRESULT STDMETHODCALLTYPE SetPrivateData(REFGUID key, UINT size, const void* value) override { return real->SetPrivateData(key, size, value); }
    HRESULT STDMETHODCALLTYPE SetPrivateDataInterface(REFGUID key, const IUnknown* value) override { return real->SetPrivateDataInterface(key, com.unwrap(value)); }
    HRESULT STDMETHODCALLTYPE GetPrivateData(REFGUID key, UINT* size, void* value) override { return real->GetPrivateData(key, size, value); }
    HRESULT STDMETHODCALLTYPE GetParent(REFIID iid, void** out) override { return real->GetParent(iid, out); }
    HRESULT STDMETHODCALLTYPE EnumAdapters(UINT n, IDXGIAdapter** out) override { return real->EnumAdapters(n, out); }
    HRESULT STDMETHODCALLTYPE MakeWindowAssociation(HWND window, UINT flags) override { return real->MakeWindowAssociation(window, flags); }
    HRESULT STDMETHODCALLTYPE GetWindowAssociation(HWND* out) override { return real->GetWindowAssociation(out); }
    HRESULT STDMETHODCALLTYPE CreateSwapChain(IUnknown* device, DXGI_SWAP_CHAIN_DESC* desc, IDXGISwapChain** out) override { return real->CreateSwapChain(com.unwrap(device), desc, out); }
    HRESULT STDMETHODCALLTYPE CreateSoftwareAdapter(HMODULE module, IDXGIAdapter** out) override { return real->CreateSoftwareAdapter(module, out); }
};
class Blob final : public ID3DBlob {
    ID3DBlob* real;
    ULONG refs = 1;
public:
    explicit Blob(ID3DBlob* value) : real(value) { com.born(this, value, "blob"); }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** out) override {
        if (!out) return E_POINTER;
        *out = nullptr;
        if (iid != __uuidof(IUnknown) && iid != __uuidof(ID3DBlob)) return E_NOINTERFACE;
        AddRef(); *out = this; return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { real->AddRef(); ++refs; return com.add(this); }
    ULONG STDMETHODCALLTYPE Release() override {
        real->Release(); const ULONG left = --refs;
        need(com.release(this) == left, "blob exact reference mirror");
        if (!left) delete this;
        return left;
    }
    LPVOID STDMETHODCALLTYPE GetBufferPointer() override { return real->GetBufferPointer(); }
    SIZE_T STDMETHODCALLTYPE GetBufferSize() override { return real->GetBufferSize(); }
};
static HRESULT factory(UINT flags, REFIID iid, void** out) {
    if (!out) return E_POINTER;
    *out = nullptr;
    if (inject(Stage::Factory)) return E_OUTOFMEMORY;
    need(iid == __uuidof(IDXGIFactory), "backend requests actual IDXGIFactory ABI");
    IDXGIFactory* actual{};
    const HRESULT result = CreateDXGIFactory2(flags, IID_PPV_ARGS(&actual));
    if (FAILED(result)) return result;
    *out = new Factory(actual);
    succeeded(Stage::Factory);
    return result;
}
using Serializer = HRESULT (WINAPI*)(const D3D12_ROOT_SIGNATURE_DESC*, D3D_ROOT_SIGNATURE_VERSION, ID3DBlob**, ID3DBlob**);
static HRESULT WINAPI serialize(const D3D12_ROOT_SIGNATURE_DESC* desc, D3D_ROOT_SIGNATURE_VERSION version, ID3DBlob** output, ID3DBlob** errors) {
    auto actual = reinterpret_cast<Serializer>(GetProcAddress(GetModuleHandleW(L"d3d12.dll"), "D3D12SerializeRootSignature"));
    need(actual != nullptr, "real D3D12 serializer exists");
    if (!inject(Stage::Serializer)) return actual(desc, version, output, errors);
    need(output && errors, "serializer receives both ownership outputs");
    ID3DBlob* bytes{}; ID3DBlob* message{};
    hr(actual(desc, version, &bytes, &message), "real serialization before injected failure");
    need(bytes != nullptr, "real serializer produces a real output blob");
    if (message) message->Release();
    hr(D3DCreateBlob(32, &message), "real error blob allocation");
    std::memset(message->GetBufferPointer(), 0, message->GetBufferSize());
    *output = new Blob(bytes); *errors = new Blob(message);
    return E_FAIL;
}
static FARPROC symbol(HMODULE module, LPCSTR name) {
    if (reinterpret_cast<uintptr_t>(name) > 0xffff && !std::strcmp(name, "D3D12SerializeRootSignature"))
        return std::bit_cast<FARPROC>(&serialize);
    return GetProcAddress(module, name);
}

enum class AllocationFault { None, Error, Throw, NullSuccess, Untransferred };
struct Policy {
    uint64_t resourceCalls{}, heapCalls{}, resourceFrees{}, heapFrees{}, freeAttempts{};
    uint64_t failResourceAt{}, failHeapAt{};
    AllocationFault resourceFault{}, heapFault{};
    bool failResourceFree{}, failHeapFree{}, throwHeapFree{}, reenter{};
    uint64_t heapRequested{};
};
static std::array<Policy, 2> policies;
struct Creator { unsigned pair; uint32_t effect; bool heap; uint64_t offset{}, size{}; bool aliasable{}; };
static std::map<void*, Creator> owners;
static std::vector<IUnknown*> untransferred;
static void registerPair(unsigned pair);
template<unsigned Pair> static ffxReturnCode_t resourceAlloc(uint32_t effect, D3D12_RESOURCE_STATES state,
    const D3D12_HEAP_PROPERTIES* heap, const D3D12_RESOURCE_DESC* desc, const FfxApiResourceDescription* ffx,
    const D3D12_CLEAR_VALUE* clear, ID3D12Resource** output) {
    (void)ffx;
    auto& policy = policies[Pair];
    *output = nullptr;
    const auto call = ++policy.resourceCalls;
    if (std::exchange(policy.reenter, false)) registerPair(1);
    const AllocationFault fault = call == policy.failResourceAt ? policy.resourceFault : AllocationFault::None;
    if (fault == AllocationFault::Throw) throw std::bad_alloc{};
    if (fault == AllocationFault::Error) return FFX_API_RETURN_ERROR_MEMORY;
    if (fault == AllocationFault::NullSuccess) return FFX_API_RETURN_OK;
    ID3D12Resource* resource{};
    const HRESULT result = proxyDevice->CreateCommittedResource(heap, D3D12_HEAP_FLAG_NONE, desc, state, clear, IID_PPV_ARGS(&resource));
    if (FAILED(result)) return FFX_API_RETURN_ERROR_MEMORY;
    *output = resource;
    if (fault == AllocationFault::Untransferred) { untransferred.push_back(resource); return FFX_API_RETURN_ERROR_MEMORY; }
    need(owners.emplace(resource, Creator{Pair, effect, false}).second, "unique accepted custom resource creator");
    return FFX_API_RETURN_OK;
}
template<unsigned Pair> static ffxReturnCode_t resourceFree(uint32_t effect, ID3D12Resource* resource) {
    auto& policy = policies[Pair];
    ++policy.freeAttempts;
    const auto found = owners.find(resource);
    need(found != owners.end() && !found->second.heap && found->second.pair == Pair && found->second.effect == effect,
         "resource release uses exact accepted creator and effect");
    if (std::exchange(policy.failResourceFree, false)) return FFX_API_RETURN_ERROR_MEMORY;
    owners.erase(found); ++policy.resourceFrees; resource->Release();
    return FFX_API_RETURN_OK;
}
template<unsigned Pair> static ffxReturnCode_t heapAlloc(uint32_t effect, const D3D12_HEAP_DESC* desc, bool aliasable,
    ID3D12Heap** output, uint64_t* offset) {
    auto& policy = policies[Pair];
    *output = nullptr; *offset = 0;
    const auto call = ++policy.heapCalls;
    if (std::exchange(policy.reenter, false)) registerPair(1);
    const AllocationFault fault = call == policy.failHeapAt ? policy.heapFault : AllocationFault::None;
    if (fault == AllocationFault::Throw) throw std::bad_alloc{};
    if (fault == AllocationFault::Error) return FFX_API_RETURN_ERROR_MEMORY;
    if (fault == AllocationFault::NullSuccess) return FFX_API_RETURN_OK;
    auto actualDesc = *desc;
    constexpr uint64_t start = D3D12_DEFAULT_RESOURCE_PLACEMENT_ALIGNMENT;
    actualDesc.SizeInBytes += start;
    ID3D12Heap* heap{};
    const HRESULT result = proxyDevice->CreateHeap(&actualDesc, IID_PPV_ARGS(&heap));
    if (FAILED(result)) return FFX_API_RETURN_ERROR_MEMORY;
    *output = heap; *offset = start;
    if (fault == AllocationFault::Untransferred) { untransferred.push_back(heap); return FFX_API_RETURN_ERROR_MEMORY; }
    need(owners.emplace(heap, Creator{Pair, effect, true, start, policy.heapRequested, aliasable}).second,
         "unique accepted custom heap creator");
    return FFX_API_RETURN_OK;
}
template<unsigned Pair> static ffxReturnCode_t heapFree(uint32_t effect, ID3D12Heap* heap, uint64_t offset, uint64_t size) {
    auto& policy = policies[Pair];
    ++policy.freeAttempts;
    const auto found = owners.find(heap);
    need(found != owners.end() && found->second.heap && found->second.pair == Pair && found->second.effect == effect
         && found->second.offset == offset && found->second.size == size,
         "heap release uses exact creator effect pointer start offset and logical size");
    if (std::exchange(policy.throwHeapFree, false)) throw std::bad_alloc{};
    if (std::exchange(policy.failHeapFree, false)) return FFX_API_RETURN_ERROR_MEMORY;
    owners.erase(found); ++policy.heapFrees; heap->Release();
    return FFX_API_RETURN_OK;
}
static BackendAllocationCallbacks callbacks(unsigned pair) {
    if (pair == 0) return {resourceAlloc<0>, resourceFree<0>, heapAlloc<0>, heapFree<0>, nullptr};
    return {resourceAlloc<1>, resourceFree<1>, heapAlloc<1>, heapFree<1>, nullptr};
}
static void registerPair(unsigned pair) {
    const auto value = callbacks(pair);
    ffxCreateContextDescHeader root{};
    ffxCreateBackendDX12AllocationCallbacksDesc registration{};
    registration.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_BACKEND_DX12_ALLOCATION_CALLBACKS;
    registration.pfnFfxResourceAllocator = value.resourceAllocator;
    registration.pfnFfxResourceDeallocator = value.resourceDeallocator;
    registration.pfnFfxHeapAllocator = value.heapAllocator;
    registration.pfnFfxHeapDeallocator = value.heapDeallocator;
    root.pNext = &registration.header;
    const auto captured = GetBackendAllocationCallbacks(&root);
    need(captured.resourceAllocator == value.resourceAllocator && captured.resourceDeallocator == value.resourceDeallocator
         && captured.heapAllocator == value.heapAllocator && captured.heapDeallocator == value.heapDeallocator,
         "replacement callback registration captures one coherent creator set");
}
static void releaseUntransferred() {
    for (auto* object : untransferred) {
        need(com.live.count(object) == 1, "failed nonnull output remains owned by callback");
        object->Release();
    }
    untransferred.clear();
}
struct Backend {
    FfxInterface iface{};
    void* scratch{};
    size_t count;
    explicit Backend(size_t contexts, const BackendAllocationCallbacks& value = {}) : count(contexts) {
        const size_t size = ffxGetScratchMemorySizeDX12(contexts);
        need(size != 0, "valid scratch size");
        scratch = std::calloc(1, size);
        need(scratch != nullptr, "probe scratch allocation");
        ok(ffxGetInterfaceDX12WithCallbacks(&iface, proxyDevice, scratch, size, count, value), "actual source interface");
    }
    BackendContext_DX12& state() { return *static_cast<BackendContext_DX12*>(scratch); }
    uint32_t create(FfxEffect effect = FFX_EFFECT_OPTICALFLOW, FfxEffectBindlessConfig* bindless = nullptr) {
        uint32_t id = UINT32_MAX;
        ok(iface.fpCreateBackendContext(&iface, effect, bindless, &id), "create actual effect backend");
        need(id < count, "successful context publication");
        return id;
    }
    FfxApiEffectMemoryUsage usage(uint32_t id) {
        FfxApiEffectMemoryUsage result{};
        ok(iface.fpGetEffectGpuMemoryUsage(&iface, id, &result), "observable interface GPU memory query");
        return result;
    }
    void destroy(uint32_t id) { ok(iface.fpDestroyBackendContext(&iface, id), "destroy actual effect backend"); }
    ~Backend() {
        need(state().refCount == 0, "all backend contexts explicitly destroyed");
        std::free(scratch);
    }
};
static bool sameUsage(const FfxApiEffectMemoryUsage& a, const FfxApiEffectMemoryUsage& b) {
    return a.totalUsageInBytes == b.totalUsageInBytes && a.aliasableUsageInBytes == b.aliasableUsageInBytes;
}
static void memory(Backend& backend, uint32_t id, const FfxApiEffectMemoryUsage& expected) {
    const auto got = backend.usage(id);
    std::printf("MEMORY %s context=%u total=%llu aliasable=%llu expected_total=%llu expected_aliasable=%llu\n",
        scenario, id, static_cast<unsigned long long>(got.totalUsageInBytes), static_cast<unsigned long long>(got.aliasableUsageInBytes),
        static_cast<unsigned long long>(expected.totalUsageInBytes), static_cast<unsigned long long>(expected.aliasableUsageInBytes));
    need(sameUsage(got, expected), "interface GPU memory counters exactly match baseline/delta");
}
static FfxCreateResourceDescription description(FfxResourceInitDataType init, bool texture = false, bool aliasable = false) {
    static const std::array<unsigned char, 4096> data = [] {
        std::array<unsigned char, 4096> value{};
        for (size_t i = 0; i < value.size(); ++i) value[i] = static_cast<unsigned char>((i * 17 + 3) & 255);
        return value;
    }();
    FfxCreateResourceDescription desc{};
    desc.heapInfo = FfxResourceHeapPlacementInfo::InitDefault();
    desc.resourceDescription.type = texture ? FFX_API_RESOURCE_TYPE_TEXTURE2D : FFX_API_RESOURCE_TYPE_BUFFER;
    desc.resourceDescription.format = texture ? FFX_API_SURFACE_FORMAT_R8G8B8A8_UNORM : FFX_API_SURFACE_FORMAT_UNKNOWN;
    desc.resourceDescription.width = texture ? 16 : 4096;
    desc.resourceDescription.height = texture ? 16 : 1;
    desc.resourceDescription.depth = 1;
    desc.resourceDescription.mipCount = 1;
    if (!texture) { desc.resourceDescription.size = 4096; desc.resourceDescription.stride = 4; }
    desc.resourceDescription.usage = FFX_API_RESOURCE_USAGE_UAV;
    desc.resourceDescription.flags = aliasable ? FFX_API_RESOURCE_FLAGS_ALIASABLE : FFX_API_RESOURCE_FLAGS_NONE;
    desc.initialState = FFX_API_RESOURCE_STATE_UNORDERED_ACCESS;
    desc.name = L"Direct backend ownership regression";
    desc.initData.type = init;
    desc.initData.size = texture ? 1024 : 4096;
    if (init == FFX_RESOURCE_INIT_DATA_TYPE_BUFFER) desc.initData.buffer = data.data();
    if (init == FFX_RESOURCE_INIT_DATA_TYPE_VALUE) desc.initData.value = 0x6d;
    return desc;
}
static FfxResourceInternal create(Backend& backend, uint32_t id, const FfxCreateResourceDescription& desc) {
    FfxResourceInternal result{};
    ok(backend.iface.fpCreateResource(&backend.iface, &desc, id, &result), "actual primary/upload resource creation");
    need(result.internalIndex != 0, "published resource handle is nonzero");
    return result;
}
static void destroy(Backend& backend, uint32_t id, FfxResourceInternal resource) {
    ok(backend.iface.fpDestroyResource(&backend.iface, resource, id), "destroy actual resource owner");
}
// These observations use the production type included above, never a copied
// layout. Accounting itself is always queried through FfxInterface.
struct Checkpoint {
    uint32_t resourceCursor, uavCursor, dynamicResource, dynamicUav, jobs;
    FfxApiEffectMemoryUsage usage;
    std::vector<unsigned char> priorJobs;
    std::map<uint64_t, ULONG> references;
    size_t acceptedOwners;
    Checkpoint(Backend& backend, uint32_t id) :
        resourceCursor(backend.state().pEffectContexts[id].nextStaticResource),
        uavCursor(backend.state().pEffectContexts[id].nextStaticUavDescriptor),
        dynamicResource(backend.state().pEffectContexts[id].nextDynamicResource),
        dynamicUav(backend.state().pEffectContexts[id].nextDynamicUavDescriptor),
        jobs(backend.state().gpuJobCount), usage(backend.usage(id)),
        priorJobs(jobs * sizeof(FfxGpuJobDescription)),
        references(com.snapshot()), acceptedOwners(owners.size()) {
        if (jobs) std::memcpy(priorJobs.data(), backend.state().pGpuJobs, priorJobs.size());
    }
    void unchanged(Backend& backend, uint32_t id, bool compareReferences = true) const {
        const auto& effect = backend.state().pEffectContexts[id];
        need(effect.nextStaticResource == resourceCursor && effect.nextStaticUavDescriptor == uavCursor
             && effect.nextDynamicResource == dynamicResource && effect.nextDynamicUavDescriptor == dynamicUav,
             "exact static resource/UAV and dynamic cursor restoration");
        need(backend.state().gpuJobCount == jobs, "prior job queue count retained exactly");
        if (jobs) need(!std::memcmp(priorJobs.data(), backend.state().pGpuJobs, priorJobs.size()), "prior queued jobs retain exact bytes/order/identities");
        memory(backend, id, usage);
        need(owners.size() == acceptedOwners, "custom accepted owner count returns to checkpoint");
        if (compareReferences) com.baseline(references);
    }
};
static void failedResource(Backend& backend, uint32_t id, const FfxCreateResourceDescription& desc, const Checkpoint& before) {
    FfxResourceInternal result{std::numeric_limits<int32_t>::max()};
    const FfxErrorCode code = backend.iface.fpCreateResource(&backend.iface, &desc, id, &result);
    need(code != FFX_OK && result.internalIndex == 0, "failed create returns error and zero caller handle");
    releaseUntransferred();
    before.unchanged(backend, id);
    for (uint32_t i = before.resourceCursor; i < before.resourceCursor + 2; ++i) {
        const auto& slot = backend.state().pResources[i];
        need(!slot.resourcePtr && !slot.deallocator && !slot.allocationSize && !slot.uploadResourceIndex && !slot.uavDescCount,
             "all transaction-reserved resource slots are unpublished/zero");
    }
}
static void verifyUpload(Backend& backend, FfxResourceInternal primary, const FfxCreateResourceDescription& desc) {
    auto& owner = backend.state().pResources[primary.internalIndex];
    const int32_t upload = owner.uploadResourceIndex;
    need(upload != 0 && upload != primary.internalIndex, "primary publishes explicit actual upload identity");
    auto& child = backend.state().pResources[upload];
    need(child.resourcePtr != nullptr, "recorded upload identity names a live owner");
    bool found = false;
    for (uint32_t i = 0; i < backend.state().gpuJobCount; ++i) {
        const auto& job = backend.state().pGpuJobs[i];
        if (job.jobType == FFX_GPU_JOB_COPY && job.copyJobDescriptor.dst.internalIndex == primary.internalIndex) {
            need(job.copyJobDescriptor.src.internalIndex == upload, "copy job uses recorded upload, not inferred neighbor"); found = true;
        }
    }
    need(found, "initialization copy job actually scheduled");
    void* bytes{}; D3D12_RANGE empty{};
    hr(child.resourcePtr->Map(0, &empty, &bytes), "inspect actual upload allocation");
    const auto actualDesc = child.resourcePtr->GetDesc();
    need(actualDesc.Width >= desc.initData.size, "actual upload buffer contains logical data");
    const auto* actual = static_cast<const unsigned char*>(bytes);
    const auto targetDesc = owner.resourcePtr->GetDesc();
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT footprint{};
    UINT rows{}; UINT64 rowBytes{}, totalBytes{};
    realDevice->GetCopyableFootprints(&targetDesc, 0, 1, 0, &footprint, &rows, &rowBytes, &totalBytes);
    need(rowBytes * rows == desc.initData.size && totalBytes <= actualDesc.Width,
         "real footprint matches source initialization extent");
    for (UINT row = 0; row < rows; ++row) for (UINT64 column = 0; column < rowBytes; ++column) {
        const size_t source = static_cast<size_t>(row * rowBytes + column);
        const size_t target = static_cast<size_t>(footprint.Offset + row * footprint.Footprint.RowPitch + column);
        need(actual[target] == (desc.initData.type == FFX_RESOURCE_INIT_DATA_TYPE_VALUE ? desc.initData.value
                          : static_cast<const unsigned char*>(desc.initData.buffer)[source]),
             "real BUFFER/VALUE upload bytes including texture row pitch");
    }
    child.resourcePtr->Unmap(0, nullptr);
}

static uint64_t allocationBytes(ID3D12Resource* resource) {
    const auto desc = resource->GetDesc();
    const auto size = realDevice->GetResourceAllocationInfo(0, 1, &desc).SizeInBytes;
    need(size != 0 && size != UINT64_MAX, "real D3D12 allocation size is available");
    return size;
}
static void resourceLifecycle() {
    for (const bool custom : {false, true}) for (const bool texture : {false, true})
        for (const auto init : {FFX_RESOURCE_INIT_DATA_TYPE_BUFFER, FFX_RESOURCE_INIT_DATA_TYPE_VALUE}) {
            plan = {}; policies = {};
            const auto base = com.snapshot();
            Backend backend(1, custom ? callbacks(0) : BackendAllocationCallbacks{});
            const auto id = backend.create();
            const auto references = com.snapshot();
            const auto before = backend.usage(id);
            const auto desc = description(init, texture, true);
            if (custom) policies[0].reenter = true;
            const auto primary = create(backend, id, desc);
            need(plan.successes[static_cast<size_t>(Stage::UploadMap)] == 1 && plan.unmaps == 1,
                 "successful initialization Map has exactly one Unmap");
            auto& state = backend.state();
            const int32_t upload = state.pResources[primary.internalIndex].uploadResourceIndex;
            need(upload != 0, "successful primary owns an explicit upload handle");
            const uint64_t bytes = allocationBytes(state.pResources[primary.internalIndex].resourcePtr)
                + allocationBytes(state.pResources[upload].resourcePtr);
            auto expected = before;
            expected.totalUsageInBytes += bytes; expected.aliasableUsageInBytes += bytes;
            memory(backend, id, expected);
            Checkpoint mappedOwner(backend, id);
            plan.arm(Stage::UploadMap);
            void* rejectedMap = reinterpret_cast<void*>(uintptr_t{1});
            need(backend.iface.fpMapResource(&backend.iface, {upload}, &rejectedMap) == static_cast<FfxErrorCode>(FFX_ERROR_BACKEND_API_ERROR)
                 && rejectedMap == nullptr && plan.hits == 1 && plan.unmaps == 0,
                 "public Map failure never publishes temporary nonnull output or unmaps it");
            mappedOwner.unchanged(backend, id); plan = {};
            verifyUpload(backend, primary, desc);
            registerPair(1);
            destroy(backend, id, primary);
            need(!state.pResources[primary.internalIndex].resourcePtr && !state.pResources[upload].resourcePtr,
                 "destroying only primary clears exact primary and upload owners");
            need(state.gpuJobCount == 0, "destroying primary removes its pending initialization copy");
            need(owners.empty(), "custom primary/upload owner ledger is empty");
            if (custom) need(policies[0].resourceCalls == 2 && policies[0].resourceFrees == 2, "creator A allocates and frees both owners exactly once");
            need(policies[1].resourceCalls == 0 && policies[1].freeAttempts == 0, "later registration B cannot acquire old owners");
            memory(backend, id, before); com.baseline(references);
            backend.destroy(id); com.baseline(base);
        }
}
static void resourceFaults() {
    for (const bool custom : {false, true}) {
        plan = {}; policies = {};
        Backend backend(1, custom ? callbacks(0) : BackendAllocationCallbacks{});
        const auto id = backend.create();
        const auto retained = create(backend, id, description(FFX_RESOURCE_INIT_DATA_TYPE_BUFFER));
        const auto desc = description(FFX_RESOURCE_INIT_DATA_TYPE_VALUE, false, true);
        for (const auto stage : {Stage::Resource, Stage::UploadMap, Stage::Name}) {
            const uint64_t maximum = stage == Stage::UploadMap ? 1 : 2;
            for (uint64_t ordinal = 1; ordinal <= maximum; ++ordinal) {
                Checkpoint before(backend, id);
                plan.arm(stage, ordinal);
                failedResource(backend, id, desc, before);
                need(plan.hits == 1, "requested actual resource/Map/name failure was reached");
                if (stage == Stage::UploadMap) need(plan.unmaps == 0, "failed Map is never Unmapped as a successful mapping");
                plan = {};
            }
        }
        if (custom) {
            for (const auto fault : {AllocationFault::Error, AllocationFault::Throw, AllocationFault::NullSuccess, AllocationFault::Untransferred})
                for (const uint64_t ordinal : {1u, 2u}) {
                    Checkpoint before(backend, id);
                    auto& policy = policies[0];
                    policy.resourceFault = fault; policy.failResourceAt = policy.resourceCalls + ordinal;
                    failedResource(backend, id, desc, before);
                    need(policy.resourceCalls == policy.failResourceAt, "requested primary/upload callback ordinal reached");
                    policy.failResourceAt = 0;
                }
        }
        Checkpoint beforeRetry(backend, id);
        const auto retry = create(backend, id, desc);
        need(retry.internalIndex == static_cast<int32_t>(beforeRetry.resourceCursor), "retry receives exactly rolled-back resource cursor");
        need(backend.state().pResources[retry.internalIndex].uavDescIndex == beforeRetry.uavCursor,
             "retry receives exactly rolled-back UAV cursor");
        destroy(backend, id, retry);
        if (custom) {
            const auto uninitialized = create(backend, id, description(FFX_RESOURCE_INIT_DATA_TYPE_UNINITIALIZED));
            Checkpoint live(backend, id);
            policies[0].failResourceFree = true;
            need(backend.iface.fpDestroyResource(&backend.iface, uninitialized, id) == static_cast<FfxErrorCode>(FFX_ERROR_OUT_OF_MEMORY),
                 "resource deallocator error is returned before forgetting ownership");
            live.unchanged(backend, id);
            registerPair(1);
            destroy(backend, id, uninitialized);
        }
        destroy(backend, id, retained);
        memory(backend, id, {});
        backend.destroy(id);
        need(owners.empty(), "fault/retry resource callback ledger empty");
    }
}
static void queueRollback() {
    plan = {}; policies = {};
    Backend backend(1, callbacks(0));
    const auto id = backend.create();
    const auto retained = create(backend, id, description(FFX_RESOURCE_INIT_DATA_TYPE_BUFFER));
    FfxGpuJobDescription barrier{};
    barrier.jobType = FFX_GPU_JOB_BARRIER;
    const uint32_t capacity = backend.state().maxEffectContexts * FFX_MAX_GPU_JOBS;
    while (backend.state().gpuJobCount < capacity)
        ok(backend.iface.fpScheduleGpuJob(&backend.iface, &barrier), "fill queue via actual schedule callback");
    need(backend.iface.fpScheduleGpuJob(&backend.iface, &barrier) == static_cast<FfxErrorCode>(FFX_ERROR_INSUFFICIENT_MEMORY),
         "actual queue capacity rejects next job");
    Checkpoint full(backend, id);
    const auto frees = policies[0].resourceFrees;
    failedResource(backend, id, description(FFX_RESOURCE_INIT_DATA_TYPE_VALUE, false, true), full);
    need(policies[0].resourceFrees == frees + 2, "queue-full rollback releases accepted primary and upload once");
    destroy(backend, id, retained);
    need(backend.state().gpuJobCount == capacity - 1, "destroy removes only retained initialization copy, not prior barrier jobs");
    const auto retry = create(backend, id, description(FFX_RESOURCE_INIT_DATA_TYPE_BUFFER));
    need(retry.internalIndex == static_cast<int32_t>(full.resourceCursor), "queue failure did not consume resource cursor");
    need(backend.state().gpuJobCount == capacity, "retry fills exactly freed job capacity");
    destroy(backend, id, retry);
    memory(backend, id, {});
    backend.destroy(id);
}
using CreateResourceFn = decltype(FfxInterface{}.fpCreateResource);
static CreateResourceFn originalCreateResource{};
static FfxResourceInternal interposedGap{};
static bool insertGap{};
static FfxErrorCode separatedUpload(FfxInterface* iface, const FfxCreateResourceDescription* desc, uint32_t id, FfxResourceInternal* output) {
    if (desc->heapInfo.heapType == FFX_HEAP_TYPE_UPLOAD && std::exchange(insertGap, false)) {
        const auto independent = description(FFX_RESOURCE_INIT_DATA_TYPE_UNINITIALIZED);
        const auto result = originalCreateResource(iface, &independent, id, &interposedGap);
        if (result != FFX_OK) return result;
    }
    return originalCreateResource(iface, desc, id, output);
}
static void uploadIdentity() {
    plan = {}; policies = {};
    Backend backend(1, callbacks(0));
    const auto id = backend.create();
    originalCreateResource = backend.iface.fpCreateResource;
    backend.iface.fpCreateResource = separatedUpload;
    interposedGap = {}; insertGap = true;
    const auto primary = create(backend, id, description(FFX_RESOURCE_INIT_DATA_TYPE_BUFFER));
    backend.iface.fpCreateResource = originalCreateResource;
    const auto upload = backend.state().pResources[primary.internalIndex].uploadResourceIndex;
    need(interposedGap.internalIndex != 0 && upload != interposedGap.internalIndex && upload != primary.internalIndex + 1,
         "actual successful upload is deliberately nonadjacent to primary");
    auto* independent = backend.state().pResources[interposedGap.internalIndex].resourcePtr;
    need(independent != nullptr, "interposed neighboring owner is real and independent");
    const auto bytes = allocationBytes(independent);
    destroy(backend, id, primary);
    need(backend.state().pResources[interposedGap.internalIndex].resourcePtr == independent,
         "primary destruction never infers or destroys neighboring independent owner");
    need(!backend.state().pResources[upload].resourcePtr && backend.state().gpuJobCount == 0,
         "nonadjacent recorded upload and its copy job released once");
    memory(backend, id, FfxApiEffectMemoryUsage{bytes, 0});
    destroy(backend, id, interposedGap);
    memory(backend, id, {}); backend.destroy(id);
}
static FfxCreateHeapDescription heapDescription(uint64_t size = 65537, bool aliasable = true) {
    FfxCreateHeapDescription desc{};
    desc.heapType = FFX_HEAP_TYPE_DEFAULT; desc.size = size; desc.name = L"Actual ownership heap";
    desc.flags = aliasable ? FFX_HEAP_FLAGS_ALIASABLE : FFX_HEAP_FLAGS_NONE;
    return desc;
}
static void heapFailure(Backend& backend, uint32_t id, const FfxCreateHeapDescription& desc, const Checkpoint& before) {
    FfxResourceHeap output{};
    output.heap = reinterpret_cast<FfxHeap>(uintptr_t{1}); output.startOffset = 5; output.size = 7; output.aliasable = true;
    need(backend.iface.fpCreateHeap(&backend.iface, &desc, id, &output) != FFX_OK, "actual heap creation reports injected failure");
    need(!output.heap && !output.startOffset && !output.size && !output.aliasable, "failed heap output is completely zero");
    releaseUntransferred(); before.unchanged(backend, id);
}
static void heaps() {
    for (const bool custom : {false, true}) {
        plan = {}; policies = {};
        const auto base = com.snapshot();
        Backend backend(1, custom ? callbacks(0) : BackendAllocationCallbacks{});
        const auto id = backend.create();
        need(backend.state().resourceHeapsSupported, "real device must support heap tier 2 for direct heap/placement coverage");
        Checkpoint empty(backend, id);
        for (const auto size : {uint64_t{0}, UINT64_MAX}) heapFailure(backend, id, heapDescription(size), empty);
        plan.arm(Stage::Heap); heapFailure(backend, id, heapDescription(), empty);
        need(plan.hits == 1, "real default/custom device CreateHeap failure reached"); plan = {};
        if (custom) for (const auto fault : {AllocationFault::Error, AllocationFault::Throw, AllocationFault::NullSuccess, AllocationFault::Untransferred}) {
            auto& policy = policies[0]; policy.heapFault = fault; policy.failHeapAt = policy.heapCalls + 1;
            heapFailure(backend, id, heapDescription(), empty);
            need(policy.heapCalls == policy.failHeapAt, "custom heap failure callback reached"); policy.failHeapAt = 0;
        }
        const auto desc = heapDescription();
        policies[0].heapRequested = desc.size;
        FfxResourceHeap heap{};
        if (custom) policies[0].reenter = true;
        ok(backend.iface.fpCreateHeap(&backend.iface, &desc, id, &heap), "create actual default/custom heap");
        need(heap.heap && heap.size == desc.size && heap.aliasable, "heap pointer/logical size/alias flag published");
        memory(backend, id, FfxApiEffectMemoryUsage{desc.size, desc.size});
        registerPair(1);
        if (custom) {
            Checkpoint live(backend, id);
            policies[0].failHeapFree = true;
            need(backend.iface.fpDestroyHeap(&backend.iface, heap, id) == static_cast<FfxErrorCode>(FFX_ERROR_OUT_OF_MEMORY), "heap free error is returned");
            live.unchanged(backend, id);
            policies[0].throwHeapFree = true;
            need(backend.iface.fpDestroyHeap(&backend.iface, heap, id) == static_cast<FfxErrorCode>(FFX_ERROR_BACKEND_API_ERROR), "heap free exception is contained");
            live.unchanged(backend, id);
        }
        ok(backend.iface.fpDestroyHeap(&backend.iface, heap, id), "creator heap deallocator succeeds on retry");
        memory(backend, id, {}); com.baseline(empty.references);
        if (custom) need(policies[0].heapFrees == 1, "custom heap released exactly once after failed attempts");
        need(policies[1].freeAttempts == 0, "replacement registration never deallocates old heap");
        const auto persistentDesc = heapDescription(65536, false);
        policies[0].heapRequested = persistentDesc.size;
        FfxResourceHeap persistent{};
        ok(backend.iface.fpCreateHeap(&backend.iface, &persistentDesc, id, &persistent), "create nonaliasable default/custom heap");
        need(!persistent.aliasable, "nonaliasable heap publication preserves flag");
        memory(backend, id, FfxApiEffectMemoryUsage{persistentDesc.size, 0});
        ok(backend.iface.fpDestroyHeap(&backend.iface, persistent, id), "destroy nonaliasable heap through original creator");
        memory(backend, id, {}); com.baseline(empty.references);
        backend.destroy(id); com.baseline(base);
    }
    plan = {}; policies = {}; plan.tierOne = true;
    Backend unsupported(1);
    const auto id = unsupported.create();
    need(!unsupported.state().resourceHeapsSupported, "tier-1 feature response reaches unsupported heap branch");
    Checkpoint before(unsupported, id);
    heapFailure(unsupported, id, heapDescription(), before);
    unsupported.destroy(id); plan = {};
}
static void aliasPlacement() {
    plan = {}; policies = {};
    Backend backend(1);
    const auto id = backend.create();
    need(backend.state().resourceHeapsSupported, "placement coverage requires actual tier-2 device");
    const auto desc = heapDescription(2 * D3D12_DEFAULT_RESOURCE_PLACEMENT_ALIGNMENT);
    FfxResourceHeap heap{};
    ok(backend.iface.fpCreateHeap(&backend.iface, &desc, id, &heap), "create actual aliasable placement heap");
    const auto baseline = backend.usage(id);
    auto resourceDesc = description(FFX_RESOURCE_INIT_DATA_TYPE_UNINITIALIZED, false, true);
    resourceDesc.heapInfo = FfxResourceHeapPlacementInfo::InitPlacedDefault(heap.heap, heap.startOffset);
    const auto first = create(backend, id, resourceDesc);
    const auto second = create(backend, id, resourceDesc);
    need(backend.state().pResources[first.internalIndex].resourcePtr != backend.state().pResources[second.internalIndex].resourcePtr,
         "distinct real placed resources can alias the same heap range");
    memory(backend, id, baseline);
    Checkpoint before(backend, id);
    plan.arm(Stage::Resource);
    failedResource(backend, id, resourceDesc, before);
    need(plan.hits == 1, "actual CreatePlacedResource failure reached"); plan = {};
    registerPair(1);
    destroy(backend, id, first); memory(backend, id, baseline);
    destroy(backend, id, second); memory(backend, id, baseline);
    ok(backend.iface.fpDestroyHeap(&backend.iface, heap, id), "default placement heap remains default-owned after registration");
    memory(backend, id, {}); backend.destroy(id);
}
static void contextIsolation() {
    plan = {}; policies = {};
    const auto base = com.snapshot();
    Backend backend(2, callbacks(0));
    FfxEffectBindlessConfig entire{}; entire.maxTextureSrvs = FFX_MAX_STATIC_DESCRIPTOR_COUNT;
    const auto first = backend.create(FFX_EFFECT_OPTICALFLOW, &entire);
    const auto firstResource = create(backend, first, description(FFX_RESOURCE_INIT_DATA_TYPE_BUFFER));
    Checkpoint firstLive(backend, first);
    FfxEffectBindlessConfig one{}; one.maxTextureSrvs = 1;
    uint32_t failed = 17;
    need(backend.iface.fpCreateBackendContext(&backend.iface, FFX_EFFECT_FRAMEINTERPOLATION, &one, &failed) == static_cast<FfxErrorCode>(FFX_ERROR_INSUFFICIENT_MEMORY)
         && failed == UINT32_MAX, "failed second context is not published after bindless exhaustion");
    need(backend.state().refCount == 1 && backend.state().pEffectContexts[first].active,
         "second context failure leaves first context active/refcount unchanged");
    firstLive.unchanged(backend, first);
    const auto second = backend.create(FFX_EFFECT_FRAMEINTERPOLATION);
    const auto secondResource = create(backend, second, description(FFX_RESOURCE_INIT_DATA_TYPE_VALUE));
    Checkpoint firstWithSecond(backend, first);
    failed = 17;
    need(backend.iface.fpCreateBackendContext(&backend.iface, FFX_EFFECT_OPTICALFLOW, nullptr, &failed) == static_cast<FfxErrorCode>(FFX_ERROR_INSUFFICIENT_MEMORY)
         && failed == UINT32_MAX, "exhausted effect IDs reject extra context without publication");
    need(backend.state().refCount == 2, "context exhaustion does not increment shared refcount");
    firstWithSecond.unchanged(backend, first);
    const auto firstMemory = backend.usage(first);
    auto* firstPointer = backend.state().pResources[firstResource.internalIndex].resourcePtr;
    destroy(backend, second, secondResource); backend.destroy(second);
    need(backend.state().refCount == 1 && backend.state().device && backend.state().dxgiFactory
         && backend.state().descHeapSrvCpu && backend.state().descHeapUavCpu && backend.state().descHeapUavGpu
         && backend.state().descRingBuffer && backend.state().descHeapRtvCpu, "second teardown retains all first-context shared objects");
    need(backend.state().pResources[firstResource.internalIndex].resourcePtr == firstPointer && backend.state().gpuJobCount == 1,
         "second teardown retains first resource and exact initialization job");
    firstLive.unchanged(backend, first);
    memory(backend, first, firstMemory);
    destroy(backend, first, firstResource); backend.destroy(first);
    need(!backend.state().constantBufferMutexInitialized && !backend.state().device && !backend.state().dxgiFactory,
         "final context releases shared owners and destroys constructed mutex");
    com.baseline(base);
}

struct CpuTrace {
    uint64_t allocations{}, deallocations{};
    static void* alloc(void* user, uint64_t size) {
        auto& trace = *static_cast<CpuTrace*>(user); ++trace.allocations;
        return std::malloc(static_cast<size_t>(size));
    }
    static void dealloc(void* user, void* pointer) {
        auto& trace = *static_cast<CpuTrace*>(user); ++trace.deallocations; std::free(pointer);
    }
};
static void scratchPublication() {
    plan = {}; policies = {};
    need(ffxGetScratchMemorySizeDX12(0) == 0, "zero context scratch size rejected");
    need(ffxGetScratchMemorySizeDX12(SIZE_MAX) == 0, "overflow context scratch size rejected");
    need(ffxGetScratchMemorySizeDX12(static_cast<size_t>(INT32_MAX) / FFX_MAX_RESOURCE_COUNT + 1) == 0,
         "resource-handle index overflow scratch size rejected");
    Backend backend(1);
    const auto size = backend.iface.scratchBufferSize;
    const auto rejected = [&](FfxDevice device, void* scratch, size_t bytes, size_t contexts, const BackendAllocationCallbacks& value) {
        FfxInterface sentinel{};
        std::memset(&sentinel, 0xa5, sizeof(sentinel));
        std::array<unsigned char, sizeof(FfxInterface)> before{};
        std::memcpy(before.data(), &sentinel, before.size());
        need(ffxGetInterfaceDX12WithCallbacks(&sentinel, device, scratch, bytes, contexts, value) != FFX_OK,
             "invalid source interface request rejected");
        need(!std::memcmp(before.data(), &sentinel, before.size()), "rejected interface leaves destination publication sentinel intact");
    };
    rejected(nullptr, backend.scratch, size, 1, {});
    rejected(proxyDevice, nullptr, size, 1, {});
    rejected(proxyDevice, static_cast<unsigned char*>(backend.scratch) + 1, size - 1, 1, {});
    rejected(proxyDevice, backend.scratch, size - 1, 1, {});
    rejected(proxyDevice, backend.scratch, size, 0, {});
    rejected(proxyDevice, backend.scratch, size, SIZE_MAX, {});
    auto mismatch = callbacks(0); mismatch.resourceDeallocator = nullptr;
    rejected(proxyDevice, backend.scratch, size, 1, mismatch);
    mismatch = callbacks(0); mismatch.heapDeallocator = nullptr;
    rejected(proxyDevice, backend.scratch, size, 1, mismatch);
    CpuTrace trace;
    ffxAllocationCallbacks cpu{}; cpu.alloc = CpuTrace::alloc; cpu.dealloc = CpuTrace::dealloc; cpu.pUserData = &trace;
    Allocator allocator(&cpu);
    ffxCreateContextDescHeader root{};
    ffxCreateBackendDX12Desc first{}, duplicate{};
    first.header.type = duplicate.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_BACKEND_DX12;
    first.device = duplicate.device = proxyDevice;
    root.pNext = &first.header; first.header.pNext = &duplicate.header;
    FfxInterface sentinel = backend.iface;
    std::array<unsigned char, sizeof(FfxInterface)> initial{};
    std::memcpy(initial.data(), &sentinel, initial.size());
    bool found = false;
    need(CreateBackend(&root, found, &sentinel, 1, allocator, {}) != FFX_API_RETURN_OK && !found,
         "duplicate backend descriptors fail before publication");
    need(trace.allocations == 0 && trace.deallocations == 0, "duplicate descriptors do not enter public CPU allocation sweep");
    need(!std::memcmp(initial.data(), &sentinel, initial.size()), "duplicate backend preserves existing interface");
    const auto id = backend.create();
    const auto resource = create(backend, id, description(FFX_RESOURCE_INIT_DATA_TYPE_BUFFER));
    Checkpoint live(backend, id);
    rejected(proxyDevice, backend.scratch, size, 1, {});
    live.unchanged(backend, id);
    need(backend.state().refCount == 1 && backend.state().pEffectContexts[id].active,
         "live scratch cannot be cleared or republished");
    destroy(backend, id, resource); backend.destroy(id);
}
static void firstInitialization() {
    for (const auto stage : {Stage::Descriptor, Stage::Factory, Stage::Options}) {
        const uint64_t last = stage == Stage::Descriptor ? 5 : 1;
        for (uint64_t ordinal = 1; ordinal <= last; ++ordinal) {
            plan = {}; policies = {};
            const auto base = com.snapshot();
            Backend backend(1);
            const size_t retiredBefore = com.retired.size();
            plan.arm(stage, ordinal);
            uint32_t id = 19;
            need(backend.iface.fpCreateBackendContext(&backend.iface, FFX_EFFECT_OPTICALFLOW, nullptr, &id) != FFX_OK && id == UINT32_MAX,
                 "failed first initialization returns invalid context ID");
            need(plan.hits == 1, "requested descriptor/factory/options failure actually reached");
            const auto& state = backend.state();
            need(state.refCount == 0 && !state.constantBufferMutexInitialized && !state.device && !state.dxgiFactory
                 && !state.descHeapSrvCpu && !state.descHeapUavCpu && !state.descHeapUavGpu && !state.descRingBuffer && !state.descHeapRtvCpu,
                 "failed first initialization releases every shared owner and constructed mutex");
            for (size_t i = 0; i < backend.count; ++i) need(!state.pEffectContexts[i].active, "no failed effect publication");
            if (stage == Stage::Options) {
                size_t factories = 0;
                for (size_t i = retiredBefore; i < com.retired.size(); ++i) if (com.retired[i].kind == "factory") ++factories;
                need(factories == 1, "actual returned DXGI factory reference released exactly once on later failure");
            }
            com.baseline(base);
            plan = {};
            const auto retry = backend.create();
            backend.destroy(retry); com.baseline(base);
        }
    }
    plan = {};
    const auto base = com.snapshot();
    Backend backend(1);
    FfxEffectBindlessConfig oversized{}; oversized.maxTextureSrvs = FFX_MAX_STATIC_DESCRIPTOR_COUNT + 1;
    uint32_t id = 3;
    need(backend.iface.fpCreateBackendContext(&backend.iface, FFX_EFFECT_OPTICALFLOW, &oversized, &id) == static_cast<FfxErrorCode>(FFX_ERROR_INSUFFICIENT_MEMORY)
         && id == UINT32_MAX, "first context bindless overflow never publishes effect");
    need(!backend.state().constantBufferMutexInitialized && backend.state().refCount == 0, "first bindless failure unwinds shared initialization");
    com.baseline(base);
    id = backend.create(); backend.destroy(id); com.baseline(base);
}
static void serializerOwnership() {
    plan = {}; policies = {};
    Backend backend(1);
    const auto id = backend.create();
    FfxShaderBlob shader{};
    ok(opticalflowGetPermutationBlobByIndex(FFX_OPTICALFLOW_PASS_PREPARE_LUMA, 0, &shader), "actual authenticated native shader metadata");
    need(shader.data && shader.size, "serializer test does not invent a shader blob");
    FfxPipelineDescription desc{};
    desc.stage = FFX_BIND_COMPUTE_SHADER_STAGE;
    std::wcscpy(desc.name, L"Serializer ownership failure");
    for (const uint32_t indirect : {0u, 1u}) {
        desc.indirectWorkload = indirect;
        Checkpoint before(backend, id);
        const auto retiredBefore = com.retired.size();
        plan.arm(Stage::Serializer);
        FfxPipelineState output{};
        std::memset(&output, 0xa5, sizeof(output));
        need(backend.iface.fpCreatePipeline(&backend.iface, &shader, &desc, id, &output) == static_cast<FfxErrorCode>(FFX_ERROR_BACKEND_API_ERROR),
             "real serialization output/error blob failure propagates");
        need(plan.hits == 1, "dynamically looked up serializer interception was actually reached");
        const auto* bytes = reinterpret_cast<const unsigned char*>(&output);
        for (size_t i = 0; i < sizeof(output); ++i) need(bytes[i] == 0, "failed pipeline output completely zero");
        size_t blobs = 0;
        for (size_t i = retiredBefore; i < com.retired.size(); ++i) if (com.retired[i].kind == "blob") ++blobs;
        need(blobs == 2, "both real serializer output and error blobs released once");
        before.unchanged(backend, id); plan = {};
        for (const auto stage : {Stage::Root, Stage::Command, Stage::Pipeline, Stage::Name}) {
            if (stage == Stage::Command && !indirect) continue;
            plan.arm(stage);
            std::memset(&output, 0xa5, sizeof(output));
            need(backend.iface.fpCreatePipeline(&backend.iface, &shader, &desc, id, &output)
                     == static_cast<FfxErrorCode>(FFX_ERROR_BACKEND_API_ERROR),
                 "pipeline subobject or naming failure propagates");
            need(plan.hits == 1, "requested pipeline substage actually reached");
            for (size_t i = 0; i < sizeof(output); ++i)
                need(reinterpret_cast<const unsigned char*>(&output)[i] == 0, "partial pipeline is not published");
            before.unchanged(backend, id);
            plan = {};
        }
        ok(backend.iface.fpCreatePipeline(&backend.iface, &shader, &desc, id, &output),
           "real native-shader pipeline succeeds after failures");
        need(output.pipeline && output.rootSignature && bool(output.cmdSignature) == bool(indirect),
             "successful direct/indirect pipeline publishes exactly required owners");
        ok(backend.iface.fpDestroyPipeline(&backend.iface, &output, id), "destroy complete real pipeline");
        before.unchanged(backend, id);
    }
    backend.destroy(id);
}
struct Scenario { const wchar_t* option; const char* name; void (*run)(); };
static const Scenario scenarios[] = {
    {L"scratch", "scratch-publication", scratchPublication},
    {L"first-init", "first-init-unwind-retry", firstInitialization},
    {L"heaps", "heap-accounting-provenance-retry", heaps},
    {L"resources", "resource-buffer-value-lifecycle", resourceLifecycle},
    {L"resource-faults", "resource-upload-map-name-rollback", resourceFaults},
    {L"queue", "queue-full-resource-rollback", queueRollback},
    {L"upload-identity", "nonadjacent-upload-owner", uploadIdentity},
    {L"alias", "aliasable-placement-ownership", aliasPlacement},
    {L"contexts", "shared-backend-context-isolation", contextIsolation},
    {L"serializer", "serializer-real-blob-unwind", serializerOwnership},
};
} // namespace backend_probe

HRESULT WINAPI backendProbeCreateDXGIFactory2(UINT flags, REFIID iid, void** output) {
    return backend_probe::factory(flags, iid, output);
}
FARPROC WINAPI backendProbeGetProcAddress(HMODULE module, LPCSTR name) { return backend_probe::symbol(module, name); }
extern "C" int yaagl_fsr3_install(void* module);
int wmain(int argc, wchar_t** argv) {
    using namespace backend_probe;
    if (argc == 2 && !std::wcscmp(argv[1], L"--list")) {
        for (const auto& entry : scenarios) std::printf("%s\n", entry.name);
        return 0;
    }
    const bool all = argc == 4 && !std::wcscmp(argv[1], L"--native") && !std::wcscmp(argv[3], L"--all");
    const bool single = argc == 5 && !std::wcscmp(argv[1], L"--native") && !std::wcscmp(argv[3], L"--scenario");
    if (!all && !single) {
        std::fprintf(stderr, "usage: fsr3-backend-probe.exe --native <original-native-dll> --all | --scenario <scratch|first-init|heaps|resources|resource-faults|queue|upload-identity|alias|contexts|serializer>\n");
        return 64;
    }
    bool selected = all;
    for (const auto& entry : scenarios) if (single && !std::wcscmp(argv[4], entry.option)) selected = true;
    if (!selected) { std::fprintf(stderr, "unknown scenario\n"); return 64; }
    HMODULE native = LoadLibraryW(argv[2]);
    need(native != nullptr, "load caller-selected original native FSR DLL");
    need(yaagl_fsr3_install(native) == YAAGL_FSR3_INSTALL_OK,
         "production native authentication and real native thunk initialization");
    wchar_t nativePath[32768]{};
    need(GetModuleFileNameW(native, nativePath, static_cast<DWORD>(std::size(nativePath))) != 0, "report loaded native path");
    std::printf("NATIVE %ls\n", nativePath);
    hr(D3D12CreateDevice(nullptr, D3D_FEATURE_LEVEL_12_0, IID_PPV_ARGS(&realDevice)), "create real D3D12 device");
    proxyDevice = new Device(realDevice);
    const auto baseline = com.snapshot();
    for (const auto& entry : scenarios) {
        if (single && std::wcscmp(argv[4], entry.option)) continue;
        scenario = entry.name;
        entry.run();
        need(owners.empty() && untransferred.empty(), "no accepted or untransferred custom owners left");
        com.baseline(baseline);
        std::printf("PASS %s\n", scenario); std::fflush(stdout);
    }
    proxyDevice->Release(); proxyDevice = nullptr; realDevice = nullptr;
    com.baseline({});
    std::printf("PASS fsr3-backend-probe selected_scenarios_complete\n");
    return 0;
}
