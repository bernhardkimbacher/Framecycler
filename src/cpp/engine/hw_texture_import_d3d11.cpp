#include "hw_texture_import.h"

#if defined(_WIN32)

#define WIN32_LEAN_AND_MEAN
#include <d3d11.h>
#include <d3dcompiler.h>
#include <dxgi.h>

#include <cstring>
#include <mutex>
#include <string>

namespace {

std::mutex g_share_mutex;
ID3D11Device* g_shared_device = nullptr;
ID3D11DeviceContext* g_shared_context = nullptr;

struct D3D11ImportImpl {
    ID3D11Device* device = nullptr;
    ID3D11DeviceContext* context = nullptr;
    ID3D11ComputeShader* nv12_cs = nullptr;
    ID3D11ComputeShader* p010_cs = nullptr;
};

static const char* kNv12Shader = R"(
Texture2D<float> Luma : register(t0);
Texture2D<float2> Chroma : register(t1);
RWTexture2D<float4> Dest : register(u0);

[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID)
{
    uint w, h;
    Dest.GetDimensions(w, h);
    if (id.x >= w || id.y >= h) return;
    float y = Luma.Load(int3(id.xy, 0));
    float2 cbcr = Chroma.Load(int3(id.xy / 2, 0));
    float cb = cbcr.x - 0.5f;
    float cr = cbcr.y - 0.5f;
    float r = y + 1.5748f * cr;
    float g = y - 0.1873f * cb - 0.4681f * cr;
    float b = y + 1.8556f * cb;
    Dest[id.xy] = float4(saturate(r), saturate(g), saturate(b), 1.0f);
}
)";

static const char* kP010Shader = R"(
Texture2D<float> Luma : register(t0);
Texture2D<float2> Chroma : register(t1);
RWTexture2D<float4> Dest : register(u0);

[numthreads(16, 16, 1)]
void main(uint3 id : SV_DispatchThreadID)
{
    uint w, h;
    Dest.GetDimensions(w, h);
    if (id.x >= w || id.y >= h) return;
    // P010 stores 10-bit in the high bits of 16-bit samples.
    float y = Luma.Load(int3(id.xy, 0));
    float2 cbcr = Chroma.Load(int3(id.xy / 2, 0));
    float cb = cbcr.x - 0.5f;
    float cr = cbcr.y - 0.5f;
    float r = y + 1.5748f * cr;
    float g = y - 0.1873f * cb - 0.4681f * cr;
    float b = y + 1.8556f * cb;
    Dest[id.xy] = float4(saturate(r), saturate(g), saturate(b), 1.0f);
}
)";

ID3D11ComputeShader* compile_cs(ID3D11Device* device, const char* src, std::string* error)
{
    ID3DBlob* blob = nullptr;
    ID3DBlob* errors = nullptr;
    const HRESULT hr = D3DCompile(
        src,
        std::strlen(src),
        nullptr,
        nullptr,
        nullptr,
        "main",
        "cs_5_0",
        D3DCOMPILE_ENABLE_STRICTNESS,
        0,
        &blob,
        &errors);
    if (FAILED(hr) || !blob) {
        if (error) {
            if (errors) {
                *error = static_cast<const char*>(errors->GetBufferPointer());
            } else {
                *error = "D3DCompile failed for HW import compute shader";
            }
        }
        if (errors) {
            errors->Release();
        }
        return nullptr;
    }
    if (errors) {
        errors->Release();
    }
    ID3D11ComputeShader* cs = nullptr;
    const HRESULT chr = device->CreateComputeShader(
        blob->GetBufferPointer(), blob->GetBufferSize(), nullptr, &cs);
    blob->Release();
    if (FAILED(chr) || !cs) {
        if (error) {
            *error = "CreateComputeShader failed";
        }
        return nullptr;
    }
    return cs;
}

bool create_plane_srvs(
    ID3D11Device* device,
    ID3D11Texture2D* tex,
    DXGI_FORMAT y_fmt,
    DXGI_FORMAT uv_fmt,
    ID3D11ShaderResourceView** y_srv,
    ID3D11ShaderResourceView** uv_srv,
    std::string* error)
{
    D3D11_SHADER_RESOURCE_VIEW_DESC yDesc{};
    yDesc.Format = y_fmt;
    yDesc.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D;
    yDesc.Texture2D.MostDetailedMip = 0;
    yDesc.Texture2D.MipLevels = 1;
    if (FAILED(device->CreateShaderResourceView(tex, &yDesc, y_srv)) || !*y_srv) {
        if (error) {
            *error = "CreateShaderResourceView (Y) failed";
        }
        return false;
    }

    D3D11_SHADER_RESOURCE_VIEW_DESC uvDesc{};
    uvDesc.Format = uv_fmt;
    uvDesc.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D;
    uvDesc.Texture2D.MostDetailedMip = 0;
    uvDesc.Texture2D.MipLevels = 1;
    if (FAILED(device->CreateShaderResourceView(tex, &uvDesc, uv_srv)) || !*uv_srv) {
        (*y_srv)->Release();
        *y_srv = nullptr;
        if (error) {
            *error = "CreateShaderResourceView (UV) failed";
        }
        return false;
    }
    return true;
}

} // namespace

void fc_d3d11_set_shared_device(void* device, void* context)
{
    std::lock_guard<std::mutex> lock(g_share_mutex);
    if (g_shared_device) {
        g_shared_device->Release();
        g_shared_device = nullptr;
    }
    if (g_shared_context) {
        g_shared_context->Release();
        g_shared_context = nullptr;
    }
    g_shared_device = static_cast<ID3D11Device*>(device);
    g_shared_context = static_cast<ID3D11DeviceContext*>(context);
    if (g_shared_device) {
        g_shared_device->AddRef();
    }
    if (g_shared_context) {
        g_shared_context->AddRef();
    }
}

void fc_d3d11_clear_shared_device()
{
    fc_d3d11_set_shared_device(nullptr, nullptr);
}

bool fc_d3d11_get_shared_device(void** device_out, void** context_out)
{
    std::lock_guard<std::mutex> lock(g_share_mutex);
    if (!g_shared_device) {
        return false;
    }
    if (device_out) {
        *device_out = g_shared_device;
    }
    if (context_out) {
        *context_out = g_shared_context;
    }
    return true;
}

bool fc_d3d11_create_import_context(HwD3D11ImportContext* ctx, std::string* error)
{
    if (!ctx || !ctx->device || !ctx->context) {
        if (error) {
            *error = "null D3D11 device/context";
        }
        return false;
    }
    fc_d3d11_release_import_context(ctx);

    auto* device = static_cast<ID3D11Device*>(ctx->device);
    auto* context = static_cast<ID3D11DeviceContext*>(ctx->context);
    auto* impl = new D3D11ImportImpl();
    impl->device = device;
    impl->context = context;
    device->AddRef();
    context->AddRef();

    impl->nv12_cs = compile_cs(device, kNv12Shader, error);
    if (!impl->nv12_cs) {
        ctx->impl = impl;
        fc_d3d11_release_import_context(ctx);
        return false;
    }
    impl->p010_cs = compile_cs(device, kP010Shader, error);
    if (!impl->p010_cs) {
        ctx->impl = impl;
        fc_d3d11_release_import_context(ctx);
        return false;
    }

    ctx->impl = impl;
    return true;
}

void fc_d3d11_release_import_context(HwD3D11ImportContext* ctx)
{
    if (!ctx || !ctx->impl) {
        return;
    }
    auto* impl = static_cast<D3D11ImportImpl*>(ctx->impl);
    if (impl->nv12_cs) {
        impl->nv12_cs->Release();
    }
    if (impl->p010_cs) {
        impl->p010_cs->Release();
    }
    if (impl->device) {
        impl->device->Release();
    }
    if (impl->context) {
        impl->context->Release();
    }
    delete impl;
    ctx->impl = nullptr;
}

bool fc_d3d11_import_texture_to_rgba16f(
    HwD3D11ImportContext* ctx,
    void* src_texture_2d,
    int array_index,
    void* dst_texture_rgba16f,
    int width,
    int height,
    std::string* error)
{
    if (!ctx || !ctx->impl || !src_texture_2d || !dst_texture_rgba16f
        || width <= 0 || height <= 0 || array_index < 0) {
        if (error) {
            *error = "invalid D3D11 import arguments";
        }
        return false;
    }

    auto* impl = static_cast<D3D11ImportImpl*>(ctx->impl);
    auto* device = impl->device;
    auto* context = impl->context;
    auto* src = static_cast<ID3D11Texture2D*>(src_texture_2d);
    auto* dst = static_cast<ID3D11Texture2D*>(dst_texture_rgba16f);

    D3D11_TEXTURE2D_DESC srcDesc{};
    src->GetDesc(&srcDesc);
    if (array_index >= static_cast<int>(srcDesc.ArraySize)) {
        if (error) {
            *error = "D3D11VA array index out of range";
        }
        return false;
    }

    const bool is_nv12 = (srcDesc.Format == DXGI_FORMAT_NV12);
    const bool is_p010 = (srcDesc.Format == DXGI_FORMAT_P010);
    if (!is_nv12 && !is_p010) {
        if (error) {
            *error = "Unsupported D3D11VA texture format (need NV12 or P010)";
        }
        return false;
    }

    // Copy decoder slice into a single-layer SRV-capable texture (decoder
    // pools are often BIND_DECODER-only).
    D3D11_TEXTURE2D_DESC copyDesc = srcDesc;
    copyDesc.Width = static_cast<UINT>(width);
    copyDesc.Height = static_cast<UINT>(height);
    copyDesc.MipLevels = 1;
    copyDesc.ArraySize = 1;
    copyDesc.SampleDesc.Count = 1;
    copyDesc.SampleDesc.Quality = 0;
    copyDesc.Usage = D3D11_USAGE_DEFAULT;
    copyDesc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    copyDesc.CPUAccessFlags = 0;
    copyDesc.MiscFlags = 0;

    ID3D11Texture2D* copy_tex = nullptr;
    if (FAILED(device->CreateTexture2D(&copyDesc, nullptr, &copy_tex)) || !copy_tex) {
        if (error) {
            *error = "CreateTexture2D (NV12/P010 copy) failed";
        }
        return false;
    }

    context->CopySubresourceRegion(
        copy_tex,
        0,
        0,
        0,
        0,
        src,
        static_cast<UINT>(array_index),
        nullptr);

    ID3D11ShaderResourceView* y_srv = nullptr;
    ID3D11ShaderResourceView* uv_srv = nullptr;
    const DXGI_FORMAT y_fmt = is_nv12 ? DXGI_FORMAT_R8_UNORM : DXGI_FORMAT_R16_UNORM;
    const DXGI_FORMAT uv_fmt = is_nv12 ? DXGI_FORMAT_R8G8_UNORM : DXGI_FORMAT_R16G16_UNORM;
    if (!create_plane_srvs(device, copy_tex, y_fmt, uv_fmt, &y_srv, &uv_srv, error)) {
        copy_tex->Release();
        return false;
    }

    D3D11_UNORDERED_ACCESS_VIEW_DESC uavDesc{};
    uavDesc.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;
    uavDesc.ViewDimension = D3D11_UAV_DIMENSION_TEXTURE2D;
    uavDesc.Texture2D.MipSlice = 0;
    ID3D11UnorderedAccessView* uav = nullptr;
    if (FAILED(device->CreateUnorderedAccessView(dst, &uavDesc, &uav)) || !uav) {
        y_srv->Release();
        uv_srv->Release();
        copy_tex->Release();
        if (error) {
            *error = "CreateUnorderedAccessView (RGBA16F) failed";
        }
        return false;
    }

    ID3D11ComputeShader* cs = is_nv12 ? impl->nv12_cs : impl->p010_cs;
    context->CSSetShader(cs, nullptr, 0);
    ID3D11ShaderResourceView* srvs[2] = {y_srv, uv_srv};
    context->CSSetShaderResources(0, 2, srvs);
    context->CSSetUnorderedAccessViews(0, 1, &uav, nullptr);

    const UINT groups_x = (static_cast<UINT>(width) + 15u) / 16u;
    const UINT groups_y = (static_cast<UINT>(height) + 15u) / 16u;
    context->Dispatch(groups_x, groups_y, 1);

    ID3D11ShaderResourceView* null_srv[2] = {nullptr, nullptr};
    ID3D11UnorderedAccessView* null_uav[1] = {nullptr};
    context->CSSetShaderResources(0, 2, null_srv);
    context->CSSetUnorderedAccessViews(0, 1, null_uav, nullptr);
    context->CSSetShader(nullptr, nullptr, 0);

    uav->Release();
    y_srv->Release();
    uv_srv->Release();
    copy_tex->Release();
    return true;
}

#endif // _WIN32
