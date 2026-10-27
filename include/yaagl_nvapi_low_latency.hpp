#pragma once

#include <cstddef>
#include <cstdint>

namespace yaagl::nvapi {

// Wire layouts and interface IDs from NVIDIA NVAPI SDK 61683:
// https://github.com/NVIDIA/nvapi/blob/main/nvapi.h
// https://github.com/NVIDIA/nvapi/blob/main/nvapi_interface.h
// NvBool is an unsigned byte; these are not native C++ bool ABI fields.
inline constexpr std::uint32_t GetSleepStatusId = 0xaef96ca1;
inline constexpr std::uint32_t SetSleepModeId = 0xac1ca9e0;
inline constexpr std::uint32_t SleepId = 0x852cd1d2;
inline constexpr std::uint32_t SetLatencyMarkerId = 0xd9984c05;
inline constexpr std::int32_t Ok = 0;
inline constexpr std::int32_t InvalidArgument = -5;
inline constexpr std::int32_t NotSupported = -104;

template<class T> constexpr std::uint32_t version() { return sizeof(T) | (1u << 16); }

struct SleepMode {
    std::uint32_t version;
    std::uint8_t lowLatencyMode;
    std::uint8_t lowLatencyBoost;
    std::uint32_t minimumIntervalUs;
    std::uint8_t useMarkersToOptimize;
    std::uint8_t useMinQueueTime;
    std::uint8_t reserved[30];
};
struct SleepStatus {
    std::uint32_t version;
    std::uint8_t lowLatencyMode;
    std::uint8_t fsVrr;
    std::uint8_t cplVsyncOn;
    std::uint32_t sleepIntervalUs;
    std::uint8_t useGameSleep;
    std::uint8_t fullscreenIFlip;
    std::uint8_t fgMultiplier;
    std::uint8_t dfgControl;
    std::uint32_t dfgFrameTimeTargetUs;
    std::uint8_t reserved[114];
};
enum MarkerType : std::uint32_t {
    SimulationStart = 0, SimulationEnd = 1,
    RenderSubmitStart = 2, RenderSubmitEnd = 3,
    PresentStart = 4, PresentEnd = 5, InputSample = 6,
    OutOfBandRenderSubmitStart = 9, OutOfBandRenderSubmitEnd = 10,
    OutOfBandPresentStart = 11, OutOfBandPresentEnd = 12,
};
struct LatencyMarker {
    std::uint32_t version;
    std::uint64_t frameId;
    MarkerType markerType;
    std::uint64_t reserved0;
    std::uint8_t reserved[56];
};
static_assert(sizeof(SleepMode) == 44 && offsetof(SleepMode, minimumIntervalUs) == 8);
static_assert(sizeof(SleepStatus) == 136 && offsetof(SleepStatus, sleepIntervalUs) == 8);
static_assert(sizeof(LatencyMarker) == 88 && offsetof(LatencyMarker, markerType) == 16);

using QueryInterface = void* (__attribute__((ms_abi)) *)(std::uint32_t);
using Sleep = std::int32_t (__attribute__((ms_abi)) *)(void*);
using SetSleepMode = std::int32_t (__attribute__((ms_abi)) *)(void*, SleepMode*);
using GetSleepStatus = std::int32_t (__attribute__((ms_abi)) *)(void*, SleepStatus*);
using SetLatencyMarker = std::int32_t (__attribute__((ms_abi)) *)(void*, LatencyMarker*);

} // namespace yaagl::nvapi
