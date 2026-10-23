/* FSR 4 API to MetalFX bridge. */
#if 0
#pragma makedep unix
#endif

#include "config.h"
#include <dlfcn.h>
#include <limits.h>
#include <stdint.h>
#include <pthread.h>
#include <string.h>

#include "ntstatus.h"
#include "unixlib.h"

typedef uint32_t (*yaagl_fsr_api_fn)(uint32_t operation, void *arguments);

static void *sidecar_handle;
static yaagl_fsr_api_fn sidecar_api;
static pthread_mutex_t sidecar_mutex = PTHREAD_MUTEX_INITIALIZER;

static yaagl_fsr_api_fn load_sidecar(void)
{
    yaagl_fsr_api_fn api;
    Dl_info info;
    char path[PATH_MAX];
    char *slash;

    if ((api = __atomic_load_n(&sidecar_api, __ATOMIC_ACQUIRE))) return api;
    pthread_mutex_lock(&sidecar_mutex);
    if ((api = __atomic_load_n(&sidecar_api, __ATOMIC_ACQUIRE))) goto done;
    if (!dladdr((const void *)&load_sidecar, &info) || !info.dli_fname) goto done;
    if (strlen(info.dli_fname) + sizeof("/../../external/D3DMetal.framework/Versions/A/Resources/libD3DMetalSidecar.dylib") >= sizeof(path)) goto done;
    strcpy(path, info.dli_fname);
    slash = strrchr(path, '/');
    if (!slash) goto done;
    strcpy(slash + 1, "../../external/D3DMetal.framework/Versions/A/Resources/libD3DMetalSidecar.dylib");
    sidecar_handle = dlopen(path, RTLD_NOW | RTLD_NOLOAD);
    if (!sidecar_handle) goto done;
    api = (yaagl_fsr_api_fn)dlsym(sidecar_handle, "yaagl_fsr_api");
    if (api)
    {
        /* Publish only success; the retained handle keeps the function alive. */
        __atomic_store_n(&sidecar_api, api, __ATOMIC_RELEASE);
    }
    else
    {
        dlclose(sidecar_handle);
        sidecar_handle = NULL;
    }
done:
    pthread_mutex_unlock(&sidecar_mutex);
    return api;
}

static NTSTATUS fsr_api(void *args)
{
    struct yaagl_fsr_packet_header *header = args;
    yaagl_fsr_api_fn api;
    uint32_t operation;

    if (!header || header->size < sizeof(*header)) return STATUS_INVALID_PARAMETER;
    operation = header->operation;
    api = load_sidecar();
    if (!api)
    {
        header->result = 3; /* FFX_API_RETURN_ERROR_RUNTIME_ERROR */
        return STATUS_DLL_NOT_FOUND;
    }
    header->result = api(operation, args);
    return STATUS_SUCCESS;
}

const unixlib_entry_t __wine_unix_call_funcs[] =
{
    fsr_api,
};

C_ASSERT(ARRAYSIZE(__wine_unix_call_funcs) == unix_fsr_funcs_count);
