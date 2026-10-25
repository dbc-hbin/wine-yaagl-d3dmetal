// Public D3D11 graphics -> D3DMetal -> GPU readback. No private hook exports.
// Run with async both disabled and enabled; only eligible cold draws may be omitted.
#include <windows.h>
#include <d3d11.h>
#include <d3d11_4.h>
#include <dxgi.h>
#include <d3dcompiler.h>
#include <wrl/client.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
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
static ComPtr<ID3DBlob> compile(const char* source, const char* target) {
    ComPtr<ID3DBlob> bytes, errors;
    HRESULT result = D3DCompile(source, std::strlen(source), nullptr, nullptr, nullptr,
                               "main", target, 0, 0, &bytes, &errors);
    if (FAILED(result) && errors)
        std::fprintf(stderr, "%s\n", static_cast<const char*>(errors->GetBufferPointer()));
    hr(result, "D3DCompile");
    return bytes;
}
static void queryData(ID3D11DeviceContext* context, ID3D11Query* query,
                      void* data, UINT size) {
    // The deadline bounds actual GPU completion, not background compiler readiness.
    context->Flush();
    const ULONGLONG start = GetTickCount64();
    for (;;) {
        HRESULT result = context->GetData(query, data, size, D3D11_ASYNC_GETDATA_DONOTFLUSH);
        hr(result, "GetData");
        if (result == S_OK) return;
        check(result == S_FALSE, "GetData status");
        check(GetTickCount64() - start < 30000, "GPU query completion deadline");
        SwitchToThread();
    }
}
int main() {
    std::setvbuf(stdout, nullptr, _IONBF, 0);
    constexpr UINT width = 64, height = 64;
    const float black[]{0,0,0,1};
    const HINSTANCE instance = GetModuleHandleW(nullptr);
    WNDCLASSW wc{};
    wc.hInstance = instance; wc.lpfnWndProc = DefWindowProcW;
    wc.lpszClassName = L"D3DMetalDX11AsyncSmoke";
    check(RegisterClassW(&wc) != 0, "RegisterClass");
    HWND window = CreateWindowW(wc.lpszClassName, L"D3DMetal DX11 async smoke",
        WS_OVERLAPPEDWINDOW, 0, 0, width, height, nullptr, nullptr, instance, nullptr);
    check(window != nullptr, "CreateWindow");
    DXGI_SWAP_CHAIN_DESC swapDesc{};
    swapDesc.BufferDesc.Width = width; swapDesc.BufferDesc.Height = height;
    swapDesc.BufferDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    swapDesc.SampleDesc.Count = 1; swapDesc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    swapDesc.BufferCount = 1; swapDesc.OutputWindow = window; swapDesc.Windowed = TRUE;
    swapDesc.SwapEffect = DXGI_SWAP_EFFECT_DISCARD;
    const D3D_FEATURE_LEVEL levels[]{D3D_FEATURE_LEVEL_11_0,
                                    D3D_FEATURE_LEVEL_10_1, D3D_FEATURE_LEVEL_10_0};
    D3D_FEATURE_LEVEL level{};
    ComPtr<ID3D11Device> device;
    ComPtr<ID3D11DeviceContext> context;
    ComPtr<IDXGISwapChain> swap;
    hr(D3D11CreateDeviceAndSwapChain(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0,
        levels, 3, D3D11_SDK_VERSION, &swapDesc, &swap, &device, &level, &context),
        "D3D11CreateDeviceAndSwapChain (FL10_0 minimum)");
    std::printf("feature_level=0x%x\n", unsigned(level));
    ComPtr<ID3D11Multithread> multithread;
    hr(context.As(&multithread), "Query multithread protection");
    multithread->SetMultithreadProtected(TRUE);
    check(multithread->GetMultithreadProtected(), "Enable native context locking");
    const bool graphicsUav = level >= D3D_FEATURE_LEVEL_11_0;
    const char* psProfile = graphicsUav ? "ps_5_0" : "ps_4_0";
    auto vsBytes = compile("float4 main(uint id:SV_VertexID):SV_Position {"
        "float2 p[3]={float2(-1,-1),float2(-1,3),float2(3,-1)};"
        "return float4(p[id],0,1);}", graphicsUav ? "vs_5_0" : "vs_4_0");
    ComPtr<ID3D11VertexShader> vertex;
    hr(device->CreateVertexShader(vsBytes->GetBufferPointer(), vsBytes->GetBufferSize(),
        nullptr, &vertex), "CreateVertexShader");
    auto pixelShader = [&](unsigned color, bool uav = false) {
        char source[512];
        std::snprintf(source, sizeof(source),
            "%s float4 main():SV_Target { %s return float4(%u,%u,%u,1);}",
            uav ? "RWStructuredBuffer<uint> writes:register(u1);" : "",
            uav ? "uint old; InterlockedAdd(writes[0],1,old);" : "",
            color & 1, (color >> 1) & 1, (color >> 2) & 1);
        auto bytes = compile(source, psProfile);
        ComPtr<ID3D11PixelShader> result;
        hr(device->CreatePixelShader(bytes->GetBufferPointer(), bytes->GetBufferSize(),
            nullptr, &result), "CreatePixelShader");
        return result;
    };
    D3D11_TEXTURE2D_DESC textureDesc{};
    textureDesc.Width = width; textureDesc.Height = height;
    textureDesc.MipLevels = 1; textureDesc.ArraySize = 1;
    textureDesc.Format = swapDesc.BufferDesc.Format; textureDesc.SampleDesc.Count = 1;
    textureDesc.Usage = D3D11_USAGE_DEFAULT; textureDesc.BindFlags = D3D11_BIND_RENDER_TARGET;
    auto newTarget = [&]() {
        ComPtr<ID3D11Texture2D> texture;
        hr(device->CreateTexture2D(&textureDesc, nullptr, &texture), "Create target");
        return texture;
    };
    auto target = newTarget();
    ComPtr<ID3D11RenderTargetView> rtv;
    hr(device->CreateRenderTargetView(target.Get(), nullptr, &rtv), "Create RTV");
    D3D11_TEXTURE2D_DESC stagingDesc = textureDesc;
    stagingDesc.Usage = D3D11_USAGE_STAGING; stagingDesc.BindFlags = 0;
    stagingDesc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    ComPtr<ID3D11Texture2D> staging, backbuffer;
    hr(device->CreateTexture2D(&stagingDesc, nullptr, &staging), "Create staging texture");
    hr(swap->GetBuffer(0, IID_PPV_ARGS(&backbuffer)), "GetBuffer");
    D3D11_RASTERIZER_DESC rasterDesc{};
    rasterDesc.FillMode = D3D11_FILL_SOLID; rasterDesc.CullMode = D3D11_CULL_NONE;
    rasterDesc.DepthClipEnable = TRUE;
    ComPtr<ID3D11RasterizerState> raster;
    hr(device->CreateRasterizerState(&rasterDesc, &raster), "CreateRasterizerState");
    const UINT16 indexData[]{0,1,2};
    D3D11_BUFFER_DESC indexDesc{};
    indexDesc.ByteWidth = sizeof(indexData); indexDesc.Usage = D3D11_USAGE_IMMUTABLE;
    indexDesc.BindFlags = D3D11_BIND_INDEX_BUFFER;
    D3D11_SUBRESOURCE_DATA indexInitial{}; indexInitial.pSysMem = indexData;
    ComPtr<ID3D11Buffer> indices;
    hr(device->CreateBuffer(&indexDesc, &indexInitial, &indices), "Create indices");
    D3D11_QUERY_DESC queryDesc{D3D11_QUERY_EVENT, 0};
    ComPtr<ID3D11Query> event, occlusion;
    hr(device->CreateQuery(&queryDesc, &event), "Create event");
    queryDesc.Query = D3D11_QUERY_OCCLUSION;
    hr(device->CreateQuery(&queryDesc, &occlusion), "Create occlusion query");
    auto bind = [&](ID3D11DeviceContext* ctx, ID3D11PixelShader* ps,
                    ID3D11RenderTargetView* view) {
        ctx->ClearState();
        ctx->OMSetRenderTargets(1, &view, nullptr);
        const D3D11_VIEWPORT viewport{0,0,float(width),float(height),0,1};
        ctx->RSSetViewports(1, &viewport); ctx->RSSetState(raster.Get());
        ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        ctx->IASetIndexBuffer(indices.Get(), DXGI_FORMAT_R16_UINT, 0);
        ctx->VSSetShader(vertex.Get(), nullptr, 0); ctx->PSSetShader(ps, nullptr, 0);
    };
    auto readPixels = [&](ID3D11Texture2D* texture, unsigned left, unsigned right,
                          bool mayOmit, const char* name) {
        context->CopyResource(staging.Get(), texture);
        context->End(event.Get());
        BOOL done = FALSE;
        queryData(context.Get(), event.Get(), &done, sizeof(done));
        check(done == TRUE, "GPU event signaled");
        D3D11_MAPPED_SUBRESOURCE mapped{};
        hr(context->Map(staging.Get(), 0, D3D11_MAP_READ, 0, &mapped), "Map pixels");
        const auto* first = static_cast<const unsigned char*>(mapped.pData);
        const bool omitted = mayOmit && first[0] == 0 && first[1] == 0 && first[2] == 0;
        for (UINT y = 0; y < height; ++y) {
            const auto* row = first + y * mapped.RowPitch;
            for (UINT x = 0; x < width; ++x) {
                unsigned color = omitted ? 0 : (x < width/2 ? left : right);
                for (unsigned c = 0; c < 4; ++c) {
                    unsigned wanted = c == 3 || (color & (1u << c)) ? 255 : 0;
                    if (row[x*4+c] != wanted) {
                        std::fprintf(stderr, "%s pixel(%u,%u)[%u]=%u expected=%u\n",
                            name, x, y, c, row[x*4+c], wanted);
                        check(false, name);
                    }
                }
            }
        }
        context->Unmap(staging.Get(), 0);
        std::printf("%s: %s\n", name, omitted ? "fully omitted" : "exact pixels");
    };
    auto present = [&](ID3D11Texture2D* texture) {
        context->OMSetRenderTargets(0, nullptr, nullptr);
        context->CopyResource(backbuffer.Get(), texture);
        hr(swap->Present(0, 0), "Present");
    };
    enum class Draw { direct, indexed, instanced, indexedInstanced };
    auto draw = [](ID3D11DeviceContext* ctx, Draw kind) {
        if (kind == Draw::direct) ctx->Draw(3, 0);
        else if (kind == Draw::indexed) ctx->DrawIndexed(3, 0, 0);
        else if (kind == Draw::instanced) ctx->DrawInstanced(3, 2, 0, 0);
        else ctx->DrawIndexedInstanced(3, 2, 0, 0, 0);
    };
    auto frame = [&](ID3D11PixelShader* ps, unsigned color, Draw kind,
                     bool guarded, bool mayOmit, const char* name) {
        bind(context.Get(), ps, rtv.Get());
        context->ClearRenderTargetView(rtv.Get(), black);
        if (guarded) context->Begin(occlusion.Get());
        draw(context.Get(), kind);
        if (guarded) {
            context->End(occlusion.Get());
            UINT64 samples = 0;
            queryData(context.Get(), occlusion.Get(), &samples, sizeof(samples));
            const UINT copies = kind == Draw::instanced || kind == Draw::indexedInstanced ? 2 : 1;
            check(samples == UINT64(width) * height * copies, "exact occlusion sample count");
        }
        readPixels(target.Get(), color, color, mayOmit, name);
        present(target.Get());
    };
    auto warm = pixelShader(1);
    for (unsigned i = 0; i < 8; ++i)
        frame(warm.Get(), 1, Draw::direct, true, false, "persistent target Present epoch");
    const Draw kinds[]{Draw::direct, Draw::indexed, Draw::instanced, Draw::indexedInstanced};
    for (unsigned i = 0; i < 4; ++i) {
        auto cold = pixelShader(i + 2);
        frame(cold.Get(), i + 2, kinds[i], false, true, "cold eligible pipeline");
        frame(cold.Get(), i + 2, kinds[i], true, false, "active query exact fallback");
        frame(cold.Get(), i + 2, kinds[i], false, false, "ready pipeline after guarded draw");
    }
    // A fresh target must not lose its only draw, even without a query guard.
    context->ClearState(); rtv.Reset(); target = newTarget();
    hr(device->CreateRenderTargetView(target.Get(), nullptr, &rtv), "Create first-use RTV");
    auto firstUse = pixelShader(6);
    frame(firstUse.Get(), 6, Draw::direct, false, false, "first-use target");
    if (graphicsUav) {
        for (unsigned i = 0; i < 8; ++i)
            frame(warm.Get(), 1, Draw::direct, true, false, "persistent target before UAV");
        D3D11_BUFFER_DESC desc{};
        desc.ByteWidth = sizeof(UINT); desc.Usage = D3D11_USAGE_DEFAULT;
        desc.BindFlags = D3D11_BIND_UNORDERED_ACCESS;
        desc.MiscFlags = D3D11_RESOURCE_MISC_BUFFER_STRUCTURED;
        desc.StructureByteStride = sizeof(UINT);
        ComPtr<ID3D11Buffer> counter, counterReadback;
        hr(device->CreateBuffer(&desc, nullptr, &counter), "Create UAV counter");
        desc.Usage = D3D11_USAGE_STAGING; desc.BindFlags = 0;
        desc.MiscFlags = 0; desc.StructureByteStride = 0; desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        hr(device->CreateBuffer(&desc, nullptr, &counterReadback), "Create counter readback");
        D3D11_UNORDERED_ACCESS_VIEW_DESC uavDesc{};
        uavDesc.ViewDimension = D3D11_UAV_DIMENSION_BUFFER; uavDesc.Buffer.NumElements = 1;
        ComPtr<ID3D11UnorderedAccessView> uav;
        hr(device->CreateUnorderedAccessView(counter.Get(), &uavDesc, &uav), "Create graphics UAV");
        auto ps = pixelShader(7, true);
        bind(context.Get(), ps.Get(), rtv.Get());
        const UINT zero[4]{};
        context->ClearUnorderedAccessViewUint(uav.Get(), zero);
        context->ClearRenderTargetView(rtv.Get(), black);
        ID3D11RenderTargetView* view = rtv.Get();
        ID3D11UnorderedAccessView* access = uav.Get();
        context->OMSetRenderTargetsAndUnorderedAccessViews(1, &view, nullptr, 1, 1, &access, nullptr);
        context->Draw(3, 0); // No query: UAV side effects alone require the safe path.
        access = nullptr;
        context->OMSetRenderTargetsAndUnorderedAccessViews(0, nullptr, nullptr, 1, 1, &access, nullptr);
        context->CopyResource(counterReadback.Get(), counter.Get());
        readPixels(target.Get(), 7, 7, false, "cold graphics UAV fallback");
        D3D11_MAPPED_SUBRESOURCE mapped{};
        hr(context->Map(counterReadback.Get(), 0, D3D11_MAP_READ, 0, &mapped), "Map UAV counter");
        const UINT writes = *static_cast<const UINT*>(mapped.pData);
        context->Unmap(counterReadback.Get(), 0);
        check(writes == width * height, "exact graphics UAV side effects");
        present(target.Get());
    } else {
        std::puts("graphics UAV: unsupported below FL11_0; remaining scenarios are required");
    }
    ComPtr<ID3D11DeviceContext> deferred;
    hr(device->CreateDeferredContext(0, &deferred), "CreateDeferredContext");
    auto deferredBytes = compile("cbuffer Color:register(b0){float4 color;}"
        "float4 main():SV_Target{return color;}", psProfile);
    ComPtr<ID3D11PixelShader> deferredPixel;
    hr(device->CreatePixelShader(deferredBytes->GetBufferPointer(), deferredBytes->GetBufferSize(),
        nullptr, &deferredPixel), "Create deferred pixel shader");
    ComPtr<ID3D11Buffer> colors[2], retainedIndices;
    const float colorData[2][4]{{1,0,0,1}, {0,1,0,1}};
    for (unsigned i = 0; i < 2; ++i) {
        D3D11_BUFFER_DESC desc{};
        desc.ByteWidth = sizeof(colorData[i]); desc.Usage = D3D11_USAGE_IMMUTABLE;
        desc.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
        D3D11_SUBRESOURCE_DATA initial{}; initial.pSysMem = colorData[i];
        hr(device->CreateBuffer(&desc, &initial, &colors[i]), "Create retained color buffer");
    }
    hr(device->CreateBuffer(&indexDesc, &indexInitial, &retainedIndices), "Create retained indices");
    bind(deferred.Get(), deferredPixel.Get(), rtv.Get());
    deferred->IASetIndexBuffer(retainedIndices.Get(), DXGI_FORMAT_R16_UINT, 0);
    ID3D11Buffer* coldColor = colors[0].Get();
    deferred->PSSetConstantBuffers(0, 1, &coldColor);
    deferred->DrawIndexed(3, 0, 0);
    ComPtr<ID3D11CommandList> coldCommands;
    hr(deferred->FinishCommandList(FALSE, &coldCommands), "Finish cold deferred command list");
    context->ClearRenderTargetView(rtv.Get(), black);
    context->ExecuteCommandList(coldCommands.Get(), TRUE);
    readPixels(target.Get(), 1, 1, true, "cold deferred pipeline");

    bind(deferred.Get(), deferredPixel.Get(), rtv.Get());
    deferred->IASetIndexBuffer(retainedIndices.Get(), DXGI_FORMAT_R16_UINT, 0);
    deferred->Begin(occlusion.Get());
    for (unsigned i = 0; i < 2; ++i) {
        const D3D11_VIEWPORT viewport{float(i*width/2),0,float(width/2),float(height),0,1};
        deferred->RSSetViewports(1, &viewport);
        ID3D11Buffer* color = colors[i].Get();
        deferred->PSSetConstantBuffers(0, 1, &color);
        deferred->DrawIndexed(3, 0, 0);
    }
    deferred->End(occlusion.Get());
    ComPtr<ID3D11CommandList> commands;
    hr(deferred->FinishCommandList(FALSE, &commands), "FinishCommandList");
    // The command list, not application owners or deferred state, must retain these.
    deferred->ClearState(); deferred.Reset(); deferredPixel.Reset();
    colors[0].Reset(); colors[1].Reset(); retainedIndices.Reset();
    for (unsigned replay = 0; replay < 2; ++replay) {
        bind(context.Get(), warm.Get(), rtv.Get());
        context->ClearRenderTargetView(rtv.Get(), black);
        const BOOL restore = replay == 0 ? TRUE : FALSE;
        context->ExecuteCommandList(commands.Get(), restore);
        ComPtr<ID3D11PixelShader> observed;
        context->PSGetShader(&observed, nullptr, nullptr);
        check(observed.Get() == (restore ? warm.Get() : nullptr), "ExecuteCommandList state restoration");
        UINT64 samples = 0;
        queryData(context.Get(), occlusion.Get(), &samples, sizeof(samples));
        check(samples == UINT64(width) * height, "deferred query exact sample count");
        readPixels(target.Get(), 1, 2, false, "deferred replay retained resources and viewport/CB changes");
        context->ClearRenderTargetView(rtv.Get(), black);
        context->ExecuteCommandList(coldCommands.Get(), TRUE);
        readPixels(target.Get(), 1, 1, false, "ready deferred pipeline after guarded replay");
        present(target.Get());
    }
    context->ClearState(); context->Flush();
    coldCommands.Reset(); commands.Reset(); backbuffer.Reset(); swap.Reset();
    DestroyWindow(window);
    std::puts("D3DMETAL_DX11_ASYNC_PUBLIC_SMOKE_PASS");
    return 0;
}
