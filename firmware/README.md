# Stack-chan Firmware

**M5Stack CoreS3 firmware for the Stack-chan MCP robot**

PC/Mac 上の MCP サーバーや補助ツールから、M5Stack CoreS3 上の Stack-chan を HTTP で制御するためのファームウェアです。
音声再生、録音取得、表情表示、サーボ動作、トップのタッチストリップ、カメラ撮影、環境センサー取得を担当します。

---

## ✨ 特徴

- **HTTP 音声再生**: `POST /play` で WAV URL を再生、`POST /play/pcm` と TCP PCM ストリームで低遅延 PCM 再生
- **録音の MCP pull モード**: `POST /mode` で録音状態を初期化し、`GET /audio/status` と `GET /audio` で取得
- **表情・動作・視覚**: `POST /face`、`POST /move`、`POST /nod`、`POST /shake`、`GET /snapshot`
- **タッチ操作**: タップでウェイクワード不要の録音を開始し、往復スワイプで撫で動作（5 秒間の happy 表情 + 首振り）
- **診断エンドポイント**: `GET /playback/status`、`GET /servo/status`、`GET /touch/status`、`GET /env`、`GET /env/debug`
- **Arduino / PlatformIO ベース**: CoreS3 向けの C++ ファームウェア

---

## 🔧 対応ハードウェア

| ハードウェア | 備考 |
|-------------|------|
| M5Stack CoreS3 | 推奨ターゲット |
| Stack-chan 本体 + サーボ | 首振り動作に使用 |
| M5Stack ENV III Unit | 温度・湿度・気圧の取得に対応 |

---

## ⚙️ セットアップ

### 1. `src/config.h` を用意

例から設定ファイルを作成し、Wi-Fi など必要な値を設定します。

```bash
cp config.h.example src/config.h
```

`src/config.h` では少なくとも Wi-Fi 設定を見直してください。
既存のローカル秘密情報は上書きせず、例ファイルをベースに編集します。

### 2. ビルドと書き込み

```bash
pio run
pio run -t upload
```

シリアル確認:

```bash
pio device monitor
```

表情アセットを差し替える場合は、LittleFS への `uploadfs` ではなく、
`scripts/generate_gif_assets.py` で `gif_assets.h` を再生成します。

---

## 📡 主なエンドポイント

| エンドポイント | 用途 |
|--------------|------|
| `POST /play` | WAV URL を受け取って再生 |
| `POST /play/pcm` | 24kHz mono s16le PCM を受け取って再生またはキュー投入 |
| `POST /mode` | 録音状態を初期化 (`mode` は `mcp` のみ) |
| `GET /audio/status` | 録音完了フラグと録音元 (`voice` / `touch`) を確認 |
| `GET /audio` | 録音済み WAV を取得 |
| `POST /move` / `POST /home` / `POST /nod` / `POST /shake` | 頭の向きとジェスチャー制御 |
| `POST /face` / `GET /face` | 表情を変更 / 確認 |
| `GET /snapshot` | カメラ JPEG を取得 |
| `GET /playback/status` | 音声再生・PCM キュー診断 |
| `GET /servo/status` | サーボ状態診断 |
| `GET /touch/status` | タッチ強度、録音開始回数、直近ジェスチャー、カメラ後の I2C 復帰失敗回数 |
| `GET /env` | 温度・湿度・気圧を取得 |
| `GET /env/debug` | 環境センサーの診断情報を取得 |

CoreS3 のカメラ SCCB と内部 I2C（タッチ・環境センサー）は GPIO 11/12 を共有します。
そのためカメラは常時起動せず、`GET /snapshot` の間だけタッチを停止して使用し、
成功・失敗のどちらでも内部 I2C とタッチドライバーを復帰させます。

---

## Optional Outbound WSS

Direct HTTP, TCP PCM and UDP PCM remain available. The outbound channel is
disabled by default. Provision the following defines separately for deployment;
do not commit tokens or create a private configuration for a rehearsal build:

| Define | Default |
|---|---|
| `STACKCHAN_OUTBOUND_ENABLED` | `0` |
| `STACKCHAN_OUTBOUND_HOST` | `""` (DNS hostname, no scheme/path) |
| `STACKCHAN_OUTBOUND_PORT` | `443` |
| `STACKCHAN_OUTBOUND_DEVICE_TOKEN` | `""` (device-only Bearer token) |
| `STACKCHAN_OUTBOUND_CA_PEM` | `""` (complete PEM trust anchor) |
| `STACKCHAN_OUTBOUND_NTP_SERVER` | `"pool.ntp.org"` |

The path is fixed at `/stackchan/ws`. Missing/invalid configuration or an invalid
CA disables the service. It waits for a plausible synchronized clock before
connecting. `beginSslWithCA()` enables CA and hostname verification; authorization
is set afterwards using `setAuthorization("Bearer ...")`. There is no insecure
fallback, URL token, payload token, or WebSockets debug logging.

A core-0 FreeRTOS task (priority 1, 12 KiB stack) exclusively owns WebSockets.
One bounded request/result slot hands commands to the main loop, so outbound
camera/I2C/servo/recording operations do not run concurrently with HTTP handlers.
Disconnect invalidates the connection generation: unexecuted requests are skipped,
late results are discarded, and only outbound-owned queued/staged PCM is cleared.
Every outbound PCM buffer carries its connection generation. All stale-generation
queues and staging are swept, not just the most recent session; generation is
checked again at playback start after any microphone/speaker setup waits.
An already-running operation or active audio segment may finish. There is no
command replay after reconnect. Heartbeats run every 10 seconds; commands time
out after 20 seconds and pending binary/fragment assembly after 5 seconds.
Every request must carry a positive int64 `expires_at_ms` Unix timestamp in
milliseconds. UTC expiry is checked before hardware dispatch and replies in
addition to the generation and monotonic timeout, rejecting TCP-delayed commands.
Expiry more than 25 seconds ahead of UTC (20 seconds plus 5 seconds of clock-skew
tolerance) also fails closed, rather than trusting a clock that is far behind.

Outbound routes: GET `/status` (playback diagnostics alias), `/playback/status`,
`/env`, `/face`, `/snapshot`, `/audio/status`, `/audio`; POST `/mode`, `/face`, `/move`,
`/home`, `/nod`, `/shake`, `/play/pcm`. Other endpoints remain direct-only.
PCM is mono 24 kHz s16le. Existing staged playback supports up to 2 MiB per
utterance. Incoming PCM bodies and JPEG replies are capped at 128 KiB; recorded
WAV replies alone may reach 512 KiB (the current 8-second recording is at most
256044 bytes). JSON envelopes remain capped at 8 KiB. Binary messages use `SCB1`
plus the 32-character request ID plus bytes. Binary replies are copied into
bounded PSRAM before leaving the main loop.

The voice bridge polls `/audio/status`, then intentionally consumes `/audio`.
`GET /audio` is not a health check and the firmware does not push voice uploads.
An oversized WAV or failed response allocation leaves the recording ready; a
successful response copy marks it consumed. A later timeout/disconnect may lose
that delivery and must not cause an automatic retry or replay. Direct HTTP keeps
its existing read-once behavior.

`scripts/websocket_limits.py` checks the pinned WebSockets 2.7.2 package version
and patches only this worktree's generated `.pio/libdeps` copy: its hard-coded
15 KiB frame limit becomes overrideable, and receive payloads use PSRAM. The
build sets `WEBSOCKETS_MAX_DATA_SIZE=131108` (128 KiB + 36-byte envelope); a
compile-time assertion verifies the override. Fragmented messages are also
bounded. A dependency upgrade must revalidate this compatibility patch.

Rehearsal checks (no device or private `src/config.h` needed):

```sh
pio run -e m5stack-cores3-public
pio check -e m5stack-cores3-public --severity=high --fail-on-defect=high
pio test -e native
```

The public environment defines `STACKCHAN_USE_PUBLIC_CONFIG`, causing the loader
to include only `config.h.example` and defaults, even if private `config.h` exists.
Native tests cover wire validation, size limits, fragmentation, cancellation
eligibility, GET body policy, bounded WAV/JPEG response copies, and actual PCM
queue/staging ownership with hardware stubs. They do not verify live TLS, I2S
timing, camera behavior, or firmware upload.

---

## 😀 表情アセット

現在の表情は `firmware/src/gif_assets.h` にコンパイル済みアセットとして含まれます。
`firmware/data/` へ GIF を置いて `uploadfs` する旧方式ではありません。

独自表情へ差し替える場合は、リポジトリルートから次を実行します。

```bash
python3 scripts/generate_gif_assets.py
cd firmware && pio run
```

入力は `firmware/data/A_calm.gif`、`B_thinking.gif` など、7表情すべてが
必要です。`--check` を指定すると、書き込まずに生成済みヘッダーとの一致を
確認できます。

---

## 🙏 クレジット

- [Stack-chan](https://github.com/m5stack/StackChan)
- [AnimatedGIF](https://github.com/bitbank2/AnimatedGIF)

---

## 📄 ライセンス

MIT
