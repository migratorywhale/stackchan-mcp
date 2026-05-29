#include <M5Unified.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include "audio_download.h"
#include "wav_parser.h"

#define DOWNLOAD_TIMEOUT_MS   10000
#define MAX_WAV_BYTES         (4 * 1024 * 1024)

bool downloadVoice(const String& url, uint8_t** outData, size_t* outSize) {
    HTTPClient http;
    Serial.printf("[DOWNLOAD] URL: %s\n", url.c_str());

    *outData = nullptr;
    *outSize = 0;

    http.begin(url);
    http.setTimeout(DOWNLOAD_TIMEOUT_MS);
    int httpCode = http.GET();

    if (httpCode != HTTP_CODE_OK) {
        Serial.printf("[DOWNLOAD] HTTP error: %d\n", httpCode);
        http.end();
        return false;
    }

    int len = http.getSize();
    if (len <= 0 || len > MAX_WAV_BYTES) {
        Serial.printf("[DOWNLOAD] Invalid content length: %d\n", len);
        http.end();
        return false;
    }

    uint8_t* wavData = (uint8_t*)ps_malloc(len);
    if (!wavData) {
        Serial.println("[DOWNLOAD] ps_malloc failed");
        http.end();
        return false;
    }

    WiFiClient* stream = http.getStreamPtr();
    size_t bytesRead = 0;
    unsigned long lastProgressMs = millis();
    while (bytesRead < (size_t)len) {
        size_t available = stream->available();
        if (available) {
            size_t toRead = min(available, (size_t)(len - bytesRead));
            size_t got = stream->readBytes(wavData + bytesRead, toRead);
            if (got == 0) {
                Serial.println("[DOWNLOAD] Read returned 0 bytes");
                break;
            }
            bytesRead += got;
            lastProgressMs = millis();
        } else if (!http.connected()) {
            Serial.println("[DOWNLOAD] Connection closed before full read");
            break;
        } else if (millis() - lastProgressMs > DOWNLOAD_TIMEOUT_MS) {
            Serial.println("[DOWNLOAD] Read timeout");
            break;
        }
        delay(1);
    }
    http.end();

    if (bytesRead != (size_t)len) {
        Serial.printf("[DOWNLOAD] Incomplete read: got=%u expected=%u\n",
                      (unsigned)bytesRead, (unsigned)len);
        free(wavData);
        return false;
    }

    WavInfo wavInfo;
    if (!parseWavInfo(wavData, (size_t)len, &wavInfo)) {
        free(wavData);
        return false;
    }

    Serial.printf("[DOWNLOAD] Complete: bytes=%u data=%u offset=%u\n",
                  (unsigned)len, (unsigned)wavInfo.dataSize,
                  (unsigned)wavInfo.dataOffset);
    *outData = wavData;
    *outSize = (size_t)len;
    return true;
}
