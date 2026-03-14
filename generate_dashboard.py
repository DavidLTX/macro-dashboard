#!/usr/bin/env python3
"""
Macro Dashboard Generator
Pulls central bank rates, economic events, and generates an HTML dashboard
tailored to the active forex bot portfolio.
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
    "Jet":     {"pairs": ["EURUSD", "EURGBP"], "strategy": "Dual-Dir Grid", "color": "#22c55e"},
    "HGold":   {"pairs": ["XAUUSD"], "strategy": "Fixed-Lot Scalper", "color": "#fcd34d"},
    "Hedge":   {"pairs": ["AUDCAD"], "strategy": "Dual-Dir Grid", "color": "#2dd4bf"},
}

# All unique pairs across portfolio
ALL_PAIRS = sorted(set(p for bot in PORTFOLIO.values() for p in bot["pairs"]))

# Pair → central banks that drive volatility
PAIR_CB_MAP = {
    "EURJPY":  ["ECB", "BOJ"],
    "USDCAD":  ["FED", "BOC"],
    "EURGBP":  ["ECB", "BOE"],
    "EURUSD":  ["ECB", "FED"],
    "AUDCAD":  ["RBA", "BOC"],
    "XAUUSD":  ["FED"],
}

# FRED series for central bank policy rates
FRED_SERIES = {
    # Each entry has a list of candidate series IDs tried in order until one works.
    # Candidates verified against FRED catalogue as of early 2026.
    "FED": {
        "ids": ["FEDFUNDS"],
        "name": "Fed Funds Rate", "currency": "USD", "flag": "🇺🇸",
    },
    "ECB": {
        "ids": ["ECBDFR"],
        "name": "ECB Deposit Rate", "currency": "EUR", "flag": "🇪🇺",
    },
    "BOE": {
        "ids": ["IUDSOIA", "BOEBR"],
        "name": "BOE Base Rate", "currency": "GBP", "flag": "🇬🇧",
    },
    "BOJ": {
        # BOJ near-zero rate: use overnight call rate or 3M Tibor as proxy
        "ids": ["IRSTCB01JPM156N", "IR3TIB01JPM156N", "INTGSTJPM193N"],
        "name": "BOJ Policy Rate", "currency": "JPY", "flag": "🇯🇵",
    },
    "BOC": {
        # BOC overnight rate target
        "ids": ["IRSTCB01CAM156N", "INTGSTCAM193N", "IR3TBB01CAM156N"],
        "name": "BOC Policy Rate", "currency": "CAD", "flag": "🇨🇦",
    },
    "RBA": {
        # RBA cash rate target
        "ids": ["IRSTCB01AUM156N", "INTGSTAUM193N", "IR3TIB01AUM156N"],
        "name": "RBA Cash Rate", "currency": "AUD", "flag": "🇦🇺",
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
        all_dates  = [o["date"] for o in obs]
        current  = all_values[0]
        prev     = all_values[1] if len(all_values) > 1 else current
        # Sparkline = most recent 6 data points
        spark_vals  = all_values[:6]
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
        print(f"  FRED error ({series_id}): {e}")
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
        if raw[:2] == b'\x1f\x8b':
            import gzip
            raw = gzip.decompress(raw)
        data = _json.loads(raw)
        print(f"  rateprobability.com API ({cb_key}): {len(raw)} bytes, keys={list(data.keys())[:6]}")
    except Exception as e:
        print(f"  rateprobability.com ({cb_key}): API error — {e}")
        return None

    result = {"cb": cb_key.upper(), "source": "rateprobability.com", "meetings": []}

    # All data is nested under data['today']
    today = data.get("today", {})
    if not today:
        print(f"  rateprobability.com ({cb_key}): no 'today' key in response")
        return None

    # Current rate — field name varies per CB
    CB_RATE_KEYS = [
        "cash_rate_target",       # RBA
        "Overnight Rate Target",  # BOC (capitalised)
        "current_target",         # BOE, BOJ
        "ecb_deposit_facility",   # ECB
        "depo_reported",          # ECB fallback
        "midpoint",               # FED
        "most_recent_effr",       # FED fallback
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
        print(f"  rateprobability.com ({cb_key}): could not find rate. today keys: {list(today.keys())[:10]}")
        return None
    result["current_rate"] = current_rate

    # Rows are under today['rows']
    meetings_data = today.get("rows", [])
    if not meetings_data:
        print(f"  rateprobability.com ({cb_key}): no rows in today. Keys: {list(today.keys())}")
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
        result["meetings"].append({
            "date": dt, "date_str": dt.strftime("%b %d, %Y"),
            "implied_rate": implied_rate, "probability": prob,
            "direction": direction, "delta_bp": real_delta,
        })

    if not result["meetings"]:
        print(f"  rateprobability.com ({cb_key}): no future meetings in response. Sample row: {meetings_data[0] if meetings_data else None}")
        return None

    next_mtg = result["meetings"][0]
    result.update({
        "next_meeting_date":      next_mtg["date_str"],
        "next_meeting_direction": next_mtg["direction"],
        "next_meeting_prob":      next_mtg["probability"],
        "next_implied_rate":      next_mtg["implied_rate"],
        "delta_bp":               next_mtg["delta_bp"],
    })
    print(f"  rateprobability.com ({cb_key.upper()}): {result['current_rate']}% → "
          f"{next_mtg['direction']} {next_mtg['probability']}% @ {next_mtg['date_str']} "
          f"(Δ{next_mtg['delta_bp']:+.1f}bp)")
    return result


def _rp_to_implied(rp):
    """Convert fetch_rateprobability() result → implied_moves dict format."""
    if not rp:
        return None
    return {
        "direction":    rp["next_meeting_direction"],
        "probability":  min(int(rp["next_meeting_prob"]), 95),
        "spread_bp":    round(rp["delta_bp"], 1),
        "forward_rate": round(rp["next_implied_rate"], 3),
        "current_rate": round(rp["current_rate"], 3),
        "fwd_label":    f"rateprobability.com",
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
            "cutting" if future_rate < current - 0.05 else "holding")
    # Build a sparkline-compatible history from implied path
    history = [(m["date_str"], m["implied_rate"]) for m in meetings[:6]]
    if not history:
        history = [(datetime.now().strftime("%Y-%m-%d"), current)]
    return {
        "current":  current,
        "previous": current,
        "trend":    trend,
        "history":  history,
        "date":     datetime.now().strftime("%Y-%m-%d"),
        "source":   "rateprobability.com",
    }

def _parse_ff_xml(content, seen):
    """
    Parse Forex Factory XML. FF uses <weeklyevents><event> structure (not RSS).
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
        # Fix the XML declaration to say utf-8
        content = content.replace(
            b'<?xml version="1.0" encoding="windows-1252"?>',
            b'<?xml version="1.0" encoding="utf-8"?>'
        )

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
    high_impact   = {"high", "holiday"}   # holiday = market closed = relevant
    medium_impact = {"medium"}

    for event in root.findall("event"):
        title    = (event.findtext("title")   or "").strip()
        country  = (event.findtext("country") or "").strip().upper()
        date_str = (event.findtext("date")    or "").strip()
        time_str = (event.findtext("time")    or "").strip()
        impact   = (event.findtext("impact")  or "").strip().lower()
        forecast = (event.findtext("forecast") or "").strip()
        previous = (event.findtext("previous") or "").strip()
        actual   = (event.findtext("actual")   or "").strip()

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

        events.append({
            "title": title, "date": dt,
            "date_str": dt.strftime("%Y-%m-%d") if dt else date_str,
            "country": country_code, "cb": cb, "impact": impact_norm,
            "forecast": forecast, "previous": previous, "actual": actual,
        })

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
        "Accept-Encoding": "identity",   # explicitly request no compression
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
    seen   = set()
    now    = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=14)

    # ── Source 1: Forex Factory XML ────────────────────────────────────────────
    ff_feeds = [
        ("thisweek", "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"),
        ("thisweek", "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.xml"),
        # nextweek — try both CDN variants; available Fri/Sat, 404 Mon-Thu is normal
        ("nextweek", "https://nfs.faireconomy.media/ff_calendar_nextweek.xml"),
        ("nextweek", "https://cdn-nfs.faireconomy.media/ff_calendar_nextweek.xml"),
    ]
    fetched_weeks = set()

    for week_key, url in ff_feeds:
        if week_key in fetched_weeks:
            continue
        try:
            print(f"    FF XML: {url}")
            with _make_request(url) as resp:
                raw = resp.read()
            print(f"    → {len(raw)} bytes")
            if raw[:2] == b'\x1f\x8b':
                import gzip
                raw = gzip.decompress(raw)
            parsed = _parse_ff_xml(raw, seen)
            if parsed:
                events.extend(parsed)
                fetched_weeks.add(week_key)
                print(f"    → ✓ {len(parsed)} events ({week_key})")
            else:
                print(f"    → 0 events parsed. XML preview: {raw[:300]}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f"    → 404 (not available yet this cycle)")
            else:
                print(f"    → HTTP {e.code} — skipping")
        except urllib.error.URLError as e:
            print(f"    → URLError: {e.reason} — skipping")
        except Exception as e:
            print(f"    → {type(e).__name__}: {e}")

    # Keep only future events (next 14 days); drop anything already passed
    before = len(events)
    events = [e for e in events if e["date"] and now <= e["date"] <= cutoff]
    print(f"    → {len(events)} upcoming events in next 14 days (was {before} total in feed)")
    if len(events) == 0:
        weekday = now.weekday()  # 0=Mon, 5=Sat, 6=Sun
        if weekday < 4:  # Mon-Thu
            print("    ⚠ All thisweek events are past. nextweek feed not yet published (normal Mon-Thu).")
        else:
            print("    ⚠ No future events found — both feeds may be unavailable or all events passed.")

    # ── Source 2: FRED release calendar fallback ───────────────────────────────
    if not events and FRED_API_KEY:
        print("    FF unavailable — trying FRED release calendar...")
        try:
            today     = now.strftime("%Y-%m-%d")
            two_weeks = cutoff.strftime("%Y-%m-%d")
            url = (
                f"https://api.stlouisfed.org/fred/releases/dates"
                f"?api_key={FRED_API_KEY}&file_type=json"
                f"&realtime_start={today}&realtime_end={two_weeks}"
                f"&include_release_dates_with_no_data=false&limit=200"
            )
            with _make_request(url) as resp:
                raw = resp.read()
            if raw[:2] == b'\x1f\x8b':
                import gzip
                raw = gzip.decompress(raw)
            data = json.loads(raw)

            release_cb_map = {
                "federal open market committee": ("FED", "US", "FOMC Rate Decision"),
                "fomc":                          ("FED", "US", "FOMC Rate Decision"),
                "consumer price index":          ("FED", "US", "CPI"),
                "employment situation":          ("FED", "US", "NFP / Employment"),
                "ecb":                           ("ECB", "EU", "ECB Policy Decision"),
                "bank of england":               ("BOE", "GB", "BOE Rate Decision"),
                "bank of japan":                 ("BOJ", "JP", "BOJ Rate Decision"),
                "bank of canada":                ("BOC", "CA", "BOC Rate Decision"),
                "reserve bank of australia":     ("RBA", "AU", "RBA Rate Decision"),
            }
            for rel in data.get("release_dates", []):
                name     = rel.get("release_name", "").lower()
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
                        events.append({
                            "title": label, "date": dt, "date_str": date_str,
                            "country": country, "cb": cb, "impact": "high",
                            "forecast": "", "previous": "", "actual": "",
                        })
                        break
            print(f"    → FRED fallback: {len(events)} events")
        except Exception as e:
            import traceback
            print(f"    → FRED fallback failed: {e}")
            traceback.print_exc()

    if not events:
        print("  ⚠ All calendar sources failed")
    else:
        print(f"  ✓ Total calendar events: {len(events)}")

    return sorted(events, key=lambda x: x["date"] or datetime.min.replace(tzinfo=timezone.utc))


def fetch_implied_rate_changes(cb_rates):
    """
    Estimate implied rate change probability using FRED forward/OIS spread data.
    Returns dict: cb -> {direction, probability, basis_points, source}

    Method: compare current policy rate vs 3-month OIS or forward rate.
    A spread > +10bp implies market pricing a hike; < -10bp implies a cut.
    Probability is approximated from the spread magnitude vs typical 25bp move.
    """
    if not FRED_API_KEY:
        return {}

    # Forward rate proxies: use the already-fetched cb_rates for the policy rate,
    # and try candidate forward series in order until one returns data.
    FORWARD_PROXIES = {
        "FED": {"fwd": ["DTB3"],                                        "label": "3M T-Bill"},
        "ECB": {"fwd": ["IR3TIB01EZM156N"],                             "label": "3M Euribor"},
        "BOE": {"fwd": ["IR3TIB01GBM156N", "IR3TBB01GBM156N"],         "label": "3M GBP Interbank"},
        "BOJ": {"fwd": ["IR3TIB01JPM156N"],                             "label": "3M JPY Tibor"},
        "BOC": {"fwd": ["IR3TIB01CAM156N", "IR3TBB01CAM156N", "TB3MS"],"label": "3M CAD Interbank"},
        "RBA": {"fwd": ["IR3TIB01AUM156N", "IR3TBB01AUM156N"],         "label": "3M AUD Interbank"},
    }

    implied = {}

    # Primary source: rateprobability.com — uses real OIS/futures pricing per meeting.
    # FRED 3M spreads are kept as fallback but often show 0bp for BOJ/RBA (misleading).
    rp_slugs = {"FED": "fed", "ECB": "ecb", "BOE": "boe",
                "BOJ": "boj", "BOC": "boc", "RBA": "rba"}
    print("  Fetching next-meeting probabilities from rateprobability.com...")
    for cb, slug in rp_slugs.items():
        rp = fetch_rateprobability(slug)
        imp = _rp_to_implied(rp)
        if imp:
            implied[cb] = imp
            if not cb_rates.get(cb):
                # CB rate card fully missing — build from RP
                cb_rates[cb] = _rp_to_cb_rate(rp)
                print(f"  CB rate backfilled for {cb} from rateprobability.com: {imp['current_rate']}%")
            else:
                # CB rate card exists from FRED (may be interbank proxy) —
                # patch displayed rate with RP policy rate so both sections agree
                rp_rate = imp["current_rate"]
                fred_rate = cb_rates[cb]["current"]
                if abs(rp_rate - fred_rate) > 0.01:
                    cb_rates[cb]["current"] = rp_rate
                    cb_rates[cb]["label"]   = cb_rates[cb].get("label", "") + " (policy)"
                    print(f"  CB rate patched for {cb}: FRED proxy {fred_rate}% → RP policy {rp_rate}%")

    # Fallback: FRED spread for any CB that RP couldn't load
    rp_missing = [cb for cb in FORWARD_PROXIES if cb not in implied]
    if rp_missing:
        print(f"  RP missing for {rp_missing} — using FRED spread fallback...")
    for cb in rp_missing:
        series = FORWARD_PROXIES[cb]
        try:
            now_rate = cb_rates.get(cb)
            if not now_rate:
                continue
            fwd_data  = None
            fwd_label = series["label"]
            for fwd_id in series["fwd"]:
                fwd_data = fetch_fred_series(fwd_id)
                if fwd_data:
                    fwd_label = f"{series['label']} ({fwd_id})"
                    break
            if not fwd_data:
                continue
            current   = now_rate["current"]
            forward   = fwd_data["current"]
            spread_bp = (forward - current) * 100
            if abs(spread_bp) < 5:
                direction, probability = "hold", 0
            elif spread_bp > 0:
                direction   = "hike"
                probability = min(int((spread_bp / 25) * 100), 95)
            else:
                direction   = "cut"
                probability = min(int((abs(spread_bp) / 25) * 100), 95)
            implied[cb] = {
                "direction": direction, "probability": probability,
                "spread_bp": round(spread_bp, 1), "forward_rate": round(forward, 3),
                "current_rate": round(current, 3), "fwd_label": fwd_label,
            }
            print(f"  Implied {cb} (FRED): {direction} {probability}% ({spread_bp:+.1f}bp via {fwd_label})")
        except Exception as e:
            print(f"  Implied rate error ({cb}): {e}")

    return implied


def compute_alerts(cb_rates, upcoming_events, implied_moves):
    """Generate volatility alerts for events in the next 7 days affecting portfolio pairs."""
    alerts   = []
    now      = datetime.now(timezone.utc)
    next_7d  = now + timedelta(days=7)

    # ── Confirmed alerts: events in next 7 days ──
    for event in upcoming_events:
        if not event["date"] or not (now - timedelta(minutes=30) <= event["date"] <= next_7d):
            continue

        affected_pairs = [p for p, cbs in PAIR_CB_MAP.items() if event["cb"] in cbs]
        affected_bots  = [bot for bot, cfg in PORTFOLIO.items() if any(p in affected_pairs for p in cfg["pairs"])]
        if not affected_pairs:
            continue

        days_away = max((event["date"] - now).days, 0)
        severity  = "critical" if days_away <= 1 else ("high" if days_away <= 3 else "medium")

        # Pause recommendation based on severity + event type
        is_rate_decision = any(kw in event["title"].lower() for kw in
                               ["rate", "decision", "policy", "statement", "minutes", "nfp", "non-farm", "cpi", "inflation"])
        pause_rec = ""
        if severity == "critical" and is_rate_decision:
            pause_rec = "CONSIDER PAUSING"
        elif severity == "high" and is_rate_decision:
            pause_rec = "MONITOR CLOSELY"

        alerts.append({
            "event": event["title"], "cb": event["cb"],
            "date": event["date"].strftime("%a %b %d, %H:%M UTC"),
            "days_away": days_away, "pairs": affected_pairs, "bots": affected_bots,
            "severity": severity, "impact": event["impact"],
            "forecast": event["forecast"], "previous": event["previous"],
            "pause_rec": pause_rec, "window": "7d",
        })

    # ── Structural divergence alerts ──
    loaded = {k: v for k, v in cb_rates.items() if v}
    for pair, cbs in PAIR_CB_MAP.items():
        if len(cbs) == 2 and all(c in loaded for c in cbs):
            a, b = loaded[cbs[0]], loaded[cbs[1]]
            if a["trend"] != b["trend"] and "holding" not in [a["trend"], b["trend"]]:
                affected_bots = [bot for bot, cfg in PORTFOLIO.items() if pair in cfg["pairs"]]
                alerts.append({
                    "event": f"Policy Divergence: {cbs[0]} {a['trend']} vs {cbs[1]} {b['trend']}",
                    "cb": f"{cbs[0]}/{cbs[1]}", "date": "Structural / Ongoing",
                    "days_away": 998, "pairs": [pair], "bots": affected_bots,
                    "severity": "medium", "impact": "structural",
                    "forecast": "", "previous": "", "pause_rec": "", "window": "7d",
                })

    return alerts

# ── HTML Generation ────────────────────────────────────────────────────────────

def trend_arrow(trend):
    if trend == "hiking":  return '<span class="arrow up">▲</span>'
    if trend == "cutting": return '<span class="arrow down">▼</span>'
    return '<span class="arrow flat">◆</span>'

def severity_class(s):
    return {"critical": "sev-critical", "high": "sev-high", "medium": "sev-medium"}.get(s, "sev-medium")


CB_FLAGS = {
    "FED": "🇺🇸", "ECB": "🇪🇺", "BOE": "🇬🇧",
    "BOJ": "🇯🇵", "BOC": "🇨🇦", "RBA": "🇦🇺",
}
CB_FULLNAMES = {
    "FED": "Federal Reserve",
    "ECB": "European Central Bank",
    "BOE": "Bank of England",
    "BOJ": "Bank of Japan",
    "BOC": "Bank of Canada",
    "RBA": "Reserve Bank of Australia",
}
CB_RATE_LABELS = {
    "FED": "Fed Funds Rate",
    "ECB": "Deposit Rate",
    "BOE": "Base Rate",
    "BOJ": "Policy Rate",
    "BOC": "Overnight Rate",
    "RBA": "Cash Rate",
}
COUNTRY_FLAGS = {
    "USD": "🇺🇸", "EUR": "🇪🇺", "GBP": "🇬🇧",
    "JPY": "🇯🇵", "CAD": "🇨🇦", "AUD": "🇦🇺",
    "NZD": "🇳🇿", "CHF": "🇨🇭", "CNY": "🇨🇳",
    "ALL": "🌐",
}

def generate_html(cb_rates, events, cb_rates_raw=None, implied_moves=None, alerts=None):
    # Accept old signature too
    if implied_moves is None and isinstance(cb_rates_raw, dict) and "direction" not in str(cb_rates_raw):
        implied_moves = cb_rates_raw
    if alerts is None:
        alerts = []

    now_utc = datetime.now(timezone.utc)
    now_str = now_utc.strftime("%a %b %d, %Y · %H:%M UTC")

    import base64
    svg_fav = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="6" fill="#0d1117"/><line x1="7" y1="4" x2="7" y2="28" stroke="#58a6ff" stroke-width="1.5" stroke-linecap="round"/><line x1="16" y1="6" x2="16" y2="26" stroke="#3fb950" stroke-width="1.5" stroke-linecap="round"/><line x1="25" y1="5" x2="25" y2="27" stroke="#e3b341" stroke-width="1.5" stroke-linecap="round"/><rect x="4" y="10" width="6" height="12" rx="1" fill="#58a6ff"/><rect x="13" y="8" width="6" height="8" rx="1" fill="#3fb950"/><rect x="22" y="14" width="6" height="10" rx="1" fill="#e3b341"/></svg>"""
    fav_b64 = base64.b64encode(svg_fav.encode()).decode()

    # ── Summary stats ──
    n_hike = sum(1 for imp in (implied_moves or {}).values() if imp["direction"] == "hike" and imp["probability"] > 30)
    n_cut  = sum(1 for imp in (implied_moves or {}).values() if imp["direction"] == "cut"  and imp["probability"] > 30)
    n_hold = len(implied_moves or {}) - n_hike - n_cut
    n_events = len([e for e in events if e.get("impact") in ("high",)])
    n_alerts = len([a for a in alerts if a.get("days_away", 999) < 7])

    # ── CB cards for Central Banks tab ──
    cb_cards_html = ""
    for cb, info in FRED_SERIES.items():
        rate = cb_rates.get(cb)
        imp  = (implied_moves or {}).get(cb, {})
        direction = imp.get("direction", "hold")
        prob      = imp.get("probability", 0)
        display_pct = (100 - prob) if direction == "hold" else prob
        next_mtg  = imp.get("next_meeting", "")
        trend_cls = {"hiking":"cb-hike","cutting":"cb-cut","holding":"cb-hold"}.get(
            rate["trend"] if rate else "holding", "cb-hold")
        # Override card class with implied direction if strong signal
        if direction == "hike" and prob > 40:
            trend_cls = "cb-hike"
        elif direction == "cut" and prob > 40:
            trend_cls = "cb-cut"

        if rate:
            rate_val  = f"{rate['current']:.2f}%"
            rate_lbl  = CB_RATE_LABELS.get(cb, "Policy Rate")
            spark_cls = {"hiking":"spark-up","cutting":"spark-down","holding":"spark-flat"}.get(rate["trend"],"spark-flat")
            sparkline = generate_sparkline(rate.get("history", []), spark_cls)
            as_of     = rate.get("date", "")
        else:
            rate_val = "N/A"; rate_lbl = CB_RATE_LABELS.get(cb, "Policy Rate")
            sparkline = ""; as_of = ""

        dir_label = {"hike": "▲ HIKE", "cut": "▼ CUT", "hold": "◆ HOLD"}.get(direction, "◆ HOLD")
        bps_cls   = {"hike":"bps-hike","cut":"bps-cut","hold":"bps-hold"}.get(direction,"bps-hold")
        bps_txt   = f"{imp.get('spread_bp',0):+.1f}bp" if imp else "–"
        exp_txt   = f"{'Hike' if direction=='hike' else 'Cut' if direction=='cut' else 'Hold'} {display_pct}% probability"

        cb_cards_html += f"""
        <div class="cb-card {trend_cls}">
          <div class="cb-card-glow"></div>
          <div class="cb-header">
            <span class="cb-flag">{CB_FLAGS.get(cb,"🏦")}</span>
            <div>
              <div class="cb-name">{CB_FULLNAMES.get(cb, cb)}</div>
              <div class="cb-date">{next_mtg or "–"}</div>
            </div>
          </div>
          <div class="cb-rate-row">
            <div class="cb-current-rate">{rate_val}</div>
            <div class="cb-rate-label">{rate_lbl}</div>
          </div>
          <div class="cb-exp">Market: <span class="exp-val">{dir_label} &nbsp;{display_pct}%</span></div>
          <span class="cb-bps {bps_cls}">{bps_txt} implied</span>
          {sparkline}
          <div class="cb-asof">as of {as_of}</div>
        </div>"""

    # ── Timeline / Event cards ──
    # Group by day
    from collections import defaultdict
    day_groups = defaultdict(list)
    for e in events:
        if not e.get("date"):
            continue
        # Show events from today onwards (allow up to 1 day in past for same-day events)
        if e["date"] < now_utc:
            continue
        day_key = e["date"].strftime("%A · %B %d")
        day_groups[day_key].append(e)

    timeline_html = ""
    if not day_groups:
        timeline_html = '<div class="no-data-msg">No upcoming events — calendar feed will refresh on next scheduled run.</div>'
    else:
        for day, evts in sorted(day_groups.items(), key=lambda x: list(day_groups.keys()).index(x[0])):
            evts_sorted = sorted(evts, key=lambda e: e["date"])
            cards = ""
            for e in evts_sorted:
                impact = e.get("impact","medium")
                card_cls = "card-high" if impact == "high" else "card-medium"
                pip_cls  = "pip-high" if impact == "high" else "pip-medium"
                pip_lbl  = "H" if impact == "high" else "M"
                flag = COUNTRY_FLAGS.get(e.get("country",""), "🌐")
                time_str = e["date"].strftime("%H:%M UTC")
                cb_affected = any(
                    e["cb"] in PAIR_CB_MAP.get(p, [])
                    for bot in PORTFOLIO.values() for p in bot["pairs"]
                )
                watch_badge = '<span class="rate-badge rate-watch">⚡ BOT PAIRS</span>' if cb_affected else ""
                fc  = e.get("forecast") or ""
                prv = e.get("previous") or ""
                act = e.get("actual") or ""
                detail_boxes = ""
                if fc:  detail_boxes += f'<div class="detail-box"><div class="dlbl">Forecast</div><div class="dval expected">{fc}</div></div>'
                if prv: detail_boxes += f'<div class="detail-box"><div class="dlbl">Previous</div><div class="dval prior">{prv}</div></div>'
                if act: detail_boxes += f'<div class="detail-box"><div class="dlbl">Actual</div><div class="dval" style="color:var(--accent-green)">{act}</div></div>'
                detail_html = f'<div class="event-detail"><div class="detail-grid">{detail_boxes}</div></div>' if detail_boxes else ""
                cards += f"""
                <div class="event-card {card_cls}" onclick="toggleDetail(this)">
                  <div class="event-time">{time_str}</div>
                  <div class="event-main">
                    <div class="event-header">
                      <span class="flag">{flag}</span>
                      <span class="event-name">{e["title"]}</span>
                      <span class="region-tag">{e.get("country","")}</span>
                      {watch_badge}
                    </div>
                    {detail_html}
                  </div>
                  <div class="event-impact"><div class="impact-pip {pip_cls}">{pip_lbl}</div></div>
                </div>"""
            timeline_html += f"""
            <div class="day-group">
              <div class="day-label"><div class="day-dot"></div>{day.upper()}</div>
              <div class="event-cards">{cards}</div>
            </div>"""

    # ── Summary stats row ──
    summary_html = f"""
    <div class="summary-row">
      <div class="summary-card"><div class="val val-blue">{len(cb_rates)}</div><div class="lbl">CBs Tracked</div></div>
      <div class="summary-card"><div class="val val-red">{n_hike}</div><div class="lbl">Hike Signals</div></div>
      <div class="summary-card"><div class="val val-green">{n_cut}</div><div class="lbl">Cut Signals</div></div>
      <div class="summary-card"><div class="val val-yellow">{n_events}</div><div class="lbl">High-Impact Events</div></div>
      <div class="summary-card"><div class="val val-orange">{n_alerts}</div><div class="lbl">Bot Alerts (7d)</div></div>
    </div>"""

    # ── Portfolio volatility alerts ──
    real_alerts = [a for a in alerts if a.get("days_away", 999) < 998]
    struct_alerts = [a for a in alerts if a.get("days_away", 999) >= 998]
    alert_cards = ""
    if not real_alerts and not struct_alerts:
        if not events:
            alert_cards = '<div class="no-data-msg" style="color:var(--accent-yellow)">⚠ Calendar feed empty — next week\'s events not yet published by Forex Factory (normal on weekdays). Check back Friday/Saturday when the nextweek feed becomes available.</div>'
        else:
            alert_cards = '<div class="no-data-msg" style="color:var(--accent-green)">✓ No high-impact events in the next 7 days affecting your portfolio pairs.</div>'
    else:
        for a in sorted(real_alerts + struct_alerts, key=lambda x: x["days_away"]):
            sev = a["severity"]
            sev_cls = {"critical":"rate-hike","high":"rate-watch","medium":"rate-hold"}.get(sev,"rate-hold")
            pairs_tags = " ".join(f'<span class="asset-tag asset-fx">{p}</span>' for p in a["pairs"])
            bots_tags  = " ".join(f'<span class="region-tag">{b}</span>' for b in a["bots"])
            pause = f'<div style="margin:4px 0;font-size:11px;font-weight:700;color:var(--accent-red)">{a["pause_rec"]}</div>' if a.get("pause_rec") else ""
            fc  = f'<div class="detail-box" style="display:inline-block;margin:2px"><div class="dlbl">Forecast</div><div class="dval expected">{a["forecast"]}</div></div>' if a.get("forecast") else ""
            prv = f'<div class="detail-box" style="display:inline-block;margin:2px"><div class="dlbl">Previous</div><div class="dval prior">{a["previous"]}</div></div>' if a.get("previous") else ""
            alert_cards += f"""
            <div class="event-card card-{'high' if sev in ('critical','high') else 'medium'}">
              <div class="event-time" style="font-size:12px;font-weight:700">{a["cb"]}<br/><span style="color:var(--text-muted);font-size:10px;font-weight:400">{a["date"]}</span></div>
              <div class="event-main">
                <div class="event-header">
                  <span class="event-name">{a["event"]}</span>
                  <span class="rate-badge {sev_cls}">{sev.upper()}</span>
                </div>
                {pause}
                <div class="impact-row" style="margin-top:6px">{pairs_tags}</div>
                <div style="margin-top:4px">{bots_tags}</div>
                <div class="detail-grid" style="margin-top:8px">{fc}{prv}</div>
              </div>
              <div class="event-impact"><div class="impact-pip {'pip-high' if sev in ('critical','high') else 'pip-medium'}">{'!' if sev=='critical' else '⚠'}</div></div>
            </div>"""

    # ── Portfolio pair map ──
    pair_rows = ""
    for pair in ALL_PAIRS:
        cbs_list = PAIR_CB_MAP.get(pair, [])
        bots = [bot for bot, cfg in PORTFOLIO.items() if pair in cfg["pairs"]]
        has_alert = any(a for a in alerts if pair in a["pairs"] and a["days_away"] < 999)
        cbs_html  = " ".join(f'<span class="asset-tag asset-bonds">{c}</span>' for c in cbs_list)
        bots_html = " ".join(f'<span class="bot-badge" style="border-color:{PORTFOLIO[b]["color"]};color:{PORTFOLIO[b]["color"]}">{b}</span>' for b in bots)
        icon = "⚠️" if has_alert else "✓"
        row_style = "background:rgba(248,81,73,0.05)" if has_alert else ""
        pair_rows += f"""
        <tr style="{row_style}">
          <td style="font-family:monospace;font-weight:700;font-size:13px;color:#e6edf3">{pair}</td>
          <td>{cbs_html}</td>
          <td>{bots_html}</td>
          <td style="text-align:center;font-size:16px">{'<span style="color:var(--accent-red)">⚠️</span>' if has_alert else '<span style="color:var(--accent-green)">✓</span>'}</td>
        </tr>"""

    # ── Data releases table grouped by country ──
    from collections import defaultdict as dd2
    by_country = dd2(list)
    for e in events:
        if not e.get("date") or e["date"] < now_utc - timedelta(hours=12):
            continue
        by_country[e.get("country","ALL")].append(e)

    data_releases_html = ""
    for country in sorted(by_country.keys()):
        flag = COUNTRY_FLAGS.get(country, "🌐")
        rows = ""
        for e in sorted(by_country[country], key=lambda x: x["date"]):
            impact = e.get("impact","medium")
            imp_cls = "imp-high" if impact == "high" else "imp-med"
            act = e.get("actual","")
            act_html = f'<span style="color:var(--accent-green);font-weight:600">{act}</span>' if act else '<span style="color:var(--text-muted)">–</span>'
            cb_hit = any(e["cb"] in PAIR_CB_MAP.get(p,[]) for bot in PORTFOLIO.values() for p in bot["pairs"])
            row_style = "background:rgba(88,166,255,0.04)" if cb_hit else ""
            rows += f"""<tr style="{row_style}">
              <td class="td-date">{e["date"].strftime("%a %b %d")}<br/><span style="color:var(--text-muted)">{e["date"].strftime("%H:%M")} UTC</span></td>
              <td class="td-name">{e["title"]}</td>
              <td class="td-exp">{e.get("forecast") or "–"}</td>
              <td class="td-prior">{e.get("previous") or "–"}</td>
              <td>{act_html}</td>
              <td><span class="{imp_cls}">{impact.upper()}</span></td>
            </tr>"""
        if rows:
            data_releases_html += f"""
            <div class="data-section">
              <div class="section-title">{flag} {country}</div>
              <table><thead><tr>
                <th>Date / Time</th><th>Indicator</th><th>Expected</th><th>Prior</th><th>Actual</th><th>Impact</th>
              </tr></thead><tbody>{rows}</tbody></table>
            </div>"""

    if not data_releases_html:
        data_releases_html = '<div class="no-data-msg">No upcoming data releases in the calendar feed.</div>'

    # ── Risk radar — auto-generated from implied moves + alerts ──
    risk_cards_html = ""
    # CB divergence card
    hikers = [cb for cb, imp in (implied_moves or {}).items() if imp["direction"]=="hike" and imp["probability"]>30]
    cutters = [cb for cb, imp in (implied_moves or {}).items() if imp["direction"]=="cut" and imp["probability"]>30]
    if hikers or cutters:
        risk_level = "fill-high" if (hikers and cutters) else "fill-elevated"
        risk_lbl   = "lbl-high" if (hikers and cutters) else "lbl-elevated"
        risk_lbl_txt = "HIGH" if (hikers and cutters) else "ELEVATED"
        hike_txt = ", ".join(hikers) if hikers else "none"
        cut_txt  = ", ".join(cutters) if cutters else "none"
        risk_cards_html += f"""
        <div class="risk-card">
          <div class="risk-header"><div class="risk-icon">🏦</div>
            <div class="risk-title" style="color:var(--accent-orange)">CB Policy Divergence</div></div>
          <div class="risk-body">Market pricing hike signals from: <strong>{hike_txt}</strong>. Cut signals from: <strong>{cut_txt}</strong>. Diverging policy paths create cross-currency volatility risk for your active pairs.</div>
          <div class="risk-level"><div class="risk-bar"><div class="risk-bar-fill {risk_level}"></div></div><div class="risk-lbl {risk_lbl}">{risk_lbl_txt}</div></div>
        </div>"""

    # Per-CB high-probability cards
    for cb, imp in (implied_moves or {}).items():
        if imp["direction"] == "hold" or imp["probability"] < 40:
            continue
        direction = imp["direction"]
        prob = imp["probability"]
        fill = "fill-critical" if prob > 70 else ("fill-high" if prob > 55 else "fill-elevated")
        lbl  = "CRITICAL" if prob > 70 else ("HIGH" if prob > 55 else "ELEVATED")
        lbl_cls = "lbl-critical" if prob > 70 else ("lbl-high" if prob > 55 else "lbl-elevated")
        color = "var(--accent-red)" if direction=="hike" else "var(--accent-green)"
        pairs_affected = [p for p, cbs in PAIR_CB_MAP.items() if cb in cbs]
        pairs_txt = ", ".join(pairs_affected) if pairs_affected else "–"
        risk_cards_html += f"""
        <div class="risk-card">
          <div class="risk-header"><div class="risk-icon">{CB_FLAGS.get(cb,"🏦")}</div>
            <div class="risk-title" style="color:{color}">{CB_FULLNAMES.get(cb,cb)} — {direction.upper()} RISK</div></div>
          <div class="risk-body">{prob}% market-implied probability of a {direction} at the {imp.get("next_meeting","upcoming")} meeting. Forward rate: {imp["forward_rate"]}% vs current {imp["current_rate"]}% ({imp["spread_bp"]:+.1f}bp). Exposed pairs: <strong>{pairs_txt}</strong>.</div>
          <div class="risk-level"><div class="risk-bar"><div class="risk-bar-fill {fill}"></div></div><div class="risk-lbl {lbl_cls}">{lbl}</div></div>
        </div>"""

    # Multi-asset impact matrix — built from implied data
    matrix_rows = ""
    for cb, imp in (implied_moves or {}).items():
        if imp["direction"] == "hold":
            continue
        direction = imp["direction"]
        prob = imp["probability"]
        if prob < 25:
            continue
        s_green = "color:var(--accent-green)"
        s_red   = "color:var(--accent-red)"
        s_muted = "color:var(--text-muted)"
        if direction == "hike":
            eq_txt,  eq_s   = "↓ Risk-off",  s_red
            bd_txt,  bd_s   = "↑ Yields up", s_red
            usd_txt, usd_s  = ("↑ Stronger", s_green) if cb=="FED" else ("Neutral", s_muted)
            gold_txt,gold_s = "↓ Pressured", s_red
            fx_txt          = "AUD ↑" if cb=="RBA" else "JPY ↑" if cb=="BOJ" else "USD ↑" if cb=="FED" else "CAD ↑" if cb=="BOC" else "EUR ↑" if cb=="ECB" else "GBP ↑"
        else:
            eq_txt,  eq_s   = "↑ Rally",    s_green
            bd_txt,  bd_s   = "↑ Bonds bid",s_green
            usd_txt, usd_s  = ("↓ Weaker",  s_red) if cb=="FED" else ("Neutral", s_muted)
            gold_txt,gold_s = "↑ Bid",      s_green
            fx_txt          = "GBP ↓" if cb=="BOE" else "CAD ↓" if cb=="BOC" else "EUR ↓" if cb=="ECB" else "AUD ↓" if cb=="RBA" else "JPY ↓" if cb=="BOJ" else "USD ↓"
        matrix_rows += f"""<tr>
          <td class="td-name">{CB_FLAGS.get(cb,"")} {CB_FULLNAMES.get(cb,cb)} {direction.upper()} ({prob}%)</td>
          <td style="{eq_s}">{eq_txt}</td>
          <td style="{bd_s}">{bd_txt}</td>
          <td style="{usd_s}">{usd_txt}</td>
          <td style="{gold_s}">{gold_txt}</td>
          <td style="color:var(--accent-purple)">{fx_txt}</td>
        </tr>"""

    matrix_html = ""
    if matrix_rows:
        matrix_html = f"""
        <div class="risk-card" style="grid-column:1/-1">
          <div class="risk-header"><div class="risk-icon">🗺️</div>
            <div class="risk-title" style="color:var(--accent-purple)">Multi-Asset Impact Matrix</div></div>
          <div style="overflow-x:auto;margin-top:8px">
            <table style="min-width:500px">
              <thead><tr>
                <th>Scenario</th><th>Equities</th><th>Bonds</th><th>USD</th><th>Gold</th><th>FX Pair</th>
              </tr></thead>
              <tbody>{matrix_rows}</tbody>
            </table>
          </div>
        </div>"""

    if not risk_cards_html and not matrix_html:
        risk_cards_html = '<div class="no-data-msg">No significant risk signals detected — all CBs hold with low probability of change.</div>'

    # ── Full HTML ──
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="3600">
<title>Macro Intel — FX Bot Dashboard</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,{fav_b64}">
<style>
:root {{
  --bg:             #0d1117;
  --surface:        #161b22;
  --card:           #1c2333;
  --border:         #30363d;
  --accent-blue:    #58a6ff;
  --accent-green:   #3fb950;
  --accent-red:     #f85149;
  --accent-yellow:  #e3b341;
  --accent-purple:  #bc8cff;
  --accent-orange:  #ffa657;
  --accent-cyan:    #79c0ff;
  --text-primary:   #e6edf3;
  --text-secondary: #8b949e;
  --text-muted:     #6e7681;
  --tag-hold:       #1f4068;
  --tag-cut:        #1a3a2a;
  --tag-hike:       #3a1a1a;
  --tag-watch:      #2a2a1a;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  background: var(--bg);
  color: var(--text-primary);
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  font-size: 13px;
  line-height: 1.5;
}}

/* ── Header ── */
.header {{
  background: linear-gradient(135deg, #0d1117 0%, #161b22 100%);
  border-bottom: 1px solid var(--border);
  padding: 20px 24px 14px;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}}
.header-left h1 {{
  font-size: 22px;
  font-weight: 700;
  color: var(--accent-blue);
  letter-spacing: -0.3px;
}}
.header-left h1 span {{ color: var(--text-primary); }}
.header-left .subtitle {{
  font-size: 12px;
  color: var(--text-secondary);
  margin-top: 3px;
}}
.header-badges {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }}
.badge {{
  padding: 3px 10px;
  border-radius: 20px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.4px;
}}
.badge-red    {{ background: rgba(248,81,73,0.18);   color: var(--accent-red);    border: 1px solid rgba(248,81,73,0.35); }}
.badge-yellow {{ background: rgba(227,179,65,0.18);  color: var(--accent-yellow); border: 1px solid rgba(227,179,65,0.35); }}
.badge-blue   {{ background: rgba(88,166,255,0.12);  color: var(--accent-blue);   border: 1px solid rgba(88,166,255,0.3); }}
.badge-green  {{ background: rgba(63,185,80,0.12);   color: var(--accent-green);  border: 1px solid rgba(63,185,80,0.3); }}
.header-timestamp {{
  text-align: right;
  font-size: 11px;
  color: var(--text-muted);
  white-space: nowrap;
  margin-top: 4px;
  font-family: monospace;
}}
.auto-refresh {{ color: var(--accent-green); font-size: 10px; }}

/* ── Summary row ── */
.summary-row {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px;
  margin-bottom: 20px;
}}
.summary-card {{
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px;
  text-align: center;
}}
.summary-card .val {{ font-size: 28px; font-weight: 700; line-height: 1; }}
.summary-card .lbl {{ font-size: 11px; color: var(--text-secondary); margin-top: 5px; }}
.val-red {{ color: var(--accent-red); }}
.val-yellow {{ color: var(--accent-yellow); }}
.val-blue {{ color: var(--accent-blue); }}
.val-green {{ color: var(--accent-green); }}
.val-orange {{ color: var(--accent-orange); }}

/* ── Section title ── */
.section-title {{
  font-size: 11px;
  font-weight: 700;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.8px;
  margin-bottom: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
}}
.section-title::after {{ content: ''; flex: 1; height: 1px; background: var(--border); }}

/* ── CB Cards ── */
.cb-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 12px;
  margin-bottom: 20px;
}}
.cb-card {{
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px;
  position: relative;
  overflow: hidden;
  transition: border-color 0.2s, transform 0.15s;
}}
.cb-card:hover {{ transform: translateY(-2px); border-color: #444d56; }}
.cb-card-glow {{
  position: absolute; top: 0; right: 0;
  width: 70px; height: 70px;
  border-radius: 50%;
  opacity: 0.07;
}}
.cb-hold .cb-card-glow {{ background: var(--accent-blue); }}
.cb-hold {{ border-top: 2px solid var(--accent-blue); }}
.cb-cut  .cb-card-glow {{ background: var(--accent-green); }}
.cb-cut  {{ border-top: 2px solid var(--accent-green); }}
.cb-hike .cb-card-glow {{ background: var(--accent-red); }}
.cb-hike {{ border-top: 2px solid var(--accent-red); }}
.cb-header {{ display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }}
.cb-flag {{ font-size: 22px; line-height: 1; }}
.cb-name {{ font-weight: 700; font-size: 13px; color: var(--text-primary); line-height: 1.2; }}
.cb-date {{ font-size: 10px; color: var(--text-muted); margin-top: 2px; }}
.cb-rate-row {{ display: flex; align-items: baseline; gap: 8px; margin-bottom: 5px; }}
.cb-current-rate {{ font-size: 26px; font-weight: 700; color: var(--accent-blue); }}
.cb-rate-label {{ font-size: 11px; color: var(--text-muted); }}
.cb-exp {{ font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; }}
.exp-val {{ color: var(--accent-yellow); font-weight: 600; }}
.cb-bps {{
  font-size: 11px; font-weight: 700;
  padding: 2px 9px; border-radius: 20px;
  display: inline-block; margin-bottom: 8px;
}}
.bps-hold {{ background: rgba(88,166,255,0.12);  color: var(--accent-blue); }}
.bps-cut  {{ background: rgba(63,185,80,0.12);   color: var(--accent-green); }}
.bps-hike {{ background: rgba(248,81,73,0.12);   color: var(--accent-red); }}
.cb-asof {{ font-size: 10px; color: var(--text-muted); margin-top: 4px; font-family: monospace; }}

/* Sparklines */
.spark {{ width: 100%; height: 36px; overflow: visible; display: block; margin: 8px 0 2px; }}
.spark-up   {{ color: var(--accent-green); }}
.spark-down {{ color: var(--accent-red); }}
.spark-flat {{ color: var(--accent-blue); }}

/* ── Event / Timeline cards ── */
.timeline {{ display: flex; flex-direction: column; gap: 14px; }}
.day-group {{ }}
.day-label {{
  font-size: 11px; font-weight: 700;
  color: var(--text-muted);
  letter-spacing: 0.8px;
  text-transform: uppercase;
  padding: 6px 0 6px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 8px;
  display: flex; align-items: center; gap: 8px;
}}
.day-dot {{ width: 6px; height: 6px; border-radius: 50%; background: var(--accent-blue); flex-shrink: 0; }}
.event-cards {{ display: flex; flex-direction: column; gap: 6px; }}
.event-card {{
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 14px;
  display: grid;
  grid-template-columns: 80px 1fr auto;
  gap: 12px;
  align-items: start;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
  position: relative;
}}
.event-card:hover {{ border-color: var(--accent-blue); background: rgba(88,166,255,0.04); }}
.event-card.expanded {{ border-color: var(--accent-blue); }}
.event-card::before {{
  content: ''; position: absolute; left: 0; top: 0; bottom: 0;
  width: 3px; border-radius: 8px 0 0 8px;
}}
.card-high::before   {{ background: var(--accent-red); }}
.card-medium::before {{ background: var(--accent-yellow); }}
.card-low::before    {{ background: var(--accent-blue); }}
.event-time {{ font-size: 11px; color: var(--text-secondary); padding-top: 2px; font-family: monospace; white-space: nowrap; }}
.event-main {{ }}
.event-header {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
.flag {{ font-size: 14px; line-height: 1; }}
.event-name {{ font-weight: 600; font-size: 13px; color: var(--text-primary); }}
.region-tag {{
  font-size: 10px; color: var(--text-muted);
  background: rgba(255,255,255,0.06);
  padding: 2px 6px; border-radius: 4px;
}}
.rate-badge {{
  font-size: 10px; font-weight: 700;
  padding: 2px 8px; border-radius: 12px;
  letter-spacing: 0.3px; white-space: nowrap;
}}
.rate-hold  {{ background: var(--tag-hold);  color: var(--accent-cyan);   border: 1px solid rgba(88,166,255,0.3); }}
.rate-cut   {{ background: var(--tag-cut);   color: var(--accent-green);  border: 1px solid rgba(63,185,80,0.3); }}
.rate-hike  {{ background: var(--tag-hike);  color: var(--accent-red);    border: 1px solid rgba(248,81,73,0.3); }}
.rate-watch {{ background: var(--tag-watch); color: var(--accent-yellow); border: 1px solid rgba(227,179,65,0.3); }}
.event-impact {{ text-align: right; }}
.impact-pip {{
  width: 26px; height: 26px; border-radius: 50%;
  font-size: 11px; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
  margin-left: auto;
}}
.pip-high   {{ background: rgba(248,81,73,0.2);   color: var(--accent-red);    border: 1px solid var(--accent-red); }}
.pip-medium {{ background: rgba(227,179,65,0.2);  color: var(--accent-yellow); border: 1px solid var(--accent-yellow); }}
.pip-low    {{ background: rgba(88,166,255,0.15); color: var(--accent-blue);   border: 1px solid var(--accent-blue); }}

/* Expand detail */
.event-detail {{ display: none; margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--border); grid-column: 1/-1; }}
.event-detail.open {{ display: block; }}
.detail-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 8px; margin-bottom: 8px;
}}
.detail-box {{
  background: rgba(255,255,255,0.04);
  border: 1px solid var(--border);
  border-radius: 6px; padding: 8px 10px;
}}
.detail-box .dlbl {{ font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 3px; }}
.detail-box .dval {{ font-size: 12px; color: var(--text-primary); }}
.detail-box .dval.expected {{ color: var(--accent-yellow); }}
.detail-box .dval.prior    {{ color: var(--text-secondary); }}
.impact-row {{ display: flex; gap: 6px; flex-wrap: wrap; margin-top: 6px; }}
.asset-tag {{ font-size: 11px; padding: 3px 8px; border-radius: 4px; font-weight: 500; }}
.asset-equities   {{ background: rgba(63,185,80,0.12);  color: #3fb950; }}
.asset-bonds      {{ background: rgba(88,166,255,0.12); color: #58a6ff; }}
.asset-fx         {{ background: rgba(188,140,255,0.12);color: #bc8cff; }}
.asset-commodities{{ background: rgba(255,166,87,0.12); color: #ffa657; }}

/* ── Data table ── */
.data-section {{ margin-bottom: 24px; }}
table {{ width: 100%; border-collapse: collapse; }}
th {{
  text-align: left; font-size: 10px; font-weight: 600;
  color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px;
  padding: 7px 10px;
  background: rgba(255,255,255,0.04);
  border-bottom: 1px solid var(--border);
}}
td {{
  padding: 9px 10px; font-size: 12px;
  border-bottom: 1px solid rgba(48,54,61,0.5);
  vertical-align: top;
}}
tr:hover td {{ background: rgba(255,255,255,0.025); }}
.td-date  {{ color: var(--text-secondary); white-space: nowrap; font-family: monospace; font-size: 11px; }}
.td-name  {{ color: var(--text-primary); font-weight: 500; }}
.td-exp   {{ color: var(--accent-yellow); font-weight: 600; }}
.td-prior {{ color: var(--text-secondary); }}
.imp-high {{ color: var(--accent-red);    font-weight: 700; }}
.imp-med  {{ color: var(--accent-yellow); font-weight: 700; }}

/* ── Portfolio pair map ── */
.bot-badge {{
  display: inline-block; border: 1px solid;
  border-radius: 3px; padding: 2px 8px;
  font-size: 12px; font-weight: 600;
  margin-right: 4px; background: rgba(255,255,255,0.04);
}}

/* ── Risk radar ── */
.risk-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
}}
.risk-card {{
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 8px; padding: 16px;
}}
.risk-header {{ display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }}
.risk-icon   {{ font-size: 20px; }}
.risk-title  {{ font-weight: 700; font-size: 13px; }}
.risk-body   {{ font-size: 12px; color: var(--text-secondary); line-height: 1.6; }}
.risk-level  {{ display: flex; align-items: center; gap: 8px; margin-top: 12px; }}
.risk-bar    {{ flex: 1; height: 4px; background: var(--border); border-radius: 2px; overflow: hidden; }}
.risk-bar-fill {{ height: 100%; border-radius: 2px; }}
.risk-lbl    {{ font-size: 10px; font-weight: 700; width: 60px; text-align: right; letter-spacing: 0.5px; }}
.fill-critical  {{ background: var(--accent-red);    width: 95%; }}
.fill-high      {{ background: var(--accent-orange); width: 78%; }}
.fill-elevated  {{ background: var(--accent-yellow); width: 62%; }}
.fill-moderate  {{ background: var(--accent-blue);   width: 40%; }}
.lbl-critical   {{ color: var(--accent-red); }}
.lbl-high       {{ color: var(--accent-orange); }}
.lbl-elevated   {{ color: var(--accent-yellow); }}
.lbl-moderate   {{ color: var(--accent-blue); }}

/* ── Footer ── */
.footer {{
  text-align: center;
  padding: 14px 24px;
  font-size: 11px;
  color: var(--text-muted);
  border-top: 1px solid var(--border);
  margin-top: 8px;
  font-family: monospace;
}}

/* ── Misc ── */
.no-data-msg {{ color: var(--text-muted); font-size: 12px; padding: 16px 0; font-style: italic; }}

@media (max-width: 640px) {{
  .event-card {{ grid-template-columns: 1fr; }}
  .cb-grid {{ grid-template-columns: 1fr 1fr; }}
  .summary-row {{ grid-template-columns: repeat(2,1fr); }}
  .tabs {{ overflow-x: auto; }}
}}
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <h1>MACRO <span>INTEL</span></h1>
    <div class="subtitle">FX Bot Portfolio · Volatility Monitor · Central Bank Tracker</div>
    <div class="header-badges">
      {''.join(f'<span class="badge badge-{"red" if a["severity"] in ("critical","high") else "yellow"}">⚠ {a["event"][:40]}</span>' for a in sorted(alerts, key=lambda x: x["days_away"])[:3] if a.get("days_away",999)<3)}
    </div>
  </div>
  <div class="header-timestamp">
    <div class="auto-refresh">● AUTO-REFRESHES HOURLY</div>
    {now_str}
  </div>
</div>

<main style="max-width:1400px;margin:0 auto;padding:20px 24px 40px">

  <!-- ── Summary Stats ── -->
  {summary_html}

  <!-- ── Central Bank Rates ── -->
  <div class="section-title" style="margin-bottom:12px">🏦 Central Bank Policy Rates</div>
  <div class="cb-grid" style="margin-bottom:32px">
    {cb_cards_html}
  </div>

  <!-- ── Portfolio Volatility Alerts ── -->
  <div class="section-title" style="margin-bottom:12px">📡 Portfolio Volatility Alerts — Next 7 Days</div>
  <div class="event-cards" style="margin-bottom:32px">
    {alert_cards}
  </div>

  <!-- ── Event Calendar ── -->
  <div class="section-title" style="margin-bottom:12px">📅 Economic Calendar</div>
  <div class="timeline" style="margin-bottom:32px">
    {timeline_html}
  </div>

  <!-- ── Risk Radar ── -->
  <div class="section-title" style="margin-bottom:12px">🎯 Risk Radar</div>
  <div class="risk-grid" style="margin-bottom:32px">
    {risk_cards_html}
    {matrix_html}
  </div>

  <!-- ── Portfolio Map ── -->
  <div class="section-title" style="margin-bottom:12px">🤖 Active Bot Pair Exposure</div>
  <div style="background:var(--card);border:1px solid var(--border);border-radius:8px;overflow:hidden;margin-bottom:32px">
    <table>
      <thead><tr>
        <th>Pair</th><th>Central Banks</th><th>Active Bots</th><th style="text-align:center;width:70px">Alert</th>
      </tr></thead>
      <tbody>{pair_rows}</tbody>
    </table>
  </div>

</main>

<div class="footer">
  Macro Intel · Data: FRED · Forex Factory · rateprobability.com · Generated: {now_str} · Portfolio: {len(PORTFOLIO)} bots · {len(ALL_PAIRS)} pairs
</div>

<script>
function toggleDetail(card) {{
  const detail = card.querySelector('.event-detail');
  if (!detail) return;
  const isOpen = detail.classList.contains('open');
  document.querySelectorAll('.event-detail.open').forEach(d => d.classList.remove('open'));
  document.querySelectorAll('.event-card.expanded').forEach(c => c.classList.remove('expanded'));
  if (!isOpen) {{ detail.classList.add('open'); card.classList.add('expanded'); }}
}}
</script>
</body>
</html>"""

    return html

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
        f'<polyline points="{pts}" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>'
        f'</svg>'
    )


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Starting macro dashboard generation...")

    # 1. Fetch CB rates — try FRED candidates, then rateprobability.com as fallback
    print("  Fetching central bank rates from FRED...")
    cb_rates = {}
    rp_slugs = {"FED": "fed", "ECB": "ecb", "BOE": "boe",
                "BOJ": "boj", "BOC": "boc", "RBA": "rba"}
    for cb, info in FRED_SERIES.items():
        result = None
        for sid in info["ids"]:
            print(f"    → {cb} trying {sid}")
            result = fetch_fred_series(sid)
            if result:
                print(f"    → ✓ {cb} loaded from {sid}: {result['current']}%")
                break
        if not result:
            # Fallback: pull current rate from rateprobability.com
            slug = rp_slugs.get(cb)
            if slug:
                print(f"    → {cb} FRED failed — trying rateprobability.com...")
                rp = fetch_rateprobability(slug)
                result = _rp_to_cb_rate(rp)
                if result:
                    print(f"    → ✓ {cb} loaded from rateprobability.com: {result['current']}%")
        cb_rates[cb] = result
    loaded = sum(1 for v in cb_rates.values() if v)
    print(f"  Loaded {loaded}/{len(FRED_SERIES)} CB rates")

    # 2. Fetch market-implied rate changes
    print("  Fetching market-implied rate probabilities from FRED forward rates...")
    implied_moves = fetch_implied_rate_changes(cb_rates)
    print(f"  Loaded {len(implied_moves)} implied rate estimates")

    # 3. Fetch calendar (this week + next week = ~14 days)
    print("  Fetching Forex Factory calendar (2-week window)...")
    events = fetch_forex_factory_calendar()
    print(f"  Found {len(events)} high/medium-impact events")

    # 4. Compute alerts and outlook
    alerts = compute_alerts(cb_rates, events, implied_moves)
    print(f"  Generated {len(alerts)} alerts")

    # 5. Generate HTML
    print("  Generating HTML dashboard...")
    html = generate_html(cb_rates, events, alerts, implied_moves)

    # 6. Write output
    out_path = os.path.join(os.path.dirname(__file__), "docs", "index.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"  ✓ Dashboard written to {out_path}")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Done.")

if __name__ == "__main__":
    main()
