#pragma once

// WiFi 配网 Portal（Captive Portal）
// 全部 WiFi 连接失败时启动，或开机按 BtnA 强制触发。
// 此模式下不启动 face/camera/servo/env/http 任何服务。

// 进入 portal 并阻塞直到用户完成配网并重启。
// 函数永远不会返回（内部会 ESP.restart()）。
void runWifiPortal();
