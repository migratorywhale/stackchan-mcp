#include "wav_parser.h"

#include <string.h>

#ifdef ARDUINO
#include <Arduino.h>
#define WAV_LOG(msg) Serial.println(msg)
#define WAV_LOGF(...) Serial.printf(__VA_ARGS__)
#else
#define WAV_LOG(msg) do {} while (0)
#define WAV_LOGF(...) do {} while (0)
#endif

static uint16_t readLe16(const uint8_t* p) {
    return (uint16_t)p[0] | ((uint16_t)p[1] << 8);
}

static uint32_t readLe32(const uint8_t* p) {
    return (uint32_t)p[0] |
           ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) |
           ((uint32_t)p[3] << 24);
}

bool parseWavInfo(const uint8_t* data, size_t size, WavInfo* info) {
    if (!data || !info || size < 12) {
        WAV_LOG("[WAV] Invalid: too small");
        return false;
    }
    if (memcmp(data, "RIFF", 4) != 0 || memcmp(data + 8, "WAVE", 4) != 0) {
        WAV_LOG("[WAV] Invalid: missing RIFF/WAVE");
        return false;
    }

    bool foundFmt = false;
    bool foundData = false;
    uint16_t audioFormat = 0;
    WavInfo parsed = {};

    size_t offset = 12;
    while (offset + 8 <= size) {
        const uint8_t* chunk = data + offset;
        uint32_t chunkSize = readLe32(chunk + 4);
        size_t payloadOffset = offset + 8;
        size_t nextOffset = payloadOffset + chunkSize + (chunkSize & 1);

        if (payloadOffset > size || chunkSize > size - payloadOffset) {
            WAV_LOGF("[WAV] Invalid: chunk overflow at %u size=%u\n",
                     (unsigned)offset, (unsigned)chunkSize);
            return false;
        }

        if (memcmp(chunk, "fmt ", 4) == 0) {
            if (chunkSize < 16) {
                WAV_LOG("[WAV] Invalid: fmt chunk too small");
                return false;
            }
            audioFormat = readLe16(data + payloadOffset);
            parsed.channels = readLe16(data + payloadOffset + 2);
            parsed.sampleRate = readLe32(data + payloadOffset + 4);
            parsed.bitsPerSample = readLe16(data + payloadOffset + 14);
            foundFmt = true;
        } else if (memcmp(chunk, "data", 4) == 0) {
            parsed.dataOffset = payloadOffset;
            parsed.dataSize = chunkSize;
            foundData = true;
        }

        if (nextOffset <= offset) {
            WAV_LOG("[WAV] Invalid: chunk offset overflow");
            return false;
        }
        offset = nextOffset;
    }

    if (!foundFmt || !foundData) {
        WAV_LOG("[WAV] Invalid: missing fmt or data chunk");
        return false;
    }
    if (audioFormat != 1 || parsed.channels != 1 ||
        parsed.sampleRate != 24000 || parsed.bitsPerSample != 16) {
        WAV_LOGF("[WAV] Unsupported: format=%u channels=%u rate=%u bits=%u\n",
                 audioFormat, parsed.channels,
                 (unsigned)parsed.sampleRate, parsed.bitsPerSample);
        return false;
    }
    if (parsed.dataSize == 0 || parsed.dataOffset + parsed.dataSize > size) {
        WAV_LOG("[WAV] Invalid: bad data chunk");
        return false;
    }

    *info = parsed;
    return true;
}
