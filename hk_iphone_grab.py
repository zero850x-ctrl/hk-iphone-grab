#!/usr/bin/env python3
"""
香港 Apple Store iPhone 搶購 / 庫存監控腳本

API 端點（2026 實測可用）:
  GET /hk-zh/shop/retail/pickup-message  — 查各店庫存與取貨時間文案
  GET /hk-zh/shop/beacon/atb            — 拿 atbtoken（加購物袋用）
  GET 產品頁?add-to-cart=...            — 加入購物袋（需瀏覽器 cookie）
  POST /shop/bagx?_a=checkout_now       — 進入結帳
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

# Windows 終端機 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 香港 Apple Store（storeNumber）
HK_STORES = {
    "R409": "銅鑼灣 Causeway Bay",
    "R428": "ifc mall",
    "R499": "尖沙咀 Canton Road",
    "R673": "apm Hong Kong",
    "R485": "又一城 Festival Walk",
    "R610": "新城市廣場 New Town Plaza",
}


@dataclass
class StockHit:
    part: str
    variant_name: str
    store_id: str
    store_name: str
    pickup_display: str
    pickup_quote: str
    pickup_date: str
    product_url: str


class AppleHKClient:
    def __init__(self, locale: str = "hk-zh", location: str = "香港") -> None:
        self.locale = locale
        self.location = location
        self.base = f"https://www.apple.com/{locale}/shop"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
            }
        )

    def _json_headers(self, referer: str) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "Referer": referer,
            "Origin": "https://www.apple.com",
            "content-type": "application/json;encoding=UTF8;charset=UTF-8",
        }

    def warmup(self, referer_path: str) -> None:
        url = f"{self.base}/{referer_path}"
        self.session.get(url, timeout=25)
        self.session.get(f"{self.base}/dc", timeout=10)

    def load_cookies_file(self, path: Path) -> bool:
        if not path.exists():
            return False
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return False

        # 純 Cookie header 字串
        if raw.startswith("Cookie:"):
            raw = raw.split(":", 1)[1].strip()
        if "=" in raw and not raw.startswith("[") and not raw.startswith("{"):
            for pair in raw.split(";"):
                pair = pair.strip()
                if "=" in pair:
                    name, value = pair.split("=", 1)
                    self.session.cookies.set(name.strip(), value.strip(), domain=".apple.com")
            return True

        data = json.loads(raw)
        if isinstance(data, list):
            for c in data:
                self.session.cookies.set(
                    c["name"],
                    c["value"],
                    domain=c.get("domain", ".apple.com"),
                    path=c.get("path", "/"),
                )
            return True
        if isinstance(data, dict) and "cookie" in data:
            return self.load_cookies_from_header(data["cookie"])
        return False

    def load_cookies_from_header(self, header: str) -> bool:
        for pair in header.split(";"):
            pair = pair.strip()
            if "=" in pair:
                name, value = pair.split("=", 1)
                self.session.cookies.set(name.strip(), value.strip(), domain=".apple.com")
        return True

    def check_pickup_stock(self, parts: List[str]) -> List[Dict[str, Any]]:
        """使用 retail/pickup-message API（無需 cookie，實測可用）。"""
        referer = f"{self.base}/buy-iphone/iphone-17-pro"
        self.warmup("buy-iphone/iphone-17-pro")

        params: Dict[str, str] = {
            "pl": "true",
            "location": self.location,
            "mts.0": "regular",
        }
        for i, part in enumerate(parts):
            params[f"parts.{i}"] = part
            params[f"mts.{i}"] = "regular"

        r = self.session.get(
            f"{self.base}/retail/pickup-message",
            params=params,
            headers=self._json_headers(referer),
            timeout=20,
        )
        if r.status_code != 200:
            raise RuntimeError(f"pickup-message HTTP {r.status_code}")

        body = r.json().get("body", {})
        stores = body.get("stores") or []
        if not stores and body.get("errorMessage"):
            raise RuntimeError(f"API 錯誤: {body.get('errorMessage')}")
        return stores

    def get_atb_token(self, product_url: str) -> Optional[str]:
        r = self.session.get(
            f"{self.base}/beacon/atb",
            headers={**self._json_headers(product_url), "Accept": "*/*"},
            timeout=15,
        )
        if r.status_code != 200:
            return self.session.cookies.get("as_atb")
        try:
            token = r.json().get("token")
            if token:
                return token
        except Exception:
            pass
        return self.session.cookies.get("as_atb")

    def add_to_bag(self, product_url: str, part: str) -> bool:
        """加入購物袋。通常需要從瀏覽器匯出的 cookie，否則可能 HTTP 541。"""
        self.session.get(product_url, timeout=25)
        atb = self.get_atb_token(product_url)
        add_url = f"{product_url}?product={part}&add-to-cart=add-to-cart"
        if atb:
            add_url += f"&atbtoken={atb}"

        r = self.session.get(
            add_url,
            headers={"Referer": product_url},
            timeout=25,
            allow_redirects=True,
        )
        if r.status_code == 541:
            return False
        return r.status_code == 200

    def checkout_now(self) -> Optional[str]:
        """進入結帳，回傳 checkout URL。"""
        bag_url = f"{self.base}/bag"
        r = self.session.post(
            f"{self.base}/bagx",
            params={"_a": "checkout_now"},
            headers={
                "Referer": bag_url,
                "X-Requested-With": "Fetch",
                "Accept": "application/json",
            },
            timeout=25,
        )
        if r.status_code == 541:
            return None

        # JSON 回應或 redirect
        try:
            data = r.json()
            meta = data.get("body", {}).get("meta", {})
            url = meta.get("h", {}).get("url") or meta.get("page", {}).get("url")
            if url:
                return url if url.startswith("http") else f"https://www.apple.com{url}"
        except Exception:
            pass

        if r.url and "checkout" in r.url:
            return r.url
        return f"{self.base}/checkout"


def load_models() -> Dict[str, Any]:
    return json.loads((ROOT / "models.json").read_text(encoding="utf-8"))


def resolve_variant(models: Dict[str, Any], model_key: str, variant_key: str) -> Dict[str, str]:
    model = models.get(model_key)
    if not model:
        raise KeyError(f"未知 model_key: {model_key}")
    variant = model["variants"].get(variant_key)
    if not variant:
        raise KeyError(f"未知 variant_key: {variant_key}")
    return {
        "model_name": model["name"],
        "buy_slug": model["buy_slug"],
        **variant,
    }


def build_product_url(locale: str, buy_slug: str, product_path: str) -> str:
    return f"https://www.apple.com/{locale}/shop/{buy_slug}/{product_path}"


def parse_stock_hits(
    stores: List[Dict[str, Any]],
    part: str,
    variant_name: str,
    product_url: str,
    preferred: Optional[List[str]] = None,
) -> List[StockHit]:
    hits: List[StockHit] = []
    for store in stores:
        store_id = store.get("storeNumber", "")
        if preferred and store_id not in preferred:
            continue

        info = store.get("partsAvailability", {}).get(part, {})
        display = info.get("pickupDisplay", "")
        if display != "available":
            continue

        regular = info.get("messageTypes", {}).get("regular", {})
        hits.append(
            StockHit(
                part=part,
                variant_name=variant_name,
                store_id=store_id,
                store_name=store.get("storeName", store_id),
                pickup_display=display,
                pickup_quote=info.get("pickupSearchQuote", ""),
                pickup_date=store.get("pickupEncodedUpperDateString", ""),
                product_url=product_url,
            )
        )
    return hits


def notify_telegram(token: str, chat_id: str, text: str) -> None:
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=15,
    )


def notify_bark(url: str, title: str, body: str, open_url: str = "") -> None:
    payload: Dict[str, Any] = {"title": title, "body": body, "level": "timeSensitive"}
    if open_url:
        payload["url"] = open_url
    requests.post(url, json=payload, timeout=15)


def format_hit(hit: StockHit) -> str:
    store_label = HK_STORES.get(hit.store_id, hit.store_name)
    return (
        f"🍎 有貨！{hit.variant_name}\n"
        f"店舖: {store_label} ({hit.store_id})\n"
        f"取貨: {hit.pickup_quote}\n"
        f"日期碼: {hit.pickup_date}\n"
        f"連結: {hit.product_url}"
    )


def list_all_stores(client: AppleHKClient, part: str) -> None:
    stores = client.check_pickup_stock([part])
    print(f"\n型號 {part} — 香港各店庫存 ({datetime.now():%Y-%m-%d %H:%M:%S})\n")
    print(f"{'Store ID':<8} {'店舖':<28} {'狀態':<12} {'取貨時間'}")
    print("-" * 72)
    for store in stores:
        sid = store.get("storeNumber", "")
        name = store.get("storeName", "")
        info = store.get("partsAvailability", {}).get(part, {})
        display = info.get("pickupDisplay", "?")
        quote = info.get("pickupSearchQuote", "")
        date_code = store.get("pickupEncodedUpperDateString", "")
        label = HK_STORES.get(sid, name)
        print(f"{sid:<8} {label:<28} {display:<12} {quote}  [{date_code}]")


def run_monitor(cfg: Dict[str, Any], once: bool = False) -> None:
    models = load_models()
    variant = resolve_variant(models, cfg["model_key"], cfg["variant_key"])
    part = variant["part"]
    product_url = build_product_url(cfg["locale"], variant["buy_slug"], variant["product_path"])

    client = AppleHKClient(cfg["locale"], cfg["location"])
    preferred = cfg.get("preferred_stores") or None
    interval = int(cfg.get("poll_interval_sec", 5))
    seen: set[str] = set()

    print("=" * 60)
    print(f"監控: {variant['model_name']} {variant['name']}")
    print(f"型號: {part}")
    print(f"偏好店舖: {', '.join(preferred or ['全部'])}")
    print(f"間隔: {interval}s | 按 Ctrl+C 停止")
    print("=" * 60)

    while True:
        try:
            stores = client.check_pickup_stock([part])
            hits = parse_stock_hits(stores, part, variant["name"], product_url, preferred)

            if hits:
                for hit in hits:
                    key = f"{hit.store_id}:{hit.part}"
                    if key in seen:
                        continue
                    seen.add(key)
                    msg = format_hit(hit)
                    print(f"\n{msg}\n")

                    if cfg.get("telegram", {}).get("enabled"):
                        notify_telegram(
                            cfg["telegram"]["bot_token"],
                            cfg["telegram"]["chat_id"],
                            msg,
                        )
                    if cfg.get("bark", {}).get("enabled"):
                        notify_bark(
                            cfg["bark"]["url"],
                            "iPhone 有貨",
                            msg,
                            hit.product_url,
                        )
                    if cfg.get("open_browser_on_hit"):
                        webbrowser.open(hit.product_url)

                    if cfg.get("mode") == "grab":
                        run_grab(client, cfg, variant, hit)
            else:
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"{ts} 無貨 — {variant['name']}", end="\r", flush=True)

        except Exception as e:
            print(f"\n錯誤: {e} — {interval}s 後重試")

        if once:
            break
        time.sleep(interval)


def run_grab(
    client: AppleHKClient,
    cfg: Dict[str, Any],
    variant: Dict[str, str],
    hit: Optional[StockHit] = None,
) -> None:
    """加購物袋 + 進結帳。需要 cookies.json。"""
    cookies_path = ROOT / cfg.get("cookies_file", "cookies.json")
    if not client.load_cookies_file(cookies_path):
        print(
            f"⚠️  找不到有效 cookie: {cookies_path}\n"
            "   請先登入 apple.com/hk，用瀏覽器擴充匯出 cookie 到 cookies.json"
        )
        return

    product_url = build_product_url(cfg["locale"], variant["buy_slug"], variant["product_path"])
    part = variant["part"]

    print(f"嘗試加入購物袋: {part} ...")
    ok = client.add_to_bag(product_url, part)
    if not ok:
        print("❌ 加購物袋失敗 (HTTP 541 = 需更新 cookie 或 Akamai 擋下)")
        print(f"   手動開啟: {product_url}")
        if cfg.get("open_browser_on_hit"):
            webbrowser.open(product_url)
        return

    print("✅ 已加入購物袋，進入結帳...")
    checkout_url = client.checkout_now()
    if checkout_url:
        print(f"結帳頁: {checkout_url}")
        if hit:
            print(
                f"\n📍 結帳時選店舖: {HK_STORES.get(hit.store_id, hit.store_name)} "
                f"(storeNumber={hit.store_id})\n"
                f"📅 取貨時間參考 API: {hit.pickup_quote} (日期碼 {hit.pickup_date})"
            )
        if cfg.get("open_browser_on_hit"):
            webbrowser.open(checkout_url)
    else:
        print("❌ checkout_now 失敗，請手動打開購物袋")
        webbrowser.open(f"https://www.apple.com/{cfg['locale']}/shop/bag")


def list_models() -> None:
    models = load_models()
    print("\n可選 model_key / variant_key:\n")
    for model_key, model in models.items():
        print(f"  [{model_key}] {model['name']}")
        for vk, v in model["variants"].items():
            print(f"    {vk:25} {v['part']:12} {v['name']}")


def list_store_ids() -> None:
    print("\n香港 Apple Store ID:\n")
    for sid, name in HK_STORES.items():
        print(f"  {sid}  {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="香港 Apple iPhone 庫存監控 / 搶購")
    parser.add_argument("--config", default=str(ROOT / "config.json"), help="設定檔路徑")
    parser.add_argument("--once", action="store_true", help="只查一次")
    parser.add_argument("--list-stores", action="store_true", help="列出各店庫存")
    parser.add_argument("--list-models", action="store_true", help="列出型號編號")
    parser.add_argument("--list-store-ids", action="store_true", help="列出店舖 ID")
    parser.add_argument("--grab", action="store_true", help="有貨時嘗試 API 加購物袋+結帳")
    args = parser.parse_args()

    if args.list_models:
        list_models()
        return
    if args.list_store_ids:
        list_store_ids()
        return

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.grab:
        cfg["mode"] = "grab"

    if args.list_stores:
        models = load_models()
        variant = resolve_variant(models, cfg["model_key"], cfg["variant_key"])
        client = AppleHKClient(cfg["locale"], cfg["location"])
        list_all_stores(client, variant["part"])
        return

    run_monitor(cfg, once=args.once)


if __name__ == "__main__":
    main()
