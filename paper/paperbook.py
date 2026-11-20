"""
Multi-market PAPER trading bot (no real money, no broker connection).

Once a day (after markets close) it replays that day's 5-minute bars bar-by-bar and trades
3 rule-based strategies in 9 books, each with Rs 10,000 virtual capital:

  NIFTY_FUT, NIFTY_OPT, SENSEX_OPT, STOCKS_MIS, USDINR_FUT, USDINR_OPT, BTC_FUT, ETH_FUT, BTC_OPT

Strategies (decisions use only bars up to the current bar, so no look-ahead):
  ORB    - opening-range (first 15 min) breakout, stop at the other side, exit near session end
  TREND  - price crosses TWAP with EMA9 > EMA21 (or < for shorts), exit on cross back / 1.5 ATR stop
  REVERT - RSI(2) extreme and >1 ATR away from TWAP -> fade back to TWAP, 6-bar time stop

Learning: each book splits its capital between the 3 strategies. After every day the split moves
toward strategies that have made money (exp weighting, 10% floor), and a strategy that lost 3 days
in a row sits out the next day. Weights for day D use only days before D.

Options are NOT real option-chain prices: they are Black-Scholes estimates from the underlying,
with IV from India VIX (Nifty/Sensex), 4% (USDINR) or 30-day realised vol (BTC).
"""
import json, math, os, sys, time
from datetime import datetime, timedelta, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, "ledger.json")
REPORT = os.path.join(HERE, "REPORT.md")
IST = timedelta(hours=5, minutes=30)
CAPITAL = 10000.0
START_DAY = "2026-11-21"
END_DAY = "2026-11-28"
STRATS = ["ORB", "TREND", "REVERT"]
STOCKS = ["RELIANCE.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS", "SBIN.NS"]

NSE = ("09:15", "15:15")          # trade window; last exit 15:15
FX = ("09:00", "16:45")
CRYPTO = ("09:00", "20:45")
CRYPTO_OPT = ("09:00", "17:00")   # Delta daily options expire 17:30 IST

# kind: fut | opt | stock.  lev = notional / capital for futures-like books.
BOOKS = {
    "NIFTY_FUT":  dict(sym="^NSEI", kind="fut", lev=8, cost=0.0004, win=NSE, note="asli 1 lot = ~Rs 1.5-2 lakh margin"),
    "NIFTY_OPT":  dict(sym="^NSEI", kind="opt", win=NSE, expiry=("weekday", 1, "15:30"), iv="vix"),
    "SENSEX_OPT": dict(sym="^BSESN", kind="opt", win=NSE, expiry=("weekday", 3, "15:30"), iv="vix"),
    "STOCKS_MIS": dict(sym=STOCKS, kind="stock", lev=5, cost=0.0008, win=NSE),
    "USDINR_FUT": dict(sym="USDINR=X", kind="fut", lev=30, cost=0.0001, win=FX),
    "USDINR_OPT": dict(sym="USDINR=X", kind="opt", win=FX, expiry=("weekday", 4, "12:30"), iv=0.04),
    "BTC_FUT":    dict(sym="BTC-USD", kind="fut", lev=5, cost=0.0012, win=CRYPTO),
    "ETH_FUT":    dict(sym="ETH-USD", kind="fut", lev=5, cost=0.0012, win=CRYPTO),
    "BTC_OPT":    dict(sym="BTC-USD", kind="opt", win=CRYPTO_OPT, expiry=("daily", None, "17:30"), iv="rv"),
}
OPT_COST_FRAC = 0.01   # per side, of premium (slippage + fees)
OPT_FIXED = 25.0       # Rs per side (brokerage/taxes) for INR options
# absolute slippage per side (one tick-ish) and minimum premium we are willing to buy
OPT_TICK = {"NIFTY_OPT": 0.5, "SENSEX_OPT": 1.0, "USDINR_OPT": 0.0025, "BTC_OPT": 5.0}
OPT_MIN_PREM = {"NIFTY_OPT": 10, "SENSEX_OPT": 30, "USDINR_OPT": 0.03, "BTC_OPT": 60}
MIN_HOURS_TO_EXPIRY = 2.0


# ---------------------------------------------------------------- data
def yahoo(sym, rng="7d", interval="5m"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(sym)}?range={rng}&interval={interval}"
    for i in range(3):
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
            j = r.json()["chart"]["result"][0]
            q = j["indicators"]["quote"][0]
            out = []
            for k, t in enumerate(j.get("timestamp") or []):
                o, h, l, c = q["open"][k], q["high"][k], q["low"][k], q["close"][k]
                if None in (o, h, l, c) or c <= 0: continue
                out.append((datetime.fromtimestamp(t, timezone.utc).replace(tzinfo=None) + IST, o, h, l, c))
            return out
        except Exception:
            time.sleep(3 + 3 * i)
    return []


def day_bars(bars, day, win):
    lo = datetime.strptime(f"{day} {win[0]}", "%Y-%m-%d %H:%M")
    hi = datetime.strptime(f"{day} {win[1]}", "%Y-%m-%d %H:%M")
    return [b for b in bars if lo <= b[0] <= hi]


# ---------------------------------------------------------------- indicators (causal)
def ema(vals, n):
    k, out, e = 2 / (n + 1), [], None
    for v in vals:
        e = v if e is None else e + k * (v - e); out.append(e)
    return out


def indicators(bars):
    c = [b[4] for b in bars]
    tp = [(b[2] + b[3] + b[4]) / 3 for b in bars]
    twap, s = [], 0.0
    for i, v in enumerate(tp): s += v; twap.append(s / (i + 1))
    tr = [bars[0][2] - bars[0][3]] + [max(bars[i][2] - bars[i][3], abs(bars[i][2] - c[i - 1]), abs(bars[i][3] - c[i - 1])) for i in range(1, len(bars))]
    atr = ema(tr, 14)
    rsi, up, dn = [], 0.0, 0.0
    for i in range(len(c)):
        if i == 0: rsi.append(50.0); continue
        d = c[i] - c[i - 1]
        up = up + (max(d, 0) - up) / 2; dn = dn + (max(-d, 0) - dn) / 2
        rsi.append(100.0 if dn == 0 else 100 - 100 / (1 + up / dn))
    return dict(c=c, twap=twap, atr=atr, rsi=rsi, e9=ema(c, 9), e21=ema(c, 21))


# ---------------------------------------------------------------- strategies -> list of trades
# trade = dict(dir=+1/-1, i_in, p_in, i_out, p_out, why)
def strat_orb(bars, ind):
    if len(bars) < 6: return []
    hi = max(b[2] for b in bars[:3]); lo = min(b[3] for b in bars[:3])
    for i in range(3, len(bars) - 1):
        c = bars[i][4]
        if c > hi or c < lo:
            d = 1 if c > hi else -1; stop = lo if d == 1 else hi
            for j in range(i + 1, len(bars)):
                if (d == 1 and bars[j][3] <= stop) or (d == -1 and bars[j][2] >= stop):
                    return [dict(dir=d, i_in=i, p_in=c, i_out=j, p_out=stop, why="stop")]
            return [dict(dir=d, i_in=i, p_in=c, i_out=len(bars) - 1, p_out=bars[-1][4], why="close")]
    return []


def strat_trend(bars, ind, max_trades=2):
    c, tw, e9, e21, atr = ind["c"], ind["twap"], ind["e9"], ind["e21"], ind["atr"]
    trades, i = [], 4
    while i < len(bars) - 1 and len(trades) < max_trades:
        d = 0
        if c[i] > tw[i] and c[i - 1] <= tw[i - 1] and e9[i] > e21[i]: d = 1
        if c[i] < tw[i] and c[i - 1] >= tw[i - 1] and e9[i] < e21[i]: d = -1
        if not d: i += 1; continue
        stop = c[i] - d * 1.5 * atr[i]; out = None
        for j in range(i + 1, len(bars)):
            if (d == 1 and bars[j][3] <= stop) or (d == -1 and bars[j][2] >= stop):
                out = (j, stop, "stop"); break
            if (d == 1 and c[j] < tw[j]) or (d == -1 and c[j] > tw[j]):
                out = (j, c[j], "twap"); break
        if out is None: out = (len(bars) - 1, c[-1], "close")
        trades.append(dict(dir=d, i_in=i, p_in=c[i], i_out=out[0], p_out=out[1], why=out[2]))
        i = out[0] + 1
    return trades


def strat_revert(bars, ind, max_trades=2):
    c, tw, atr, rsi = ind["c"], ind["twap"], ind["atr"], ind["rsi"]
    trades, i = [], 6
    while i < len(bars) - 1 and len(trades) < max_trades:
        d = 0
        if rsi[i] < 5 and c[i] < tw[i] - atr[i]: d = 1
        if rsi[i] > 95 and c[i] > tw[i] + atr[i]: d = -1
        if not d: i += 1; continue
        stop = c[i] - d * 1.5 * atr[i]; out = None
        for j in range(i + 1, min(i + 7, len(bars))):
            if (d == 1 and bars[j][3] <= stop) or (d == -1 and bars[j][2] >= stop):
                out = (j, stop, "stop"); break
            if (d == 1 and bars[j][2] >= tw[j]) or (d == -1 and bars[j][3] <= tw[j]):
                out = (j, tw[j], "target"); break
        if out is None:
            j = min(i + 6, len(bars) - 1); out = (j, c[j], "time")
        trades.append(dict(dir=d, i_in=i, p_in=c[i], i_out=out[0], p_out=out[1], why=out[2]))
        i = out[0] + 1
    return trades


STRAT_FN = {"ORB": strat_orb, "TREND": strat_trend, "REVERT": strat_revert}


# ---------------------------------------------------------------- option pricing
def ncdf(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(S, K, T, iv, call):
    if T <= 1e-9: return max(0.0, S - K) if call else max(0.0, K - S)
    sd = iv * math.sqrt(T); d1 = (math.log(S / K) + 0.5 * sd * sd) / sd; d2 = d1 - sd
    return S * ncdf(d1) - K * ncdf(d2) if call else K * ncdf(-d2) - S * ncdf(-d1)


def expiry_dt(t, rule):
    kind, wd, hm = rule
    h, m = map(int, hm.split(":"))
    e = t.replace(hour=h, minute=m, second=0, microsecond=0)
    if kind == "daily":
        return e if e > t else e + timedelta(days=1)
    while e.weekday() != wd or e <= t: e += timedelta(days=1)
    e = e.replace(hour=h, minute=m)
    return e


def years(a, b): return max((b - a).total_seconds(), 0) / (365 * 86400)




# ---------------------------------------------------------------- P&L per trade (for the full book capital)
def trade_pnl(book, cfg, tr, bars, iv):
    t_in, t_out = bars[tr["i_in"]][0], bars[tr["i_out"]][0]
    S0, S1, d = tr["p_in"], tr["p_out"], tr["dir"]
    if cfg["kind"] in ("fut", "stock"):
        notional = CAPITAL * cfg["lev"]
        return notional * d * (S1 / S0 - 1) - notional * cfg["cost"]
    # option buy: CE for long, PE for short, ATM strike
    if book == "USDINR_OPT": step = 0.25
    elif book == "BTC_OPT": step = 500
    else: step = 50 if "NIFTY" in book else 100
    K = max(step, round(S0 / step) * step)
    exp = expiry_dt(t_in, cfg["expiry"])
    call = d == 1
    if (exp - t_in).total_seconds() < MIN_HOURS_TO_EXPIRY * 3600:
        exp = expiry_dt(exp + timedelta(minutes=1), cfg["expiry"])      # too close: use next expiry
    p0 = bs(S0, K, years(t_in, exp), iv, call)
    p1 = bs(S1, K, years(t_out, exp), iv, call)
    if p0 < OPT_MIN_PREM[book]: return 0.0                               # skip: premium too small
    tick = OPT_TICK[book]
    buy = p0 * (1 + OPT_COST_FRAC) + tick
    sell = max(0.0, p1 * (1 - OPT_COST_FRAC) - tick)
    fixed = 0.0 if book == "BTC_OPT" else OPT_FIXED
    qty = (CAPITAL - 2 * fixed) / buy
    return qty * sell - CAPITAL


def run_book_day(book, cfg, day, data, iv):
    """Returns {strat: (pnl_rs_for_full_capital, [trade summaries])} or None if no session."""
    syms = cfg["sym"] if isinstance(cfg["sym"], list) else [cfg["sym"]]
    res = {s: [0.0, []] for s in STRATS}; any_bars = False
    for sym in syms:
        bars = day_bars(data.get(sym, []), day, cfg["win"])
        if len(bars) < 12: continue
        any_bars = True
        ind = indicators(bars)
        for s in STRATS:
            for tr in STRAT_FN[s](bars, ind):
                p = trade_pnl(book, cfg, tr, bars, iv) / len(syms)
                res[s][0] += p
                res[s][1].append(f"{sym.replace('.NS','')} {'LONG' if tr['dir']==1 else 'SHORT'} "
                                 f"{bars[tr['i_in']][0]:%H:%M}@{tr['p_in']:.4g} -> {bars[tr['i_out']][0]:%H:%M}@{tr['p_out']:.4g} "
                                 f"({tr['why']}) Rs {p:+.0f}")
    for st in STRATS: res[st][0] = max(res[st][0], -CAPITAL)
    return res if any_bars else None


# ---------------------------------------------------------------- learning
def weights_for(history):
    """history: list of per-day {strat: pnl} (oldest first, days BEFORE today). Returns {strat: w}."""
    cum = {s: sum(h.get(s, 0.0) for h in history) / CAPITAL for s in STRATS}
    raw = {s: math.exp(3 * cum[s]) for s in STRATS}
    for s in STRATS:   # sit out after 3 straight losing days
        last = [h.get(s, 0.0) for h in history[-3:]]
        if len(last) == 3 and all(x < 0 for x in last): raw[s] = 0.0
    tot = sum(raw.values())
    if tot == 0: return {s: 0.0 for s in STRATS}
    w = {s: raw[s] / tot for s in STRATS}
    active = [s for s in STRATS if raw[s] > 0]
    for s in active: w[s] = max(w[s], 0.10)
    tot = sum(w[s] for s in active)
    return {s: (w[s] / tot if s in active else 0.0) for s in STRATS}


# ---------------------------------------------------------------- daily driver
def load_ledger():
    try:
        with open(LEDGER) as f: return json.load(f)
    except Exception:
        return {"days": {}}


def save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f: json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def implied_vols(data, daily_btc, day):
    vix = [b for b in data.get("^INDIAVIX", []) if b[0].strftime("%Y-%m-%d") < day]
    iv_vix = 1.25 * ((vix[-1][4] / 100) if vix else 0.15)   # short-dated ATM IV usually above VIX: be conservative
    rv = 0.5
    closes = [c for d, c in daily_btc if d < day][-31:]
    if len(closes) > 10:
        r = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        m = sum(r) / len(r); rv = max(0.2, math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1)) * math.sqrt(365))
    return {"vix": iv_vix, "rv": rv}


def process_day(ledger, day, data, daily_btc):
    ivs = implied_vols(data, daily_btc, day)
    entry = {}
    for book, cfg in BOOKS.items():
        prev = [ledger["days"][d][book] for d in sorted(ledger["days"]) if d < day and book in ledger["days"][d]]
        if sum(p["pnl"] for p in prev) <= -CAPITAL * 0.95: continue          # money finished: book stopped
        hist = [p["by_strat"] for p in prev]
        w = weights_for(hist)
        iv = ivs[cfg["iv"]] if isinstance(cfg.get("iv"), str) else cfg.get("iv")
        res = run_book_day(book, cfg, day, data, iv)
        if res is None: continue
        by = {s: round(res[s][0], 1) for s in STRATS}
        pnl = max(sum(w[s] * res[s][0] for s in STRATS), -(CAPITAL + sum(p["pnl"] for p in prev)))  # can't lose money you no longer have
        entry[book] = dict(pnl=round(pnl, 1), weights={s: round(w[s], 2) for s in STRATS}, by_strat=by,
                           trades={s: res[s][1] for s in STRATS if res[s][1]})
    if entry: ledger["days"][day] = entry
    return entry


def lessons(ledger):
    out = []
    for book in BOOKS:
        days = [(d, ledger["days"][d][book]) for d in sorted(ledger["days"]) if book in ledger["days"][d]]
        if not days: continue
        d, e = days[-1]
        worst = min(STRATS, key=lambda s: e["by_strat"][s]); best = max(STRATS, key=lambda s: e["by_strat"][s])
        nxt = weights_for([x[1]["by_strat"] for x in days])
        out.append(f"- {book}: aaj {best} sabh ton vadiya ({e['by_strat'][best]:+.0f}), {worst} maada ({e['by_strat'][worst]:+.0f}). "
                   f"Kal da hissa: " + ", ".join(f"{s} {nxt[s]:.0%}" for s in STRATS))
    return out


def write_report(ledger):
    days = sorted(ledger["days"])
    lines = [f"# Paper trading hisaab ({START_DAY} ton {END_DAY})", "", "Har book: Rs 10,000 kaagzi. Asli paisa NAHI.", "",
             "| Book | Total P&L | Din | Plus din | Aaj |", "|---|---|---|---|---|"]
    grand = 0.0
    for book in BOOKS:
        vals = [(d, ledger["days"][d][book]["pnl"]) for d in days if book in ledger["days"][d]]
        if not vals: lines.append(f"| {book} | - | 0 | - | - |"); continue
        tot = sum(v for _, v in vals); grand += tot
        today = vals[-1][1] if vals[-1][0] == days[-1] else 0.0
        bust = " (PAISA KHATAM)" if tot <= -CAPITAL * 0.95 else ""
        lines.append(f"| {book}{bust} | Rs {tot:+,.0f} | {len(vals)} | {sum(1 for _, v in vals if v > 0)} | Rs {today:+,.0f} |")
    lines += ["", f"**Sare 9 book milake (Rs 90,000 kaagzi): Rs {grand:+,.0f}**", "", "## Seekh (aaj)", *lessons(ledger),
              "", "Note: options di keemat Black-Scholes andaza hai (asli option chain nahi). NIFTY_FUT asli 1 lot layi ~Rs 1.5-2 lakh margin chahida."]
    with open(REPORT, "w") as f: f.write("\n".join(lines) + "\n")
    return grand


def main():
    ledger = load_ledger()
    syms = sorted({s for c in BOOKS.values() for s in (c["sym"] if isinstance(c["sym"], list) else [c["sym"]])} | {"^INDIAVIX"})
    data = {s: yahoo(s) for s in syms}
    daily_btc = [(b[0].strftime("%Y-%m-%d"), b[4]) for b in yahoo("BTC-USD", "3mo", "1d")]
    now = datetime.now(timezone.utc).replace(tzinfo=None) + IST
    force = "--day" in sys.argv and sys.argv[sys.argv.index("--day") + 1]
    days = [force] if force else [(now - timedelta(days=k)).strftime("%Y-%m-%d") for k in (3, 2, 1, 0)]
    for day in days:
        if not force and (day < START_DAY or day > END_DAY or day in ledger["days"]): continue
        if day == now.strftime("%Y-%m-%d") and now.hour < 21 and not force: continue   # wait for crypto window to finish
        e = process_day(ledger, day, data, daily_btc)
        print(day, {b: v["pnl"] for b, v in e.items()})
    save(LEDGER, ledger)
    print(f"TOTAL Rs {write_report(ledger):+,.0f}")


if __name__ == "__main__":
    main()
