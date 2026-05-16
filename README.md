# NQ Intraday Level Generator

Pre-market level sheet for scalping NQ futures. Run 30–60 minutes before the open each day. Generates a chart + terminal report with all five layer types overlaid.

**No paid data feeds required.** Uses `yfinance` (free) as data source.

---

## Layers calculated

| Layer | Source | Levels |
|---|---|---|
| Previous day/week | Daily OHLC | PDH, PDL, PDS, PWH, PWL |
| Volume Profile | Prior session 5-min | POC, VAH, VAL, HVNs |
| VWAP bands | Session intraday | VWAP, ±1σ, ±2σ, weekly VWAP |
| Options / GEX | Options chain (nearest weekly) | Call wall, put wall, GEX flip, max pain |
| Delta bias | OHLCV approximation | CVD, session bias, imbalance levels |

---

## Setup

```bash
git clone https://github.com/YOUR_USERNAME/nq-intraday-levels
cd nq-intraday-levels
pip install -r requirements.txt
```

Python 3.8+ required.

---

## Usage

```bash
# Default: QQQ as NQ proxy (×42), live data
python intraday_levels.py

# NQ futures direct (if your broker/feed exposes NQ=F)
python intraday_levels.py --ticker NQ=F

# Demo mode — no internet, synthetic data, test the output
python intraday_levels.py --demo

# Terminal report only, no chart
python intraday_levels.py --no-chart

# Skip JSON export
python intraday_levels.py --export none
```

---

## Output

### Terminal report
```
══════════════════════════════════════════════════════════════
  NQ INTRADAY LEVEL SHEET
  Tuesday 16 May 2025  08:45
  Data: QQQ  (×42 → NQ)
══════════════════════════════════════════════════════════════

  PREVIOUS DAY / WEEK ──────────────────────────
  Prev Day High  (PDH)          20,244.0
  Prev Day Low   (PDL)          19,988.0
  Prev Day Settle(PDS)          20,118.0
  Prev Week High (PWH)          20,440.0
  Prev Week Low  (PWL)          19,720.0

  VOLUME PROFILE  (prev session) ───────────────
  Value Area High (VAH)         20,195.0
  Point of Control(POC)  ★      20,076.0
  Value Area Low  (VAL)         19,944.0

  VWAP BANDS ────────────────────────────────────
  VWAP +2σ                      20,310.0
  VWAP +1σ                      20,214.0
  VWAP      ★                   20,118.0
  VWAP −1σ                      20,022.0
  VWAP −2σ                      19,926.0

  OPTIONS / GEX LEVELS ──────────────────────────  expiry 2025-05-17
  Call Wall   (resistance)      20,500.0
  GEX Flip    (bull/bear line) ★ 20,100.0
  Max Pain    (Fri magnet)      20,150.0
  Put Wall    (support)         19,750.0

  DELTA / ORDER FLOW (approx) ───────────────────
  Session bias:            Mild buying
  ▲ BUY  imbalance @ 09:45       20,134.0
  ▼ SELL imbalance @ 14:15       20,022.0
```

### Chart (`charts/` folder)
Three-panel PNG:
- Left: candlestick price bars with all level lines overlaid + labels
- Right: volume profile histogram (previous session)
- Bottom: CVD approximation with session bias

### JSON (`data/nq_levels_latest.json`)
All levels in machine-readable format — pipe into alerts, dashboards, or your own scripts.

---

## How to use the levels for scalping

### Confluence = conviction
Levels where 2+ layer types coincide are your highest-conviction zones. For example:
- PDH aligns with VAH and VWAP +1σ → very strong resistance
- POC aligns with VWAP → magnetic zone, expect chop or clean rejection

### Layer-by-layer playbook

**Previous day/week levels**
- PDH/PWH = first overhead resistance. Break + hold = trend day up.
- PDL/PWL = first support. Break + hold = trend day down.
- PDS = mean reversion target if price is extended either direction.

**Volume Profile**
- POC = the "fair price" magnet. Price gravitates here in balanced markets.
- Inside VAL–VAH = expect two-way, mean-reverting price action.
- Outside value area = directional. Entering value = likely reverting to POC.

**VWAP**
- Price above VWAP = long bias. Fade shorts to VWAP, hold longs.
- Price below VWAP = short bias. Fade longs to VWAP, hold shorts.
- ±1σ = first target. ±2σ = overextended, high-probability fade.
- Weekly VWAP crossing = significant structural shift.

**Options / GEX**
- Call wall = MMs selling NQ futures as price approaches → resistance.
- Put wall = MMs buying NQ futures as price drops → support.
- GEX flip = the bull/bear dividing line for the week. Above = positive gamma (chop), below = negative gamma (trend/accelerate).
- Max pain = gravitational pull into Friday close.

**Delta / CVD**
- Positive CVD = buyers absorbing offers. Lean long on pullbacks.
- Negative CVD = sellers hitting bids. Lean short on bounces.
- Imbalance levels = price points where aggressive one-sided flow hit. Often act as support/resistance on revisit.

### Best setups
1. **Failed breakout of PDH/PWH with negative delta** → short back to VWAP
2. **Acceptance above VAH with positive CVD** → long to call wall
3. **POC + VWAP confluence hold** → mean reversion trade both ways
4. **GEX flip reclaim with volume** → directional trade with momentum
5. **VWAP ±2σ touch with delta divergence** → fade to VWAP

---

## Known limitations

**Delta / order flow is approximate**
True delta (bid vs ask aggressor volume) requires tick-by-tick data:
- [Rithmic](https://rithmic.com) — best for NQ, used by Sierra Chart / Bookmap
- [Databento](https://databento.com) — pay-per-GB tick data, good API
- [Tradovate](https://tradovate.com) — built-in order flow tools
- [IQFeed](https://iqfeed.net) — low-latency tick feed

This tool's CVD uses close-position-within-bar as a proxy — directionally useful but not a replacement for real tape reading.

**Options / GEX is next expiry only**
Real GEX aggregates across all expiries simultaneously. For full GEX:
- [Spot Gamma](https://spotgamma.com) — industry standard
- [Tradytics](https://tradytics.com) — cheaper alternative

**yfinance data quality**
- 5-min intraday data available for last 5 days only
- May have gaps around market open/close
- NQ=F futures data is less reliable than QQQ in yfinance — QQQ ×42 is the more stable proxy

---

## Automating daily runs

```bash
# Run at 8:45 AM ET every weekday (cron)
45 8 * * 1-5 cd /path/to/nq-intraday-levels && python intraday_levels.py >> logs/levels.log 2>&1
```

Or with Python scheduler:
```python
import schedule, time, subprocess
schedule.every().monday.at("08:45").do(lambda: subprocess.run(["python", "intraday_levels.py"]))
# repeat for tue–fri
while True:
    schedule.run_pending()
    time.sleep(60)
```

---

## Extending the tool

The `data/nq_levels_latest.json` output is designed to be consumed by other tools:

```python
import json
with open("data/nq_levels_latest.json") as f:
    levels = json.load(f)

vwap     = levels["vwap"]["VWAP"]
poc      = levels["volume_profile"]["POC"]
gex_flip = levels["gex"]["gex_flip"]
```

Ideas for extension:
- Add NQ price feed (Alpaca, Polygon.io) for real-time level alerts
- Push level sheet to Slack/Discord webhook each morning
- Overlay on TradingView via Pine Script using the JSON output
- Add ES/SPY correlation levels for confluence confirmation

---

## Related tools in this repo

- `../nq-cta-tracker/` — weekly CTA positioning tracker using CFTC COT data

---

## License

MIT
