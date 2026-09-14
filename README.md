# 香港 Apple iPhone 搶購腳本

用 Apple 香港官網 API 查各店取貨庫存，有貨時用專用 Chrome 自動選購並嘗試加入購物袋。

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

## 程式在做什麼

```text
config.json 指定要搶的型號
        │
        ▼
每 N 秒查 pickup-message API（比人手刷新產品頁快）
        │
        ├── 無貨 → 繼續輪詢
        └── 有貨 → 通知（終端機 / Telegram / Bark）
                    │
                    └── --grab 模式：
                          已預先打開的 Chrome
                          （已選：顏色、容量、不換購、無 AppleCare+）
                          立刻按「加入購物袋」
                          視窗保持開啟，你手動結帳、選店取貨
```

兩條線分開：

| 線 | 做什麼 | 靠什麼 |
|----|--------|--------|
| 監控 | 查哪間店有貨 | `retail/pickup-message` API |
| 加購 | 把手機放進購物袋 | 長駐 Chrome（Playwright），不是純 API |

Apple 會擋腳本直接 `add-to-cart`（HTTP 541），所以加購必須走瀏覽器。

## 1. 安裝

```powershell
cd hk-iphone-grab
pip install -r requirements.txt
python -m playwright install chrome
copy config.example.json config.json
```

## 2. 選要搶的型號（model_key / variant_key）

`models.json` 是本地型號表。新一代 iPhone 開賣前，先從官網拉最新清單：

```powershell
python hk_iphone_grab.py --refresh-models
```

這會：

1. 讀 `https://www.apple.com/hk-zh/shop/buy-iphone`
2. 解析各系列購買頁的 `productSelectionData`
3. 覆寫 `models.json`
4. 印出所有 `model_key` / `variant_key`

只看現有清單（不連網）：

```powershell
python hk_iphone_grab.py --list-models
```

連網更新再列出：

```powershell
python hk_iphone_grab.py --list-models --refresh-models
```

寫進 `config.json`（例如 17 Pro 256GB 宇宙橙）：

```powershell
python hk_iphone_grab.py --set-model iphone_17_pro --set-variant 256gb_cosmic_orange
```

`config.json` 相關欄位：

| 欄位 | 說明 |
|------|------|
| `model_key` | 系列，例如 `iphone_17_pro`、`iphone_17_pro_max` |
| `variant_key` | 容量+顏色，例如 `256gb_cosmic_orange` |
| `preferred_stores` | 只盯這些店，見下方店舖 ID |
| `poll_interval_sec` | 查庫存間隔，建議 10–15 秒（太密會 HTTP 541） |
| `mode` | `monitor` 只通知；`grab` 有貨時加購物袋 |
| `browser_profile_dir` | 搶購 Chrome 設定檔，預設 `.chrome-grab` |
| `chrome_cdp` | 選填。連到已開的 Chrome，例如 `http://127.0.0.1:9222` |

## 3. 店舖 ID

```powershell
python hk_iphone_grab.py --list-store-ids
```

| ID | 店舖 |
|----|------|
| R409 | 銅鑼灣 Causeway Bay |
| R428 | ifc mall |
| R499 | 尖沙咀 Canton Road |
| R673 | apm Hong Kong |
| R485 | 又一城 Festival Walk |
| R610 | 新城市廣場 New Town Plaza |

查目前各店庫存（用 config 裡的型號）：

```powershell
python hk_iphone_grab.py --list-stores
```

## 4. 日常用法

### 只監控（有貨就印出來／開瀏覽器）

把 `config.json` 的 `mode` 設成 `monitor`，或直接：

```powershell
python hk_iphone_grab.py
python hk_iphone_grab.py --once
```

### 搶購（建議開賣當天）

```powershell
python hk_iphone_grab.py --grab
```

流程：

1. 腳本打開**專用 Chrome**（`.chrome-grab`，不是你平常那個視窗）
2. **第一次**請在該視窗登入 Apple 帳户
3. 自動打開產品頁，預選顏色／容量／不換購／無 AppleCare+
4. 背景繼續查庫存
5. 有貨 → 立刻按「加入購物袋」
6. 視窗保持開啟，你在**同一個視窗**結帳、選取貨店

測試加購（不等庫存，用來確認登入與按鈕）：

```powershell
python hk_iphone_grab.py --try-bag
```

**請只看腳本打開的 Chrome。** 自己平常開的瀏覽器購物袋可以是空的——那是另一個登入狀態。

## 5. 指令一覽

```powershell
python hk_iphone_grab.py --refresh-models
python hk_iphone_grab.py --list-models
python hk_iphone_grab.py --list-models --refresh-models
python hk_iphone_grab.py --set-model iphone_17_pro --set-variant 256gb_cosmic_orange
python hk_iphone_grab.py --list-store-ids
python hk_iphone_grab.py --list-stores
python hk_iphone_grab.py
python hk_iphone_grab.py --once
python hk_iphone_grab.py --grab
python hk_iphone_grab.py --try-bag
```

## 6. 完整 workflow（開賣日）

```text
前一天
  登入專用 Chrome 測一次 --try-bag（確認帳户、付款資料）
  python hk_iphone_grab.py --refresh-models
  python hk_iphone_grab.py --set-model iphone_17_pro --set-variant 256gb_cosmic_orange

開賣前 10–20 分鐘
  python hk_iphone_grab.py --grab
  在彈出的 Chrome 登入（若尚未登入）
  不要關閉該視窗

開賣瞬間
  腳本會：
    - 偵測官網是否出現 iphone-18-pro 等新購買頁
    - 自動切到同一系列新一代 + 最接近的容量／顏色
    - 若停在「即將發售／活動／空氣頁」，定期重載直到出現「加入購物袋」
    - 有貨就按加入購物袋
  你在同一視窗選店、結帳、付款
```

`--grab` 預設 `follow_latest: true`。若你只想盯 config 裡的舊款，在 `config.json` 設 `"follow_latest": false`。

### 這能避免什麼、不能避免什麼

| 情況 | 能否避開 |
|------|----------|
| 停在舊款產品頁，別人已在買新一代 | 盡量：偵測新購買頁並切換 |
| 停在即將發售／活動／空氣頁 | 盡量：定期重載直到有「加入購物袋」 |
| Apple 排隊頁、541 風控、結帳驗證 | 不能保證：還是要人手在專用 Chrome 完成結帳 |
| 比已在結帳頁的人更快付款 | 不能：加進袋之後的店舖／付款仍要你自己按 |

開賣不是「載入對的 SKU 就贏」。真正關閘是結帳與風控。這支腳本的目標是：**不要停在錯頁**，讓你在正確的可購買頁上搶按加入購物袋。

## 7. 通知（選用）

`config.json`：

- Telegram：`telegram.enabled = true`，填 `bot_token`、`chat_id`
- Bark：`bark.enabled = true`，填你的 Bark URL

## 8. 常見問題

**購物袋是空的**  
看的是不是腳本那個 Chrome。加購失敗時，請在該視窗手動再按一次「加入購物袋」。

**HTTP 541**  
Apple 暫時擋查詢。把 `poll_interval_sec` 加大（15–30），或換網絡／等幾分鐘。

**`playwright` 不是指令**  
用 `python -m playwright install chrome`。

**換下一代 iPhone**  
跑 `--refresh-models`，再用 `--list-models` 看新的 `model_key` / `variant_key`，`--set-model` / `--set-variant` 寫入 config。不必手改 `models.json`。
