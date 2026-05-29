#pragma once

#include <stddef.h>
#include <stdint.h>

struct WavInfo {
    size_t dataOffset = 0;
    size_t dataSize = 0;
    uint32_t sampleRate = 0;
    uint16_t channels = 0;
    uint16_t bitsPerSample = 0;
};

bool parseWavInfo(const uint8_t* data, size_t size, WavInfo* info);
