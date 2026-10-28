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

#pragma once
#include "../include/ffx_api.hpp"
#include "../include/ffx_api_types.h"
#include "ffx_internal_types.h"
#include "ffx_api_helper.h"

class ffxProvider
{
public:
    ffxProvider(uint64_t i_id, uint64_t i_effect_id, const char* i_version_name)
        : RefCount(1), Id(i_id), EffectId(i_effect_id), VersionName(i_version_name)
    {
    }

    virtual ~ffxProvider() = default;

    size_t GetRefCount() const
    {
        return RefCount;
    }

    uint64_t GetId() const
    {
        return Id;
    }

    const char* GetVersionName() const
    {
        return VersionName;
    }

    virtual bool CanProvide(uint64_t descType) const
    {
        return (descType & FFX_API_EFFECT_MASK) == EffectId;
    }

    virtual bool IsSupported(void* device) const
    {
        (void)device;
        // most providers have no special device requirements
        return true;
    }

    virtual ffxReturnCode_t CreateContext(ffxContext* context, ffxCreateContextDescHeader* desc, Allocator& alloc) = 0;

    virtual ffxReturnCode_t DestroyContext(ffxContext* context, Allocator& alloc) = 0;

    virtual ffxReturnCode_t Configure(ffxContext* context, const ffxConfigureDescHeader* desc) const = 0;

    virtual ffxReturnCode_t Query(ffxContext* context, ffxQueryDescHeader* desc) const = 0;

    virtual ffxReturnCode_t Dispatch(ffxContext* context, const ffxDispatchDescHeader* desc) const = 0;

protected:
    size_t RefCount = 0;
    uint64_t Id;
    uint64_t EffectId;
    const char* VersionName;
};

struct InternalContextHeader
{
    ffxProvider* provider;
};

inline ffxProvider* GetAssociatedProvider(ffxContext context)
{
    return reinterpret_cast<InternalContextHeader*>(context)->provider;
}
// Provider selection and external-provider discovery remain in the pinned native DLL.
