import unittest
import pump_scanner as ps

NOW = 1_800_000_000_000


def pair(age_min=60, liq=40000, mc=300000, vol=50000, buys=300, sells=150, h1=40, m5=2, mint="M1"):
    return {"pairCreatedAt": NOW - age_min * 60000, "liquidity": {"usd": liq}, "marketCap": mc,
            "volume": {"h1": vol}, "txns": {"h1": {"buys": buys, "sells": sells}},
            "priceChange": {"h1": h1, "m5": m5}, "baseToken": {"address": mint, "name": "Test", "symbol": "TST"},
            "url": "https://dexscreener.com/solana/x"}


class TestMarketCheck(unittest.TestCase):
    def ok(self, **kw): return ps.market_check(pair(**kw), NOW)[:2]

    def test_healthy_coin_passes(self): self.assertEqual(self.ok(), (True, "ok"))
    def test_too_young(self): self.assertEqual(self.ok(age_min=3)[1], "age")
    def test_too_old(self): self.assertEqual(self.ok(age_min=600)[1], "age")
    def test_thin_liquidity(self): self.assertEqual(self.ok(liq=5000)[1], "liquidity")
    def test_market_cap_range(self):
        self.assertEqual(self.ok(mc=10000)[1], "market cap"); self.assertEqual(self.ok(mc=9e6)[1], "market cap")
    def test_low_volume(self): self.assertEqual(self.ok(vol=2000)[1], "volume")
    def test_sellers_winning(self): self.assertEqual(self.ok(buys=100, sells=150)[1], "sellers winning")
    def test_parabolic(self): self.assertEqual(self.ok(h1=900)[1], "already parabolic")
    def test_dumping(self): self.assertEqual(self.ok(m5=-25)[1], "dumping now")
    def test_missing_fields_do_not_crash(self):
        self.assertFalse(ps.market_check({}, NOW)[0])
        self.assertFalse(ps.market_check({"pairCreatedAt": NOW - 3600000}, NOW)[0])


class TestRugVerdict(unittest.TestCase):
    def test_low_score_ok(self):
        self.assertEqual(ps.rug_verdict({"score_normalised": 5, "risks": []})[0], True)

    def test_high_score_rejected(self):
        self.assertFalse(ps.rug_verdict({"score_normalised": 80, "risks": []})[0])

    def test_danger_risk_rejected_even_with_low_score(self):
        self.assertFalse(ps.rug_verdict({"score_normalised": 10, "risks": [{"name": "Mint authority", "level": "danger"}]})[0])

    def test_missing_score_rejected(self):
        self.assertFalse(ps.rug_verdict({})[0])


class TestBestPair(unittest.TestCase):
    def test_picks_most_liquid_pair(self):
        a = pair(liq=10000, mint="X"); b = pair(liq=90000, mint="X")
        self.assertIs(ps.best_pair_per_token([a, b])["X"], b)


class TestScanOnce(unittest.TestCase):
    def test_flow_alerts_once_and_rechecks_young_coins(self):
        sent = []
        ps.candidate_mints = lambda: ["GOOD", "YOUNG", "RUG"]
        ps.pairs_for = lambda m: [pair(mint="GOOD"), pair(mint="YOUNG", age_min=2), pair(mint="RUG")]
        ps.rug_check = lambda mint: (mint != "RUG", 5, [])
        ps.alert = lambda cfg, text: sent.append(text)
        import time as _t
        real = _t.time; _t.time = lambda: NOW / 1000
        try:
            seen = set()
            ps.scan_once({}, seen)
            self.assertEqual(len(sent), 1); self.assertIn("Test", sent[0])
            self.assertIn("GOOD", seen); self.assertIn("RUG", seen); self.assertNotIn("YOUNG", seen)
            ps.scan_once({}, seen)                      # second pass: nothing new
            self.assertEqual(len(sent), 1)
        finally:
            _t.time = real


class TestPaper(unittest.TestCase):
    def fresh(self): return {"cash": 2000.0, "open": {}, "closed": [], "last_summary_day": ""}

    def test_open_limits(self):
        pp = self.fresh()
        for i in range(7): ps.paper_open(pp, f"M{i}", "X", 1.0, 0)
        self.assertEqual(len(pp["open"]), 5); self.assertAlmostEqual(pp["cash"], 0.0)

    def test_stop_loses_about_42_percent(self):
        pp = self.fresh(); ps.paper_open(pp, "A", "A", 1.0, 0); ps.paper_step(pp, "A", 0.6, 60)
        self.assertEqual(len(pp["closed"]), 1)
        self.assertTrue(-190 < pp["closed"][0]["pnl"] < -170)

    def test_2x_sells_half_then_24h_exit(self):
        pp = self.fresh(); ps.paper_open(pp, "A", "A", 1.0, 0)
        ps.paper_step(pp, "A", 2.0, 600)
        self.assertTrue(pp["open"]["A"]["half_sold"]); self.assertGreater(pp["open"]["A"]["realised"], 350)
        ps.paper_step(pp, "A", 2.0, 24 * 3600 + 1)
        self.assertNotIn("A", pp["open"]); self.assertGreater(pp["closed"][0]["pnl"], 330)

    def test_rug_to_zero(self):
        pp = self.fresh(); ps.paper_open(pp, "A", "A", 1.0, 0); ps.paper_step(pp, "A", 0.0001, 60)
        self.assertLess(pp["closed"][0]["pnl"], -390)

    def test_new_day_tops_up_to_2000(self):
        pp = self.fresh(); pp["cash"] = 140.0
        self.assertTrue(ps.paper_new_day(pp, "2026-11-23"))
        self.assertAlmostEqual(pp["cash"], 2000.0); self.assertAlmostEqual(pp["added"], 1860.0)
        self.assertFalse(ps.paper_new_day(pp, "2026-11-23"))      # same day: nothing
        pp["cash"] = 2500.0; ps.paper_new_day(pp, "2026-11-24")    # winning: no top-up
        self.assertAlmostEqual(pp["cash"], 2500.0); self.assertAlmostEqual(pp["added"], 1860.0)

    def test_missing_price_does_nothing(self):
        pp = self.fresh(); ps.paper_open(pp, "A", "A", 1.0, 0); ps.paper_step(pp, "A", None, 60)
        self.assertIn("A", pp["open"])


if __name__ == "__main__":
    unittest.main()
