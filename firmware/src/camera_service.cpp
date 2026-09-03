#include "camera_service.h"
#include "touch_service.h"
#include "esp_camera.h"
#include "img_converters.h"
#include <M5Unified.h>
#include <Arduino.h>

// ── CoreS3 GC0308 Camera Pin Configuration ─────────
// From M5CoreS3 library (GC0308.cpp)
#define CAM_PIN_PWDN    -1
#define CAM_PIN_RESET   -1
#define CAM_PIN_XCLK     2   // GPIO 2 generates XCLK for GC0308
#define CAM_PIN_SIOD    12   // I2C SDA (shared with system I2C)
#define CAM_PIN_SIOC    11   // I2C SCL (shared with system I2C)
#define CAM_PIN_D0      39
#define CAM_PIN_D1      40
#define CAM_PIN_D2      41
#define CAM_PIN_D3      42
#define CAM_PIN_D4      15
#define CAM_PIN_D5      16
#define CAM_PIN_D6      48
#define CAM_PIN_D7      47
#define CAM_PIN_VSYNC   46
#define CAM_PIN_HREF    38
#define CAM_PIN_PCLK    45

static bool cameraReady = false;

static bool initCamera() {
    camera_config_t config;
    memset(&config, 0, sizeof(config));

    config.pin_pwdn     = CAM_PIN_PWDN;
    config.pin_reset    = CAM_PIN_RESET;
    config.pin_xclk     = CAM_PIN_XCLK;
    config.pin_sccb_sda = CAM_PIN_SIOD;
    config.pin_sccb_scl = CAM_PIN_SIOC;
    config.pin_d0       = CAM_PIN_D0;
    config.pin_d1       = CAM_PIN_D1;
    config.pin_d2       = CAM_PIN_D2;
    config.pin_d3       = CAM_PIN_D3;
    config.pin_d4       = CAM_PIN_D4;
    config.pin_d5       = CAM_PIN_D5;
    config.pin_d6       = CAM_PIN_D6;
    config.pin_d7       = CAM_PIN_D7;
    config.pin_vsync    = CAM_PIN_VSYNC;
    config.pin_href     = CAM_PIN_HREF;
    config.pin_pclk     = CAM_PIN_PCLK;

    config.xclk_freq_hz = 20000000;  // 20MHz
    config.ledc_timer   = LEDC_TIMER_0;
    config.ledc_channel = LEDC_CHANNEL_0;

    // GC0308 does NOT support hardware JPEG
    // Must capture RGB565 and convert with frame2jpg()
    config.pixel_format = PIXFORMAT_RGB565;
    config.frame_size   = FRAMESIZE_QVGA;  // 320x240
    config.jpeg_quality = 0;               // Not used for RGB565
    config.fb_count     = 1;               // Single buffer to save RAM
    config.fb_location  = CAMERA_FB_IN_PSRAM;
    config.grab_mode    = CAMERA_GRAB_WHEN_EMPTY;

    esp_err_t err = esp_camera_init(&config);
    if (err != ESP_OK) {
        Serial.printf("[CAM] Init failed: 0x%x\n", err);
        cameraReady = false;
        return false;
    }

    cameraReady = true;
    Serial.println("[CAM] GC0308 ready (QVGA 320x240)");
    return true;
}

static void deinitCamera() {
    if (cameraReady) {
        esp_camera_deinit();
        cameraReady = false;
        Serial.println("[CAM] Deinitialized");
    }
}

bool captureJpeg(uint8_t** outBuf, size_t* outLen, int quality) {
    if (!outBuf || !outLen) {
        return false;
    }
    *outBuf = nullptr;
    *outLen = 0;

    // GC0308 SCCB and M5.In_I2C both use GPIO 11/12. Keep the camera lazy so
    // touch and environmental sensors own the bus between snapshots.
    suspendTouchService();
    const bool busReleased = M5.In_I2C.release();
    if (!busReleased) {
        Serial.println("[CAM] Internal I2C release failed; camera start aborted");
        if (!resumeTouchService()) {
            Serial.println("[CAM] Internal I2C also failed to recover after release failure");
        }
        return false;
    }

    bool captureOk = false;
    if (initCamera()) {
        // With CAMERA_GRAB_WHEN_EMPTY, the driver captures one frame immediately
        // after esp_camera_fb_return(). Discard it to force a fresh capture.
        camera_fb_t* stale = esp_camera_fb_get();
        if (!stale) {
            Serial.println("[CAM] Stale flush failed");
        } else {
            esp_camera_fb_return(stale);

            camera_fb_t* fb = esp_camera_fb_get();
            if (!fb) {
                Serial.println("[CAM] Capture failed");
            } else {
                captureOk = frame2jpg(fb, quality, outBuf, outLen);
                esp_camera_fb_return(fb);
                if (!captureOk) {
                    Serial.println("[CAM] JPEG conversion failed");
                }
            }
        }
    }

    deinitCamera();
    const bool touchRestored = resumeTouchService();

    if (!captureOk || !touchRestored) {
        if (*outBuf) {
            free(*outBuf);
            *outBuf = nullptr;
        }
        *outLen = 0;
        if (!touchRestored) {
            Serial.println("[CAM] Snapshot rejected because internal I2C/touch did not recover");
        }
        return false;
    }

    Serial.printf("[CAM] Captured JPEG: %u bytes\n", (unsigned)*outLen);
    return true;
}
