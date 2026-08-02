# 香港 Apple iPhone 搶購腳本

用 API 查庫存（比 Chrome 快），可選有貨時自動加購物袋並進結帳。

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

## 功能

- 輪詢 Apple `retail/pickup-message` API 查香港各店庫存
- 支援 iPhone 17 Pro / Pro Max 全型號（`ZA/A` 香港編號）
- 可篩選偏好店舖（銅鑼灣、ifc、又一城等）
- 可選 Telegram / Bark 通知
- 可選 API 加購物袋 + 進結帳（需瀏覽器 cookie）

## 快速開始

```bash
git clone https://github.com/zero850x-ctrl/hk-iphone-grab.git
cd hk-iphone-grab
pip install -r requirements.txt
cp config.example.json config.json
python hk_iphone_grab.py --list-stores
python hk_iphone_grab.py
```

## 2. 設定 config.json

| 欄位 | 說明 |
|------|------|
| `model_key` | `iphone_17_pro` 或 `iphone_17_pro_max` |
| `variant_key` | 見 `python hk_iphone_grab.py --list-models` |
| `preferred_stores` | 只通知這些店有貨 |
| `mode` | `monitor` 只監控；`grab` 有貨時加購物袋 |
| `poll_interval_sec` | 輪詢間隔，建議 5–10 秒 |

## 3. 常用指令

```powershell
python hk_iphone_grab.py --list-models
python hk_iphone_grab.py --list-store-ids
python hk_iphone_grab.py --list-stores
python hk_iphone_grab.py
python hk_iphone_grab.py --once
python hk_iphone_grab.py --grab
```

## 4. 選店舖

在 `preferred_stores` 填入店舖 ID：R409 銅鑼灣、R428 ifc、R499 尖沙咀、R673 apm、R485 又一城、R610 新城市。

## 5. 取貨時間

`--list-stores` 會顯示 `pickupSearchQuote`（如「今日：今日」）與日期碼 `pickupEncodedUpperDateString`。

結帳頁選店舖與時段在 checkout UI 完成；API 搶購模式會打開結帳頁後由你手動選店。

## 6. Cookie（搶購模式）

Chrome 登入 apple.com/hk-zh，用 Cookie-Editor 匯出 JSON 存為 `cookies.json`。

開賣前 10 分鐘更新 cookie，執行 `python hk_iphone_grab.py --grab`。
