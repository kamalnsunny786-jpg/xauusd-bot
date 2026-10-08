#!/usr/bin/env python3
"""
XAUUSD Telegram signal bot (free, no pip installs needed).
Same strategy engine as xauusd-dashboard.html, run on closed candles only.
Sends a Telegram message when a timeframe (5m/15m/1h/4h) flips to BUY or SELL.

Setup:  export TELEGRAM_TOKEN="123:ABC..."   export TELEGRAM_CHAT_ID="123456789"
Run:    python3 xauusd_bot.py --test    (sends a test message)
        python3 xauusd_bot.py --once    (one check, for GitHub Actions / cron)
        python3 xauusd_bot.py           (loop every 60s, for PC / Termux / VPS)
Educational tool, not financial advice. Always use a stop loss.
"""
import os, sys, json, math, time, urllib.request, urllib.parse

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
TFS = ["5m", "15m", "1h", "4h"]
BASES = ["https://data-api.binance.vision", "https://api.binance.com"]
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
MIN_CONF = int(os.environ.get("MIN_CONF", "0"))   # only send if confidence >= this (e.g. 50)

def get_json(url, data=None, timeout=10):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "xauusd-bot"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def klines(tf):
    for b in BASES:
        try:
            j = get_json(f"{b}/api/v3/klines?symbol=PAXGUSDT&interval={tf}&limit=300")
            if isinstance(j, list) and len(j) > 100:
                return [dict(t=k[0], o=float(k[1]), h=float(k[2]), l=float(k[3]), c=float(k[4])) for k in j]
        except Exception as e:
            print("kline fail", b, tf, e)
    return None

def spot():
    try:
        return float(get_json("https://api.gold-api.com/price/XAU")["price"])
    except Exception:
        return None

# ---------------- indicators ----------------
def ema(a, n):
    k = 2 / (n + 1); p = a[0]; out = []
    for i, v in enumerate(a):
        p = v * k + p * (1 - k) if i else a[0]
        out.append(p)
    return out

def rsi_calc(c, n=14):
    g = l = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]
        if d >= 0: g += d
        else: l -= d
    g /= n; l /= n
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n
        l = (l * (n - 1) + max(-d, 0)) / n
    return 100 if l == 0 else 100 - 100 / (1 + g / l)

def atr_calc(d, n=14):
    a = 0.0
    for i in range(1, len(d)):
        tr = max(d[i]["h"] - d[i]["l"], abs(d[i]["h"] - d[i - 1]["c"]), abs(d[i]["l"] - d[i - 1]["c"]))
        a = a + tr / n if i <= n else (a * (n - 1) + tr) / n
    return a

def pivots(d, k=4):
    H, L = [], []
    for i in range(k, len(d) - k):
        hi = lo = True
        for j in range(1, k + 1):
            if d[i]["h"] <= d[i - j]["h"] or d[i]["h"] <= d[i + j]["h"]: hi = False
            if d[i]["l"] >= d[i - j]["l"] or d[i]["l"] >= d[i + j]["l"]: lo = False
        if hi: H.append(d[i]["h"])
        if lo: L.append(d[i]["l"])
    return H, L

# ---------------- strategy engine (7 votes) ----------------
def analyze(d):
    if not d or len(d) < 60: return None
    c = [x["c"] for x in d]; n = len(d); last = c[-1]
    e20 = ema(c, 20)[-1]; e50 = ema(c, 50)[-1]; e200 = ema(c, min(200, n - 1))[-1]
    m12, m26 = ema(c, 12), ema(c, 26)
    macd = [a - b for a, b in zip(m12, m26)]; sg = ema(macd, 9)
    hist = [a - b for a, b in zip(macd, sg)]; h0, h1 = hist[-1], hist[-2]
    rsi, atr = rsi_calc(c), atr_calc(d)
    w = c[-20:]; mean = sum(w) / 20
    sd = math.sqrt(sum((x - mean) ** 2 for x in w) / 20); bu, bl = mean + 2 * sd, mean - 2 * sd
    H, L = pivots(d, 4); hh, ll = H[-2:], L[-2:]
    struct = "range"
    if len(hh) == 2 and len(ll) == 2:
        if hh[1] > hh[0] and ll[1] > ll[0]: struct = "up"
        elif hh[1] < hh[0] and ll[1] < ll[0]: struct = "down"
    swH = H[-1] if H else None; swL = L[-1] if L else None
    votes = []
    # 1 EMA trend
    if last > e20 > e50 > e200: tv, tt = 1, "EMA 20>50>200, price above: strong uptrend"
    elif last < e20 < e50 < e200: tv, tt = -1, "EMA 20<50<200, price below: strong downtrend"
    elif last > e50 and e20 > e50: tv, tt = 0.5, "Price above EMA50: mild uptrend"
    elif last < e50 and e20 < e50: tv, tt = -0.5, "Price below EMA50: mild downtrend"
    else: tv, tt = 0, "EMAs mixed"
    votes.append((tv, 2, tt))
    # 2 structure
    votes.append((1 if struct == "up" else -1 if struct == "down" else 0, 3,
                  {"up": "Higher highs + higher lows", "down": "Lower highs + lower lows", "range": "Range, no clear HH/LL trend"}[struct]))
    # 3 swing breakout
    if swH is not None and last > swH: bv, bt = 1, "Broke above last swing high"
    elif swL is not None and last < swL: bv, bt = -1, "Broke below last swing low"
    else: bv, bt = 0, "Inside last swing range"
    votes.append((bv, 2, bt))
    # 4 MACD
    if h0 > 0: mv, mt = (1, "MACD positive and rising") if h0 > h1 else (0.5, "MACD positive but fading")
    elif h0 < 0: mv, mt = (-1, "MACD negative and falling") if h0 < h1 else (-0.5, "MACD negative but recovering")
    else: mv, mt = 0, "MACD flat"
    votes.append((mv, 1.5, mt))
    # 5 RSI
    rv = max(-1, min(1, (rsi - 50) / 25)); rt = f"RSI {rsi:.0f}: {'bullish' if rv > 0 else 'bearish'} momentum"
    if rsi > 75: rv, rt = -0.5, f"RSI {rsi:.0f}: overbought, pullback risk"
    elif rsi < 25: rv, rt = 0.5, f"RSI {rsi:.0f}: oversold, bounce possible"
    elif abs(rv) < 0.15: rt = f"RSI {rsi:.0f}: neutral"
    votes.append((rv, 1, rt))
    # 6 Bollinger
    gv, gt = 0, "Inside Bollinger bands"
    if struct == "range":
        if last < bl: gv, gt = 1, "Below lower band in range: bounce setup"
        elif last > bu: gv, gt = -1, "Above upper band in range: reversal setup"
    else:
        gv = 0.3 if last > mean else -0.3; gt = "Above Bollinger midline" if gv > 0 else "Below Bollinger midline"
    votes.append((gv, 1, gt))
    # 7 candle pattern (last closed candle = d[-2] since d[-1] is the newest closed here -> use d[-1], d[-2])
    b, p = d[-1], d[-2]; body = abs(b["c"] - b["o"])
    up = b["h"] - max(b["o"], b["c"]); lo = min(b["o"], b["c"]) - b["l"]
    pv, pt = 0, "No candle pattern"
    if p["c"] < p["o"] and b["c"] > b["o"] and b["c"] >= p["o"] and b["o"] <= p["c"]: pv, pt = 1, "Bullish engulfing"
    elif p["c"] > p["o"] and b["c"] < b["o"] and b["c"] <= p["o"] and b["o"] >= p["c"]: pv, pt = -1, "Bearish engulfing"
    elif body > 0 and lo >= 2 * body and up <= body * 0.6: pv, pt = 1, "Hammer"
    elif body > 0 and up >= 2 * body and lo <= body * 0.6: pv, pt = -1, "Shooting star"
    votes.append((pv, 1, pt))
    raw = sum(v * w_ for v, w_, _ in votes) / sum(w_ for _, w_, _ in votes)
    return dict(raw=raw, last=last, atr=atr, rsi=rsi, struct=struct, swH=swH, swL=swL, votes=votes)

def finalize(res):
    for i, t in enumerate(TFS):
        a = res.get(t)
        if not a: continue
        higher = [res[x] for x in TFS[i + 1:] if res.get(x)]
        s = a["raw"]; a["note"] = None
        if higher:
            hb = sum(x["raw"] for x in higher) / len(higher)
            if abs(hb) > 0.15:
                if (hb > 0) == (s > 0): s *= 1.15; a["note"] = "Agrees with higher timeframes"
                else: s *= 0.6; a["note"] = "AGAINST higher timeframe trend (risky)"
        s = max(-1, min(1, s))
        a["score"] = s
        a["sig"] = "BUY" if s >= 0.22 else "SELL" if s <= -0.22 else "WAIT"
        a["strong"] = abs(s) >= 0.45
        a["conf"] = min(99, round(abs(s) * 140))

def levels(a):
    sgn = 1 if a["sig"] == "BUY" else -1
    sl = a["last"] - sgn * a["atr"] * 1.5
    sw = a["swL"] if sgn == 1 else a["swH"]
    if sw is not None and a["atr"] * 0.5 < sgn * (a["last"] - sw) < a["atr"] * 3: sl = sw - sgn * a["atr"] * 0.2
    r = abs(a["last"] - sl)
    return dict(entry=a["last"], sl=sl, tp1=a["last"] + sgn * r * 1.5, tp2=a["last"] + sgn * r * 2.5)

# ---------------- telegram ----------------
def send(text):
    if not TOKEN or not CHAT:
        print("Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID first."); print(text); return False
    data = urllib.parse.urlencode({"chat_id": CHAT, "text": text, "parse_mode": "HTML"}).encode()
    try:
        get_json(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=data)
        return True
    except Exception as e:
        print("telegram error:", e); return False

def message(tf, a, off, strong):
    lv = levels(a); f = lambda v: f"{v + off:,.2f}"
    emoji = "🟢" if a["sig"] == "BUY" else "🔴"
    st = {"up": "Higher High + Higher Low", "down": "Lower High + Lower Low", "range": "Range"}[a["struct"]]
    why = "\n".join("• " + t for v, w, t in a["votes"] if (v > 0) == (a["sig"] == "BUY") and v != 0)
    return (f"{emoji} <b>{'STRONG ' if strong else ''}{a['sig']} XAUUSD</b>  [{tf.upper()}]\n"
            f"Confidence: {a['conf']}%   Trend: {st}\n\n"
            f"Entry: <b>{f(lv['entry'])}</b>\nStop loss: {f(lv['sl'])}\nTP1: {f(lv['tp1'])}\nTP2: {f(lv['tp2'])}\n\n"
            f"{why}\n" + (f"⚠️ {a['note']}\n" if a['note'] else "") +
            "\nEducational signal, not financial advice. Use a stop loss.")

def load_state():
    try: return json.load(open(STATE))
    except Exception: return {}

def check():
    res = {}
    for t in TFS:
        d = klines(t)
        if d: res[t] = analyze(d[:-1])        # closed candles only
    if len(res) < len(TFS) or not all(res.values()):
        print("Not all timeframes loaded, skipping this round (no fake signals)."); return
    finalize(res)
    sp = spot(); off = 0.0
    if sp is not None: off = sp - res["15m"]["last"]   # align PAXG candles to XAU/USD spot
    state = load_state(); changed = False
    for t in TFS:
        a = res[t]
        print(t, a["sig"], a["conf"], "%", a["struct"])
        if a["sig"] != state.get(t):
            if a["sig"] in ("BUY", "SELL") and a["conf"] >= MIN_CONF:
                if send(message(t, a, off, a["strong"])): state[t] = a["sig"]; changed = True
            elif a["sig"] == "WAIT":
                state[t] = "WAIT"; changed = True    # reset silently, next BUY/SELL will be sent
    if changed: json.dump(state, open(STATE, "w"))

if __name__ == "__main__":
    if "--test" in sys.argv:
        print("sent" if send("✅ XAUUSD signal bot connected. You will get BUY/SELL alerts here.") else "failed")
    elif "--once" in sys.argv:
        check()
    else:
        while True:
            try: check()
            except Exception as e: print("error:", e)
            time.sleep(60)
