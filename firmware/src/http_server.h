#pragma once
#include <stdint.h>
#include <stddef.h>

// HTTPサーバー初期化（setup()で呼ぶ）
void initHttpServer();

// HTTPリクエスト処理（loop()で呼ぶ）
void handleHttpServer();
