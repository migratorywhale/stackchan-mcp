#include <M5Unified.h>
#include "pcm_upload.h"

#define HTTP_PCM_MAX_BYTES (128 * 1024)

static uint8_t* s_buf = nullptr;
static size_t s_size = 0;
static size_t s_capacity = 0;
static bool s_ready = false;
static const char* s_error = nullptr;

void clearPcmUpload() {
    if (s_buf) {
        free(s_buf);
    }
    s_buf = nullptr;
    s_size = 0;
    s_capacity = 0;
    s_ready = false;
}

static bool reservePcmUpload(size_t requiredSize) {
    if (requiredSize <= s_capacity) {
        return true;
    }

    size_t newCapacity = s_capacity ? s_capacity : 8192;
    while (newCapacity < requiredSize) {
        if (newCapacity > HTTP_PCM_MAX_BYTES / 2) {
            newCapacity = HTTP_PCM_MAX_BYTES;
            break;
        }
        newCapacity *= 2;
    }

    uint8_t* newBuf = (uint8_t*)ps_malloc(newCapacity);
    if (!newBuf) {
        return false;
    }
    if (s_buf && s_size > 0) {
        memcpy(newBuf, s_buf, s_size);
    }
    if (s_buf) {
        free(s_buf);
    }
    s_buf = newBuf;
    s_capacity = newCapacity;
    return true;
}

void handlePcmUploadRaw(PcmUploadRawStatus status, const uint8_t* data, size_t size) {
    if (status == PCM_UPLOAD_RAW_START) {
        clearPcmUpload();
        s_error = nullptr;
        return;
    }

    if (status == PCM_UPLOAD_RAW_WRITE) {
        if (size > HTTP_PCM_MAX_BYTES - s_size) {
            s_error = "pcm too large";
            Serial.println("[HTTP] PCM upload too large");
            clearPcmUpload();
            return;
        }
        size_t newSize = s_size + size;
        if (!reservePcmUpload(newSize)) {
            s_error = "pcm alloc failed";
            Serial.println("[HTTP] PCM upload alloc failed");
            clearPcmUpload();
            return;
        }
        memcpy(s_buf + s_size, data, size);
        s_size += size;
        return;
    }

    if (status == PCM_UPLOAD_RAW_END) {
        s_ready = s_buf != nullptr && s_size > 0;
        return;
    }

    if (status == PCM_UPLOAD_RAW_ABORTED) {
        s_error = "pcm upload aborted";
        clearPcmUpload();
    }
}

const char* consumePcmUploadError() {
    const char* error = s_error;
    s_error = nullptr;
    return error;
}

bool hasPcmUploadBody() {
    return s_ready && s_buf != nullptr && s_size > 0;
}

PcmUploadBuffer takePcmUploadBody() {
    PcmUploadBuffer body;
    body.data = s_buf;
    body.size = s_size;
    s_buf = nullptr;
    s_size = 0;
    s_capacity = 0;
    s_ready = false;
    return body;
}
