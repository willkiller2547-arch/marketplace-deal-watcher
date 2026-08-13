from __future__ import annotations

import json
import re
import sqlite3
import statistics
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
DB_PATH = BASE_DIR / "marketplace_v2.db"
PROFILE_DIR = BASE_DIR / "browser_profile"
ITEM_RE = re.compile(r"/marketplace/item/(\d+)")
PRICE_RE = re.compile(r"(?:CA|C)?\$\s*([0-9][0-9 ,.]*)", re.I)


def default_config():
    return {
        "discord_webhook_url": "",
        "check_interval_minutes": 30,
        "headless": False,
        "max_alerts_per_scan": 5,
        "scroll_count": 4,
        "alert_on_first_scan": True,
        "alert_on_price_drop": True,
        "price_drop_percent": 10,
        "searches": [],
    }


def load_config():
    cfg = default_config()
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    cfg["check_interval_minutes"] = max(15, int(cfg.get("check_interval_minutes", 30)))
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def test_discord(webhook_url):
    if not webhook_url.strip():
        raise ValueError("Discord webhook is empty.")
    r = requests.post(webhook_url.strip(), json={"content": "✅ Marketplace Deal Watcher v2 is connected.", "allowed_mentions": {"parse": []}}, timeout=15)
    r.raise_for_status()


def _db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS listings(
        search_name TEXT NOT NULL,item_id TEXT NOT NULL,title TEXT,url TEXT NOT NULL,
        first_price REAL,last_price REAL,first_seen INTEGER,last_seen INTEGER,last_alert_price REAL,
        PRIMARY KEY(search_name,item_id))""")
    con.commit()
    return con


def _clean_url(href):
    p = urlsplit(urljoin("https://www.facebook.com", href))
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def _price(text):
    for line in text.splitlines():
        if "$" not in line:
            continue
        m = PRICE_RE.search(line)
        if m:
            raw = m.group(1).replace(" ", "").replace(",", "")
            try:
                return float(raw)
            except ValueError:
                pass
    return None


def _title(text):
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    for line in lines:
        if "$" not in line and len(line) > 2:
            return line[:180]
    return lines[0][:180] if lines else "Marketplace listing"


def _extract(page, scroll_count):
    page.wait_for_timeout(2200)
    for _ in range(max(0, min(10, int(scroll_count)))):
        page.mouse.wheel(0, 1700)
        page.wait_for_timeout(700)
    anchors = page.locator('a[href*="/marketplace/item/"]')
    found = {}
    for i in range(min(anchors.count(), 350)):
        a = anchors.nth(i)
        try:
            href = a.get_attribute("href") or ""
            m = ITEM_RE.search(href)
            if not m:
                continue
            text = a.inner_text(timeout=1200).strip()
            if not text:
                text = a.locator("xpath=..").inner_text(timeout=1200).strip()
            if not text:
                continue
            item_id = m.group(1)
            found[item_id] = {"id": item_id, "title": _title(text), "price": _price(text), "text": text[:2500], "url": _clean_url(href)}
        except Exception:
            continue
    return list(found.values())


def _num(v):
    try:
        return None if v in (None, "") else float(v)
    except Exception:
        return None


def _baseline(items, search):
    manual = _num(search.get("estimated_value"))
    if manual and manual > 0:
        return manual, "configured value"
    if not search.get("auto_value", True):
        return None, ""
    prices = sorted(x["price"] for x in items if x["price"] and x["price"] > 0)
    if len(prices) < max(5, int(search.get("min_baseline_samples", 7))):
        return None, ""
    if len(prices) >= 10:
        n = max(1, int(len(prices) * .1)); prices = prices[n:-n] or prices
    return float(statistics.median(prices)), "visible-listing median"


def _score(item, search, baseline, source):
    text = (item["title"] + "\n" + item["text"]).casefold()
    req = [str(x).casefold() for x in search.get("required_keywords", []) if str(x).strip()]
    exc = [str(x).casefold() for x in search.get("excluded_keywords", []) if str(x).strip()]
    pref = [str(x) for x in search.get("preferred_keywords", []) if str(x).strip()]
    if req and not all(x in text for x in req): return 0, []
    if exc and any(x in text for x in exc): return 0, []
    if item["price"] is None: return 0, []
    score, reasons = 0, []
    max_price = _num(search.get("max_price"))
    if max_price is not None:
        if item["price"] > max_price: return 0, []
        score += 20; reasons.append(f"Under max price ${max_price:,.0f}")
    matches = [x for x in pref if x.casefold() in text]
    if matches:
        score += min(20, len(matches) * 5); reasons.append("Matched: " + ", ".join(matches[:5]))
    if baseline:
        discount = (baseline - item["price"]) / baseline * 100
        if discount < float(search.get("min_discount_percent", 0)): return 0, []
        score += max(0, min(60, int(discount * 1.5)))
        reasons.append(f"{discount:.0f}% under {source} (${baseline:,.0f})")
    elif max_price is not None:
        score += 30
    elif req or pref:
        score += 25
    return min(score, 100), reasons


def _send(webhook, search_name, item, score, reasons, drop=None):
    if not webhook.strip(): return
    title = f"📉 Price drop — {search_name}" if drop is not None else f"🔥 Deal found — {search_name}"
    fields = [{"name": "Price", "value": f"${item['price']:,.0f}", "inline": True}, {"name": "Deal score", "value": f"{score}/100", "inline": True}]
    if drop is not None: fields.append({"name": "Price drop", "value": f"{drop:.0f}%", "inline": True})
    payload = {"embeds": [{"title": title, "description": f"**{item['title']}**\n\n" + "\n".join("• " + r for r in reasons), "url": item["url"], "fields": fields, "footer": {"text": "Marketplace Deal Watcher v2"}}], "allowed_mentions": {"parse": []}}
    r = requests.post(webhook.strip(), json=payload, timeout=15); r.raise_for_status()


def _scan(page, con, cfg, search, log):
    name = str(search.get("name") or "Marketplace search")
    url = str(search.get("url") or "").strip()
    if not url: return
    log(""); log(f"Checking: {name}")
    try: page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except PlaywrightTimeoutError: log("Page load timed out; checking what loaded anyway.")
    page.wait_for_timeout(2500)
    if "login" in page.url.casefold() or "checkpoint" in page.url.casefold():
        log("Facebook needs login/verification. Click Facebook Login in the app."); return
    items = _extract(page, cfg.get("scroll_count", 4)); log(f"Found {len(items)} unique visible listing(s).")
    if not items: return
    baseline, source = _baseline(items, search)
    if baseline: log(f"Baseline: ${baseline:,.0f} ({source}).")
    first_scan = con.execute("SELECT 1 FROM listings WHERE search_name=? LIMIT 1", (name,)).fetchone() is None
    alerts = []
    now = int(time.time())
    for item in items:
        row = con.execute("SELECT last_price,last_alert_price FROM listings WHERE search_name=? AND item_id=?", (name, item["id"])).fetchone()
        score, reasons = _score(item, search, baseline, source)
        min_score = int(search.get("min_score", 55))
        drop = None
        if row and item["price"] is not None and row[0] and item["price"] < row[0] and cfg.get("alert_on_price_drop", True):
            compared = row[1] or row[0]; drop = (compared - item["price"]) / compared * 100
            if drop < float(cfg.get("price_drop_percent", 10)): drop = None
        if score >= min_score and (row is None or drop is not None): alerts.append((score, item, reasons, drop))
        if row is None:
            con.execute("INSERT INTO listings VALUES(?,?,?,?,?,?,?,?,?)", (name,item["id"],item["title"],item["url"],item["price"],item["price"],now,now,None))
        else:
            con.execute("UPDATE listings SET title=?,url=?,last_price=?,last_seen=? WHERE search_name=? AND item_id=?", (item["title"],item["url"],item["price"],now,name,item["id"]))
    con.commit()
    alerts.sort(key=lambda x: x[0], reverse=True)
    if first_scan and not cfg.get("alert_on_first_scan", True): alerts = []
    alerts = alerts[:int(cfg.get("max_alerts_per_scan", 5))]
    for score, item, reasons, drop in alerts:
        try:
            _send(cfg.get("discord_webhook_url", ""), name, item, score, reasons, drop)
            con.execute("UPDATE listings SET last_alert_price=? WHERE search_name=? AND item_id=?", (item["price"],name,item["id"])); con.commit()
            log(f"ALERT {score}/100 | ${item['price']:,.0f} | {item['title']}")
        except requests.RequestException as exc:
            log(f"Discord notification failed: {exc}")
    log(f"Scan complete: {len(items)} listings | {len(alerts)} alert(s) sent.")


def facebook_login(log=print):
    PROFILE_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(PROFILE_DIR), headless=False, viewport={"width": 1400, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.facebook.com/marketplace/", wait_until="domcontentloaded")
        log("Log into Facebook. When Marketplace is visible, close the browser window.")
        try:
            while ctx.pages: time.sleep(1)
        except Exception: pass
        try: ctx.close()
        except Exception: pass
    log("Facebook browser session saved locally.")


def run_watcher(cfg, log=print, stop_event=None, once=False):
    stop_event = stop_event or threading.Event()
    searches = cfg.get("searches", [])
    if not searches: log("No searches configured."); return
    PROFILE_DIR.mkdir(exist_ok=True); con = _db()
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(str(PROFILE_DIR), headless=bool(cfg.get("headless", False)), viewport={"width": 1400, "height": 900})
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                while not stop_event.is_set():
                    for search in searches:
                        if stop_event.is_set(): break
                        try: _scan(page, con, cfg, search, log)
                        except Exception as exc: log(f"Search error: {type(exc).__name__}: {exc}")
                    if once or stop_event.is_set(): break
                    minutes = max(15, int(cfg.get("check_interval_minutes", 30))); log(f"Next scan in {minutes} minutes."); stop_event.wait(minutes * 60)
            finally:
                try: ctx.close()
                except Exception: pass
    finally:
        con.close()


if __name__ == "__main__":
    run_watcher(load_config())
