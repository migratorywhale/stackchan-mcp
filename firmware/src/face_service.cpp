// face_service.cpp
// Custom whale face PNG display from SPIFFS
// Replaces m5stack-avatar with 小克's own face

#include "face_service.h"
#include <M5Unified.h>
#include <SPIFFS.h>

// ── Face file paths in SPIFFS ──────────────────
static const char* FACE_FILES[] = {
    "/A_calm_320x240.png",      // WHALE_CALM
    "/B_thinking_320x240.png",  // WHALE_THINKING
    "/C_happy_320x240.png",     // WHALE_HAPPY
    "/D_sleepy_320x240.png",    // WHALE_SLEEPY
    "/E_shy_320x240.png",       // WHALE_SHY
    "/F_smug_320x240.png",      // WHALE_SMUG
    "/G_pouty_320x240.png",     // WHALE_POUTY
};
static const int NUM_FACES = 7;

static WhaleFace currentFace = WHALE_CALM;
static bool spiffsReady = false;
static bool isTalking = false;

// Preloaded PNG buffers in PSRAM for fast switching
static uint8_t* faceBuffers[7] = {nullptr};
static size_t   faceSizes[7]   = {0};

// ── Internal: preload PNGs into PSRAM ──────────
static bool preloadFaces() {
    for (int i = 0; i < NUM_FACES; i++) {
        File f = SPIFFS.open(FACE_FILES[i], "r");
        if (!f) {
            Serial.printf("[FACE] Cannot open: %s\n", FACE_FILES[i]);
            continue;
        }
        size_t sz = f.size();
        faceBuffers[i] = (uint8_t*)ps_malloc(sz);
        if (!faceBuffers[i]) {
            Serial.printf("[FACE] PSRAM alloc failed for %s (%u bytes)\n", FACE_FILES[i], (unsigned)sz);
            f.close();
            continue;
        }
        f.read(faceBuffers[i], sz);
        faceSizes[i] = sz;
        f.close();
        Serial.printf("[FACE] Loaded: %s (%u bytes)\n", FACE_FILES[i], (unsigned)sz);
    }
    return true;
}

// ── Internal: draw a face from preloaded buffer ─
static void drawFace(WhaleFace face) {
    if (face < 0 || face >= NUM_FACES) face = WHALE_CALM;
    if (!faceBuffers[face] || faceSizes[face] == 0) {
        Serial.printf("[FACE] Buffer not loaded for face %d\n", (int)face);
        return;
    }

    currentFace = face;
    M5.Display.drawPng(faceBuffers[face], faceSizes[face], 0, 0);
}

// ── Public API ─────────────────────────────────

void initFace() {
    // Mount SPIFFS
    if (!SPIFFS.begin(true)) {
        Serial.println("[FACE] SPIFFS mount failed!");
        spiffsReady = false;
        return;
    }
    spiffsReady = true;

    // Preload all face PNGs into PSRAM for instant switching
    preloadFaces();

    // Display initial face
    M5.Display.fillScreen(TFT_BLACK);
    drawFace(WHALE_CALM);

    Serial.println("[FACE] Whale face ready");
    Serial.printf("[FACE] Free heap: %u  Free PSRAM: %u\n",
                  ESP.getFreeHeap(), ESP.getFreePsram());
}

void setFaceExpression(FaceExpression expr) {
    WhaleFace target;

    switch (expr) {
        case FACE_IDLE: {
            target = WHALE_CALM;
            isTalking = false;
            break;
        }

        case FACE_LISTENING:
            target = WHALE_THINKING;  // Ripple eyes = paying attention
            isTalking = false;
            break;

        case FACE_PLAYING:
            target = WHALE_HAPPY;  // Open mouth face for speaking
            isTalking = true;
            break;

        case FACE_THINKING:
            target = WHALE_THINKING;
            isTalking = false;
            break;

        case FACE_HAPPY:
            target = WHALE_HAPPY;
            isTalking = false;
            break;

        default:
            target = WHALE_CALM;
            isTalking = false;
            break;
    }

    if (target != currentFace) {
        drawFace(target);
    }
}

void setMouthOpen(float ratio) {
    // PNG lip sync: toggle between calm and happy during speech
    if (!isTalking) return;

    if (ratio > 0.15f) {
        if (currentFace != WHALE_HAPPY) {
            drawFace(WHALE_HAPPY);
        }
    } else {
        if (currentFace != WHALE_CALM) {
            drawFace(WHALE_CALM);
        }
    }
}

void setWhaleFace(WhaleFace face) {
    isTalking = false;
    drawFace(face);
}

const char* getCurrentFaceName() {
    return whaleFaceName(currentFace);
}
