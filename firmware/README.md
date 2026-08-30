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
