// Public Wine PE API -> Unix bridge -> beta3 MPL recording/replay -> GPU readback.
// Run only against a disposable staged runtime. FG also needs its native presenter DLL.
#include <windows.h>
#include <d3d12.h>
#include <d3dcompiler.h>
#include <wrl/client.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
#include "../d3dmetal-sidecar/third-party/fidelityfx/Kits/FidelityFX/api/include/dx12/ffx_api_dx12.h"
#include "../d3dmetal-sidecar/third-party/fidelityfx/Kits/FidelityFX/upscalers/include/ffx_upscale.h"
#include "../d3dmetal-sidecar/third-party/fidelityfx/Kits/FidelityFX/framegeneration/include/ffx_framegeneration.h"
using Microsoft::WRL::ComPtr;

static void check(bool ok, const char* operation) {
    if (!ok) { std::fprintf(stderr, "FAIL: %s\n", operation); std::exit(1); }
}
static void hr(HRESULT result, const char* operation) {
    if (FAILED(result)) { std::fprintf(stderr, "%s: HRESULT %08lx\n", operation, (unsigned long)result); std::exit(1); }
}
static void ffx(ffxReturnCode_t result, const char* operation) {
    if (result) { std::fprintf(stderr, "%s: FSR result %u\n", operation, unsigned(result)); std::exit(1); }
}
struct Api {
    HMODULE module;
    PfnFfxCreateContext create;
    PfnFfxDestroyContext destroy;
    PfnFfxConfigure configure;
    PfnFfxDispatch dispatch;
    explicit Api(const wchar_t* name) : module(LoadLibraryW(name)) {
        check(module != nullptr, "load FSR builtin");
        create = reinterpret_cast<PfnFfxCreateContext>(GetProcAddress(module, "ffxCreateContext"));
        destroy = reinterpret_cast<PfnFfxDestroyContext>(GetProcAddress(module, "ffxDestroyContext"));
        configure = reinterpret_cast<PfnFfxConfigure>(GetProcAddress(module, "ffxConfigure"));
        dispatch = reinterpret_cast<PfnFfxDispatch>(GetProcAddress(module, "ffxDispatch"));
        check(create && destroy && configure && dispatch, "FSR public exports");
    }
    ~Api() { FreeLibrary(module); }
};
static void transition(ID3D12GraphicsCommandList* list, ID3D12Resource* resource,
                       D3D12_RESOURCE_STATES before, D3D12_RESOURCE_STATES after) {
    D3D12_RESOURCE_BARRIER barrier{};
    barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    barrier.Transition = {resource, D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES, before, after};
    list->ResourceBarrier(1, &barrier);
}
int main(int argc, char** argv) {
    const bool fgEnabled = argc == 2 && std::strcmp(argv[1], "--fg") == 0;
    check(argc == 1 || fgEnabled, "usage: fsr-public-smoke.exe [--fg]");
    constexpr UINT iw = 256, ih = 144, ow = 512, oh = 288;
    ComPtr<ID3D12Device> device;
    hr(D3D12CreateDevice(nullptr, D3D_FEATURE_LEVEL_12_0, IID_PPV_ARGS(&device)), "D3D12CreateDevice");
    ComPtr<ID3D12CommandQueue> queue;
    D3D12_COMMAND_QUEUE_DESC qd{};
    hr(device->CreateCommandQueue(&qd, IID_PPV_ARGS(&queue)), "CreateCommandQueue");
    ComPtr<ID3D12CommandAllocator> allocator;
    ComPtr<ID3D12GraphicsCommandList> list;
    hr(device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&allocator)), "CreateCommandAllocator");
    hr(device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, allocator.Get(), nullptr, IID_PPV_ARGS(&list)), "CreateCommandList");
    // Supply valid caller compute state, as a game does before evaluating FSR.
    // The transport replaces the dispatch tail, not the caller's root state.
    D3D12_ROOT_SIGNATURE_DESC rootDesc{};
    ComPtr<ID3DBlob> rootBytes, shaderBytes, errors;
    hr(D3D12SerializeRootSignature(&rootDesc, D3D_ROOT_SIGNATURE_VERSION_1, &rootBytes, &errors), "SerializeRootSignature");
    ComPtr<ID3D12RootSignature> signature;
    hr(device->CreateRootSignature(0, rootBytes->GetBufferPointer(), rootBytes->GetBufferSize(), IID_PPV_ARGS(&signature)), "CreateRootSignature");
    const char shader[] = "[numthreads(1,1,1)] void main() {}";
    hr(D3DCompile(shader, sizeof(shader), nullptr, nullptr, nullptr, "main", "cs_5_0", 0, 0, &shaderBytes, &errors), "Compile caller shader");
    D3D12_COMPUTE_PIPELINE_STATE_DESC pipelineDesc{};
    pipelineDesc.pRootSignature = signature.Get();
    pipelineDesc.CS = {shaderBytes->GetBufferPointer(), shaderBytes->GetBufferSize()};
    ComPtr<ID3D12PipelineState> pipeline;
    hr(device->CreateComputePipelineState(&pipelineDesc, IID_PPV_ARGS(&pipeline)), "CreateComputePipelineState");
    list->SetComputeRootSignature(signature.Get());
    list->SetPipelineState(pipeline.Get());
    ComPtr<ID3D12DescriptorHeap> rtv;
    D3D12_DESCRIPTOR_HEAP_DESC hd{D3D12_DESCRIPTOR_HEAP_TYPE_RTV, 5, D3D12_DESCRIPTOR_HEAP_FLAG_NONE, 0};
    hr(device->CreateDescriptorHeap(&hd, IID_PPV_ARGS(&rtv)), "CreateDescriptorHeap");
    const UINT stride = device->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_RTV);
    auto texture = [&](UINT w, UINT h, DXGI_FORMAT format, bool output) {
        D3D12_RESOURCE_DESC desc{};
        desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
        desc.Width = w; desc.Height = h; desc.DepthOrArraySize = 1; desc.MipLevels = 1;
        desc.Format = format; desc.SampleDesc.Count = 1;
        desc.Flags = output ? D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS : D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET;
        D3D12_HEAP_PROPERTIES heap{}; heap.Type = D3D12_HEAP_TYPE_DEFAULT;
        ComPtr<ID3D12Resource> result;
        hr(device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &desc,
            output ? D3D12_RESOURCE_STATE_UNORDERED_ACCESS : D3D12_RESOURCE_STATE_RENDER_TARGET,
            nullptr, IID_PPV_ARGS(&result)), "CreateCommittedResource texture");
        return result;
    };
    auto color = texture(iw, ih, DXGI_FORMAT_R8G8B8A8_UNORM, false);
    auto depth = texture(iw, ih, DXGI_FORMAT_R32_FLOAT, false);
    auto motion = texture(iw, ih, DXGI_FORMAT_R16G16_FLOAT, false);
    auto reactive = texture(iw, ih, DXGI_FORMAT_R8_UNORM, false);
    auto composition = texture(iw, ih, DXGI_FORMAT_R8_UNORM, false);
    auto output = texture(ow, oh, DXGI_FORMAT_R8G8B8A8_UNORM, true);
    auto generated = texture(ow, oh, DXGI_FORMAT_R8G8B8A8_UNORM, true);
    ID3D12Resource* inputs[]{color.Get(), depth.Get(), motion.Get(), reactive.Get(), composition.Get()};
    const float values[5][4]{{.25f,.5f,.75f,1.f},{.5f,0,0,0},{0,0,0,0},{0,0,0,0},{0,0,0,0}};
    for (UINT i = 0; i < 5; ++i) {
        auto handle = rtv->GetCPUDescriptorHandleForHeapStart(); handle.ptr += i * stride;
        device->CreateRenderTargetView(inputs[i], nullptr, handle);
        list->ClearRenderTargetView(handle, values[i], 0, nullptr);
        transition(list.Get(), inputs[i], D3D12_RESOURCE_STATE_RENDER_TARGET, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    }
    Api sr(L"amd_fidelityfx_upscaler_dx12.dll");
    ffxCreateBackendDX12Desc backend{}; backend.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_BACKEND_DX12; backend.device = device.Get();
    ffxCreateContextDescUpscale create{}; create.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_UPSCALE;
    create.header.pNext = &backend.header; create.maxRenderSize = {iw,ih}; create.maxUpscaleSize = {ow,oh};
    ffxContext srContext{};
    ffx(sr.create(&srContext, &create.header, nullptr), "SR create");
    ffxDispatchDescUpscale dispatch{}; dispatch.header.type = FFX_API_DISPATCH_DESC_TYPE_UPSCALE;
    dispatch.commandList = list.Get(); dispatch.color = ffxApiGetResourceDX12(color.Get());
    dispatch.depth = ffxApiGetResourceDX12(depth.Get()); dispatch.motionVectors = ffxApiGetResourceDX12(motion.Get());
    dispatch.reactive = ffxApiGetResourceDX12(reactive.Get()); dispatch.transparencyAndComposition = ffxApiGetResourceDX12(composition.Get());
    dispatch.output = ffxApiGetResourceDX12(output.Get(), FFX_API_RESOURCE_STATE_UNORDERED_ACCESS);
    dispatch.renderSize = {iw,ih}; dispatch.upscaleSize = {ow,oh}; dispatch.motionVectorScale = {float(iw),float(ih)};
    dispatch.preExposure = 1; dispatch.frameTimeDelta = 16.67f; dispatch.cameraNear = .1f; dispatch.cameraFar = 100.f;
    dispatch.cameraFovAngleVertical = 1.f; dispatch.viewSpaceToMetersFactor = 1; dispatch.reset = true;
    dispatch.enableSharpening = true; dispatch.sharpness = .2f;
    ffx(sr.dispatch(&srContext, &dispatch.header), "SR record");
    // Context destruction before submission exercises allocator-owned prepared work.
    ffx(sr.destroy(&srContext, nullptr), "SR destroy before replay");

    if (fgEnabled) {
        Api fg(L"amd_fidelityfx_framegeneration_dx12.dll");
        ffxCreateContextDescFrameGeneration fgCreate{}; fgCreate.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_FRAMEGENERATION;
        ffxCreateContextDescFrameGenerationVersion version{};
        version.header.type = FFX_API_CREATE_CONTEXT_DESC_TYPE_FRAMEGENERATION_VERSION;
        version.version = FFX_FRAMEGENERATION_VERSION;
        version.header.pNext = &backend.header;
        fgCreate.header.pNext = &version.header; fgCreate.displaySize = {ow,oh}; fgCreate.maxRenderSize = {iw,ih};
        fgCreate.backBufferFormat = FFX_API_SURFACE_FORMAT_R8G8B8A8_UNORM;
        ffxContext fgContext{}; ffx(fg.create(&fgContext, &fgCreate.header, nullptr), "FG create");
        ffxConfigureDescFrameGeneration config{}; config.header.type = FFX_API_CONFIGURE_DESC_TYPE_FRAMEGENERATION;
        config.frameGenerationEnabled = true; config.flags = FFX_FRAMEGENERATION_FLAG_NO_SWAPCHAIN_CONTEXT_NOTIFY;
        config.generationRect = {0,0,int(ow),int(oh)}; config.frameID = 1;
        ffx(fg.configure(&fgContext, &config.header), "FG configure");
        ffxDispatchDescFrameGenerationPrepareV2 prepare{};
        prepare.header.type = FFX_API_DISPATCH_DESC_TYPE_FRAMEGENERATION_PREPARE_V2;
        prepare.frameID = 1; prepare.commandList = list.Get(); prepare.renderSize = {iw,ih};
        prepare.motionVectorScale = {float(iw),float(ih)}; prepare.frameTimeDelta = 16.67f; prepare.reset = true;
        prepare.cameraNear = .1f; prepare.cameraFar = 100.f; prepare.cameraFovAngleVertical = 1.f; prepare.viewSpaceToMetersFactor = 1;
        prepare.depth = dispatch.depth; prepare.motionVectors = dispatch.motionVectors;
        prepare.cameraUp[1] = 1; prepare.cameraRight[0] = 1; prepare.cameraForward[2] = 1;
        ffx(fg.dispatch(&fgContext, &prepare.header), "FG prepare record");
        ffxDispatchDescFrameGeneration generate{}; generate.header.type = FFX_API_DISPATCH_DESC_TYPE_FRAMEGENERATION;
        generate.commandList = list.Get(); generate.frameID = 1; generate.reset = true; generate.numGeneratedFrames = 1;
        generate.presentColor = dispatch.output;
        generate.outputs[0] = ffxApiGetResourceDX12(generated.Get(), FFX_API_RESOURCE_STATE_UNORDERED_ACCESS);
        generate.backbufferTransferFunction = FFX_API_BACKBUFFER_TRANSFER_FUNCTION_SRGB;
        generate.minMaxLuminance[1] = 100; generate.generationRect = config.generationRect;
        ffx(fg.dispatch(&fgContext, &generate.header), "FG generate record");
        ffx(fg.destroy(&fgContext, nullptr), "FG destroy before replay");
    }
    ID3D12Resource* result = fgEnabled ? generated.Get() : output.Get();
    transition(list.Get(), result, D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE);
    auto desc = result->GetDesc(); D3D12_PLACED_SUBRESOURCE_FOOTPRINT footprint{}; UINT64 size = 0;
    device->GetCopyableFootprints(&desc, 0, 1, 0, &footprint, nullptr, nullptr, &size);
    D3D12_RESOURCE_DESC readDesc{}; readDesc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER; readDesc.Width = size;
    readDesc.Height = 1; readDesc.DepthOrArraySize = 1; readDesc.MipLevels = 1; readDesc.SampleDesc.Count = 1;
    readDesc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    D3D12_HEAP_PROPERTIES heap{}; heap.Type = D3D12_HEAP_TYPE_READBACK; ComPtr<ID3D12Resource> readback;
    hr(device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &readDesc, D3D12_RESOURCE_STATE_COPY_DEST,
        nullptr, IID_PPV_ARGS(&readback)), "Create readback");
    D3D12_TEXTURE_COPY_LOCATION src{}; src.pResource = result; src.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    D3D12_TEXTURE_COPY_LOCATION dst{}; dst.pResource = readback.Get(); dst.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT; dst.PlacedFootprint = footprint;
    list->CopyTextureRegion(&dst, 0, 0, 0, &src, nullptr);
    transition(list.Get(), result, D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    for (auto* input : inputs)
        transition(list.Get(), input, D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE, D3D12_RESOURCE_STATE_RENDER_TARGET);
    hr(list->Close(), "Close");
    ID3D12CommandList* submitted[]{list.Get()};
    ComPtr<ID3D12Fence> fence; hr(device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&fence)), "CreateFence");
    HANDLE event = CreateEventW(nullptr, FALSE, FALSE, nullptr); check(event != nullptr, "CreateEvent");
    // Replay the same recorded native ownership after its first GPU completion.
    // Both public FSR contexts were already destroyed before either submission.
    for (UINT64 replay = 1; replay <= 2; ++replay) {
        hr(fence->SetEventOnCompletion(replay, event), "SetEventOnCompletion");
        queue->ExecuteCommandLists(1, submitted);
        hr(queue->Signal(fence.Get(), replay), "Signal");
        check(WaitForSingleObject(event, 60000) == WAIT_OBJECT_0, "GPU completion");
        hr(device->GetDeviceRemovedReason(), "GPU device status");
        void* mapped = nullptr; D3D12_RANGE range{0, SIZE_T(size)};
        hr(readback->Map(0, &range, &mapped), "Readback map");
        auto pixel = static_cast<const unsigned char*>(mapped) + footprint.Offset + (oh/2)*footprint.Footprint.RowPitch + (ow/2)*4;
        std::printf("%s replay=%llu readback center RGBA=%u,%u,%u,%u\n", fgEnabled ? "SR+FG" : "SR",
            static_cast<unsigned long long>(replay), pixel[0],pixel[1],pixel[2],pixel[3]);
        check(pixel[0] > 30 && pixel[0] < 100 && pixel[1] > 90 && pixel[1] < 170 && pixel[2] > 145 && pixel[2] < 235, "record/replay color readback");
        D3D12_RANGE none{0,0}; readback->Unmap(0, &none);
    }
    CloseHandle(event);
    hr(allocator->Reset(), "allocator lifetime retirement");
    std::puts(fgEnabled ? "FSR_PUBLIC_SR_FG_PASS" : "FSR_PUBLIC_SR_PASS");
    return 0;
}
