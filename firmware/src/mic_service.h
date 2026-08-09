#pragma once
#include <stdint.h>

struct MicRuntimeStatus {
    bool enabled = false;
    bool running = false;
    float lastRms = 0.0f;
    float recentPeakRms = 0.0f;
    uint32_t lastFrameMs = 0;
    uint32_t frameCount = 0;
    uint32_t recordFailureCount = 0;
    uint32_t triggerCount = 0;
    uint32_t storedRecordingCount = 0;
};

bool initMicrophone();
void updateMicrophone();
const char* getMicStateName();
MicRuntimeStatus getMicRuntimeStatus();
