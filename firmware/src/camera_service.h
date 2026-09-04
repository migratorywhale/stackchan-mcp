#ifndef CAMERA_SERVICE_H
#define CAMERA_SERVICE_H

#include <stdint.h>
#include <stddef.h>

/**
 * Stack-chan Camera Service
 * Uses GC0308 sensor on M5Stack CoreS3
 * Captures RGB565 frames and converts to JPEG
 */

// Start a bounded burst session for repeated snapshots. Camera SCCB and the
// internal I2C bus share GPIO 11/12, so top-touch input is suspended while the
// session is active. The idle timeout is refreshed by each captured frame.
bool startCameraSession(uint32_t idleTimeoutMs = 5000);

// End a burst session and restore the internal I2C bus/top-touch input.
bool stopCameraSession();

// Release a burst session after its idle timeout, even if the host disappears.
void updateCameraService();

bool isCameraSessionActive();
uint32_t cameraSessionIdleTimeoutMs();

// Capture a JPEG snapshot. Outside a burst session, captureJpeg owns the
// complete camera/touch handoff lifecycle. Inside a session, it reuses the
// initialized camera and leaves touch suspended for the next frame.
// Returns true on success, sets outBuf and outLen
// Caller must free(*outBuf) after use
bool captureJpeg(uint8_t** outBuf, size_t* outLen, int quality = 80);

#endif
