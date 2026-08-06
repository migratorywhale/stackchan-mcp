#ifndef ENV_SERVICE_H
#define ENV_SERVICE_H
#include <Arduino.h>

/**
 * Stack-chan Environment Sensor Service
 * Supports SHT31 (temp/humidity) and QMP6988 (barometric pressure)
 * Wired via pin headers to the CoreS3 internal I2C bus (SDA=12, SCL=11)
 *
 * Auto-detects sensors at startup via I2C bus scan.
 * Partial availability is fine — NAN is reported for any missing sensor.
 */

// Initialize I2C and detect sensors. Call in setup() after M5.begin().
// Logs detected addresses to Serial. Returns true if at least one sensor found.
bool initEnvService();

// Read current values from detected sensors.
// Missing sensors produce NAN for their fields.
// Returns false if no sensors are available at all.
bool readEnv(float& temperature, float& humidity, float& pressure);

// 诊断：校准字节+原始ADC的JSON快照
String envDebugJson();

// Returns true if at least one sensor was detected during initEnvService().
bool isEnvAvailable();

// 最后一次读取失败的原因，空字符串表示没有错误
const char* envLastError();

#endif
