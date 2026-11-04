#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XAUUSD PRO BOT  (single file)
  python gold_bot.py auto      -> signals + news + weekly (GitHub Actions har 5 min)
  python gold_bot.py signals   -> sirf signals
  python gold_bot.py news      -> sirf news
  python gold_bot.py weekly    -> weekly report abhi bhejo (test)
  python gold_bot.py test      -> Telegram test message + chart
"""
import os, sys, json, math, re, io, html, traceback
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
import xml.etree.ElementTree as ET
import numpy as np
import pandas as pd
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

UTC = timezone.utc
BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, "bot_state.json")
LOG_FILE = os.path.join(BASE, "signals_log.json")
OUT_DIR = os.path.join(BASE, "out")


def env(*names, default=""):
    for n in names:
        v = os.environ.get(n)
        if v and v.strip():
            return v.strip()
    return default


TOKEN = env("TELEGRAM_BOT_TOKEN", "BOT_TOKEN", "TELEGRAM_TOKEN", "TG_TOKEN")
CHAT_ID = env("TELEGRAM_CHAT_ID", "CHAT_ID", "TG_CHAT_ID")
FRED_KEY = env("FRED_API_KEY")
DRY = env("DRY_RUN") == "1"

SYMBOLS = [env("GOLD_SYMBOL", default="GC=F"), "XAUUSD=X"]   # Yahoo symbols (pehla fail ho to doosra)
PRICE_OFFSET = float(env("PRICE_OFFSET", default="0"))        # GC=F - Exness price (futures premium)
LAST_SYMBOL = [""]
TFS = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}
HTF = {"5m": "1h", "15m": "4h", "1h": "4h", "4h": "1d"}
COOLDOWN_MIN = {"5m": 45, "15m": 120, "1h": 360, "4h": 960}
_ADJ = float(env("MIN_SCORE_ADJ", default="0"))          # +1 = zyada strict, -1 = zyada signals
MIN_SCORE = {k: v + _ADJ for k, v in {"5m": 7.5, "15m": 7.0, "1h": 6.5, "4h": 6.5}.items()}
GRADE_A = 9.0
SEND_TFS = [x.strip() for x in env("SEND_TFS", default="5m,15m,1h,4h").split(",") if x.strip()]
WEEKLY_HOUR_UTC = int(env("WEEKLY_HOUR_UTC", default="19"))
MAX_HEADLINES_PER_DAY = int(env("MAX_HEADLINES_PER_DAY", default="5"))
NOTIFY_HITS = env("NOTIFY_HITS", default="0") == "1"
LOOK = 3            # kitni purani candle tak ka pattern "taaza" maana jaye


# ----------------------------------------------------------------- utils
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False, default=str)


def now_utc():
    return datetime.now(UTC)


def esc(s):
    return html.escape(str(s), quote=False)


def fmt(p, d=2):
    return f"{p:,.{d}f}"


# ----------------------------------------------------------------- telegram
def _save_dry(name, png):
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, name)
    with open(p, "wb") as f:
        f.write(png)
    print("[DRY] saved", p)


_dry_counter = [0]


def tg(method, data=None, files=None):
    if DRY or not TOKEN or not CHAT_ID:
        print("[DRY]", method, {k: str(v)[:300] for k, v in (data or {}).items()})
        if files:
            _dry_counter[0] += 1
            _save_dry(f"msg{_dry_counter[0]:02d}.png", files["photo"][1])
        return {"ok": True, "result": {"message_id": 0}}
    try:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}",
                          data=data, files=files, timeout=45)
        j = r.json()
    except Exception as e:
        print("TG exception", method, e)
        return {"ok": False}
    if not j.get("ok"):
        print("TG error", method, j.get("description"))
    return j


def send_text(text):
    return tg("sendMessage", {"chat_id": CHAT_ID, "text": text[:4000], "parse_mode": "HTML",
                              "disable_web_page_preview": "true"})


def send_photo(png, caption):
    return tg("sendPhoto", {"chat_id": CHAT_ID, "caption": caption[:1020], "parse_mode": "HTML"},
              files={"photo": ("chart.png", png)})


def pin(message_id, old_id=None):
    if not message_id:
        return
    if old_id:
        tg("unpinChatMessage", {"chat_id": CHAT_ID, "message_id": old_id})
    tg("pinChatMessage", {"chat_id": CHAT_ID, "message_id": message_id, "disable_notification": "true"})


# ----------------------------------------------------------------- market data
_cache = {}


def yahoo(symbol, interval, rng):
    key = (symbol, interval, rng)
    if key in _cache:
        return _cache[key]
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    last_err = None
    for attempt in range(3):
        try:
            r = requests.get(url, params={"interval": interval, "range": rng, "includePrePost": "false"},
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
            res = r.json()["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            df = pd.DataFrame({"open": q["open"], "high": q["high"], "low": q["low"], "close": q["close"]},
                              index=pd.to_datetime(res["timestamp"], unit="s", utc=True)).dropna()
            if len(df) > 20:
                _cache[key] = df
                return df
        except Exception as e:
            last_err = e
    raise RuntimeError(f"yahoo fail {symbol} {interval}: {last_err}")


def gold(interval, rng):
    last = None
    for sym in dict.fromkeys(SYMBOLS):
        try:
            df = yahoo(sym, interval, rng)
            LAST_SYMBOL[0] = sym
            if PRICE_OFFSET:
                df = df.copy()
                df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]] - PRICE_OFFSET
            return df
        except Exception as e:
            last = e
    raise RuntimeError(f"gold data nahi mila: {last}")


def drop_open_bar(df, tf_min):
    """abhi chal rahi (adhoori) candle hata do"""
    limit = now_utc() - timedelta(minutes=tf_min)
    return df[df.index <= limit]


def get_tf(tf):
    if tf == "5m":
        df = gold("5m", "5d")
    elif tf == "15m":
        df = gold("15m", "10d")
    elif tf == "1h":
        df = gold("60m", "60d")
    elif tf == "4h":
        h1 = gold("60m", "60d")
        df = h1.resample("4h").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    elif tf == "1d":
        df = gold("1d", "1y")
        return df
    else:
        raise ValueError(tf)
    return drop_open_bar(df, TFS[tf])


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def add_ind(df):
    df = df.copy()
    pc = df.close.shift(1)
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    df["ema20"] = ema(df.close, 20)
    df["ema50"] = ema(df.close, 50)
    return df


# =================================================================== PATTERN ENGINE
@dataclass
class Ev:
    name: str
    dir: int            # +1 buy, -1 sell, 0 sirf info
    w: float            # weight
    cat: str            # candle / structure / liquidity / zone / pattern / level / context
    idx: int            # kis candle par bana
    draw: list = field(default_factory=list)
    sl: float = None    # invalidation level (SL hint)
    info: bool = False


def pivots(df, k=3):
    h = df.high.values
    l = df.low.values
    n = len(df)
    hs, ls = [], []
    for i in range(k, n - k):
        if h[i] > h[i - k:i].max() and h[i] >= h[i + 1:i + k + 1].max():
            hs.append((i, float(h[i])))
        if l[i] < l[i - k:i].min() and l[i] <= l[i + 1:i + k + 1].min():
            ls.append((i, float(l[i])))
    return hs, ls


def zigzag(hs, ls):
    """H/L pivots ko alternate sequence me badlo"""
    pts = sorted([(i, p, "H") for i, p in hs] + [(i, p, "L") for i, p in ls])
    out = []
    for pt in pts:
        if out and out[-1][2] == pt[2]:
            if (pt[2] == "H" and pt[1] > out[-1][1]) or (pt[2] == "L" and pt[1] < out[-1][1]):
                out[-1] = pt
        else:
            out.append(pt)
    return out


def label_swings(zz):
    labs = []
    lastH = lastL = None
    for i, p, t in zz:
        if t == "H":
            labs.append((i, p, "HH" if (lastH is not None and p > lastH) else "LH" if lastH is not None else "H"))
            lastH = p
        else:
            labs.append((i, p, "HL" if (lastL is not None and p > lastL) else "LL" if lastL is not None else "L"))
            lastL = p
    return labs


def trend_from_labels(labs):
    hh = [x for x in labs if x[2] in ("HH", "LH")][-2:]
    ll = [x for x in labs if x[2] in ("HL", "LL")][-2:]
    if not hh or not ll:
        return 0
    up = sum(1 for x in hh + ll if x[2] in ("HH", "HL"))
    dn = sum(1 for x in hh + ll if x[2] in ("LH", "LL"))
    if up >= 3:
        return 1
    if dn >= 3:
        return -1
    return 0


def cluster_levels(points, tol):
    """pivots ko cluster karke S/R levels: [(price, touches, last_idx)]"""
    pts = sorted(points, key=lambda x: x[1])
    groups = []
    for i, p in pts:
        if groups and abs(p - np.mean([q for _, q in groups[-1]])) <= tol:
            groups[-1].append((i, p))
        else:
            groups.append([(i, p)])
    out = []
    for g in groups:
        out.append((float(np.mean([q for _, q in g])), len(g), max(i for i, _ in g)))
    return out


# ------------------------------------------------------------------ structure: BOS / CHoCH / MSS
def structure_events(df, k=3):
    """pichli 70 candles me BOS / CHoCH / MSS dhundo. Return (events_all, labs, trend)"""
    n = len(df)
    c = df.close.values
    o = df.open.values
    a = df.atr.values
    hs, ls = pivots(df, k)
    zz = zigzag(hs, ls)
    labs = label_swings(zz)
    evs = []
    start = max(k * 3, n - 70)
    for j in range(start, n):
        known = [s for s in labs if s[0] + k <= j - 1]
        Hs = [s for s in known if s[2] in ("HH", "LH", "H")]
        Ls = [s for s in known if s[2] in ("HL", "LL", "L")]
        if len(Hs) < 2 or len(Ls) < 2:
            continue
        lastH, lastL = Hs[-1], Ls[-1]
        tr = trend_from_labels(known)
        body = abs(c[j] - o[j])
        if c[j] > lastH[1] and c[j - 1] <= lastH[1]:
            if tr == -1 and lastH[2] == "LH":
                nm = "Market Structure Shift (MSS)" if body >= 1.2 * a[j] else "Change of Character (CHoCH)"
                evs.append(Ev(nm, +1, 2.5 if "MSS" in nm else 2.0, "structure", j,
                              [dict(kind="break", x0=lastH[0], x1=j, y=lastH[1], label=nm.split(" (")[-1].rstrip(")") if "(" in nm else nm,
                                    color="#00e676")], sl=lastL[1]))
                evs[-1].draw[0]["mb"] = (lastL[0], float(df.low.values[lastL[0]]), float(df.high.values[lastL[0]]))
            else:
                evs.append(Ev("Break of Structure (BOS)", +1, 1.5, "structure", j,
                              [dict(kind="break", x0=lastH[0], x1=j, y=lastH[1], label="BOS", color="#00e676")], sl=lastL[1]))
        if c[j] < lastL[1] and c[j - 1] >= lastL[1]:
            if tr == 1 and lastL[2] == "HL":
                nm = "Market Structure Shift (MSS)" if body >= 1.2 * a[j] else "Change of Character (CHoCH)"
                evs.append(Ev(nm, -1, 2.5 if "MSS" in nm else 2.0, "structure", j,
                              [dict(kind="break", x0=lastL[0], x1=j, y=lastL[1], label=nm.split(" (")[-1].rstrip(")") if "(" in nm else nm,
                                    color="#ff5252")], sl=lastH[1]))
                evs[-1].draw[0]["mb"] = (lastH[0], float(df.low.values[lastH[0]]), float(df.high.values[lastH[0]]))
            else:
                evs.append(Ev("Break of Structure (BOS)", -1, 1.5, "structure", j,
                              [dict(kind="break", x0=lastL[0], x1=j, y=lastL[1], label="BOS", color="#ff5252")], sl=lastH[1]))
    return evs, labs, trend_from_labels(labs), zz


# ------------------------------------------------------------------ candlestick patterns
def candle_events(df, look=LOOK):
    o, h, l, c, a = (df[x].values for x in ("open", "high", "low", "close", "atr"))
    n = len(df)
    body = np.abs(c - o)
    rng = h - l
    upw = h - np.maximum(o, c)
    low_w = np.minimum(o, c) - l
    evs = []

    def pt(j):  # pehle ka trend
        if j < 7:
            return 0
        d = c[j - 1] - c[j - 6]
        return 1 if d > 0.9 * a[j] else -1 if d < -0.9 * a[j] else 0

    def add(name, d, w, j, sl=None, info=False):
        txt = name
        evs.append(Ev(name, d, w, "candle", j, [dict(kind="note", x=j, y=(l[j] if d >= 0 else h[j]), text=txt, dir=d)],
                      sl=sl, info=info))

    for j in range(max(8, n - look), n):
        A = a[j]
        if A <= 0 or rng[j] <= 0:
            continue
        bull = c[j] > o[j]
        bear = c[j] < o[j]
        t = pt(j)
        # --- Marubozu
        if body[j] >= 0.9 * rng[j] and rng[j] >= 1.2 * A:
            add("Marubozu", 1 if bull else -1, 1.0, j, l[j] if bull else h[j])
        # --- Doji family
        if rng[j] >= 0.5 * A and body[j] <= 0.1 * rng[j]:
            if low_w[j] >= 0.65 * rng[j] and upw[j] <= 0.15 * rng[j]:
                add("Dragonfly Doji", 1, 1.0 if t == -1 else 0.6, j, l[j])
            elif upw[j] >= 0.65 * rng[j] and low_w[j] <= 0.15 * rng[j]:
                add("Gravestone Doji", -1, 1.0 if t == 1 else 0.6, j, h[j])
            else:
                add("Doji", 0, 0.0, j, info=True)
        # --- Hammer / Hanging man / Inverted hammer / Shooting star
        if rng[j] >= 0.6 * A and body[j] > 0:
            if low_w[j] >= 2 * body[j] and upw[j] <= 0.5 * body[j]:
                if t == -1:
                    add("Hammer", 1, 1.3, j, l[j])
                elif t == 1:
                    add("Hanging Man", -1, 1.0, j, h[j])
            if upw[j] >= 2 * body[j] and low_w[j] <= 0.5 * body[j]:
                if t == -1:
                    add("Inverted Hammer", 1, 1.0, j, l[j])
                elif t == 1:
                    add("Shooting Star", -1, 1.3, j, h[j])
        # --- two candle
        p = j - 1
        if c[p] < o[p] and bull and o[j] <= c[p] and c[j] >= o[p] and body[j] > body[p] >= 0.2 * A:
            add("Bullish Engulfing", 1, 1.6 if t == -1 else 1.0, j, min(l[j], l[p]))
        if c[p] > o[p] and bear and o[j] >= c[p] and c[j] <= o[p] and body[j] > body[p] >= 0.2 * A:
            add("Bearish Engulfing", -1, 1.6 if t == 1 else 1.0, j, max(h[j], h[p]))
        if t == -1 and c[p] < o[p] and body[p] >= 0.5 * A and bull and o[j] <= c[p] + 0.1 * A \
                and (o[p] + c[p]) / 2 < c[j] < o[p]:
            add("Piercing Line", 1, 1.3, j, min(l[j], l[p]))
        if t == 1 and c[p] > o[p] and body[p] >= 0.5 * A and bear and o[j] >= c[p] - 0.1 * A \
                and o[p] < c[j] < (o[p] + c[p]) / 2:
            add("Dark Cloud Cover", -1, 1.3, j, max(h[j], h[p]))
        if t == -1 and abs(l[j] - l[p]) <= 0.1 * A and c[p] < o[p] and bull:
            add("Tweezer Bottom", 1, 1.2, j, min(l[j], l[p]))
        if t == 1 and abs(h[j] - h[p]) <= 0.1 * A and c[p] > o[p] and bear:
            add("Tweezer Top", -1, 1.2, j, max(h[j], h[p]))
        # --- inside / outside bar
        if j >= 2 and h[p] < h[j - 2] and l[p] > l[j - 2]:
            if c[j] > h[j - 2]:
                add("Inside Bar", 1, 1.0, j, l[p])
            elif c[j] < l[j - 2]:
                add("Inside Bar", -1, 1.0, j, h[p])
        if h[j] > h[p] and l[j] < l[p] and body[j] >= 0.6 * rng[j] and rng[j] >= 1.0 * A:
            add("Outside Bar", 1 if bull else -1, 0.8, j, l[j] if bull else h[j])
        # --- three candle
        if j >= 2:
            q = j - 2
            if c[q] < o[q] and body[q] >= 0.6 * A and body[p] <= 0.35 * body[q] and bull \
                    and c[j] > (o[q] + c[q]) / 2 and max(o[p], c[p]) <= c[q] + 0.3 * A:
                add("Morning Star", 1, 2.0, j, min(l[j], l[p], l[q]))
            if c[q] > o[q] and body[q] >= 0.6 * A and body[p] <= 0.35 * body[q] and bear \
                    and c[j] < (o[q] + c[q]) / 2 and min(o[p], c[p]) >= c[q] - 0.3 * A:
                add("Evening Star", -1, 2.0, j, max(h[j], h[p], h[q]))
            tri = [q, p, j]
            if all(c[x] > o[x] and body[x] >= 0.4 * A and upw[x] <= 0.35 * body[x] for x in tri) \
                    and c[q] < c[p] < c[j] and o[p] >= min(o[q], c[q]) and o[j] >= min(o[p], c[p]):
                add("Three White Soldiers", 1, 2.0, j, l[q])
            if all(c[x] < o[x] and body[x] >= 0.4 * A and low_w[x] <= 0.35 * body[x] for x in tri) \
                    and c[q] > c[p] > c[j] and o[p] <= max(o[q], c[q]) and o[j] <= max(o[p], c[p]):
                add("Three Black Crows", -1, 2.0, j, h[q])
    return evs


# ------------------------------------------------------------------ levels, liquidity
def sr_levels(df, hs, ls):
    n = len(df)
    A = float(df.atr.values[-1])
    pts = [(i, p) for i, p in hs + ls if i >= n - 200]
    lv = cluster_levels(pts, 0.5 * A)
    return [x for x in lv if x[1] >= 2]


def eq_pools(hs, ls, A, n):
    """Equal highs / equal lows (liquidity pools)"""
    pools = []
    for pts, t in ((hs, "H"), (ls, "L")):
        pts = [x for x in pts if x[0] >= n - 80]
        for g in cluster_levels(pts, 0.25 * A):
            if g[1] >= 2:
                pools.append((g[0], g[1], g[2], t))
    return pools


def liquidity_events(df, hs, ls, pools, look=LOOK):
    o, h, l, c, a = (df[x].values for x in ("open", "high", "low", "close", "atr"))
    n = len(df)
    evs = []
    pool_prices = [(p, t) for p, _, _, t in pools]
    # EQH / EQL (info: target)
    for p, cnt, li, t in pools:
        evs.append(Ev("Equal Highs (EQH)" if t == "H" else "Equal Lows (EQL)", 0, 0.0, "liquidity", n - 1,
                      [dict(kind="hline", y=p, x0=max(0, li - 25), x1=n - 1, label="EQH" if t == "H" else "EQL",
                            color="#ffd54f", style=":")], info=True))
    # sweeps of major swing highs/lows (confirmed pivots, 3..60 bars old)
    for j in range(max(10, n - look), n):
        for i, p in hs:
            if (j - 60 <= i <= j - 3) and h[j] > p and c[j] < p and h[i + 1:j].max() < p + 0.05 * a[j]:
                eq = any(t == "H" and abs(pp - p) <= 0.3 * a[j] for pp, t in pool_prices)
                wick = (h[j] - max(o[j], c[j])) >= 0.5 * (h[j] - l[j])
                nm = "Liquidity Grab (EQH)" if eq else ("Liquidity Grab" if wick else "Liquidity Sweep")
                evs.append(Ev(nm, -1, 2.2 if eq else 2.0, "liquidity", j,
                              [dict(kind="hline", y=p, x0=i, x1=j + 1, label=nm, color="#ff80ab", style="--"),
                               dict(kind="mark", x=j, y=h[j], marker="v", color="#ff80ab")], sl=float(h[j])))
                break
        for i, p in ls:
            if (j - 60 <= i <= j - 3) and l[j] < p and c[j] > p and l[i + 1:j].min() > p - 0.05 * a[j]:
                eq = any(t == "L" and abs(pp - p) <= 0.3 * a[j] for pp, t in pool_prices)
                wick = (min(o[j], c[j]) - l[j]) >= 0.5 * (h[j] - l[j])
                nm = "Liquidity Grab (EQL)" if eq else ("Liquidity Grab" if wick else "Liquidity Sweep")
                evs.append(Ev(nm, +1, 2.2 if eq else 2.0, "liquidity", j,
                              [dict(kind="hline", y=p, x0=i, x1=j + 1, label=nm, color="#69f0ae", style="--"),
                               dict(kind="mark", x=j, y=l[j], marker="^", color="#69f0ae")], sl=float(l[j])))
                break
    # inducement: chhota (k=2) pivot sweep
    mh, ml = pivots(df, 2)
    major_h = {i for i, _ in hs}
    major_l = {i for i, _ in ls}
    for j in range(max(10, n - 2), n):
        for i, p in mh:
            if i in major_h or not (j - 12 <= i <= j - 3):
                continue
            if h[j] > p and c[j] < p and o[j] > c[j]:
                evs.append(Ev("Inducement", -1, 1.0, "liquidity", j,
                              [dict(kind="hline", y=p, x0=i, x1=j + 1, label="IDM", color="#ff80ab", style=":")], sl=float(h[j])))
                break
        for i, p in ml:
            if i in major_l or not (j - 12 <= i <= j - 3):
                continue
            if l[j] < p and c[j] > p and c[j] > o[j]:
                evs.append(Ev("Inducement", +1, 1.0, "liquidity", j,
                              [dict(kind="hline", y=p, x0=i, x1=j + 1, label="IDM", color="#69f0ae", style=":")], sl=float(l[j])))
                break
    return evs


# ------------------------------------------------------------------ SMC zones
def disp_flags(df):
    o, h, l, c, a = (df[x].values for x in ("open", "high", "low", "close", "atr"))
    body = np.abs(c - o)
    rng = np.maximum(h - l, 1e-9)
    prev_a = np.roll(a, 1)
    prev_a[0] = a[0]
    return (body >= 1.6 * prev_a) & (body >= 0.65 * rng)


def zone_events(df, structure_evs):
    o, h, l, c, a = (df[x].values for x in ("open", "high", "low", "close", "atr"))
    n = len(df)
    j = n - 1
    A = a[j]
    evs = []
    disp = disp_flags(df)

    def zone_draw(x0, y0, y1, color, label):
        return dict(kind="zone", x0=x0, x1=n - 1, y0=y0, y1=y1, color=color, label=label)

    # ---- displacement (taaza)
    for k in range(max(1, n - LOOK), n):
        if disp[k]:
            d = 1 if c[k] > o[k] else -1
            evs.append(Ev("Displacement", d, 1.0, "zone", k,
                          [dict(kind="note", x=k, y=(l[k] if d > 0 else h[k]), text="Displacement", dir=d)],
                          sl=float(l[k] if d > 0 else h[k])))

    # ---- FVG / IFVG
    for i in range(max(2, n - 60), n - 1):
        if l[i] > h[i - 2] and (l[i] - h[i - 2]) >= 0.2 * a[i]:      # bullish FVG
            bot, top = float(h[i - 2]), float(l[i])
            seg_l, seg_c, seg_h = l[i + 1:j], c[i + 1:j], h[i + 1:j]
            broken = np.where(seg_c < bot)[0]
            if len(broken) == 0:
                touched = (seg_l <= top).any()
                if not touched and i + 2 <= j and l[j] <= top and c[j] > bot:
                    evs.append(Ev("Fair Value Gap (FVG)", 1, 1.5, "zone", j, [zone_draw(i - 1, bot, top, "#00e676", "Bull FVG")], sl=bot))
            else:
                jb = i + 1 + int(broken[0])
                after_h = h[jb + 1:j]
                if jb < j and not (len(after_h) and (after_h >= bot).any()) and h[j] >= bot and c[j] < top:
                    evs.append(Ev("Inverse Fair Value Gap (IFVG)", -1, 1.5, "zone", j, [zone_draw(i - 1, bot, top, "#ff5252", "IFVG")], sl=top))
        if h[i] < l[i - 2] and (l[i - 2] - h[i]) >= 0.2 * a[i]:      # bearish FVG
            bot, top = float(h[i]), float(l[i - 2])
            seg_h, seg_c, seg_l = h[i + 1:j], c[i + 1:j], l[i + 1:j]
            broken = np.where(seg_c > top)[0]
            if len(broken) == 0:
                touched = (seg_h >= bot).any()
                if not touched and i + 2 <= j and h[j] >= bot and c[j] < top:
                    evs.append(Ev("Fair Value Gap (FVG)", -1, 1.5, "zone", j, [zone_draw(i - 1, bot, top, "#ff5252", "Bear FVG")], sl=top))
            else:
                jb = i + 1 + int(broken[0])
                after_l = l[jb + 1:j]
                if jb < j and not (len(after_l) and (after_l <= top).any()) and l[j] <= top and c[j] > (bot + top) / 2:
                    evs.append(Ev("Inverse Fair Value Gap (IFVG)", 1, 1.5, "zone", j, [zone_draw(i - 1, bot, top, "#00e676", "IFVG")], sl=bot))

    # ---- Order Block / Breaker Block
    for d in range(max(6, n - 70), n - 2):
        if not disp[d]:
            continue
        if c[d] > o[d]:                                    # bullish displacement -> bearish candle OB
            m = next((x for x in range(d - 1, max(d - 6, 0), -1) if c[x] < o[x]), None)
            if m is None or c[d] <= h[m]:
                continue
            bot, top = float(l[m]), float(h[m])
            seg_c, seg_l, seg_h = c[d + 1:j], l[d + 1:j], h[d + 1:j]
            br = np.where(seg_c < bot)[0]
            if len(br) == 0:
                if not (seg_l <= top).any() and l[j] <= top and c[j] > bot:
                    evs.append(Ev("Bullish Order Block", 1, 2.0, "zone", j, [zone_draw(m, bot, top, "#00e676", "Bull OB")], sl=bot))
            else:
                jb = d + 1 + int(br[0])
                if jb < j and not (h[jb + 1:j] >= bot).any() and h[j] >= bot and c[j] < top:
                    evs.append(Ev("Breaker Block", -1, 2.0, "zone", j, [zone_draw(m, bot, top, "#ff5252", "Breaker")], sl=top))
        else:                                              # bearish displacement -> bullish candle OB
            m = next((x for x in range(d - 1, max(d - 6, 0), -1) if c[x] > o[x]), None)
            if m is None or c[d] >= l[m]:
                continue
            bot, top = float(l[m]), float(h[m])
            seg_c, seg_l, seg_h = c[d + 1:j], l[d + 1:j], h[d + 1:j]
            br = np.where(seg_c > top)[0]
            if len(br) == 0:
                if not (seg_h >= bot).any() and h[j] >= bot and c[j] < top:
                    evs.append(Ev("Bearish Order Block", -1, 2.0, "zone", j, [zone_draw(m, bot, top, "#ff5252", "Bear OB")], sl=top))
            else:
                jb = d + 1 + int(br[0])
                if jb < j and not (l[jb + 1:j] <= top).any() and l[j] <= top and c[j] > bot:
                    evs.append(Ev("Breaker Block", 1, 2.0, "zone", j, [zone_draw(m, bot, top, "#00e676", "Breaker")], sl=bot))

    # ---- Mitigation block (CHoCH/MSS ke origin candle ka zone)
    for e in structure_evs:
        mb = e.draw[0].get("mb") if e.draw else None
        if not mb or "Change" not in e.name and "MSS" not in e.name:
            continue
        mi, mlow, mhigh = mb
        if e.idx >= j - 1 or j - e.idx > 45:
            continue
        seg_h, seg_l, seg_c = h[e.idx + 1:j], l[e.idx + 1:j], c[e.idx + 1:j]
        if e.dir > 0:      # bullish shift -> pichli girti candle ka zone support
            if not (seg_l <= mhigh).any() and not (seg_c < mlow).any() and l[j] <= mhigh and c[j] > mlow:
                evs.append(Ev("Mitigation Block", 1, 1.5, "zone", j, [zone_draw(mi, mlow, mhigh, "#69f0ae", "Mitigation")], sl=mlow))
        else:
            if not (seg_h >= mlow).any() and not (seg_c > mhigh).any() and h[j] >= mlow and c[j] < mhigh:
                evs.append(Ev("Mitigation Block", -1, 1.5, "zone", j, [zone_draw(mi, mlow, mhigh, "#ff8a80", "Mitigation")], sl=mhigh))

    # ---- Rejection block (lambi wick ka zone)
    hs, ls = pivots(df, 3)
    for i, p in hs:
        if i < n - 60 or i > j - 3:
            continue
        rg = h[i] - l[i]
        wick_bot = max(o[i], c[i])
        if rg > 0 and (h[i] - wick_bot) >= 0.5 * rg and rg >= 0.8 * a[i]:
            if not (h[i + 1:j] >= wick_bot).any() and h[j] >= wick_bot and c[j] < wick_bot:
                evs.append(Ev("Rejection Block", -1, 1.5, "zone", j, [zone_draw(i, wick_bot, float(h[i]), "#ff8a80", "Rejection")], sl=float(h[i])))
    for i, p in ls:
        if i < n - 60 or i > j - 3:
            continue
        rg = h[i] - l[i]
        wick_top = min(o[i], c[i])
        if rg > 0 and (wick_top - l[i]) >= 0.5 * rg and rg >= 0.8 * a[i]:
            if not (l[i + 1:j] <= wick_top).any() and l[j] <= wick_top and c[j] > wick_top:
                evs.append(Ev("Rejection Block", 1, 1.5, "zone", j, [zone_draw(i, float(l[i]), wick_top, "#69f0ae", "Rejection")], sl=float(l[i])))

    # ---- Liquidity void (info/target): 3+ lagatar tez candles
    for s in range(max(1, n - 40), n - 3):
        ok_up = all(c[x] > o[x] and abs(c[x] - o[x]) >= 0.8 * a[x] for x in (s, s + 1, s + 2))
        ok_dn = all(c[x] < o[x] and abs(c[x] - o[x]) >= 0.8 * a[x] for x in (s, s + 1, s + 2))
        if ok_up or ok_dn:
            e_ = s + 2
            while e_ + 1 < n - 1 and ((c[e_ + 1] > o[e_ + 1]) == ok_up) and abs(c[e_ + 1] - o[e_ + 1]) >= 0.5 * a[e_ + 1]:
                e_ += 1
            lo_, hi_ = float(min(o[s], c[s])), float(max(o[e_], c[e_]))
            if ok_dn:
                lo_, hi_ = float(min(o[e_], c[e_])), float(max(o[s], c[s]))
            unfilled = not ((l[e_ + 1:] <= hi_) & (h[e_ + 1:] >= lo_)).any() if e_ + 1 < n else True
            if unfilled and hi_ - lo_ >= 2.0 * A:
                evs.append(Ev("Liquidity Void", 0, 0.0, "zone", e_, [zone_draw(s, lo_, hi_, "#b388ff", "Void")], info=True))
                break
    return evs


# ------------------------------------------------------------------ premium / discount, OTE
def context_events(df, zz):
    o, h, l, c, a = (df[x].values for x in ("open", "high", "low", "close", "atr"))
    n = len(df)
    j = n - 1
    A = a[j]
    evs = []
    w = min(60, n)
    hi, lo = float(h[-w:].max()), float(l[-w:].min())
    if hi - lo >= 2.5 * A:
        eq = (hi + lo) / 2
        pos = (c[j] - lo) / (hi - lo)
        dr = [dict(kind="zone", x0=n - w, x1=n - 1, y0=eq, y1=hi, color="#ff5252", label="Premium", alpha=0.05),
              dict(kind="zone", x0=n - w, x1=n - 1, y0=lo, y1=eq, color="#00e676", label="Discount", alpha=0.05),
              dict(kind="hline", y=eq, x0=n - w, x1=n - 1, label="EQ 50%", color="#90a4ae", style="--")]
        if pos <= 0.4:
            evs.append(Ev("Discount Zone", 1, 1.0, "context", j, dr))
        elif pos >= 0.6:
            evs.append(Ev("Premium Zone", -1, 1.0, "context", j, dr))
        else:
            evs.append(Ev("Equilibrium", 0, 0.0, "context", j, dr, info=True))
    # OTE (0.62 - 0.79)
    if len(zz) >= 2:
        (i1, p1, t1), (i2, p2, t2) = zz[-2], zz[-1]
        size = abs(p2 - p1)
        if size >= 2.5 * A and (j - i2) >= 2 and (n - i2) <= 60:
            if t2 == "H" and t1 == "L":
                zt, zb = p2 - 0.62 * size, p2 - 0.79 * size
                if l[j] <= zt and c[j] >= zb - 0.3 * A:
                    evs.append(Ev("Optimal Trade Entry (OTE)", 1, 1.5, "context", j,
                                  [dict(kind="zone", x0=i2, x1=n - 1, y0=zb, y1=zt, color="#ffd54f", label="OTE 62-79%", alpha=0.18),
                                   dict(kind="line", x0=i1, y0=p1, x1=i2, y1=p2, color="#ffd54f", style="-", label="")],
                                  sl=float(zb - 0.4 * A)))
            if t2 == "L" and t1 == "H":
                zb, zt = p2 + 0.62 * size, p2 + 0.79 * size
                if h[j] >= zb and c[j] <= zt + 0.3 * A:
                    evs.append(Ev("Optimal Trade Entry (OTE)", -1, 1.5, "context", j,
                                  [dict(kind="zone", x0=i2, x1=n - 1, y0=zb, y1=zt, color="#ffd54f", label="OTE 62-79%", alpha=0.18),
                                   dict(kind="line", x0=i1, y0=p1, x1=i2, y1=p2, color="#ffd54f", style="-", label="")],
                                  sl=float(zt + 0.4 * A)))
    return evs


# ------------------------------------------------------------------ chart patterns
def _line_val(p0, p1, x):
    (x0, y0), (x1, y1) = p0, p1
    if x1 == x0:
        return y0
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def chart_pattern_events(df, hs, ls, look=LOOK):
    o, h, l, c, a = (df[x].values for x in ("open", "high", "low", "close", "atr"))
    n = len(df)
    A = float(a[-1])
    tol = 0.6 * A
    evs = []

    def first_break(start, cond):
        for j in range(start, n):
            if cond(j):
                return j
        return None

    def poly(pts, color, label):
        return dict(kind="poly", pts=pts, color=color, label=label)

    # ---------- Triple top / bottom, Double top / bottom
    done_top = done_bot = False
    if len(hs) >= 3 and len(ls) >= 2:
        (i1, p1), (i2, p2), (i3, p3) = hs[-3:]
        if max(p1, p2, p3) - min(p1, p2, p3) <= tol and i2 - i1 >= 4 and i3 - i2 >= 4:
            lows_b = [p for i, p in ls if i1 < i < i3]
            if len(lows_b) >= 2:
                neck = min(lows_b)
                top = max(p1, p2, p3)
                br = first_break(i3 + 1, lambda j: c[j] < neck)
                if br is not None and br >= n - look and h[i3 + 1:br + 1].max() < top + 0.3 * A and top - neck >= 1.2 * A:
                    evs.append(Ev("Triple Top", -1, 2.8, "pattern", br,
                                  [poly([(i1, p1), (i2, p2), (i3, p3)], "#ff5252", "Triple Top"),
                                   dict(kind="hline", y=neck, x0=i1, x1=br + 3, label="Neckline", color="#ff5252", style="--")],
                                  sl=top + 0.2 * A))
                    done_top = True
    if len(ls) >= 3 and len(hs) >= 2:
        (i1, p1), (i2, p2), (i3, p3) = ls[-3:]
        if max(p1, p2, p3) - min(p1, p2, p3) <= tol and i2 - i1 >= 4 and i3 - i2 >= 4:
            highs_b = [p for i, p in hs if i1 < i < i3]
            if len(highs_b) >= 2:
                neck = max(highs_b)
                bot = min(p1, p2, p3)
                br = first_break(i3 + 1, lambda j: c[j] > neck)
                if br is not None and br >= n - look and l[i3 + 1:br + 1].min() > bot - 0.3 * A and neck - bot >= 1.2 * A:
                    evs.append(Ev("Triple Bottom", 1, 2.8, "pattern", br,
                                  [poly([(i1, p1), (i2, p2), (i3, p3)], "#00e676", "Triple Bottom"),
                                   dict(kind="hline", y=neck, x0=i1, x1=br + 3, label="Neckline", color="#00e676", style="--")],
                                  sl=bot - 0.2 * A))
                    done_bot = True
    if not done_top and len(hs) >= 2:
        (i1, p1), (i2, p2) = hs[-2:]
        between = [p for i, p in ls if i1 < i < i2]
        if i2 - i1 >= 5 and abs(p1 - p2) <= tol and between:
            neck, top = min(between), max(p1, p2)
            br = first_break(i2 + 1, lambda j: c[j] < neck)
            if br is not None and br >= n - look and top - neck >= 1.2 * A and h[i2 + 1:br + 1].max() < top + 0.3 * A:
                evs.append(Ev("Double Top", -1, 2.5, "pattern", br,
                              [poly([(i1, p1), (i2, p2)], "#ff5252", "Double Top"),
                               dict(kind="hline", y=neck, x0=i1, x1=br + 3, label="Neckline", color="#ff5252", style="--")],
                              sl=top + 0.2 * A))
    if not done_bot and len(ls) >= 2:
        (i1, p1), (i2, p2) = ls[-2:]
        between = [p for i, p in hs if i1 < i < i2]
        if i2 - i1 >= 5 and abs(p1 - p2) <= tol and between:
            neck, bot = max(between), min(p1, p2)
            br = first_break(i2 + 1, lambda j: c[j] > neck)
            if br is not None and br >= n - look and neck - bot >= 1.2 * A and l[i2 + 1:br + 1].min() > bot - 0.3 * A:
                evs.append(Ev("Double Bottom", 1, 2.5, "pattern", br,
                              [poly([(i1, p1), (i2, p2)], "#00e676", "Double Bottom"),
                               dict(kind="hline", y=neck, x0=i1, x1=br + 3, label="Neckline", color="#00e676", style="--")],
                              sl=bot - 0.2 * A))

    # ---------- Head & Shoulders / Inverse
    if len(hs) >= 3:
        (i1, p1), (i2, p2), (i3, p3) = hs[-3:]
        if p2 > p1 + 0.5 * A and p2 > p3 + 0.5 * A and abs(p1 - p3) <= 1.2 * A and i2 - i1 >= 3 and i3 - i2 >= 3:
            b1 = [(i, p) for i, p in ls if i1 < i < i2]
            b2 = [(i, p) for i, p in ls if i2 < i < i3]
            if b1 and b2:
                n1, n2 = min(b1, key=lambda x: x[1]), min(b2, key=lambda x: x[1])
                br = first_break(i3 + 1, lambda j: c[j] < _line_val(n1, n2, j))
                if br is not None and br >= n - look:
                    evs.append(Ev("Head and Shoulders", -1, 3.0, "pattern", br,
                                  [poly([(i1, p1), (n1[0], n1[1]), (i2, p2), (n2[0], n2[1]), (i3, p3)], "#ff5252", "Head & Shoulders"),
                                   dict(kind="line", x0=n1[0], y0=n1[1], x1=br + 3, y1=_line_val(n1, n2, br + 3), color="#ff5252", style="--", label="Neckline")],
                                  sl=p3 + 0.3 * A))
    if len(ls) >= 3:
        (i1, p1), (i2, p2), (i3, p3) = ls[-3:]
        if p2 < p1 - 0.5 * A and p2 < p3 - 0.5 * A and abs(p1 - p3) <= 1.2 * A and i2 - i1 >= 3 and i3 - i2 >= 3:
            b1 = [(i, p) for i, p in hs if i1 < i < i2]
            b2 = [(i, p) for i, p in hs if i2 < i < i3]
            if b1 and b2:
                n1, n2 = max(b1, key=lambda x: x[1]), max(b2, key=lambda x: x[1])
                br = first_break(i3 + 1, lambda j: c[j] > _line_val(n1, n2, j))
                if br is not None and br >= n - look:
                    evs.append(Ev("Inverse Head and Shoulders", 1, 3.0, "pattern", br,
                                  [poly([(i1, p1), (n1[0], n1[1]), (i2, p2), (n2[0], n2[1]), (i3, p3)], "#00e676", "Inv. H&S"),
                                   dict(kind="line", x0=n1[0], y0=n1[1], x1=br + 3, y1=_line_val(n1, n2, br + 3), color="#00e676", style="--", label="Neckline")],
                                  sl=p3 - 0.3 * A))

    # ---------- Triangles / Wedges
    hh = [(i, p) for i, p in hs if i >= n - 90][-4:]
    ll = [(i, p) for i, p in ls if i >= n - 90][-4:]
    if len(hh) >= 2 and len(ll) >= 2 and len(hh) + len(ll) >= 5:
        x0 = min(hh[0][0], ll[0][0])
        if n - x0 >= 20:
            mh, bh = np.polyfit([i for i, _ in hh], [p for _, p in hh], 1)
            ml, bl = np.polyfit([i for i, _ in ll], [p for _, p in ll], 1)
            up = lambda x: mh * x + bh
            lo = lambda x: ml * x + bl
            sh, sl_ = mh * 10 / A, ml * 10 / A                    # ATR per 10 bars
            w0, w1 = up(x0) - lo(x0), up(n - 1) - lo(n - 1)
            if w1 > 0.5 * A and w0 > 1.25 * w1:                    # converge ho rahe hain
                kind = None
                if abs(sh) <= 0.25 and sl_ >= 0.3:
                    kind = ("Ascending Triangle", 1, "up")
                elif abs(sl_) <= 0.25 and sh <= -0.3:
                    kind = ("Descending Triangle", -1, "down")
                elif sh <= -0.2 and sl_ >= 0.2:
                    kind = ("Symmetrical Triangle", 0, "either")
                elif sh >= 0.15 and sl_ >= 0.15 and sl_ > sh * 1.15:
                    kind = ("Rising Wedge", -1, "down")
                elif sh <= -0.15 and sl_ <= -0.15 and abs(sh) > abs(sl_) * 1.15:
                    kind = ("Falling Wedge", 1, "up")
                if kind:
                    nm, d, side = kind
                    for j in range(max(x0 + 5, n - look), n):
                        bu = c[j] > up(j) + 0.1 * A and c[j - 1] <= up(j - 1) + 0.1 * A
                        bd = c[j] < lo(j) - 0.1 * A and c[j - 1] >= lo(j - 1) - 0.1 * A
                        dd = None
                        if side == "up" and bu:
                            dd = 1
                        elif side == "down" and bd:
                            dd = -1
                        elif side == "either":
                            dd = 1 if bu else -1 if bd else None
                        if dd is not None:
                            col = "#00e676" if dd > 0 else "#ff5252"
                            evs.append(Ev(nm, dd, 2.6, "pattern", j,
                                          [dict(kind="line", x0=x0, y0=up(x0), x1=j, y1=up(j), color=col, style="-", label=nm),
                                           dict(kind="line", x0=x0, y0=lo(x0), x1=j, y1=lo(j), color=col, style="-", label="")],
                                          sl=float(lo(j) - 0.2 * A) if dd > 0 else float(up(j) + 0.2 * A)))
                            break

    # ---------- Flags / Pennants
    found = False
    for m in range(5, 16):
        if found:
            break
        jj = n - 3                                       # consolidation end (breakout last 2 bars me)
        j0 = jj - m + 1                                  # consolidation start
        if j0 < 12:
            continue
        cons_h, cons_l = h[j0:jj + 1].max(), l[j0:jj + 1].min()
        for d in (1, -1):
            pole_seg = slice(max(0, j0 - 9), j0 + 1)
            if d == 1:
                pole = c[j0] - l[pole_seg].min()
            else:
                pole = h[pole_seg].max() - c[j0]
            if pole < 3.0 * A or (cons_h - cons_l) > 0.5 * pole:
                continue
            for j in range(n - 2, n):
                if (d == 1 and c[j] > cons_h and c[j - 1] <= cons_h) or (d == -1 and c[j] < cons_l and c[j - 1] >= cons_l):
                    xs = np.arange(j0, jj + 1)
                    sh_ = np.polyfit(xs, h[j0:jj + 1], 1)[0] * m / A
                    sl2 = np.polyfit(xs, l[j0:jj + 1], 1)[0] * m / A
                    pennant = sh_ < -0.1 and sl2 > 0.1
                    if pennant:
                        nm = "Bullish Pennant" if d == 1 else "Bearish Pennant"
                    else:
                        nm = "Bull Flag" if d == 1 else "Bear Flag"
                    col = "#00e676" if d == 1 else "#ff5252"
                    hi_l = np.polyfit(xs, h[j0:jj + 1], 1)
                    lo_l = np.polyfit(xs, l[j0:jj + 1], 1)
                    evs.append(Ev(nm, d, 2.4, "pattern", j,
                                  [dict(kind="line", x0=j0, y0=np.polyval(hi_l, j0), x1=j, y1=np.polyval(hi_l, j), color=col, style="-", label=nm),
                                   dict(kind="line", x0=j0, y0=np.polyval(lo_l, j0), x1=j, y1=np.polyval(lo_l, j), color=col, style="-", label=""),
                                   dict(kind="line", x0=max(0, j0 - 9), y0=float(l[pole_seg].min()) if d == 1 else float(h[pole_seg].max()),
                                        x1=j0, y1=float(c[j0]), color="#ffd54f", style=":", label="Pole")],
                                  sl=float(cons_l - 0.2 * A) if d == 1 else float(cons_h + 0.2 * A)))
                    found = True
                    break
            if found:
                break

    # ---------- Range formation + range fade
    w = 30
    if n > w + 5:
        rh, rl = h[-w:].max(), l[-w:].min()
        top_t = sum(1 for i, p in hs if i >= n - w and p >= rh - 0.3 * (rh - rl))
        bot_t = sum(1 for i, p in ls if i >= n - w and p <= rl + 0.3 * (rh - rl))
        if (rh - rl) <= 3.5 * A and top_t >= 1 and bot_t >= 1 and (top_t + bot_t) >= 3:
            dr = [dict(kind="zone", x0=n - w, x1=n - 1, y0=float(rl), y1=float(rh), color="#9e9e9e", label="Range", alpha=0.10)]
            pos = (c[-1] - rl) / (rh - rl)
            evs.append(Ev("Range Formation", 0, 0.0, "pattern", n - 1, dr, info=True))
            if pos >= 0.85 and c[-1] < o[-1]:
                evs.append(Ev("Range Formation", -1, 1.0, "pattern", n - 1, dr, sl=float(rh + 0.2 * A)))
            elif pos <= 0.15 and c[-1] > o[-1]:
                evs.append(Ev("Range Formation", 1, 1.0, "pattern", n - 1, dr, sl=float(rl - 0.2 * A)))
    return evs


# ------------------------------------------------------------------ level based: S/R flip, breakout-retest, false breakout
def level_events(df, levels):
    o, h, l, c, a = (df[x].values for x in ("open", "high", "low", "close", "atr"))
    n = len(df)
    j = n - 1
    A = a[j]
    evs = []
    for price, touches, last_i in levels:
        # --- breakout up, ab upar se retest (resistance -> support)
        seg = c[max(0, j - 40):j]
        if len(seg) >= 6:
            base = max(0, j - 40)
            above = np.where(seg > price + 0.15 * A)[0]
            if len(above):
                b = base + int(above[0])
                if b > 0 and c[b - 1] <= price + 0.1 * A and (c[b:j] >= price - 0.4 * A).all() and j - b >= 3:
                    if l[j] <= price + 0.35 * A and c[j] > price and c[j] > o[j]:
                        nm = "Support and Resistance Flip" if touches >= 3 else "Breakout and Retest"
                        evs.append(Ev(nm, 1, 2.2, "level", j,
                                      [dict(kind="hline", y=price, x0=max(0, b - 10), x1=n - 1, label="S/R flip", color="#40c4ff", style="--")],
                                      sl=float(price - 0.6 * A)))
            below = np.where(seg < price - 0.15 * A)[0]
            if len(below):
                b = base + int(below[0])
                if b > 0 and c[b - 1] >= price - 0.1 * A and (c[b:j] <= price + 0.4 * A).all() and j - b >= 3:
                    if h[j] >= price - 0.35 * A and c[j] < price and c[j] < o[j]:
                        nm = "Support and Resistance Flip" if touches >= 3 else "Breakout and Retest"
                        evs.append(Ev(nm, -1, 2.2, "level", j,
                                      [dict(kind="hline", y=price, x0=max(0, b - 10), x1=n - 1, label="S/R flip", color="#40c4ff", style="--")],
                                      sl=float(price + 0.6 * A)))
        # --- false breakout (wick bahar, close andar)
        for k in range(max(1, n - 2), n):
            if h[k] > price + 0.1 * A and c[k] < price and c[k - 1] < price + 0.1 * A and (h[k] - max(o[k], c[k])) >= 0.4 * (h[k] - l[k]):
                evs.append(Ev("False Breakout", -1, 2.0, "level", k,
                              [dict(kind="hline", y=price, x0=max(0, k - 25), x1=n - 1, label="False BO", color="#ff80ab", style="--"),
                               dict(kind="mark", x=k, y=float(h[k]), marker="v", color="#ff80ab")], sl=float(h[k] + 0.15 * A)))
            if l[k] < price - 0.1 * A and c[k] > price and c[k - 1] > price - 0.1 * A and (min(o[k], c[k]) - l[k]) >= 0.4 * (h[k] - l[k]):
                evs.append(Ev("False Breakout", 1, 2.0, "level", k,
                              [dict(kind="hline", y=price, x0=max(0, k - 25), x1=n - 1, label="False BO", color="#69f0ae", style="--"),
                               dict(kind="mark", x=k, y=float(l[k]), marker="^", color="#69f0ae")], sl=float(l[k] - 0.15 * A)))
    return evs


# ------------------------------------------------------------------ analyze (score + SL/TP)
def htf_bias(df):
    """higher timeframe ka bias: +1 bullish, -1 bearish, 0 neutral"""
    d = add_ind(df).iloc[-200:]
    hs, ls = pivots(d, 3)
    labs = label_swings(zigzag(hs, ls))
    t = trend_from_labels(labs)
    if t != 0:
        return t
    c, e20, e50 = d.close.iloc[-1], d.ema20.iloc[-1], d.ema50.iloc[-1]
    if c > e50 and e20 > e50:
        return 1
    if c < e50 and e20 < e50:
        return -1
    return 0


def _dedupe(evs):
    seen, out = set(), []
    for e in sorted(evs, key=lambda x: -x.w):
        key = (e.name, e.dir)
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def _score(evs, d):
    by = {}
    for e in evs:
        if e.dir == d and e.w > 0:
            by.setdefault(e.cat, []).append(e.w)
    tot = 0.0
    for ws in by.values():
        ws.sort(reverse=True)
        tot += min(sum(ws[:2]), 3.6)
    return tot, len(by)


def analyze(df_raw, tf, bias):
    df = add_ind(df_raw).iloc[-260:]
    n = len(df)
    if n < 120:
        return None
    A = float(df.atr.iloc[-1])
    hs, ls = pivots(df, 3)
    s_all, labs, trend, zz = structure_events(df)
    levels = sr_levels(df, hs, ls)
    pools = eq_pools(hs, ls, A, n)

    evs = [e for e in s_all if e.idx >= n - LOOK]
    evs += candle_events(df)
    evs += liquidity_events(df, hs, ls, pools)
    evs += zone_events(df, s_all)
    evs += context_events(df, zz)
    evs += chart_pattern_events(df, hs, ls)
    evs += level_events(df, levels)
    evs = _dedupe(evs)

    # candle ko level / zone par bonus
    low_n, high_n, close_n = float(df.low.iloc[-1]), float(df.high.iloc[-1]), float(df.close.iloc[-1])
    for e in evs:
        if e.cat == "candle" and e.dir != 0:
            near = any((e.dir > 0 and abs(low_n - p) <= 0.6 * A) or (e.dir < 0 and abs(high_n - p) <= 0.6 * A)
                       for p, _, _ in levels)
            zone_same = any(z.cat in ("zone", "liquidity") and z.dir == e.dir and z.w > 0 for z in evs)
            if near or zone_same:
                e.w += 0.8

    sb, nb = _score(evs, 1)
    ss, ns = _score(evs, -1)
    d = 1 if sb >= ss else -1
    best, ncat, opp = (sb, nb, ss) if d == 1 else (ss, ns, sb)
    if bias == d:
        best += 1.0
    elif bias == -d:
        best -= 2.0
    if ncat < 3 or best < MIN_SCORE[tf] or opp >= 0.6 * best:
        return {"skip": True, "bull": sb, "bear": ss}
    used = [e for e in evs if e.dir == d and e.w > 0]
    used.sort(key=lambda e: -e.w)
    trade = build_trade(df, d, used, zz, levels, pools, A)
    if trade is None:
        return {"skip": True, "bull": sb, "bear": ss, "why": "SL door / room nahi"}
    trade.update(dict(dir=d, score=round(best, 1), grade="A" if best >= GRADE_A else "B", tf=tf, bias=bias,
                      names=[e.name for e in used][:8], bar_time=str(df.index[-1])))
    return {"skip": False, "trade": trade, "df": df, "evs": evs, "used": used, "labs": labs,
            "levels": levels, "pools": pools, "zz": zz}


def build_trade(df, d, used, zz, levels, pools, A):
    entry = float(df.close.iloc[-1])
    buf = 0.15 * A
    hints = []
    for e in used:
        if e.sl is None:
            continue
        sl = e.sl - buf if d > 0 else e.sl + buf
        risk = (entry - sl) if d > 0 else (sl - entry)
        if risk > 0:
            hints.append(risk)
    valid = sorted([r for r in hints if 0.8 * A <= r <= 3.0 * A])
    if valid:
        risk = valid[0]                                  # sabse nazdeek valid invalidation
    elif hints and min(hints) < 0.8 * A:
        risk = 1.0 * A                                   # bahut tight tha, ATR se chauda kiya
    elif hints:
        return None                                      # SL bahut door
    else:
        sw = [p for i, p, t in zz[-6:] if (t == "L" if d > 0 else t == "H")]
        sw = [(entry - p + 0.25 * A) if d > 0 else (p - entry + 0.25 * A) for p in sw]
        sw = [r for r in sw if 0.8 * A <= r <= 3.0 * A]
        risk = min(sw) if sw else 1.5 * A
    sl = entry - d * risk
    # raaste me opposite level kitni door hai
    opp = [p for p, t, _ in levels if (p - entry) * d > 0]
    if opp:
        nearest = min(abs(p - entry) for p in opp)
        if nearest < 1.2 * risk:
            return None
    tp1, tp2 = entry + d * 1.5 * risk, entry + d * 2.5 * risk
    tp3 = entry + d * 4.0 * risk
    cand = [p for p, _, _, t in pools if (p - entry) * d > 0 and 2.8 * risk <= abs(p - entry) <= 7 * risk]
    if cand:
        tp3 = min(cand, key=lambda p: abs(p - entry))
    return dict(entry=round(entry, 2), sl=round(sl, 2), tp1=round(tp1, 2), tp2=round(tp2, 2), tp3=round(tp3, 2),
                risk=round(risk, 2), atr=round(A, 2), rr3=round(abs(tp3 - entry) / risk, 1))


# ------------------------------------------------------------------ chart drawing
BG, FG, GRID = "#0e1117", "#d0d7de", "#222a35"
UPC, DNC = "#26a69a", "#ef5350"


def _style(ax):
    ax.set_facecolor(BG)
    for s in ax.spines.values():
        s.set_color(GRID)
    ax.tick_params(colors=FG, labelsize=9)
    ax.grid(color=GRID, linewidth=0.5)


def draw_candles(ax, df, off=0):
    o, h, l, c = (df[x].values for x in ("open", "high", "low", "close"))
    x = np.arange(len(df)) + off
    col = np.where(c >= o, UPC, DNC)
    ax.vlines(x, l, h, colors=col, linewidth=1.0, zorder=3)
    ax.bar(x, np.maximum(np.abs(c - o), 1e-6), bottom=np.minimum(o, c), width=0.62, color=col, zorder=4)


def _xt(ax, idx, offx, W, tf_label):
    step = max(1, W // 7)
    pos = list(range(0, W, step))
    ax.set_xticks(pos)
    fmt_ = "%d %b" if tf_label in ("1d", "4h") else "%d %b %H:%M"
    ax.set_xticklabels([idx[offx + p].strftime(fmt_) for p in pos if offx + p < len(idx)], rotation=0)


def draw_items(ax, items, off, W, n):
    notes = 0
    for it in items:
        k = it["kind"]
        if k == "zone":
            x0 = max(it["x0"] - off, -0.5)
            x1 = it["x1"] - off + 0.5
            if x1 < 0:
                continue
            y0, y1 = it["y0"], it["y1"]
            ax.add_patch(Rectangle((x0, min(y0, y1)), x1 - x0, abs(y1 - y0), facecolor=it["color"],
                                   alpha=it.get("alpha", 0.2), edgecolor=it["color"], linewidth=0.6, zorder=1))
            if it.get("label") and it.get("alpha", 0.2) >= 0.1:
                ax.text(x0 + 0.5, max(y0, y1), it["label"], color=it["color"], fontsize=8, va="bottom", zorder=6)
        elif k == "hline":
            x0, x1 = max(it["x0"] - off, 0), it["x1"] - off
            if x1 < 0:
                continue
            ax.plot([x0, x1], [it["y"], it["y"]], color=it["color"], linestyle=it.get("style", "--"), linewidth=1.1, zorder=2)
            ax.text(x1, it["y"], " " + it["label"], color=it["color"], fontsize=8, va="center", ha="left", zorder=6)
        elif k == "line":
            ax.plot([it["x0"] - off, it["x1"] - off], [it["y0"], it["y1"]], color=it["color"],
                    linestyle=it.get("style", "-"), linewidth=1.4, zorder=5)
            if it.get("label"):
                ax.text(it["x1"] - off, it["y1"], " " + it["label"], color=it["color"], fontsize=8, va="bottom", zorder=6)
        elif k == "poly":
            xs = [p[0] - off for p in it["pts"]]
            ys = [p[1] for p in it["pts"]]
            ax.plot(xs, ys, color=it["color"], linewidth=1.6, marker="o", markersize=4, zorder=5)
            ax.text(xs[len(xs) // 2], ys[len(ys) // 2], it["label"], color=it["color"], fontsize=9,
                    ha="center", va="bottom", fontweight="bold", zorder=6)
        elif k == "mark":
            ax.scatter([it["x"] - off], [it["y"]], marker=it["marker"], color=it["color"], s=70, zorder=7)
        elif k == "break":
            ax.plot([it["x0"] - off, it["x1"] - off], [it["y"], it["y"]], color=it["color"], linewidth=1.3, linestyle="-", zorder=5)
            ax.text((it["x0"] + it["x1"]) / 2 - off, it["y"], it["label"], color=it["color"], fontsize=8,
                    ha="center", va="bottom", fontweight="bold", zorder=6)
        elif k == "note" and notes < 5:
            notes += 1
            up = it["dir"] >= 0
            ax.annotate(it["text"], xy=(it["x"] - off, it["y"]), xytext=(0, -26 if up else 26),
                        textcoords="offset points", color="#fff59d", fontsize=8, ha="center", zorder=8,
                        arrowprops=dict(arrowstyle="->", color="#fff59d", lw=0.8))


def chart_png(res, tf, W=90, title=None):
    df, trade, used = res["df"], res["trade"], res["used"]
    n = len(df)
    W = min(W, n)
    off = n - W
    view = df.iloc[off:]
    fig, ax = plt.subplots(figsize=(12, 7.5), dpi=100)
    fig.patch.set_facecolor(BG)
    _style(ax)
    draw_candles(ax, view)
    xs = np.arange(W)
    ax.plot(xs, view.ema20.values, color="#4dd0e1", linewidth=1, alpha=0.8, zorder=2)
    ax.plot(xs, view.ema50.values, color="#ffb74d", linewidth=1, alpha=0.8, zorder=2)
    # support / resistance
    last = float(df.close.iloc[-1])
    lv = sorted(res["levels"], key=lambda x: abs(x[0] - last))[:4]
    for p, t, _ in lv:
        ax.axhline(p, color="#607d8b", linewidth=0.8, linestyle="-.", alpha=0.7, zorder=1)
        ax.text(W + 0.2, p, f" S/R {p:.1f}", color="#90a4ae", fontsize=7, va="center")
    # swing labels
    for i, p, lab in res["labs"][-10:]:
        if i >= off and lab in ("HH", "HL", "LH", "LL"):
            top = lab in ("HH", "LH")
            ax.text(i - off, p, lab, color="#00e676" if lab in ("HH", "HL") else "#ff5252", fontsize=8, fontweight="bold",
                    ha="center", va="bottom" if top else "top", zorder=6)
    # strategies (jo is signal me bani) + info zones
    items = []
    for e in res["used"]:
        items += e.draw
    for e in res["evs"]:
        if e.info:
            items += e.draw
    # context shading (premium/discount) bhi dikhao
    for e in res["evs"]:
        if e.cat == "context" and e.dir == trade["dir"] and e not in used:
            items += e.draw
    draw_items(ax, items, off, W, n)
    # trade levels
    d = trade["dir"]
    x0, x1 = W - 1, W + 9
    ent, sl = trade["entry"], trade["sl"]
    ax.add_patch(Rectangle((x0, min(ent, sl)), x1 - x0, abs(ent - sl), facecolor="#ef5350", alpha=0.18, zorder=1))
    ax.add_patch(Rectangle((x0, min(ent, trade["tp3"])), x1 - x0, abs(trade["tp3"] - ent), facecolor="#26a69a", alpha=0.10, zorder=1))
    for lab, val, col in (("ENTRY", ent, "#ffffff"), ("SL", sl, "#ef5350"), ("TP1", trade["tp1"], "#80cbc4"),
                          ("TP2", trade["tp2"], "#4db6ac"), ("TP3", trade["tp3"], "#26a69a")):
        ax.plot([x0, x1], [val, val], color=col, linewidth=1.6, zorder=6)
        ax.text(x1 + 0.2, val, f"{lab} {val:.2f}", color=col, fontsize=9, fontweight="bold", va="center", zorder=7)
    ax.scatter([W - 1], [ent], marker="^" if d > 0 else "v", s=140, color="#ffffff", zorder=9)
    ax.set_xlim(-1, W + 15)
    lo = min(float(view.low.min()), sl, trade["tp3"] if d < 0 else 1e9)
    hi = max(float(view.high.max()), sl, trade["tp3"] if d > 0 else -1e9)
    pad = (hi - lo) * 0.06
    ax.set_ylim(lo - pad, hi + pad)
    _xt(ax, df.index, off, W, tf)
    head = title or (f"XAUUSD  {tf}   {'BUY' if d > 0 else 'SELL'}   Grade {trade['grade']}  (score {trade['score']})")
    ax.set_title(head, color="#ffffff", fontsize=15, fontweight="bold", loc="left")
    names = []
    for e in used:
        if e.name not in names:
            names.append(e.name)
    leg = "Strategies:\n" + "\n".join(f"- {x}" for x in names[:9])
    ax.text(0.01, 0.985, leg, transform=ax.transAxes, color="#cfd8dc", fontsize=8.5, va="top", ha="left", zorder=10,
            bbox=dict(facecolor="#161b22", edgecolor=GRID, alpha=0.85, boxstyle="round,pad=0.4"))
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    plt.close(fig)
    return buf.getvalue()


# =================================================================== SIGNAL RUNNER
def market_open(now):
    wd = now.weekday()
    if wd == 5:
        return False
    if wd == 6 and now.hour < 22:
        return False
    if wd == 4 and now.hour >= 22:
        return False
    return True


def in_blackout(state, now):
    """bade news (CPI/NFP/FOMC...) se pehle 15 min aur baad ke 5 min signal band"""
    for ev in state.get("cal", {}).get("events", []):
        if not ev.get("vh"):
            continue
        try:
            t = datetime.fromisoformat(ev["time"])
        except Exception:
            continue
        if -5 * 60 <= (t - now).total_seconds() <= 15 * 60:
            return ev.get("label", "news")
    return None


BIAS_TXT = {1: "Bullish", -1: "Bearish", 0: "Neutral"}


def signal_caption(t):
    side = "BUY" if t["dir"] > 0 else "SELL"
    dot = "\U0001F7E2" if t["dir"] > 0 else "\U0001F534"
    R = t["risk"]
    names = t["names"][:7]
    cap = (f"{dot} <b>{side} XAUUSD</b> | {t['tf']} | Grade <b>{t['grade']}</b>\n"
           f"Entry: <b>{t['entry']:.2f}</b>\n"
           f"SL: <b>{t['sl']:.2f}</b>  ({R:.2f} pts)\n"
           f"TP1: <b>{t['tp1']:.2f}</b>  (1.5R)\n"
           f"TP2: <b>{t['tp2']:.2f}</b>  (2.5R)\n"
           f"TP3: <b>{t['tp3']:.2f}</b>  ({t['rr3']}R)\n"
           f"Score {t['score']} | Bias ({HTF[t['tf']]}): {BIAS_TXT[t['bias']]}\n"
           f"<i>{esc(' + '.join(names))}</i>\n"
           f"Risk 1% se zyada nahi. Pehle demo me test karo.")
    return cap


def run_signals(state):
    now = now_utc()
    if not market_open(now):
        print("market band")
        return
    st = state.setdefault("sig", {"last_bar": {}, "last_sent": {}})
    update_log(state)
    bl = in_blackout(state, now)
    if bl:
        print("news blackout:", bl)
        return
    log = load_json(LOG_FILE, [])
    for tf in SEND_TFS:
        if tf not in TFS:
            continue
        try:
            df = get_tf(tf)
            if len(df) < 120:
                continue
            last_bar = df.index[-1]
            if (now - last_bar) > timedelta(minutes=TFS[tf] * 3):
                print(tf, "data purana, skip")
                continue
            if st["last_bar"].get(tf) == str(last_bar):
                continue
            st["last_bar"][tf] = str(last_bar)
            bias = htf_bias(get_tf(HTF[tf]))
            res = analyze(df, tf, bias)
            if not res:
                continue
            if res.get("skip"):
                print(tf, "no signal", {k: round(v, 1) if isinstance(v, float) else v for k, v in res.items() if k != "skip"})
                continue
            t = res["trade"]
            ls_ = st["last_sent"].get(tf)
            if ls_:
                age = (now - datetime.fromisoformat(ls_["time"])).total_seconds() / 60
                if age < COOLDOWN_MIN[tf]:
                    print(tf, "cooldown")
                    continue
                if ls_["dir"] == t["dir"] and abs(ls_["entry"] - t["entry"]) < 0.6 * t["atr"] and age < COOLDOWN_MIN[tf] * 4:
                    print(tf, "same setup, skip")
                    continue
            png = chart_png(res, tf)
            send_photo(png, signal_caption(t))
            st["last_sent"][tf] = {"time": now.isoformat(), "dir": t["dir"], "entry": t["entry"]}
            log.append(dict(id=f"{tf}-{int(now.timestamp())}", time=now.isoformat(), tf=tf, dir=t["dir"], entry=t["entry"],
                            sl=t["sl"], tp1=t["tp1"], tp2=t["tp2"], tp3=t["tp3"], score=t["score"], grade=t["grade"],
                            names=t["names"], status="open", tp_hit=0, result="open", r=0.0))
        except Exception:
            print("signal error", tf)
            traceback.print_exc()
    save_json(LOG_FILE, log[-400:])


def update_log(state):
    """khule signals ka result check: SL pehle ya TP pehle"""
    log = load_json(LOG_FILE, [])
    openl = [s for s in log if s.get("status") == "open"]
    if not openl:
        return
    try:
        df = gold("5m", "5d")
    except Exception as e:
        print("log update data fail", e)
        return
    now = now_utc()
    for s in openl:
        t0 = pd.Timestamp(s["time"])
        seg = df[df.index > t0]
        d = s["dir"]
        prev = s["tp_hit"]
        for _, row in seg.iterrows():
            hit_sl = row.low <= s["sl"] if d > 0 else row.high >= s["sl"]
            reach = 0
            for k, key in ((1, "tp1"), (2, "tp2"), (3, "tp3")):
                if (d > 0 and row.high >= s[key]) or (d < 0 and row.low <= s[key]):
                    reach = k
            if s["tp_hit"] == 0 and hit_sl:
                s.update(status="closed", result="loss", r=-1.0)
                break
            s["tp_hit"] = max(s["tp_hit"], reach)
            if s["tp_hit"] >= 3:
                s.update(status="closed", result="win", r=1.5)
                break
            if s["tp_hit"] >= 1 and hit_sl:
                s.update(status="closed", result="win", r=1.5)
                break
        if s["status"] == "open" and (now - t0) > timedelta(days=5):
            if s["tp_hit"] >= 1:
                s.update(status="closed", result="win", r=1.5)
            else:
                s.update(status="closed", result="expired", r=0.0)
        if NOTIFY_HITS and (s["tp_hit"] > prev or s["status"] == "closed"):
            side = "BUY" if d > 0 else "SELL"
            msg = f"{side} {s['tf']} @ {s['entry']}: "
            msg += f"TP{s['tp_hit']} hit" if s["tp_hit"] > prev else ""
            if s["status"] == "closed":
                msg += f"  -> {s['result'].upper()}"
            send_text("\U0001F4CC " + msg)
    save_json(LOG_FILE, log)


# =================================================================== NEWS
import textwrap, hashlib
from email.utils import parsedate_to_datetime

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FF_NEXT = "https://nfs.faireconomy.media/ff_calendar_nextweek.json"

# (regex, countries, label, tier, kind)   -> sirf yahi news aayengi
WATCH = [
    (r"\bcpi\b", {"USD"}, "US CPI / Core CPI", "Very High", "cpi"),
    (r"non-farm|nfp", {"USD"}, "US NFP", "Very High", "nfp"),
    (r"unemployment rate", {"USD"}, "US Unemployment Rate", "Very High", "unemp"),
    (r"federal funds rate|fomc", {"USD"}, "FOMC", "Very High", "fomc"),
    (r"powell|fed chair", {"USD"}, "Fed Chair Speech", "Very High", "powell"),
    (r"pce price|\bpce\b", {"USD"}, "US PCE Inflation", "Very High", "pce"),
    (r"\bppi\b", {"USD"}, "US PPI", "High", "ppi"),
    (r"gdp", {"USD"}, "US GDP", "High", "gdp"),
    (r"ism (manufacturing|services)", {"USD"}, "ISM PMI", "High", "ism"),
    (r"jolts", {"USD"}, "JOLTS Job Openings", "High", "jolts"),
    (r"unemployment claims", {"USD"}, "Initial Jobless Claims", "Medium-High", "claims"),
    (r"cpi|gdp|pmi|retail sales|industrial production|trade balance", {"CNY"}, "China Economic Data", "Medium-High", "china"),
]
TIER_COL = {"Very High": "#d32f2f", "High": "#ef6c00", "Medium-High": "#f9a825", "Long-term": "#1565c0"}
HIGHER_BEARISH = {"cpi", "ppi", "pce", "nfp", "gdp", "jolts", "ism"}      # actual zyada -> USD strong -> gold neeche
HIGHER_BULLISH = {"unemp", "claims"}                                      # actual zyada -> USD weak -> gold upar

FRED_MAP = {
    "cpi": {"headline": "CPIAUCSL", "core": "CPILFESL"},
    "ppi": {"headline": "PPIFIS", "core": "PPIFES"},
    "pce": {"headline": "PCEPI", "core": "PCEPILFE"},
    "nfp": {"headline": "PAYEMS"},
    "unemp": {"headline": "UNRATE"},
    "gdp": {"headline": "A191RL1Q225SBEA"},
    "jolts": {"headline": "JTSJOL"},
    "claims": {"headline": "ICSA"},
}


def _classify(title, country, impact):
    tl = title.lower()
    if "adp" in tl:
        return None
    if ("member" in tl or ("speaks" in tl and "chair" not in tl and "powell" not in tl)) and "fomc" in tl:
        return None
    for rx, ctry, label, tier, kind in WATCH:
        if country in ctry and re.search(rx, tl):
            if kind == "china" and impact not in ("High", "Medium"):
                return None
            return label, tier, kind
    return None


def fetch_calendar(url=FF_URL):
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
    r.raise_for_status()
    out = []
    for it in r.json():
        c = _classify(it.get("title", ""), it.get("country", ""), it.get("impact", ""))
        if not c:
            continue
        try:
            t = datetime.fromisoformat(it["date"]).astimezone(UTC)
        except Exception:
            continue
        label, tier, kind = c
        out.append(dict(id=f"{it['title']}|{t.isoformat()}", time=t.isoformat(), title=it["title"], country=it["country"],
                        impact=it.get("impact", ""), forecast=it.get("forecast") or "", previous=it.get("previous") or "",
                        actual=it.get("actual") or "", label=label, tier=tier, kind=kind, vh=(tier == "Very High")))
    return out


def get_calendar(state):
    cal = state.setdefault("cal", {"fetched": None, "events": []})
    need = True
    if cal.get("fetched"):
        age = (now_utc() - datetime.fromisoformat(cal["fetched"])).total_seconds() / 3600
        need = age > 3
    if need:
        try:
            cal["events"] = fetch_calendar()
            cal["fetched"] = now_utc().isoformat()
        except Exception as e:
            print("calendar fetch fail", e)
            cal["fetched"] = (now_utc() - timedelta(hours=2)).isoformat()      # 1 ghante baad dobara try
    return cal["events"]


# ---------------------------------------------------------------- FRED (actual numbers)
def fred_obs(series, limit=15):
    if not FRED_KEY:
        return None
    try:
        r = requests.get("https://api.stlouisfed.org/fred/series/observations",
                         params={"series_id": series, "api_key": FRED_KEY, "file_type": "json",
                                 "sort_order": "desc", "limit": limit}, timeout=20)
        obs = [(o["date"], float(o["value"])) for o in r.json().get("observations", []) if o["value"] not in (".", "")]
        return obs or None
    except Exception as e:
        print("fred fail", series, e)
        return None


def fred_baseline(kind):
    if kind not in FRED_MAP:
        return {}
    base = {}
    for k, sid in FRED_MAP[kind].items():
        o = fred_obs(sid, 2)
        if o:
            base[sid] = o[0][0]
    return base


def fred_actual(ev, base):
    """FF event ke liye FRED se asli number (sirf jab naya data aa chuka ho)"""
    kind = ev["kind"]
    if kind not in FRED_MAP or not base:
        return ""
    tl = ev["title"].lower()
    key = "core" if "core" in tl and "core" in FRED_MAP[kind] else "headline"
    sid = FRED_MAP[kind].get(key)
    o = fred_obs(sid, 15)
    if not o or base.get(sid) is None or o[0][0] <= base[sid]:
        return ""                                          # abhi FRED par naya data nahi
    v = [x[1] for x in o]
    if kind in ("cpi", "ppi", "pce"):
        if "y/y" in tl and len(v) >= 13:
            return f"{(v[0] / v[12] - 1) * 100:.1f}%"
        return f"{(v[0] / v[1] - 1) * 100:.1f}%"
    if kind == "nfp":
        return f"{v[0] - v[1]:+.0f}K"
    if kind in ("unemp", "gdp"):
        return f"{v[0]:.1f}%"
    if kind == "jolts":
        return f"{v[0] / 1000:.2f}M"
    if kind == "claims":
        return f"{v[0] / 1000:.0f}K"
    return ""


def num(s):
    try:
        s = str(s).replace(",", "").replace("%", "").strip()
        mult = 1.0
        if s.endswith("K"):
            s, mult = s[:-1], 1.0
        elif s.endswith("M"):
            s, mult = s[:-1], 1000.0
        elif s.endswith("B"):
            s, mult = s[:-1], 1e6
        return float(s) * mult
    except Exception:
        return None


def gold_bias_text(kind, rows):
    ups = dns = 0
    for r in rows:
        kind = r.get("kind", kind)
        a, f = num(r["actual"]), num(r["forecast"])
        if a is None or f is None or a == f:
            continue
        beat = a > f
        if kind in HIGHER_BEARISH:
            dns += beat
            ups += (not beat)
        elif kind in HIGHER_BULLISH:
            ups += beat
            dns += (not beat)
    if ups > dns:
        return "Gold bias: BULLISH (USD kamzor)", "#26a69a"
    if dns > ups:
        return "Gold bias: BEARISH (USD mazboot)", "#ef5350"
    return "Gold bias: unclear / in-line", "#b0bec5"


# ---------------------------------------------------------------- news card (image)
def news_card(tag, color, title, rows, bias, gold_df, t_mark, footer="", overlay=None, big_title=False):
    fig = plt.figure(figsize=(10, 8), dpi=100)
    fig.patch.set_facecolor(BG)
    fig.patches.append(Rectangle((0, 0.91), 1, 0.09, transform=fig.transFigure, color=color, zorder=0))
    fig.text(0.03, 0.955, tag.upper(), color="white", fontsize=19, fontweight="bold", va="center")
    fig.text(0.97, 0.955, t_mark.strftime("%d %b  %H:%M UTC"), color="white", fontsize=12, ha="right", va="center")
    y = 0.865
    wrapped = textwrap.wrap(title, 42 if big_title else 56)[:4]
    for line in wrapped:
        fig.text(0.03, y, line, color="#ffffff", fontsize=19 if big_title else 15, fontweight="bold", va="center")
        y -= 0.05 if big_title else 0.04
    y -= 0.01
    if rows:
        for x, h in ((0.03, "EVENT"), (0.58, "ACTUAL"), (0.72, "FORECAST"), (0.88, "PREVIOUS")):
            fig.text(x, y, h, color="#78909c", fontsize=10, fontweight="bold", va="center")
        y -= 0.04
        for r in rows[:5]:
            fig.text(0.03, y, textwrap.shorten(r["title"], 34), color="#eceff1", fontsize=12, va="center")
            fig.text(0.58, y, r["actual"] or "-", color="#ffeb3b" if r["actual"] else "#78909c", fontsize=14, fontweight="bold", va="center")
            fig.text(0.72, y, r["forecast"] or "-", color="#b0bec5", fontsize=12, va="center")
            fig.text(0.88, y, r["previous"] or "-", color="#b0bec5", fontsize=12, va="center")
            y -= 0.04
    if bias:
        fig.text(0.03, y - 0.005, bias[0], color=bias[1], fontsize=14, fontweight="bold", va="center")
        y -= 0.04
    if footer:
        fig.text(0.03, y - 0.005, footer, color="#cfd8dc", fontsize=11, va="center")
        y -= 0.035
    bottom = 0.07
    top = max(0.2, min(0.66, y - 0.03))
    ax = fig.add_axes([0.07, bottom, 0.82, top - bottom])
    _style(ax)
    if gold_df is not None and len(gold_df) > 3:
        g = gold_df.iloc[-80:]
        draw_candles(ax, g)
        pos = int(g.index.searchsorted(pd.Timestamp(t_mark)))
        if 0 <= pos < len(g):
            ax.axvline(pos, color="#ffd54f", linestyle="--", linewidth=1.2)
            ax.text(pos, g.high.max(), " NEWS", color="#ffd54f", fontsize=9, va="top")
        step = max(1, len(g) // 6)
        ax.set_xticks(range(0, len(g), step))
        ax.set_xticklabels([g.index[i].strftime("%H:%M") for i in range(0, len(g), step)])
        ax.set_title("XAUUSD 5m", color="#cfd8dc", fontsize=10, loc="left")
        if overlay is not None:
            o = overlay.reindex(g.index, method="nearest", tolerance=pd.Timedelta("10min"))
            ax2 = ax.twinx()
            ax2.plot(np.arange(len(g)), o.values, color="#4fc3f7", linewidth=1.6)
            ax2.tick_params(colors="#4fc3f7", labelsize=8)
            ax2.set_ylabel("", color="#4fc3f7")
            for s in ax2.spines.values():
                s.set_color(GRID)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    plt.close(fig)
    return buf.getvalue()


def gold_reaction(t_ev):
    """news ke time se ab tak gold ki chaal"""
    try:
        g = gold("5m", "2d")
    except Exception:
        return None, ""
    before = g[g.index <= pd.Timestamp(t_ev)]
    after = g[g.index > pd.Timestamp(t_ev)]
    seg = g[g.index >= pd.Timestamp(t_ev) - pd.Timedelta(minutes=120)]
    if before.empty or after.empty:
        return seg, ""
    p0 = float(before.close.iloc[-1])
    p1 = float(after.close.iloc[-1])
    hi, lo = float(after.high.max()), float(after.low.min())
    txt = f"Gold reaction: {p1 - p0:+.2f}$ ({(p1 / p0 - 1) * 100:+.2f}%)  |  range {lo:.0f}-{hi:.0f}"
    return seg, txt


# ---------------------------------------------------------------- main news runner
def run_news(state):
    now = now_utc()
    ns = state.setdefault("news", {})
    for k in ("pre", "done", "seen"):
        ns.setdefault(k, [])
    for k in ("base", "mkt", "heads"):
        ns.setdefault(k, {})
    ns.setdefault("log", [])
    events = get_calendar(state)

    groups = {}
    for ev in events:
        gk = {"unemp": "nfp"}.get(ev["kind"], ev["kind"])          # NFP + Unemployment ek saath
        groups.setdefault((ev["time"], gk), []).append(ev)

    for (tiso, kind), evs in sorted(groups.items()):
        try:
            t = datetime.fromisoformat(tiso)
            dt = (t - now).total_seconds() / 60
            gid = f"{tiso}|{kind}"
            label, tier = evs[0]["label"], evs[0]["tier"]
            if len({e["kind"] for e in evs}) > 1:
                label = "US NFP / Unemployment"
            if -1 < dt <= 120 and gid not in ns["base"]:
                b = {}
                for k_ in {e["kind"] for e in evs}:
                    b.update(fred_baseline(k_))
                ns["base"][gid] = b
            if tier == "Very High" and 5 < dt <= 35 and gid not in ns["pre"]:
                lines = [f"\u23F0 <b>{esc(label)}</b> - {int(dt)} min me  ({t.strftime('%H:%M')} UTC)  [{tier}]"]
                for e in evs[:4]:
                    lines.append(f"- {esc(e['title'])}: forecast {esc(e['forecast'] or '-')} | previous {esc(e['previous'] or '-')}")
                lines.append("Is time signals auto band rahenge.")
                send_text("\n".join(lines))
                ns["pre"].append(gid)
            if -360 <= dt <= -3 and gid not in ns["done"]:
                rows = []
                for e in evs:
                    act = e["actual"] or fred_actual(e, ns["base"].get(gid))
                    rows.append(dict(title=e["title"], actual=act, forecast=e["forecast"], previous=e["previous"], kind=e["kind"]))
                has_data = any(e["kind"] in FRED_MAP for e in evs) and FRED_KEY and ns["base"].get(gid)
                if has_data and not any(r["actual"] for r in rows) and dt > -50:
                    continue                                   # FRED par data aane ka intezaar
                seg, react = gold_reaction(t)
                bias = gold_bias_text(kind, rows) if any(r["actual"] for r in rows) else None
                foot = react
                if any(e["kind"] in FRED_MAP for e in evs) and not any(r["actual"] for r in rows):
                    foot = (react + "   |   " if react else "") + "Actual abhi verify nahi hua"
                png = news_card(label, TIER_COL[tier], (f"{label} - result" if any(e["kind"] in FRED_MAP for e in evs) else f"{label}: abhi aaya"), rows, bias, seg, t, foot)
                cap = f"<b>{esc(label)}</b> [{tier}]\n" + "\n".join(
                    f"{esc(r['title'])}: <b>{esc(r['actual'] or '-')}</b> (F {esc(r['forecast'] or '-')}, P {esc(r['previous'] or '-')})" for r in rows[:5])
                if bias:
                    cap += "\n" + bias[0]
                if react:
                    cap += "\n" + react
                send_photo(png, cap)
                ns["done"].append(gid)
                ns["log"].append(dict(time=tiso, label=label, tier=tier, rows=rows[:5], react=react))
        except Exception:
            print("news group error", tiso, kind)
            traceback.print_exc()
    for k in ("pre", "done", "seen"):
        ns[k] = ns[k][-120:]
    ns["log"] = ns["log"][-60:]
    for k in list(ns["base"].keys())[:-60]:
        ns["base"].pop(k, None)

    try:
        market_moves(ns, now)
    except Exception:
        traceback.print_exc()
    try:
        headline_scan(ns, now)
    except Exception:
        traceback.print_exc()


# ---------------------------------------------------------------- DXY / yields / oil moves
MKT = {
    "dxy": ("DX-Y.NYB", "US Dollar Index (DXY)", 0.35, "pct", "DXY upar = gold par dabaav, DXY neeche = gold ko support"),
    "us10y": ("^TNX", "US 10Y Treasury Yield", 0.07, "abs", "Yield upar = gold par dabaav, yield neeche = gold ko support"),
    "oil": ("CL=F", "Crude Oil", 2.0, "pct", "Oil tez = inflation/geopolitical darr, gold par asar mixed"),
}


def market_moves(ns, now):
    for key, (sym, name, thr, mode, note) in MKT.items():
        last = ns["mkt"].get(key)
        if last and (now - datetime.fromisoformat(last)).total_seconds() < 3 * 3600:
            continue
        try:
            s = yahoo(sym, "5m", "1d").close
        except Exception:
            continue
        if len(s) < 14 or (now - s.index[-1]).total_seconds() > 20 * 60:
            continue
        chg = float(s.iloc[-1] - s.iloc[-13])
        pct = chg / float(s.iloc[-13]) * 100
        big = abs(pct) >= thr if mode == "pct" else abs(chg) >= thr
        if not big:
            continue
        g = gold("5m", "2d")
        seg = g.iloc[-80:]
        val = f"{pct:+.2f}% (1 ghante me)" if mode == "pct" else f"{chg:+.3f} pts (1 ghante me)"
        gchg = float(g.close.iloc[-1] - g.close.iloc[-13])
        rows = [dict(title=name, actual=f"{float(s.iloc[-1]):.2f}", forecast="", previous=f"{float(s.iloc[-13]):.2f}")]
        foot = f"Gold last 1h: {gchg:+.2f}$"
        png = news_card("Market Alert", TIER_COL["Very High"] if key != "oil" else TIER_COL["High"],
                        f"{name} badi chaal: {val}", rows, (note, "#cfd8dc"), seg, now, foot, overlay=s)
        send_photo(png, f"<b>{esc(name)}</b>: {val}\n{esc(note)}\n{foot}")
        ns["mkt"][key] = now.isoformat()


# ---------------------------------------------------------------- headlines (kam aur sirf kaam ki)
HEADS = [
    ("Geopolitical / War", "gold war OR missile OR ceasefire OR sanctions OR attack when:3h",
     r"war|missile|strike|attack|ceasefire|sanction|troops|invasion|escalat", "High", 3, 3),
    ("Central Bank Gold Purchases", "central bank gold purchases when:2d",
     r"central bank|pboc|reserves|tonnes|tons|world gold council", "Long-term", 24, 48),
    ("Gold ETF Inflows / Outflows", "gold ETF inflows outflows when:2d",
     r"etf|inflow|outflow|holdings|spdr|gld", "Medium-High", 24, 48),
    ("China Economic News", "China economy PMI OR GDP OR stimulus OR PBOC when:3h",
     r"china|pboc|yuan", "Medium-High", 8, 3),
]


def headline_scan(ns, now):
    today = now.strftime("%Y-%m-%d")
    cnt = ns["heads"].setdefault("count", {})
    if cnt.get("day") != today:
        cnt.clear()
        cnt.update(day=today, n=0)
    if cnt["n"] >= MAX_HEADLINES_PER_DAY:
        return
    for name, q, rx, tier, cool_h, max_age_h in HEADS:
        last = ns["heads"].get(name)
        if last and (now - datetime.fromisoformat(last)).total_seconds() < cool_h * 3600:
            continue
        try:
            r = requests.get("https://news.google.com/rss/search",
                             params={"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"},
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            root = ET.fromstring(r.content)
        except Exception as e:
            print("rss fail", name, e)
            continue
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = item.findtext("link") or ""
            src = item.findtext("source") or ""
            try:
                pub = parsedate_to_datetime(item.findtext("pubDate")).astimezone(UTC)
            except Exception:
                continue
            if (now - pub).total_seconds() > max_age_h * 3600:
                continue
            tl = title.lower()
            if not re.search(rx, tl) or ("gold" not in tl and name.startswith(("Geo", "Central", "Gold"))):
                continue
            h = hashlib.md5(re.sub(r"\W+", "", tl)[:80].encode()).hexdigest()[:12]
            if h in ns["seen"]:
                continue
            seg = None
            try:
                g = gold("5m", "2d")
                seg = g[g.index >= pd.Timestamp(pub) - pd.Timedelta(minutes=150)]
                if len(seg) < 5:
                    seg = g.iloc[-80:]
            except Exception:
                pass
            png = news_card(name, TIER_COL[tier], title, [], None, seg, pub,
                            f"Source: {src}" if src else "", big_title=True)
            send_photo(png, f"<b>{esc(name)}</b> [{tier}]\n{esc(title)}\n{esc(src)}\n{esc(link)}")
            ns["seen"].append(h)
            ns["heads"][name] = now.isoformat()
            cnt["n"] += 1
            break
        if cnt["n"] >= MAX_HEADLINES_PER_DAY:
            return


# =================================================================== WEEKLY REPORT
def weekly_chart(wk, levels, pv, sigs, title):
    fig, ax = plt.subplots(figsize=(12, 7.5), dpi=100)
    fig.patch.set_facecolor(BG)
    _style(ax)
    draw_candles(ax, wk)
    W = len(wk)
    xs = np.arange(W)
    ax.plot(xs, ema(wk.close, 20).values, color="#4dd0e1", linewidth=1, alpha=0.8)
    hi, lo = float(wk.high.max()), float(wk.low.min())
    ax.axhline(hi, color="#ef5350", linewidth=1.2, linestyle="--")
    ax.text(W + 0.3, hi, f" Week High {hi:.1f}", color="#ef5350", fontsize=9, va="center")
    ax.axhline(lo, color="#26a69a", linewidth=1.2, linestyle="--")
    ax.text(W + 0.3, lo, f" Week Low {lo:.1f}", color="#26a69a", fontsize=9, va="center")
    for name, val, col in pv:
        ax.axhline(val, color=col, linewidth=0.8, linestyle=":", alpha=0.9)
        ax.text(W + 0.3, val, f" {name} {val:.1f}", color=col, fontsize=8, va="center")
    for p, t, _ in levels:
        if lo - 40 < p < hi + 40:
            ax.axhline(p, color="#607d8b", linewidth=0.8, linestyle="-.", alpha=0.6)
    for s in sigs:
        t = pd.Timestamp(s["time"])
        pos = int(wk.index.searchsorted(t))
        if pos >= W:
            continue
        col = "#69f0ae" if s["result"] == "win" else "#ff5252" if s["result"] == "loss" else "#ffd54f"
        ax.scatter([pos], [s["entry"]], marker="^" if s["dir"] > 0 else "v", s=90, color=col, edgecolors="white", linewidths=0.6, zorder=8)
    step = max(1, W // 8)
    ax.set_xticks(range(0, W, step))
    ax.set_xticklabels([wk.index[i].strftime("%a %d %H:%M") for i in range(0, W, step)], fontsize=8)
    ax.set_xlim(-1, W + 14)
    ax.set_title(title, color="#ffffff", fontsize=15, fontweight="bold", loc="left")
    ax.text(0.01, 0.02, "Arrows = bot signals (green win / red loss / yellow open)   |   dotted = weekly pivots", transform=ax.transAxes,
            color="#90a4ae", fontsize=8.5)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    plt.close(fig)
    return buf.getvalue()


def run_weekly(state, force=False):
    now = now_utc()
    wkid = now.strftime("%G-W%V")
    w = state.setdefault("weekly", {})
    if not force and w.get("sent") == wkid:
        return
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    h1 = gold("60m", "60d")
    wk = h1[h1.index >= monday - timedelta(hours=2)]
    if len(wk) < 8:
        wk = h1.iloc[-100:]
    op, cl = float(wk.open.iloc[0]), float(wk.close.iloc[-1])
    hi, lo = float(wk.high.max()), float(wk.low.min())
    chg = cl - op
    # daily line
    days = []
    for d, g in wk.groupby(wk.index.date):
        if len(g) >= 3:
            days.append(f"{pd.Timestamp(d).strftime('%a')} {float(g.close.iloc[-1]) - float(g.open.iloc[0]):+.1f}")
    # pivots + levels
    P = (hi + lo + cl) / 3
    pv = [("R2", P + (hi - lo), "#ef9a9a"), ("R1", 2 * P - lo, "#ef9a9a"), ("P", P, "#b0bec5"),
          ("S1", 2 * P - hi, "#a5d6a7"), ("S2", P - (hi - lo), "#a5d6a7")]
    try:
        d1 = add_ind(get_tf("1d"))
        hs, ls = pivots(d1, 3)
        levels = sr_levels(d1, hs, ls)
        bias = BIAS_TXT[htf_bias(get_tf("1d"))]
    except Exception:
        levels, bias = [], "-"
    above = sorted([p for p, _, _ in levels if p > cl])[:2]
    below = sorted([p for p, _, _ in levels if p < cl], reverse=True)[:2]
    # signals of the week
    log = [s for s in load_json(LOG_FILE, []) if pd.Timestamp(s["time"]) >= pd.Timestamp(monday)]
    wins = sum(1 for s in log if s["result"] == "win")
    loss = sum(1 for s in log if s["result"] == "loss")
    opn = sum(1 for s in log if s["result"] == "open")
    netr = sum(s["r"] for s in log)
    wr = f"{wins / (wins + loss) * 100:.0f}%" if wins + loss else "-"
    tfline = []
    for tf in TFS:
        a = [s for s in log if s["tf"] == tf]
        if a:
            tfline.append(f"{tf}: {len(a)} ({sum(1 for s in a if s['result'] == 'win')}W/{sum(1 for s in a if s['result'] == 'loss')}L)")
    title = f"XAUUSD Weekly Report  {monday.strftime('%d %b')} - {(monday + timedelta(days=4)).strftime('%d %b %Y')}"
    png = weekly_chart(wk, levels, pv, log, title)
    cap = (f"\U0001F4C5 <b>XAUUSD Weekly Report</b>\n"
           f"{monday.strftime('%d %b')} - {(monday + timedelta(days=4)).strftime('%d %b %Y')}\n"
           f"Open {op:.2f} -> Close {cl:.2f}  (<b>{chg:+.2f}</b>, {chg / op * 100:+.2f}%)\n"
           f"High {hi:.2f} | Low {lo:.2f} | Range {hi - lo:.1f}\n"
           f"Trend (1D): {bias}\n"
           f"Signals: {len(log)} | Win {wins} | Loss {loss} | Open {opn} | WR {wr} | Net {netr:+.1f}R")
    res = send_photo(png, cap)
    mid = (res.get("result") or {}).get("message_id")
    # detail message
    ns = state.get("news", {})
    nl = [x for x in ns.get("log", []) if pd.Timestamp(x["time"]) >= pd.Timestamp(monday)]
    lines = [f"<b>Is hafte ka detail</b>", "Daily (open->close): " + ", ".join(days)]
    lines.append(f"Weekly Pivot P {P:.1f} | R1 {pv[1][1]:.1f} | R2 {pv[0][1]:.1f} | S1 {pv[3][1]:.1f} | S2 {pv[4][1]:.1f}")
    parts = []
    if above:
        parts.append("upar " + ", ".join(f"{x:.1f}" for x in above))
    if below:
        parts.append("neeche " + ", ".join(f"{x:.1f}" for x in below))
    if parts:
        lines.append("Daily key levels: " + " | ".join(parts))
    if tfline:
        lines.append("Timeframe wise: " + " | ".join(tfline))
        lines.append("(R: TP1 par exit maan kar; TP2 tak gaye: "
                     f"{sum(1 for s in log if s['tp_hit'] >= 2)}, TP3: {sum(1 for s in log if s['tp_hit'] >= 3)})")
    if nl:
        lines.append("\n<b>Is hafte ki badi news</b>")
        for x in nl[-8:]:
            r0 = x["rows"][0] if x.get("rows") else {}
            lines.append(f"- {esc(x['label'])}: actual {esc(r0.get('actual') or '-')} (F {esc(r0.get('forecast') or '-')})")
    try:
        nxt = [e for e in fetch_calendar(FF_NEXT) if e["vh"]]
        if nxt:
            lines.append("\n<b>Agle hafte ki badi news (UTC)</b>")
            for e in sorted(nxt, key=lambda e: e["time"])[:8]:
                lines.append(f"- {datetime.fromisoformat(e['time']).strftime('%a %H:%M')}  {esc(e['title'])}")
    except Exception:
        pass
    lines.append("\nWeekend: market band. Monday ko naye signals.")
    send_text("\n".join(lines))
    if mid:
        pin(mid, w.get("pin_id"))
        w["pin_id"] = mid
    w["sent"] = wkid


# =================================================================== MAIN
def run_test():
    send_text("\u2705 <b>XAUUSD Pro Bot</b> connected. Telegram setup sahi hai.")
    try:
        g = gold("5m", "1d")
        send_text(f"Data OK ({LAST_SYMBOL[0]}): last close {float(g.close.iloc[-1]):.2f}  ({g.index[-1].strftime('%d %b %H:%M')} UTC)\n"
                  f"Price offset: {PRICE_OFFSET:+.2f}\n"
                  "Apne Exness XAUUSD price se compare karo. Fark ho to PRICE_OFFSET variable me (data - Exness) daalo.")
    except Exception as e:
        send_text(f"Data fail: {esc(e)}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "auto"
    state = load_json(STATE_FILE, {})
    now = now_utc()
    try:
        if mode == "test":
            run_test()
        elif mode == "weekly":
            run_weekly(state, force=True)
        else:
            if mode == "auto" and ((now.weekday() == 4 and now.hour >= WEEKLY_HOUR_UTC) or now.weekday() == 5):
                try:
                    run_weekly(state)
                except Exception:
                    traceback.print_exc()
            if mode in ("auto", "signals"):
                try:
                    run_signals(state)
                except Exception:
                    traceback.print_exc()
            if mode in ("auto", "news") and market_open(now):
                try:
                    run_news(state)
                except Exception:
                    traceback.print_exc()
    finally:
        save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
