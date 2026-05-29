#pragma once

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
