#ifndef YAAGL_FSR3_LIFETIME_H
#define YAAGL_FSR3_LIFETIME_H

/* Private PE component, packaged beside the original native FFX DLL. */
#define YAAGL_FSR3_LIFETIME_DLL L"yaagl_fsr3_lifetime.dll"

enum yaagl_fsr3_install_result
{
    YAAGL_FSR3_INSTALL_ERROR = -1,
    YAAGL_FSR3_INSTALL_UNSUPPORTED = 0,
    YAAGL_FSR3_INSTALL_OK = 1
};

typedef int (*yaagl_fsr3_install_func)(void *native_module);

#endif
