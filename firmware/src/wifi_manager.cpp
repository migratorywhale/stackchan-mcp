// wifi_manager.cpp

#include <M5Unified.h>
#include <WiFi.h>
#include "wifi_manager.h"
#include "config.h"

// ネットワーク定義（config.h の定数を配列にまとめる）
struct NetworkConfig {
    const char* ssid;
    const char* password;
};

static const NetworkConfig NETWORKS[WIFI_NETWORK_COUNT] = {
    { WIFI_SSID_0, WIFI_PASSWORD_0 },
    { WIFI_SSID_1, WIFI_PASSWORD_1 },
};

#define WIFI_RECONNECT_INTERVAL_MS 5000

void connectWiFi() {
    Serial.println("\nConnecting to WiFi...");
    WiFi.mode(WIFI_STA);

    for (int i = 0; i < WIFI_NETWORK_COUNT; i++) {
        Serial.printf("[WIFI] 試行 %d/%d: %s\n", i + 1, WIFI_NETWORK_COUNT, NETWORKS[i].ssid);

        WiFi.begin(NETWORKS[i].ssid, NETWORKS[i].password);

        int attempts = 0;
        while (WiFi.status() != WL_CONNECTED && attempts < 20) {
            delay(500);
            Serial.print(".");
            attempts++;
        }

        if (WiFi.status() == WL_CONNECTED) {
            Serial.printf("\n[WIFI] 接続成功: %s\n", NETWORKS[i].ssid);
            Serial.printf("[WIFI] IP: %s\n", WiFi.localIP().toString().c_str());

            return;
        }

        // このネットワークに繋がらなかった → 次を試す
        WiFi.disconnect();
        Serial.printf("\n[WIFI] %s に接続できませんでした\n", NETWORKS[i].ssid);
    }

    // 全部失敗
    Serial.println("[WIFI] ❌ すべてのネットワークへの接続に失敗しました");
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
