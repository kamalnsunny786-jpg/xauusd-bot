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

# ---------------- gold headlines (with short summaries) ----------------
# Feeds that include a short summary. Dead or blocked feeds are skipped automatically.
# (name, url, must_mention_gold)
FEEDS = [
    ("FXStreet", "https://www.fxstreet.com/rss/news", True),
    ("FXStreet Analysis", "https://www.fxstreet.com/rss/analysis", True),
    ("Investing.com", "https://www.investing.com/rss/news_11.rss", True),
]
GOLD_WORDS = re.compile(r"\b(gold|xau|xauusd|bullion|precious metals?)\b", re.I)

def strip_html(x):
    x = html.unescape(x or "")
    x = re.sub(r"<(script|style).*?</\1>", " ", x, flags=re.S | re.I)
    x = re.sub(r"<[^>]+>", " ", x)
    x = html.unescape(re.sub(r"\s+", " ", x)).strip()
    return re.sub(r"(The post .*? appeared first on .*|Read more.*|Continue reading.*)$", "", x, flags=re.I).strip()

def short(x, n=450):
    if len(x) <= n: return x
    cut = x[:n]; i = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[:i + 1] if i > n * 0.5 else cut.rsplit(" ", 1)[0] + "..."

def key_of(title):
    return re.sub(r"\W+", " ", re.sub(r"\s-\s[^-]*$", "", title).lower()).strip()[:80]

def pub_of(it):
    try:
        p = parsedate_to_datetime(it.findtext("pubDate"))
        return p if p.tzinfo else p.replace(tzinfo=timezone.utc)
    except Exception:
        return None

def collect(seen):
    now = datetime.now(timezone.utc); fresh = {}
    # 1) feeds with summaries (preferred, listed first)
    for name, url, need_gold in FEEDS:
        try: root = ET.fromstring(fetch(url))
        except Exception as e:
            print("feed skipped:", name, e); continue
        for it in root.iter("item"):
            title = strip_html(it.findtext("title")); link = (it.findtext("link") or "").strip()
            pub = pub_of(it); summ = strip_html(it.findtext("description"))
            if not title or pub is None or now - pub > timedelta(hours=6): continue
            if need_gold and not GOLD_WORDS.search(title + " " + summ): continue
            k = key_of(title)
            if k and k not in seen and k not in fresh:
                if len(summ) < 40 or summ.lower().startswith(title.lower()[:40]): summ = ""
                fresh[k] = dict(pub=pub, title=title, link=link, src=name, summ=short(summ))
    # 2) Google News headlines (fallback, headline only)
    for q in QUERIES:
        url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(q + " when:1d") + "&hl=en-US&gl=US&ceid=US:en"
        try: root = ET.fromstring(fetch(url))
        except Exception as e:
            print("news fetch failed:", e); continue
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip(); link = (it.findtext("link") or "").strip()
            pub = pub_of(it)
            if not title or pub is None or now - pub > timedelta(hours=4): continue
            k = key_of(title)
            if k and k not in seen and k not in fresh:
                head, _, src = title.rpartition(" - ")
                if not head: head, src = title, "News"
                fresh[k] = dict(pub=pub, title=head.strip(), link=link, src=src.strip(), summ="")
    return fresh

def news(state):
    first = "seen" not in state
    seen = state.setdefault("seen", [])
    items = sorted(collect(seen).items(), key=lambda kv: kv[1]["pub"], reverse=True)
    if first:
        seen.extend(k for k, _ in items)
        print("first run: marked", len(items), "headlines as seen (no spam)")
        send("✅ Gold news alerts are ON. You will get high-impact USD news reminders and fresh gold headlines here.")
    else:
        for k, n in items[:MAX_NEWS]:
            ist = n["pub"].astimezone(timezone.utc) + timedelta(hours=5, minutes=30)
            msg = f"📰 <b>GOLD NEWS</b>\n\n<b>{html.escape(n['title'])}</b>\n"
            if n["summ"]: msg += f"\n{html.escape(n['summ'])}\n"
            msg += f"\n🏷 {html.escape(n['src'])}   🕒 {ist:%d %b, %I:%M %p} IST"
            if n["link"]: msg += f"\n👉 <a href=\"{html.escape(n['link'], quote=True)}\">Read full story</a>"
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
