#ifndef WIFI_MANAGER_H
#define WIFI_MANAGER_H

// WiFi 连接尝试。
// 返回 true  = 连上了某个网络。
// 返回 false = 全部失败，调用方应进入配网模式。
bool connectWiFi();

// 在 loop() 里调用：断线后自动重连。
void serviceWiFi();

#endif
