"""
NQ Intraday Levels Generator
==============================
Generates a complete pre-market level sheet for scalping NQ futures.
Run each morning 30–60 minutes before the open.

Layers calculated:
  1. Previous day/week highs, lows, settlements (PDH, PDL, PDS, PWH, PWL)
  2. VWAP + 1σ / 2σ bands (from prior session intraday data)
  3. Volume Profile — POC, VAH, VAL, High Volume Nodes (HVNs)
  4. Options-derived GEX levels — call wall, put wall, gamma flip, max pain
  5. Delta imbalance approximation — directional bias at each key level

Data: yfinance (QQQ proxy for NQ, NQ=F for futures)
      NQ point conversion: QQQ price × 42 ≈ NQ

Usage:
    python intraday_levels.py                  # live data, QQQ proxy
    python intraday_levels.py --ticker NQ=F    # NQ futures direct
    python intraday_levels.py --demo           # synthetic data, no internet
    python intraday_levels.py --export pdf     # also save PDF report
"""

import argparse
import json
import sys
import warnings
from datetime import datetime, timedelta, time
from pathlib import Path

warnings.filterwarnings("ignore")

MISSING = []
for pkg in ["yfinance", "pandas", "matplotlib", "numpy", "scipy"]:
    try:
        __import__(pkg)
    except ImportError:
        MISSING.append(pkg)

if MISSING:
    print(f"Missing: pip install {' '.join(MISSING)}")
    sys.exit(1)

import yfinance as yf
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.gridspec import GridSpec
from scipy.ndimage import gaussian_filter1d

# ── config ────────────────────────────────────────────────────────────────────

CHART_DIR = Path("charts")
DATA_DIR  = Path("data")
CHART_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

QQQ_TO_NQ = 42.0       # approximate QQQ→NQ conversion multiplier
VAH_PCT   = 0.70       # value area = 70% of volume
HVN_TOP_N = 4          # number of high volume nodes to mark

# Colours
C_BG     = "#FAFAF8"
C_GRID   = "#ECEAE2"
C_TEXT   = "#2C2C2A"
C_MUTED  = "#888780"
C_BULL   = "#639922"
C_BEAR   = "#E24B4A"
C_NEUT   = "#888780"
C_VWAP   = "#185FA5"
C_VOL    = "#7F77DD"
C_OPT    = "#BA7517"
C_PREV   = "#5DCAA5"
C_DELTA  = "#D85A30"


# ── data fetching ─────────────────────────────────────────────────────────────

def fetch_data(ticker: str = "QQQ", demo: bool = False) -> dict:
    """Fetch all required data and return as a dict of DataFrames."""
    if demo:
        return _generate_demo_data(ticker)

    print(f"  Fetching data for {ticker}...")
    tk = yf.Ticker(ticker)

    # Daily bars — last 10 sessions for prev day/week levels
    daily = tk.history(period="15d", interval="1d", auto_adjust=True)
    if daily.empty:
        print(f"  ERROR: Could not fetch daily data for {ticker}.")
        print("  Try --demo to test without internet, or check your ticker.")
        sys.exit(1)

    # Intraday 5-min bars — last 5 sessions
    intra = tk.history(period="5d", interval="5m", auto_adjust=True)

    # Options chain — nearest weekly expiry
    options = _fetch_options(tk, ticker)

    is_futures = "=F" in ticker
    multiplier = 1.0 if is_futures else QQQ_TO_NQ

    return {
        "ticker"    : ticker,
        "daily"     : daily,
        "intraday"  : intra,
        "options"   : options,
        "multiplier": multiplier,
        "is_futures": is_futures,
        "demo"      : False,
    }


def _fetch_options(tk, ticker: str) -> dict:
    """Fetch nearest weekly expiry options chain."""
    try:
        expiries = tk.options
        if not expiries:
            return {}
        # Pick soonest expiry that's at least 1 day away
        today = datetime.now().date()
        future = [e for e in expiries
                  if datetime.strptime(e, "%Y-%m-%d").date() > today]
        if not future:
            return {}
        chain  = tk.option_chain(future[0])
        return {
            "expiry": future[0],
            "calls" : chain.calls,
            "puts"  : chain.puts,
        }
    except Exception as e:
        print(f"  Warning: Could not fetch options data ({e}). Skipping GEX layer.")
        return {}


# ── level calculations ────────────────────────────────────────────────────────

def calc_prev_levels(daily: pd.DataFrame, multiplier: float) -> dict:
    """Previous day and previous week OHLC levels."""
    d = daily.copy()
    d.index = pd.to_datetime(d.index)

    # Previous day (most recent completed session)
    prev = d.iloc[-2] if len(d) >= 2 else d.iloc[-1]
    pdh  = float(prev["High"])
    pdl  = float(prev["Low"])
    pds  = float(prev["Close"])
    pdo  = float(prev["Open"])

    # Previous week (last full Mon–Fri week)
    d["week"] = d.index.isocalendar().week
    this_wk   = d.index[-1].isocalendar().week
    last_wk   = d[d["week"] != this_wk]
    if not last_wk.empty:
        pwh = float(last_wk["High"].max())
        pwl = float(last_wk["Low"].min())
        pws = float(last_wk["Close"].iloc[-1])
    else:
        pwh = pdh * 1.005
        pwl = pdl * 0.995
        pws = pds

    # Overnight / globex range (approximation: today's pre-market if available)
    today = d.iloc[-1]
    onh   = float(today["High"])
    onl   = float(today["Low"])

    return {
        "PDH": pdh * multiplier,
        "PDL": pdl * multiplier,
        "PDS": pds * multiplier,
        "PDO": pdo * multiplier,
        "PWH": pwh * multiplier,
        "PWL": pwl * multiplier,
        "PWS": pws * multiplier,
        "ONH": onh * multiplier,
        "ONL": onl * multiplier,
    }


def calc_vwap_levels(intraday: pd.DataFrame, multiplier: float) -> dict:
    """
    VWAP and standard deviation bands from the most recent session.
    Also calculates anchored VWAP from the weekly open.
    """
    df = intraday.copy()
    df.index = pd.to_datetime(df.index)

    # Isolate most recent trading session (today or last session)
    last_date = df.index[-1].date()
    session   = df[df.index.date == last_date].copy()

    if len(session) < 5:
        # Fall back to previous session
        dates  = sorted(pd.Series(df.index.date).unique())
        if len(dates) >= 2:
            session = df[df.index.date == dates[-2]].copy()

    if session.empty:
        return {}

    tp   = (session["High"] + session["Low"] + session["Close"]) / 3
    vol  = session["Volume"].replace(0, 1)
    cvol = vol.cumsum()
    ctpv = (tp * vol).cumsum()

    vwap      = ctpv / cvol
    deviation = ((((tp - vwap) ** 2 * vol).cumsum()) / cvol) ** 0.5

    current_vwap = float(vwap.iloc[-1])
    current_std  = float(deviation.iloc[-1])

    # Weekly anchored VWAP — from Monday's open of this week
    week_start = df[df.index.isocalendar().week == df.index[-1].isocalendar().week]
    if not week_start.empty:
        wtp   = (week_start["High"] + week_start["Low"] + week_start["Close"]) / 3
        wvol  = week_start["Volume"].replace(0, 1)
        wvwap = (wtp * wvol).cumsum() / wvol.cumsum()
        weekly_vwap = float(wvwap.iloc[-1])
    else:
        weekly_vwap = current_vwap

    m = multiplier
    return {
        "VWAP"      : current_vwap * m,
        "VWAP_1U"   : (current_vwap + current_std) * m,
        "VWAP_1D"   : (current_vwap - current_std) * m,
        "VWAP_2U"   : (current_vwap + 2 * current_std) * m,
        "VWAP_2D"   : (current_vwap - 2 * current_std) * m,
        "WVWAP"     : weekly_vwap * m,
        "std"       : current_std * m,
        "session_df": session,
    }


def calc_volume_profile(intraday: pd.DataFrame, multiplier: float,
                        bins: int = 120) -> dict:
    """
    Volume Profile: POC, VAH, VAL, and top High Volume Nodes.
    Uses the prior full session's 5-min bars.
    """
    df = intraday.copy()
    df.index = pd.to_datetime(df.index)
    dates = sorted(pd.Series(df.index.date).unique())
    if len(dates) < 2:
        return {}
    prev_date = dates[-2]
    session   = df[df.index.date == prev_date].copy()
    if session.empty or len(session) < 10:
        return {}

    lo    = session["Low"].min()
    hi    = session["High"].max()
    edges = np.linspace(lo, hi, bins + 1)
    mids  = (edges[:-1] + edges[1:]) / 2

    vol_profile = np.zeros(bins)
    for _, row in session.iterrows():
        touched = (mids >= row["Low"]) & (mids <= row["High"])
        n_bins  = touched.sum()
        if n_bins > 0:
            vol_profile[touched] += row["Volume"] / n_bins

    # Smooth slightly for cleaner HVN detection
    smooth = gaussian_filter1d(vol_profile, sigma=1.5)

    poc_idx = int(np.argmax(smooth))
    poc     = float(mids[poc_idx])

    # Value area (70% of total volume centred on POC)
    total_vol    = smooth.sum()
    target_vol   = total_vol * VAH_PCT
    included_vol = smooth[poc_idx]
    lo_idx = hi_idx = poc_idx

    while included_vol < target_vol:
        add_lo = smooth[lo_idx - 1] if lo_idx > 0 else 0
        add_hi = smooth[hi_idx + 1] if hi_idx < bins - 1 else 0
        if add_lo >= add_hi and lo_idx > 0:
            lo_idx -= 1
            included_vol += smooth[lo_idx]
        elif hi_idx < bins - 1:
            hi_idx += 1
            included_vol += smooth[hi_idx]
        else:
            break

    vah = float(mids[hi_idx])
    val = float(mids[lo_idx])

    # High Volume Nodes — local maxima above 70th percentile
    threshold = np.percentile(smooth, 70)
    hvns      = []
    for i in range(1, bins - 1):
        if (smooth[i] > smooth[i-1] and smooth[i] > smooth[i+1]
                and smooth[i] > threshold and mids[i] not in [poc]):
            hvns.append((float(mids[i]), float(smooth[i])))
    hvns = sorted(hvns, key=lambda x: -x[1])[:HVN_TOP_N]

    m = multiplier
    return {
        "POC"        : poc * m,
        "VAH"        : vah * m,
        "VAL"        : val * m,
        "HVNs"       : [(p * m, v) for p, v in hvns],
        "profile_mids": mids * m,
        "profile_vol" : smooth,
        "lo"          : lo * m,
        "hi"          : hi * m,
    }


def calc_gex_levels(options: dict, spot: float, multiplier: float) -> dict:
    """
    Gamma Exposure levels from options chain.
    GEX ≈ Gamma × OI × 100 × Spot  (sign: calls positive, puts negative)
    We use OI as gamma proxy (actual gamma requires BS, but OI gives good walls).
    """
    if not options or "calls" not in options:
        return {}

    calls = options["calls"].copy()
    puts  = options["puts"].copy()

    # Filter to strikes within ±8% of spot
    lo = spot * 0.92
    hi = spot * 1.08
    calls = calls[(calls["strike"] >= lo) & (calls["strike"] <= hi)].copy()
    puts  = puts[(puts["strike"] >= lo)  & (puts["strike"] <= hi)].copy()

    if calls.empty or puts.empty:
        return {}

    calls["gex"] =  calls["openInterest"].fillna(0) * spot * 100
    puts["gex"]  = -puts["openInterest"].fillna(0)  * spot * 100

    # Merge by strike
    c = calls[["strike", "gex", "openInterest"]].rename(
        columns={"gex": "call_gex", "openInterest": "call_oi"})
    p = puts[["strike", "gex", "openInterest"]].rename(
        columns={"gex": "put_gex",  "openInterest": "put_oi"})
    merged = pd.merge(c, p, on="strike", how="outer").fillna(0)
    merged["net_gex"] = merged["call_gex"] + merged["put_gex"]
    merged = merged.sort_values("strike").reset_index(drop=True)

    # GEX flip — where net GEX crosses zero
    sign_changes = merged[merged["net_gex"].diff().apply(np.sign).diff() != 0]
    if not sign_changes.empty:
        gex_flip = float(sign_changes.iloc[0]["strike"])
    else:
        gex_flip = spot

    # Call wall — strike with highest positive GEX
    call_wall_row = merged.loc[merged["call_gex"].idxmax()]
    call_wall = float(call_wall_row["strike"])

    # Put wall — strike with most negative GEX
    put_wall_row = merged.loc[merged["put_gex"].idxmin()]
    put_wall = float(put_wall_row["strike"])

    # Max pain — strike where total OI pain is minimised for option buyers
    strikes = merged["strike"].values
    max_pain_losses = []
    for s in strikes:
        call_loss = ((merged["strike"] - s).clip(lower=0) * merged["call_oi"]).sum()
        put_loss  = ((s - merged["strike"]).clip(lower=0) * merged["put_oi"]).sum()
        max_pain_losses.append(call_loss + put_loss)
    max_pain = float(strikes[np.argmin(max_pain_losses)])

    m = multiplier
    return {
        "call_wall" : call_wall * m,
        "put_wall"  : put_wall * m,
        "gex_flip"  : gex_flip * m,
        "max_pain"  : max_pain * m,
        "expiry"    : options.get("expiry", ""),
        "gex_df"    : merged,
        "spot"      : spot * m,
    }


def calc_delta_bias(intraday: pd.DataFrame, multiplier: float) -> dict:
    """
    Delta imbalance approximation from OHLCV data.
    True delta requires tick data; this uses close position + volume as proxy.
    Up volume = volume when close > open; Down volume = opposite.
    Also calculates CVD (cumulative volume delta) approximation.
    """
    df = intraday.copy()
    df.index = pd.to_datetime(df.index)
    dates = sorted(pd.Series(df.index.date).unique())
    if not dates:
        return {}

    session = df[df.index.date == dates[-1]].copy()
    if session.empty:
        session = df[df.index.date == dates[-1 if len(dates) == 1 else -2]].copy()

    # Bar close position ratio (0=low, 1=high) as delta proxy
    rng = (session["High"] - session["Low"]).replace(0, np.nan)
    session["close_pos"] = (session["Close"] - session["Low"]) / rng
    session["up_vol"]    = session["Volume"] * session["close_pos"].fillna(0.5)
    session["dn_vol"]    = session["Volume"] * (1 - session["close_pos"].fillna(0.5))
    session["bar_delta"] = session["up_vol"] - session["dn_vol"]
    session["cvd"]       = session["bar_delta"].cumsum()

    cvd_current = float(session["cvd"].iloc[-1])
    cvd_max     = float(session["cvd"].max())
    cvd_min     = float(session["cvd"].min())

    # Bias: positive CVD = buyers in control, negative = sellers
    if cvd_current > 0:
        if cvd_current > cvd_max * 0.7:
            bias = "Strong buying"
            bias_color = C_BULL
        else:
            bias = "Mild buying"
            bias_color = "#9BB55A"
    else:
        if cvd_current < cvd_min * 0.7:
            bias = "Strong selling"
            bias_color = C_BEAR
        else:
            bias = "Mild selling"
            bias_color = "#E07070"

    # Imbalance levels — price levels where largest delta spikes occurred
    session["abs_delta"] = session["bar_delta"].abs()
    top_bars = session.nlargest(5, "abs_delta")
    imbalance_levels = []
    for _, row in top_bars.iterrows():
        price = float(row["Close"]) * multiplier
        delta = float(row["bar_delta"])
        imbalance_levels.append({
            "price"    : price,
            "delta"    : delta,
            "direction": "buy" if delta > 0 else "sell",
            "time"     : row.name.strftime("%H:%M"),
        })

    return {
        "cvd"             : cvd_current,
        "cvd_max"         : cvd_max,
        "cvd_min"         : cvd_min,
        "bias"            : bias,
        "bias_color"      : bias_color,
        "imbalance_levels": imbalance_levels,
        "session_df"      : session,
    }


# ── chart ─────────────────────────────────────────────────────────────────────

def make_chart(levels: dict, data: dict) -> str:
    """
    Generate the full intraday level chart with 3 panels:
      Left:  Price chart with all levels overlaid
      Right: Volume profile
      Bottom: CVD approximation
    """
    intra  = data["intraday"]
    ticker = data["ticker"]
    mult   = data["multiplier"]
    demo   = data.get("demo", False)

    intra.index = pd.to_datetime(intra.index)
    dates  = sorted(pd.Series(intra.index.date).unique())
    today  = intra[intra.index.date == dates[-1]].copy()
    if today.empty and len(dates) >= 2:
        today = intra[intra.index.date == dates[-2]].copy()

    price_data = today["Close"] * mult
    price_hi   = today["High"]  * mult
    price_lo   = today["Low"]   * mult

    fig = plt.figure(figsize=(16, 11), facecolor=C_BG)
    label = "[DEMO] " if demo else ""
    fig.suptitle(
        f"{label}NQ Intraday Level Sheet — {ticker} proxy\n"
        f"{datetime.now().strftime('%A %d %b %Y  %H:%M')}",
        fontsize=13, fontweight="bold", color=C_TEXT, y=0.99
    )

    # Layout: main price | vol profile // CVD bottom
    gs = GridSpec(2, 2, figure=fig,
                  height_ratios=[3, 1],
                  width_ratios=[3.5, 1],
                  hspace=0.35, wspace=0.05,
                  top=0.94, bottom=0.07,
                  left=0.07, right=0.97)

    ax_price  = fig.add_subplot(gs[0, 0])
    ax_vol    = fig.add_subplot(gs[0, 1], sharey=ax_price)
    ax_cvd    = fig.add_subplot(gs[1, 0])

    _draw_price_panel(ax_price, price_data, price_hi, price_lo, levels, today)
    _draw_vol_profile(ax_vol,   levels.get("volume_profile", {}))
    _draw_cvd_panel(ax_cvd,     levels.get("delta", {}), today)

    out_path = CHART_DIR / f"nq_levels_{datetime.now().strftime('%Y%m%d_%H%M')}.png"
    plt.savefig(out_path, dpi=150, bbox_inches="tight", facecolor=C_BG)
    plt.close()
    print(f"  Chart saved → {out_path}")
    return str(out_path)


def _draw_price_panel(ax, price, hi, lo, levels, session_df):
    """Main price panel with all levels overlaid."""
    x = range(len(price))

    # Candlestick-style bars
    for i, (idx, row) in enumerate(session_df.iterrows()):
        o = row["Open"]  * levels.get("mult", 1)
        c = row["Close"] * levels.get("mult", 1)
        h = row["High"]  * levels.get("mult", 1)
        l = row["Low"]   * levels.get("mult", 1)
        color = C_BULL if c >= o else C_BEAR
        ax.plot([i, i], [l, h], color=color, linewidth=0.7, alpha=0.6)
        ax.plot([i, i], [min(o, c), max(o, c)], color=color, linewidth=2.5, solid_capstyle="butt")

    if price.empty:
        _style_ax(ax)
        return

    p_lo = price.min() * 0.998
    p_hi = price.max() * 1.002

    def hline(price_val, color, lw, ls, label, alpha=0.85, zorder=2):
        """Draw a horizontal level line with label if within view."""
        if p_lo * 0.995 <= price_val <= p_hi * 1.005:
            ax.axhline(price_val, color=color, linewidth=lw,
                       linestyle=ls, alpha=alpha, zorder=zorder)
            ax.text(len(price) * 1.002, price_val, f" {label}  {price_val:,.0f}",
                    fontsize=7.5, color=color, va="center", alpha=alpha)

    # ── Previous day/week levels ──────────────────────────────────────────
    prev = levels.get("prev", {})
    for key, ls in [("PDH","--"), ("PDL","--"), ("PDS",":"),
                    ("PWH","-."), ("PWL","-.")]:
        if key in prev:
            hline(prev[key], C_PREV, 1.0, ls, key)

    # ── Volume profile levels ─────────────────────────────────────────────
    vp = levels.get("volume_profile", {})
    if vp:
        hline(vp.get("POC", 0), C_VOL, 1.8, "-",  "POC")
        hline(vp.get("VAH", 0), C_VOL, 1.0, "--", "VAH", alpha=0.6)
        hline(vp.get("VAL", 0), C_VOL, 1.0, "--", "VAL", alpha=0.6)
        for hvn_price, _ in vp.get("HVNs", []):
            hline(hvn_price, C_VOL, 0.7, ":", "HVN", alpha=0.45)

    # ── VWAP levels ───────────────────────────────────────────────────────
    vw = levels.get("vwap", {})
    if vw:
        hline(vw.get("VWAP",   0), C_VWAP, 2.0, "-",  "VWAP")
        hline(vw.get("VWAP_1U",0), C_VWAP, 1.0, "--", "+1σ",  alpha=0.55)
        hline(vw.get("VWAP_1D",0), C_VWAP, 1.0, "--", "−1σ",  alpha=0.55)
        hline(vw.get("VWAP_2U",0), C_VWAP, 0.7, ":",  "+2σ",  alpha=0.35)
        hline(vw.get("VWAP_2D",0), C_VWAP, 0.7, ":",  "−2σ",  alpha=0.35)
        if "WVWAP" in vw:
            hline(vw["WVWAP"], C_VWAP, 1.2, "-.", "wVWAP", alpha=0.6)

    # ── Options / GEX levels ──────────────────────────────────────────────
    gex = levels.get("gex", {})
    if gex:
        hline(gex.get("call_wall", 0), C_OPT, 1.5, "--", "Call wall", alpha=0.8)
        hline(gex.get("put_wall",  0), C_OPT, 1.5, "--", "Put wall",  alpha=0.8)
        hline(gex.get("gex_flip",  0), C_VOL, 1.5, "-",  "GEX flip",  alpha=0.75)
        hline(gex.get("max_pain",  0), C_OPT, 1.0, ":",  "Max pain",  alpha=0.6)

    # ── Delta imbalance levels ────────────────────────────────────────────
    delta = levels.get("delta", {})
    for lvl in delta.get("imbalance_levels", [])[:3]:
        color  = C_BULL if lvl["direction"] == "buy" else C_BEAR
        marker = "▲" if lvl["direction"] == "buy" else "▼"
        hline(lvl["price"], color, 0.8, ":", f"{marker} {lvl['time']}", alpha=0.5)

    ax.set_xlim(-1, len(price) * 1.15)
    ax.set_ylabel("NQ Price", fontsize=10, color=C_MUTED)
    ax.set_title("Price  +  All Levels", fontsize=11, color=C_TEXT, pad=5)

    # x-axis time labels
    n = len(session_df)
    if n > 0:
        step = max(1, n // 8)
        tick_idx   = list(range(0, n, step))
        tick_labels = [session_df.index[i].strftime("%H:%M") for i in tick_idx]
        ax.set_xticks(tick_idx)
        ax.set_xticklabels(tick_labels, fontsize=7.5, color=C_MUTED)

    _style_ax(ax)

    # Legend
    legend_items = [
        mpatches.Patch(color=C_PREV, label="Prev day/week"),
        mpatches.Patch(color=C_VOL,  label="Vol profile"),
        mpatches.Patch(color=C_VWAP, label="VWAP bands"),
        mpatches.Patch(color=C_OPT,  label="Options/GEX"),
        mpatches.Patch(color=C_DELTA,label="Delta imbal."),
    ]
    ax.legend(handles=legend_items, fontsize=7.5, loc="upper left",
              framealpha=0.6, ncol=5, columnspacing=0.8)


def _draw_vol_profile(ax, vp: dict):
    """Volume profile horizontal bar chart."""
    if not vp or "profile_mids" not in vp:
        ax.set_visible(False)
        return

    mids = vp["profile_mids"]
    vol  = vp["profile_vol"]
    poc  = vp.get("POC", 0)
    vah  = vp.get("VAH", 0)
    val  = vp.get("VAL", 0)

    colors = []
    for m in mids:
        if abs(m - poc) < (mids[1] - mids[0]) * 1.5:
            colors.append(C_VOL)
        elif val <= m <= vah:
            colors.append("#AFA9EC")
        else:
            colors.append(C_NEUT)

    ax.barh(mids, vol, height=(mids[1]-mids[0])*0.9,
            color=colors, alpha=0.7)
    ax.set_title("Vol Profile\n(prev session)", fontsize=9, color=C_TEXT, pad=4)
    ax.set_xticks([])
    ax.yaxis.set_visible(False)
    _style_ax(ax)
    ax.spines["left"].set_visible(False)


def _draw_cvd_panel(ax, delta: dict, session_df):
    """CVD approximation panel."""
    if not delta or "session_df" not in delta:
        ax.set_visible(False)
        return

    cvd_series = delta["session_df"]["cvd"]
    x = range(len(cvd_series))

    ax.fill_between(x, cvd_series, 0,
                    where=(cvd_series >= 0), color=C_BULL, alpha=0.4)
    ax.fill_between(x, cvd_series, 0,
                    where=(cvd_series < 0),  color=C_BEAR, alpha=0.4)
    ax.plot(x, cvd_series, color=C_DELTA, linewidth=1.2)
    ax.axhline(0, color=C_MUTED, linewidth=0.8, linestyle="--")

    bias       = delta.get("bias", "")
    bias_color = delta.get("bias_color", C_NEUT)
    ax.set_title(f"CVD approx  ({bias})", fontsize=10,
                 color=bias_color, pad=5)
    ax.set_ylabel("Δ volume", fontsize=9, color=C_MUTED)
    _style_ax(ax)


def _style_ax(ax):
    ax.set_facecolor(C_BG)
    ax.grid(True, color=C_GRID, linewidth=0.5, axis="y", zorder=0)
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color(C_GRID)
    ax.tick_params(colors=C_MUTED, labelsize=8)


# ── terminal report ───────────────────────────────────────────────────────────

def print_report(levels: dict, data: dict):
    """Print a structured pre-market level sheet to terminal."""
    mult   = data["multiplier"]
    ticker = data["ticker"]
    demo   = data.get("demo", False)
    now    = datetime.now().strftime("%A %d %b %Y  %H:%M")

    div = "─" * 62

    print(f"""
{'═'*62}
  NQ INTRADAY LEVEL SHEET  {'[DEMO]' if demo else ''}
  {now}
  Data: {ticker}{'  (×' + str(int(mult)) + ' → NQ)' if mult != 1.0 else ''}
{'═'*62}""")

    # ── Previous levels ───────────────────────────────────────────────────
    prev = levels.get("prev", {})
    if prev:
        print(f"\n  {'PREVIOUS DAY / WEEK':─<50}")
        for k, label in [
            ("PDH", "Prev Day High  (PDH)"),
            ("PDL", "Prev Day Low   (PDL)"),
            ("PDS", "Prev Day Settle(PDS)"),
            ("PWH", "Prev Week High (PWH)"),
            ("PWL", "Prev Week Low  (PWL)"),
        ]:
            if k in prev:
                print(f"  {label:<28} {prev[k]:>10,.1f}")

    # ── Volume profile ────────────────────────────────────────────────────
    vp = levels.get("volume_profile", {})
    if vp:
        print(f"\n  {'VOLUME PROFILE  (prev session)':─<50}")
        for k, label in [
            ("VAH","Value Area High (VAH)"),
            ("POC","Point of Control(POC)  ★"),
            ("VAL","Value Area Low  (VAL)"),
        ]:
            if k in vp:
                print(f"  {label:<28} {vp[k]:>10,.1f}")
        for i, (hvn_p, _) in enumerate(vp.get("HVNs", []), 1):
            print(f"  {'HVN ' + str(i):<28} {hvn_p:>10,.1f}")

    # ── VWAP ─────────────────────────────────────────────────────────────
    vw = levels.get("vwap", {})
    if vw:
        print(f"\n  {'VWAP BANDS':─<50}")
        for k, label in [
            ("VWAP_2U", "VWAP +2σ"),
            ("VWAP_1U", "VWAP +1σ"),
            ("VWAP",    "VWAP      ★"),
            ("WVWAP",   "Weekly VWAP"),
            ("VWAP_1D", "VWAP −1σ"),
            ("VWAP_2D", "VWAP −2σ"),
        ]:
            if k in vw:
                print(f"  {label:<28} {vw[k]:>10,.1f}")

    # ── GEX / Options ─────────────────────────────────────────────────────
    gex = levels.get("gex", {})
    if gex:
        exp = gex.get("expiry", "")
        print(f"\n  {'OPTIONS / GEX LEVELS':─<50}  expiry {exp}")
        for k, label in [
            ("call_wall", "Call Wall   (resistance)"),
            ("gex_flip",  "GEX Flip    (bull/bear line) ★"),
            ("max_pain",  "Max Pain    (Fri magnet)"),
            ("put_wall",  "Put Wall    (support)"),
        ]:
            if k in gex:
                print(f"  {label:<28} {gex[k]:>10,.1f}")

    # ── Delta bias ────────────────────────────────────────────────────────
    delta = levels.get("delta", {})
    if delta:
        print(f"\n  {'DELTA / ORDER FLOW (approx)':─<50}")
        print(f"  Session bias:                {delta.get('bias',''):>20}")
        print(f"  CVD:                         {delta.get('cvd',0):>20,.0f}")
        for lvl in delta.get("imbalance_levels", [])[:4]:
            arrow = "▲ BUY " if lvl["direction"] == "buy" else "▼ SELL"
            print(f"  {arrow} imbalance @ {lvl['time']}    {lvl['price']:>10,.1f}")

    # ── Trade bias summary ────────────────────────────────────────────────
    print(f"\n{'═'*62}")
    print(f"  KEY LEVELS TO WATCH  (marked ★)")
    print(f"{'═'*62}")

    key = {}
    if vw:   key["VWAP"]    = vw.get("VWAP", 0)
    if vp:   key["POC"]     = vp.get("POC",  0)
    if gex:  key["GEX flip"]= gex.get("gex_flip", 0)
    if prev: key["PDH"]     = prev.get("PDH", 0)
    if prev: key["PDL"]     = prev.get("PDL", 0)

    sorted_key = sorted(key.items(), key=lambda x: -x[1])
    for label, price in sorted_key:
        if price > 0:
            print(f"  {label:<20} {price:>10,.1f}")

    bias = delta.get("bias","Unknown") if delta else "Unknown"
    print(f"""
  Delta bias:   {bias}
  Note: ★ levels are high-confluence — prioritise these for
        entry/exit. Confluence of 2+ layer types = strongest.

  Scalping rules of thumb:
  → Above VWAP + PDH = buyers in control, fade shorts
  → Below VWAP + PDL = sellers in control, fade longs
  → At POC = chop zone, wait for direction break
  → GEX flip = key bull/bear dividing line for the week
{'═'*62}
""")


# ── export ────────────────────────────────────────────────────────────────────

def export_json(levels: dict, data: dict) -> str:
    """Export all levels to JSON for use in other tools / dashboards."""
    out = {
        "generated_at": datetime.now().isoformat(),
        "ticker"      : data["ticker"],
        "multiplier"  : data["multiplier"],
        "demo"        : data.get("demo", False),
        "prev"        : levels.get("prev", {}),
        "vwap"        : {k: v for k, v in levels.get("vwap", {}).items()
                         if k not in ("session_df",)},
        "volume_profile": {k: v for k, v in levels.get("volume_profile", {}).items()
                           if k not in ("profile_mids","profile_vol","session_df")},
        "gex"         : {k: v for k, v in levels.get("gex", {}).items()
                         if k not in ("gex_df",)},
        "delta"       : {k: v for k, v in levels.get("delta", {}).items()
                         if k not in ("session_df",)},
    }
    # Convert numpy types
    def to_python(obj):
        if isinstance(obj, (np.integer,)): return int(obj)
        if isinstance(obj, (np.floating,)): return round(float(obj), 2)
        if isinstance(obj, dict): return {k: to_python(v) for k, v in obj.items()}
        if isinstance(obj, list): return [to_python(i) for i in obj]
        return obj

    out = to_python(out)
    path = DATA_DIR / "nq_levels_latest.json"
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"  JSON exported → {path}")
    return str(path)


# ── demo data ─────────────────────────────────────────────────────────────────

def _generate_demo_data(ticker: str) -> dict:
    """Generate realistic synthetic data for testing without internet."""
    print("  [DEMO MODE] Generating synthetic market data...")
    np.random.seed(7)
    mult = 1.0 if "=F" in ticker else QQQ_TO_NQ
    base = 480.0  # QQQ base price

    # Daily bars
    dates_daily  = pd.bdate_range(end=datetime.now().date(), periods=14)
    n_days       = len(dates_daily)
    daily_closes = base + np.cumsum(np.random.normal(0, 1.2, n_days))
    daily_data   = {
        "Open"  : daily_closes - np.random.uniform(0.3, 1.5, n_days),
        "High"  : daily_closes + np.random.uniform(0.8, 2.5, n_days),
        "Low"   : daily_closes - np.random.uniform(0.8, 2.5, n_days),
        "Close" : daily_closes,
        "Volume": np.random.randint(30_000_000, 80_000_000, n_days),
    }
    daily       = pd.DataFrame(daily_data, index=dates_daily)

    # Intraday 5-min bars (2 sessions)
    rows = []
    for d in [datetime.now().date() - timedelta(days=1), datetime.now().date()]:
        t   = datetime.combine(d, time(9, 30))
        price = base + np.random.normal(0, 0.5)
        for i in range(78):  # 6.5hr session
            o  = price
            c  = o + np.random.normal(0, 0.18)
            hi = max(o, c) + abs(np.random.normal(0, 0.1))
            lo = min(o, c) - abs(np.random.normal(0, 0.1))
            rows.append({
                "Open": o, "High": hi, "Low": lo, "Close": c,
                "Volume": int(np.random.randint(200_000, 1_500_000)),
            })
            price = c
            t    += timedelta(minutes=5)

    intra_idx = []
    for d in [datetime.now().date() - timedelta(days=1), datetime.now().date()]:
        t = datetime.combine(d, time(9, 30))
        for _ in range(78):
            intra_idx.append(t)
            t += timedelta(minutes=5)

    intra = pd.DataFrame(rows, index=pd.DatetimeIndex(intra_idx))
    print("  Synthetic data generated.")

    return {
        "ticker"    : ticker,
        "daily"     : daily,
        "intraday"  : intra,
        "options"   : {},   # no synthetic options in demo
        "multiplier": mult,
        "is_futures": "=F" in ticker,
        "demo"      : True,
    }


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="NQ Intraday Level Generator")
    parser.add_argument("--ticker",   default="QQQ",
                        help="Ticker to use (default: QQQ). Try NQ=F for futures.")
    parser.add_argument("--demo",     action="store_true",
                        help="Use synthetic data (no internet required)")
    parser.add_argument("--no-chart", action="store_true",
                        help="Skip chart, print terminal report only")
    parser.add_argument("--export",   choices=["json","none"], default="json",
                        help="Export format (default: json)")
    args = parser.parse_args()

    print("\n" + "═"*62)
    print("  NQ INTRADAY LEVEL GENERATOR")
    if args.demo:
        print("  [DEMO MODE — synthetic data]")
    print("═"*62 + "\n")

    print("[ 1/5 ] Fetching market data...")
    data = fetch_data(ticker=args.ticker, demo=args.demo)
    mult = data["multiplier"]

    print("[ 2/5 ] Calculating previous day/week levels...")
    prev = calc_prev_levels(data["daily"], mult)

    print("[ 3/5 ] Calculating VWAP, volume profile & delta...")
    vwap   = calc_vwap_levels(data["intraday"], mult)
    vol_p  = calc_volume_profile(data["intraday"], mult)
    delta  = calc_delta_bias(data["intraday"], mult)

    print("[ 4/5 ] Calculating GEX / options levels...")
    spot = float(data["daily"]["Close"].iloc[-1])
    gex  = calc_gex_levels(data["options"], spot, mult)

    levels = {
        "prev"          : prev,
        "vwap"          : vwap,
        "volume_profile": vol_p,
        "gex"           : gex,
        "delta"         : delta,
        "mult"          : mult,
    }

    print_report(levels, data)

    if not args.no_chart:
        print("[ 5/5 ] Rendering chart...")
        make_chart(levels, data)
    else:
        print("[ 5/5 ] Chart skipped.")

    if args.export == "json":
        export_json(levels, data)

    print("  Done.\n")


if __name__ == "__main__":
    main()


