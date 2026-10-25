// Public D3D12 graphics -> D3DMetal -> GPU readback in a disposable Wine prefix.
// Pending frames may omit a draw; query-guarded frames must render exactly.
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_4.h>
#include <d3dcompiler.h>
#include <wrl/client.h>
#include <array>
#include <climits>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
using Microsoft::WRL::ComPtr;

static void check(bool value, const char* operation) {
    if (!value) { std::fprintf(stderr, "FAIL: %s\n", operation); std::exit(1); }
}
static void hr(HRESULT value, const char* operation) {
    if (FAILED(value)) {
        std::fprintf(stderr, "FAIL: %s HRESULT=%08lx\n", operation, (unsigned long)value);
        std::exit(1);
    }
}
static void transition(ID3D12GraphicsCommandList* list, ID3D12Resource* resource,
                       D3D12_RESOURCE_STATES before, D3D12_RESOURCE_STATES after) {
    D3D12_RESOURCE_BARRIER barrier{};
    barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    barrier.Transition = {resource, D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES, before, after};
    list->ResourceBarrier(1, &barrier);
}
static ComPtr<ID3DBlob> compile(const std::string& source, const char* target) {
    ComPtr<ID3DBlob> bytes, errors;
    HRESULT result = D3DCompile(source.data(), source.size(), nullptr, nullptr, nullptr,
                               "main", target, 0, 0, &bytes, &errors);
    if (FAILED(result) && errors)
        std::fprintf(stderr, "%s\n", static_cast<const char*>(errors->GetBufferPointer()));
    hr(result, "D3DCompile");
    return bytes;
}
int main() {
    std::setvbuf(stdout, nullptr, _IONBF, 0);
    constexpr UINT width = 64, height = 64;
    const HINSTANCE instance = GetModuleHandleW(nullptr);
    WNDCLASSW wc{};
    wc.hInstance = instance; wc.lpfnWndProc = DefWindowProcW;
    wc.lpszClassName = L"D3DMetalAsyncSmoke";
    check(RegisterClassW(&wc) != 0, "RegisterClass");
    HWND window = CreateWindowW(wc.lpszClassName, L"D3DMetal async smoke", WS_OVERLAPPEDWINDOW,
                                0, 0, width, height, nullptr, nullptr, instance, nullptr);
    check(window != nullptr, "CreateWindow");
    ComPtr<ID3D12Device> device;
    hr(D3D12CreateDevice(nullptr, D3D_FEATURE_LEVEL_12_0, IID_PPV_ARGS(&device)), "D3D12CreateDevice");
    ComPtr<ID3D12CommandQueue> queue;
    D3D12_COMMAND_QUEUE_DESC queueDesc{};
    hr(device->CreateCommandQueue(&queueDesc, IID_PPV_ARGS(&queue)), "CreateCommandQueue");
    ComPtr<IDXGIFactory4> factory;
    hr(CreateDXGIFactory1(IID_PPV_ARGS(&factory)), "CreateDXGIFactory");
    DXGI_SWAP_CHAIN_DESC1 swapDesc{};
    swapDesc.Width = width; swapDesc.Height = height;
    swapDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM; swapDesc.SampleDesc.Count = 1;
    swapDesc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    swapDesc.BufferCount = 2; swapDesc.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
    ComPtr<IDXGISwapChain1> swapBase;
    hr(factory->CreateSwapChainForHwnd(queue.Get(), window, &swapDesc, nullptr, nullptr, &swapBase),
       "CreateSwapChainForHwnd");
    ComPtr<IDXGISwapChain3> swap;
    hr(swapBase.As(&swap), "IDXGISwapChain3");
    ComPtr<ID3D12CommandAllocator> allocator;
    ComPtr<ID3D12GraphicsCommandList> list;
    hr(device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&allocator)), "CreateCommandAllocator");
    hr(device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, allocator.Get(), nullptr,
                                 IID_PPV_ARGS(&list)), "CreateCommandList");
    hr(list->Close(), "initial Close");
    D3D12_ROOT_PARAMETER parameters[2]{};
    parameters[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    parameters[0].Constants = {0, 0, 1};
    parameters[0].ShaderVisibility = D3D12_SHADER_VISIBILITY_PIXEL;
    parameters[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_UAV;
    parameters[1].Descriptor.ShaderRegister = 1;
    parameters[1].ShaderVisibility = D3D12_SHADER_VISIBILITY_PIXEL;
    D3D12_ROOT_SIGNATURE_DESC rootDesc{};
    rootDesc.NumParameters = 1; rootDesc.pParameters = parameters;
    rootDesc.Flags = D3D12_ROOT_SIGNATURE_FLAG_ALLOW_INPUT_ASSEMBLER_INPUT_LAYOUT;
    ComPtr<ID3DBlob> rootBytes, rootErrors;
    hr(D3D12SerializeRootSignature(&rootDesc, D3D_ROOT_SIGNATURE_VERSION_1, &rootBytes, &rootErrors), "SerializeRootSignature");
    ComPtr<ID3D12RootSignature> root;
    hr(device->CreateRootSignature(0, rootBytes->GetBufferPointer(), rootBytes->GetBufferSize(),
                                  IID_PPV_ARGS(&root)), "CreateRootSignature");
    rootDesc.NumParameters = 2;
    rootBytes.Reset(); rootErrors.Reset();
    hr(D3D12SerializeRootSignature(&rootDesc, D3D_ROOT_SIGNATURE_VERSION_1, &rootBytes, &rootErrors),
       "Serialize UAV root signature");
    ComPtr<ID3D12RootSignature> uavRoot;
    hr(device->CreateRootSignature(0, rootBytes->GetBufferPointer(), rootBytes->GetBufferSize(),
                                  IID_PPV_ARGS(&uavRoot)), "Create UAV root signature");
    auto vertex = compile("float4 main(uint id:SV_VertexID):SV_Position {"
                          "float2 p[3]={float2(-1,-1),float2(-1,3),float2(3,-1)};"
                          "return float4(p[id],0,1);}", "vs_5_0");
    auto pipeline = [&](unsigned seed, bool writesUav = false) {
        const unsigned channel = seed % 3;
        const float level = 0.5f + float(seed % 31) / 64.0f;
        char source[512];
        std::snprintf(source, sizeof(source),
            "cbuffer Config:register(b0){float gain;}"
            "RWStructuredBuffer<uint> writes:register(u1);"
            "float4 main():SV_Target{%sreturn float4(%.8f*gain,%.8f*gain,%.8f*gain,1);}",
            writesUav ? "uint previous;InterlockedAdd(writes[0],1,previous);" : "",
            channel == 0 ? level : 0.0f, channel == 1 ? level : 0.0f, channel == 2 ? level : 0.0f);
        auto pixel = compile(source, "ps_5_0");
        D3D12_GRAPHICS_PIPELINE_STATE_DESC desc{};
        desc.pRootSignature = writesUav ? uavRoot.Get() : root.Get();
        desc.VS = {vertex->GetBufferPointer(), vertex->GetBufferSize()};
        desc.PS = {pixel->GetBufferPointer(), pixel->GetBufferSize()};
        desc.BlendState.RenderTarget[0].RenderTargetWriteMask = D3D12_COLOR_WRITE_ENABLE_ALL;
        desc.BlendState.RenderTarget[0].SrcBlend = D3D12_BLEND_ONE;
        desc.BlendState.RenderTarget[0].DestBlend = D3D12_BLEND_ZERO;
        desc.BlendState.RenderTarget[0].BlendOp = D3D12_BLEND_OP_ADD;
        desc.BlendState.RenderTarget[0].SrcBlendAlpha = D3D12_BLEND_ONE;
        desc.BlendState.RenderTarget[0].DestBlendAlpha = D3D12_BLEND_ZERO;
        desc.BlendState.RenderTarget[0].BlendOpAlpha = D3D12_BLEND_OP_ADD;
        desc.BlendState.RenderTarget[0].LogicOp = D3D12_LOGIC_OP_NOOP;
        desc.SampleMask = UINT_MAX;
        desc.RasterizerState.FillMode = D3D12_FILL_MODE_SOLID;
        desc.RasterizerState.CullMode = D3D12_CULL_MODE_NONE;
        desc.RasterizerState.DepthClipEnable = TRUE;
        desc.DepthStencilState.DepthFunc = D3D12_COMPARISON_FUNC_ALWAYS;
        desc.PrimitiveTopologyType = D3D12_PRIMITIVE_TOPOLOGY_TYPE_TRIANGLE;
        desc.NumRenderTargets = 1; desc.RTVFormats[0] = swapDesc.Format;
        desc.SampleDesc.Count = 1;
        ComPtr<ID3D12PipelineState> result;
        hr(device->CreateGraphicsPipelineState(&desc, IID_PPV_ARGS(&result)), "CreateGraphicsPipelineState");
        return result;
    };
    D3D12_RESOURCE_DESC textureDesc{};
    textureDesc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    textureDesc.Width = width; textureDesc.Height = height; textureDesc.DepthOrArraySize = 1;
    textureDesc.MipLevels = 1; textureDesc.Format = swapDesc.Format; textureDesc.SampleDesc.Count = 1;
    textureDesc.Flags = D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET;
    D3D12_HEAP_PROPERTIES defaultHeap{}; defaultHeap.Type = D3D12_HEAP_TYPE_DEFAULT;
    ComPtr<ID3D12Resource> target;
    hr(device->CreateCommittedResource(&defaultHeap, D3D12_HEAP_FLAG_NONE, &textureDesc,
        D3D12_RESOURCE_STATE_RENDER_TARGET, nullptr, IID_PPV_ARGS(&target)), "Create target");
    D3D12_DESCRIPTOR_HEAP_DESC rtvDesc{};
    rtvDesc.Type = D3D12_DESCRIPTOR_HEAP_TYPE_RTV; rtvDesc.NumDescriptors = 1;
    ComPtr<ID3D12DescriptorHeap> rtvHeap;
    hr(device->CreateDescriptorHeap(&rtvDesc, IID_PPV_ARGS(&rtvHeap)), "Create RTV heap");
    auto rtv = rtvHeap->GetCPUDescriptorHandleForHeapStart();
    device->CreateRenderTargetView(target.Get(), nullptr, rtv);
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT footprint{};
    UINT64 readbackSize = 0;
    device->GetCopyableFootprints(&textureDesc, 0, 1, 0, &footprint, nullptr, nullptr, &readbackSize);
    auto buffer = [&](UINT64 size, D3D12_HEAP_TYPE type, D3D12_RESOURCE_STATES state,
                      D3D12_RESOURCE_FLAGS flags = D3D12_RESOURCE_FLAG_NONE) {
        D3D12_RESOURCE_DESC desc{};
        desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER; desc.Width = size;
        desc.Height = 1; desc.DepthOrArraySize = 1; desc.MipLevels = 1;
        desc.SampleDesc.Count = 1; desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
        desc.Flags = flags;
        D3D12_HEAP_PROPERTIES heap{}; heap.Type = type;
        ComPtr<ID3D12Resource> result;
        hr(device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &desc, state,
                                           nullptr, IID_PPV_ARGS(&result)), "Create buffer");
        return result;
    };
    auto readback = buffer(readbackSize, D3D12_HEAP_TYPE_READBACK, D3D12_RESOURCE_STATE_COPY_DEST);
    auto indices = buffer(3 * sizeof(UINT16), D3D12_HEAP_TYPE_UPLOAD, D3D12_RESOURCE_STATE_GENERIC_READ);
    void* mapped = nullptr; D3D12_RANGE noRead{0,0};
    hr(indices->Map(0, &noRead, &mapped), "Map indices");
    const UINT16 indexData[]{0,1,2}; std::memcpy(mapped, indexData, sizeof(indexData));
    indices->Unmap(0, nullptr);
    auto counter = buffer(sizeof(UINT), D3D12_HEAP_TYPE_DEFAULT,
                          D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
    auto counterReadback = buffer(sizeof(UINT), D3D12_HEAP_TYPE_READBACK, D3D12_RESOURCE_STATE_COPY_DEST);
    auto zero = buffer(sizeof(UINT), D3D12_HEAP_TYPE_UPLOAD, D3D12_RESOURCE_STATE_GENERIC_READ);
    hr(zero->Map(0, &noRead, &mapped), "Map counter zero");
    std::memset(mapped, 0, sizeof(UINT)); zero->Unmap(0, nullptr);
    D3D12_INDEX_BUFFER_VIEW indexView{indices->GetGPUVirtualAddress(), sizeof(indexData), DXGI_FORMAT_R16_UINT};
    D3D12_QUERY_HEAP_DESC queryDesc{}; queryDesc.Type = D3D12_QUERY_HEAP_TYPE_OCCLUSION; queryDesc.Count = 1;
    ComPtr<ID3D12QueryHeap> query;
    hr(device->CreateQueryHeap(&queryDesc, IID_PPV_ARGS(&query)), "CreateQueryHeap");
    auto queryReadback = buffer(sizeof(UINT64), D3D12_HEAP_TYPE_READBACK, D3D12_RESOURCE_STATE_COPY_DEST);
    D3D12_INDIRECT_ARGUMENT_DESC arguments[2]{};
    arguments[0].Type = D3D12_INDIRECT_ARGUMENT_TYPE_CONSTANT;
    arguments[0].Constant = {0, 0, 1};
    arguments[1].Type = D3D12_INDIRECT_ARGUMENT_TYPE_DRAW;
    struct IndirectArguments { float gain; D3D12_DRAW_ARGUMENTS draw; } indirectData{1.f,{3,1,0,0}};
    D3D12_COMMAND_SIGNATURE_DESC commandDesc{};
    commandDesc.ByteStride = sizeof(indirectData); commandDesc.NumArgumentDescs = 2;
    commandDesc.pArgumentDescs = arguments;
    ComPtr<ID3D12CommandSignature> command;
    hr(device->CreateCommandSignature(&commandDesc, root.Get(), IID_PPV_ARGS(&command)), "CreateCommandSignature");
    auto indirect = buffer(sizeof(indirectData), D3D12_HEAP_TYPE_UPLOAD, D3D12_RESOURCE_STATE_GENERIC_READ);
    hr(indirect->Map(0, &noRead, &mapped), "Map indirect args");
    std::memcpy(mapped, &indirectData, sizeof(indirectData)); indirect->Unmap(0, nullptr);
    ComPtr<ID3D12Fence> fence;
    hr(device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&fence)), "CreateFence");
    HANDLE event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    check(event != nullptr, "CreateEvent");
    UINT64 serial = 0;
    enum class Draw { direct, indexed, indirect, afterIndirect };
    auto frame = [&](ID3D12PipelineState* pso, Draw kind, bool guarded, bool writesUav = false) {
        hr(allocator->Reset(), "Reset allocator");
        hr(list->Reset(allocator.Get(), nullptr), "Reset list");
        transition(list.Get(),counter.Get(),D3D12_RESOURCE_STATE_UNORDERED_ACCESS,D3D12_RESOURCE_STATE_COPY_DEST);
        list->CopyBufferRegion(counter.Get(),0,zero.Get(),0,sizeof(UINT));
        transition(list.Get(),counter.Get(),D3D12_RESOURCE_STATE_COPY_DEST,D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        const float clear[]{0,0,0,1}; list->ClearRenderTargetView(rtv, clear, 0, nullptr);
        list->OMSetRenderTargets(1, &rtv, FALSE, nullptr);
        const D3D12_VIEWPORT viewport{0,0,float(width),float(height),0,1};
        const D3D12_RECT scissor{0,0,LONG(width),LONG(height)};
        list->RSSetViewports(1, &viewport); list->RSSetScissorRects(1, &scissor);
        list->SetGraphicsRootSignature(writesUav ? uavRoot.Get() : root.Get());
        const float gain = 1.f; UINT bits; std::memcpy(&bits, &gain, sizeof(bits));
        list->SetGraphicsRoot32BitConstant(0, bits, 0);
        if (writesUav) list->SetGraphicsRootUnorderedAccessView(1, counter->GetGPUVirtualAddress());
        list->SetPipelineState(pso); list->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        list->IASetIndexBuffer(&indexView);
        if (guarded) list->BeginQuery(query.Get(), D3D12_QUERY_TYPE_OCCLUSION, 0);
        if (kind == Draw::indexed) list->DrawIndexedInstanced(3,1,0,0,0);
        else if (kind == Draw::indirect || kind == Draw::afterIndirect) {
            list->ExecuteIndirect(command.Get(),1,indirect.Get(),0,nullptr,0);
            if (kind == Draw::afterIndirect) {
                list->ClearRenderTargetView(rtv, clear, 0, nullptr);
                list->DrawInstanced(3,1,0,0);
            }
        } else list->DrawInstanced(3,1,0,0);
        if (guarded) {
            list->EndQuery(query.Get(), D3D12_QUERY_TYPE_OCCLUSION, 0);
            list->ResolveQueryData(query.Get(), D3D12_QUERY_TYPE_OCCLUSION, 0, 1, queryReadback.Get(), 0);
        }
        transition(list.Get(),counter.Get(),D3D12_RESOURCE_STATE_UNORDERED_ACCESS,D3D12_RESOURCE_STATE_COPY_SOURCE);
        list->CopyBufferRegion(counterReadback.Get(),0,counter.Get(),0,sizeof(UINT));
        transition(list.Get(),counter.Get(),D3D12_RESOURCE_STATE_COPY_SOURCE,D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        transition(list.Get(),target.Get(),D3D12_RESOURCE_STATE_RENDER_TARGET,D3D12_RESOURCE_STATE_COPY_SOURCE);
        D3D12_TEXTURE_COPY_LOCATION source{}; source.pResource = target.Get();
        source.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
        D3D12_TEXTURE_COPY_LOCATION dest{}; dest.pResource = readback.Get();
        dest.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT; dest.PlacedFootprint = footprint;
        list->CopyTextureRegion(&dest,0,0,0,&source,nullptr);
        ComPtr<ID3D12Resource> backbuffer;
        hr(swap->GetBuffer(swap->GetCurrentBackBufferIndex(), IID_PPV_ARGS(&backbuffer)), "GetBuffer");
        transition(list.Get(),backbuffer.Get(),D3D12_RESOURCE_STATE_PRESENT,D3D12_RESOURCE_STATE_COPY_DEST);
        list->CopyResource(backbuffer.Get(),target.Get());
        transition(list.Get(),backbuffer.Get(),D3D12_RESOURCE_STATE_COPY_DEST,D3D12_RESOURCE_STATE_PRESENT);
        transition(list.Get(),target.Get(),D3D12_RESOURCE_STATE_COPY_SOURCE,D3D12_RESOURCE_STATE_RENDER_TARGET);
        hr(list->Close(), "Close list");
        // Subscribe before submission: no timing sleeps or completion polling.
        hr(fence->SetEventOnCompletion(++serial,event), "SetEventOnCompletion");
        ID3D12CommandList* submitted[]{list.Get()}; queue->ExecuteCommandLists(1,submitted);
        hr(queue->Signal(fence.Get(),serial), "Signal fence");
        check(WaitForSingleObject(event,60000) == WAIT_OBJECT_0, "GPU completion event");
        hr(device->GetDeviceRemovedReason(), "device status");
        hr(swap->Present(0,0), "Present");
        D3D12_RANGE range{0,SIZE_T(readbackSize)};
        hr(readback->Map(0,&range,&mapped), "Map readback");
        const auto* pixel = static_cast<const unsigned char*>(mapped) + footprint.Offset +
                            (height/2)*footprint.Footprint.RowPitch + (width/2)*4;
        std::array<unsigned,4> result{pixel[0],pixel[1],pixel[2],pixel[3]};
        readback->Unmap(0,&noRead);
        if (writesUav) {
            D3D12_RANGE counterRange{0,sizeof(UINT)};
            hr(counterReadback->Map(0,&counterRange,&mapped), "Map UAV counter");
            const UINT fragments = *static_cast<const UINT*>(mapped);
            counterReadback->Unmap(0,&noRead);
            std::printf("graphics UAV fragments=%u\n", fragments);
            check(fragments == width * height, "graphics UAV side effects preserved");
        }
        if (guarded) {
            D3D12_RANGE queryRange{0,sizeof(UINT64)};
            hr(queryReadback->Map(0,&queryRange,&mapped), "Map query");
            const UINT64 samples = *static_cast<const UINT64*>(mapped);
            queryReadback->Unmap(0,&noRead);
            check(samples != 0, "query-guarded draw executed");
        }
        return result;
    };
    auto expect = [&](const std::array<unsigned,4>& pixel, unsigned seed, bool black, const char* name) {
        const unsigned channel = seed % 3;
        const int expected = int((0.5f + float(seed % 31)/64.0f)*255.0f + 0.5f);
        std::printf("%s RGBA=%u,%u,%u,%u\n",name,pixel[0],pixel[1],pixel[2],pixel[3]);
        for (unsigned c = 0; c < 3; ++c) {
            const int wanted = !black && c == channel ? expected : 0;
            check(std::abs(int(pixel[c])-wanted) <= 1, name);
        }
        check(pixel[3] == 255, "opaque alpha");
    };
    auto warm = pipeline(0);
    // These are actual presented frames, not delays to guess compiler completion.
    for (unsigned i = 0; i < 8; ++i)
        expect(frame(warm.Get(),Draw::direct,true),0,false,"warm recurring target");
    for (unsigned seed = 1; seed <= 3; ++seed) {
        if (seed != 1) {
            for (unsigned i = 0; i < 8; ++i)
                expect(frame(warm.Get(),Draw::direct,true),0,false,"restore recurring target before cold PSO");
        }
        auto cold = pipeline(seed);
        auto first = frame(cold.Get(), seed == 2 ? Draw::indexed : Draw::direct, false);
        const bool skipped = first[0] == 0 && first[1] == 0 && first[2] == 0;
        expect(first,seed,skipped,skipped ? "pending draw omitted" : "ready draw rendered");
        // A real query makes the draw non-skippable and joins any pending compile.
        expect(frame(cold.Get(),Draw::indexed,true),seed,false,"query fallback and ready indexed draw");
        expect(frame(cold.Get(),Draw::direct,false),seed,false,"ready direct draw resumes");
        expect(frame(cold.Get(),Draw::indirect,true),seed,false,"indirect graphics fallback");
        expect(frame(cold.Get(),Draw::afterIndirect,true),seed,true,"indirect root constants reset");
    }
    for (unsigned seed = 10; seed < 18; ++seed) {
        auto releasedDuringCompile = pipeline(seed);
        releasedDuringCompile.Reset();
    }
    for (unsigned i = 0; i < 8; ++i)
        expect(frame(warm.Get(),Draw::direct,true),0,false,"restore recurring target before UAV");
    auto uavPipeline = pipeline(20, true);
    expect(frame(uavPipeline.Get(),Draw::direct,false,true),20,false,"cold graphics UAV fallback");
    target.Reset();
    hr(device->CreateCommittedResource(&defaultHeap, D3D12_HEAP_FLAG_NONE, &textureDesc,
        D3D12_RESOURCE_STATE_RENDER_TARGET, nullptr, IID_PPV_ARGS(&target)), "Create one-shot target");
    device->CreateRenderTargetView(target.Get(), nullptr, rtv);
    auto oneShot = pipeline(21);
    expect(frame(oneShot.Get(),Draw::direct,false),21,false,"first-use target is not skipped");
    expect(frame(warm.Get(),Draw::direct,true),0,false,"render after pending PSO retirement");
    hr(list->Reset(allocator.Get(),nullptr), "retire list PSO references");
    hr(list->Close(), "close retired list");
    CloseHandle(event);
    swap.Reset(); swapBase.Reset(); DestroyWindow(window);
    std::puts("D3DMETAL_ASYNC_PUBLIC_SMOKE_PASS");
    return 0;
}
