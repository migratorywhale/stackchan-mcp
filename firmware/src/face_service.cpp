// face_service.cpp
// AnimatedGIF-based face expression system
// Replaces static PNG display with looping GIF animations

#include "face_service.h"
#include "gif_assets.h"
#include <AnimatedGIF.h>
#include <M5Unified.h>
#include <SPIFFS.h>

// ── GIF engine state ───────────────────────────────────────────────────────
static AnimatedGIF gif;
static const uint8_t* current_gif_data   = nullptr;
static size_t         current_gif_len    = 0;
static volatile bool  gif_changed        = false;
static int            gif_src_width      = 0;
static int            gif_src_height     = 0;

// ── Face tracking ──────────────────────────────────────────────────────────
static WhaleFace currentFace = WHALE_CALM;
static bool      isTalking   = false;
static uint32_t  faceCommandRevision = 0;

// ── GIF asset table ────────────────────────────────────────────────────────
struct GifAsset { const uint8_t* data; size_t len; };

static const GifAsset GIF_ASSETS[] = {
    { face_gif_calm,    face_gif_calm_len    },  // WHALE_CALM    = 0
    { face_gif_thinking,face_gif_thinking_len},  // WHALE_THINKING = 1
    { face_gif_happy,   face_gif_happy_len   },  // WHALE_HAPPY   = 2
    { face_gif_sleepy,  face_gif_sleepy_len  },  // WHALE_SLEEPY  = 3
    { face_gif_shy,     face_gif_shy_len     },  // WHALE_SHY     = 4
    { face_gif_smug,    face_gif_smug_len    },  // WHALE_SMUG    = 5
    { face_gif_pouty,   face_gif_pouty_len   },  // WHALE_POUTY   = 6
};
static const int NUM_GIF_ASSETS = 7;

// ── GIFDraw callback ───────────────────────────────────────────────────────
// Called once per scanline of each frame.
// pDraw->iX, iY: frame's top-left offset on the canvas
// pDraw->y:      current scanline index within this frame (0 = frame top)
// pDraw->iWidth: pixel width of this frame
// pDraw->pPalette: uint16_t* RGB565 LE palette (256 entries max)
// pDraw->pPixels: uint8_t* palette indices for this scanline
//
// GIF is 192x192 — display 1:1, centered on the 320x240 screen.
// Color note: ILI9342 (CoreS3 LCD) is configured in BGR mode via MADCTL.
// M5GFX::pushImage(uint16_t*, _swapBytes=false) passes data straight to SPI
// as swap565_t (big-endian on wire). ILI9342 then interprets the incoming
// 16-bit word as [B4..B0][G5..G0][R4..R0] due to BGR mode. To compensate,
// we swap the R and B fields of the RGB565_LE palette value before buffering.
// Formula: color = ((c & 0x1F) << 11) | (c & 0x07E0) | (c >> 11)
// Scale factor: 240 / 192 = 1.25 — fills screen height nicely.
// We use fixed-point: scale_num=5, scale_den=4 (= 1.25x).
#define SCALE_NUM 5
#define SCALE_DEN 4

static void GIFDraw(GIFDRAW* pDraw) {
    int srcW = pDraw->iWidth;
    int srcH = gif_src_height;

    // Scaled dimensions
    int dstW = srcW * SCALE_NUM / SCALE_DEN;
    int dstH = srcH * SCALE_NUM / SCALE_DEN;

    // Center on display
    int xOff = (320 - dstW) / 2;
    if (xOff < 0) xOff = 0;
    int yOff = (240 - dstH) / 2;
    if (yOff < 0) yOff = 0;

    // Source scanline Y
    int srcY = pDraw->iY + pDraw->y;

    uint8_t*  src     = pDraw->pPixels;
    uint16_t* palette = pDraw->pPalette;
    bool      hasTrans = (pDraw->ucHasTransparency != 0);
    uint8_t   transIdx = pDraw->ucTransparent;

    // Build scaled output line (nearest neighbor)
    uint16_t lineBuf[320];
    int outW = (dstW <= 320) ? dstW : 320;

    for (int dx = 0; dx < outW; dx++) {
        int sx = dx * SCALE_DEN / SCALE_NUM;  // reverse map to source
        if (sx >= srcW) sx = srcW - 1;
        uint8_t idx = src[sx];
        if (hasTrans && idx == transIdx) {
            lineBuf[dx] = 0x0000;
        } else {
            lineBuf[dx] = palette[idx];
        }
    }

    // Each source scanline maps to SCALE_NUM/SCALE_DEN destination rows
    // Write multiple rows for this source line
    int destY_start = yOff + srcY * SCALE_NUM / SCALE_DEN;
    int destY_end   = yOff + (srcY + 1) * SCALE_NUM / SCALE_DEN;

    M5.Display.setSwapBytes(true);
    for (int dy = destY_start; dy < destY_end && dy < 240; dy++) {
        if (dy >= 0) {
            M5.Display.pushImage(xOff, dy, outW, 1, lineBuf);
        }
    }
    M5.Display.setSwapBytes(false);
}

// ── Animation task ─────────────────────────────────────────────────────────
static void faceTask(void* param) {
    // GIF_PALETTE_RGB565_LE: palette entries are standard little-endian RGB565.
    // The GIFDraw callback applies an R5<->B5 swap to compensate for ILI9342 BGR mode.
    gif.begin(GIF_PALETTE_RGB565_LE);

    while (true) {
        if (gif_changed) {
            gif.close();
            M5.Display.fillScreen(TFT_BLACK);

            if (current_gif_data != nullptr) {
                // openFLASH is the correct call for PROGMEM / flash-resident data.
                int ok = gif.openFLASH((uint8_t*)current_gif_data,
                                       (int)current_gif_len,
                                       GIFDraw);
                if (ok) {
                    gif_src_width  = gif.getCanvasWidth();
                    gif_src_height = gif.getCanvasHeight();
                    Serial.printf("[FACE] GIF opened: %dx%d\n",
                                  gif_src_width, gif_src_height);
                } else {
                    Serial.printf("[FACE] GIF open failed (err %d)\n",
                                  gif.getLastError());
                }
            }
            gif_changed = false;
        }

        if (current_gif_data != nullptr) {
            int frameResult = gif.playFrame(true, nullptr);
            if (frameResult == 0) {
                // End of animation — reset to loop.
                gif.reset();
            }
        } else {
            vTaskDelay(pdMS_TO_TICKS(50));
        }
    }
}

// ── Internal helpers ───────────────────────────────────────────────────────
static void switchToFace(WhaleFace face) {
    if (face < 0 || face >= NUM_GIF_ASSETS) face = WHALE_CALM;
    currentFace      = face;
    current_gif_data = GIF_ASSETS[face].data;
    current_gif_len  = GIF_ASSETS[face].len;
    gif_changed      = true;
}

// ── Public API ─────────────────────────────────────────────────────────────

void initFace() {
    // SPIFFS is used by other services (http_server, etc.) — mount it here.
    if (!SPIFFS.begin(true)) {
        Serial.println("[FACE] SPIFFS mount failed!");
    } else {
        Serial.println("[FACE] SPIFFS mounted");
    }

    // Set the initial GIF asset before starting the task.
    current_gif_data = GIF_ASSETS[WHALE_CALM].data;
    current_gif_len  = GIF_ASSETS[WHALE_CALM].len;
    gif_changed      = true;
    currentFace      = WHALE_CALM;

    // Spawn animation task on core 1 (core 0 handles WiFi/BT).
    xTaskCreatePinnedToCore(faceTask, "face_gif", 8192, nullptr, 1, nullptr, 1);

    Serial.println("[FACE] GIF face service started");
    Serial.printf("[FACE] Free heap: %u  Free PSRAM: %u\n",
                  ESP.getFreeHeap(), ESP.getFreePsram());
}

void setFaceExpression(FaceExpression expr) {
    WhaleFace target;

    switch (expr) {
        case FACE_IDLE:
            target    = WHALE_CALM;
            isTalking = false;
            break;
        case FACE_LISTENING:
            target    = WHALE_THINKING;
            isTalking = false;
            break;
        case FACE_PLAYING:
            target    = WHALE_HAPPY;
            isTalking = true;
            break;
        case FACE_THINKING:
            target    = WHALE_THINKING;
            isTalking = false;
            break;
        case FACE_HAPPY:
            target    = WHALE_HAPPY;
            isTalking = false;
            break;
        default:
            target    = WHALE_CALM;
            isTalking = false;
            break;
    }

    ++faceCommandRevision;
    if (target != currentFace) {
        switchToFace(target);
    }
}

void setMouthOpen(float ratio) {
    // Lip sync disabled for AnimatedGIF faces — pixel art has no mouth
    // animation, and switching GIFs causes visible flicker (close + open + redraw).
    // Keep the function signature for API compatibility.
    (void)ratio;
}

void setWhaleFace(WhaleFace face) {
    isTalking = false;
    ++faceCommandRevision;
    switchToFace(face);
}

const char* getCurrentFaceName() {
    return whaleFaceName(currentFace);
}

uint32_t getFaceCommandRevision() {
    return faceCommandRevision;
}
