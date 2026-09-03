#pragma once

#include <stdint.h>

struct TouchRuntimeStatus {
    bool available = false;
    bool suspended = false;
    bool pettingActive = false;
    uint8_t intensities[3] = {0, 0, 0};
    const char* lastEvent = "none";
    uint32_t lastEventMs = 0;
    uint32_t petCount = 0;
    uint32_t suppressedPetCount = 0;
    uint32_t recordRequestCount = 0;
    uint32_t recordStartFailureCount = 0;
    uint32_t resumeFailureCount = 0;
};

// M5StackChan.begin() initializes the driver. This probe records whether the
// Si12T is actually reachable on the shared internal I2C bus.
bool initTouchService();

// Consume one-shot touch events after M5StackChan.update().
void updateTouchService();

// The CoreS3 camera shares GPIO 11/12 with the internal I2C bus. Snapshot code
// must suspend touch before taking the bus and resume it on every exit path.
void suspendTouchService();
bool resumeTouchService();

TouchRuntimeStatus getTouchRuntimeStatus();
