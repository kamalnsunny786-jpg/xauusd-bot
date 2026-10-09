#!/usr/bin/env python3
"""
Gold (XAUUSD) news alerts for Telegram. Free, no pip installs.
 1) Economic calendar (Forex Factory's free weekly JSON): reminder ~35 min before
    high-impact USD events (CPI, NFP, FOMC, etc.).
 2) Gold headlines (Google News RSS): new gold / Fed / dollar headlines, max 3 per run.
Run:  python3 news_bot.py --test   |   python3 news_bot.py --once
Env:  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID  (optional: CAL_IMPACT="High,Medium", MAX_NEWS=3)
Educational info only, not financial advice.
"""
import os, sys, json, time, html, re, urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
IMPACTS = [x.strip() for x in os.environ.get("CAL_IMPACT", "High").split(",")]
COUNTRIES = [x.strip() for x in os.environ.get("CAL_COUNTRIES", "USD").split(",")]
MAX_NEWS = int(os.environ.get("MAX_NEWS", "3"))
QUERIES = ["gold price XAUUSD", "gold Fed dollar inflation"]
CAL_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "news_state.json")

def fetch(url, data=None, timeout=15):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "Mozilla/5.0 (xauusd-news-bot)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

def send(text):
    if not TOKEN or not CHAT:
        print("Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID first."); print(text); return False
    data = urllib.parse.urlencode({"chat_id": CHAT, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    try:
        fetch(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=data); return True
    except Exception as e:
        print("telegram error:", e); return False

def load_state():
    try: return json.load(open(STATE))
    except Exception: return {}

# ---------------- economic calendar ----------------
def calendar(state):
    c = state.get("cal", {})
    if time.time() - c.get("t", 0) > 3600 or not c.get("ev"):
        try:
            ev = json.loads(fetch(CAL_URL))
            keep = [e for e in ev if e.get("country") in COUNTRIES and e.get("impact") in IMPACTS]
            state["cal"] = {"t": time.time(), "ev": keep}
            print("calendar refreshed:", len(keep), "events this week")
        except Exception as e:
            print("calendar fetch failed (using cache):", e)
    sent = state.setdefault("cal_sent", [])
    now = datetime.now(timezone.utc)
    for e in state.get("cal", {}).get("ev", []):
        try:
            t = datetime.fromisoformat(e["date"])
            if t.tzinfo is None: t = t.replace(tzinfo=timezone.utc)
            t = t.astimezone(timezone.utc)
        except Exception:
            continue
        mins = (t - now).total_seconds() / 60
        key = e.get("title", "") + e.get("date", "")
        if 0 < mins <= 35 and key not in sent:
            ist = t + timedelta(hours=5, minutes=30)
            msg = (f"⚠️ <b>{e.get('impact', '').upper()} IMPACT NEWS in {int(mins)} min</b>\n"
                   f"{html.escape(e.get('country', ''))}: <b>{html.escape(e.get('title', ''))}</b>\n"
                   f"Time: {t:%H:%M} UTC / {ist:%H:%M} IST\n"
                   f"Forecast: {html.escape(str(e.get('forecast') or '-'))}   Previous: {html.escape(str(e.get('previous') or '-'))}\n\n"
                   "Gold can move fast on this. Avoid fresh trades right before the release, or keep a tight stop.")
            if send(msg): sent.append(key)
    state["cal_sent"] = sent[-200:]

# ---------------- gold headlines ----------------
def news(state):
    first = "seen" not in state
    seen = state.setdefault("seen", [])
    now = datetime.now(timezone.utc)
    fresh = {}
    for q in QUERIES:
        url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(q + " when:1d") + "&hl=en-US&gl=US&ceid=US:en"
        try:
            root = ET.fromstring(fetch(url))
        except Exception as e:
            print("news fetch failed:", e); continue
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            try: pub = parsedate_to_datetime(it.findtext("pubDate"))
            except Exception: continue
            if now - pub > timedelta(hours=4): continue
            key = re.sub(r"\W+", " ", re.sub(r"\s-\s[^-]*$", "", title).lower()).strip()[:80]
            if key and key not in seen and key not in fresh:
                fresh[key] = (pub, title, link)
    items = sorted(fresh.items(), key=lambda kv: kv[1][0], reverse=True)
    if first:
        seen.extend(k for k, _ in items)
        print("first run: marked", len(items), "headlines as seen (no spam)")
        send("✅ Gold news alerts are ON. You will get high-impact USD news reminders and fresh gold headlines here.")
    else:
        for k, (pub, title, link) in items[:MAX_NEWS]:
            ist = pub.astimezone(timezone.utc) + timedelta(hours=5, minutes=30)
            msg = f"📰 <b>Gold news</b>\n{html.escape(title)}\n{ist:%d %b %H:%M} IST\n{link}"
            send(msg)
        seen.extend(k for k, _ in items)   # mark all as seen, so no backlog flood
    state["seen"] = seen[-300:]

if __name__ == "__main__":
    if "--test" in sys.argv:
        print("sent" if send("✅ News bot connected.") else "failed")
    else:
        st = load_state()
        news(st); calendar(st)
        json.dump(st, open(STATE, "w"))
