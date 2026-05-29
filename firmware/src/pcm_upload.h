#pragma once

#include <stddef.h>
#include <stdint.h>

enum PcmUploadRawStatus {
    PCM_UPLOAD_RAW_START,
    PCM_UPLOAD_RAW_WRITE,
    PCM_UPLOAD_RAW_END,
    PCM_UPLOAD_RAW_ABORTED,
};

struct PcmUploadBuffer {
    uint8_t* data = nullptr;
    size_t size = 0;
};

void clearPcmUpload();
void handlePcmUploadRaw(PcmUploadRawStatus status, const uint8_t* data, size_t size);
const char* consumePcmUploadError();
bool hasPcmUploadBody();
PcmUploadBuffer takePcmUploadBody();
