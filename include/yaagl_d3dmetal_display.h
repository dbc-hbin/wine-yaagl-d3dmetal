/* Private SysV contract between Wine's macdrv Unix module and D3DMetal sidecar. */
#pragma once

#include <stdint.h>

struct yaagl_d3dmetal_display
{
    uintptr_t monitor;
    uint32_t refresh_rate;
    int32_t left;
    int32_t top;
    int32_t right;
    int32_t bottom;
};

#ifdef __cplusplus
extern "C" {
#endif

/* A nonzero monitor_override selects an explicit fullscreen output. */
int macdrv_query_d3dmetal_display(uintptr_t window, uintptr_t monitor_override,
                                 struct yaagl_d3dmetal_display *out);

#ifdef __cplusplus
}
#endif
