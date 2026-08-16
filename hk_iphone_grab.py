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
import html as html_lib
import json
import re
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
        if getattr(self, "_warmed", False):
            return
        url = f"{self.base}/{referer_path}"
        self.session.get(url, timeout=25)
        self.session.get(f"{self.base}/dc", timeout=10)
        self._warmed = True

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


def save_models(models: Dict[str, Any]) -> Path:
    path = ROOT / "models.json"
    path.write_text(json.dumps(models, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _plain_text(raw: str) -> str:
    text = html_lib.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\xa0", " ").replace("&nbsp;", " ")
    text = re.sub(r"註腳\s*\d+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _family_to_model_key(family: str) -> str:
    s = (family or "").lower()
    s = s.replace("promax", "_pro_max").replace("plus", "_plus")
    s = s.replace("pro", "_pro")
    s = re.sub(r"iphone(\d+)", r"iphone_\1", s)
    s = re.sub(r"iphone(?![_\d])", "iphone_", s)
    return re.sub(r"_+", "_", s).strip("_")


def _color_to_variant_suffix(color: str) -> str:
    c = re.sub(r"[^a-z0-9]", "", (color or "").lower())
    known = {
        "cosmicorange": "cosmic_orange",
        "deepblue": "deep_blue",
        "naturaltitanium": "natural_titanium",
        "bluetitanium": "blue_titanium",
        "whitetitanium": "white_titanium",
        "blacktitanium": "black_titanium",
        "desertsand": "desert_sand",
        "spaceblack": "space_black",
        "stargaze": "stargaze",
        "lightgold": "light_gold",
        "cloudwhite": "cloud_white",
        "skyeblue": "sky_blue",
    }
    if c in known:
        return known[c]
    c = re.sub(r"(orange|blue|black|white|gold|titanium|silver|pink|green|yellow)$", r"_\1", c)
    return c.strip("_") or "unknown"


def _product_path(product: Dict[str, Any]) -> str:
    token = (product.get("seoUrlToken") or "").strip()
    if token:
        return token
    screen = (product.get("dimensionScreensize") or "").replace("_", ".").replace("inch", "-inch-display")
    cap = product.get("dimensionCapacity") or ""
    color = _color_to_variant_suffix(product.get("dimensionColor") or "").replace("_", "-")
    return f"{screen}-{cap}-{color}-unlocked".strip("-")


def _parse_product_selection(html: str) -> Optional[Dict[str, Any]]:
    idx = html.find("productSelectionData:")
    if idx < 0:
        return None
    blob = html[idx + len("productSelectionData:") :].lstrip()
    try:
        data, _ = json.JSONDecoder().raw_decode(blob)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _discover_buy_slugs(client: AppleHKClient) -> List[str]:
    landing = f"https://www.apple.com/{client.locale}/shop/buy-iphone"
    r = client.session.get(landing, timeout=30)
    r.raise_for_status()
    slugs = set(re.findall(r"buy-iphone/([a-z0-9-]+)", r.text, re.I))
    slugs = {s.lower() for s in slugs if s and "/" not in s}
    # Pro 頁包含 Pro Max，優先放前面
    ordered = sorted(slugs, key=lambda s: (0 if "pro" in s else 1, s))
    return ordered


def fetch_latest_models(client: Optional[AppleHKClient] = None) -> Dict[str, Any]:
    """從香港 Apple Store 購買頁抓最新 model_key / variant_key。"""
    client = client or AppleHKClient()
    slugs = _discover_buy_slugs(client)
    if not slugs:
        slugs = ["iphone-17-pro", "iphone-17", "iphone-air"]

    models: Dict[str, Any] = {}
    color_names: Dict[str, str] = {}
    capacity_names: Dict[str, str] = {}

    for slug in slugs:
        url = f"https://www.apple.com/{client.locale}/shop/buy-iphone/{slug}"
        try:
            r = client.session.get(url, timeout=30)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        data = _parse_product_selection(r.text)
        if not data or not data.get("products"):
            continue

        display = data.get("displayValues") or {}
        for key, info in (display.get("dimensionColor") or {}).items():
            if isinstance(info, dict) and info.get("value"):
                color_names[key] = _plain_text(info["value"])
        for key, info in (display.get("dimensionCapacity") or {}).items():
            if isinstance(info, dict) and info.get("value"):
                capacity_names[key] = _plain_text(info["value"]).replace(" ", "")

        buy_slug = f"buy-iphone/{slug}"
        for product in data["products"]:
            family = product.get("familyType") or slug.replace("-", "")
            model_key = _family_to_model_key(family)
            model_name = model_key.replace("_", " ").title().replace("Iphone", "iPhone")
            screensize = product.get("dimensionScreensize") or ""
            screen_info = (display.get("dimensionScreensize") or {}).get(screensize) or {}
            screen_label = _plain_text(screen_info.get("value") or "")
            if screen_label:
                model_name = re.split(r"\s*6\.\d", screen_label)[0].strip() or model_name

            part = product.get("partNumber") or ""
            if not part:
                continue
            cap = product.get("dimensionCapacity") or "unknown"
            color = product.get("dimensionColor") or "unknown"
            variant_key = f"{cap}_{_color_to_variant_suffix(color)}"
            color_label = color_names.get(color, color)
            cap_label = capacity_names.get(cap, cap.upper())
            variant_name = f"{cap_label} {color_label}".strip()

            bucket = models.setdefault(
                model_key,
                {"name": model_name, "buy_slug": buy_slug, "variants": {}},
            )
            bucket["name"] = model_name or bucket["name"]
            bucket["buy_slug"] = buy_slug
            bucket["variants"][variant_key] = {
                "part": part,
                "name": variant_name,
                "product_path": _product_path(product),
            }

    if not models:
        raise RuntimeError("無法從 Apple 購買頁解析型號，請稍後再試")

    cap_rank = {"128gb": 0, "256gb": 1, "512gb": 2, "1tb": 3, "2tb": 4}

    def variant_sort_key(key: str) -> tuple:
        cap, _, rest = key.partition("_")
        return (cap_rank.get(cap, 99), rest)

    def model_sort_key(key: str) -> tuple:
        if key.endswith("pro_max"):
            return (0, key)
        if key.endswith("_pro"):
            return (1, key)
        return (2, key)

    ordered: Dict[str, Any] = {}
    for model_key in sorted(models, key=model_sort_key):
        bucket = models[model_key]
        bucket["variants"] = dict(sorted(bucket["variants"].items(), key=lambda kv: variant_sort_key(kv[0])))
        ordered[model_key] = bucket
    return ordered


def _model_generation(model_key: str) -> int:
    m = re.search(r"iphone_(\d+)", model_key)
    return int(m.group(1)) if m else 0


def _model_kind(model_key: str) -> str:
    if "pro_max" in model_key:
        return "pro_max"
    if re.search(r"iphone_\d+_pro$", model_key):
        return "pro"
    if re.search(r"iphone_\d+e$", model_key):
        return "e"
    if "air" in model_key:
        return "air"
    if "plus" in model_key:
        return "plus"
    return "base"


def pick_successor_model(current_key: str, models: Dict[str, Any]) -> Optional[str]:
    """同一系列（Pro / Pro Max / Air…）裡選世代數字最高的 model_key。"""
    kind = _model_kind(current_key)
    gen = _model_generation(current_key)
    candidates = [
        key for key in models if _model_kind(key) == kind and _model_generation(key) >= gen
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda k: (_model_generation(k), k))


def match_variant_key(model: Dict[str, Any], wanted: str) -> Optional[str]:
    variants = model.get("variants") or {}
    if wanted in variants:
        return wanted
    cap = wanted.split("_", 1)[0] if wanted else ""
    same_cap = [k for k in variants if k.startswith(cap + "_")]
    return same_cap[0] if same_cap else (next(iter(variants), None))


def apply_latest_catalog(
    client: AppleHKClient,
    model_key: str,
    variant_key: str,
) -> Optional[tuple[str, str, Dict[str, str]]]:
    """開賣當下若官網出現新一代，切到對應系列與最接近的 variant。"""
    try:
        latest = fetch_latest_models(client)
    except Exception as e:
        print(f"⚠️  更新型號表失敗: {e}")
        return None

    successor = pick_successor_model(model_key, latest)
    if not successor:
        return None
    new_vk = match_variant_key(latest[successor], variant_key)
    if not new_vk:
        return None
    save_models(latest)
    variant = resolve_variant(latest, successor, new_vk)
    return successor, new_vk, variant


def refresh_models_file(client: Optional[AppleHKClient] = None) -> Dict[str, Any]:
    models = fetch_latest_models(client)
    path = save_models(models)
    print(f"已更新 {path.name}（{len(models)} 個 model_key）\n")
    return models


def update_config_selection(config_path: Path, model_key: str, variant_key: str) -> None:
    models = load_models()
    if model_key not in models:
        raise KeyError(f"未知 model_key: {model_key}（先執行 --refresh-models / --list-models）")
    variant = models[model_key]["variants"].get(variant_key)
    if not variant:
        raise KeyError(f"未知 variant_key: {variant_key}")

    if config_path.exists():
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    else:
        example = ROOT / "config.example.json"
        cfg = json.loads(example.read_text(encoding="utf-8")) if example.exists() else {}

    cfg["model_key"] = model_key
    cfg["variant_key"] = variant_key
    config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"已寫入 {config_path.name}:\n"
        f"  model_key   = {model_key}  ({models[model_key]['name']})\n"
        f"  variant_key = {variant_key}  ({variant['name']}, {variant['part']})"
    )


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


STEALTH_INIT_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
"""


def _click_if_present(page: Any, selector: str, wait_ms: int = 400) -> bool:
    loc = page.locator(selector)
    if not loc.count():
        return False
    loc.first.click(force=True, timeout=5000)
    page.wait_for_timeout(wait_ms)
    return True


def _click_label_if_present(page: Any, text: str, wait_ms: int = 400) -> bool:
    loc = page.locator("label").filter(has_text=text)
    if not loc.count():
        return False
    loc.first.click(force=True, timeout=5000)
    page.wait_for_timeout(wait_ms)
    return True


def _page_looks_blocked(page: Any) -> bool:
    title = (page.title() or "").lower()
    if "page not found" in title or "找不到" in title:
        return True
    try:
        snippet = page.inner_text("body")[:800]
    except Exception:
        return False
    return "The page you’re looking for can’t be found" in snippet or "找不到你所要求的頁面" in snippet


def _bag_has_items(text: str) -> bool:
    empty_markers = ("沒有任何項目", "no items", "your bag is empty", "購物袋沒有")
    return not any(m.lower() in text.lower() for m in empty_markers)


class GrabSession:
    """長駐 Chrome：先預選型號，有貨時立刻按加入購物袋。不要注入過期 cookies.json。"""

    def __init__(self, cfg: Dict[str, Any], variant: Dict[str, str], product_url: str) -> None:
        self.cfg = cfg
        self.variant = variant
        self.product_url = product_url
        self.locale = cfg.get("locale", "hk-zh")
        self.pw: Any = None
        self.context: Any = None
        self.page: Any = None
        self.browser: Any = None
        self.owned = False

    def start(self) -> bool:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("❌ 請先安裝: pip install playwright")
            print("   然後執行: python -m playwright install chrome")
            return False

        self.pw = sync_playwright().start()
        cdp = (self.cfg.get("chrome_cdp") or "").strip()
        headless = bool(self.cfg.get("browser_headless", False))
        profile_dir = ROOT / self.cfg.get("browser_profile_dir", ".chrome-grab")
        profile_dir.mkdir(parents=True, exist_ok=True)

        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
        ]

        try:
            if cdp:
                print(f"🔗 連接已開啟的 Chrome: {cdp}")
                self.browser = self.pw.chromium.connect_over_cdp(cdp)
                self.context = self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
                self.owned = False
            else:
                print(f"🌐 啟動搶購 Chrome（設定檔: {profile_dir.name}）")
                print("   請在此視窗登入 Apple 帳户（只需第一次）。不要關閉視窗。")
                self.context = self.pw.chromium.launch_persistent_context(
                    str(profile_dir),
                    channel="chrome",
                    headless=headless,
                    locale="zh-HK",
                    viewport={"width": 1440, "height": 1200},
                    args=launch_args,
                    ignore_default_args=["--enable-automation"],
                )
                self.owned = True
        except Exception as e:
            print(f"❌ 無法啟動 Chrome: {e}")
            print("   請執行: python -m playwright install chrome")
            self.close()
            return False

        self.context.add_init_script(STEALTH_INIT_JS)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        return True

    def prepare_product_page(self) -> bool:
        page = self.page
        print("🌐 打開產品頁並預選：不換購 + 無 AppleCare+")
        page.goto(self.product_url, wait_until="domcontentloaded", timeout=90000)
        try:
            page.wait_for_load_state("networkidle", timeout=25000)
        except Exception:
            pass
        page.wait_for_timeout(1200)

        if _page_looks_blocked(page):
            print("❌ 產品頁被擋（541）。請在該 Chrome 手動重新整理，或稍後再試。")
            return False

        name = self.variant.get("name", "")
        if "宇宙橙" in name:
            _click_label_if_present(page, "宇宙橙")
        elif "銀色" in name:
            _click_label_if_present(page, "銀色")
        elif "深墨藍" in name or "深藍" in name:
            _click_label_if_present(page, "深墨藍")

        for storage in ("256GB", "512GB", "1TB", "2TB"):
            if storage in name:
                _click_label_if_present(page, storage)
                break

        _click_if_present(page, "[data-autom='choose-noTradeIn']")
        _click_if_present(page, "input#noTradeIn")
        _click_if_present(page, "[data-autom='noapplecare']")
        page.wait_for_timeout(800)

        btn = page.locator("button[name='add-to-cart']")
        try:
            btn.wait_for(state="visible", timeout=20000)
            btn.scroll_into_view_if_needed()
        except Exception:
            print("⚠️  找不到「加入購物袋」按鈕，請確認產品頁已載入")
            return False

        for _ in range(30):
            if btn.get_attribute("aria-disabled") != "true" and not btn.is_disabled():
                print("✅ 選項已選好，「加入購物袋」已可按。等待有貨後會自動按下。")
                return True
            page.wait_for_timeout(400)

        print("⚠️  按鈕仍未啟用。請在視窗內確認已選：不換購、無 AppleCare+")
        return False

    def is_configurator_ready(self) -> bool:
        page = self.page
        if page is None:
            return False
        if _page_looks_blocked(page):
            return False
        url = (page.url or "").lower()
        if any(x in url for x in ("/news/", "/apple-events", "going-nowhere")):
            return False
        btn = page.locator("button[name='add-to-cart']")
        return btn.count() > 0

    def add_to_bag(self) -> bool:
        page = self.page
        btn = page.locator("button[name='add-to-cart']")
        if not btn.count():
            if not self.prepare_product_page():
                return False
            btn = page.locator("button[name='add-to-cart']")

        if btn.get_attribute("aria-disabled") == "true" or btn.is_disabled():
            print("⚠️  按鈕未啟用，重新預選一次...")
            self.prepare_product_page()
            btn = page.locator("button[name='add-to-cart']")

        add_status: Dict[str, Any] = {"code": None, "url": ""}

        def on_response(resp: Any) -> None:
            url = resp.url or ""
            if "add-to-cart" in url or "/shop/bag" in url:
                add_status["code"] = resp.status
                add_status["url"] = url

        page.on("response", on_response)
        print("🛒 按「加入購物袋」...")
        try:
            with page.expect_navigation(timeout=18000, wait_until="domcontentloaded"):
                btn.click(timeout=8000)
        except Exception:
            btn.click(force=True, timeout=8000)
            page.wait_for_timeout(4000)

        page.wait_for_timeout(1500)

        if add_status["code"] == 541 or _page_looks_blocked(page):
            print("❌ Apple 擋下自動加購（HTTP 541）。")
            print("   請直接在這個已打開的視窗手動按「加入購物袋」。")
            print("   不要用你平常的 Chrome 看購物袋——那是另一個登入狀態。")
            try:
                page.goto(self.product_url, wait_until="domcontentloaded", timeout=60000)
                self.prepare_product_page()
            except Exception:
                pass
            return False

        url = page.url or ""
        if any(x in url for x in ("step=attach", "/shop/bag", "checkout")):
            for sel in (
                "[data-autom='proceed']",
                "button:has-text('檢視購物袋')",
                "button:has-text('查看購物袋')",
                "a[href*='/shop/bag']",
            ):
                if _click_if_present(page, sel, wait_ms=1500):
                    break

        bag_url = f"https://www.apple.com/{self.locale}/shop/bag"
        if "/shop/bag" in (page.url or ""):
            body = page.inner_text("body")
            if _bag_has_items(body) and "iPhone" in body:
                print("✅ 已加入購物袋。請在此視窗完成結帳。")
                return True

        bag_page = self.context.new_page()
        try:
            bag_page.goto(bag_url, wait_until="domcontentloaded", timeout=45000)
            bag_page.wait_for_timeout(1500)
            body = bag_page.inner_text("body")
            ok = _bag_has_items(body) and "iPhone" in body
            if ok:
                print("✅ 已加入購物袋。請在彈出的購物袋分頁完成結帳。")
                bag_page.bring_to_front()
                return True
            print("⚠️  購物袋仍為空。請回到產品頁那個分頁，手動按「加入購物袋」。")
            bag_page.close()
            page.bring_to_front()
            return False
        except Exception as e:
            print(f"⚠️  無法確認購物袋: {e}")
            print("   請在已打開的 Chrome 視窗手動檢查。")
            return False

    def keep_open(self) -> None:
        print("瀏覽器保持開啟。完成結帳後按 Ctrl+C 結束腳本。")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n已停止")

    def close(self) -> None:
        try:
            if self.owned and self.context:
                self.context.close()
        except Exception:
            pass
        try:
            if self.pw:
                self.pw.stop()
        except Exception:
            pass
        self.pw = None
        self.context = None
        self.page = None


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

    model_key = cfg["model_key"]
    variant_key = cfg["variant_key"]
    follow_latest = bool(cfg.get("follow_latest", True))
    reload_page_sec = int(cfg.get("reload_page_sec", 45))
    watch_catalog_sec = int(cfg.get("watch_catalog_sec", 30))

    if follow_latest:
        switched = apply_latest_catalog(client, model_key, variant_key)
        if switched:
            new_mk, new_vk, new_variant = switched
            if new_mk != model_key or new_variant.get("part") != part:
                print(
                    f"🔄 官網已是更新一代，改盯: {new_mk} / {new_vk}\n"
                    f"   {new_variant['model_name']} {new_variant['name']} ({new_variant['part']})"
                )
                model_key, variant_key, variant = new_mk, new_vk, new_variant
                part = variant["part"]
                product_url = build_product_url(cfg["locale"], part)

    print("=" * 60)
    print(f"監控: {variant['model_name']} {variant['name']}")
    print(f"型號: {part}")
    print(f"偏好店舖: {', '.join(preferred or ['全部'])}")
    print(f"模式: {mode}" + (" (有貨時自動加購物袋)" if mode == "grab" else " (只通知，不加購物袋)"))
    if mode != "grab":
        print("提示: 要自動加購物袋請執行 python hk_iphone_grab.py --grab")
    if follow_latest:
        print("開賣守門: 定期重載產品頁；若官網出現新一代會自動切換，避免停在舊款／空氣頁")
    print(f"間隔: {interval}s | 按 Ctrl+C 停止")
    print("=" * 60)

    grab: Optional[GrabSession] = None
    if mode == "grab":
        grab = GrabSession(cfg, variant, product_url)
        if not grab.start():
            print("❌ 無法啟動搶購瀏覽器，改為只監控")
            grab = None
        else:
            grab.prepare_product_page()

    known_slugs: set[str] = set()
    try:
        known_slugs = set(_discover_buy_slugs(client))
    except Exception:
        pass
    last_reload = time.time()
    last_watch = time.time()

    try:
        while True:
            try:
                now = time.time()
                if follow_latest and now - last_watch >= watch_catalog_sec:
                    last_watch = now
                    try:
                        slugs = set(_discover_buy_slugs(client))
                    except Exception:
                        slugs = set()
                    new_slugs = slugs - known_slugs
                    if new_slugs:
                        print(f"\n🚨 官網出現新購買頁: {', '.join(sorted(new_slugs))}")
                        switched = apply_latest_catalog(client, model_key, variant_key)
                        if switched:
                            new_mk, new_vk, new_variant = switched
                            if new_mk != model_key or new_vk != variant_key or new_variant["part"] != part:
                                print(
                                    f"🔄 已切換到 {new_mk} / {new_vk}\n"
                                    f"   {new_variant['model_name']} {new_variant['name']} ({new_variant['part']})\n"
                                    f"   （本次行程生效，config.json 未改）"
                                )
                                model_key, variant_key, variant = new_mk, new_vk, new_variant
                                part = variant["part"]
                                product_url = build_product_url(cfg["locale"], part)
                                seen.clear()
                                if grab:
                                    grab.variant = variant
                                    grab.product_url = product_url
                                    grab.prepare_product_page()
                                    last_reload = time.time()
                        known_slugs |= slugs
                    elif slugs:
                        known_slugs |= slugs

                if grab and now - last_reload >= reload_page_sec:
                    if not grab.is_configurator_ready():
                        print("\n⚠️  目前不是可購買頁（空氣頁／活動頁／被擋），重新整理…")
                    grab.prepare_product_page()
                    last_reload = now

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
                        if cfg.get("open_browser_on_hit") and mode != "grab":
                            webbrowser.open(hit.product_url)

                        if mode == "grab" and not grab_attempted:
                            grab_attempted = True
                            ok = False
                            if grab:
                                ok = grab.add_to_bag()
                            else:
                                ok = run_grab(client, cfg, variant, hit)
                            if hit:
                                print(
                                    f"\n📍 結帳時選店舖: {HK_STORES.get(hit.store_id, hit.store_name)} "
                                    f"(storeNumber={hit.store_id})\n"
                                    f"📅 取貨時間參考 API: {hit.pickup_quote} (日期碼 {hit.pickup_date})"
                                )
                            if grab:
                                grab.keep_open()
                                return
                            if not ok:
                                print("❌ 自動加購未完成，請手動完成")
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
    finally:
        if grab:
            grab.close()


def run_grab(
    client: AppleHKClient,
    cfg: Dict[str, Any],
    variant: Dict[str, str],
    hit: Optional[StockHit] = None,
) -> bool:
    """啟動瀏覽器加購物袋。"""
    part = variant["part"]
    product_url = client.resolve_product_url(part)
    grab = GrabSession(cfg, variant, product_url)
    if not grab.start():
        return False
    grab.prepare_product_page()
    print(f"啟動瀏覽器自動加購: {part} ...")
    ok = grab.add_to_bag()
    if ok:
        grab.keep_open()
    else:
        print("❌ 瀏覽器自動加購未完成，請在已打開的視窗手動按「加入購物袋」")
        grab.keep_open()
    grab.close()
    return ok


def try_bag_now(cfg: Dict[str, Any]) -> None:
    """不查庫存，立刻用長駐 Chrome 嘗試加購物袋（用來測試流程）。"""
    models = load_models()
    variant = resolve_variant(models, cfg["model_key"], cfg["variant_key"])
    client = AppleHKClient(cfg["locale"], cfg["location"])
    product_url = client.resolve_product_url(variant["part"])
    print(f"立刻嘗試加購: {variant['model_name']} {variant['name']} ({variant['part']})")
    grab = GrabSession(cfg, variant, product_url)
    try:
        if not grab.start():
            return
        grab.prepare_product_page()
        grab.add_to_bag()
        grab.keep_open()
    finally:
        grab.close()


def list_models(refresh: bool = False) -> None:
    models = refresh_models_file() if refresh else load_models()
    print("可選 model_key / variant_key:\n")
    for model_key, model in models.items():
        print(f"  [{model_key}] {model['name']}")
        for vk, v in model["variants"].items():
            print(f"    {vk:28} {v['part']:12} {v['name']}")
    print("\n寫入 config.json 範例:")
    print("  python hk_iphone_grab.py --set-model iphone_17_pro --set-variant 256gb_cosmic_orange")
    print("從官網更新型號:")
    print("  python hk_iphone_grab.py --refresh-models")


def list_store_ids() -> None:
    print("\n香港 Apple Store ID:\n")
    for sid, name in HK_STORES.items():
        print(f"  {sid}  {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="香港 Apple iPhone 庫存監控 / 搶購")
    parser.add_argument("--config", default=str(ROOT / "config.json"), help="設定檔路徑")
    parser.add_argument("--once", action="store_true", help="只查一次")
    parser.add_argument("--list-stores", action="store_true", help="列出各店庫存")
    parser.add_argument("--list-models", action="store_true", help="列出型號編號（可加 --refresh-models 先更新）")
    parser.add_argument("--list-store-ids", action="store_true", help="列出店舖 ID")
    parser.add_argument("--refresh-models", action="store_true", help="從 Apple 香港官網抓最新 model_key / variant_key 並寫入 models.json")
    parser.add_argument("--set-model", metavar="MODEL_KEY", help="寫入 config.json 的 model_key")
    parser.add_argument("--set-variant", metavar="VARIANT_KEY", help="寫入 config.json 的 variant_key")
    parser.add_argument("--grab", action="store_true", help="有貨時用長駐 Chrome 自動加購物袋")
    parser.add_argument("--try-bag", action="store_true", help="立刻嘗試加購物袋（不查庫存，用來測試）")
    args = parser.parse_args()

    if args.list_store_ids:
        list_store_ids()
        return

    if args.refresh_models and not args.list_models and not args.set_model:
        refresh_models_file()
        list_models(refresh=False)
        return

    if args.list_models:
        list_models(refresh=args.refresh_models)
        return

    cfg_path = Path(args.config)
    if args.set_model or args.set_variant:
        if args.refresh_models:
            refresh_models_file()
        if not args.set_model or not args.set_variant:
            print("❌ 請同時提供 --set-model 與 --set-variant")
            sys.exit(2)
        update_config_selection(cfg_path, args.set_model, args.set_variant)
        return

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.grab:
        cfg["mode"] = "grab"

    if args.try_bag:
        try_bag_now(cfg)
        return

    if args.list_stores:
        models = load_models()
        variant = resolve_variant(models, cfg["model_key"], cfg["variant_key"])
        client = AppleHKClient(cfg["locale"], cfg["location"])
        list_all_stores(client, variant["part"])
        return

    run_monitor(cfg, once=args.once)


if __name__ == "__main__":
    main()
