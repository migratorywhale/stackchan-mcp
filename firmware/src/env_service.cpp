#include "env_service.h"
#include <Arduino.h>
#include <Wire.h>
#include <M5Unified.h>

// ── I2C bus ──────────────────────────────────────────────────────────────────
// CoreS3 internal I2C: SDA=12, SCL=11 (shared with GC0308 SCCB and other
// onboard peripherals). M5Unified drives this bus as In_I2C; we re-use it
// here via the global Wire instance that M5StackChan.begin() already
// initialised at 400 kHz.
//
// Library choice: direct I2C reads, no external library.
// Rationale: the project has no M5Unit-ENV dependency and every other
// service in the codebase talks to hardware directly (e.g. camera_service
// calls esp_camera_init directly, servo_service calls StackChan-BSP directly).
// Rolling our own thin driver keeps the pattern consistent and avoids pulling
// in a library that wants to own the Wire bus.

// ── Known addresses ───────────────────────────────────────────────────────────
// SHT31:   0x44 (ADDR pin low, default)  or  0x45 (ADDR pin high)
// QMP6988: 0x70 (SDO pin low, default)   or  0x56 (SDO pin high)
static constexpr uint8_t SHT31_ADDR_PRIMARY   = 0x44;
static constexpr uint8_t SHT31_ADDR_ALTERNATE = 0x45;
static constexpr uint8_t QMP_ADDR_PRIMARY      = 0x70;
static constexpr uint8_t QMP_ADDR_ALTERNATE    = 0x56;

// ── Module state ─────────────────────────────────────────────────────────────
static bool    s_sht31Found   = false;
static bool    s_qmpFound     = false;
static uint8_t s_sht31Addr   = 0;
static uint8_t s_qmpAddr     = 0;
static bool    s_usingPortA  = false;   // true = 外部Port A (G1/G2), false = 内部总线

// CoreS3 Port A (Grove红口 / 排针引出) 的 I2C 引脚
// 2026/7/29 实测：CoreS3 的 Port A 是 G2=SDA、G1=SCL（跟直觉相反）。
// 穷举扫描的证据：G1/G2 那组一个都扫不到，反过来才看到 0x44 + 0x70。
static constexpr int PORTA_SDA = 2;
static constexpr int PORTA_SCL = 1;

// 当前使用的总线。内部总线是 M5Unified 已经初始化好的 Wire；
// Port A 用第二个 I2C 外设 Wire1 —— 不去 end/re-begin M5 占着的 Wire，
// 否则会在启动时挂死（2026/7/29 实测：Wire.end() 后板子卡在扫描不动）。
static constexpr uint32_t I2C_FREQ = 100000;   // Grove 线偏长，100kHz 稳
static char s_lastError[64] = "";   // 最后一次读取失败的原因，走 HTTP 暴露（串口在启动后会哑）

// QMP6988 calibration coefficients (loaded once in initEnvService)
// 2026/7/29 v2: 第一版是凭记忆写的假补偿(读数191.7hPa)。真实流程见数据手册§4.3:
// ①OTP原始值要过仿射映射 K = A + S*OTP/32767 (每个系数A/S不同,下表)
// ②a0/b00是20bit带符号(高16bit + 0xB8扩展字节各4bit),除16
// ③ADC原始值带偏置: Dt = rawT - 2^23, Dp = rawP - 2^23
static float   s_qmpA0  = 0.0f, s_qmpA1 = 0.0f, s_qmpA2 = 0.0f;
static float   s_qmpB00 = 0.0f, s_qmpBt1 = 0.0f, s_qmpBt2 = 0.0f;
static float   s_qmpBp1 = 0.0f, s_qmpB11 = 0.0f, s_qmpBp2 = 0.0f;
static float   s_qmpB12 = 0.0f, s_qmpB21 = 0.0f, s_qmpBp3 = 0.0f;
static bool    s_qmpCalOk  = false;

// ── I2C probe ────────────────────────────────────────────────────────────────
// Returns true when a device ACKs at the given address.
static bool i2cProbe(uint8_t addr) {
    bool ok = M5.Ex_I2C.start(addr, false, I2C_FREQ);
    M5.Ex_I2C.stop();
    return ok;
}

// ── SHT31 helpers ────────────────────────────────────────────────────────────
// Issue a soft-reset so the sensor starts from a known state.
static void sht31SoftReset(uint8_t addr) {
    const uint8_t cmd[2] = { 0x30, 0xA2 };
    M5.Ex_I2C.start(addr, false, I2C_FREQ);
    M5.Ex_I2C.write(cmd, 2);
    M5.Ex_I2C.stop();
    delay(15);
}

// Read temperature (°C) and relative humidity (%) from SHT31.
// Returns false on CRC error or read timeout.
static bool sht31Read(uint8_t addr, float& temp, float& hum) {
    // 0x2C06 = 单次测量、高重复度、开启 clock stretching。
    // 比 0x2400 更稳：由传感器拉住时钟线直到数据就绪，不依赖主机 delay。
    const uint8_t cmd[2] = { 0x2C, 0x06 };
    if (!M5.Ex_I2C.start(addr, false, I2C_FREQ) || !M5.Ex_I2C.write(cmd, 2)) {
        M5.Ex_I2C.stop();
        snprintf(s_lastError, sizeof(s_lastError), "cmd write failed");
        return false;
    }
    M5.Ex_I2C.stop();

    delay(20);

    uint8_t raw[6];
    if (!M5.Ex_I2C.start(addr, true, I2C_FREQ) || !M5.Ex_I2C.read(raw, 6)) {
        M5.Ex_I2C.stop();
        snprintf(s_lastError, sizeof(s_lastError), "read failed");
        return false;
    }
    M5.Ex_I2C.stop();

    // CRC-8 check (polynomial 0x31, init 0xFF)
    auto crc8 = [](const uint8_t* data, int len) -> uint8_t {
        uint8_t crc = 0xFF;
        for (int i = 0; i < len; i++) {
            crc ^= data[i];
            for (int b = 0; b < 8; b++) {
                crc = (crc & 0x80) ? ((crc << 1) ^ 0x31) : (crc << 1);
            }
        }
        return crc;
    };

    if (crc8(raw, 2) != raw[2] || crc8(raw + 3, 2) != raw[5]) {
        snprintf(s_lastError, sizeof(s_lastError), "CRC mismatch");
        return false;
    }

    uint16_t rawT = ((uint16_t)raw[0] << 8) | raw[1];
    uint16_t rawH = ((uint16_t)raw[3] << 8) | raw[4];

    temp = -45.0f + 175.0f * ((float)rawT / 65535.0f);
    hum  = 100.0f * ((float)rawH / 65535.0f);
    return true;
}

// ── QMP6988 helpers ───────────────────────────────────────────────────────────
// QMP6988 register map (abbreviated)
static constexpr uint8_t QMP_REG_CHIP_ID   = 0xD1;
static constexpr uint8_t QMP_REG_RESET     = 0xE0;
static constexpr uint8_t QMP_REG_IO_SETUP  = 0xF5;
static constexpr uint8_t QMP_REG_CTRL_MEAS = 0xF3;
static constexpr uint8_t QMP_REG_PRESS_MSB = 0xF7;
static constexpr uint8_t QMP_REG_CALIB     = 0xA0;  // calibration block start

static uint8_t qmpReadReg(uint8_t addr, uint8_t reg) {
    uint8_t val = 0;
    if (M5.Ex_I2C.start(addr, false, I2C_FREQ) && M5.Ex_I2C.write(&reg, 1)) {
        M5.Ex_I2C.stop();
        if (M5.Ex_I2C.start(addr, true, I2C_FREQ)) {
            M5.Ex_I2C.read(&val, 1);
        }
    }
    M5.Ex_I2C.stop();
    return val;
}

static bool qmpReadBurst(uint8_t addr, uint8_t reg, uint8_t* buf, uint8_t len) {
    if (!M5.Ex_I2C.start(addr, false, I2C_FREQ) || !M5.Ex_I2C.write(&reg, 1)) {
        M5.Ex_I2C.stop();
        return false;
    }
    M5.Ex_I2C.stop();
    if (!M5.Ex_I2C.start(addr, true, I2C_FREQ) || !M5.Ex_I2C.read(buf, len)) {
        M5.Ex_I2C.stop();
        return false;
    }
    M5.Ex_I2C.stop();
    return true;
}

static void qmpWriteReg(uint8_t addr, uint8_t reg, uint8_t val) {
    M5.Ex_I2C.start(addr, false, I2C_FREQ);
    { uint8_t _b = (reg); M5.Ex_I2C.write(&_b, 1); }
    { uint8_t _b = (val); M5.Ex_I2C.write(&_b, 1); }
    (M5.Ex_I2C.stop(), 0);
}

// Convert two consecutive bytes (big-endian) to a signed 16-bit integer.
static int16_t be16s(const uint8_t* b) {
    return (int16_t)(((uint16_t)b[0] << 8) | b[1]);
}

// Convert two consecutive bytes (big-endian) to an unsigned 16-bit integer.
static uint16_t be16u(const uint8_t* b) {
    return ((uint16_t)b[0] << 8) | b[1];
}

// OTP仿射映射：K = A + S * otp / 32767  (datasheet §4.3 Conversion factors)
static float qmpCoef(int16_t otp, float A, float S) {
    return A + S * (float)otp / 32767.0f;
}

// Load calibration coefficients from QMP6988 OTP registers (datasheet §4.3).
// OTP布局 0xA0起25字节：b00,bt1,bt2,bp1,b11,bp2,b12,b21,bp3 各16bit BE，
// 然后 a0(0xB2,20bit),a1,a2，最后 0xB8 = b00_a0_ex（b00低4bit<<4 | a0低4bit）。
static bool qmpLoadCalibration(uint8_t addr) {
    uint8_t cal[25];
    if (!qmpReadBurst(addr, QMP_REG_CALIB, cal, 25)) return false;

    // 20bit带符号的 a0/b00（高16bit拼上扩展字节的4bit，算术右移补符号）
    int32_t b00_20 = ((int32_t)((be16u(cal + 0)  << 16) | ((cal[24] & 0xF0) << 8))) >> 12;
    int32_t a0_20  = ((int32_t)((be16u(cal + 18) << 16) | ((cal[24] & 0x0F) << 12))) >> 12;
    s_qmpB00 = (float)b00_20 / 16.0f;
    s_qmpA0  = (float)a0_20  / 16.0f;

    s_qmpBt1 = qmpCoef(be16s(cal + 2),  1.0e-01f,  9.1e-02f);
    s_qmpBt2 = qmpCoef(be16s(cal + 4),  1.2e-08f,  1.2e-06f);
    s_qmpBp1 = qmpCoef(be16s(cal + 6),  3.3e-02f,  1.9e-02f);
    s_qmpB11 = qmpCoef(be16s(cal + 8),  2.1e-07f,  1.4e-07f);
    s_qmpBp2 = qmpCoef(be16s(cal + 10), -6.3e-10f, 3.5e-10f);
    s_qmpB12 = qmpCoef(be16s(cal + 12), 2.9e-13f,  7.6e-13f);
    s_qmpB21 = qmpCoef(be16s(cal + 14), 2.1e-15f,  1.2e-14f);
    s_qmpBp3 = qmpCoef(be16s(cal + 16), 1.3e-16f,  7.9e-17f);
    s_qmpA1  = qmpCoef(be16s(cal + 20), -6.3e-03f, 4.3e-04f);
    s_qmpA2  = qmpCoef(be16s(cal + 22), -1.9e-11f, 1.2e-10f);

    s_qmpCalOk = true;
    return true;
}

// Read pressure (hPa) and temperature (°C) from QMP6988.
// Temperature output is a bonus; we only use it as a cross-check;
// humidity always comes from SHT31.
static bool qmpRead(uint8_t addr, float& pressure, float& tempOut) {
    if (!s_qmpCalOk) return false;

    // Trigger forced measurement: oversampling ×8 for both P and T (0b 101 101),
    // mode = forced (01). Register 0xF3.
    qmpWriteReg(addr, QMP_REG_CTRL_MEAS, 0xB5);  // 0b1011_0101
    delay(40);  // worst-case measurement time ≈ 37 ms at ×8 oversampling

    uint8_t raw[6];
    if (!qmpReadBurst(addr, QMP_REG_PRESS_MSB, raw, 6)) return false;

    // Raw 24-bit，减 2^23 偏置得到带符号的 Dt/Dp（第一版漏了这步 → 191.7hPa）
    uint32_t rawP = ((uint32_t)raw[0] << 16) | ((uint32_t)raw[1] << 8) | raw[2];
    uint32_t rawT = ((uint32_t)raw[3] << 16) | ((uint32_t)raw[4] << 8) | raw[5];
    float Dp = (float)((int32_t)rawP - 8388608);
    float Dt = (float)((int32_t)rawT - 8388608);

    // Datasheet §4.3 compensation（Tr单位是256*°C，Pr单位是Pa）
    float Tr = s_qmpA0 + s_qmpA1 * Dt + s_qmpA2 * Dt * Dt;

    float Pr = s_qmpB00
             + s_qmpBt1 * Tr
             + s_qmpBp1 * Dp
             + s_qmpB11 * Tr * Dp
             + s_qmpBt2 * Tr * Tr
             + s_qmpBp2 * Dp * Dp
             + s_qmpB12 * Dp * Tr * Tr
             + s_qmpB21 * Dp * Dp * Tr
             + s_qmpBp3 * Dp * Dp * Dp;

    pressure = Pr / 100.0f;   // Pa → hPa
    tempOut  = Tr / 256.0f;   // 256*°C → °C
    return true;
}

// ── QMP6988 initialisation ────────────────────────────────────────────────────
static bool qmpInit(uint8_t addr) {
    // Verify chip ID (should be 0x5C)
    uint8_t id = qmpReadReg(addr, QMP_REG_CHIP_ID);
    if (id != 0x5C) {
        // 2026/7/29：读回 0x00 说明寄存器读取协议不对，继续用会在
        // 读气压时卡死整台设备（实测 /env 请求打不通）。宁可不要这颗，
        // 也不能让它拖垮温湿度和整个 HTTP 服务。
        Serial.printf("[ENV] QMP6988 @ 0x%02X: chip ID 0x%02X (expected 0x5C) — 放弃这颗\n", addr, id);
        return false;
    }

    // Soft-reset
    qmpWriteReg(addr, QMP_REG_RESET, 0xE6);
    delay(20);

    // Standby mode while loading calibration
    qmpWriteReg(addr, QMP_REG_CTRL_MEAS, 0x00);

    return qmpLoadCalibration(addr);
}

// ── Public API ────────────────────────────────────────────────────────────────
// 在当前 Wire 配置下扫描一遍两颗传感器。返回是否找到任何一颗。
static bool scanCurrentBus();

bool initEnvService() {
    // 传感器可能挂在两条总线之一：
    //   a) 内部总线 (SDA=12 SCL=11) —— M5StackChan.begin() 已经初始化好
    //   b) 外部 Port A (G1/G2) —— Grove 红口或底部排针引出
    // 先扫内部，找不到就切到 Port A 再扫一遍。这样不管接哪边都能用。
    Serial.println("[ENV] Scanning Port A (G1/G2) for environment sensors...");

    // 只扫 Port A (G1/G2)，用第二个 I2C 外设 Wire1。
    //
    // 为什么不扫内部总线（SDA=12 SCL=11）：M5Unified 用自己的驱动
    // (M5.In_I2C) 占着那条总线，Arduino 的 Wire 对象从未被 begin 过——
    // 直接调用 Wire.beginTransmission() 会死等，把整个启动卡住。
    // 2026/7/29 实测两次烧录都停在扫描那一行不动。
    // 传感器接在排针/Grove红口上，本来就是 Port A，没必要碰内部总线。
    // 100kHz 不是 400kHz：实测穷举扫描在 100kHz 下能稳定看到 0x44/0x70，
    // 400kHz 下检测不到——Grove 排线偏长，上拉电阻扛不住高速。
    // 用 M5Unified 自己的外部 I2C（Port A）。
    // 不能用 Arduino 的 Wire/Wire1：Wire 被 M5Unified 内部总线占着，
    // Wire1 跟摄像头的 SCCB 抢同一个 I2C 外设（实测报
    // "i2c driver install error" 后卡死）。Ex_I2C 是库自己管的，不打架。
    M5.Ex_I2C.begin();
    delay(10);

    if (scanCurrentBus()) {
        s_usingPortA = true;
        Serial.println("[ENV] Sensors on Port A (Wire1).");
    } else {
        Serial.println("[ENV] No environment sensors detected on Port A.");
        return false;
    }

    Serial.printf("[ENV] Ready — SHT31:%s QMP6988:%s (bus: %s)\n",
                  s_sht31Found ? "yes" : "no",
                  s_qmpFound ? "yes" : "no",
                  s_usingPortA ? "PortA" : "internal");
    return true;
}

static bool scanCurrentBus() {
    s_sht31Found = false;
    s_qmpFound   = false;
    s_sht31Addr  = 0;
    s_qmpAddr    = 0;

    const uint8_t sht31Candidates[] = { SHT31_ADDR_PRIMARY, SHT31_ADDR_ALTERNATE };
    for (uint8_t candidate : sht31Candidates) {
        if (i2cProbe(candidate)) {
            s_sht31Addr  = candidate;
            s_sht31Found = true;
            sht31SoftReset(candidate);
            Serial.printf("[ENV] SHT31 found @ 0x%02X\n", candidate);
            break;
        }
    }

    const uint8_t qmpCandidates[] = { QMP_ADDR_PRIMARY, QMP_ADDR_ALTERNATE };
    for (uint8_t candidate : qmpCandidates) {
        if (i2cProbe(candidate)) {
            s_qmpAddr  = candidate;
            s_qmpFound = true;
            if (!qmpInit(candidate)) {
                Serial.printf("[ENV] QMP6988 @ 0x%02X: calibration failed, ignoring\n", candidate);
                s_qmpFound = false;
                s_qmpAddr  = 0;
            } else {
                Serial.printf("[ENV] QMP6988 found @ 0x%02X\n", candidate);
            }
            break;
        }
    }

    return s_sht31Found || s_qmpFound;
}

bool isEnvAvailable() {
    return s_sht31Found || s_qmpFound;
}


bool readEnv(float& temperature, float& humidity, float& pressure) {
    if (!s_sht31Found && !s_qmpFound) return false;

    temperature = NAN;
    humidity    = NAN;
    pressure    = NAN;

    if (s_sht31Found) {
        float t = NAN, h = NAN;
        // 注意：不要在这里重建总线。Wire1.end() 在 HTTP 处理线程里会
        // 把整个 server 卡死（2026/7/29 实测：ping 通但所有请求超时）。
        // 正确做法是在 setup() 里让 initEnvService() 最后跑，见 main.cpp。
        bool ok = sht31Read(s_sht31Addr, t, h);
        if (ok) {
            temperature = t;
            humidity    = h;
        } else {
            Serial.println("[ENV] SHT31 read failed");
        }
    }

    if (s_qmpFound) {
        float p = NAN, tQmp = NAN;
        if (qmpRead(s_qmpAddr, p, tQmp)) {
            pressure = p;
            // Use QMP temperature only when SHT31 is absent
            if (!s_sht31Found) temperature = tQmp;
        } else {
            Serial.println("[ENV] QMP6988 read failed");
        }
    }

    // Succeed as long as we got at least something
    return !isnan(temperature) || !isnan(humidity) || !isnan(pressure);
}

// 最后一次读取失败的原因（诊断用；串口在启动后不可靠）
const char* envLastError() { return s_lastError; }
