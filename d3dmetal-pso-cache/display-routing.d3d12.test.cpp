#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_4.h>
#include <wrl/client.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <optional>
#include <stdexcept>
#include <vector>

using Microsoft::WRL::ComPtr;

namespace {

constexpr UINT kSize = 256;

void require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

void check(HRESULT result, const char* message) {
    if (FAILED(result)) {
        std::fprintf(stderr, "DISPLAY_ROUTING_HRESULT operation=%s value=%08lx\n",
                     message, static_cast<unsigned long>(result));
        throw std::runtime_error(message);
    }
}

struct Display {
    HMONITOR monitor;
    MONITORINFOEXW info;
    DEVMODEW current;
};

Display displayFor(HMONITOR monitor) {
    Display result{};
    result.monitor = monitor;
    result.info.cbSize = sizeof(result.info);
    result.current.dmSize = sizeof(result.current);
    require(GetMonitorInfoW(monitor, &result.info), "GetMonitorInfoW");
    require(EnumDisplaySettingsW(result.info.szDevice, ENUM_CURRENT_SETTINGS, &result.current),
            "EnumDisplaySettingsW(CURRENT)");
    require(result.current.dmDisplayFrequency != 0, "current refresh unavailable");
    return result;
}

std::vector<Display> displays() {
    std::vector<Display> result;
    require(EnumDisplayMonitors(nullptr, nullptr,
            [](HMONITOR monitor, HDC, LPRECT, LPARAM context) -> BOOL {
                auto* output = reinterpret_cast<std::vector<Display>*>(context);
                output->push_back(displayFor(monitor));
                return TRUE;
            }, reinterpret_cast<LPARAM>(&result)), "EnumDisplayMonitors");
    require(!result.empty(), "no displays");
    return result;
}

class SavedMode final {
public:
    explicit SavedMode(const Display& display) : device_(display.info.szDevice) {
        original_.dmSize = sizeof(original_);
        require(EnumDisplaySettingsW(device_, ENUM_REGISTRY_SETTINGS, &original_),
                "EnumDisplaySettingsW(REGISTRY)");
        const auto& current = display.current;
        DEVMODEW alternate{};
        for (DWORD index = 0; ; ++index) {
            DEVMODEW candidate{};
            candidate.dmSize = sizeof(candidate);
            if (!EnumDisplaySettingsW(device_, index, &candidate)) break;
            if (candidate.dmPelsWidth != current.dmPelsWidth ||
                candidate.dmPelsHeight != current.dmPelsHeight ||
                candidate.dmDisplayFrequency == 0 ||
                candidate.dmDisplayFrequency == current.dmDisplayFrequency) continue;
            if (!alternate.dmSize || (current.dmDisplayFrequency == 120 &&
                candidate.dmDisplayFrequency == 60)) alternate = candidate;
            if (current.dmDisplayFrequency == 120 && alternate.dmDisplayFrequency == 60) break;
        }
        if (!alternate.dmSize) {
            std::puts("DISPLAY_ROUTING_SAVED_MODE_SKIP no same-resolution alternate refresh");
            return;
        }
        const LONG status = ChangeDisplaySettingsExW(device_, &alternate, nullptr,
                                                       CDS_UPDATEREGISTRY | CDS_NORESET, nullptr);
        require(status == DISP_CHANGE_SUCCESSFUL, "saved-only ChangeDisplaySettingsExW");
        active_ = true;
        DEVMODEW actual{};
        actual.dmSize = sizeof(actual);
        require(EnumDisplaySettingsW(device_, ENUM_REGISTRY_SETTINGS, &actual),
                "saved mode query");
        require(actual.dmDisplayFrequency == alternate.dmDisplayFrequency,
                "saved refresh did not change");
        const auto live = displayFor(display.monitor);
        require(live.current.dmDisplayFrequency == current.dmDisplayFrequency &&
                live.current.dmPelsWidth == current.dmPelsWidth &&
                live.current.dmPelsHeight == current.dmPelsHeight,
                "current display changed by saved-only update");
        std::printf("DISPLAY_ROUTING_SAVED_SPLIT current=%lu registry=%lu\n",
                    live.current.dmDisplayFrequency, actual.dmDisplayFrequency);
    }

    ~SavedMode() { restore(); }
    SavedMode(const SavedMode&) = delete;
    SavedMode& operator=(const SavedMode&) = delete;
    bool active() const { return active_; }
    bool restored() const { return restored_; }
    void restore() {
        if (!active_) return;
        const LONG status = ChangeDisplaySettingsExW(device_, &original_, nullptr,
                                                       CDS_UPDATEREGISTRY | CDS_NORESET, nullptr);
        restored_ = status == DISP_CHANGE_SUCCESSFUL;
        std::printf("DISPLAY_ROUTING_SAVED_RESTORE status=%ld\n", status);
        active_ = false;
    }

private:
    const WCHAR* device_;
    DEVMODEW original_{};
    bool active_ = false;
    bool restored_ = false;
};

LRESULT CALLBACK windowProc(HWND window, UINT message, WPARAM wparam, LPARAM lparam) {
    return DefWindowProcW(window, message, wparam, lparam);
}

class Window final {
public:
    explicit Window(const Display& display) {
        WNDCLASSW type{};
        type.lpfnWndProc = windowProc;
        type.hInstance = GetModuleHandleW(nullptr);
        type.lpszClassName = L"YaaglDisplayRoutingTest";
        require(RegisterClassW(&type) != 0, "RegisterClassW");
        handle = CreateWindowExW(0, type.lpszClassName, L"D3DMetal display routing",
                                 WS_OVERLAPPEDWINDOW | WS_VISIBLE,
                                 display.info.rcMonitor.left + 24,
                                 display.info.rcMonitor.top + 24, kSize, kSize,
                                 nullptr, nullptr, type.hInstance, nullptr);
        require(handle != nullptr, "CreateWindowExW");
        ShowWindow(handle, SW_SHOW);
        UpdateWindow(handle);
    }
    ~Window() { if (handle) DestroyWindow(handle); }
    Window(const Window&) = delete;
    Window& operator=(const Window&) = delete;
    HWND handle = nullptr;
};

void pump() {
    MSG message{};
    while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) {
        TranslateMessage(&message);
        DispatchMessageW(&message);
    }
}

struct Graphics {
    ComPtr<ID3D12Device> device;
    ComPtr<ID3D12CommandQueue> queue;
    ComPtr<ID3D12CommandAllocator> allocator;
    ComPtr<ID3D12GraphicsCommandList> list;
    ComPtr<ID3D12Fence> fence;
    HANDLE event = nullptr;
    UINT64 serial = 0;

    Graphics() {
        check(D3D12CreateDevice(nullptr, D3D_FEATURE_LEVEL_12_0, IID_PPV_ARGS(&device)),
              "D3D12CreateDevice");
        D3D12_COMMAND_QUEUE_DESC description{};
        description.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
        check(device->CreateCommandQueue(&description, IID_PPV_ARGS(&queue)),
              "CreateCommandQueue");
        check(device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT,
                                              IID_PPV_ARGS(&allocator)), "CreateCommandAllocator");
        check(device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, allocator.Get(),
                                         nullptr, IID_PPV_ARGS(&list)), "CreateCommandList");
        check(list->Close(), "initial Close");
        check(device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&fence)), "CreateFence");
        event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
        require(event != nullptr, "CreateEventW");
    }
    ~Graphics() { if (event) CloseHandle(event); }

    void execute() {
        check(list->Close(), "Close command list");
        ID3D12CommandList* lists[] = {list.Get()};
        queue->ExecuteCommandLists(1, lists);
        check(queue->Signal(fence.Get(), ++serial), "queue Signal");
        if (fence->GetCompletedValue() < serial) {
            check(fence->SetEventOnCompletion(serial, event), "SetEventOnCompletion");
            require(WaitForSingleObject(event, 30000) == WAIT_OBJECT_0, "GPU fence timeout");
        }
    }
};

ComPtr<IDXGISwapChain3> createSwapchain(IDXGIFactory4* factory, Graphics& graphics, HWND window) {
    DXGI_SWAP_CHAIN_DESC1 description{};
    description.Width = kSize;
    description.Height = kSize;
    description.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    description.SampleDesc.Count = 1;
    description.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    description.BufferCount = 2;
    description.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
    ComPtr<IDXGISwapChain1> base;
    check(factory->CreateSwapChainForHwnd(graphics.queue.Get(), window, &description,
                                          nullptr, nullptr, &base), "CreateSwapChainForHwnd");
    ComPtr<IDXGISwapChain3> result;
    check(base.As(&result), "IDXGISwapChain3");
    DXGI_SWAP_CHAIN_DESC1 actual{};
    check(result->GetDesc1(&actual), "GetDesc1");
    require(!(actual.Flags & DXGI_SWAP_CHAIN_FLAG_ALLOW_MODE_SWITCH),
            "fullscreen regression must not switch physical display modes");
    return result;
}

void checkOutput(IDXGISwapChain3* chain, const Display& expected, const char* stage) {
    ComPtr<IDXGIOutput> output;
    check(chain->GetContainingOutput(&output), "GetContainingOutput");
    DXGI_OUTPUT_DESC outputDescription{};
    check(output->GetDesc(&outputDescription), "IDXGIOutput::GetDesc");
    DXGI_SWAP_CHAIN_DESC chainDescription{};
    check(chain->GetDesc(&chainDescription), "IDXGISwapChain::GetDesc");
    std::printf("DISPLAY_ROUTING_OUTPUT stage=%s device=%ls expected=%ls actual_hz=%u expected_hz=%lu\n",
                stage, outputDescription.DeviceName, expected.info.szDevice,
                chainDescription.BufferDesc.RefreshRate.Numerator,
                expected.current.dmDisplayFrequency);
    require(outputDescription.Monitor == expected.monitor &&
            wcscmp(outputDescription.DeviceName, expected.info.szDevice) == 0,
            "swapchain selected wrong monitor");
    require(chainDescription.BufferDesc.RefreshRate.Denominator == 1 &&
            chainDescription.BufferDesc.RefreshRate.Numerator ==
                expected.current.dmDisplayFrequency,
            "swapchain refresh is not current monitor refresh");
}

void renderAndPresent(Graphics& graphics, IDXGISwapChain3* chain, UINT interval) {
    ComPtr<ID3D12Resource> backbuffer;
    check(chain->GetBuffer(chain->GetCurrentBackBufferIndex(), IID_PPV_ARGS(&backbuffer)),
          "GetBuffer");
    D3D12_RESOURCE_DESC image = backbuffer->GetDesc();
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT footprint{};
    UINT64 bytes = 0;
    graphics.device->GetCopyableFootprints(&image, 0, 1, 0, &footprint, nullptr, nullptr, &bytes);
    D3D12_HEAP_PROPERTIES properties{};
    properties.Type = D3D12_HEAP_TYPE_READBACK;
    D3D12_RESOURCE_DESC buffer{};
    buffer.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    buffer.Width = bytes;
    buffer.Height = 1;
    buffer.DepthOrArraySize = 1;
    buffer.MipLevels = 1;
    buffer.SampleDesc.Count = 1;
    buffer.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    ComPtr<ID3D12Resource> readback;
    check(graphics.device->CreateCommittedResource(&properties, D3D12_HEAP_FLAG_NONE, &buffer,
               D3D12_RESOURCE_STATE_COPY_DEST, nullptr, IID_PPV_ARGS(&readback)),
          "CreateCommittedResource(readback)");
    check(graphics.allocator->Reset(), "allocator Reset");
    check(graphics.list->Reset(graphics.allocator.Get(), nullptr), "list Reset");
    auto transition = [&](D3D12_RESOURCE_STATES before, D3D12_RESOURCE_STATES after) {
        D3D12_RESOURCE_BARRIER barrier{};
        barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
        barrier.Transition.pResource = backbuffer.Get();
        barrier.Transition.StateBefore = before;
        barrier.Transition.StateAfter = after;
        barrier.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
        graphics.list->ResourceBarrier(1, &barrier);
    };
    transition(D3D12_RESOURCE_STATE_PRESENT, D3D12_RESOURCE_STATE_RENDER_TARGET);
    D3D12_DESCRIPTOR_HEAP_DESC heapDescription{};
    heapDescription.Type = D3D12_DESCRIPTOR_HEAP_TYPE_RTV;
    heapDescription.NumDescriptors = 1;
    ComPtr<ID3D12DescriptorHeap> heap;
    check(graphics.device->CreateDescriptorHeap(&heapDescription, IID_PPV_ARGS(&heap)),
          "CreateDescriptorHeap");
    graphics.device->CreateRenderTargetView(backbuffer.Get(), nullptr,
                                            heap->GetCPUDescriptorHandleForHeapStart());
    const float clear[] = {0.25f, 0.5f, 0.75f, 1.0f};
    graphics.list->ClearRenderTargetView(heap->GetCPUDescriptorHandleForHeapStart(), clear, 0, nullptr);
    transition(D3D12_RESOURCE_STATE_RENDER_TARGET, D3D12_RESOURCE_STATE_COPY_SOURCE);
    D3D12_TEXTURE_COPY_LOCATION source{};
    source.pResource = backbuffer.Get();
    source.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    D3D12_TEXTURE_COPY_LOCATION destination{};
    destination.pResource = readback.Get();
    destination.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    destination.PlacedFootprint = footprint;
    graphics.list->CopyTextureRegion(&destination, 0, 0, 0, &source, nullptr);
    transition(D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_PRESENT);
    graphics.execute();
    const std::array<unsigned char, 4> expected{64, 128, 191, 255};
    void* mapping = nullptr;
    D3D12_RANGE range{0, static_cast<SIZE_T>(bytes)};
    check(readback->Map(0, &range, &mapping), "Map readback");
    const auto* pixel = static_cast<const unsigned char*>(mapping) + footprint.Offset +
                        (kSize / 2) * footprint.Footprint.RowPitch + (kSize / 2) * 4;
    bool valid = true;
    for (size_t channel = 0; channel < expected.size(); ++channel)
        valid &= std::abs(int(pixel[channel]) - int(expected[channel])) <= 2;
    std::printf("DISPLAY_ROUTING_PIXEL interval=%u rgba=%u,%u,%u,%u valid=%d\n",
                interval, pixel[0], pixel[1], pixel[2], pixel[3], valid);
    D3D12_RANGE written{0, 0};
    readback->Unmap(0, &written);
    require(valid, "presented backbuffer readback mismatch");
    check(chain->Present(interval, 0), "Present");
}

void move(HWND window, const Display& display) {
    require(SetWindowPos(window, nullptr, display.info.rcMonitor.left + 24,
                         display.info.rcMonitor.top + 24, 0, 0,
                         SWP_NOSIZE | SWP_NOZORDER), "SetWindowPos");
    pump();
    require(MonitorFromWindow(window, MONITOR_DEFAULTTONEAREST) == display.monitor,
            "window did not move to requested monitor");
}

ComPtr<IDXGIOutput> outputFor(IDXGIFactory4* factory, HMONITOR monitor) {
    for (UINT adapterIndex = 0; ; ++adapterIndex) {
        ComPtr<IDXGIAdapter> adapter;
        if (factory->EnumAdapters(adapterIndex, &adapter) == DXGI_ERROR_NOT_FOUND) break;
        require(adapter != nullptr, "EnumAdapters");
        for (UINT outputIndex = 0; ; ++outputIndex) {
            ComPtr<IDXGIOutput> output;
            const HRESULT result = adapter->EnumOutputs(outputIndex, &output);
            if (result == DXGI_ERROR_NOT_FOUND) break;
            check(result, "EnumOutputs");
            DXGI_OUTPUT_DESC description{};
            check(output->GetDesc(&description), "output GetDesc");
            if (description.Monitor == monitor) return output;
        }
    }
    throw std::runtime_error("target monitor absent from native DXGI factory");
}

void checkFullscreenState(IDXGISwapChain3* chain, const Display* expected) {
    BOOL fullscreen = FALSE;
    ComPtr<IDXGIOutput> target;
    check(chain->GetFullscreenState(&fullscreen, &target), "GetFullscreenState");
    require((fullscreen != FALSE) == (expected != nullptr), "unexpected fullscreen state");
    if (!expected) return;
    require(target != nullptr, "fullscreen target is missing");
    DXGI_OUTPUT_DESC description{};
    check(target->GetDesc(&description), "fullscreen target GetDesc");
    require(description.Monitor == expected->monitor, "fullscreen target selected wrong monitor");
}

void verifyModesUnchanged(const std::vector<Display>& expected) {
    for (const auto& display : expected) {
        const auto live = displayFor(display.monitor);
        require(live.current.dmPelsWidth == display.current.dmPelsWidth &&
                live.current.dmPelsHeight == display.current.dmPelsHeight &&
                live.current.dmDisplayFrequency == display.current.dmDisplayFrequency,
                "physical current display mode changed");
    }
}

void run(bool savedSplit) {
    auto monitors = displays();
    HMONITOR primary = MonitorFromPoint(POINT{0, 0}, MONITOR_DEFAULTTOPRIMARY);
    auto selected = monitors.begin();
    for (auto it = monitors.begin(); it != monitors.end(); ++it)
        if (it->monitor == primary) selected = it;
    const Display first = *selected;
    Window window(first);
    std::optional<SavedMode> saved;
    if (savedSplit) {
        saved.emplace(first);
        require(saved->active(), "same-resolution saved-only alternate unavailable");
    }

    Graphics graphics;
    ComPtr<IDXGIFactory4> factory;
    check(CreateDXGIFactory1(IID_PPV_ARGS(&factory)), "CreateDXGIFactory1");
    auto chain = createSwapchain(factory.Get(), graphics, window.handle);
    checkOutput(chain.Get(), first, savedSplit ? "saved-only" : "baseline");
    renderAndPresent(graphics, chain.Get(), 1);
    renderAndPresent(graphics, chain.Get(), 0);

    if (monitors.size() < 2) {
        std::puts("DISPLAY_ROUTING_MULTIMONITOR_SKIP only one display");
    } else {
        const Display second = *std::find_if(monitors.begin(), monitors.end(),
            [&](const Display& output) { return output.monitor != first.monitor; });
        move(window.handle, second);
        checkOutput(chain.Get(), second, "window-moved");
        renderAndPresent(graphics, chain.Get(), 1);
        move(window.handle, first);
        checkOutput(chain.Get(), first, "window-returned");
        for (const auto& area : monitors) {
            const RECT& rect = area.info.rcMonitor;
            const std::array<POINT, 5> positions{{
                {rect.left - int(kSize / 2), rect.top + 32},
                {rect.right - int(kSize / 2), rect.top + 32},
                {rect.left + 32, rect.top - int(kSize / 2)},
                {rect.left + 32, rect.bottom - int(kSize / 2)},
                {rect.right + int(kSize), rect.bottom + int(kSize)},
            }};
            for (const POINT position : positions) {
                require(SetWindowPos(window.handle, nullptr, position.x, position.y, 0, 0,
                                     SWP_NOSIZE | SWP_NOZORDER), "boundary SetWindowPos");
                pump();
                const HMONITOR nearest = MonitorFromWindow(window.handle, MONITOR_DEFAULTTONEAREST);
                const auto expected = std::find_if(monitors.begin(), monitors.end(),
                    [&](const Display& candidate) { return candidate.monitor == nearest; });
                require(expected != monitors.end(), "nearest monitor not enumerated");
                checkOutput(chain.Get(), *expected, "boundary-nearest");
            }
        }
        move(window.handle, first);
        auto target = outputFor(factory.Get(), second.monitor);
        check(chain->SetFullscreenState(TRUE, target.Get()), "explicit SetFullscreenState");
        checkFullscreenState(chain.Get(), &second);
        checkOutput(chain.Get(), second, "fullscreen-explicit");
        verifyModesUnchanged(monitors);
        check(chain->SetFullscreenState(FALSE, nullptr), "return windowed");
        checkFullscreenState(chain.Get(), nullptr);
        move(window.handle, first);
        checkOutput(chain.Get(), first, "windowed-return");
        verifyModesUnchanged(monitors);
        std::puts("DISPLAY_ROUTING_MULTIMONITOR_PASS");
    }
    auto primaryTarget = outputFor(factory.Get(), first.monitor);
    check(chain->SetFullscreenState(TRUE, primaryTarget.Get()), "explicit primary SetFullscreenState");
    checkFullscreenState(chain.Get(), &first);
    checkOutput(chain.Get(), first, "fullscreen-primary-explicit");
    verifyModesUnchanged(monitors);
    check(chain->SetFullscreenState(FALSE, nullptr), "return primary windowed");
    checkFullscreenState(chain.Get(), nullptr);
    checkOutput(chain.Get(), first, "primary-windowed-return");
    verifyModesUnchanged(monitors);
    std::puts("DISPLAY_ROUTING_EXPLICIT_PRIMARY_PASS");
    chain.Reset();
    factory.Reset();
    graphics.queue.Reset();
    verifyModesUnchanged(monitors);
    if (saved) {
        saved->restore();
        require(saved->restored(), "saved mode restoration failed");
    }
    std::puts(savedSplit ? "DISPLAY_ROUTING_SAVED_PASS" : "DISPLAY_ROUTING_BASELINE_PASS");
}

void runResidency() {
    const auto monitors = displays();
    const HMONITOR primary = MonitorFromPoint(POINT{0, 0}, MONITOR_DEFAULTTOPRIMARY);
    const auto selected = std::find_if(monitors.begin(), monitors.end(),
        [&](const Display& display) { return display.monitor == primary; });
    require(selected != monitors.end(), "primary monitor not enumerated");
    Window window(*selected);
    Graphics graphics;
    ComPtr<IDXGIFactory4> factory;
    check(CreateDXGIFactory1(IID_PPV_ARGS(&factory)), "CreateDXGIFactory1");

    // A fresh queue per swapchain would conceal Metal4's 32-set limit. Keep
    // this queue and HWND alive while each layer, drawable, and swapchain turns
    // over; resize while the previous Present may still await the display.
    for (unsigned cycle = 0; cycle < 128; ++cycle) {
        pump();
        auto chain = createSwapchain(factory.Get(), graphics, window.handle);
        renderAndPresent(graphics, chain.Get(), 0);
        check(chain->ResizeBuffers(2, kSize + 16, kSize + 16,
                                   DXGI_FORMAT_R8G8B8A8_UNORM, 0), "ResizeBuffers");
        DXGI_SWAP_CHAIN_DESC1 resized{};
        check(chain->GetDesc1(&resized), "GetDesc1 after ResizeBuffers");
        require(resized.Width == kSize + 16 && resized.Height == kSize + 16,
                "swapchain did not adopt resized backbuffers");
        renderAndPresent(graphics, chain.Get(), 0);
        chain.Reset();
        if ((cycle + 1) % 32 == 0)
            std::printf("DISPLAY_ROUTING_RESIDENCY_CYCLE completed=%u\n", cycle + 1);
    }
    verifyModesUnchanged(monitors);
    std::puts("DISPLAY_ROUTING_RESIDENCY_PASS");
}

} // namespace

int main(int argc, char** argv) {
    try {
        require(argc == 2, "usage: display-routing.exe baseline|saved|residency");
        if (!std::strcmp(argv[1], "baseline")) run(false);
        else if (!std::strcmp(argv[1], "saved")) run(true);
        else if (!std::strcmp(argv[1], "residency")) runResidency();
        else throw std::runtime_error("unknown test case");
        return 0;
    } catch (const std::exception& error) {
        std::fprintf(stderr, "DISPLAY_ROUTING_FAIL %s\n", error.what());
        return 1;
    }
}
