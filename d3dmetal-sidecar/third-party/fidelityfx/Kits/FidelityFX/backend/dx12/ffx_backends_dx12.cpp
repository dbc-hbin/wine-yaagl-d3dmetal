// This file is part of the FidelityFX SDK.
//
// Copyright (C) 2025 Advanced Micro Devices, Inc.
// 
// Permission is hereby granted, free of charge, to any person obtaining a copy
// of this software and associated documentation files(the "Software"), to deal
// in the Software without restriction, including without limitation the rights
// to use, copy, modify, merge, publish, distribute, sublicense, and /or sell
// copies of the Software, and to permit persons to whom the Software is
// furnished to do so, subject to the following conditions :
//
// The above copyright notice and this permission notice shall be included in
// all copies or substantial portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
// IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
// AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
// OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
// THE SOFTWARE.

#include "../../api/include/dx12/ffx_api_dx12.h"
#include "../../api/internal/ffx_backends.h"

#if defined(FFX_FRAMEGENERATION)
#include "../../framegeneration/include/ffx_framegeneration.h"
#include "../../framegeneration/include/dx12/ffx_api_framegeneration_dx12.h"
#endif // defined(FFX_FRAMEGENERATION)
#if defined(FFX_UPSCALER)
#include "../../upscalers/include/ffx_upscale.h"
#endif // defined(FFX_UPSCALER)
#if defined(FFX_DENOISER)
#include "../../denoisers/include/ffx_denoiser.h"
#endif // defined(FFX_DENOISER)
#if defined(FFX_RADIANCECACHE)
#include "../../radiancecache/include/ffx_radiancecache.h"
#endif // defined(FFX_RADIANCECACHE)
#if defined(FFX_AMBIENTOCCLUSION)
#include "../../ambientocclusion/include/ffx_ambientocclusion.h"
#endif // defined(FFX_AMBIENTOCCLUSION)

#include "ffx_dx12.h"

ffxReturnCode_t CreateBackend(const ffxCreateContextDescHeader *desc, bool& backendFound, FfxInterface *iface, size_t contexts, Allocator& alloc, const BackendAllocationCallbacks& callbacks)
{
    const ffxCreateBackendDX12Desc* backendDesc = nullptr;
    for (const auto* it = desc->pNext; it; it = it->pNext)
    {
        if (it->type == FFX_API_CREATE_CONTEXT_DESC_TYPE_BACKEND_DX12)
        {
            if (backendFound || backendDesc)
                return FFX_API_RETURN_ERROR;
            backendDesc = reinterpret_cast<const ffxCreateBackendDX12Desc*>(it);
        }
    }
    if (!backendDesc)
        return FFX_API_RETURN_OK;

    const size_t scratchBufferSize = ffxGetScratchMemorySizeDX12(contexts);
    if (!scratchBufferSize)
        return FFX_API_RETURN_ERROR_MEMORY;
    void* scratchBuffer = nullptr;
    try
    {
        scratchBuffer = alloc.alloc(scratchBufferSize);
        if (!scratchBuffer)
            return FFX_API_RETURN_ERROR_MEMORY;
        memset(scratchBuffer, 0, scratchBufferSize);
        FfxInterface candidate = {};
        const FfxErrorCode error = ffxGetInterfaceDX12WithCallbacks(
            &candidate, backendDesc->device, scratchBuffer, scratchBufferSize, contexts, callbacks);
        if (error != FFX_OK)
        {
            void* failedScratch = scratchBuffer;
            scratchBuffer = nullptr;
            alloc.dealloc(failedScratch);
            return FFX_API_RETURN_ERROR_RUNTIME_ERROR;
        }
        *iface = candidate;
        backendFound = true;
    }
    catch (...)
    {
        if (scratchBuffer)
            alloc.dealloc(scratchBuffer);
        return FFX_API_RETURN_ERROR_RUNTIME_ERROR;
    }
    return FFX_API_RETURN_OK;
}

void* GetDevice(const ffxApiHeader* desc)
{
    for (const auto* it = desc; it; it = it->pNext)
    {
        switch (it->type)
        {
        case FFX_API_QUERY_DESC_TYPE_GET_VERSIONS:
        {
            return reinterpret_cast<const ffxQueryDescGetVersions*>(it)->device;
        }
#if defined(FFX_UPSCALER)
        case FFX_API_QUERY_DESC_TYPE_UPSCALE_GPU_MEMORY_USAGE_V2:
        {
            return reinterpret_cast<const ffxQueryDescUpscaleGetGPUMemoryUsageV2*>(it)->device;
        }
#endif //#if defined(FFX_UPSCALER)
#if defined(FFX_DENOISER)
        case FFX_API_QUERY_DESC_TYPE_DENOISER_GPU_MEMORY_USAGE:
        {
            return reinterpret_cast<const ffxQueryDescDenoiserGetGPUMemoryUsage*>(it)->device;
        }
        case FFX_API_QUERY_DESC_TYPE_DENOISER_GET_VERSION:
        {
            return reinterpret_cast<const ffxQueryDescDenoiserGetVersion*>(it)->device;
        }
        case FFX_API_QUERY_DESC_TYPE_DENOISER_GET_DEFAULT_SETTINGS:
        {
            return reinterpret_cast<const ffxQueryDescDenoiserGetDefaultSettings*>(it)->device;
        }
#endif //#if defined(FFX_DENOISER)
#if defined(FFX_AMBIENTOCCLUSION)
        case FFX_API_QUERY_DESC_TYPE_AMBIENTOCCLUSION_GPU_MEMORY_USAGE:
        {
            return reinterpret_cast<const ffxQueryDescAmbientOcclusionGetGPUMemoryUsage*>(it)->device;
        }
#endif //#if defined(FFX_AMBIENTOCCLUSION)
        case FFX_API_CREATE_CONTEXT_DESC_TYPE_BACKEND_DX12:
        {
            return reinterpret_cast<const ffxCreateBackendDX12Desc*>(it)->device;
        }
#if defined(FFX_FRAMEGENERATION)
        case FFX_API_CREATE_CONTEXT_DESC_TYPE_FRAMEGENERATIONSWAPCHAIN_FOR_HWND_DX12:
        {
            ID3D12Device* device = nullptr;
            reinterpret_cast<const ffxCreateContextDescFrameGenerationSwapChainForHwndDX12*>(it)->gameQueue->GetDevice(IID_PPV_ARGS(&device));
            device->Release();
            return device;
        }
        case FFX_API_CREATE_CONTEXT_DESC_TYPE_FRAMEGENERATIONSWAPCHAIN_NEW_DX12:
        {
            ID3D12Device* device = nullptr;
            reinterpret_cast<const ffxCreateContextDescFrameGenerationSwapChainNewDX12*>(it)->gameQueue->GetDevice(IID_PPV_ARGS(&device));
            device->Release();
            return device;
        }
        case FFX_API_CREATE_CONTEXT_DESC_TYPE_FRAMEGENERATIONSWAPCHAIN_WRAP_DX12:
        {
            ID3D12Device* device = nullptr;
            reinterpret_cast<const ffxCreateContextDescFrameGenerationSwapChainWrapDX12*>(it)->gameQueue->GetDevice(IID_PPV_ARGS(&device));
            device->Release();
            return device;
        }
        case FFX_API_QUERY_DESC_TYPE_FRAMEGENERATION_GPU_MEMORY_USAGE_V2:
        {
            return reinterpret_cast<const ffxQueryDescFrameGenerationGetGPUMemoryUsageV2*>(it)->device;
        }
        case FFX_API_QUERY_DESC_TYPE_FRAMEGENERATIONSWAPCHAIN_GPU_MEMORY_USAGE_DX12_V2:
        {
            return reinterpret_cast<const ffxQueryFrameGenerationSwapChainGetGPUMemoryUsageDX12V2*>(it)->device;
        }
#endif // defined(FFX_FRAMEGENERATION)
        }
    }
    return nullptr;
}
