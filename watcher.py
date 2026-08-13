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
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

import ai_analyzer

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
        "ai_enabled": False,
        "ai_model": "gpt-5-mini",
        "ai_min_score": 75,
        "ai_max_listings_per_scan": 5,
        "ai_candidate_min_rule_score": 25,
        "ai_use_images": True,
        "searches": [],
    }


def load_config():
    cfg = default_config()
    if CONFIG_PATH.exists():
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg.update(loaded)
        except Exception:
            pass
    cfg["check_interval_minutes"] = max(15, int(cfg.get("check_interval_minutes", 30)))
    cfg["max_alerts_per_scan"] = max(1, min(20, int(cfg.get("max_alerts_per_scan", 5))))
    cfg["ai_max_listings_per_scan"] = max(1, min(20, int(cfg.get("ai_max_listings_per_scan", 5))))
    cfg["ai_min_score"] = max(0, min(100, int(cfg.get("ai_min_score", 75))))
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def test_discord(webhook_url):
    if not webhook_url.strip():
        raise ValueError("Discord webhook is empty.")
    r = requests.post(
        webhook_url.strip(),
        json={
            "content": "✅ Marketplace Deal Watcher v3 is connected.",
            "allowed_mentions": {"parse": []},
        },
        timeout=15,
    )
    r.raise_for_status()


def _db():
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """CREATE TABLE IF NOT EXISTS listings(
        search_name TEXT NOT NULL,
        item_id TEXT NOT NULL,
        title TEXT,
        url TEXT NOT NULL,
        image_url TEXT,
        first_price REAL,
        last_price REAL,
        first_seen INTEGER,
        last_seen INTEGER,
        last_alert_price REAL,
        last_ai_price REAL,
        ai_score INTEGER,
        ai_json TEXT,
        ai_analyzed_at INTEGER,
        PRIMARY KEY(search_name,item_id))"""
    )

    existing = {row[1] for row in con.execute("PRAGMA table_info(listings)")}
    migrations = {
        "image_url": "TEXT",
        "last_ai_price": "REAL",
        "ai_score": "INTEGER",
        "ai_json": "TEXT",
        "ai_analyzed_at": "INTEGER",
    }
    for column, column_type in migrations.items():
        if column not in existing:
            con.execute(f"ALTER TABLE listings ADD COLUMN {column} {column_type}")
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
                value = float(raw)
                if 0 < value < 10_000_000:
                    return value
            except ValueError:
                pass
    return None


def _title(text):
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    for line in lines:
        if "$" not in line and len(line) > 2:
            return line[:180]
    return lines[0][:180] if lines else "Marketplace listing"


def _extract_image(anchor):
    for target in (anchor, anchor.locator("xpath=.."), anchor.locator("xpath=../..")):
        try:
            imgs = target.locator("img")
            for i in range(min(imgs.count(), 4)):
                src = (imgs.nth(i).get_attribute("src") or "").strip()
                if src.startswith(("http://", "https://")):
                    return src
        except Exception:
            continue
    return None


def _extract(page, scroll_count):
    page.wait_for_timeout(2200)
    for _ in range(max(0, min(10, int(scroll_count)))):
        try:
            page.mouse.wheel(0, 1700)
            page.wait_for_timeout(700)
        except Exception:
            break

    anchors = page.locator('a[href*="/marketplace/item/"]')
    found = {}
    for i in range(min(anchors.count(), 350)):
        a = anchors.nth(i)
        try:
            href = a.get_attribute("href") or ""
            m = ITEM_RE.search(href)
            if not m:
                continue

            text = ""
            for target in (a, a.locator("xpath=.."), a.locator("xpath=../..")):
                try:
                    candidate = target.inner_text(timeout=1200).strip()
                    if len(candidate) > len(text):
                        text = candidate
                except Exception:
                    continue
            if not text:
                continue

            item_id = m.group(1)
            item = {
                "id": item_id,
                "title": _title(text),
                "price": _price(text),
                "text": text[:2500],
                "url": _clean_url(href),
                "image_url": _extract_image(a),
            }
            old = found.get(item_id)
            if old is None or len(item["text"]) > len(old["text"]):
                found[item_id] = item
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
        n = max(1, int(len(prices) * 0.1))
        prices = prices[n:-n] or prices
    return float(statistics.median(prices)), "visible-listing median"


def _score(item, search, baseline, source):
    text = (item["title"] + "\n" + item["text"]).casefold()
    req = [str(x).casefold() for x in search.get("required_keywords", []) if str(x).strip()]
    exc = [str(x).casefold() for x in search.get("excluded_keywords", []) if str(x).strip()]
    pref = [str(x) for x in search.get("preferred_keywords", []) if str(x).strip()]

    if req and not all(x in text for x in req):
        return 0, []
    if exc and any(x in text for x in exc):
        return 0, []
    if item["price"] is None:
        return 0, []

    score, reasons = 0, []
    max_price = _num(search.get("max_price"))
    if max_price is not None:
        if item["price"] > max_price:
            return 0, []
        score += 20
        reasons.append(f"Under max price ${max_price:,.0f}")

    matches = [x for x in pref if x.casefold() in text]
    if matches:
        score += min(20, len(matches) * 5)
        reasons.append("Matched: " + ", ".join(matches[:5]))

    if baseline:
        discount = (baseline - item["price"]) / baseline * 100
        if discount < float(search.get("min_discount_percent", 0)):
            return 0, []
        score += max(0, min(60, int(discount * 1.5)))
        reasons.append(f"{discount:.0f}% under {source} (${baseline:,.0f})")
    elif max_price is not None:
        score += 30
    elif req or pref:
        score += 25

    return min(score, 100), reasons


def _get_row(con, name, item_id):
    return con.execute(
        """SELECT last_price,last_alert_price,last_ai_price,ai_score,ai_json,ai_analyzed_at
           FROM listings WHERE search_name=? AND item_id=?""",
        (name, item_id),
    ).fetchone()


def _upsert(con, name, item, now):
    row = _get_row(con, name, item["id"])
    if row is None:
        con.execute(
            """INSERT INTO listings(
               search_name,item_id,title,url,image_url,first_price,last_price,first_seen,last_seen,last_alert_price,
               last_ai_price,ai_score,ai_json,ai_analyzed_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                name,
                item["id"],
                item["title"],
                item["url"],
                item.get("image_url"),
                item["price"],
                item["price"],
                now,
                now,
                None,
                None,
                None,
                None,
                None,
            ),
        )
    else:
        con.execute(
            """UPDATE listings SET title=?,url=?,image_url=?,last_price=?,last_seen=?
               WHERE search_name=? AND item_id=?""",
            (
                item["title"],
                item["url"],
                item.get("image_url"),
                item["price"],
                now,
                name,
                item["id"],
            ),
        )


def _record_ai(con, name, item, analysis):
    con.execute(
        """UPDATE listings SET last_ai_price=?,ai_score=?,ai_json=?,ai_analyzed_at=?
           WHERE search_name=? AND item_id=?""",
        (
            item["price"],
            int(analysis.get("deal_score", 0)),
            json.dumps(analysis, ensure_ascii=False),
            int(time.time()),
            name,
            item["id"],
        ),
    )
    con.commit()


def _discord_embed(search_name, item, rule_score, rule_reasons, ai=None, drop=None):
    price_text = "Unknown" if item["price"] is None else f"${item['price']:,.0f}"

    if ai:
        score = int(ai.get("deal_score", 0))
        confidence = int(ai.get("confidence", 0))
        verdict = str(ai.get("verdict", "fair")).replace("_", " ").title()
        title = f"🤖 AI Deal {score}/100 — {search_name}"
        if drop is not None:
            title = f"📉🤖 AI Price Drop {score}/100 — {search_name}"

        low = float(ai.get("estimated_value_low", 0) or 0)
        high = float(ai.get("estimated_value_high", 0) or 0)
        value_text = "Not enough evidence"
        if low > 0 and high >= low:
            value_text = f"${low:,.0f}–${high:,.0f}"

        description_lines = [f"**{item['title']}**", "", str(ai.get("summary", "")).strip()]
        positives = ai.get("positives", []) or []
        flags = ai.get("red_flags", []) or []
        if positives:
            description_lines += ["", "**Why AI likes it**"] + [f"• {x}" for x in positives[:5]]
        if flags:
            description_lines += ["", "**Things to verify**"] + [f"• {x}" for x in flags[:5]]

        fields = [
            {"name": "Price", "value": price_text, "inline": True},
            {"name": "AI score", "value": f"{score}/100", "inline": True},
            {"name": "Confidence", "value": f"{confidence}%", "inline": True},
            {"name": "Verdict", "value": verdict, "inline": True},
            {"name": "AI value range", "value": value_text, "inline": True},
            {"name": "Rule score", "value": f"{rule_score}/100", "inline": True},
        ]
    else:
        title = f"🔥 Deal found — {search_name}"
        if drop is not None:
            title = f"📉 Price drop — {search_name}"
        description_lines = [f"**{item['title']}**", ""] + [f"• {r}" for r in rule_reasons]
        fields = [
            {"name": "Price", "value": price_text, "inline": True},
            {"name": "Deal score", "value": f"{rule_score}/100", "inline": True},
        ]

    if drop is not None:
        fields.append({"name": "Price drop", "value": f"{drop:.0f}%", "inline": True})

    embed = {
        "title": title,
        "description": "\n".join(x for x in description_lines if x is not None)[:3900],
        "url": item["url"],
        "fields": fields,
        "footer": {"text": "Marketplace Deal Watcher v3 • AI estimates are not guarantees"},
    }
    if item.get("image_url"):
        embed["thumbnail"] = {"url": item["image_url"]}
    return embed


def _send(webhook, search_name, item, rule_score, rule_reasons, ai=None, drop=None):
    if not webhook.strip():
        return
    payload = {
        "embeds": [_discord_embed(search_name, item, rule_score, rule_reasons, ai=ai, drop=drop)],
        "allowed_mentions": {"parse": []},
    }
    r = requests.post(webhook.strip(), json=payload, timeout=15)
    r.raise_for_status()


def _scan(page, con, cfg, search, log):
    name = str(search.get("name") or "Marketplace search")
    url = str(search.get("url") or "").strip()
    if not url:
        return

    log("")
    log(f"Checking: {name}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except PlaywrightTimeoutError:
        log("Page load timed out; checking what loaded anyway.")
    page.wait_for_timeout(2500)

    if "login" in page.url.casefold() or "checkpoint" in page.url.casefold():
        log("Facebook needs login/verification. Click Facebook Login in the app.")
        return

    items = _extract(page, cfg.get("scroll_count", 4))
    log(f"Found {len(items)} unique visible listing(s).")
    if not items:
        return

    baseline, source = _baseline(items, search)
    if baseline:
        log(f"Baseline: ${baseline:,.0f} ({source}).")
    elif search.get("auto_value", True):
        log("Not enough comparable prices for an automatic baseline yet.")

    first_scan = (
        con.execute("SELECT 1 FROM listings WHERE search_name=? LIMIT 1", (name,)).fetchone()
        is None
    )
    now = int(time.time())
    candidates = []

    for item in items:
        old_row = _get_row(con, name, item["id"])
        old_last_price = old_row[0] if old_row else None
        old_alert_price = old_row[1] if old_row else None
        old_ai_price = old_row[2] if old_row else None
        old_ai_analyzed_at = old_row[5] if old_row else None

        rule_score, reasons = _score(item, search, baseline, source)
        min_rule_score = int(search.get("min_score", 55))

        drop = None
        if (
            old_row
            and item["price"] is not None
            and old_last_price
            and item["price"] < old_last_price
            and cfg.get("alert_on_price_drop", True)
        ):
            compared = old_alert_price or old_last_price
            calculated = (compared - item["price"]) / compared * 100 if compared else 0
            if calculated >= float(cfg.get("price_drop_percent", 10)):
                drop = calculated

        _upsert(con, name, item, now)

        is_new = old_row is None
        needs_ai = old_ai_analyzed_at is None
        if item["price"] is not None and old_ai_price and item["price"] < old_ai_price:
            needs_ai = True

        if cfg.get("ai_enabled", False):
            candidate_floor = int(cfg.get("ai_candidate_min_rule_score", 25))
            if rule_score >= candidate_floor and (is_new or drop is not None or needs_ai):
                candidates.append((rule_score, item, reasons, drop, min_rule_score))
        elif rule_score >= min_rule_score and (is_new or drop is not None):
            candidates.append((rule_score, item, reasons, drop, min_rule_score))

    con.commit()

    if first_scan and not cfg.get("alert_on_first_scan", True):
        log("First scan learned current listings without alerts or AI calls.")
        log(f"Scan complete: {len(items)} listings | 0 alert(s) sent.")
        return

    candidates.sort(key=lambda x: x[0], reverse=True)
    alerts = []

    ai_enabled = bool(cfg.get("ai_enabled", False))
    if ai_enabled and not ai_analyzer.has_api_key():
        log("AI is enabled but no OpenAI API key is saved. Falling back to normal scoring.")
        ai_enabled = False

    if ai_enabled:
        ai_limit = int(cfg.get("ai_max_listings_per_scan", 5))
        selected = candidates[:ai_limit]
        log(f"AI shortlist: analyzing {len(selected)} candidate(s).")

        for rule_score, item, reasons, drop, min_rule_score in selected:
            try:
                log(f"AI analyzing {item['title'][:80]}...")
                analysis = ai_analyzer.analyze_listing(
                    item=item,
                    search_name=name,
                    baseline=baseline,
                    baseline_source=source,
                    rule_score=rule_score,
                    rule_reasons=reasons,
                    model=str(cfg.get("ai_model", "gpt-5-mini")),
                    use_image=bool(cfg.get("ai_use_images", True)),
                )
                _record_ai(con, name, item, analysis)
                ai_score = int(analysis.get("deal_score", 0))
                eligible = bool(analysis.get("eligible", False))
                log(
                    f"AI result {ai_score}/100 ({analysis.get('confidence', 0)}% confidence) | "
                    f"{analysis.get('verdict', 'unknown')}"
                )
                if eligible and ai_score >= int(cfg.get("ai_min_score", 75)):
                    alerts.append((ai_score, rule_score, item, reasons, drop, analysis))
            except Exception as exc:
                log(f"AI analysis failed: {type(exc).__name__}: {exc}")
    else:
        for rule_score, item, reasons, drop, min_rule_score in candidates:
            if rule_score >= min_rule_score:
                alerts.append((rule_score, rule_score, item, reasons, drop, None))

    alerts.sort(key=lambda x: x[0], reverse=True)
    alerts = alerts[: int(cfg.get("max_alerts_per_scan", 5))]

    for _, rule_score, item, reasons, drop, analysis in alerts:
        try:
            _send(
                cfg.get("discord_webhook_url", ""),
                name,
                item,
                rule_score,
                reasons,
                ai=analysis,
                drop=drop,
            )
            con.execute(
                "UPDATE listings SET last_alert_price=? WHERE search_name=? AND item_id=?",
                (item["price"], name, item["id"]),
            )
            con.commit()
            if analysis:
                log(
                    f"AI ALERT {analysis.get('deal_score', 0)}/100 | "
                    f"${item['price']:,.0f} | {item['title']}"
                )
            else:
                log(f"ALERT {rule_score}/100 | ${item['price']:,.0f} | {item['title']}")
        except requests.RequestException as exc:
            log(f"Discord notification failed: {exc}")

    log(f"Scan complete: {len(items)} listings | {len(alerts)} alert(s) sent.")


def facebook_login(log=print):
    PROFILE_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1400, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.facebook.com/marketplace/", wait_until="domcontentloaded")
        log("Log into Facebook. When Marketplace is visible, close the browser window.")
        try:
            while ctx.pages:
                time.sleep(1)
        except Exception:
            pass
        try:
            ctx.close()
        except Exception:
            pass
    log("Facebook browser session saved locally.")


def run_watcher(cfg, log=print, stop_event=None, once=False):
    stop_event = stop_event or threading.Event()
    searches = cfg.get("searches", [])
    if not searches:
        log("No searches configured.")
        return

    PROFILE_DIR.mkdir(exist_ok=True)
    con = _db()
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                str(PROFILE_DIR),
                headless=bool(cfg.get("headless", False)),
                viewport={"width": 1400, "height": 900},
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                while not stop_event.is_set():
                    for search in searches:
                        if stop_event.is_set():
                            break
                        try:
                            _scan(page, con, cfg, search, log)
                        except Exception as exc:
                            log(f"Search error: {type(exc).__name__}: {exc}")
                    if once or stop_event.is_set():
                        break
                    minutes = max(15, int(cfg.get("check_interval_minutes", 30)))
                    log(f"Next scan in {minutes} minutes.")
                    stop_event.wait(minutes * 60)
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
    finally:
        con.close()


if __name__ == "__main__":
    run_watcher(load_config())
