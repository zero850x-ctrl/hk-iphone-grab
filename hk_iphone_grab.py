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
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

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
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

try:
    from curl_cffi import requests as _http
    _USE_CURL = True
except ImportError:
    _USE_CURL = False

# 香港 Apple Store（storeNumber）
# 搶購時建議從瀏覽器匯出的 cookie（在產品頁完全載入後匯出）
GRAB_COOKIE_HINTS = ("shld_bt_ck", "shld_bt_m", "as_sfa", "dssid2")

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
        if _USE_CURL:
            self.session = _http.Session(impersonate="chrome131")
        else:
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
        """使用 retail/pickup-message API。"""
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
        if r.status_code == 541:
            raise RuntimeError(
                "pickup-message HTTP 541（Apple 暫時擋下，請加大 poll_interval_sec 或稍後再試）"
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

    def resolve_product_url(self, part: str) -> str:
        """用 part 編號解析實際產品頁（香港站會 redirect 到中文 slug）。"""
        url = f"{self.base}/product/{part}"
        r = self.session.get(url, timeout=25, allow_redirects=True)
        if r.status_code == 200 and "Page Not Found" not in r.text[:2000]:
            return r.url
        return url

    @staticmethod
    def extract_fnode(html: str) -> str:
        m = re.search(r"fnode=([a-f0-9]{40,})", html)
        return m.group(1) if m else ""

    def prepare_add_to_bag(self, product_url: str, part: str) -> Optional[str]:
        """預熱加購物袋所需 session（fnode / sba init / atbtoken）。"""
        if "/product/" in product_url:
            product_url = self.resolve_product_url(part)

        r = self.session.get(product_url, timeout=25)
        if r.status_code != 200:
            return None

        fnode = self.extract_fnode(r.text)
        self.session.get(f"{self.base}/dc", timeout=10)
        if fnode:
            init = self.session.get(
                f"{self.base}/sba/d/init",
                params={"fnode": fnode, "product": part},
                headers={"Referer": product_url, "Accept": "application/json"},
                timeout=20,
            )
            if init.status_code == 200:
                try:
                    if init.json().get("body", {}).get("status") != "OK":
                        return None
                except Exception:
                    return None

        return self.get_atb_token(product_url)

    @staticmethod
    def build_add_to_cart_url(product_url: str, part: str, atb: Optional[str] = None) -> str:
        encoded_part = quote(part, safe="")
        add_url = f"{product_url}?product={encoded_part}&add-to-cart=add-to-cart"
        if atb:
            add_url += f"&atbtoken={atb}"
        return add_url

    def add_to_bag(self, product_url: str, part: str) -> bool:
        """加入購物袋。通常需要從瀏覽器匯出的 cookie，否則可能 HTTP 541。"""
        atb = self.prepare_add_to_bag(product_url, part)
        add_url = self.build_add_to_cart_url(product_url, part, atb)

        r = self.session.get(
            add_url,
            headers={"Referer": product_url},
            timeout=25,
            allow_redirects=True,
        )
        if r.status_code == 541:
            return False
        if r.status_code != 200:
            return False

        bag = self.session.get(f"{self.base}/bag", timeout=25)
        part_key = part.split("/")[0]
        return part_key in bag.text

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


def build_product_url(locale: str, part: str) -> str:
    return f"https://www.apple.com/{locale}/shop/product/{part}"


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


def _session_cookie_names(session: Any) -> set[str]:
    jar = session.cookies
    if hasattr(jar, "keys"):
        return set(jar.keys())
    return {c.name for c in jar}


def validate_grab_cookies(client: AppleHKClient) -> List[str]:
    """回傳缺少的建議 cookie 名稱。"""
    names = _session_cookie_names(client.session)
    return [name for name in GRAB_COOKIE_HINTS if name not in names]


def _load_playwright_cookies(path: Path) -> List[Dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cookies: List[Dict[str, Any]] = []
    for c in data:
        same_site = c.get("sameSite")
        if same_site not in ("Strict", "Lax", "None"):
            same_site = "Lax"
        item: Dict[str, Any] = {
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain", ".apple.com"),
            "path": c.get("path", "/"),
            "secure": c.get("secure", True),
            "httpOnly": c.get("httpOnly", False),
            "sameSite": same_site,
        }
        if c.get("expirationDate"):
            item["expires"] = int(c["expirationDate"])
        cookies.append(item)
    return cookies


def _click_if_present(page: Any, selector: str, wait_ms: int = 600) -> bool:
    loc = page.locator(selector)
    if not loc.count():
        return False
    loc.first.click(force=True)
    page.wait_for_timeout(wait_ms)
    return True


def _click_label_if_present(page: Any, text: str, wait_ms: int = 600) -> bool:
    loc = page.locator("label").filter(has_text=text)
    if not loc.count():
        return False
    loc.first.click(force=True)
    page.wait_for_timeout(wait_ms)
    return True


def ensure_playwright_chrome() -> bool:
    """確保 Playwright 與 Chrome 已安裝。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("❌ 請先安裝: pip install playwright")
        print("   然後執行: python -m playwright install chrome")
        return False

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            browser.close()
        return True
    except Exception:
        print("正在安裝 Playwright Chrome（首次需幾分鐘）...")
        print("   指令: python -m playwright install chrome")
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chrome"],
            check=False,
        )
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel="chrome", headless=True)
                browser.close()
            return True
        except Exception:
            print("❌ Chrome 安裝失敗，請手動執行: python -m playwright install chrome")
            return False


def add_to_bag_browser(
    product_url: str,
    cookies_path: Path,
    variant: Dict[str, str],
    locale: str = "hk-zh",
    headless: bool = False,
    keep_open_sec: int = 90,
) -> bool:
    """用 Playwright 打開產品頁，自動選不換購/無 AppleCare+ 並加入購物袋。"""
    if not ensure_playwright_chrome():
        return False

    from playwright.sync_api import sync_playwright

    if not cookies_path.exists():
        return False

    pw_cookies = _load_playwright_cookies(cookies_path)
    bag_url = f"https://www.apple.com/{locale}/shop/bag"

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=headless)
        context = browser.new_context(locale="zh-HK", viewport={"width": 1400, "height": 1200})
        context.add_cookies(pw_cookies)
        page = context.new_page()

        print("🌐 打開產品頁，自動選購選項...")
        page.goto(product_url, wait_until="networkidle", timeout=90000)
        page.wait_for_timeout(1500)

        # 顏色 / 容量（產品頁 URL 有時未預選）
        variant_name = variant.get("name", "")
        if "宇宙橙" in variant_name:
            _click_label_if_present(page, "宇宙橙")
        elif "銀色" in variant_name:
            _click_label_if_present(page, "銀色")
        elif "深墨藍" in variant_name or "深藍" in variant_name:
            _click_label_if_present(page, "深墨藍")

        for storage in ("256GB", "512GB", "1TB", "2TB"):
            if storage in variant_name:
                _click_label_if_present(page, storage)
                break

        _click_if_present(page, "[data-autom='choose-noTradeIn']")
        _click_if_present(page, "input#noTradeIn")
        _click_if_present(page, "[data-autom='noapplecare']")

        btn = page.locator("button[name='add-to-cart']")
        btn.scroll_into_view_if_needed()
        for _ in range(24):
            disabled = btn.get_attribute("aria-disabled")
            if disabled != "true":
                break
            page.wait_for_timeout(500)

        if btn.get_attribute("aria-disabled") == "true":
            print("⚠️  加入購物袋按鈕仍未啟用，請在瀏覽器手動完成選項後按鈕")
            if not headless:
                page.wait_for_timeout(keep_open_sec * 1000)
            browser.close()
            return False

        print("🛒 按「加入購物袋」...")
        btn.click()
        page.wait_for_timeout(5000)

        for sel in (
            "[data-autom='proceed']",
            "button:has-text('檢視購物袋')",
            "button:has-text('查看購物袋')",
            "a[href*='/shop/bag']",
        ):
            if _click_if_present(page, sel, wait_ms=2000):
                break

        page.goto(bag_url, wait_until="networkidle", timeout=60000)
        body = page.inner_text("body")
        ok = "沒有任何項目" not in body and "no items" not in body.lower()

        if ok:
            print("✅ 瀏覽器已加入購物袋")
            if not headless:
                page.wait_for_timeout(3000)
        else:
            print("⚠️  購物袋仍為空，請在已打開的瀏覽器手動按「加入購物袋」")
            if not headless:
                page.goto(product_url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(keep_open_sec * 1000)

        browser.close()
        return ok


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
    product_url = build_product_url(cfg["locale"], part)

    client = AppleHKClient(cfg["locale"], cfg["location"])
    cookies_path = ROOT / cfg.get("cookies_file", "cookies.json")
    if cookies_path.exists():
        client.load_cookies_file(cookies_path)

    preferred = cfg.get("preferred_stores") or None
    interval = int(cfg.get("poll_interval_sec", 5))
    base_interval = interval
    seen: set[str] = set()
    grab_attempted = False
    mode = cfg.get("mode", "monitor")

    print("=" * 60)
    print(f"監控: {variant['model_name']} {variant['name']}")
    print(f"型號: {part}")
    print(f"偏好店舖: {', '.join(preferred or ['全部'])}")
    print(f"模式: {mode}" + (" (有貨時自動加購物袋)" if mode == "grab" else " (只通知，不加購物袋)"))
    if mode != "grab":
        print("提示: 要自動加購物袋請設 config mode=grab 或執行 python hk_iphone_grab.py --grab")
    print(f"間隔: {interval}s | 按 Ctrl+C 停止")
    print("=" * 60)

    try:
        while True:
            try:
                stores = client.check_pickup_stock([part])
                interval = base_interval
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
                        if cfg.get("open_browser_on_hit") and cfg.get("mode") != "grab":
                            webbrowser.open(hit.product_url)

                        if cfg.get("mode") == "grab" and not grab_attempted:
                            grab_attempted = True
                            run_grab(client, cfg, variant, hit)
                else:
                    ts = datetime.now().strftime("%H:%M:%S")
                    print(f"{ts} 無貨 — {variant['name']}", end="\r", flush=True)

            except Exception as e:
                err = str(e)
                if "541" in err:
                    interval = min(interval * 2, 60)
                    print(f"\n⚠️  {err}")
                    print(f"   已延長輪詢間隔至 {interval}s")
                else:
                    print(f"\n錯誤: {e} — {interval}s 後重試")

            if once:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n已停止監控")


def run_grab(
    client: AppleHKClient,
    cfg: Dict[str, Any],
    variant: Dict[str, str],
    hit: Optional[StockHit] = None,
) -> bool:
    """加購物袋 + 進結帳。需要 cookies.json。成功回傳 True。"""
    cookies_path = ROOT / cfg.get("cookies_file", "cookies.json")
    if not client.load_cookies_file(cookies_path):
        print(
            f"⚠️  找不到有效 cookie: {cookies_path}\n"
            "   請先登入 apple.com/hk，用瀏覽器擴充匯出 cookie 到 cookies.json"
        )
        return False

    missing = validate_grab_cookies(client)
    if missing:
        print("⚠️  cookies.json 可能過期，缺少: " + ", ".join(missing))
        print(
            "   請在 Chrome 打開產品頁、等頁面完全載入後，\n"
            "   用 Cookie-Editor 匯出 JSON 覆蓋 cookies.json"
        )

    part = variant["part"]
    product_url = client.resolve_product_url(part)
    headless = bool(cfg.get("browser_headless", False))

    print(f"啟動瀏覽器自動加購: {part} ...")
    if add_to_bag_browser(
        product_url,
        cookies_path,
        variant,
        locale=cfg["locale"],
        headless=headless,
        keep_open_sec=int(cfg.get("browser_keep_open_sec", 90)),
    ):
        if hit:
            print(
                f"\n📍 結帳時選店舖: {HK_STORES.get(hit.store_id, hit.store_name)} "
                f"(storeNumber={hit.store_id})\n"
                f"📅 取貨時間參考 API: {hit.pickup_quote} (日期碼 {hit.pickup_date})"
            )
        webbrowser.open(f"https://www.apple.com/{cfg['locale']}/shop/bag")
        return True

    print("❌ 瀏覽器自動加購未完成，請手動完成")
    return False


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
