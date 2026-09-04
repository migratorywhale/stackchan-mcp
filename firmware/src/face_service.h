#pragma once

#include <stddef.h>
#include <stdint.h>

#include "face_names.h"

// Expression types (state-based, used by mic/audio services)
enum FaceExpression {
    FACE_IDLE      = 0,  // Default (calm or sleepy based on time)
    FACE_LISTENING = 1,  // Listening to mic
    FACE_PLAYING   = 2,  // Speaking (happy/open mouth)
    FACE_THINKING  = 3,  // Processing
    FACE_HAPPY     = 4,  // Happy
};

void initFace();
void setFaceExpression(FaceExpression expr);
void setMouthOpen(float ratio);  // 0.0~1.0 for lip sync
void setWhaleFace(WhaleFace face);  // Direct face control
const char* getCurrentFaceName();
// Advances on every face command, including a same-face reassertion. This lets
// temporary effects avoid restoring over a newer owner's explicit command.
uint32_t getFaceCommandRevision();

// Temporarily replace the animated face with the exact JPEG returned by the
// camera endpoint. The latest requested face is redrawn after the preview.
bool showCameraPreview(const uint8_t* jpegData, size_t jpegLen,
                       uint32_t durationMs = 6000);
