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
            print(f" → FRED fallback: {len(events)} events")
        except Exception as e:
            import traceback

            print(f" → FRED fallback failed: {e}")
            traceback.print_exc()

    if not events:
        print(" ⚠ All calendar sources failed")
    else:
        print(f" ✓ Total calendar events: {len(events)}")

    return sorted(events, key=lambda x: x["date"] or datetime.min.replace(tzinfo=timezone.utc))


def fetch_implied_rate_changes(cb_rates):
    """
    Estimate implied rate change probability using rateprobability.com as primary
    and FRED forward/OIS spread as fallback.
    Returns dict: cb -> {direction, probability, basis_points, source}
    """
    if not FRED_API_KEY:
        # Still allow rateprobability.com even without FRED key
        pass

    implied = {}

    # Primary source: rateprobability.com — uses real OIS/futures pricing per meeting.
    rp_slugs = {"FED": "fed", "ECB": "ecb", "BOE": "boe", "BOJ": "boj", "BOC": "boc", "RBA": "rba"}
    print(" Fetching next-meeting probabilities from rateprobability.com...")
    for cb, slug in rp_slugs.items():
        rp = fetch_rateprobability(slug)
        imp = _rp_to_implied(rp)
        if imp:
            implied[cb] = imp
            if not cb_rates.get(cb):
                # CB rate card fully missing — build from RP
                cb_rates[cb] = _rp_to_cb_rate(rp)
                print(f" CB rate backfilled for {cb} from rateprobability.com: {imp['current_rate']}%")
            else:
                # Patch displayed rate with RP policy rate so both sections agree
                rp_rate = imp["current_rate"]
                fred_rate = cb_rates[cb]["current"]
                if abs(rp_rate - fred_rate) > 0.01:
                    cb_rates[cb]["current"] = rp_rate
                    cb_rates[cb]["label"] = cb_rates[cb].get("label", "") + " (policy)"
                    print(f" CB rate patched for {cb}: FRED proxy {fred_rate}% → RP policy {rp_rate}%")

    # Fallback: FRED spread for any CB that RP couldn't load
    FORWARD_PROXIES = {
        "FED": {"fwd": ["DTB3"], "label": "3M T-Bill"},
        "ECB": {"fwd": ["IR3TIB01EZM156N"], "label": "3M Euribor"},
        "BOE": {"fwd": ["IR3TIB01GBM156N", "IR3TBB01GBM156N"], "label": "3M GBP Interbank"},
        "BOJ": {"fwd": ["IR3TIB01JPM156N"], "label": "3M JPY Tibor"},
        "BOC": {"fwd": ["IR3TIB01CAM156N", "IR3TBB01CAM156N", "TB3MS"], "label": "3M CAD Interbank"},
        "RBA": {"fwd": ["IR3TIB01AUM156N", "IR3TBB01AUM156N"], "label": "3M AUD Interbank"},
    }

    rp_missing = [cb for cb in FORWARD_PROXIES if cb not in implied]
    if rp_missing:
        print(f" RP missing for {rp_missing} — using FRED spread fallback...")
        for cb in rp_missing:
            series = FORWARD_PROXIES[cb]
            try:
                now_rate = cb_rates.get(cb)
                if not now_rate:
                    continue
                fwd_data = None
                fwd_label = series["label"]
                for fwd_id in series["fwd"]:
                    fwd_data = fetch_fred_series(fwd_id)
                    if fwd_data:
                        fwd_label = f"{series['label']} ({fwd_id})"
                        break
                if not fwd_data:
                    continue
                current = now_rate["current"]
                forward = fwd_data["current"]
                spread_bp = (forward - current) * 100
                if abs(spread_bp) < 5:
                    direction, probability = "hold", 0
                elif spread_bp > 0:
                    direction = "hike"
                    probability = min(int((spread_bp / 25) * 100), 95)
                else:
                    direction = "cut"
                    probability = min(int((abs(spread_bp) / 25) * 100), 95)
                implied[cb] = {
                    "direction": direction,
                    "probability": probability,
                    "spread_bp": round(spread_bp, 1),
                    "forward_rate": round(forward, 3),
                    "current_rate": round(current, 3),
                    "fwd_label": fwd_label,
                }
                print(f" Implied {cb} (FRED): {direction} {probability}% ({spread_bp:+.1f}bp via {fwd_label})")
            except Exception as e:
                print(f" Implied rate error ({cb}): {e}")

    return implied


# ── Risk scoring helpers (NEW in v1) ───────────────────────────────────────────

def _event_volatility_score(event, days_away, affected_pairs, affected_bots):
    """
    Simple heuristic 0–100 event volatility score:
    - base by impact (high/medium)
    - +time proximity (0 days away = strongest)
    - +event type weight (rates/inflation/labor > GDP > sentiment/etc.)
    - scaled by how many portfolio pairs/bots are touched
    """
    # Impact base
    impact = event.get("impact", "medium")
    base = 70 if impact == "high" else 45

    # Time decay: same day = +25, 1–2d = +15, 3–5d = +8, 6–7d = +3
    if days_away <= 0:
        time_bonus = 25
    elif days_away <= 2:
        time_bonus = 15
    elif days_away <= 5:
        time_bonus = 8
    else:
        time_bonus = 3

    title = (event.get("title") or "").lower()

    # Type weight
    type_bonus = 0
    if any(k in title for k in ["rate decision", "rate statement", "fomc", "policy"]):
        type_bonus = 20
    elif any(k in title for k in ["cpi", "inflation", "pce"]):
        type_bonus = 18
    elif any(k in title for k in ["employment", "unemployment", "job", "payroll", "nfp"]):
        type_bonus = 16
    elif any(k in title for k in ["gdp"]):
        type_bonus = 12
    elif any(k in title for k in ["pmis", "manufacturing", "services"]):
        type_bonus = 8
    else:
        type_bonus = 5  # generic macro

    # Portfolio exposure: more pairs/bots → higher score
    exposure_factor = min(len(affected_pairs), 3) * 4 + min(len(affected_bots), 3) * 5

    raw = base + time_bonus + type_bonus + exposure_factor
    return max(0, min(int(raw), 100))


def _bot_risk_indices(alerts):
    """
    Aggregate event scores into per-bot risk indices for 24h and 72h windows.
    Simple capped sum with decay by horizon and bot-specific weight.
    """
    risk = {bot: {"24h": 0.0, "72h": 0.0} for bot in PORTFOLIO.keys()}
    now = datetime.now(timezone.utc)

    for a in alerts:
        score = a.get("score", 0)
        if score <= 0:
            continue
        event_dt = a.get("_dt")
        if not event_dt or not isinstance(event_dt, datetime):
            continue
        delta_hours = (event_dt - now).total_seconds() / 3600.0
        if delta_hours < 0:
            continue

        for bot in a.get("bots", []):
            w = BOT_SEVERITY_WEIGHTS.get(bot, 1.0)
            if delta_hours <= 24:
                risk[bot]["24h"] += score * w
            if delta_hours <= 72:
                # 72h bucket includes 24h too (no double weighting)
                risk[bot]["72h"] += score * w * 0.7

    # Normalise to 0–100 with simple cap
    for bot in risk:
        for horizon in ("24h", "72h"):
            risk[bot][horizon] = int(min(risk[bot][horizon], 100))

    return risk


def compute_alerts(cb_rates, upcoming_events, implied_moves):
    """Generate volatility alerts for events in the next 7 days affecting portfolio pairs."""
    alerts = []
    now = datetime.now(timezone.utc)
    next_7d = now + timedelta(days=7)

    # ── Confirmed alerts: events in next 7 days ──
    for event in upcoming_events:
        if not event["date"] or not (now <= event["date"] <= next_7d):
            continue

        affected_pairs = [p for p, cbs in PAIR_CB_MAP.items() if event["cb"] in cbs]
        affected_bots = [bot for bot, cfg in PORTFOLIO.items() if any(p in affected_pairs for p in cfg["pairs"])]
        if not affected_pairs:
            continue

        days_away = max((event["date"] - now).days, 0)
        severity = "critical" if days_away <= 1 else ("high" if days_away <= 3 else "medium")

        # Pause recommendation based on severity + event type
        is_rate_decision = any(
            kw in event["title"].lower()
            for kw in ["rate", "decision", "policy", "statement", "minutes", "nfp", "non-farm", "cpi", "inflation"]
        )
        pause_rec = ""
        if severity == "critical" and is_rate_decision:
            pause_rec = "CONSIDER PAUSING"
        elif severity == "high" and is_rate_decision:
            pause_rec = "MONITOR CLOSELY"

        # NEW: event volatility score
        score = _event_volatility_score(event, days_away, affected_pairs, affected_bots)

        alerts.append(
            {
                "event": event["title"],
                "cb": event["cb"],
                "date": event["date"].strftime("%a %b %d, %H:%M UTC"),
                "_dt": event["date"],  # for risk computation
                "days_away": days_away,
                "pairs": affected_pairs,
                "bots": affected_bots,
                "severity": severity,
                "impact": event["impact"],
                "forecast": event["forecast"],
                "previous": event["previous"],
                "pause_rec": pause_rec,
                "window": "7d",
                "score": score,
            }
        )

    # ── Structural divergence alerts ──
    loaded = {k: v for k, v in cb_rates.items() if v}
    for pair, cbs in PAIR_CB_MAP.items():
        if len(cbs) == 2 and all(c in loaded for c in cbs):
            a, b = loaded[cbs[0]], loaded[cbs[1]]
            if a["trend"] != b["trend"] and "holding" not in [a["trend"], b["trend"]]:
                affected_bots = [bot for bot, cfg in PORTFOLIO.items() if pair in cfg["pairs"]]
                alerts.append(
                    {
                        "event": f"Policy Divergence: {cbs[0]} {a['trend']} vs {cbs[1]} {b['trend']}",
                        "cb": f"{cbs[0]}/{cbs[1]}",
                        "date": "Structural / Ongoing",
                        "_dt": None,
                        "days_away": 998,
                        "pairs": [pair],
                        "bots": affected_bots,
                        "severity": "medium",
                        "impact": "structural",
                        "forecast": "",
                        "previous": "",
                        "pause_rec": "",
                        "window": "7d",
                        "score": 40,
                    }
                )

    return alerts


# ── HTML Generation ────────────────────────────────────────────────────────────

def trend_arrow(trend):
    if trend == "hiking":
        return "▲"
    if trend == "cutting":
        return "▼"
    return "◆"


def severity_class(s):
    return {"critical": "sev-critical", "high": "sev-high", "medium": "sev-medium"}.get(s, "sev-medium")


def generate_sparkline(history, css_class="spark-flat"):
    if not history or len(history) < 2:
        return ""
    try:
        vals = [v for _, v in history]
    except (TypeError, ValueError):
        return ""
    mn, mx = min(vals), max(vals)
    rng = mx - mn or 0.01
    w, h = 200, 36
    n = len(vals)
    pts = " ".join(
        f"{int(i * (w / max(n - 1, 1)))},{int(h - (v - mn) / rng * (h - 4) + 2)}"
        for i, v in enumerate(vals)
    )
    return (
        f'<svg class="spark {css_class}" viewBox="0 0 {w} {h}" preserveAspectRatio="none">'
        f'<polyline points="{pts}" />'
        f"</svg>"
    )


def generate_html(cb_rates, events, alerts, implied_moves, bot_risk):
    now_str = datetime.now(timezone.utc).strftime("%A, %B %d %Y — %H:%M UTC")
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── CB Rate Cards ──
    cb_cards_html = ""
    for cb, info in FRED_SERIES.items():
        rate = cb_rates.get(cb)
        if rate:
            val = f"{rate['current']:.2f}%"
            prev = f"{rate['previous']:.2f}%"
            arrow = trend_arrow(rate["trend"])
            trend_cls = {
                "hiking": "trend-up",
                "cutting": "trend-down",
                "holding": "trend-flat",
            }.get(rate["trend"], "trend-flat")
            spark_cls = {
                "hiking": "spark-up",
                "cutting": "spark-down",
                "holding": "spark-flat",
            }.get(rate["trend"], "spark-flat")
            sparkline = generate_sparkline(rate.get("history", []), spark_cls)
            as_of = rate.get("date", "")
        else:
            val = "N/A"
            prev = "–"
            arrow = ""
            trend_cls = "trend-flat"
            sparkline = ""
            as_of = ""

        cb_cards_html += f"""
        <article class="card cb-card {trend_cls}">
            <header class="cb-header">
                <div class="cb-flag">{info['flag']}</div>
                <div class="cb-meta">
                    <div class="cb-code">{info['currency']} {cb}</div>
                    <div class="cb-trend">{arrow}</div>
                </div>
            </header>
            <div class="cb-body">
                <div class="cb-rate">{val}</div>
                <div class="cb-name">{info['name']}</div>
                <div class="cb-prev">Prev: {prev}</div>
                <div class="cb-spark">
                    {sparkline}
                    <div class="spark-label">6-month rate history</div>
                    <div class="spark-date">as of {as_of}</div>
                </div>
            </div>
        </article>
        """

    # ── Implied Rate Probability Cards ──
    implied_html = ""
    if not implied_moves:
        implied_html = (
            "Market-implied probability data unavailable — forward rate series may not have loaded. "
            "Check FRED API key and connectivity."
        )
    else:
        for cb, imp in implied_moves.items():
            dir_cls = imp["direction"]
            pct = imp["probability"]
            fill_cls = {"hike": "imp-hike", "cut": "imp-cut", "hold": "imp-hold"}.get(dir_cls, "imp-hold")
            pct_cls = {"hike": "hike", "cut": "cut", "hold": "hold"}.get(dir_cls, "hold")
            dir_label = {"hike": "▲ HIKE", "cut": "▼ CUT", "hold": "◆ HOLD"}.get(dir_cls, dir_cls.upper())
            # For hold: show hold confidence (100 - move prob). For hike/cut: show move prob.
            display_pct = (100 - pct) if dir_cls == "hold" else pct
            display_pct = min(int(display_pct), 99)
            next_mtg = imp.get("next_meeting", "")
            next_mtg_str = f"📅 {next_mtg} · " if next_mtg else ""
            source_str = imp.get("fwd_label", "")
            implied_html += f"""
            <article class="card implied-card {fill_cls}">
                <header class="implied-header">
                    <div class="implied-cb">{cb}</div>
                    <div class="implied-label">Probability of next move</div>
                </header>
                <div class="implied-body">
                    <div class="implied-dir {pct_cls}">{dir_label} {display_pct}%</div>
                    <div class="implied-rates">
                        Current: {imp['current_rate']}% → Fwd: {imp['forward_rate']}% ({imp['spread_bp']:+.1f}bp)
                    </div>
                    <div class="implied-meta">
                        {next_mtg_str}{source_str}
                    </div>
                </div>
            </article>
            """

    # ── Alert Cards (0-7 days) ──
    alert_html = ""
    real_alerts = [a for a in alerts if a.get("days_away", 999) < 998]
    struct_alerts = [a for a in alerts if a.get("days_away", 999) >= 998]
    if not real_alerts and not struct_alerts:
        alert_html = "✓ No high-impact events in the next 7 days affecting your portfolio."
    else:
        for a in sorted(real_alerts + struct_alerts, key=lambda x: x["days_away"]):
            pairs_str = " ".join(f"{p}" for p in a["pairs"])
            bots_str = " ".join(f"{b}" for b in a["bots"])
            fc_str = f"Forecast: {a['forecast']}" if a.get("forecast") else ""
            pr_str = f"Prev: {a['previous']}" if a.get("previous") else ""
            pause_html = f"{a['pause_rec']}" if a.get("pause_rec") else ""
            score_str = f"Score {a.get('score', 0)}/100"
            alert_html += f"""
            <article class="card alert-card {severity_class(a['severity'])}">
                <header class="alert-header">
                    <div class="alert-cb">{a['cb']}</div>
                    <div class="alert-sev">{a['severity'].upper()}</div>
                    <div class="alert-score">{score_str}</div>
                </header>
                <div class="alert-body">
                    <div class="alert-title">{a['event']}</div>
                    <div class="alert-date">📅 {a['date']}</div>
                    <div class="alert-forecast">{fc_str}{pr_str}</div>
                    <div class="alert-pairs">Pairs at risk: {pairs_str}</div>
                    <div class="alert-bots">Exposed bots: {bots_str}</div>
                    <div class="alert-pause">{pause_html}</div>
                </div>
            </article>
            """

    # ── Event Calendar ──
    cal_rows = ""
    shown = 0
    now_utc = datetime.now(timezone.utc)
    for e in events[:40]:
        if not e["date"]:
            continue
        # Skip events that have already passed
        if e["date"] < now_utc - timedelta(hours=1):
            continue
        impact_cls = "imp-high" if e["impact"] == "high" else "imp-med"
        cb_affected = any(
            e["cb"] in PAIR_CB_MAP.get(p, [])
            for bot in PORTFOLIO.values()
            for p in bot["pairs"]
        )
        row_cls = "row-highlight" if cb_affected else ""
        is_past = e["date"] < now_utc
        if e["actual"]:
            actual_str = f"{e['actual']}"
        elif is_past:
            actual_str = "–"
        else:
            actual_str = "pending"
        cal_rows += f"""
        <tr class="{row_cls}">
            <td>{e['date'].strftime('%a %b %d') if e['date'] else '–'}</td>
            <td>{e['date'].strftime('%H:%M') if e['date'] else '–'}</td>
            <td>{e['country']}</td>
            <td>{e['title']}</td>
            <td class="{impact_cls}">{e['impact'].upper()}</td>
            <td>{e['forecast'] or '–'}</td>
            <td>{e['previous'] or '–'}</td>
            <td>{actual_str}</td>
        </tr>
        """
        shown += 1

    if not shown:
        cal_rows = (
            "No events loaded this run — Forex Factory feed may be temporarily unavailable. "
            "Data will appear on the next scheduled refresh."
        )

    # ── Portfolio Pair Map ──
    pair_rows = ""
    for pair in ALL_PAIRS:
        cbs = PAIR_CB_MAP.get(pair, [])
        bots = [bot for bot, cfg in PORTFOLIO.items() if pair in cfg["pairs"]]
        has_alert = any(a for a in alerts if pair in a["pairs"] and a["days_away"] < 999)
        risk_cls = "risk-alert" if has_alert else "risk-ok"
        cbs_html = " ".join(f"{c}" for c in cbs)
        bots_html = " ".join(f"{b}" for b in bots)
        warn_icon = "⚠️" if has_alert else "✓"
        pair_rows += f"""
        <tr class="{risk_cls}">
            <td>{pair}</td>
            <td>{cbs_html}</td>
            <td>{bots_html}</td>
            <td>{warn_icon}</td>
        </tr>
        """

    # ── Bot risk banner (NEW) ──
    # Build a simple line like: Control 42/68 · Jet 75/90 · ...
    parts = []
    for bot, scores in bot_risk.items():
        parts.append(f"{bot} {scores['24h']}/{scores['72h']}")
    banner_text = " · ".join(parts) if parts else "No active macro risk computed."
    risk_banner_html = f"""
    <section class="risk-banner">
        <div class="risk-title">Today's macro risk (24h/72h):</div>
        <div class="risk-values">{banner_text}</div>
    </section>
    """

    # ── Full HTML ──
    html = f"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
    <meta charset="utf-8" />
    <title>Macro Dashboard — FX Bot Portfolio</title>
    <meta name="description" content="Macro intel dashboard for an algorithmic FX portfolio — central bank policy, market-implied moves, and high-impact events mapped to active bots." />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta name="generated-at" content="{now_ts}" />
    <style>
        /* (keep your existing CSS here; omitted for brevity in this snippet) */
    </style>
</head>
<body>
    <main class="layout">
        <header class="topbar">
            <div class="topbar-left">
                <h1>Macro Intel Dashboard</h1>
                <p>Central bank policy &amp; macro risk overlay for your FX bots</p>
            </div>
            <div class="topbar-right">
                <div class="ts-label">Last updated (UTC)</div>
                <div class="ts-value">{now_str}</div>
            </div>
        </header>

        {risk_banner_html}

        <section class="grid cb-grid">
            <h2>Central Bank Policy Rates</h2>
            <div class="cb-grid-inner">
                {cb_cards_html}
            </div>
        </section>

        <section class="grid implied-grid">
            <h2>📊 Market-Implied Rate Move Probability</h2>
            <div class="implied-grid-inner">
                {implied_html}
            </div>
        </section>

        <section class="grid alerts-grid">
            <h2>⚠ Portfolio Volatility Alerts — Next 7 Days</h2>
            <div class="alerts-grid-inner">
                {alert_html}
            </div>
        </section>

        <section class="grid exposure-grid">
            <h2>Portfolio Pair Exposure Map</h2>
            <table class="exposure-table">
                <thead>
                    <tr>
                        <th>Pair</th>
                        <th>Central Banks</th>
                        <th>Active Bots</th>
                        <th>Alert</th>
                    </tr>
                </thead>
                <tbody>
                    {pair_rows}
                </tbody>
            </table>
        </section>

        <section class="grid calendar-grid">
            <h2>High-Impact Economic Calendar — Next 14 Days</h2>
            <table class="calendar-table">
                <thead>
                    <tr>
                        <th>Date</th>
                        <th>Time (UTC)</th>
                        <th>Country</th>
                        <th>Event</th>
                        <th>Impact</th>
                        <th>Forecast</th>
                        <th>Previous</th>
                        <th>Actual</th>
                    </tr>
                </thead>
                <tbody>
                    {cal_rows}
                </tbody>
            </table>
        </section>
    </main>
</body>
</html>
"""
    return html


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Starting macro dashboard generation...")

    # 1. Fetch CB rates — try FRED candidates, then rateprobability.com as fallback
    print(" Fetching central bank rates from FRED...")
    cb_rates = {}
    rp_slugs = {"FED": "fed", "ECB": "ecb", "BOE": "boe", "BOJ": "boj", "BOC": "boc", "RBA": "rba"}
    for cb, info in FRED_SERIES.items():
        result = None
        for sid in info["ids"]:
            print(f" → {cb} trying {sid}")
            result = fetch_fred_series(sid)
            if result:
                print(f" → ✓ {cb} loaded from {sid}: {result['current']}%")
                break
        if not result:
            # Fallback: pull current rate from rateprobability.com
            slug = rp_slugs.get(cb)
            if slug:
                print(f" → {cb} FRED failed — trying rateprobability.com...")
                rp = fetch_rateprobability(slug)
                result = _rp_to_cb_rate(rp)
                if result:
                    print(f" → ✓ {cb} loaded from rateprobability.com: {result['current']}%")
        cb_rates[cb] = result
    loaded = sum(1 for v in cb_rates.values() if v)
    print(f" Loaded {loaded}/{len(FRED_SERIES)} CB rates")

    # 2. Fetch market-implied rate changes
    print(" Fetching market-implied rate probabilities...")
    implied_moves = fetch_implied_rate_changes(cb_rates)
    print(f" Loaded {len(implied_moves)} implied rate estimates")

    # 3. Fetch calendar (this week + next week = ~14 days)
    print(" Fetching Forex Factory calendar (2-week window)...")
    events = fetch_forex_factory_calendar()
    print(f" Found {len(events)} high/medium-impact events")

    # 4. Compute alerts and outlook
    alerts = compute_alerts(cb_rates, events, implied_moves)
    print(f" Generated {len(alerts)} alerts")

    # 4b. Compute bot risk indices (NEW)
    bot_risk = _bot_risk_indices(alerts)
    print(" Bot risk indices:", bot_risk)

    # 5. Generate HTML
    print(" Generating HTML dashboard...")
    html = generate_html(cb_rates, events, alerts, implied_moves, bot_risk)

    # 6. Write output
    out_path = os.path.join(os.path.dirname(__file__), "docs", "index.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f" ✓ Dashboard written to {out_path}")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Done.")


if __name__ == "__main__":
    main()
