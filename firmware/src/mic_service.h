#pragma once
#include <stdint.h>

bool initMicrophone();
void updateMicrophone();  
const char* getMicStateName();

struct MicStreamResult {
    bool success;
    const char* error;
    size_t bytesSent;
    uint32_t durationMs;
};

MicStreamResult streamMicrophoneToTcp(
    const char* host,
    uint16_t port,
    uint32_t maxMs,
    size_t frameSamples
);
