// wifi_portal.cpp
// WiFi captive portal for Stack-chan CoreS3
//
// Runs when all known WiFi networks fail, or when BtnA is held at boot.
// Does NOT start face / camera / servo / env / http_server services.
// Blocks until the user saves new credentials, then calls ESP.restart().

#include "wifi_portal.h"

#include <M5Unified.h>
#include <WiFi.h>
#include <WebServer.h>
#include <Preferences.h>

static const char* PORTAL_AP_SSID     = "StackChan-Setup";
static const char* PORTAL_AP_PASSWORD = "";   // empty = open network
static const IPAddress PORTAL_IP(192, 168, 4, 1);

// ---------------------------------------------------------------------------
// Screen display
// ---------------------------------------------------------------------------

static void drawPortalScreen() {
    M5.Display.fillScreen(0x000F);            // dark-blue background

    // Title
    M5.Display.setTextColor(0xFFFF);
    M5.Display.setTextSize(2);
    M5.Display.setCursor(10, 10);
    M5.Display.println("WiFi Setup");

    // Divider
    M5.Display.drawLine(0, 38, M5.Display.width(), 38, 0x7BEF);

    // AP name (yellow)
    M5.Display.setTextSize(2);
    M5.Display.setTextColor(0xFFE0);
    M5.Display.setCursor(10, 48);
    M5.Display.print("Wi-Fi: ");
    M5.Display.println(PORTAL_AP_SSID);

    // URL (cyan)
    M5.Display.setTextColor(0x07FF);
    M5.Display.setCursor(10, 78);
    M5.Display.print("Open:  ");
    M5.Display.println("192.168.4.1");

    // Steps (large enough for phone camera)
    M5.Display.setTextSize(2);
    M5.Display.setTextColor(0xFFFF);
    M5.Display.setCursor(10, 118);
    M5.Display.println("1. Join above Wi-Fi");
    M5.Display.setCursor(10, 142);
    M5.Display.println("2. Open browser");
    M5.Display.setCursor(10, 166);
    M5.Display.println("3. Pick SSID & Save");

    // Footer note
    M5.Display.setTextSize(1);
    M5.Display.setTextColor(0x8410);
    M5.Display.setCursor(10, 210);
    M5.Display.println("Hold BtnA at boot to force portal");
}

// ---------------------------------------------------------------------------
// HTML pages  (all ASCII — browser renders fine; device-side chars are ASCII)
// ---------------------------------------------------------------------------

static String escapeHtml(const String& value) {
    String escaped;
    escaped.reserve(value.length() + 16);
    for (size_t i = 0; i < value.length(); ++i) {
        switch (value[i]) {
            case '&':
                escaped += "&amp;";
                break;
            case '<':
                escaped += "&lt;";
                break;
            case '>':
                escaped += "&gt;";
                break;
            case '"':
                escaped += "&quot;";
                break;
            case '\'':
                escaped += "&#39;";
                break;
            default:
                escaped += value[i];
                break;
        }
    }
    return escaped;
}

// Build the Wi-Fi selection form dynamically with scanned SSIDs.
static String buildIndexPage(int networkCount, const String ssidList[]) {
    String html;
    html += "<!DOCTYPE html>\n";
    html += "<html lang=\"zh\">\n";
    html += "<head>\n";
    html += "<meta charset=\"utf-8\">\n";
    html += "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n";
    html += "<title>StackChan WiFi Setup</title>\n";
    html += "<style>\n";
    html += "body{font-family:sans-serif;max-width:400px;margin:40px auto;padding:0 16px;background:#f0f4ff}\n";
    html += "h1{color:#334;font-size:1.4em}\n";
    html += "label{display:block;margin-top:16px;font-weight:bold;color:#445}\n";
    html += "select,input{width:100%;padding:10px;font-size:1em;border:1px solid #aac;border-radius:6px;box-sizing:border-box;margin-top:4px}\n";
    html += "button{margin-top:20px;width:100%;padding:12px;background:#3366cc;color:#fff;border:none;border-radius:6px;font-size:1em;cursor:pointer}\n";
    html += "button:active{background:#2244aa}\n";
    html += ".note{color:#888;font-size:0.85em;margin-top:8px}\n";
    html += "</style>\n";
    html += "</head>\n";
    html += "<body>\n";
    html += "<h1>StackChan WiFi Setup</h1>\n";
    html += "<form method=\"POST\" action=\"/save\">\n";
    html += "  <label>Select Network</label>\n";
    html += "  <select name=\"ssid\">\n";

    for (int i = 0; i < networkCount; i++) {
        const String escapedSsid = escapeHtml(ssidList[i]);
        html += "    <option value=\"" + escapedSsid + "\">" + escapedSsid + "</option>\n";
    }

    html += "  </select>\n";
    html += "  <label>Password</label>\n";
    html += "  <input type=\"password\" name=\"pass\" placeholder=\"(leave blank if none)\">\n";
    html += "  <p class=\"note\">Device will restart and connect to the selected network.</p>\n";
    html += "  <button type=\"submit\">Save &amp; Restart</button>\n";
    html += "</form>\n";
    html += "</body>\n";
    html += "</html>\n";
    return html;
}

static const char SAVE_OK_PAGE[] =
    "<!DOCTYPE html>"
    "<html lang=\"zh\">"
    "<head>"
    "<meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<title>Saved</title>"
    "<style>"
    "body{font-family:sans-serif;max-width:400px;margin:60px auto;text-align:center;background:#f0f4ff}"
    "h1{color:#393}"
    "p{color:#555}"
    "</style>"
    "</head>"
    "<body>"
    "<h1>&#10003; Saved</h1>"
    "<p>Credentials saved. Restarting...</p>"
    "<p>Wait ~10 seconds for StackChan to reconnect.</p>"
    "</body>"
    "</html>";

// ---------------------------------------------------------------------------
// Main portal entry point
// ---------------------------------------------------------------------------

void runWifiPortal() {
    Serial.println("[PORTAL] Starting WiFi captive portal");

    // Switch to AP-only mode
    WiFi.disconnect(true);
    delay(100);
    WiFi.mode(WIFI_AP);

    bool apOk = WiFi.softAP(PORTAL_AP_SSID, PORTAL_AP_PASSWORD);
    if (!apOk) {
        Serial.println("[PORTAL] softAP() failed!");
    }
    WiFi.softAPConfig(PORTAL_IP, PORTAL_IP, IPAddress(255, 255, 255, 0));
    delay(200);

    Serial.printf("[PORTAL] AP up: SSID=%s  IP=%s\n",
                  PORTAL_AP_SSID, WiFi.softAPIP().toString().c_str());

    // Draw screen first, then scan (scan takes a few seconds)
    drawPortalScreen();

    // Scan for nearby networks
    int n = WiFi.scanNetworks();
    String ssids[32];
    int ssidCount = 0;
    for (int i = 0; i < n && ssidCount < 32; i++) {
        String s = WiFi.SSID(i);
        if (s.length() == 0) continue;
        bool dup = false;
        for (int j = 0; j < ssidCount; j++) {
            if (ssids[j] == s) { dup = true; break; }
        }
        if (!dup) ssids[ssidCount++] = s;
    }
    Serial.printf("[PORTAL] Scanned %d unique SSIDs\n", ssidCount);

    // Serve the portal
    WebServer portalServer(80);

    portalServer.on("/", HTTP_GET, [&]() {
        portalServer.send(200, "text/html", buildIndexPage(ssidCount, ssids));
    });

    // Captive-portal redirect: phones probe random URLs to detect a portal
    portalServer.onNotFound([&]() {
        portalServer.sendHeader("Location", "http://192.168.4.1/", true);
        portalServer.send(302, "text/plain", "");
    });

    portalServer.on("/save", HTTP_POST, [&]() {
        String ssid = portalServer.arg("ssid");
        String pass = portalServer.arg("pass");

        Serial.printf("[PORTAL] Saving credentials: ssid=%s\n", ssid.c_str());

        Preferences prefs;
        prefs.begin("wifi", false);
        prefs.putString("ssid", ssid);
        prefs.putString("pass", pass);
        prefs.end();

        portalServer.send(200, "text/html", SAVE_OK_PAGE);
        delay(2000);
        ESP.restart();
    });

    portalServer.begin();
    Serial.println("[PORTAL] HTTP server started on port 80");

    // Block here — no other tasks running, so no scheduling conflicts
    while (true) {
        portalServer.handleClient();
        M5.update();
        delay(10);
    }
    // Never reached; /save handler calls ESP.restart()
}
