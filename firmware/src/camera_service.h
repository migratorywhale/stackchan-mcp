#ifndef CAMERA_SERVICE_H
#define CAMERA_SERVICE_H

#include <stdint.h>
#include <stddef.h>

/**
 * Stack-chan Camera Service
 * Uses GC0308 sensor on M5Stack CoreS3
 * Captures RGB565 frames and converts to JPEG
 */

// Capture a JPEG snapshot. Camera SCCB and the internal I2C bus share GPIO
// 11/12, so captureJpeg owns the complete camera/touch handoff lifecycle.
// Returns true on success, sets outBuf and outLen
// Caller must free(*outBuf) after use
bool captureJpeg(uint8_t** outBuf, size_t* outLen, int quality = 80);

#endif
