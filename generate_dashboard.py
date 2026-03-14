#!/usr/bin/env python3
"""
Macro Dashboard Generator (v1 risk upgrade)
Pulls central bank rates, economic events, and generates an HTML dashboard
tailored to the active forex bot portfolio, with event volatility scores
+ bot-level risk indices.
"""

import json
import os
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

# ── Portfolio Configuration ────────────────────────────────────────────────────
PORTFOLIO = {
    "Control": {"pairs": ["EURJPY", "USDCAD"], "strategy": "Fibonacci Grid", "color": "#a78bfa"},
    "Jet": {"pairs": ["EURUSD", "EURGBP"], "strategy": "Dual-Dir Grid", "color": "#22c55e"},
    "HGold": {"pairs": ["XAUUSD"], "strategy": "Fixed-Lot Scalper", "color": "#fcd34d"},
    "Hedge": {"pairs": ["AUDCAD"], "strategy": "Dual-Dir Grid", "color": "#2dd4bf"},
}

# Optional bot-specific severity tuning (v1 keeps it simple but configurable)
BOT_SEVERITY_WEIGHTS = {
    # base multiplier for event score when affecting this bot
    "Control": 1.0,
    "Jet": 1.1,
    "HGold": 1.3,  # more sensitive to macro spikes
    "Hedge": 0.9,
}

# All unique pairs across portfolio
ALL_PAIRS = sorted(set(p for bot in PORTFOLIO.values() for p in bot["pairs"]))

# Pair → central banks that drive volatility
PAIR_CB_MAP = {
    "EURJPY": ["ECB", "BOJ"],
    "USDCAD": ["FED", "BOC"],
    "EURGBP": ["ECB", "BOE"],
    "EURUSD": ["ECB", "FED"],
    "AUDCAD": ["RBA", "BOC"],
    "XAUUSD": ["FED"],
}

# FRED series for central bank policy rates
FRED_SERIES = {
    # Each entry has a list of candidate series IDs tried in order until one works.
    # Candidates verified against FRED catalogue as of early 2026.
    "FED": {
        "ids": ["FEDFUNDS"],
        "name": "Fed Funds Rate",
        "currency": "USD",
        "flag": "🇺🇸",
    },
    "ECB": {
        "ids": ["ECBDFR"],
        "name": "ECB Deposit Rate",
        "currency": "EUR",
        "flag": "🇪🇺",
    },
    "BOE": {
        "ids": ["IUDSOIA", "BOEBR"],
        "name": "BOE Base Rate",
        "currency": "GBP",
        "flag": "🇬🇧",
    },
    "BOJ": {
        # BOJ near-zero rate: use overnight call rate or 3M Tibor as proxy
        "ids": ["IRSTCB01JPM156N", "IR3TIB01JPM156N", "INTGSTJPM193N"],
        "name": "BOJ Policy Rate",
        "currency": "JPY",
        "flag": "🇯🇵",
    },
    "BOC": {
        # BOC overnight rate target
        "ids": ["IRSTCB01CAM156N", "INTGSTCAM193N", "IR3TBB01CAM156N"],
        "name": "BOC Policy Rate",
        "currency": "CAD",
        "flag": "🇨🇦",
    },
    "RBA": {
        # RBA cash rate target
        "ids": ["IRSTCB01AUM156N", "INTGSTAUM193N", "IR3TIB01AUM156N"],
        "name": "RBA Cash Rate",
        "currency": "AUD",
        "flag": "🇦🇺",
    },
}

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")

# ── Data Fetching ──────────────────────────────────────────────────────────────

def fetch_fred_series(series_id):
    """Fetch last 6 months of a FRED data series."""
    if not FRED_API_KEY:
        return None
    twelve_months_ago = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    url = (
        f"https://api.stlouisfed.org/fred/series/observations"
        f"?series_id={series_id}&api_key={FRED_API_KEY}"
        f"&observation_start={twelve_months_ago}&file_type=json&sort_order=desc&limit=13"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        obs = [o for o in data.get("observations", []) if o["value"] != "."]
        if not obs:
            return None
        all_values = [float(o["value"]) for o in obs]
        all_dates = [o["date"] for o in obs]
        current = all_values[0]
        prev = all_values[1] if len(all_values) > 1 else current
        # Sparkline = most recent 6 data points
        spark_vals = all_values[:6]
        spark_dates = all_dates[:6]
        # Trend derived from sparkline endpoints so arrow always matches the visible chart
        spark_oldest = spark_vals[-1] if len(spark_vals) > 1 else current
        if current > spark_oldest + 0.05:
            trend = "hiking"
        elif current < spark_oldest - 0.05:
            trend = "cutting"
        else:
            trend = "holding"
        history = list(zip(spark_dates[::-1], spark_vals[::-1]))
        return {"current": current, "previous": prev, "trend": trend, "history": history, "date": all_dates[0]}
    except Exception as e:
        print(f" FRED error ({series_id}): {e}")
        return None


def fetch_rateprobability(cb_key):
    """
    Fetch rate probabilities from rateprobability.com JSON API.
    The pages are JS-rendered SPAs — we call the underlying API endpoint directly.
    Endpoint: /api/{cb}/latest — returns JSON with current rate + per-meeting path.
    cb_key: fed, ecb, boj, boe, boc, rba
    """
    import re, json as _json
    url = f"https://rateprobability.com/api/{cb_key.lower()}/latest"
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
            "Accept": "application/json, */*",
            "Accept-Encoding": "identity",
            "Referer": f"https://rateprobability.com/{cb_key.lower()}",
            "Origin": "https://rateprobability.com",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
        if raw[:2] == b"\x1f\x8b":
            import gzip
            raw = gzip.decompress(raw)
        data = _json.loads(raw)
        print(f" rateprobability.com API ({cb_key}): {len(raw)} bytes, keys={list(data.keys())[:6]}")
    except Exception as e:
        print(f" rateprobability.com ({cb_key}): API error — {e}")
        return None

    result = {"cb": cb_key.upper(), "source": "rateprobability.com", "meetings": []}

    # All data is nested under data['today']
    today = data.get("today", {})
    if not today:
        print(f" rateprobability.com ({cb_key}): no 'today' key in response")
        return None

    # Current rate — field name varies per CB
    CB_RATE_KEYS = [
        "cash_rate_target",  # RBA
        "Overnight Rate Target",  # BOC (capitalised)
        "current_target",  # BOE, BOJ
        "ecb_deposit_facility",  # ECB
        "depo_reported",  # ECB fallback
        "midpoint",  # FED
        "most_recent_effr",  # FED fallback
    ]

    current_rate = None
    for key in CB_RATE_KEYS:
        v = today.get(key)
        if v is not None:
            try:
                current_rate = float(v)
                break
            except (TypeError, ValueError):
                pass

    if current_rate is None:
        print(f" rateprobability.com ({cb_key}): could not find rate. today keys: {list(today.keys())[:10]}")
        return None
    result["current_rate"] = current_rate

    # Rows are under today['rows']
    meetings_data = today.get("rows", [])
    if not meetings_data:
        print(f" rateprobability.com ({cb_key}): no rows in today. Keys: {list(today.keys())}")
        return None

    now = datetime.now(timezone.utc)
    for row in meetings_data:
        if not isinstance(row, dict):
            continue

        # Date — use meeting_iso (YYYY-MM-DD) preferentially
        date_val = row.get("meeting_iso") or row.get("meeting") or row.get("date")
        if not date_val:
            continue
        dt = None
        for fmt in ("%Y-%m-%d", "%b %d, %Y", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(str(date_val)[:10], fmt[:10]).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        if not dt or dt < now - timedelta(days=1):
            continue

        # Implied rate post-meeting
        implied_rate = None
        for k in ("implied_rate_post_meeting", "impliedRate", "implied_rate", "postMeetingRate"):
            try:
                implied_rate = float(row[k])
                break
            except (TypeError, ValueError, KeyError):
                pass

        # Probability of move
        prob = None
        for k in ("prob_move_pct", "probability", "prob", "moveProbability"):
            try:
                prob = abs(float(row[k]))
                break
            except (TypeError, ValueError, KeyError):
                pass

        # Delta bp (cumulative change vs current)
        delta_bp = None
        for k in ("change_bps", "deltaBp", "delta_bp", "delta", "bpChange"):
            try:
                delta_bp = float(row[k])
                break
            except (TypeError, ValueError, KeyError):
                pass

        if implied_rate is None or prob is None or delta_bp is None:
            continue

        is_cut = bool(row.get("prob_is_cut", False))
        # Use implied - current for delta (change_bps may be vs stale baseline)
        real_delta = (implied_rate - result["current_rate"]) * 100
        if prob < 5:
            direction = "hold"
        elif is_cut:
            direction = "cut"
        else:
            direction = "hike"
        result["meetings"].append(
            {
                "date": dt,
                "date_str": dt.strftime("%b %d, %Y"),
                "implied_rate": implied_rate,
                "probability": prob,
                "direction": direction,
                "delta_bp": real_delta,
            }
        )

    if not result["meetings"]:
        print(f" rateprobability.com ({cb_key}): no future meetings in response. Sample row: {meetings_data[0] if meetings_data else None}")
        return None

    next_mtg = result["meetings"][0]
    result.update(
        {
            "next_meeting_date": next_mtg["date_str"],
            "next_meeting_direction": next_mtg["direction"],
            "next_meeting_prob": next_mtg["probability"],
            "next_implied_rate": next_mtg["implied_rate"],
            "delta_bp": next_mtg["delta_bp"],
        }
    )
    print(
        f" rateprobability.com ({cb_key.upper()}): {result['current_rate']}% → "
        f"{next_mtg['direction']} {next_mtg['probability']}% @ {next_mtg['date_str']} "
        f"(Δ{next_mtg['delta_bp']:+.1f}bp)"
    )
    return result


def _rp_to_implied(rp):
    """Convert fetch_rateprobability() result → implied_moves dict format."""
    if not rp:
        return None
    return {
        "direction": rp["next_meeting_direction"],
        "probability": min(int(rp["next_meeting_prob"]), 95),
        "spread_bp": round(rp["delta_bp"], 1),
        "forward_rate": round(rp["next_implied_rate"], 3),
        "current_rate": round(rp["current_rate"], 3),
        "fwd_label": f"rateprobability.com",
        "next_meeting": rp["next_meeting_date"],
    }


def _rp_to_cb_rate(rp):
    """Convert fetch_rateprobability() result → cb_rates dict format."""
    if not rp or "current_rate" not in rp:
        return None
    current = rp["current_rate"]
    meetings = rp.get("meetings", [])
    future_rate = meetings[0]["implied_rate"] if meetings else current
    trend = "hiking" if future_rate > current + 0.05 else (
        "cutting" if future_rate < current - 0.05 else "holding"
    )
    # Build a sparkline-compatible history from implied path
    history = [(m["date_str"], m["implied_rate"]) for m in meetings[:6]]
    if not history:
        history = [(datetime.now().strftime("%Y-%m-%d"), current)]
    return {
        "current": current,
        "previous": current,
        "trend": trend,
        "history": history,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "source": "rateprobability.com",
    }


def _parse_ff_xml(content, seen):
    """
    Parse Forex Factory XML. FF uses structure (not RSS).
    Dates are MM-DD-YYYY, times are like '7:00am', impact is 'High'/'Medium' etc.
    Country codes are currency codes: USD, EUR, GBP, JPY, CAD, AUD, CHF, NZD.
    """
    events = []

    # FF uses windows-1252 encoding — decode accordingly
    if isinstance(content, bytes):
        try:
            text = content.decode("windows-1252")
        except Exception:
            text = content.decode("utf-8", errors="replace")
        # Re-encode as UTF-8 for ElementTree
        content = text.encode("utf-8")
        # Try to ensure UTF-8 declaration
        content = content.replace(b'encoding="windows-1252"', b'encoding="utf-8"')

    root = ET.fromstring(content)

    # Map FF currency codes → our CB identifiers
    currency_cb_map = {
        "USD": ("FED", "US"),
        "EUR": ("ECB", "EU"),
        "GBP": ("BOE", "GB"),
        "JPY": ("BOJ", "JP"),
        "CAD": ("BOC", "CA"),
        "AUD": ("RBA", "AU"),
        "CHF": ("SNB", "CH"),
        "NZD": ("RBNZ", "NZ"),
    }

    # FF impact levels to normalise
    high_impact = {"high", "holiday"}  # holiday = market closed = relevant
    medium_impact = {"medium"}

    for event in root.findall("event"):
        title = (event.findtext("title") or "").strip()
        country = (event.findtext("country") or "").strip().upper()
        date_str = (event.findtext("date") or "").strip()
        time_str = (event.findtext("time") or "").strip()
        impact = (event.findtext("impact") or "").strip().lower()
        forecast = (event.findtext("forecast") or "").strip()
        previous = (event.findtext("previous") or "").strip()
        actual = (event.findtext("actual") or "").strip()

        if impact not in high_impact and impact not in medium_impact:
            continue

        key = f"{title}|{date_str}|{country}"
        if key in seen:
            continue
        seen.add(key)

        # Parse date: MM-DD-YYYY + time like "2:00pm"
        dt = None
        try:
            # Combine date + time
            time_clean = time_str.replace("\u200b", "").strip()  # remove zero-width spaces
            if time_clean and time_clean.lower() not in ("all day", "tentative", ""):
                dt = datetime.strptime(f"{date_str} {time_clean}", "%m-%d-%Y %I:%M%p")
            else:
                dt = datetime.strptime(date_str, "%m-%d-%Y")
            dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            try:
                dt = datetime.strptime(date_str, "%m-%d-%Y").replace(tzinfo=timezone.utc)
            except Exception:
                dt = None

        cb, country_code = currency_cb_map.get(country, (country, country[:2]))
        impact_norm = "high" if impact in high_impact else "medium"

        events.append(
            {
                "title": title,
                "date": dt,
                "date_str": dt.strftime("%Y-%m-%d") if dt else date_str,
                "country": country_code,
                "cb": cb,
                "impact": impact_norm,
                "forecast": forecast,
                "previous": previous,
                "actual": actual,
            }
        )

    return events


def _make_request(url, extra_headers=None, timeout=15):
    """Make a URL request with realistic browser headers. No gzip to avoid decode issues."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "application/xml,text/xml,*/*;q=0.9",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",  # explicitly request no compression
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_forex_factory_calendar():
    """
    Fetch 14-day economic calendar. FF only serves thisweek XML but includes
    dates up to ~10 days out depending on when in the week you fetch.
    We accept all events within 14 days regardless of which feed they came from.
    Fallback: FRED release calendar if FF is unreachable.
    """
    events = []
    seen = set()
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=14)

    # ── Source 1: Forex Factory XML ────────────────────────────────────────────
    ff_feeds = [
        ("thisweek", "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"),
        ("thisweek", "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.xml"),
        # nextweek — FF publishes this late in the week; 404 is normal Mon–Thu
        ("nextweek", "https://nfs.faireconomy.media/ff_calendar_nextweek.xml"),
    ]

    fetched_weeks = set()

    for week_key, url in ff_feeds:
        if week_key in fetched_weeks:
            continue
        try:
            print(f" FF XML: {url}")
            with _make_request(url) as resp:
                raw = resp.read()
            print(f" → {len(raw)} bytes")
            if raw[:2] == b"\x1f\x8b":
                import gzip
                raw = gzip.decompress(raw)
            parsed = _parse_ff_xml(raw, seen)
            if parsed:
                events.extend(parsed)
                fetched_weeks.add(week_key)
                print(f" → ✓ {len(parsed)} events ({week_key})")
            else:
                print(f" → 0 events parsed. XML preview: {raw[:300]}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f" → 404 (not available yet this cycle)")
            else:
                print(f" → HTTP {e.code} — skipping")
        except urllib.error.URLError as e:
            print(f" → URLError: {e.reason} — skipping")
        except Exception as e:
            print(f" → {type(e).__name__}: {e}")

    # Filter to 14-day window — FF thisweek XML often contains dates beyond Sunday
    before = len(events)
    events = [e for e in events if e["date"] and e["date"] <= cutoff]
    if before != len(events):
        print(f" → Trimmed to 14-day window: {len(events)} events (was {before})")

    # ── Source 2: FRED release calendar fallback ───────────────────────────────
    if not events and FRED_API_KEY:
        print(" FF unavailable — trying FRED release calendar...")
        try:
            today = now.strftime("%Y-%m-%d")
            two_weeks = cutoff.strftime("%Y-%m-%d")
            url = (
                f"https://api.stlouisfed.org/fred/releases/dates"
                f"?api_key={FRED_API_KEY}&file_type=json"
                f"&realtime_start={today}&realtime_end={two_weeks}"
                f"&include_release_dates_with_no_data=false&limit=200"
            )
            with _make_request(url) as resp:
                raw = resp.read()
            if raw[:2] == b"\x1f\x8b":
                import gzip
                raw = gzip.decompress(raw)
            data = json.loads(raw)

            release_cb_map = {
                "federal open market committee": ("FED", "US", "FOMC Rate Decision"),
                "fomc": ("FED", "US", "FOMC Rate Decision"),
                "consumer price index": ("FED", "US", "CPI"),
                "employment situation": ("FED", "US", "NFP / Employment"),
                "ecb": ("ECB", "EU", "ECB Policy Decision"),
                "bank of england": ("BOE", "GB", "BOE Rate Decision"),
                "bank of japan": ("BOJ", "JP", "BOJ Rate Decision"),
                "bank of canada": ("BOC", "CA", "BOC Rate Decision"),
                "reserve bank of australia": ("RBA", "AU", "RBA Rate Decision"),
            }

            for rel in data.get("release_dates", []):
                name = rel.get("release_name", "").lower()
                date_str = rel.get("date", "")
                for keyword, (cb, country, label) in release_cb_map.items():
                    if keyword in name:
                        key = f"{label}|{date_str}|{country}"
                        if key in seen:
                            continue
                        seen.add(key)
                        try:
                            dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                        except Exception:
                            dt = None
                        events.append(
                            {
                                "title": label,
                                "date": dt,
                                "date_str": date_str,
                                "country": country,
                                "cb": cb,
                                "impact": "high",
                                "forecast": "",
                                "previous": "",
                                "actual": "",
                            }
                        )
                        break
            print(f" → FRED
