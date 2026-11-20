"""
pump_scanner.py -- Solana / Pump.fun new-coin scanner. Runs on YOUR PC, checks every minute.

It does NOT try to snipe the first seconds (bots win that race). It looks for coins that
SURVIVED their first 5 minutes and still look healthy, and throws out the usual rug signs:
  * age 5 min - 6 h, liquidity >= $15k, market cap $40k - $3M
  * at least $15k traded in the last hour, more buys than sells (ratio >= 1.2)
  * not already gone parabolic (+300% in 1h) and not dumping right now (5-min change > -10%)
  * RugCheck risk score <= 40 (0 = safe, 100 = risky) and no "danger" level risks
Alerts: console + beep + popup + Telegram (same watch_config.json as live_watch, if present).
Data: DexScreener public API (no key) + RugCheck public API (no key).
NEVER buys anything -- you buy in Phantom yourself, small size.
Run: python pump_scanner.py
"""
import json, os, sys, time
from datetime import datetime, timedelta, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
SEEN = os.path.join(HERE, "pump_seen.json")
CFG_CANDIDATES = [os.path.join(HERE, "watch_config.json"), os.path.join(HERE, "..", "watch_config.json")]
DEX = "https://api.dexscreener.com"
RUG = "https://api.rugcheck.xyz/v1"
H = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

RULES = dict(min_age_min=5, max_age_min=360, min_liq=15000, min_mc=40000, max_mc=3000000,
             min_vol_h1=15000, min_buy_ratio=1.2, max_h1_change=300, min_m5_change=-10,
             max_rug_score=40)


def load_cfg():
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        return {"telegram_bot_token": os.environ["TELEGRAM_BOT_TOKEN"], "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", "")}
    for p in CFG_CANDIDATES:
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8-sig") as f: return json.load(f)
            except Exception:
                print("watch_config.json padhya nahi gaya -- Telegram band, scanner chalu.")
    return {}


def load_seen():
    try:
        with open(SEEN) as f: return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen):
    tmp = SEEN + ".tmp"
    with open(tmp, "w") as f: json.dump(sorted(seen)[-5000:], f)
    os.replace(tmp, SEEN)


def get_json(url):
    r = requests.get(url, headers=H, timeout=20)
    if r.status_code == 429:
        time.sleep(5); r = requests.get(url, headers=H, timeout=20)
    r.raise_for_status()
    return r.json()


def candidate_mints():
    """Newest Solana tokens that got a DexScreener profile or boost (dev spent money on it)."""
    mints = []
    for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1"):
        try:
            for t in get_json(DEX + path) or []:
                if t.get("chainId") == "solana" and t.get("tokenAddress"):
                    mints.append(t["tokenAddress"])
        except Exception as e:
            print(f"  dexscreener {path} error: {e}")
    return list(dict.fromkeys(mints))


def pairs_for(mints):
    out = []
    for i in range(0, len(mints), 30):
        try:
            out += get_json(f"{DEX}/tokens/v1/solana/{','.join(mints[i:i + 30])}") or []
        except Exception as e:
            print(f"  dexscreener pairs error: {e}")
    return out


def best_pair_per_token(pairs):
    best = {}
    for p in pairs:
        mint = (p.get("baseToken") or {}).get("address")
        if not mint: continue
        liq = ((p.get("liquidity") or {}).get("usd")) or 0
        if mint not in best or liq > ((best[mint].get("liquidity") or {}).get("usd") or 0):
            best[mint] = p
    return best


def market_check(p, now_ms=None, rules=RULES):
    """Pure function (unit-tested). Returns (ok, reason, facts)."""
    now_ms = now_ms or int(time.time() * 1000)
    created = p.get("pairCreatedAt")
    if not created: return False, "no age", {}
    age_min = (now_ms - created) / 60000
    liq = float((p.get("liquidity") or {}).get("usd") or 0)
    mc = float(p.get("marketCap") or p.get("fdv") or 0)
    vol_h1 = float((p.get("volume") or {}).get("h1") or 0)
    tx = (p.get("txns") or {}).get("h1") or {}
    buys, sells = int(tx.get("buys") or 0), int(tx.get("sells") or 0)
    pc = p.get("priceChange") or {}
    h1, m5 = float(pc.get("h1") or 0), float(pc.get("m5") or 0)
    facts = dict(age_min=age_min, liq=liq, mc=mc, vol_h1=vol_h1, buys=buys, sells=sells, h1=h1, m5=m5)
    if not rules["min_age_min"] <= age_min <= rules["max_age_min"]: return False, "age", facts
    if liq < rules["min_liq"]: return False, "liquidity", facts
    if not rules["min_mc"] <= mc <= rules["max_mc"]: return False, "market cap", facts
    if vol_h1 < rules["min_vol_h1"]: return False, "volume", facts
    if buys < rules["min_buy_ratio"] * max(sells, 1): return False, "sellers winning", facts
    if h1 > rules["max_h1_change"]: return False, "already parabolic", facts
    if m5 < rules["min_m5_change"]: return False, "dumping now", facts
    return True, "ok", facts


def rug_check(mint, rules=RULES):
    """Returns (ok, score, top_risks). Unknown/failed check = NOT ok (be conservative)."""
    try:
        s = get_json(f"{RUG}/tokens/{mint}/report/summary")
    except Exception as e:
        return False, None, [f"rugcheck failed: {e}"]
    return rug_verdict(s, rules)


def rug_verdict(s, rules=RULES):
    """Pure function (unit-tested) on a RugCheck summary JSON."""
    score = s.get("score_normalised")
    risks = s.get("risks") or []
    danger = [r.get("name", "?") for r in risks if str(r.get("level", "")).lower() == "danger"]
    names = [f"{r.get('name', '?')} ({r.get('level', '?')})" for r in risks][:4]
    if score is None: return False, None, names or ["no score"]
    if danger: return False, score, names
    return score <= rules["max_rug_score"], score, names


def alert(cfg, text):
    print("\n" + "=" * 64 + "\n" + text + "\n" + "=" * 64, flush=True)
    try:
        import winsound
        for _ in range(2): winsound.Beep(1500, 300)
    except Exception:
        print("\a")
    try:
        import ctypes, threading
        threading.Thread(target=ctypes.windll.user32.MessageBoxW, args=(0, text, "NEW COIN", 0x40000), daemon=True).start()
    except Exception:
        pass
    if cfg.get("telegram_bot_token") and cfg.get("telegram_chat_id"):
        try:
            requests.post(f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/sendMessage",
                          data={"chat_id": cfg["telegram_chat_id"], "text": text, "disable_web_page_preview": True}, timeout=20)
        except Exception as e:
            print("telegram failed:", e)


def message(p, f, score, risks):
    bt = p.get("baseToken") or {}; mint = bt.get("address", "")
    return (f"PUMP SCANNER: {bt.get('name', '?')} (${bt.get('symbol', '?')})\n"
            f"Age {f['age_min']:.0f} min | MC ${f['mc']:,.0f} | Liq ${f['liq']:,.0f}\n"
            f"1h: {f['h1']:+.0f}%  vol ${f['vol_h1']:,.0f}  buys {f['buys']} / sells {f['sells']}\n"
            f"RugCheck score {score} (0=safe). {('Risks: ' + '; '.join(risks)) if risks else 'No risks listed'}\n"
            f"Chart: {p.get('url', '')}\n"
            f"Pump.fun: https://pump.fun/coin/{mint}\n"
            f"Plan: max Rs 200-300 is coin te. 2x te adha vech. -40% te bahar.\n"
            f"98% naven coin doob jaande ne -- eh lottery hai, salah nahi.")


def scan_once(cfg, seen, paper=None):
    mints = [m for m in candidate_mints() if m not in seen]
    found = 0
    wanted = set(mints)
    for mint, p in best_pair_per_token(pairs_for(mints)).items():
        # a pair can list some OTHER token as base (our coin as quote) -- only judge coins we asked about
        if mint not in wanted or mint in seen: continue
        ok, why, facts = market_check(p)
        if why in ("age",) and facts.get("age_min", 0) < RULES["min_age_min"]:
            continue                      # too young: look again next minute
        seen.add(mint)                    # decided (pass or fail) -> don't re-check
        if not ok: continue
        rok, score, risks = rug_check(mint)
        if not rok: continue
        alert(cfg, message(p, facts, score, risks)); found += 1
        if paper is not None:
            try: price = float(p.get("priceUsd") or 0)
            except Exception: price = 0
            paper_open(paper, mint, (p.get("baseToken") or {}).get("symbol", "?"), price, time.time())
    return len(mints), found


def main():
    cfg = load_cfg(); seen = load_seen(); paper = load_paper()
    if "--once" in sys.argv:          # cloud mode (GitHub Actions): one pass, save, exit
        one_pass(cfg, seen, paper); return
    print(f"Pump scanner chalu {ist_now():%H:%M}. Har minute check. Band karan layi window band kar.", flush=True)
    while True:
        try:
            one_pass(cfg, seen, paper)
        except Exception as e:
            print(f"{ist_now():%H:%M}  error: {e}", flush=True)
        time.sleep(60)


def ist_now():
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=5, minutes=30)


def one_pass(cfg, seen, paper):
    n, found = scan_once(cfg, seen, paper); save_seen(seen)
    prices = paper_tick(paper); save_paper(paper)
    line = paper_summary(paper, prices)
    print(f"{ist_now():%H:%M}  checked {n} new coins, alerts {found} | {line}", flush=True)
    today = ist_now().strftime("%Y-%m-%d")
    if ist_now().hour >= 21 and paper.get("last_summary_day") != today:
        paper["last_summary_day"] = today; save_paper(paper)
        if cfg.get("telegram_bot_token") and cfg.get("telegram_chat_id"):
            try:
                requests.post(f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/sendMessage",
                              data={"chat_id": cfg["telegram_chat_id"], "text": "Pump scanner roz da hisaab: " + line}, timeout=20)
            except Exception: pass


# ---------------------------------------------------------------------------
# PAPER TRACKER: what would Rs 2,000 have made following every alert?
# Each alert = Rs 400 "bought" at the alert price (max 5 open, like a real Rs 2,000 pot).
# Every minute: price >= 2x entry -> sell half (once); price <= 0.6x entry -> sell the rest;
# after 24 h -> sell the rest at market. 1% fee each side + Rs 10 network per trade.
# Results in pump_paper.json; a summary line prints every minute and goes to Telegram once a day.
# ---------------------------------------------------------------------------
PAPER = os.path.join(HERE, "pump_paper.json")
STAKE, MAX_OPEN, FEE, NET_RS = 400.0, 5, 0.01, 10.0


def load_paper():
    try:
        with open(PAPER) as f: return json.load(f)
    except Exception:
        return {"cash": 2000.0, "open": {}, "closed": [], "last_summary_day": ""}


def save_paper(pp):
    tmp = PAPER + ".tmp"
    with open(tmp, "w") as f: json.dump(pp, f, indent=1)
    os.replace(tmp, PAPER)


def paper_open(pp, mint, name, price, now):
    """Pure-ish (unit-tested). Returns True if a position was opened."""
    if mint in pp["open"] or len(pp["open"]) >= MAX_OPEN or price <= 0 or pp["cash"] < STAKE: return False
    pp["cash"] -= STAKE
    rs_in = STAKE * (1 - FEE) - NET_RS
    pp["open"][mint] = {"name": name, "entry": price, "t0": now, "rs_left": rs_in, "half_sold": False, "realised": 0.0}
    return True


def paper_step(pp, mint, price, now):
    """Apply exit rules to one open position at the current price. Pure-ish (unit-tested)."""
    p = pp["open"].get(mint)
    if not p or price is None or price <= 0: return
    mult = price / p["entry"]
    def sell(frac):
        value = p["rs_left"] * frac * mult
        p["realised"] += value * (1 - FEE) - NET_RS
        p["rs_left"] *= (1 - frac)
    if not p["half_sold"] and mult >= 2.0:
        sell(0.5); p["half_sold"] = True
        p["entry_after_half"] = price
    done = mult <= 0.6 or (now - p["t0"]) >= 24 * 3600
    if done:
        sell(1.0)
        pp["cash"] += p["realised"]
        pp["closed"].append({"mint": mint, "name": p["name"], "pnl": round(p["realised"] - STAKE, 1),
                             "held_h": round((now - p["t0"]) / 3600, 1)})
        del pp["open"][mint]


def paper_value(pp, prices):
    open_val = sum(p["realised"] + p["rs_left"] * prices.get(m, p["entry"]) / p["entry"] for m, p in pp["open"].items())
    return pp["cash"] + open_val


def paper_tick(pp):
    mints = list(pp["open"])
    if not mints: return {}
    prices = {}
    for mint, p in best_pair_per_token(pairs_for(mints)).items():
        try: prices[mint] = float(p.get("priceUsd") or 0)
        except Exception: pass
    now = time.time()
    for m in mints: paper_step(pp, m, prices.get(m), now)
    return prices


def paper_summary(pp, prices):
    v = paper_value(pp, prices); c = pp["closed"]
    wins = sum(1 for t in c if t["pnl"] > 0)
    return (f"PAPER Rs 2,000 -> Rs {v:,.0f} ({v - 2000:+,.0f}) | band trades {len(c)}, jitte {wins} | khulle {len(pp['open'])}")


if __name__ == "__main__":
    main()
