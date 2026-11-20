# Pump scanner (cloud, paper only)
Runs every ~10 min on GitHub Actions. No real money. Results: `pump_paper.json`.
Rs 400 virtual buy per alert, max 5 open; 2x -> sell half; <=0.6x or 24h -> exit; 1% fee/side + Rs 10.

# Paper trading (21-28 Nov 2026)
`paper/paperbook.py` replays each day's 5-min bars for 9 books (Rs 10,000 each): Nifty fut/opt, Sensex opt,
5 stocks intraday, USDINR fut/opt, BTC/ETH fut, BTC opt. 3 strategies (ORB, TREND, REVERT); capital shifts
toward what has worked. Daily summary: `paper/REPORT.md`. Paper only.
