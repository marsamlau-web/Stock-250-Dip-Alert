"""
Stock 250 Dip Alert
===============

Standalone watchlist monitor for individual stocks. Sends silent Telegram
alerts when stocks drop below configured drawdown thresholds from their
all-time high.

Signal-only — never suggests a deployment amount.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"


# ---------------------------------------------------------------------------
# Config + state I/O
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        print(f"WARNING: could not parse {STATE_PATH}; starting from empty state.", file=sys.stderr)
        return {}


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_price_and_history(symbol: str) -> tuple[float | None, float | None, str | None]:
    """Returns (current_price, all_time_high, ath_date_iso). None on failure."""
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(start="2020-01-01", auto_adjust=True)
        if hist.empty:
            return None, None, None

        closes = hist["Close"].dropna()
        if closes.empty:
            return None, None, None

        current = float(closes.iloc[-1])

        # Sanity check: filter implausible spikes (>5x median of last 60 days)
        recent_median = float(closes.tail(60).median()) if len(closes) >= 30 else current
        threshold = recent_median * 5
        clean_closes = closes[closes <= threshold]

        if clean_closes.empty:
            return current, current, hist.index[-1].strftime("%Y-%m-%d")

        ath_idx = clean_closes.idxmax()
        ath = float(clean_closes.loc[ath_idx])
        ath_date = ath_idx.strftime("%Y-%m-%d")

        return current, ath, ath_date
    except Exception as e:
        print(f"  ERROR fetching {symbol}: {e}", file=sys.stderr)
        return None, None, None


# ---------------------------------------------------------------------------
# Alert detection logic
# ---------------------------------------------------------------------------

def threshold_price(reference: float, pct: int) -> float:
    """Price at which a -pct% drawdown is reached from `reference`."""
    return reference * (1 - pct / 100)


def detect_ath_alerts(
    ticker_state: dict,
    current_price: float,
    ath: float,
    ath_date: str,
    thresholds_pct: list[int],
) -> list[int]:
    """
    Returns a list of threshold percentages that fired this check.

    Rules:
    - Re-arm: when a new ATH is set, all thresholds re-arm.
    - First-run: when state is empty for this ticker, initialize without
      firing any alerts. Pre-disarm any thresholds the stock is ALREADY
      below — we don't know WHEN it crossed them and don't want to spam
      stale alerts.
    - Multi-threshold: if a stock crosses multiple thresholds at once
      (e.g., genuine earnings crash from -10% to -50% between checks),
      fire ONLY the deepest one. Disarm the others to prevent later
      duplicate alerts on the same drawdown.
    """
    has_prior_state = "ath" in ticker_state
    prev_ath = ticker_state.get("ath", 0.0)
    disarmed = set(ticker_state.get("ath_disarmed", []))

    if not has_prior_state:
        # First observation of this ticker. Pre-disarm any thresholds the
        # stock is already below to avoid firing stale alerts on stocks
        # that have been down for ages before they entered the watchlist.
        for pct in thresholds_pct:
            if current_price <= threshold_price(ath, pct):
                disarmed.add(pct)
        ticker_state["ath"] = ath
        ticker_state["ath_date"] = ath_date
        ticker_state["ath_disarmed"] = sorted(disarmed)
        ticker_state["initialized"] = True
        return []

    if ath > prev_ath:
        # New all-time high — re-arm all thresholds.
        ticker_state["ath"] = ath
        ticker_state["ath_date"] = ath_date
        disarmed = set()

    eff_ath = ticker_state["ath"]

    # Find every threshold the price is below AND that hasn't been disarmed.
    candidates = []
    for pct in thresholds_pct:
        if pct in disarmed:
            continue
        if current_price <= threshold_price(eff_ath, pct):
            candidates.append(pct)

    if not candidates:
        ticker_state["ath_disarmed"] = sorted(disarmed)
        return []

    # Fire ONLY the deepest. Disarm all candidates so we don't re-fire on
    # the next check — the price is already below them, so nothing new
    # has happened. They'll re-arm when a new ATH is set.
    deepest = max(candidates)
    for pct in candidates:
        disarmed.add(pct)
    ticker_state["ath_disarmed"] = sorted(disarmed)
    return [deepest]


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------

def build_alert_message(t: dict, pct: int, current: float, ath: float, ath_date: str) -> str:
    """Stock watchlist alert — informational, signal-only."""
    return (
        f"📰 <b>STOCK: {t['display_name']} ({t['track_symbol']}) −{pct}% from ATH</b>\n"
        f"\n"
        f"Current: ${current:,.2f}\n"
        f"ATH: ${ath:,.2f} ({ath_date})\n"
        f"Drawdown: −{pct}%\n"
        f"\n"
        f"<i>Signal only — no deployment suggested.</i>"
    )


def build_heartbeat_for_group(config: dict, group_name: str, snapshots: list[dict]) -> str | None:
    """Build the heartbeat message for a single group, or None if nothing to report."""
    tz = ZoneInfo(config["timezone"])
    now_local = datetime.now(tz)
    timestamp = now_local.strftime("%Y-%m-%d %H:%M %Z")

    min_dd = config.get("heartbeat_min_drawdown_pct", 10)

    group_snaps = [s for s in snapshots if s["ticker"].get("group") == group_name]
    total_in_group = len(group_snaps)

    displayed = []
    failed = []
    for snap in group_snaps:
        if snap.get("fetch_failed"):
            failed.append(snap)
            continue
        if snap["dd_ath_pct"] <= -min_dd:
            displayed.append(snap)

    if not displayed and not failed:
        return None

    header = f"📰 <b>Stocks Weekly check — {group_name}</b>"
    lines = [header, f"<i>{timestamp}</i>", ""]

    if displayed:
        lines.append(f"<b>Down ≥ {min_dd}% from ATH ({len(displayed)} of {total_in_group}):</b>")

        sector_order = config.get("sector_order", [])
        by_sector: dict[str, list[dict]] = {}
        for snap in displayed:
            sector = snap["ticker"].get("sector", "Other")
            by_sector.setdefault(sector, []).append(snap)

        known = [s for s in sector_order if s in by_sector]
        extras = sorted(s for s in by_sector if s not in sector_order)
        ordered_sectors = known + extras

        for sector in ordered_sectors:
            stocks = by_sector[sector]
            stocks.sort(key=lambda s: s["ticker"]["display_name"].lower())
            lines.append("")
            lines.append(f"<b>— {sector} —</b>")
            for snap in stocks:
                name = snap["ticker"]["display_name"]
                sym = snap["ticker"]["track_symbol"]
                price = snap["current"]
                dd_ath = snap["dd_ath_pct"]
                markers = []
                for pct in snap.get("ath_fired", []):
                    markers.append(f"📰 −{pct}%")
                marker_str = " " + " ".join(markers) if markers else ""
                lines.append(f"• <b>{name}</b> <i>({sym})</i>: ${price:,.2f} | ATH {dd_ath:+.1f}%{marker_str}")

    if failed:
        lines.append("")
        lines.append("<i>Fetch failed:</i>")
        for snap in failed:
            lines.append(f"• {snap['ticker']['display_name']} <i>({snap['ticker']['track_symbol']})</i>")

    return "\n".join(lines)


def build_all_heartbeats(config: dict, snapshots: list[dict]) -> list[str]:
    """One heartbeat per group, skipping groups with nothing to report."""
    messages = []
    for group_name in config.get("group_order", []):
        msg = build_heartbeat_for_group(config, group_name, snapshots)
        if msg is not None:
            messages.append(msg)
    return messages


# ---------------------------------------------------------------------------
# Telegram delivery
# ---------------------------------------------------------------------------

def send_telegram(bot_token: str, chat_id: str, text: str, silent: bool = False) -> None:
    """Send a Telegram message via the Bot API. HTML parse mode."""
    if not bot_token or not chat_id:
        print("  (Telegram not configured — would send:)\n" + text + "\n")
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": silent,
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            print(f"  Telegram error {r.status_code}: {r.text}", file=sys.stderr)
        else:
            print("  → sent to Telegram")
    except requests.RequestException as e:
        print(f"  Telegram exception: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Heartbeat scheduling
# ---------------------------------------------------------------------------

def is_heartbeat_time(config: dict, state: dict, force: bool = False) -> bool:
    """Returns True if it's time to send the weekly heartbeat."""
    if force:
        return True

    now_utc = datetime.now(timezone.utc)
    target_weekday = config.get("heartbeat_weekday", 4)
    target_hour = config.get("heartbeat_hour_utc", 18)

    iso_year, iso_week, _ = now_utc.isocalendar()
    week_key = f"{iso_year}-W{iso_week:02d}"

    if state.get("last_heartbeat_week") == week_key:
        return False

    if now_utc.weekday() < target_weekday:
        return False
    if now_utc.weekday() == target_weekday and now_utc.hour < target_hour:
        return False

    return True


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def process_stocks(config: dict, state: dict, bot_token: str, chat_id: str) -> list[dict]:
    """Fetch every stock, detect alerts, fire silent Telegram messages."""
    print("\n=== STOCKS WATCHLIST (signal-only, silent) ===\n")
    thresholds = config["thresholds_pct"]

    snapshots = []
    for t in config.get("tickers", []):
        sym = t["track_symbol"]
        print(f"[{sym}] {t['display_name']}")

        current, ath, ath_date = fetch_price_and_history(sym)
        if current is None:
            print("  ⚠️  Skipped — fetch failed")
            snapshots.append({"ticker": t, "fetch_failed": True})
            continue

        ts = state.setdefault(sym, {})
        ath_fired = detect_ath_alerts(ts, current, ath, ath_date, thresholds)

        eff_ath = ts["ath"]
        eff_ath_date = ts["ath_date"]
        dd_ath = (current - eff_ath) / eff_ath * 100
        print(f"  Price: ${current:,.2f} | ATH: ${eff_ath:,.2f} ({dd_ath:+.2f}%)")

        for pct in ath_fired:
            text = build_alert_message(t, pct, current, eff_ath, eff_ath_date)
            send_telegram(bot_token, chat_id, text, silent=True)

        snapshots.append({
            "ticker": t,
            "current": current,
            "ath": eff_ath,
            "ath_date": eff_ath_date,
            "dd_ath_pct": dd_ath,
            "ath_fired": ath_fired,
        })

        if not ath_fired:
            print("  No new alerts.")

    return snapshots


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def main() -> int:
    print(f"=== Stock 250 Dip Alert run @ {datetime.now(timezone.utc).isoformat()} ===")

    config = load_config()
    state = load_state()

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        print("WARNING: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set.", file=sys.stderr)

    snapshots = process_stocks(config, state, bot_token, chat_id)

    force_hb = os.environ.get("FORCE_HEARTBEAT", "").lower() in ("1", "true", "yes")
    if force_hb:
        print("\n(FORCE_HEARTBEAT=true — sending heartbeat regardless of time.)")

    if is_heartbeat_time(config, state, force=force_hb):
        print("\n=== HEARTBEAT (weekly summary) ===\n")
        messages = build_all_heartbeats(config, snapshots)
        if not messages:
            now_utc = datetime.now(timezone.utc)
            timestamp = now_utc.strftime("%Y-%m-%d %H:%M UTC")
            text = (
                f"📰 <b>Stocks Weekly check</b>\n"
                f"<i>{timestamp}</i>\n\n"
                f"<i>All {len(snapshots)} watchlist stocks are within "
                f"{config.get('heartbeat_min_drawdown_pct', 10)}% of ATH.</i>"
            )
            send_telegram(bot_token, chat_id, text, silent=True)
        else:
            print(f"Sending {len(messages)} group heartbeat message(s).")
            for text in messages:
                send_telegram(bot_token, chat_id, text, silent=True)
        now_utc = datetime.now(timezone.utc)
        iso_year, iso_week, _ = now_utc.isocalendar()
        state["last_heartbeat_week"] = f"{iso_year}-W{iso_week:02d}"
    else:
        print("\n(No heartbeat this run.)")

    save_state(state)
    print("\n=== Done. State saved. ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
