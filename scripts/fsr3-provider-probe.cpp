// Public, process-isolated FSR3 3.1.6 construction and GPU regression diagnostic.
// No private provider/context layouts, replacement shaders, or production test exports.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <bcrypt.h>
#include <d3d12.h>
#include <dxgi1_4.h>
#include <wrl/client.h>
#include <algorithm>
#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cwchar>
#include <exception>
#include <functional>
#include <map>
#include <mutex>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>
#include "../d3dmetal-sidecar/third-party/fidelityfx/Kits/FidelityFX/api/include/dx12/ffx_api_dx12.h"
#include "../d3dmetal-sidecar/third-party/fidelityfx/Kits/FidelityFX/framegeneration/include/ffx_framegeneration.h"

using Microsoft::WRL::ComPtr;
constexpr DWORD kWait = 180000;
constexpr UINT kRenderWidth = 128, kRenderHeight = 72;
constexpr UINT kDisplayWidth = 256, kDisplayHeight = 144;
constexpr uint64_t kFsr316 = UINT64_C(0xf600000000c01006);

[[noreturn]] static void fail(const char* text) {
    std::fprintf(stderr, "FAIL %s (last_error=%lu)\n", text, GetLastError());
    std::fflush(nullptr);
    ExitProcess(1);
}
static void need(bool value, const char* text) { if (!value) fail(text); }
static void hr(HRESULT value, const char* text) {
    if (FAILED(value)) {
        std::fprintf(stderr, "HRESULT operation=%s value=%08lx\n", text, static_cast<unsigned long>(value));
        fail(text);
    }
}
static void ffx(ffxReturnCode_t value, const char* text) {
    if (value != FFX_API_RETURN_OK) {
        std::fprintf(stderr, "FFX operation=%s value=%u\n", text, value);
        fail(text);
    }
}
struct Event {
    HANDLE value{};
    explicit Event(bool manual = true) : value(CreateEventW(nullptr, manual, FALSE, nullptr)) {
        need(value != nullptr, "CreateEvent");
    }
    ~Event() { CloseHandle(value); }
    Event(const Event&) = delete;
    Event& operator=(const Event&) = delete;
    void set() { need(SetEvent(value), "SetEvent"); }
    void wait() const { need(WaitForSingleObject(value, kWait) == WAIT_OBJECT_0, "event bounded completion"); }
};
static std::string utf8(const std::wstring& text) {
    if (text.empty()) return {};
    int size = WideCharToMultiByte(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), nullptr, 0, nullptr, nullptr);
    need(size > 0, "UTF-8 size");
    std::string result(size, '\0');
    need(WideCharToMultiByte(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), result.data(), size, nullptr, nullptr) == size, "UTF-8 conversion");
    return result;
}
static std::wstring modulePath(HMODULE module) {
    std::vector<wchar_t> buffer(32768);
    DWORD count = GetModuleFileNameW(module, buffer.data(), static_cast<DWORD>(buffer.size()));
    need(count && count < buffer.size(), "GetModuleFileName");
    return {buffer.data(), count};
}
static std::string sha256(const std::wstring& path) {
    HANDLE file = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_DELETE,
                              nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    need(file != INVALID_HANDLE_VALUE, "open module identity bytes");
    BCRYPT_ALG_HANDLE algorithm{};
    BCRYPT_HASH_HANDLE hash{};
    need(BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) >= 0, "SHA256 provider");
    need(BCryptCreateHash(algorithm, &hash, nullptr, 0, nullptr, 0, 0) >= 0, "SHA256 create");
    std::array<unsigned char, 65536> bytes{};
    DWORD count{};
    uint64_t total{};
    for (;;) {
        need(ReadFile(file, bytes.data(), static_cast<DWORD>(bytes.size()), &count, nullptr), "read module identity bytes");
        if (!count) break;
        total += count;
        need(BCryptHashData(hash, bytes.data(), count, 0) >= 0, "SHA256 update");
    }
    std::array<unsigned char, 32> digest{};
    need(BCryptFinishHash(hash, digest.data(), static_cast<ULONG>(digest.size()), 0) >= 0, "SHA256 finish");
    BCryptDestroyHash(hash);
    BCryptCloseAlgorithmProvider(algorithm, 0);
    CloseHandle(file);
    need(total != 0, "module identity nonempty file");
    char hex[65]{};
    for (size_t i = 0; i < digest.size(); ++i) std::snprintf(hex + i * 2, 3, "%02x", digest[i]);
    return hex;
}
struct Api {
    HMODULE module{};
    PfnFfxCreateContext create{};
    PfnFfxDestroyContext destroy{};
    PfnFfxConfigure configure{};
    PfnFfxQuery query{};
    PfnFfxDispatch dispatch{};
    uint64_t selected{};
    bool dependenciesReported{};
    std::mutex identityMutex;
    explicit Api(const wchar_t* path) {
        module = LoadLibraryW(path);
        need(module != nullptr, "LoadLibrary public module");
        auto resolved = modulePath(module);
        std::printf("MODULE path=%s sha256=%s\n", utf8(resolved).c_str(), sha256(resolved).c_str());
#define LOAD_API(field, type, symbol) \
        field = reinterpret_cast<type>(GetProcAddress(module, symbol)); \
        need(field != nullptr, symbol); \
        { HMODULE owner{}; need(GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT, \
            reinterpret_cast<LPCWSTR>(field), &owner), "export owner"); \
          std::printf("EXPORT name=%s owner=%s\n", symbol, utf8(modulePath(owner)).c_str()); }
        LOAD_API(create, PfnFfxCreateContext, "ffxCreateContext");
        LOAD_API(destroy, PfnFfxDestroyContext, "ffxDestroyContext");
        LOAD_API(configure, PfnFfxConfigure, "ffxConfigure");
        LOAD_API(query, PfnFfxQuery, "ffxQuery");
        LOAD_API(dispatch, PfnFfxDispatch, "ffxDispatch");
#undef LOAD_API
    }
    ~Api() { FreeLibrary(module); }
    void select(ID3D12Device* device) {
        const LUID luid = device->GetAdapterLuid();
        std::printf("D3D12_DEVICE luid=%08lx:%08lx nodes=%u feature_level=12_0\n",
            static_cast<unsigned long>(luid.HighPart), static_cast<unsigned long>(luid.LowPart), device->GetNodeCount());
        uint64_t count{};
        ffxQueryDescGetVersions versions{};
        versions.header.type = FFX_API_QUERY_DESC_TYPE_GET_VERSIONS;
        versions.createDescType = FFX_API_CREATE_CONTEXT_DESC_TYPE_FRAMEGENERATION;
        versions.device = device;
        versions.outputCount = &count;
        ffx(query(nullptr, &versions.header), "enumerate provider count");
        need(count && count <= 1024, "provider count valid");
        std::vector<uint64_t> ids(count);
        std::vector<const char*> names(count);
        versions.versionIds = ids.data();
        versions.versionNames = names.data();
        ffx(query(nullptr, &versions.header), "enumerate provider identities");
        need(count <= ids.size(), "provider enumeration capacity");
        for (uint64_t i = 0; i < count; ++i) {
            std::printf("PROVIDER id=%016llx name=%s\n", static_cast<unsigned long long>(ids[i]), names[i] ? names[i] : "<null>");
            if (ids[i] == kFsr316) {
                need(!selected && names[i], "unique named FSR3 3.1.6 provider");
                selected = ids[i];
            }
        }
        need(selected == kFsr316, "queried FSR3 3.1.6 provider available");
    }
    void identity(ffxContext* context) {
        std::lock_guard lock(identityMutex);
        if (!dependenciesReported) {
            for (const auto* name : {L"amd_fidelityfx_framegeneration_dx12_native.dll", L"yaagl_fsr3_lifetime.dll"}) {
                HMODULE dependency = GetModuleHandleW(name);
                if (dependency) {
                    const auto path = modulePath(dependency);
                    std::printf("PROVIDER_MODULE path=%s sha256=%s\n", utf8(path).c_str(), sha256(path).c_str());
                }
            }
            dependenciesReported = true;
        }
        ffxQueryGetProviderVersion version{};
        version.header.type = FFX_API_QUERY_DESC_TYPE_GET_PROVIDER_VERSION;
        ffx(query(context, &version.header), "query created provider");
        need(version.versionId == selected && version.versionName, "created provider matches queried override");
        std::printf("CONTEXT_PROVIDER id=%016llx name=%s\n", static_cast<unsigned long long>(version.versionId), version.versionName);
    }
};

enum class Stage : unsigned { Cpu, Descriptor, Root, Command, Pipeline, Resource, Heap, ResourceCallback, HeapCallback, UploadMap, Count };
constexpr size_t kStages = static_cast<size_t>(Stage::Count);
static const char* stageName(Stage stage) {
    static const char* names[]{"cpu", "descriptor", "root", "command", "pso", "resource", "heap", "resource-callback", "heap-callback", "upload-map"};
    return names[static_cast<size_t>(stage)];
}
using Counts = std::array<uint64_t, kStages>;
struct Lane;
thread_local Lane* currentLane{};
struct Plan {
    Counts calls{}, succeeded{};
    Stage stage = Stage::Count;
    uint64_t ordinal{}, hits{};
    bool enter(Stage at) {
        uint64_t n = ++calls[static_cast<size_t>(at)];
        if (stage == at && n == ordinal) { ++hits; return true; }
        return false;
    }
    void success(Stage at) { ++succeeded[static_cast<size_t>(at)]; }
};
struct Lane {
    Plan plan;
    unsigned pair{};
    std::map<void*, uint64_t> cpu;
    void* independent{};
    std::function<void()> onAlloc, onDealloc;
    bool hookActive{};
    unsigned hooks{};
    void hook(bool allocation) {
        auto& fn = allocation ? onAlloc : onDealloc;
        if (fn && !hookActive) {
            hookActive = true;
            ++hooks;
            auto once = std::exchange(fn, {});
            once();
            hookActive = false;
        }
    }
    static void* alloc(void* user, uint64_t size) {
        auto& lane = *static_cast<Lane*>(user);
        lane.hook(true);
        if (lane.plan.enter(Stage::Cpu)) return nullptr;
        static_assert(sizeof(size_t) == sizeof(uint64_t));
        need(size != 0, "CPU allocation size");
        void* memory = std::malloc(static_cast<size_t>(size));
        if (!memory) return nullptr;
        need(lane.cpu.emplace(memory, size).second, "unique CPU creator");
        lane.plan.success(Stage::Cpu);
        return memory;
    }
    static void dealloc(void* user, void* memory) {
        if (!memory) return;
        auto& lane = *static_cast<Lane*>(user);
        auto found = lane.cpu.find(memory);
        need(found != lane.cpu.end(), "CPU deallocator matches exact creator userdata/pointer");
        lane.hook(false);
        lane.cpu.erase(found);
        std::free(memory);
    }
    void start(unsigned callbackPair) {
        pair = callbackPair;
        independent = alloc(this, 37);
        need(independent != nullptr, "independent allocation");
        std::memset(independent, 0xa7, 37);
        plan = {};
    }
    void clean() const {
        need(cpu.size() == 1 && cpu.count(independent) && cpu.at(independent) == 37, "CPU owner baseline");
        const auto* bytes = static_cast<const unsigned char*>(independent);
        for (unsigned i = 0; i < 37; ++i) need(bytes[i] == 0xa7, "independent allocation still alive");
    }
    void finish() { clean(); dealloc(this, independent); independent = nullptr; need(cpu.empty(), "CPU final empty"); }
};
struct Current {
    Lane* previous;
    explicit Current(Lane& lane) : previous(currentLane) { currentLane = &lane; }
    ~Current() { currentLane = previous; }
};
static bool inject(Stage stage) { return currentLane && currentLane->plan.enter(stage); }
static void succeeded(Stage stage) { if (currentLane) currentLane->plan.success(stage); }

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
    HRESULT STDMETHODCALLTYPE SetName(LPCWSTR name) override { return real->SetName(name); }
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
        hr(actual->QueryInterface(IID_PPV_ARGS(&a)), "real child device interface");
        hr(realDevice->QueryInterface(IID_PPV_ARGS(&b)), "real device interface");
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
        if (upload && inject(Stage::UploadMap)) { if (output) *output = nullptr; return E_OUTOFMEMORY; }
        HRESULT result = real->Map(subresource, range, output);
        if (upload && SUCCEEDED(result)) succeeded(Stage::UploadMap);
        return result;
    }
    void STDMETHODCALLTYPE Unmap(UINT subresource, const D3D12_RANGE* range) override { real->Unmap(subresource, range); }
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
    HRESULT STDMETHODCALLTYPE CheckFeatureSupport(D3D12_FEATURE feature, void* value, UINT size) override { return real->CheckFeatureSupport(feature, value, size); }
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

struct GpuOwner { Lane* lane; unsigned pair; uint32_t effect; bool heap; uint64_t capacity; };
static std::mutex gpuMutex;
static std::map<void*, GpuOwner> gpuOwners;
static void gpuClean() { std::lock_guard lock(gpuMutex); need(gpuOwners.empty(), "exact resource/heap callback owner baseline"); }
static void rememberGpu(void* pointer, unsigned pair, uint32_t effect, bool heap, uint64_t capacity) {
    need(currentLane && currentLane->pair == pair, "allocator registration pair belongs to creator");
    std::lock_guard lock(gpuMutex);
    need(gpuOwners.emplace(pointer, GpuOwner{currentLane, pair, effect, heap, capacity}).second, "unique GPU callback creator");
}
static GpuOwner forgetGpu(void* pointer, unsigned pair, uint32_t effect, bool heap) {
    std::lock_guard lock(gpuMutex);
    auto found = gpuOwners.find(pointer);
    need(found != gpuOwners.end(), "deallocator must not release unknown/unaccepted output");
    auto owner = found->second;
    need(owner.pair == pair && owner.effect == effect && owner.heap == heap, "exact GPU creator/deallocator/effect identity");
    gpuOwners.erase(found);
    return owner;
}
static std::function<void()> gpuAllocateHook, gpuDeallocateHook;
static void runGpuHook(bool allocation) {
    auto& fn = allocation ? gpuAllocateHook : gpuDeallocateHook;
    if (fn) { auto once = std::exchange(fn, {}); once(); }
}
template<unsigned Pair> static ffxReturnCode_t resourceAllocate(uint32_t effect, D3D12_RESOURCE_STATES state,
    const D3D12_HEAP_PROPERTIES* heap, const D3D12_RESOURCE_DESC* desc,
    const FfxApiResourceDescription*, const D3D12_CLEAR_VALUE* clear, ID3D12Resource** output) {
    need(output && currentLane, "resource allocator arguments");
    *output = nullptr;
    runGpuHook(true);
    if (inject(Stage::ResourceCallback)) return FFX_API_RETURN_ERROR_MEMORY;
    ID3D12Resource* value{};
    HRESULT result = proxyDevice->CreateCommittedResource(heap, D3D12_HEAP_FLAG_NONE, desc, state, clear, IID_PPV_ARGS(&value));
    if (FAILED(result)) return FFX_API_RETURN_ERROR_MEMORY;
    rememberGpu(value, Pair, effect, false, realDevice->GetResourceAllocationInfo(0, 1, desc).SizeInBytes);
    succeeded(Stage::ResourceCallback);
    *output = value;
    return FFX_API_RETURN_OK;
}
template<unsigned Pair> static ffxReturnCode_t resourceDeallocate(uint32_t effect, ID3D12Resource* resource) {
    auto owner = forgetGpu(resource, Pair, effect, false);
    Current active(*owner.lane);
    runGpuHook(false);
    resource->Release();
    return FFX_API_RETURN_OK;
}
template<unsigned Pair> static ffxReturnCode_t heapAllocate(uint32_t effect, const D3D12_HEAP_DESC* desc,
    bool, ID3D12Heap** output, uint64_t* offset) {
    need(output && offset && currentLane, "heap allocator arguments");
    *output = nullptr; *offset = 0;
    if (inject(Stage::HeapCallback)) return FFX_API_RETURN_ERROR_MEMORY;
    ID3D12Heap* value{};
    HRESULT result = proxyDevice->CreateHeap(desc, IID_PPV_ARGS(&value));
    if (FAILED(result)) return FFX_API_RETURN_ERROR_MEMORY;
    rememberGpu(value, Pair, effect, true, desc->SizeInBytes);
    succeeded(Stage::HeapCallback);
    *output = value;
    return FFX_API_RETURN_OK;
}
template<unsigned Pair> static ffxReturnCode_t heapDeallocate(uint32_t effect, ID3D12Heap* heap, uint64_t offset, uint64_t size) {
    auto owner = forgetGpu(heap, Pair, effect, true);
    need(offset == 0 && size && size <= owner.capacity, "heap creator offset and requested size within exact real capacity");
    heap->Release();
    return FFX_API_RETURN_OK;
}
static void callbackPair(ffxCreateBackendDX12AllocationCallbacksDesc& callbacks, unsigned pair) {
    callbacks = {};
    callbacks.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_BACKEND_DX12_ALLOCATION_CALLBACKS;
    if (pair == 1) {
        callbacks.pfnFfxResourceAllocator = resourceAllocate<1>; callbacks.pfnFfxResourceDeallocator = resourceDeallocate<1>;
        callbacks.pfnFfxHeapAllocator = heapAllocate<1>; callbacks.pfnFfxHeapDeallocator = heapDeallocate<1>;
    } else if (pair == 2) {
        callbacks.pfnFfxResourceAllocator = resourceAllocate<2>; callbacks.pfnFfxResourceDeallocator = resourceDeallocate<2>;
        callbacks.pfnFfxHeapAllocator = heapAllocate<2>; callbacks.pfnFfxHeapDeallocator = heapDeallocate<2>;
    } else need(pair == 0, "valid callback pair");
}
struct Context {
    Api& api;
    Lane& lane;
    ffxContext value{};
    ffxCreateContextDescFrameGeneration desc{};
    ffxCreateContextDescFrameGenerationVersion version{};
    ffxOverrideVersion overrideVersion{};
    ffxCreateBackendDX12Desc backend{};
    ffxCreateBackendDX12AllocationCallbacksDesc callbacks{}, replacement{};
    ffxAllocationCallbacks cpu{}, creatorCpu{};
    bool defaultCpu;
    Context(Api& a, Lane& l, ID3D12Device* device, bool useDefaultCpu = false, bool replacePair = false)
        : api(a), lane(l), defaultCpu(useDefaultCpu) {
        cpu = {&lane, Lane::alloc, Lane::dealloc}; creatorCpu = cpu;
        backend.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_BACKEND_DX12; backend.device = device;
        version.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_FRAMEGENERATION_VERSION;
        version.version = FFX_FRAMEGENERATION_VERSION;
        overrideVersion.header.type = FFX_API_DESC_TYPE_OVERRIDE_VERSION; overrideVersion.versionId = api.selected;
        desc.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_FRAMEGENERATION;
        desc.displaySize = {kDisplayWidth, kDisplayHeight}; desc.maxRenderSize = {kRenderWidth, kRenderHeight};
        desc.backBufferFormat = FFX_API_SURFACE_FORMAT_R8G8B8A8_UNORM;
        desc.header.pNext = &version.header; version.header.pNext = &overrideVersion.header;
        overrideVersion.header.pNext = &backend.header;
        if (lane.pair) {
            callbackPair(callbacks, replacePair ? 1 : lane.pair);
            backend.header.pNext = &callbacks.header;
            if (replacePair) { callbackPair(replacement, lane.pair); callbacks.header.pNext = &replacement.header; }
        }
    }
    ffxReturnCode_t create() { Current active(lane); return api.create(&value, &desc.header, defaultCpu ? nullptr : &cpu); }
    void destroy() {
        need(value != nullptr, "destroy only fully published context");
        Current active(lane);
        ffx(api.destroy(&value, defaultCpu ? nullptr : &creatorCpu), "destroy successful context");
        // The native public API destroys ownership but does not clear the
        // caller's handle. Neither implementation may be called twice on it.
        value = nullptr;
    }
};
struct Harness {
    Api api;
    ComPtr<ID3D12Device> device;
    ComPtr<ID3D12CommandQueue> queue;
    Device* proxy{};
    std::map<uint64_t, ULONG> initial;
    explicit Harness(const wchar_t* module) : api(module) {
        hr(D3D12CreateDevice(nullptr, D3D_FEATURE_LEVEL_12_0, IID_PPV_ARGS(&device)), "real D3D12 device");
        D3D12_COMMAND_QUEUE_DESC desc{}; desc.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
        hr(device->CreateCommandQueue(&desc, IID_PPV_ARGS(&queue)), "real direct queue");
        api.select(device.Get());
        realDevice = device.Get(); device->AddRef(); proxy = new Device(device.Get()); proxyDevice = proxy;
        initial = com.snapshot();
        for (const auto& iid : {__uuidof(IUnknown), __uuidof(ID3D12Object), __uuidof(ID3D12Device)}) {
            void* nativeValue{}; void* proxyValue{};
            HRESULT nativeResult = device->QueryInterface(iid, &nativeValue);
            HRESULT proxyResult = proxy->QueryInterface(iid, &proxyValue);
            std::printf("DEVICE_QI iid=%08lx native=%08lx forwarded=%08lx\n",
                static_cast<unsigned long>(iid.Data1), static_cast<unsigned long>(nativeResult),
                static_cast<unsigned long>(proxyResult));
            need(nativeResult == proxyResult, "proxy preserves real base-interface QI result");
            if (SUCCEEDED(nativeResult)) {
                need(nativeValue && proxyValue == static_cast<ID3D12Device*>(proxy), "supported QI keeps wrapper identity");
                static_cast<IUnknown*>(nativeValue)->Release();
                static_cast<IUnknown*>(proxyValue)->Release();
            } else {
                // This D3DMetal device rejects even IUnknown. Preserve that
                // observed result instead of inventing support in the probe.
                need(!nativeValue && !proxyValue, "failed native QI preserves null output");
            }
        }
        const GUID impossible{0xe40b6c73, 0x3cbb, 0x47db, {0xa6,0x4a,0x91,0x12,0x07,0x4d,0xc4,0xe9}};
        void* a{}; void* b{};
        HRESULT actual = device->QueryInterface(impossible, &a), forwarded = proxy->QueryInterface(impossible, &b);
        need(actual == forwarded && !a && !b, "unknown QI faithfully rejects unsupported IID");
        com.baseline(initial);
    }
    void clean(Lane& lane) { lane.clean(); gpuClean(); com.baseline(initial); }
    void successful(Lane& lane, bool defaultCpu = false, bool replace = false) {
        Context context(api, lane, proxy, defaultCpu, replace);
        ffx(context.create(), "successful create/recovery"); need(context.value != nullptr, "successful context publication");
        api.identity(&context.value);
        context.destroy();
        clean(lane);
    }
    void finish() {
        gpuClean(); com.baseline(initial);
        need(proxy->Release() == 0, "probe device owner released");
        proxy = nullptr; proxyDevice = nullptr; realDevice = nullptr;
        com.baseline({});
        std::printf("COM_LEDGER retired=%llu live=0 escaped_qi=0\n", static_cast<unsigned long long>(com.retired.size()));
    }
};
struct Shared { uint32_t magic{}, done{}; Counts counts{}; };
constexpr uint32_t kSharedMagic = 0x33525346;

static Counts measure(Harness& harness, unsigned pair) {
    Lane lane; lane.start(pair);
    Context context(harness.api, lane, harness.proxy);
    ffx(context.create(), "baseline construction"); need(context.value != nullptr, "baseline publication");
    need(lane.plan.calls == lane.plan.succeeded, "baseline every observed construction call succeeded");
    Counts counts = lane.plan.succeeded;
    harness.api.identity(&context.value);
    context.destroy(); harness.clean(lane); lane.finish();
    need(counts[static_cast<size_t>(Stage::Cpu)] && counts[static_cast<size_t>(Stage::Descriptor)]
        && counts[static_cast<size_t>(Stage::Root)] && counts[static_cast<size_t>(Stage::Pipeline)], "baseline covers CPU/descriptor/root/PSO construction");
    if (pair) need(counts[static_cast<size_t>(Stage::ResourceCallback)], "custom baseline reached real resource allocator");
    for (size_t i = 0; i < kStages; ++i)
        std::printf("BASELINE profile=%s stage=%s successful_ordinals=%llu\n", pair ? "custom" : "default-gpu", stageName(static_cast<Stage>(i)), static_cast<unsigned long long>(counts[i]));
    return counts;
}
static void failureCase(Harness& harness, unsigned pair, Stage stage, uint64_t ordinal) {
    Lane lane; lane.start(pair); lane.plan.stage = stage; lane.plan.ordinal = ordinal;
    Context context(harness.api, lane, harness.proxy);
    ffxReturnCode_t result = context.create();
    need(lane.plan.hits == 1, "specified baseline failure ordinal reached exactly once");
    need(result != FFX_API_RETURN_OK, "injected construction failure propagated");
    need(context.value == nullptr, "failed create leaves NULL context; never destroy partial handle");
    harness.clean(lane);
    lane.plan = {};
    harness.successful(lane);
    lane.finish();
    std::printf("RECOVERED profile=%s stage=%s ordinal=%llu result=%u null_context=1 exact_owners=1 independent_alive=1 retry=1\n",
        pair ? "custom" : "default-gpu", stageName(stage), static_cast<unsigned long long>(ordinal), result);
}
static void provenanceCase(Harness& harness) {
    Lane a, b; a.start(1); b.start(2);
    Context first(harness.api, a, harness.proxy), second(harness.api, b, harness.proxy);
    ffx(first.create(), "creator A create"); harness.api.identity(&first.value);
    // Changing the caller's registration storage must not change A's saved CPU
    // owner. Destroy still supplies a separately saved compatible callback value.
    first.cpu = {&b, Lane::alloc, Lane::dealloc};
    ffx(second.create(), "later creator B registration/create"); harness.api.identity(&second.value);
    first.destroy(); a.clean(); // B remains genuinely live; do not demand global zero yet.
    second.destroy(); harness.clean(a); harness.clean(b);
    a.finish(); b.finish();
    Lane replacement; replacement.start(2);
    harness.successful(replacement, false, true); replacement.finish();
    std::puts("PROVENANCE_PASS cpu_snapshot=1 resource_pair_snapshot=1 heap_pair_if_reached=1 chained_replacement=1");
}
static void recursiveCase(Harness& harness, const std::wstring& scenario) {
    Lane outer; outer.start(1);
    unsigned nestedCalls{};
    auto nested = [&] {
        ++nestedCalls;
        Lane inner; inner.start(1);
        Context context(harness.api, inner, harness.proxy);
        ffx(context.create(), "recursive public create"); harness.api.identity(&context.value);
        context.destroy(); inner.finish();
    };
    if (scenario == L"cpu-allocate-recursive") outer.onAlloc = nested;
    else if (scenario == L"cpu-deallocate-recursive") outer.onDealloc = nested;
    else if (scenario == L"resource-allocate-recursive") gpuAllocateHook = nested;
    else if (scenario == L"resource-deallocate-recursive") gpuDeallocateHook = nested;
    else fail("recursive scenario name");
    harness.successful(outer);
    need(nestedCalls == 1, "recursive create executed exactly once without callback lock deadlock");
    outer.finish();
    std::printf("RECURSIVE_PASS scenario=%s nested_create_destroy=1\n", utf8(scenario).c_str());
}
struct Creator {
    Harness* harness{};
    unsigned pair{};
    Event ready, created;
    Event* start{};
    Event* retire{};
    HANDLE thread{};
    static DWORD WINAPI entry(void* parameter) {
        auto& self = *static_cast<Creator*>(parameter);
        Lane lane; lane.start(self.pair);
        Context context(self.harness->api, lane, self.harness->proxy);
        self.ready.set(); self.start->wait();
        ffx(context.create(), "concurrent creator");
        self.harness->api.identity(&context.value);
        self.created.set(); self.retire->wait();
        context.destroy(); lane.finish();
        return 0;
    }
};
static void concurrentCase(Harness& harness) {
    Event start, retire;
    std::array<Creator, 8> creators;
    std::array<HANDLE, 8> ready{}, created{}, threads{};
    for (size_t i = 0; i < creators.size(); ++i) {
        auto& creator = creators[i]; creator.harness = &harness; creator.pair = 1 + (i % 2);
        creator.start = &start; creator.retire = &retire;
        ready[i] = creator.ready.value; created[i] = creator.created.value;
        creator.thread = CreateThread(nullptr, 0, Creator::entry, &creator, 0, nullptr);
        need(creator.thread != nullptr, "CreateThread"); threads[i] = creator.thread;
    }
    need(WaitForMultipleObjects(8, ready.data(), TRUE, kWait) == WAIT_OBJECT_0, "eight creators subscribed before release");
    start.set();
    need(WaitForMultipleObjects(8, created.data(), TRUE, kWait) == WAIT_OBJECT_0, "eight simultaneous live contexts");
    retire.set();
    need(WaitForMultipleObjects(8, threads.data(), TRUE, kWait) == WAIT_OBJECT_0, "eight exact thread completions");
    for (HANDLE thread : threads) { DWORD code{}; need(GetExitCodeThread(thread, &code) && code == 0, "creator completion status"); CloseHandle(thread); }
    gpuClean(); com.baseline(harness.initial);
    Lane recovery; recovery.start(1); harness.successful(recovery); recovery.finish();
    std::puts("CONCURRENT_PASS creators=8 simultaneous_live=8 exact_owners=1 recovery=1");
}
static int childCase(const wchar_t* module, const std::wstring& scenario, uint64_t ordinal, unsigned pair, Shared* shared) {
    need(shared && shared->magic == kSharedMagic, "inherited result mapping");
    Harness harness(module);
    if (scenario == L"measure") shared->counts = measure(harness, pair);
    else if (scenario == L"default-cpu") { Lane lane; lane.start(0); harness.successful(lane, true); lane.finish(); }
    else if (scenario == L"provenance") provenanceCase(harness);
    else if (scenario == L"concurrent") concurrentCase(harness);
    else if (scenario.find(L"recursive") != std::wstring::npos) recursiveCase(harness, scenario);
    else {
        Stage stage = Stage::Count;
        for (size_t i = 0; i < kStages; ++i) if (utf8(scenario) == stageName(static_cast<Stage>(i))) stage = static_cast<Stage>(i);
        need(stage != Stage::Count && ordinal, "failure scenario and ordinal");
        failureCase(harness, pair, stage, ordinal);
    }
    harness.finish(); shared->done = 1;
    std::fflush(nullptr);
    return 0;
}
static std::wstring quote(const std::wstring& argument) {
    std::wstring result = L"\""; unsigned slashes{};
    for (wchar_t ch : argument) {
        if (ch == L'\\') { ++slashes; continue; }
        if (ch == L'\"') { result.append(slashes * 2 + 1, L'\\'); result += ch; }
        else { result.append(slashes, L'\\'); result += ch; }
        slashes = 0;
    }
    result.append(slashes * 2, L'\\'); result += L'\"'; return result;
}
static bool spawnCase(const std::wstring& module, const std::wstring& scenario, uint64_t ordinal, unsigned pair, Counts* counts = nullptr) {
    SECURITY_ATTRIBUTES security{sizeof(security), nullptr, TRUE};
    HANDLE mapping = CreateFileMappingW(INVALID_HANDLE_VALUE, &security, PAGE_READWRITE, 0, sizeof(Shared), nullptr);
    need(mapping != nullptr, "CreateFileMapping case result");
    auto* result = static_cast<Shared*>(MapViewOfFile(mapping, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(Shared)));
    need(result != nullptr, "MapViewOfFile case result"); *result = {}; result->magic = kSharedMagic;
    std::wstring executable = modulePath(nullptr);
    std::wstring command = quote(executable) + L" " + quote(module) + L" --case " + quote(scenario)
        + L" " + std::to_wstring(ordinal) + L" " + std::to_wstring(pair) + L" "
        + std::to_wstring(reinterpret_cast<uintptr_t>(mapping));
    STARTUPINFOW startup{}; startup.cb = sizeof(startup);
    PROCESS_INFORMATION process{};
    std::printf("CASE_BEGIN scenario=%s ordinal=%llu pair=%u\n", utf8(scenario).c_str(), static_cast<unsigned long long>(ordinal), pair);
    std::fflush(nullptr);
    need(CreateProcessW(executable.c_str(), command.data(), nullptr, nullptr, TRUE, 0, nullptr, nullptr, &startup, &process), "CreateProcess isolated case");
    DWORD waited = WaitForSingleObject(process.hProcess, kWait);
    if (waited == WAIT_TIMEOUT) {
        need(TerminateProcess(process.hProcess, 124), "terminate owned timed-out diagnostic");
        need(WaitForSingleObject(process.hProcess, kWait) == WAIT_OBJECT_0, "timed-out diagnostic retired");
    } else need(waited == WAIT_OBJECT_0, "case process completion");
    DWORD code{}; need(GetExitCodeProcess(process.hProcess, &code), "GetExitCodeProcess");
    bool ok = waited == WAIT_OBJECT_0 && code == 0 && result->done == 1;
    if (ok && counts) *counts = result->counts;
    std::printf("CASE_END scenario=%s ordinal=%llu pair=%u status=%s exit=%lu completed=%u\n", utf8(scenario).c_str(),
        static_cast<unsigned long long>(ordinal), pair, ok ? "PASS" : "FAIL", code, result->done);
    CloseHandle(process.hThread); CloseHandle(process.hProcess); UnmapViewOfFile(result); CloseHandle(mapping);
    return ok;
}
static int failureSweep(const std::wstring& module) {
    unsigned failed{}, cases{};
    std::array<Counts, 2> baselines{};
    for (unsigned profile = 0; profile < baselines.size(); ++profile) {
        ++cases;
        if (!spawnCase(module, L"measure", 0, profile, &baselines[profile])) {
            std::fprintf(stderr, "BLOCKED no successful observed baseline for profile=%u; ordinals cannot be guessed\n", profile);
            return 2;
        }
    }
    for (unsigned profile = 0; profile < baselines.size(); ++profile) {
        for (size_t stage = 0; stage < kStages; ++stage) {
            uint64_t count = baselines[profile][stage];
            if (!count) {
                std::printf("NOT_REACHED profile=%u stage=%s public_create_successful_calls=0\n", profile, stageName(static_cast<Stage>(stage)));
                continue;
            }
            std::string name = stageName(static_cast<Stage>(stage));
            std::wstring wide(name.begin(), name.end());
            for (uint64_t ordinal = 1; ordinal <= count; ++ordinal) { ++cases; if (!spawnCase(module, wide, ordinal, profile)) ++failed; }
        }
    }
    for (const auto* name : {L"default-cpu", L"provenance", L"cpu-allocate-recursive", L"cpu-deallocate-recursive",
                             L"resource-allocate-recursive", L"resource-deallocate-recursive", L"concurrent"}) {
        ++cases; if (!spawnCase(module, name, 0, 1)) ++failed;
    }
    std::printf("FSR3_FAILURE_SWEEP_%s cases=%u failures=%u\n", failed ? "FAIL" : "PASS", cases, failed);
    return failed ? 1 : 0;
}

static void transition(ID3D12GraphicsCommandList* list, ID3D12Resource* resource, D3D12_RESOURCE_STATES before, D3D12_RESOURCE_STATES after) {
    D3D12_RESOURCE_BARRIER barrier{}; barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    barrier.Transition = {resource, D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES, before, after};
    list->ResourceBarrier(1, &barrier);
}
static D3D12_RESOURCE_DESC bufferDescription(UINT64 size) {
    D3D12_RESOURCE_DESC desc{}; desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    desc.Width = size; desc.Height = 1; desc.DepthOrArraySize = 1; desc.MipLevels = 1;
    desc.SampleDesc.Count = 1; desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR; return desc;
}
static ComPtr<ID3D12Resource> buffer(ID3D12Device* device, UINT64 size, D3D12_HEAP_TYPE type, D3D12_RESOURCE_STATES state) {
    D3D12_HEAP_PROPERTIES properties{}; properties.Type = type;
    auto desc = bufferDescription(size); ComPtr<ID3D12Resource> result;
    hr(device->CreateCommittedResource(&properties, D3D12_HEAP_FLAG_NONE, &desc, state, nullptr, IID_PPV_ARGS(&result)), "render buffer creation");
    return result;
}
struct Texture {
    ComPtr<ID3D12Resource> resource, upload;
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT footprint{};
    UINT64 bytes{};
    UINT width{}, height{}, pixelBytes{};
};
static Texture texture(ID3D12Device* device, UINT width, UINT height, DXGI_FORMAT format, UINT pixelBytes, bool output) {
    Texture result; result.width = width; result.height = height; result.pixelBytes = pixelBytes;
    D3D12_RESOURCE_DESC desc{}; desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    desc.Width = width; desc.Height = height; desc.DepthOrArraySize = 1; desc.MipLevels = 1;
    desc.Format = format; desc.SampleDesc.Count = 1;
    desc.Flags = output ? D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS : D3D12_RESOURCE_FLAG_NONE;
    D3D12_HEAP_PROPERTIES properties{}; properties.Type = D3D12_HEAP_TYPE_DEFAULT;
    hr(device->CreateCommittedResource(&properties, D3D12_HEAP_FLAG_NONE, &desc, D3D12_RESOURCE_STATE_COPY_DEST, nullptr,
                                      IID_PPV_ARGS(&result.resource)), "render texture creation");
    device->GetCopyableFootprints(&desc, 0, 1, 0, &result.footprint, nullptr, nullptr, &result.bytes);
    result.upload = buffer(device, result.bytes, D3D12_HEAP_TYPE_UPLOAD, D3D12_RESOURCE_STATE_GENERIC_READ);
    return result;
}
static void uploadTexture(ID3D12GraphicsCommandList* list, Texture& texture, const std::function<void(unsigned char*, UINT, UINT)>& pixel) {
    void* memory{}; D3D12_RANGE none{0, 0}; hr(texture.upload->Map(0, &none, &memory), "render upload Map");
    auto* bytes = static_cast<unsigned char*>(memory);
    std::memset(bytes, 0, static_cast<size_t>(texture.bytes));
    for (UINT y = 0; y < texture.height; ++y) for (UINT x = 0; x < texture.width; ++x)
        pixel(bytes + texture.footprint.Offset + y * texture.footprint.Footprint.RowPitch + x * texture.pixelBytes, x, y);
    D3D12_RANGE written{0, static_cast<SIZE_T>(texture.bytes)}; texture.upload->Unmap(0, &written);
    D3D12_TEXTURE_COPY_LOCATION src{}, dst{};
    src.pResource = texture.upload.Get(); src.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT; src.PlacedFootprint = texture.footprint;
    dst.pResource = texture.resource.Get(); dst.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    list->CopyTextureRegion(&dst, 0, 0, 0, &src, nullptr);
}
static uint64_t hashPixels(const unsigned char* bytes, const Texture& texture) {
    uint64_t hash = UINT64_C(14695981039346656037);
    for (UINT y = 0; y < texture.height; ++y) for (UINT x = 0; x < texture.width * texture.pixelBytes; ++x) {
        hash ^= bytes[texture.footprint.Offset + y * texture.footprint.Footprint.RowPitch + x];
        hash *= UINT64_C(1099511628211);
    }
    return hash;
}
static int render(const wchar_t* module) {
    // Fresh process, real unwrapped device and default GPU allocation. The
    // construction-only transparent wrappers never escape to a command list.
    Api api(module); ComPtr<ID3D12Device> device;
    hr(D3D12CreateDevice(nullptr, D3D_FEATURE_LEVEL_12_0, IID_PPV_ARGS(&device)), "render real device");
    api.select(device.Get());
    D3D12_COMMAND_QUEUE_DESC queueDesc{}; queueDesc.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
    ComPtr<ID3D12CommandQueue> queue; hr(device->CreateCommandQueue(&queueDesc, IID_PPV_ARGS(&queue)), "render direct queue");
    ComPtr<ID3D12CommandAllocator> allocator; ComPtr<ID3D12GraphicsCommandList> list;
    hr(device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&allocator)), "render allocator");
    hr(device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, allocator.Get(), nullptr, IID_PPV_ARGS(&list)), "render command list");
    hr(list->Close(), "initial render close");
    ComPtr<ID3D12Fence> fence; hr(device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&fence)), "render fence");
    Event completed(false);
    auto color = texture(device.Get(), kDisplayWidth, kDisplayHeight, DXGI_FORMAT_R8G8B8A8_UNORM, 4, false);
    auto depth = texture(device.Get(), kRenderWidth, kRenderHeight, DXGI_FORMAT_R32_FLOAT, 4, false);
    auto motion = texture(device.Get(), kRenderWidth, kRenderHeight, DXGI_FORMAT_R32G32_FLOAT, 8, false);
    auto output = texture(device.Get(), kDisplayWidth, kDisplayHeight, DXGI_FORMAT_R8G8B8A8_UNORM, 4, true);
    auto readback = buffer(device.Get(), output.bytes, D3D12_HEAP_TYPE_READBACK, D3D12_RESOURCE_STATE_COPY_DEST);
    std::array<uint64_t, 6> referenceHashes{};
    for (unsigned control = 0; control < 2; ++control) {
    const bool legacy = control != 0;
    Lane lane; lane.start(0);
    Context context(api, lane, device.Get()); ffx(context.create(), "render public create"); api.identity(&context.value);
    FfxApiEffectMemoryUsage usage{};
    ffxQueryDescFrameGenerationGetGPUMemoryUsage memory{};
    memory.header.type = FFX_API_QUERY_DESC_TYPE_FRAMEGENERATION_GPU_MEMORY_USAGE;
    memory.gpuMemoryUsageFrameGeneration = &usage;
    ffx(api.query(&context.value, &memory.header), "render public GPU memory query");
    need(usage.totalUsageInBytes != 0 && usage.aliasableUsageInBytes <= usage.totalUsageInBytes, "render GPU memory query sane");
    std::printf("GPU_MEMORY total=%llu aliasable=%llu\n", static_cast<unsigned long long>(usage.totalUsageInBytes), static_cast<unsigned long long>(usage.aliasableUsageInBytes));
    std::array<uint64_t, 6> hashes{};
    unsigned temporallyGenerated{};
    for (UINT frame = 0; frame < hashes.size(); ++frame) {
        hr(allocator->Reset(), "render allocator reset after exact preceding fence");
        hr(list->Reset(allocator.Get(), nullptr), "render list reset");
        if (frame || legacy) {
            for (auto* input : {color.resource.Get(), depth.resource.Get(), motion.resource.Get()})
                transition(list.Get(), input, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_COPY_DEST);
            transition(list.Get(), output.resource.Get(), D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_DEST);
        }
        // Spatially nonconstant translated scene with matching normalized
        // previous-frame motion. This exercises OF/FI history, not SR/MetalFX.
        uploadTexture(list.Get(), color, [frame](unsigned char* p, UINT x, UINT y) {
            UINT shifted = (x + kDisplayWidth - frame * 4) % kDisplayWidth;
            bool square = shifted >= 48 && shifted < 112 && y >= 40 && y < 104;
            p[0] = static_cast<unsigned char>(square ? 224 : 24 + shifted * 128 / kDisplayWidth);
            p[1] = static_cast<unsigned char>(square ? 48 : 32 + y * 160 / kDisplayHeight);
            p[2] = static_cast<unsigned char>(48 + ((shifted / 16 + y / 16) & 1) * 128); p[3] = 255;
        });
        uploadTexture(list.Get(), depth, [](unsigned char* p, UINT x, UINT y) {
            float z = .2f + .6f * static_cast<float>(x + y) / static_cast<float>(kRenderWidth + kRenderHeight);
            std::memcpy(p, &z, sizeof(z));
        });
        uploadTexture(list.Get(), motion, [frame](unsigned char* p, UINT, UINT) {
            const float motion[2]{frame ? -4.f / kDisplayWidth : 0.f, 0.f};
            std::memcpy(p, motion, sizeof(motion));
        });
        // Poison changes per frame; accepting unchanged/all-poison data would
        // turn a dropped dispatch into a false positive.
        const unsigned char poison = static_cast<unsigned char>(0xd0 + frame);
        uploadTexture(list.Get(), output, [poison](unsigned char* p, UINT, UINT) { std::memset(p, poison, 4); });
        for (auto* input : {color.resource.Get(), depth.resource.Get(), motion.resource.Get()})
            transition(list.Get(), input, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
        transition(list.Get(), output.resource.Get(), D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        ffxConfigureDescFrameGeneration config{}; config.header.type = FFX_API_CONFIGURE_DESC_TYPE_FRAMEGENERATION;
        config.frameGenerationEnabled = true; config.flags = FFX_FRAMEGENERATION_FLAG_NO_SWAPCHAIN_CONTEXT_NOTIFY;
        config.generationRect = {0, 0, int(kDisplayWidth), int(kDisplayHeight)}; config.frameID = frame + 1;
        ffx(api.configure(&context.value, &config.header), "render public configure");
        ffxDispatchDescFrameGenerationPrepareV2 prepare{}; prepare.header.type = FFX_API_DISPATCH_DESC_TYPE_FRAMEGENERATION_PREPARE_V2;
        prepare.commandList = list.Get(); prepare.frameID = frame + 1; prepare.renderSize = {kRenderWidth, kRenderHeight};
        prepare.motionVectorScale = {float(kRenderWidth), float(kRenderHeight)};
        prepare.frameTimeDelta = 1000.f / 60.f; prepare.reset = frame == 0;
        prepare.cameraNear = .1f; prepare.cameraFar = 100.f; prepare.cameraFovAngleVertical = 1.f; prepare.viewSpaceToMetersFactor = 1.f;
        prepare.depth = ffxApiGetResourceDX12(depth.resource.Get()); prepare.motionVectors = ffxApiGetResourceDX12(motion.resource.Get());
        prepare.cameraUp[1] = 1; prepare.cameraRight[0] = 1; prepare.cameraForward[2] = 1;
        if (legacy) {
            // Use the official old packet's byte layout without naming a
            // deprecated C++ convenience type or suppressing its diagnostics.
            struct CameraPacket {
                ffxApiHeader header;
                float position[3], up[3], right[3], forward[3];
            } camera{};
            static_assert(offsetof(CameraPacket, position) == sizeof(ffxApiHeader));
            static_assert(sizeof(CameraPacket) == sizeof(ffxApiHeader) + 12 * sizeof(float));
            camera.header.type = FFX_API_DISPATCH_DESC_TYPE_FRAMEGENERATION_PREPARE_CAMERAINFO;
            std::memcpy(camera.position, prepare.cameraPosition, sizeof(camera.position));
            std::memcpy(camera.up, prepare.cameraUp, sizeof(camera.up));
            std::memcpy(camera.right, prepare.cameraRight, sizeof(camera.right));
            std::memcpy(camera.forward, prepare.cameraForward, sizeof(camera.forward));
            alignas(ffxApiHeader) std::array<unsigned char, offsetof(ffxDispatchDescFrameGenerationPrepareV2, cameraPosition)> packet{};
            std::memcpy(packet.data(), &prepare, packet.size());
            auto* header = reinterpret_cast<ffxDispatchDescHeader*>(packet.data());
            header->type = FFX_API_DISPATCH_DESC_TYPE_FRAMEGENERATION_PREPARE;
            header->pNext = &camera.header;
            ffx(api.dispatch(&context.value, header), "render public legacy Prepare+CameraInfo");
        } else {
            ffx(api.dispatch(&context.value, &prepare.header), "render public PrepareV2");
        }
        ffxDispatchDescFrameGeneration generate{}; generate.header.type = FFX_API_DISPATCH_DESC_TYPE_FRAMEGENERATION;
        generate.commandList = list.Get(); generate.frameID = frame + 1; generate.numGeneratedFrames = 1; generate.reset = frame == 0;
        generate.presentColor = ffxApiGetResourceDX12(color.resource.Get());
        generate.outputs[0] = ffxApiGetResourceDX12(output.resource.Get(), FFX_API_RESOURCE_STATE_UNORDERED_ACCESS);
        generate.backbufferTransferFunction = FFX_API_BACKBUFFER_TRANSFER_FUNCTION_SRGB; generate.minMaxLuminance[1] = 100;
        generate.generationRect = config.generationRect;
        ffx(api.dispatch(&context.value, &generate.header), "render public Dispatch");
        transition(list.Get(), output.resource.Get(), D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE);
        D3D12_TEXTURE_COPY_LOCATION src{}, dst{};
        src.pResource = output.resource.Get(); src.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
        dst.pResource = readback.Get(); dst.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT; dst.PlacedFootprint = output.footprint;
        list->CopyTextureRegion(&dst, 0, 0, 0, &src, nullptr);
        transition(list.Get(), output.resource.Get(), D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        hr(list->Close(), "render close");
        UINT64 value = control * hashes.size() + frame + 1;
        hr(fence->SetEventOnCompletion(value, completed.value), "subscribe render completion before submit");
        ID3D12CommandList* submitted[]{list.Get()}; queue->ExecuteCommandLists(1, submitted);
        hr(queue->Signal(fence.Get(), value), "render queue signal"); completed.wait();
        need(fence->GetCompletedValue() >= value, "exact submitted frame completed");
        hr(device->GetDeviceRemovedReason(), "render device status");
        void* mapped{}; D3D12_RANGE range{0, static_cast<SIZE_T>(output.bytes)};
        hr(readback->Map(0, &range, &mapped), "render readback Map");
        const auto* bytes = static_cast<const unsigned char*>(mapped);
        hashes[frame] = hashPixels(bytes, output);
        void* inputMapped{}; D3D12_RANGE inputRange{0, static_cast<SIZE_T>(color.bytes)};
        hr(color.upload->Map(0, &inputRange, &inputMapped), "source-pattern hash Map after frame fence");
        const uint64_t inputHash = hashPixels(static_cast<const unsigned char*>(inputMapped), color);
        uint64_t interpolatedRgb{};
        for (UINT y = 0; y < kDisplayHeight; ++y) for (UINT x = 0; x < kDisplayWidth; ++x) {
            const auto* generated = bytes + output.footprint.Offset + y * output.footprint.Footprint.RowPitch + x * 4;
            const auto* source = static_cast<const unsigned char*>(inputMapped) +
                color.footprint.Offset + y * color.footprint.Footprint.RowPitch + x * 4;
            interpolatedRgb += std::memcmp(generated, source, 3) != 0;
        }
        D3D12_RANGE inputUnwritten{0, 0}; color.upload->Unmap(0, &inputUnwritten);
        if (frame && interpolatedRgb) ++temporallyGenerated;
        uint64_t changed{}, alpha255{}; unsigned low = 255, high = 0;
        for (UINT y = 0; y < kDisplayHeight; ++y) for (UINT x = 0; x < kDisplayWidth; ++x) {
            const auto* p = bytes + output.footprint.Offset + y * output.footprint.Footprint.RowPitch + x * 4;
            changed += p[0] != poison || p[1] != poison || p[2] != poison || p[3] != poison;
            alpha255 += p[3] == 255; low = std::min(low, unsigned(p[0])); high = std::max(high, unsigned(p[0]));
        }
        const auto* center = bytes + output.footprint.Offset + (kDisplayHeight / 2) * output.footprint.Footprint.RowPitch + (kDisplayWidth / 2) * 4;
        std::printf("FRAME api=%s frame=%u hash_fnv1a64=%016llx source_hash=%016llx changed=%llu alpha255=%llu r_min=%u r_max=%u center=%u,%u,%u,%u\n",
            legacy ? "legacy" : "v2", frame + 1, static_cast<unsigned long long>(hashes[frame]), static_cast<unsigned long long>(inputHash), static_cast<unsigned long long>(changed),
            static_cast<unsigned long long>(alpha255), low, high, center[0], center[1], center[2], center[3]);
        std::printf("INTERPOLATED_RGB api=%s frame=%u pixels=%llu\n", legacy ? "legacy" : "v2",
            frame + 1, static_cast<unsigned long long>(interpolatedRgb));
        need(changed == uint64_t(kDisplayWidth) * kDisplayHeight && high > low + 32, "generated output overwrote poison with spatial scene");
        D3D12_RANGE none{0, 0}; readback->Unmap(0, &none);
    }
    need(std::adjacent_find(hashes.begin(), hashes.end(), std::not_equal_to<uint64_t>()) != hashes.end(), "generated scene varies across frames");
    need(temporallyGenerated != 0, "nonreset generated frames differ from current input; not copy-only echo");
    // Every dispatch is retired before disabling, destroying, or freeing upload
    // memory. No callback is installed, so there is no pending present callback.
    ffxConfigureDescFrameGeneration disable{}; disable.header.type = FFX_API_CONFIGURE_DESC_TYPE_FRAMEGENERATION;
    disable.flags = FFX_FRAMEGENERATION_FLAG_NO_SWAPCHAIN_CONTEXT_NOTIFY; disable.frameID = hashes.size() + 1;
    ffx(api.configure(&context.value, &disable.header), "render disable after fence");
    context.destroy(); lane.finish();
    if (legacy) need(hashes == referenceHashes, "legacy Prepare+CameraInfo pixels equal equivalent V2 control");
    else referenceHashes = hashes;
    }
    std::puts("FSR3_RENDER_PASS frames=12 queried_provider=fsr3-3.1.6 gpu_fences=12 cpu_live=0 legacy_v2_equal=1");
    return 0;
}
int wmain(int argc, wchar_t** argv) {
    std::setvbuf(stdout, nullptr, _IONBF, 0);
    need(argc >= 3, "usage: fsr3-provider-probe.exe <module> --failure-sweep|--render|--failure-at <stage> <ordinal> <profile>");
    std::vector<wchar_t> full(32768);
    DWORD count = GetFullPathNameW(argv[1], static_cast<DWORD>(full.size()), full.data(), nullptr);
    need(count && count < full.size(), "absolute selected module path");
    const std::wstring module(full.data(), count);
    std::printf("REQUEST module=%s\n", utf8(module).c_str());
    try {
        if (argc == 3 && std::wcscmp(argv[2], L"--failure-sweep") == 0) return failureSweep(module);
        if (argc == 3 && std::wcscmp(argv[2], L"--render") == 0) return render(module.c_str());
        if (argc == 6 && std::wcscmp(argv[2], L"--failure-at") == 0) {
            uint64_t ordinal = std::wcstoull(argv[4], nullptr, 10);
            unsigned pair = static_cast<unsigned>(std::wcstoul(argv[5], nullptr, 10));
            need(pair <= 1 && ordinal, "failure profile and ordinal");
            Stage stage = Stage::Count;
            for (size_t i = 0; i < kStages; ++i)
                if (utf8(argv[3]) == stageName(static_cast<Stage>(i))) stage = static_cast<Stage>(i);
            need(stage != Stage::Count, "failure stage");
            Counts baseline{};
            if (!spawnCase(module, L"measure", 0, pair, &baseline)) return 2;
            need(ordinal <= baseline[static_cast<size_t>(stage)], "requested failure ordinal observed in successful baseline");
            return spawnCase(module, argv[3], ordinal, pair) ? 0 : 1;
        }
        if (argc == 7 && std::wcscmp(argv[2], L"--case") == 0) {
            uint64_t ordinal = std::wcstoull(argv[4], nullptr, 10);
            unsigned pair = static_cast<unsigned>(std::wcstoul(argv[5], nullptr, 10)); need(pair <= 2, "case profile");
            HANDLE mapping = reinterpret_cast<HANDLE>(static_cast<uintptr_t>(std::wcstoull(argv[6], nullptr, 10)));
            auto* shared = static_cast<Shared*>(MapViewOfFile(mapping, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(Shared)));
            need(shared != nullptr, "child result mapping");
            int result = childCase(module.c_str(), argv[3], ordinal, pair, shared);
            UnmapViewOfFile(shared); CloseHandle(mapping); return result;
        }
        fail("usage: fsr3-provider-probe.exe <module> --failure-sweep|--render|--failure-at <stage> <ordinal> <profile>");
    } catch (const std::exception& error) {
        std::fprintf(stderr, "UNCAUGHT_EXCEPTION %s\n", error.what()); return 1;
    } catch (...) { std::fputs("UNCAUGHT_EXCEPTION nonstandard\n", stderr); return 1; }
}
