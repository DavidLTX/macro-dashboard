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
    "FED": {"id": "FEDFUNDS",        "name": "Fed Funds Rate",   "currency": "USD", "flag": "🇺🇸"},
    "ECB": {"id": "ECBDFR",          "name": "ECB Deposit Rate", "currency": "EUR", "flag": "🇪🇺"},
    "BOE": {"id": "IUDSOIA",         "name": "BOE SONIA Rate",   "currency": "GBP", "flag": "🇬🇧"},
    "BOJ": {"id": "IRSTCB01JPM156N", "name": "BOJ Policy Rate",  "currency": "JPY", "flag": "🇯🇵"},
    "BOC": {"id": "IRSTCB01CAM156N", "name": "BOC Policy Rate",  "currency": "CAD", "flag": "🇨🇦"},
    "RBA": {"id": "RBAAOARD",        "name": "RBA Cash Rate",    "currency": "AUD", "flag": "🇦🇺"},
}

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")

# ── Data Fetching ──────────────────────────────────────────────────────────────

def fetch_fred_series(series_id):
    """Fetch last 6 months of a FRED data series."""
    if not FRED_API_KEY:
        return None
    six_months_ago = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")
    url = (
        f"https://api.stlouisfed.org/fred/series/observations"
        f"?series_id={series_id}&api_key={FRED_API_KEY}"
        f"&observation_start={six_months_ago}&file_type=json&sort_order=desc&limit=6"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        obs = [o for o in data.get("observations", []) if o["value"] != "."]
        if not obs:
            return None
        values = [float(o["value"]) for o in obs[:6]]
        dates  = [o["date"] for o in obs[:6]]
        current = values[0]
        prev    = values[1] if len(values) > 1 else current
        trend   = "hiking" if current > prev else ("cutting" if current < prev else "holding")
        history = list(zip(dates[::-1], values[::-1]))
        return {"current": current, "previous": prev, "trend": trend, "history": history, "date": dates[0]}
    except Exception as e:
        print(f"  FRED error ({series_id}): {e}")
        return None

def _parse_ff_xml(content, cb_map, seen):
    """Parse raw Forex Factory XML bytes into event dicts. Returns list."""
    events = []
    root = ET.fromstring(content)
    channel = root.find("channel")
    if channel is None:
        return events
    for item in channel.findall("item"):
        title    = item.findtext("title", "").strip()
        date_str = item.findtext("date", "").strip()
        country  = item.findtext("country", "").upper().strip()
        impact   = item.findtext("impact", "").lower().strip()
        forecast = item.findtext("forecast", "").strip()
        previous = item.findtext("previous", "").strip()
        actual   = item.findtext("actual", "").strip()

        if impact not in ("high", "medium"):
            continue
        key = f"{title}|{date_str}|{country}"
        if key in seen:
            continue
        seen.add(key)

        dt = None
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M"):
            try:
                dt = datetime.strptime(date_str[:len(fmt)], fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                break
            except Exception:
                continue

        cb = cb_map.get(country, country)
        events.append({
            "title": title, "date": dt,
            "date_str": date_str[:10] if date_str else "",
            "country": country, "cb": cb, "impact": impact,
            "forecast": forecast, "previous": previous, "actual": actual,
        })
    return events


def _make_request(url, extra_headers=None, timeout=15):
    """Make a URL request with realistic browser headers."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "application/xml,text/xml,application/json,*/*;q=0.9",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
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
    Fetch 14-day economic calendar from multiple sources with fallback chain:
    1. Forex Factory XML (primary + CDN mirror)
    2. FRED known CB meeting dates (always available if FRED key works)
    """
    events = []
    cb_map = {"US":"FED","EU":"ECB","GB":"BOE","JP":"BOJ","CA":"BOC","AU":"RBA","CH":"SNB","NZ":"RBNZ"}
    seen   = set()

    # ── Source 1: Forex Factory XML feeds ─────────────────────────────────────
    ff_feeds = [
        ("thisweek", "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"),
        ("thisweek", "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.xml"),
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
            # Handle gzip
            if raw[:2] == b'\x1f\x8b':
                import gzip
                raw = gzip.decompress(raw)
            parsed = _parse_ff_xml(raw, cb_map, seen)
            if parsed:
                events.extend(parsed)
                fetched_weeks.add(week_key)
                print(f"    → ✓ {len(parsed)} events ({week_key})")
            else:
                print(f"    → 0 events parsed — trying next URL")
        except urllib.error.HTTPError as e:
            print(f"    → HTTP {e.code} — trying next")
        except urllib.error.URLError as e:
            print(f"    → URLError: {e.reason} — trying next")
        except Exception as e:
            print(f"    → {type(e).__name__}: {e} — trying next")

    # ── Source 2: FRED release calendar (fallback if FF failed) ───────────────
    # FRED has a releases/dates endpoint listing upcoming data releases
    if not events and FRED_API_KEY:
        print("    FF failed — trying FRED release calendar as fallback...")
        try:
            today     = datetime.now().strftime("%Y-%m-%d")
            two_weeks = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")
            url = (
                f"https://api.stlouisfed.org/fred/releases/dates"
                f"?api_key={FRED_API_KEY}&file_type=json"
                f"&realtime_start={today}&realtime_end={two_weeks}"
                f"&include_release_dates_with_no_data=false&limit=100"
            )
            with _make_request(url) as resp:
                data = json.loads(resp.read())

            # Map FRED release names to central banks
            release_cb_map = {
                "federal open market committee": ("FED",  "US", "FOMC Rate Decision"),
                "fomc":                          ("FED",  "US", "FOMC Rate Decision"),
                "consumer price index":          ("FED",  "US", "CPI"),
                "employment situation":          ("FED",  "US", "NFP / Employment"),
                "ecb":                           ("ECB",  "EU", "ECB Policy Decision"),
                "bank of england":               ("BOE",  "GB", "BOE Rate Decision"),
                "bank of japan":                 ("BOJ",  "JP", "BOJ Rate Decision"),
                "bank of canada":                ("BOC",  "CA", "BOC Rate Decision"),
                "reserve bank of australia":     ("RBA",  "AU", "RBA Rate Decision"),
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
            print(f"    → FRED fallback failed: {e}")

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

    # OIS / near-term forward rate proxies available on FRED
    IMPLIED_SERIES = {
        "FED": "SOFR",            # Secured Overnight Financing Rate — tracks expected Fed path
        "BOE": "IUDSOIA",         # Same as current rate — use 3m GBP OIS when available
        "ECB": "ECBDFR",          # ECB deposit facility — use ESTR as proxy
    }

    # Better: use 3-month treasury / overnight spread as proxy for hike pricing
    FORWARD_PROXIES = {
        "FED": {"now": "FEDFUNDS", "fwd": "DTB3"},   # Fed Funds vs 3M T-Bill
        "ECB": {"now": "ECBDFR",   "fwd": "IR3TIB01EZM156N"},  # ECB vs 3M Euribor
        "BOE": {"now": "IUDSOIA",  "fwd": "IR3TBB01GBM156N"},  # BOE SONIA vs 3M GBP
        "BOC": {"now": "IRSTCB01CAM156N", "fwd": "IR3TBB01CAM156N"},
        "RBA": {"now": "RBAAOARD", "fwd": "IR3TBB01AUM156N"},
    }

    implied = {}
    for cb, series in FORWARD_PROXIES.items():
        try:
            now_rate  = cb_rates.get(cb, {})
            fwd_data  = fetch_fred_series(series["fwd"])
            if not now_rate or not fwd_data:
                continue

            current   = now_rate["current"]
            forward   = fwd_data["current"]
            spread_bp = (forward - current) * 100  # convert to basis points

            # Market convention: 25bp = full move; scale probability accordingly
            if abs(spread_bp) < 5:
                direction = "hold"
                probability = 0
            elif spread_bp > 0:
                direction = "hike"
                probability = min(int((spread_bp / 25) * 100), 95)
            else:
                direction = "cut"
                probability = min(int((abs(spread_bp) / 25) * 100), 95)

            implied[cb] = {
                "direction":    direction,
                "probability":  probability,
                "spread_bp":    round(spread_bp, 1),
                "forward_rate": round(forward, 3),
                "current_rate": round(current, 3),
            }
        except Exception as e:
            print(f"  Implied rate error ({cb}): {e}")

    return implied


def compute_alerts(cb_rates, upcoming_events, implied_moves):
    """Generate volatility alerts: 0-7 days (confirmed events) + 7-14 days (outlook)."""
    alerts   = []
    outlook  = []
    now      = datetime.now(timezone.utc)
    next_7d  = now + timedelta(days=7)
    next_14d = now + timedelta(days=14)

    # ── Confirmed alerts: events in next 7 days ──
    for event in upcoming_events:
        if not event["date"] or not (now <= event["date"] <= next_7d):
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

    # ── Outlook: events in 7-14 days ──
    for event in upcoming_events:
        if not event["date"] or not (next_7d < event["date"] <= next_14d):
            continue

        affected_pairs = [p for p, cbs in PAIR_CB_MAP.items() if event["cb"] in cbs]
        affected_bots  = [bot for bot, cfg in PORTFOLIO.items() if any(p in affected_pairs for p in cfg["pairs"])]
        if not affected_pairs:
            continue

        days_away = (event["date"] - now).days
        is_rate_decision = any(kw in event["title"].lower() for kw in
                               ["rate", "decision", "policy", "statement", "minutes", "nfp", "non-farm", "cpi", "inflation"])

        # Check if there's also a strong implied move for this CB
        imp = implied_moves.get(event["cb"], {})
        imp_prob   = imp.get("probability", 0)
        imp_dir    = imp.get("direction", "")
        imp_str    = f"Market implies {imp_prob}% chance of {imp_dir}" if imp_prob > 20 else ""

        severity = "high" if (is_rate_decision and imp_prob > 50) else "medium"
        pause_rec = "PREPARE TO PAUSE" if severity == "high" else ""

        outlook.append({
            "event": event["title"], "cb": event["cb"],
            "date": event["date"].strftime("%a %b %d, %H:%M UTC"),
            "days_away": days_away, "pairs": affected_pairs, "bots": affected_bots,
            "severity": severity, "impact": event["impact"],
            "forecast": event["forecast"], "previous": event["previous"],
            "implied": imp_str, "pause_rec": pause_rec, "window": "14d",
        })

    # ── Implied move alerts (market-based, no scheduled event needed) ──
    for cb, imp in implied_moves.items():
        if imp["probability"] < 40 or imp["direction"] == "hold":
            continue
        affected_pairs = [p for p, cbs in PAIR_CB_MAP.items() if cb in cbs]
        affected_bots  = [bot for bot, cfg in PORTFOLIO.items() if any(p in affected_pairs for p in cfg["pairs"])]
        if not affected_pairs:
            continue
        # Only add if not already covered by a scheduled event
        already = any(a["cb"] == cb for a in alerts + outlook)
        if not already:
            severity = "high" if imp["probability"] >= 60 else "medium"
            outlook.append({
                "event": f"Market pricing {imp['probability']}% chance of {imp['direction']} ({imp['spread_bp']:+.0f}bp spread)",
                "cb": cb, "date": "No meeting scheduled yet",
                "days_away": 999, "pairs": affected_pairs, "bots": affected_bots,
                "severity": severity, "impact": "implied",
                "forecast": f"{imp['forward_rate']}%", "previous": f"{imp['current_rate']}%",
                "implied": f"Forward rate: {imp['forward_rate']}% vs current {imp['current_rate']}%",
                "pause_rec": "MONITOR" if imp["probability"] >= 60 else "",
                "window": "14d",
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

    return alerts, outlook

# ── HTML Generation ────────────────────────────────────────────────────────────

def trend_arrow(trend):
    if trend == "hiking":  return '<span class="arrow up">▲</span>'
    if trend == "cutting": return '<span class="arrow down">▼</span>'
    return '<span class="arrow flat">◆</span>'

def severity_class(s):
    return {"critical": "sev-critical", "high": "sev-high", "medium": "sev-medium"}.get(s, "sev-medium")

def generate_html(cb_rates, events, alerts, outlook, implied_moves):
    now_str = datetime.now(timezone.utc).strftime("%A, %B %d %Y — %H:%M UTC")
    now_ts  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── CB Rate Cards ──
    cb_cards_html = ""
    for cb, info in FRED_SERIES.items():
        rate = cb_rates.get(cb)
        if rate:
            val  = f"{rate['current']:.2f}%"
            prev = f"{rate['previous']:.2f}%"
            arrow = trend_arrow(rate["trend"])
            trend_cls = {"hiking":"trend-up","cutting":"trend-down","holding":"trend-flat"}.get(rate["trend"],"trend-flat")
            spark_cls = {"hiking":"spark-up","cutting":"spark-down","holding":"spark-flat"}.get(rate["trend"],"spark-flat")
            sparkline = generate_sparkline(rate.get("history", []), spark_cls)
            as_of = rate.get("date", "")
        else:
            val = "N/A"; prev = "–"; arrow = ""; trend_cls = "trend-flat"; sparkline = ""; as_of = ""; spark_cls = ""

        cb_cards_html += f"""
        <div class="cb-card {trend_cls}">
          <div class="cb-header">
            <span class="cb-currency">{info['currency']}</span>
            <span class="cb-name">{cb}</span>
            {arrow}
          </div>
          <div class="cb-rate">{val}</div>
          <div class="cb-sub">{info['name']}</div>
          <div class="cb-prev">Prev: {prev}</div>
          {sparkline}
          <div class="cb-asof">as of {as_of}</div>
        </div>"""

    # ── Alert Cards (0-7 days) ──
    alert_html = ""
    real_alerts = [a for a in alerts if a.get("days_away", 999) < 998]
    struct_alerts = [a for a in alerts if a.get("days_away", 999) >= 998]
    if not real_alerts and not struct_alerts:
        alert_html = '<div class="no-alerts">✓ No high-impact events in the next 7 days affecting your portfolio.</div>'
    else:
        for a in sorted(real_alerts + struct_alerts, key=lambda x: x["days_away"]):
            pairs_str = " ".join(f'<span class="pair-tag">{p}</span>' for p in a["pairs"])
            bots_str  = " ".join(f'<span class="bot-tag">{b}</span>' for b in a["bots"])
            fc_str    = f'<span class="forecast">Forecast: {a["forecast"]}</span>' if a.get("forecast") else ""
            pr_str    = f'<span class="forecast-prev">Prev: {a["previous"]}</span>' if a.get("previous") else ""
            pause_html = f'<div class="pause-rec pause-{a["severity"]}">{a["pause_rec"]}</div>' if a.get("pause_rec") else ""
            alert_html += f"""
            <div class="alert-card {severity_class(a['severity'])}">
              <div class="alert-top">
                <span class="alert-cb">{a['cb']}</span>
                <span class="alert-sev">{a['severity'].upper()}</span>
              </div>
              {pause_html}
              <div class="alert-event">{a['event']}</div>
              <div class="alert-date">📅 {a['date']}</div>
              <div class="alert-meta">{fc_str}{pr_str}</div>
              <div class="alert-pairs">Pairs at risk: {pairs_str}</div>
              <div class="alert-bots">Exposed bots: {bots_str}</div>
            </div>"""

    # ── Outlook Cards (7-14 days) ──
    outlook_html = ""
    if not outlook:
        outlook_html = '<div class="no-alerts">✓ No notable scheduled events in the 7–14 day window for your pairs.</div>'
    else:
        for a in sorted(outlook, key=lambda x: x["days_away"]):
            pairs_str  = " ".join(f'<span class="pair-tag">{p}</span>' for p in a["pairs"])
            bots_str   = " ".join(f'<span class="bot-tag">{b}</span>' for b in a["bots"])
            fc_str     = f'<span class="forecast">Forecast: {a["forecast"]}</span>' if a.get("forecast") else ""
            pr_str     = f'<span class="forecast-prev">Prev: {a["previous"]}</span>' if a.get("previous") else ""
            imp_str    = f'<div class="implied-badge">📊 {a["implied"]}</div>' if a.get("implied") else ""
            pause_html = f'<div class="pause-rec pause-{a["severity"]}">{a["pause_rec"]}</div>' if a.get("pause_rec") else ""
            days_label = f"In {a['days_away']} days" if a["days_away"] < 900 else ""
            outlook_html += f"""
            <div class="alert-card outlook-card {severity_class(a['severity'])}">
              <div class="alert-top">
                <span class="alert-cb">{a['cb']}</span>
                <span class="days-badge">{days_label}</span>
              </div>
              {pause_html}
              <div class="alert-event">{a['event']}</div>
              <div class="alert-date">📅 {a['date']}</div>
              {imp_str}
              <div class="alert-meta">{fc_str}{pr_str}</div>
              <div class="alert-pairs">Pairs at risk: {pairs_str}</div>
              <div class="alert-bots">Watching bots: {bots_str}</div>
            </div>"""

    # ── Event Calendar ──
    cal_rows = ""
    shown = 0
    for e in events[:20]:
        if not e["date"]:
            continue
        impact_cls = "imp-high" if e["impact"] == "high" else "imp-med"
        cb_affected = any(
            e["cb"] in PAIR_CB_MAP.get(p, [])
            for bot in PORTFOLIO.values() for p in bot["pairs"]
        )
        row_cls = "row-highlight" if cb_affected else ""
        actual_str = f'<span class="actual-val">{e["actual"]}</span>' if e["actual"] else '<span class="pending">pending</span>'
        cal_rows += f"""
        <tr class="{row_cls}">
          <td>{e['date'].strftime('%a %b %d') if e['date'] else '–'}</td>
          <td>{e['date'].strftime('%H:%M') if e['date'] else '–'}</td>
          <td><span class="country-badge">{e['country']}</span></td>
          <td class="event-title">{e['title']}</td>
          <td><span class="{impact_cls}">{e['impact'].upper()}</span></td>
          <td>{e['forecast'] or '–'}</td>
          <td>{e['previous'] or '–'}</td>
          <td>{actual_str}</td>
        </tr>"""
        shown += 1

    if not shown:
        cal_rows = '<tr><td colspan="8" class="no-data">No events loaded this run — Forex Factory feed may be temporarily unavailable. Data will appear on the next scheduled refresh.</td></tr>'

    # ── Portfolio Pair Map ──
    pair_rows = ""
    for pair in ALL_PAIRS:
        cbs  = PAIR_CB_MAP.get(pair, [])
        bots = [bot for bot, cfg in PORTFOLIO.items() if pair in cfg["pairs"]]
        has_alert = any(a for a in alerts if pair in a["pairs"] and a["days_away"] < 999)
        risk_cls  = "risk-alert" if has_alert else "risk-ok"
        cbs_html  = " ".join(f'<span class="cb-badge">{c}</span>' for c in cbs)
        bots_html = " ".join(f'<span class="bot-badge" style="border-color:{PORTFOLIO[b]["color"]}">{b}</span>' for b in bots)
        warn_icon = "⚠️" if has_alert else "✓"
        pair_rows += f"""
        <tr class="{risk_cls}">
          <td class="pair-name">{pair}</td>
          <td>{cbs_html}</td>
          <td>{bots_html}</td>
          <td class="risk-icon">{warn_icon}</td>
        </tr>"""

    # ── Inline Sparkline SVG ──
    def mini_spark(history, css_class="spark-flat"):
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
            f"{int(i*(w/max(n-1,1)))},{int(h - (v-mn)/rng*(h-4)+2)}"
            for i, v in enumerate(vals)
        )
        return (
            f'<svg class="spark {css_class}" viewBox="0 0 {w} {h}" preserveAspectRatio="none">'
            f'<polyline points="{pts}" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>'
            f'</svg>'
        )

    spark_lookup = {cb: mini_spark(cb_rates[cb]["history"]) if cb_rates.get(cb) else "" for cb in FRED_SERIES}

    # ── Implied Rate Probability Cards ──
    implied_html = ""
    if not implied_moves:
        implied_html = '<div class="no-implied">Market-implied probability data unavailable — forward rate series may not have loaded. Check FRED API key and connectivity.</div>'
    else:
        for cb, imp in implied_moves.items():
            dir_cls   = imp["direction"]
            pct       = imp["probability"]
            fill_cls  = {"hike": "imp-hike", "cut": "imp-cut", "hold": "imp-hold"}.get(dir_cls, "imp-hold")
            pct_cls   = {"hike": "hike", "cut": "cut", "hold": "hold"}.get(dir_cls, "hold")
            dir_label = {"hike": "▲ HIKE", "cut": "▼ CUT", "hold": "◆ HOLD"}.get(dir_cls, dir_cls.upper())
            implied_html += f"""
            <div class="implied-card">
              <div class="imp-cb">{cb}</div>
              <div class="imp-label">Market-implied next move</div>
              <span class="imp-pct {pct_cls}">{dir_label} &nbsp;{pct}%</span>
              <div class="imp-prob-bar">
                <div class="imp-prob-fill {fill_cls}" style="width:{pct}%"></div>
              </div>
              <div class="imp-rates">Current: {imp['current_rate']}% → Fwd: {imp['forward_rate']}% ({imp['spread_bp']:+.1f}bp)</div>
            </div>"""

    # ── Full HTML ──────────────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="3600">
<title>Macro Dashboard — FX Bot Portfolio</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600;700&family=Barlow:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root {{
  --bg:       #08080f;
  --card:     #10101a;
  --card2:    #14141f;
  --border:   #1e1e30;
  --border2:  #2a2a40;
  --red:      #ff3b3b;
  --amber:    #f59e0b;
  --green:    #22c55e;
  --blue:     #60a5fa;
  --purple:   #a78bfa;
  --text:     #ddddf0;
  --dim:      #9999cc;
  --dimmer:   #6666aa;
}}
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
html {{ scroll-behavior: smooth; }}
body {{
  background: var(--bg);
  color: var(--text);
  font-family: 'Barlow', sans-serif;
  font-size: 14px;
  min-height: 100vh;
  background-image:
    radial-gradient(ellipse 80% 50% at 50% -20%, #1a0a3a44 0%, transparent 60%),
    repeating-linear-gradient(0deg, transparent, transparent 40px, #ffffff03 40px, #ffffff03 41px),
    repeating-linear-gradient(90deg, transparent, transparent 40px, #ffffff03 40px, #ffffff03 41px);
}}

/* ── Header ── */
.header {{
  padding: 40px 40px 24px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 16px;
}}
.header-left h1 {{
  font-family: 'Barlow', sans-serif;
  font-size: 30px;
  font-weight: 800;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: #fff;
}}
.header-left h1 span {{ color: var(--purple); }}
.header-sub {{
  color: var(--dim);
  font-size: 13px;
  margin-top: 6px;
  letter-spacing: 0.03em;
  font-weight: 500;
}}
.header-right {{
  text-align: right;
}}
.updated-label {{
  font-size: 11px;
  color: #8888bb;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  font-family: 'IBM Plex Mono', monospace;
}}
.updated-time {{
  font-size: 12px;
  color: var(--dim);
  margin-top: 4px;
  font-family: 'IBM Plex Mono', monospace;
}}
.live-dot {{
  display: inline-block;
  width: 7px; height: 7px;
  border-radius: 50%;
  background: var(--green);
  margin-right: 6px;
  animation: pulse 2s infinite;
}}
@keyframes pulse {{
  0%, 100% {{ opacity: 1; box-shadow: 0 0 0 0 #22c55e66; }}
  50% {{ opacity: 0.7; box-shadow: 0 0 0 5px #22c55e00; }}
}}

/* ── Layout ── */
.main {{ padding: 32px 40px; display: flex; flex-direction: column; gap: 36px; }}
.section-title {{
  font-family: 'Barlow', sans-serif;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--dim);
  margin-bottom: 18px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--border);
}}

/* ── CB Rate Cards ── */
.cb-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
  gap: 12px;
}}
.cb-card {{
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 20px 20px 16px;
  position: relative;
  overflow: hidden;
  transition: border-color 0.2s, transform 0.15s;
}}
.cb-card::before {{
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 3px;
}}
.trend-up::before   {{ background: linear-gradient(90deg, var(--red), #ff6b6b); }}
.trend-down::before {{ background: linear-gradient(90deg, var(--green), #4ade80); }}
.trend-flat::before {{ background: linear-gradient(90deg, var(--blue), #93c5fd); }}
.cb-card:hover {{ border-color: var(--border2); transform: translateY(-1px); }}
.cb-header {{
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  line-height: 1;
}}
.cb-currency {{
  font-family: 'IBM Plex Mono', monospace;
  font-size: 11px;
  font-weight: 700;
  color: #7070a0;
  letter-spacing: 0.08em;
  line-height: 1;
  display: flex;
  align-items: center;
  padding: 2px 5px;
  background: #ffffff0a;
  border: 1px solid #2a2a45;
  border-radius: 3px;
}}
.cb-name {{
  font-family: 'Barlow', sans-serif;
  font-size: 15px;
  font-weight: 800;
  color: #fff;
  flex: 1;
  letter-spacing: 0.04em;
  line-height: 1;
}}
.arrow {{ font-size: 12px; line-height: 1; display: flex; align-items: center; }}
.arrow.up   {{ color: var(--red); }}
.arrow.down {{ color: var(--green); }}
.arrow.flat {{ color: var(--blue); font-size: 9px; }}
.cb-rate {{
  font-family: 'IBM Plex Mono', monospace;
  font-size: 34px;
  font-weight: 700;
  color: #fff;
  letter-spacing: -1px;
  line-height: 1;
}}
.cb-sub  {{ font-size: 12px; color: #b0b0d8; margin-top: 6px; font-weight: 600; }}
.cb-prev {{ font-size: 12px; color: #a0a0cc; margin-top: 4px; font-family: 'IBM Plex Mono', monospace; }}
.cb-asof {{ font-size: 11px; color: #8888bb; margin-top: 8px; font-family: 'IBM Plex Mono', monospace; }}
.spark {{
  display: block;
  width: 100%;
  height: 36px;
  margin-top: 12px;
}}
.spark-up   {{ color: #ff6060; }}
.spark-down {{ color: #4ade80; }}
.spark-flat {{ color: #93c5fd; }}

/* ── Alerts ── */
.alerts-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
}}
.alert-card {{
  background: var(--card);
  border-radius: 8px;
  padding: 16px;
  border: 1px solid var(--border);
  border-left-width: 4px;
}}
.sev-critical {{ border-left-color: var(--red);   background: #1a080840; }}
.sev-high     {{ border-left-color: var(--amber);  background: #1a100040; }}
.sev-medium   {{ border-left-color: var(--blue);   background: #08101a40; }}
.alert-top {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}}
.alert-cb {{
  font-family: 'Barlow', sans-serif;
  font-weight: 800;
  color: #fff;
  font-size: 15px;
  letter-spacing: 0.04em;
}}
.alert-sev {{
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.1em;
  padding: 2px 8px;
  border-radius: 3px;
}}
.sev-critical .alert-sev {{ background: var(--red);   color: #fff; }}
.sev-high     .alert-sev {{ background: var(--amber); color: #000; }}
.sev-medium   .alert-sev {{ background: var(--blue);  color: #000; }}
.alert-event {{ font-size: 14px; color: var(--text); margin-bottom: 6px; line-height: 1.4; font-weight: 500; }}
.alert-date  {{ font-size: 12px; color: var(--dim); margin-bottom: 8px; font-family: 'IBM Plex Mono', monospace; }}
.alert-meta  {{ font-size: 11px; color: var(--dim); margin-bottom: 8px; display: flex; gap: 12px; }}
.forecast      {{ color: var(--blue); }}
.forecast-prev {{ color: #9898c8; }}
.alert-pairs, .alert-bots {{ font-size: 11px; margin-top: 4px; display: flex; flex-wrap: wrap; gap: 4px; align-items: center; color: var(--dim); }}
.pair-tag {{
  background: #ffffff12;
  border: 1px solid var(--border2);
  border-radius: 3px;
  padding: 1px 6px;
  font-size: 10px;
  color: var(--text);
  font-weight: 700;
}}
.bot-tag {{
  background: #ffffff08;
  border: 1px solid var(--border);
  border-radius: 3px;
  padding: 1px 6px;
  font-size: 10px;
  color: var(--dim);
}}
.no-alerts {{
  background: var(--card);
  border: 1px solid var(--border);
  border-left: 4px solid var(--green);
  border-radius: 8px;
  padding: 20px 24px;
  color: var(--green);
  font-size: 13px;
}}

/* ── Calendar Table ── */
.table-wrap {{ overflow-x: auto; }}
table {{
  width: 100%;
  border-collapse: collapse;
}}
th {{
  text-align: left;
  font-size: 12px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #c0c0e0;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border2);
  font-weight: 700;
  background: #0e0e1a;
  font-family: 'Barlow', sans-serif;
}}
td {{
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
  font-size: 13px;
  color: var(--text);
}}
tr:last-child td {{ border-bottom: none; }}
tr:hover td {{ background: #ffffff04; }}
.row-highlight td {{ background: #a78bfa0a; }}
.row-highlight:hover td {{ background: #a78bfa12; }}
.event-title {{ max-width: 280px; }}
.country-badge {{
  background: var(--card2);
  border: 1px solid var(--border2);
  border-radius: 3px;
  padding: 1px 6px;
  font-size: 10px;
  font-weight: 700;
}}
.imp-high {{ color: var(--red);   font-weight: 700; font-size: 10px; }}
.imp-med  {{ color: var(--amber); font-weight: 700; font-size: 10px; }}
.actual-val {{ color: var(--green); font-weight: 700; }}
.pending    {{ color: var(--dimmer); font-style: italic; }}
.no-data    {{ color: var(--dimmer); text-align: center; padding: 24px; }}

/* ── Portfolio Map ── */
.portfolio-table {{ background: var(--card); border-radius: 8px; overflow: hidden; border: 1px solid var(--border); }}
.risk-ok    td {{ }}
.risk-alert td {{ background: #ff3b3b08; }}
.pair-name {{
  font-family: 'IBM Plex Mono', monospace;
  font-weight: 700;
  color: #fff;
  font-size: 14px;
  letter-spacing: 0.05em;
}}
.cb-badge {{
  display: inline-block;
  background: #ffffff10;
  border: 1px solid var(--border2);
  border-radius: 3px;
  padding: 1px 7px;
  font-size: 10px;
  font-weight: 700;
  margin-right: 4px;
  color: var(--blue);
}}
.bot-badge {{
  display: inline-block;
  border: 1px solid;
  border-radius: 3px;
  padding: 1px 7px;
  font-size: 10px;
  margin-right: 4px;
  color: var(--text);
  background: #ffffff06;
}}
.risk-icon {{ font-size: 14px; text-align: center; }}
.risk-ok    .risk-icon {{ color: var(--green); }}
.risk-alert .risk-icon {{ color: var(--amber); }}

/* ── Footer ── */
.footer {{
  padding: 24px 40px;
  border-top: 1px solid var(--border);
  display: flex;
  justify-content: space-between;
  align-items: center;
  color: #8888bb;
  font-size: 11px;
  flex-wrap: wrap;
  gap: 8px;
  font-family: 'IBM Plex Mono', monospace;
}}
.footer a {{ color: var(--dim); text-decoration: none; }}
.footer a:hover {{ color: var(--text); }}

@media (max-width: 600px) {{
  .header, .main, .footer {{ padding-left: 16px; padding-right: 16px; }}
  .cb-grid {{ grid-template-columns: repeat(2, 1fr); }}
}}

/* ── Pause Recommendations ── */
.pause-rec {{
  display: inline-block;
  font-family: 'IBM Plex Mono', monospace;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.12em;
  padding: 3px 10px;
  border-radius: 3px;
  margin-bottom: 8px;
}}
.pause-critical {{ background: var(--red);   color: #fff; }}
.pause-high     {{ background: var(--amber); color: #000; }}
.pause-medium   {{ background: #1e3a5f;      color: var(--blue); border: 1px solid var(--blue); }}

/* ── Outlook Cards ── */
.outlook-card {{
  opacity: 0.92;
  position: relative;
}}
.outlook-card::after {{
  content: '7–14 DAYS';
  position: absolute;
  top: 10px; right: 12px;
  font-size: 9px;
  font-family: 'IBM Plex Mono', monospace;
  letter-spacing: 0.1em;
  color: var(--dimmer);
}}
.days-badge {{
  font-size: 11px;
  color: var(--dim);
  font-family: 'IBM Plex Mono', monospace;
}}
.implied-badge {{
  font-size: 11px;
  color: var(--blue);
  background: #0a1a2e;
  border: 1px solid #1e3a5f;
  border-radius: 4px;
  padding: 5px 10px;
  margin-bottom: 8px;
  font-family: 'IBM Plex Mono', monospace;
}}

/* ── Implied Rates Table ── */
.implied-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 10px;
}}
.implied-card {{
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}}
.imp-cb {{ font-family: 'Barlow', sans-serif; font-weight: 800; font-size: 15px; color: #fff; }}
.imp-prob-bar {{ height: 5px; background: var(--border2); border-radius: 3px; overflow: hidden; margin: 4px 0; }}
.imp-prob-fill {{ height: 100%; border-radius: 3px; transition: width 0.3s; }}
.imp-hike {{ background: linear-gradient(90deg, #ff3b3b, #ff6b6b); }}
.imp-cut  {{ background: linear-gradient(90deg, #22c55e, #4ade80); }}
.imp-hold {{ background: linear-gradient(90deg, #60a5fa, #93c5fd); }}
.imp-label {{ font-size: 12px; color: #b0b0d8; font-weight: 500; }}
.imp-pct   {{ font-family: 'IBM Plex Mono', monospace; font-size: 15px; font-weight: 700; }}
.imp-pct.hike {{ color: var(--red); }}
.imp-pct.cut  {{ color: var(--green); }}
.imp-pct.hold {{ color: var(--blue); }}
.imp-rates {{ font-size: 11px; color: #9898c8; font-family: 'IBM Plex Mono', monospace; }}
.no-implied {{ color: var(--dimmer); font-size: 12px; padding: 12px 0; font-style: italic; }}
</style>
</head>
<body>

<header class="header">
  <div class="header-left">
    <h1>MACRO <span>INTEL</span></h1>
    <div class="header-sub">FX Bot Portfolio · Volatility Monitor · Central Bank Tracker</div>
  </div>
  <div class="header-right">
    <div class="updated-label"><span class="live-dot"></span>Auto-refreshes hourly</div>
    <div class="updated-time">{now_str}</div>
  </div>
</header>

<main class="main">

  <!-- ── Section 1: CB Rates ── -->
  <section>
    <div class="section-title">Central Bank Policy Rates</div>
    <div class="cb-grid">
      {cb_cards_html}
    </div>
  </section>

  <!-- ── Section 2: Market-Implied Next Move ── -->
  <section>
    <div class="section-title">📊 Market-Implied Rate Move Probability</div>
    <div class="implied-grid">
      {implied_html}
    </div>
  </section>

  <!-- ── Section 3: Confirmed Alerts 0-7 days ── -->
  <section>
    <div class="section-title">⚠ Portfolio Volatility Alerts — Next 7 Days</div>
    <div class="alerts-grid">
      {alert_html}
    </div>
  </section>

  <!-- ── Section 4: Outlook 7-14 days ── -->
  <section>
    <div class="section-title">🔭 14-Day Outlook — Potential Risk Events</div>
    <div class="alerts-grid">
      {outlook_html}
    </div>
  </section>

  <!-- ── Section 5: Portfolio Pair Map ── -->
  <section>
    <div class="section-title">Portfolio Pair Exposure Map</div>
    <div class="portfolio-table">
      <table>
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
    </div>
  </section>

  <!-- ── Section 6: Event Calendar ── -->
  <section>
    <div class="section-title">High-Impact Economic Calendar — Next 14 Days</div>
    <div class="table-wrap">
      <table>
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
    </div>
  </section>

</main>

<footer class="footer">
  <span>Data: <a href="https://fred.stlouisfed.org" target="_blank">FRED</a> · <a href="https://www.forexfactory.com" target="_blank">Forex Factory</a></span>
  <span>Generated: {now_str}</span>
  <span>Portfolio: {len(PORTFOLIO)} bots · {len(ALL_PAIRS)} pairs</span>
</footer>

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

    # 1. Fetch CB rates
    print("  Fetching central bank rates from FRED...")
    cb_rates = {}
    for cb, info in FRED_SERIES.items():
        print(f"    → {cb} ({info['id']})")
        cb_rates[cb] = fetch_fred_series(info["id"])
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
    alerts, outlook = compute_alerts(cb_rates, events, implied_moves)
    print(f"  Generated {len(alerts)} alerts + {len(outlook)} outlook items")

    # 5. Generate HTML
    print("  Generating HTML dashboard...")
    html = generate_html(cb_rates, events, alerts, outlook, implied_moves)

    # 6. Write output
    out_path = os.path.join(os.path.dirname(__file__), "docs", "index.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"  ✓ Dashboard written to {out_path}")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Done.")

if __name__ == "__main__":
    main()
