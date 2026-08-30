// wifi_manager.cpp
//
// 连接优先级：
//   1. NVS 里用户配网存的凭证（Preferences, namespace "wifi"）
//   2. config.h 里的 WIFI_SSID_0 / WIFI_SSID_1（硬编码备用）
//
// 每个网络超时 15 秒。
// 全部失败返回 false，main.cpp 负责调用 runWifiPortal()。

#include <M5Unified.h>
#include <WiFi.h>
#include <Preferences.h>
#include "wifi_manager.h"
#include "config_loader.h"

#define WIFI_CONNECT_TIMEOUT_MS  15000   // 每个网络的超时（ms）
#define WIFI_RECONNECT_INTERVAL_MS 5000  // serviceWiFi() 重连间隔

// 内部：尝试连接一个 SSID，超时 WIFI_CONNECT_TIMEOUT_MS。
// 返回 true = 连上。
static bool tryConnect(const char* ssid, const char* password) {
    if (!ssid || strlen(ssid) == 0) return false;

    Serial.printf("[WIFI] Trying: %s\n", ssid);
    WiFi.begin(ssid, (password && strlen(password) > 0) ? password : nullptr);

    unsigned long start = millis();
    while (WiFi.status() != WL_CONNECTED) {
        if (millis() - start >= WIFI_CONNECT_TIMEOUT_MS) {
            WiFi.disconnect(false);
            Serial.printf("[WIFI] Timeout: %s\n", ssid);
            return false;
        }
        delay(250);
        Serial.print(".");
    }
    Serial.printf("\n[WIFI] Connected: %s  IP: %s\n",
                  ssid, WiFi.localIP().toString().c_str());
    // 连网成功toast：屏幕短暂显示SSID+IP（约3秒），随后被脸覆盖。
    // 出门/回国场景下让人一眼确认它上的是哪个网。
    M5.Display.fillScreen(TFT_BLACK);
    M5.Display.setTextColor(0x07E0);  // 绿色
    M5.Display.setTextSize(2);
    M5.Display.setCursor(10, 90);
    M5.Display.printf("WiFi OK!\n\n  %s\n\n  %s", ssid, WiFi.localIP().toString().c_str());
    delay(3000);
    return true;
}

bool connectWiFi() {
    Serial.println("\n[WIFI] Starting connection sequence");
    WiFi.mode(WIFI_STA);

    // ── 1. NVS 保存的用户凭证（优先）────────────────────────────────────────
    {
        Preferences prefs;
        prefs.begin("wifi", true);   // read-only
        String nvsSsid = prefs.getString("ssid", "");
        String nvsPass = prefs.getString("pass", "");
        prefs.end();

        if (nvsSsid.length() > 0) {
            Serial.printf("[WIFI] NVS credentials found: %s\n", nvsSsid.c_str());
            if (tryConnect(nvsSsid.c_str(), nvsPass.c_str())) {
                return true;
            }
            Serial.println("[WIFI] NVS credentials failed, trying hardcoded networks");
        } else {
            Serial.println("[WIFI] No NVS credentials");
        }
    }

    // ── 2. config.h の硬编码网络（依次尝试）────────────────────────────────

    struct NetEntry { const char* ssid; const char* pass; };
    static const NetEntry NETWORKS[WIFI_NETWORK_COUNT] = {
#if WIFI_NETWORK_COUNT >= 1
        { WIFI_SSID_0, WIFI_PASSWORD_0 },
#endif
#if WIFI_NETWORK_COUNT >= 2
        { WIFI_SSID_1, WIFI_PASSWORD_1 },
#endif
#if WIFI_NETWORK_COUNT >= 3
        { WIFI_SSID_2, WIFI_PASSWORD_2 },
#endif
    };

    for (int i = 0; i < WIFI_NETWORK_COUNT; i++) {
        if (tryConnect(NETWORKS[i].ssid, NETWORKS[i].pass)) {
            return true;
        }
    }

    // ── 全部失败 ──────────────────────────────────────────────────────────────
    Serial.println("[WIFI] All networks failed");
    return false;
}

void serviceWiFi() {
    static unsigned long lastReconnectMs = 0;
    static bool wasConnected = false;

    if (WiFi.status() == WL_CONNECTED) {
        if (!wasConnected) {
            Serial.printf("[WIFI] Connected: %s\n", WiFi.localIP().toString().c_str());
        }
        wasConnected = true;
        return;
    }

    wasConnected = false;
    unsigned long now = millis();
    if (now - lastReconnectMs < WIFI_RECONNECT_INTERVAL_MS) {
        return;
    }
    lastReconnectMs = now;

    Serial.println("[WIFI] Disconnected. Reconnect requested.");
    WiFi.reconnect();
}
