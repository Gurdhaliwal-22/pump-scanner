import math, random, unittest
from datetime import datetime, timedelta
import paperbook as pb


def fake_bars(n=70, seed=1, start=datetime(2026, 11, 24, 9, 15), p=100.0):
    random.seed(seed); out = []
    for i in range(n):
        o = p; p = p * (1 + random.gauss(0, 0.002)); h = max(o, p) * 1.001; l = min(o, p) * 0.999
        out.append((start + timedelta(minutes=5 * i), o, h, l, p))
    return out


class T(unittest.TestCase):
    def test_bs_put_call_parity(self):
        S, K, T_, iv = 100, 100, 0.02, 0.2
        self.assertAlmostEqual(pb.bs(S, K, T_, iv, True) - pb.bs(S, K, T_, iv, False), S - K, places=6)
        self.assertEqual(pb.bs(110, 100, 0, 0.2, True), 10)

    def test_no_lookahead(self):
        bars = fake_bars()
        for name, fn in pb.STRAT_FN.items():
            full = fn(bars, pb.indicators(bars))
            for tr in full:   # same entry must appear when the future after entry is cut off
                cut = bars[: tr["i_in"] + 2]
                part = fn(cut, pb.indicators(cut))
                self.assertTrue(any(p["i_in"] == tr["i_in"] and p["dir"] == tr["dir"] for p in part), name)

    def test_weights(self):
        w = pb.weights_for([])
        self.assertAlmostEqual(sum(w.values()), 1); self.assertAlmostEqual(w["ORB"], 1 / 3)
        h = [{"ORB": 2000, "TREND": -500, "REVERT": 0}] * 2
        w = pb.weights_for(h); self.assertGreater(w["ORB"], w["REVERT"]); self.assertGreater(w["REVERT"], w["TREND"])
        self.assertGreaterEqual(min(w.values()), 0.0999)
        h = [{"ORB": -1, "TREND": 5, "REVERT": 5}] * 3
        self.assertEqual(pb.weights_for(h)["ORB"], 0.0)

    def test_option_loss_bounded(self):
        bars = fake_bars()
        cfg = pb.BOOKS["NIFTY_OPT"]
        for S1 in (50, 100, 200):
            tr = dict(dir=1, i_in=5, p_in=100.0, i_out=60, p_out=S1)
            b = [(t, o * 250, h * 250, l * 250, c * 250) for t, o, h, l, c in bars]
            tr["p_in"] *= 250; tr["p_out"] *= 250
            pnl = pb.trade_pnl("NIFTY_OPT", cfg, tr, b, 0.15)
            self.assertGreaterEqual(pnl, -pb.CAPITAL - 1e-6)

    def test_expiry(self):
        t = datetime(2026, 11, 24, 10, 0)   # Tuesday
        self.assertEqual(pb.expiry_dt(t, ("weekday", 1, "15:30")), datetime(2026, 11, 24, 15, 30))
        self.assertEqual(pb.expiry_dt(datetime(2026, 11, 24, 16, 0), ("weekday", 1, "15:30")), datetime(2026, 12, 1, 15, 30))
        self.assertEqual(pb.expiry_dt(datetime(2026, 11, 24, 18, 0), ("daily", None, "17:30")), datetime(2026, 11, 25, 17, 30))

    def test_process_day_and_bust(self):
        bars = fake_bars(start=datetime(2026, 11, 24, 9, 0), n=150)
        data = {s: bars for s in ["^NSEI", "^BSESN", "USDINR=X", "BTC-USD", "ETH-USD", *pb.STOCKS]}
        data["^INDIAVIX"] = [(datetime(2026, 11, 23, 15, 0), 15, 15, 15, 15)]
        L = {"days": {}}
        e = pb.process_day(L, "2026-11-24", data, [])
        self.assertIn("NIFTY_FUT", e)
        L = {"days": {"2026-11-23": {"NIFTY_FUT": {"pnl": -9800, "by_strat": {}, "weights": {}}}}}
        e = pb.process_day(L, "2026-11-24", data, [])
        self.assertNotIn("NIFTY_FUT", e)


if __name__ == "__main__":
    unittest.main()
