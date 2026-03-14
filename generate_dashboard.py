#!/usr/bin/env python3
"""
Macro Dashboard Generator (v1 — risk scores + bot risk indices)
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
    "Control": {"pairs": ["EURJPY", "USDCAD"], "strategy": "Fibonacci Grid",  "color": "#a78bfa"},
    "Jet":     {"pairs": ["EURUSD", "EURGBP"], "strategy": "Dual-Dir Grid",   "color": "#22c55e"},
    "HGold":   {"pairs": ["XAUUSD"],           "strategy": "Fixed-Lot Scalper","color": "#fcd34d"},
    "Hedge":   {"pairs": ["AUDCAD"],           "strategy": "Dual-Dir Grid",   "color": "#2dd4bf"},
}

# Bot-specific severity weight (higher = more sensitive to macro events)
BOT_SEVERITY_WEIGHTS = {
    "Control": 1.0,
    "Jet":     1.1,
    "HGold":   1.3,
    "Hedge":   0.9,
}

ALL_PAIRS = sorted(set(p for bot in PORTFOLIO.values() for p in bot["pairs"]))

PAIR_CB_MAP = {
    "EURJPY": ["ECB", "BOJ"],
    "USDCAD": ["FED", "BOC"],
    "EURGBP": ["ECB", "BOE"],
    "EURUSD": ["ECB", "FED"],
    "AUDCAD": ["RBA", "BOC"],
    "XAUUSD": ["FED"],
}

FRED_SERIES = {
    "FED": {"ids": ["FEDFUNDS"],                                              "name": "Fed Funds Rate",  "currency": "USD", "flag": "🇺🇸"},
    "ECB": {"ids": ["ECBDFR"],                                                "name": "ECB Deposit Rate","currency": "EUR", "flag": "🇪🇺"},
    "BOE": {"ids": ["IUDSOIA", "BOEBR"],                                      "name": "BOE Base Rate",   "currency": "GBP", "flag": "🇬🇧"},
    "BOJ": {"ids": ["IRSTCB01JPM156N", "IR3TIB01JPM156N", "INTGSTJPM193N"],  "name": "BOJ Policy Rate", "currency": "JPY", "flag": "🇯🇵"},
    "BOC": {"ids": ["IRSTCB01CAM156N", "INTGSTCAM193N",  "IR3TBB01CAM156N"], "name": "BOC Policy Rate", "currency": "CAD", "flag": "🇨🇦"},
    "RBA": {"ids": ["IRSTCB01AUM156N", "INTGSTAUM193N",  "IR3TIB01AUM156N"], "name": "RBA Cash Rate",   "currency": "AUD", "flag": "🇦🇺"},
}

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")

# ── Data Fetching ──────────────────────────────────────────────────────────────

def fetch_fred_series(series_id):
    """Fetch last 12 months of a FRED data series, return latest value + 6-point sparkline."""
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
        all_dates  = [o["date"]          for o in obs]
        current    = all_values[0]
        prev       = all_values[1] if len(all_values) > 1 else current
        spark_vals = all_values[:6]
        spark_oldest = spark_vals[-1] if len(spark_vals) > 1 else current
        if   current > spark_oldest + 0.05: trend = "hiking"
        elif current < spark_oldest - 0.05: trend = "cutting"
        else:                                trend = "holding"
        history = list(zip(all_dates[:6][::-1], spark_vals[::-1]))
        return {"current": current, "previous": prev, "trend": trend, "history": history, "date": all_dates[0]}
    except Exception as e:
        print(f"  FRED error ({series_id}): {e}")
        return None


def fetch_rateprobability(cb_key):
    """Fetch next-meeting rate probabilities from rateprobability.com JSON API."""
    import json as _json
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
            import gzip; raw = gzip.decompress(raw)
        data = _json.loads(raw)
        print(f"  rateprobability.com API ({cb_key}): {len(raw)} bytes")
    except Exception as e:
        print(f"  rateprobability.com ({cb_key}): API error — {e}")
        return None

    result = {"cb": cb_key.upper(), "source": "rateprobability.com", "meetings": []}
    today  = data.get("today", {})
    if not today:
        return None

    CB_RATE_KEYS = ["cash_rate_target","Overnight Rate Target","current_target",
                    "ecb_deposit_facility","depo_reported","midpoint","most_recent_effr"]
    current_rate = None
    for key in CB_RATE_KEYS:
        v = today.get(key)
        if v is not None:
            try: current_rate = float(v); break
            except (TypeError, ValueError): pass
    if current_rate is None:
        print(f"  rateprobability.com ({cb_key}): could not find rate")
        return None
    result["current_rate"] = current_rate

    meetings_data = today.get("rows", [])
    if not meetings_data:
        return None

    now = datetime.now(timezone.utc)
    for row in meetings_data:
        if not isinstance(row, dict): continue
        date_val = row.get("meeting_iso") or row.get("meeting") or row.get("date")
        if not date_val: continue
        dt = None
        for fmt in ("%Y-%m-%d", "%b %d, %Y", "%Y-%m-%dT%H:%M:%S"):
            try: dt = datetime.strptime(str(date_val)[:10], fmt[:10]).replace(tzinfo=timezone.utc); break
            except ValueError: continue
        if not dt or dt < now - timedelta(days=1): continue

        implied_rate = None
        for k in ("implied_rate_post_meeting","impliedRate","implied_rate","postMeetingRate"):
            try: implied_rate = float(row[k]); break
            except (TypeError, ValueError, KeyError): pass

        prob = None
        for k in ("prob_move_pct","probability","prob","moveProbability"):
            try: prob = abs(float(row[k])); break
            except (TypeError, ValueError, KeyError): pass

        delta_bp = None
        for k in ("change_bps","deltaBp","delta_bp","delta","bpChange"):
            try: delta_bp = float(row[k]); break
            except (TypeError, ValueError, KeyError): pass

        if implied_rate is None or prob is None or delta_bp is None: continue
        is_cut   = bool(row.get("prob_is_cut", False))
        real_delta = (implied_rate - result["current_rate"]) * 100
        direction = "hold" if prob < 5 else ("cut" if is_cut else "hike")
        result["meetings"].append({
            "date": dt, "date_str": dt.strftime("%b %d, %Y"),
            "implied_rate": implied_rate, "probability": prob,
            "direction": direction, "delta_bp": real_delta,
        })

    if not result["meetings"]: return None
    m = result["meetings"][0]
    result.update({
        "next_meeting_date": m["date_str"], "next_meeting_direction": m["direction"],
        "next_meeting_prob": m["probability"], "next_implied_rate": m["implied_rate"],
        "delta_bp": m["delta_bp"],
    })
    print(f"  RP {cb_key.upper()}: {result['current_rate']}% → {m['direction']} {m['probability']}% @ {m['date_str']} (Δ{m['delta_bp']:+.1f}bp)")
    return result


def _rp_to_implied(rp):
    if not rp: return None
    return {
        "direction":    rp["next_meeting_direction"],
        "probability":  min(int(rp["next_meeting_prob"]), 95),
        "spread_bp":    round(rp["delta_bp"], 1),
        "forward_rate": round(rp["next_implied_rate"], 3),
        "current_rate": round(rp["current_rate"], 3),
        "fwd_label":    "rateprobability.com",
        "next_meeting": rp["next_meeting_date"],
    }


def _rp_to_cb_rate(rp):
    if not rp or "current_rate" not in rp: return None
    current = rp["current_rate"]
    meetings = rp.get("meetings", [])
    future_rate = meetings[0]["implied_rate"] if meetings else current
    trend = "hiking" if future_rate > current + 0.05 else ("cutting" if future_rate < current - 0.05 else "holding")
    history = [(m["date_str"], m["implied_rate"]) for m in meetings[:6]]
    if not history: history = [(datetime.now().strftime("%Y-%m-%d"), current)]
    return {"current": current, "previous": current, "trend": trend, "history": history,
            "date": datetime.now().strftime("%Y-%m-%d"), "source": "rateprobability.com"}


def _parse_ff_xml(content, seen):
    events = []
    if isinstance(content, bytes):
        try:    text = content.decode("windows-1252")
        except: text = content.decode("utf-8", errors="replace")
        content = text.encode("utf-8")
        content = content.replace(b'encoding="windows-1252"', b'encoding="utf-8"')
    root = ET.fromstring(content)
    currency_cb_map = {
        "USD": ("FED","US"), "EUR": ("ECB","EU"), "GBP": ("BOE","GB"),
        "JPY": ("BOJ","JP"), "CAD": ("BOC","CA"), "AUD": ("RBA","AU"),
        "CHF": ("SNB","CH"), "NZD": ("RBNZ","NZ"),
    }
    high_impact   = {"high","holiday"}
    medium_impact = {"medium"}
    for event in root.findall("event"):
        title   = (event.findtext("title")   or "").strip()
        country = (event.findtext("country") or "").strip().upper()
        date_str= (event.findtext("date")    or "").strip()
        time_str= (event.findtext("time")    or "").strip()
        impact  = (event.findtext("impact")  or "").strip().lower()
        forecast= (event.findtext("forecast")or "").strip()
        previous= (event.findtext("previous")or "").strip()
        actual  = (event.findtext("actual")  or "").strip()
        if impact not in high_impact and impact not in medium_impact: continue
        key = f"{title}|{date_str}|{country}"
        if key in seen: continue
        seen.add(key)
        dt = None
        try:
            time_clean = time_str.replace("\u200b","").strip()
            if time_clean and time_clean.lower() not in ("all day","tentative",""):
                dt = datetime.strptime(f"{date_str} {time_clean}", "%m-%d-%Y %I:%M%p")
            else:
                dt = datetime.strptime(date_str, "%m-%d-%Y")
            dt = dt.replace(tzinfo=timezone.utc)
        except:
            try:    dt = datetime.strptime(date_str, "%m-%d-%Y").replace(tzinfo=timezone.utc)
            except: dt = None
        cb, country_code = currency_cb_map.get(country, (country, country[:2]))
        impact_norm = "high" if impact in high_impact else "medium"
        events.append({"title": title, "date": dt,
                        "date_str": dt.strftime("%Y-%m-%d") if dt else date_str,
                        "country": country_code, "cb": cb, "impact": impact_norm,
                        "forecast": forecast, "previous": previous, "actual": actual})
    return events


def _make_request(url, extra_headers=None, timeout=15):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/xml,text/xml,*/*;q=0.9",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    if extra_headers: headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_forex_factory_calendar():
    events, seen = [], set()
    now    = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=14)
    ff_feeds = [
        ("thisweek", "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"),
        ("thisweek", "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.xml"),
        ("nextweek", "https://nfs.faireconomy.media/ff_calendar_nextweek.xml"),
    ]
    fetched_weeks = set()
    for week_key, url in ff_feeds:
        if week_key in fetched_weeks: continue
        try:
            print(f"  FF XML: {url}")
            with _make_request(url) as resp: raw = resp.read()
            print(f"  → {len(raw)} bytes")
            if raw[:2] == b"\x1f\x8b":
                import gzip; raw = gzip.decompress(raw)
            parsed = _parse_ff_xml(raw, seen)
            if parsed:
                events.extend(parsed)
                fetched_weeks.add(week_key)
                print(f"  → ✓ {len(parsed)} events ({week_key})")
            else:
                print(f"  → 0 events parsed")
        except urllib.error.HTTPError as e:
            print(f"  → HTTP {e.code} — skipping")
        except urllib.error.URLError as e:
            print(f"  → URLError: {e.reason}")
        except Exception as e:
            print(f"  → {type(e).__name__}: {e}")
    before = len(events)
    events = [e for e in events if e["date"] and e["date"] <= cutoff]
    if before != len(events): print(f"  → Trimmed to 14-day window: {len(events)} (was {before})")

    if not events and FRED_API_KEY:
        print("  FF unavailable — trying FRED release calendar fallback...")
        try:
            today_s  = now.strftime("%Y-%m-%d")
            cutoff_s = cutoff.strftime("%Y-%m-%d")
            url = (f"https://api.stlouisfed.org/fred/releases/dates"
                   f"?api_key={FRED_API_KEY}&file_type=json"
                   f"&realtime_start={today_s}&realtime_end={cutoff_s}"
                   f"&include_release_dates_with_no_data=false&limit=200")
            with _make_request(url) as resp: raw = resp.read()
            if raw[:2] == b"\x1f\x8b":
                import gzip; raw = gzip.decompress(raw)
            data = json.loads(raw)
            release_cb_map = {
                "federal open market committee": ("FED","US","FOMC Rate Decision"),
                "fomc":                           ("FED","US","FOMC Rate Decision"),
                "consumer price index":           ("FED","US","CPI"),
                "employment situation":           ("FED","US","NFP / Employment"),
                "ecb":                            ("ECB","EU","ECB Policy Decision"),
                "bank of england":                ("BOE","GB","BOE Rate Decision"),
                "bank of japan":                  ("BOJ","JP","BOJ Rate Decision"),
                "bank of canada":                 ("BOC","CA","BOC Rate Decision"),
                "reserve bank of australia":      ("RBA","AU","RBA Rate Decision"),
            }
            for rel in data.get("release_dates", []):
                name     = rel.get("release_name","").lower()
                date_str = rel.get("date","")
                for keyword, (cb, country, label) in release_cb_map.items():
                    if keyword in name:
                        key = f"{label}|{date_str}|{country}"
                        if key in seen: continue
                        seen.add(key)
                        try:    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                        except: dt = None
                        events.append({"title": label, "date": dt, "date_str": date_str,
                                       "country": country, "cb": cb, "impact": "high",
                                       "forecast": "", "previous": "", "actual": ""})
                        break
            print(f"  → FRED fallback: {len(events)} events")
        except Exception as e:
            import traceback; print(f"  → FRED fallback failed: {e}"); traceback.print_exc()

    if not events: print("  ⚠ All calendar sources failed")
    else:          print(f"  ✓ Total events: {len(events)}")
    return sorted(events, key=lambda x: x["date"] or datetime.min.replace(tzinfo=timezone.utc))


def fetch_implied_rate_changes(cb_rates):
    implied   = {}
    rp_slugs  = {"FED":"fed","ECB":"ecb","BOE":"boe","BOJ":"boj","BOC":"boc","RBA":"rba"}
    print("  Fetching rate probabilities from rateprobability.com...")
    for cb, slug in rp_slugs.items():
        rp  = fetch_rateprobability(slug)
        imp = _rp_to_implied(rp)
        if imp:
            implied[cb] = imp
            if not cb_rates.get(cb):
                cb_rates[cb] = _rp_to_cb_rate(rp)
                print(f"  CB rate backfilled for {cb}: {imp['current_rate']}%")
            else:
                rp_rate, fred_rate = imp["current_rate"], cb_rates[cb]["current"]
                if abs(rp_rate - fred_rate) > 0.01:
                    cb_rates[cb]["current"] = rp_rate
                    print(f"  CB rate patched {cb}: {fred_rate}% → {rp_rate}%")

    FORWARD_PROXIES = {
        "FED": {"fwd": ["DTB3"],                            "label": "3M T-Bill"},
        "ECB": {"fwd": ["IR3TIB01EZM156N"],                 "label": "3M Euribor"},
        "BOE": {"fwd": ["IR3TIB01GBM156N","IR3TBB01GBM156N"],"label": "3M GBP Interbank"},
        "BOJ": {"fwd": ["IR3TIB01JPM156N"],                 "label": "3M JPY Tibor"},
        "BOC": {"fwd": ["IR3TIB01CAM156N","IR3TBB01CAM156N","TB3MS"],"label": "3M CAD Interbank"},
        "RBA": {"fwd": ["IR3TIB01AUM156N","IR3TBB01AUM156N"],"label": "3M AUD Interbank"},
    }
    rp_missing = [cb for cb in FORWARD_PROXIES if cb not in implied]
    if rp_missing:
        print(f"  RP missing for {rp_missing} — FRED spread fallback...")
        for cb in rp_missing:
            series = FORWARD_PROXIES[cb]
            try:
                now_rate = cb_rates.get(cb)
                if not now_rate: continue
                fwd_data, fwd_label = None, series["label"]
                for fwd_id in series["fwd"]:
                    fwd_data = fetch_fred_series(fwd_id)
                    if fwd_data: fwd_label = f"{series['label']} ({fwd_id})"; break
                if not fwd_data: continue
                current, forward = now_rate["current"], fwd_data["current"]
                spread_bp = (forward - current) * 100
                if   abs(spread_bp) < 5: direction, probability = "hold", 0
                elif spread_bp > 0:      direction = "hike"; probability = min(int((spread_bp/25)*100), 95)
                else:                    direction = "cut";  probability = min(int((abs(spread_bp)/25)*100), 95)
                implied[cb] = {"direction": direction, "probability": probability,
                               "spread_bp": round(spread_bp,1), "forward_rate": round(forward,3),
                               "current_rate": round(current,3), "fwd_label": fwd_label}
                print(f"  Implied {cb} (FRED): {direction} {probability}% ({spread_bp:+.1f}bp)")
            except Exception as e:
                print(f"  Implied rate error ({cb}): {e}")
    return implied


# ── v1: Risk Scoring ───────────────────────────────────────────────────────────

def _event_volatility_score(event, days_away, affected_pairs, affected_bots):
    """Heuristic 0-100 score per event based on impact, proximity, type, and exposure."""
    base       = 70 if event.get("impact") == "high" else 45
    time_bonus = 25 if days_away <= 0 else (15 if days_away <= 2 else (8 if days_away <= 5 else 3))
    title      = (event.get("title") or "").lower()
    if   any(k in title for k in ["rate decision","rate statement","fomc","policy"]): type_bonus = 20
    elif any(k in title for k in ["cpi","inflation","pce"]):                          type_bonus = 18
    elif any(k in title for k in ["employment","unemployment","job","payroll","nfp"]): type_bonus = 16
    elif any(k in title for k in ["gdp"]):                                             type_bonus = 12
    elif any(k in title for k in ["pmi","manufacturing","services"]):                  type_bonus = 8
    else:                                                                               type_bonus = 5
    exposure = min(len(affected_pairs),3)*4 + min(len(affected_bots),3)*5
    return max(0, min(int(base + time_bonus + type_bonus + exposure), 100))


def _bot_risk_indices(alerts):
    """Aggregate event scores into per-bot 24h and 72h risk indices (0-100)."""
    risk = {bot: {"24h": 0.0, "72h": 0.0} for bot in PORTFOLIO}
    now  = datetime.now(timezone.utc)
    for a in alerts:
        score    = a.get("score", 0)
        event_dt = a.get("_dt")
        if score <= 0 or not event_dt or not isinstance(event_dt, datetime): continue
        delta_hours = (event_dt - now).total_seconds() / 3600.0
        if delta_hours < 0: continue
        for bot in a.get("bots", []):
            w = BOT_SEVERITY_WEIGHTS.get(bot, 1.0)
            if delta_hours <= 24:  risk[bot]["24h"] += score * w
            if delta_hours <= 72:  risk[bot]["72h"] += score * w * 0.7
    for bot in risk:
        risk[bot]["24h"] = int(min(risk[bot]["24h"], 100))
        risk[bot]["72h"] = int(min(risk[bot]["72h"], 100))
    return risk


# ── Alert Computation ──────────────────────────────────────────────────────────

def compute_alerts(cb_rates, upcoming_events, implied_moves):
    alerts = []
    now    = datetime.now(timezone.utc)
    next_7d= now + timedelta(days=7)

    for event in upcoming_events:
        if not event["date"] or not (now <= event["date"] <= next_7d): continue
        affected_pairs = [p for p, cbs in PAIR_CB_MAP.items() if event["cb"] in cbs]
        affected_bots  = [bot for bot, cfg in PORTFOLIO.items() if any(p in affected_pairs for p in cfg["pairs"])]
        if not affected_pairs: continue
        days_away = max((event["date"] - now).days, 0)
        severity  = "critical" if days_away <= 1 else ("high" if days_away <= 3 else "medium")
        is_rate   = any(kw in event["title"].lower() for kw in
                        ["rate","decision","policy","statement","minutes","nfp","non-farm","cpi","inflation"])
        pause_rec = ""
        if severity == "critical" and is_rate: pause_rec = "CONSIDER PAUSING"
        elif severity == "high"    and is_rate: pause_rec = "MONITOR CLOSELY"
        score = _event_volatility_score(event, days_away, affected_pairs, affected_bots)
        alerts.append({
            "event": event["title"], "cb": event["cb"],
            "date":  event["date"].strftime("%a %b %d, %H:%M UTC"),
            "_dt":   event["date"],
            "days_away": days_away, "pairs": affected_pairs, "bots": affected_bots,
            "severity": severity, "impact": event["impact"],
            "forecast": event["forecast"], "previous": event["previous"],
            "pause_rec": pause_rec, "window": "7d", "score": score,
        })

    loaded = {k: v for k, v in cb_rates.items() if v}
    for pair, cbs in PAIR_CB_MAP.items():
        if len(cbs) == 2 and all(c in loaded for c in cbs):
            a, b = loaded[cbs[0]], loaded[cbs[1]]
            if a["trend"] != b["trend"] and "holding" not in [a["trend"], b["trend"]]:
                affected_bots = [bot for bot, cfg in PORTFOLIO.items() if pair in cfg["pairs"]]
                alerts.append({
                    "event": f"Policy Divergence: {cbs[0]} {a['trend']} vs {cbs[1]} {b['trend']}",
                    "cb": f"{cbs[0]}/{cbs[1]}", "date": "Structural / Ongoing",
                    "_dt": None, "days_away": 998, "pairs": [pair], "bots": affected_bots,
                    "severity": "medium", "impact": "structural",
                    "forecast": "", "previous": "", "pause_rec": "", "window": "7d", "score": 40,
                })
    return alerts


# ── HTML Helpers ───────────────────────────────────────────────────────────────

def trend_arrow(trend):
    return {"hiking": "▲", "cutting": "▼"}.get(trend, "◆")

def severity_class(s):
    return {"critical":"sev-critical","high":"sev-high","medium":"sev-medium"}.get(s,"sev-medium")

def generate_sparkline(history, css_class="spark-flat"):
    if not history or len(history) < 2: return ""
    try:    vals = [v for _, v in history]
    except: return ""
    mn, mx = min(vals), max(vals)
    rng = mx - mn or 0.01
    w, h, n = 200, 36, len(vals)
    pts = " ".join(f"{int(i*(w/max(n-1,1)))},{int(h-(v-mn)/rng*(h-4)+2)}" for i, v in enumerate(vals))
    return (f'<svg class="spark {css_class}" viewBox="0 0 {w} {h}" preserveAspectRatio="none">'
            f'<polyline points="{pts}" /></svg>')


# ── HTML Generation ────────────────────────────────────────────────────────────

def generate_html(cb_rates, events, alerts, implied_moves, bot_risk=None):
    now_str = datetime.now(timezone.utc).strftime("%A, %B %d %Y — %H:%M UTC")
    now_ts  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── CB Rate Cards ──
    cb_cards_html = ""
    for cb, info in FRED_SERIES.items():
        rate = cb_rates.get(cb)
        if rate:
            val       = f"{rate['current']:.2f}%"
            prev      = f"{rate['previous']:.2f}%"
            arrow     = trend_arrow(rate["trend"])
            trend_cls = {"hiking":"trend-up","cutting":"trend-down","holding":"trend-flat"}.get(rate["trend"],"trend-flat")
            spark_cls = {"hiking":"spark-up","cutting":"spark-down","holding":"spark-flat"}.get(rate["trend"],"spark-flat")
            sparkline = generate_sparkline(rate.get("history",[]), spark_cls)
            as_of     = rate.get("date","")
        else:
            val = "N/A"; prev = "–"; arrow = ""; trend_cls = "trend-flat"; sparkline = ""; as_of = ""
        cb_cards_html += f"""
        <article class="card cb-card {trend_cls}">
          <header class="cb-header">
            <span class="cb-flag">{info['flag']}</span>
            <span class="cb-currency">{info['currency']}</span>
            <span class="cb-code">{cb}</span>
            <span class="cb-arrow {trend_cls}">{arrow}</span>
          </header>
          <div class="cb-rate">{val}</div>
          <div class="cb-name">{info['name']}</div>
          <div class="cb-prev">Prev: {prev}</div>
          <div class="cb-spark">{sparkline}<span class="spark-label">6-month rate history</span><span class="spark-date">as of {as_of}</span></div>
        </article>"""

    # ── Implied Rate Probability Cards ──
    if not implied_moves:
        implied_html = '<p class="no-data">Market-implied probability data unavailable — check FRED API key and connectivity.</p>'
    else:
        implied_html = ""
        for cb, imp in implied_moves.items():
            dir_cls   = imp["direction"]
            pct       = imp["probability"]
            fill_cls  = {"hike":"imp-hike","cut":"imp-cut","hold":"imp-hold"}.get(dir_cls,"imp-hold")
            pct_cls   = {"hike":"hike","cut":"cut","hold":"hold"}.get(dir_cls,"hold")
            dir_label = {"hike":"▲ HIKE","cut":"▼ CUT","hold":"◆ HOLD"}.get(dir_cls,dir_cls.upper())
            display_pct = min(int((100 - pct) if dir_cls == "hold" else pct), 99)
            next_mtg    = imp.get("next_meeting","")
            next_mtg_str= f"📅 {next_mtg} · " if next_mtg else ""
            implied_html += f"""
        <article class="card implied-card {fill_cls}">
          <header class="implied-header">
            <span class="implied-cb">{cb}</span>
            <span class="implied-label">Probability of next move</span>
          </header>
          <div class="implied-dir {pct_cls}">{dir_label} {display_pct}%</div>
          <div class="implied-rates">Current: {imp['current_rate']}% → Fwd: {imp['forward_rate']}% ({imp['spread_bp']:+.1f}bp)</div>
          <div class="implied-meta">{next_mtg_str}{imp.get('fwd_label','')}</div>
        </article>"""

    # ── Alert Cards ──
    real_alerts   = [a for a in alerts if a.get("days_away",999) < 998]
    struct_alerts = [a for a in alerts if a.get("days_away",999) >= 998]
    if not real_alerts and not struct_alerts:
        alert_html = '<p class="no-alerts">✓ No high-impact events in the next 7 days affecting your portfolio.</p>'
    else:
        alert_html = ""
        for a in sorted(real_alerts + struct_alerts, key=lambda x: x["days_away"]):
            pairs_str = " ".join(f'<span class="pair-tag">{p}</span>' for p in a["pairs"])
            bots_str  = " ".join(f'<span class="bot-tag">{b}</span>'  for b in a["bots"])
            fc_str    = f'<span class="fc">Forecast: {a["forecast"]}</span>' if a.get("forecast") else ""
            pr_str    = f'<span class="pr">Prev: {a["previous"]}</span>'     if a.get("previous") else ""
            pause_html= f'<div class="pause-rec">{a["pause_rec"]}</div>'      if a.get("pause_rec") else ""
            score_val = a.get("score", 0)
            score_col = "#ef4444" if score_val >= 70 else ("#f59e0b" if score_val >= 40 else "#22c55e")
            alert_html += f"""
        <article class="card alert-card {severity_class(a['severity'])}">
          <header class="alert-header">
            <span class="alert-cb">{a['cb']}</span>
            <span class="alert-sev">{a['severity'].upper()}</span>
            <span class="alert-score" style="color:{score_col}">Score {score_val}/100</span>
          </header>
          <div class="alert-body">
            {pause_html}
            <div class="alert-title">{a['event']}</div>
            <div class="alert-date">📅 {a['date']}</div>
            <div class="alert-forecast">{fc_str}{pr_str}</div>
            <div class="alert-pairs">Pairs at risk: {pairs_str}</div>
            <div class="alert-bots">Exposed bots: {bots_str}</div>
          </div>
        </article>"""

    # ── Calendar ──
    cal_rows = ""
    shown    = 0
    now_utc  = datetime.now(timezone.utc)
    for e in events[:40]:
        if not e["date"]: continue
        if e["date"] < now_utc - timedelta(hours=1): continue
        impact_cls  = "imp-high" if e["impact"] == "high" else "imp-med"
        cb_affected = any(e["cb"] in PAIR_CB_MAP.get(p,[]) for bot in PORTFOLIO.values() for p in bot["pairs"])
        row_cls     = "row-highlight" if cb_affected else ""
        is_past     = e["date"] < now_utc
        actual_str  = (f'<span class="actual-val">{e["actual"]}</span>' if e["actual"]
                       else ("–" if is_past else '<span class="pending">pending</span>'))
        cal_rows += f"""
        <tr class="{row_cls}">
          <td>{e['date'].strftime('%a %b %d')}</td>
          <td>{e['date'].strftime('%H:%M')}</td>
          <td>{e['country']}</td>
          <td>{e['title']}</td>
          <td class="{impact_cls}">{e['impact'].upper()}</td>
          <td>{e['forecast'] or '–'}</td>
          <td>{e['previous'] or '–'}</td>
          <td>{actual_str}</td>
        </tr>"""
        shown += 1
    if not shown:
        cal_rows = '<tr><td colspan="8" class="no-data">No events loaded — Forex Factory feed may be temporarily unavailable.</td></tr>'

    # ── Pair Exposure Map ──
    pair_rows = ""
    for pair in ALL_PAIRS:
        cbs       = PAIR_CB_MAP.get(pair,[])
        bots      = [bot for bot, cfg in PORTFOLIO.items() if pair in cfg["pairs"]]
        has_alert = any(a for a in alerts if pair in a["pairs"] and a["days_away"] < 999)
        risk_cls  = "risk-alert" if has_alert else "risk-ok"
        cbs_html  = " ".join(f'<span class="cb-tag">{c}</span>'  for c in cbs)
        bots_html = " ".join(f'<span class="bot-tag">{b}</span>' for b in bots)
        warn_icon = "⚠️" if has_alert else "✓"
        pair_rows += f"""
        <tr class="{risk_cls}">
          <td class="pair-name">{pair}</td>
          <td>{cbs_html}</td>
          <td>{bots_html}</td>
          <td class="alert-icon">{warn_icon}</td>
        </tr>"""

    # ── Bot Risk Banner (v1) ──
    if bot_risk:
        def _risk_color(s): return "#ef4444" if s >= 70 else ("#f59e0b" if s >= 40 else "#22c55e")
        banner_parts = []
        for bot, scores in bot_risk.items():
            s24, s72 = scores["24h"], scores["72h"]
            col = _risk_color(max(s24, s72))
            banner_parts.append(
                f'<span class="rb-bot"><span class="rb-name" style="color:{col}">{bot}</span>'
                f'<span class="rb-score">{s24}<small class="rb-72">/72h&nbsp;{s72}</small></span></span>'
            )
        risk_banner_html = (
            '<div class="risk-banner">'
            '<span class="rb-label">⚡ Macro Risk (24h)</span>'
            + "".join(banner_parts) + "</div>"
        )
    else:
        risk_banner_html = ""

    # ── Full HTML ──────────────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>Macro Intel Dashboard — FX Bot Portfolio</title>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <meta name="generated-at" content="{now_ts}"/>
  <style>
    /* ── Reset & Base ── */
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    html {{ font-size: 15px; }}
    body {{
      background: #0d0d14;
      color: #e2e8f0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Inter", sans-serif;
      min-height: 100vh;
      padding: 0 0 60px;
    }}
    a {{ color: inherit; text-decoration: none; }}

    /* ── Layout ── */
    .layout {{ max-width: 1400px; margin: 0 auto; padding: 0 20px; }}

    /* ── Topbar ── */
    .topbar {{
      display: flex; justify-content: space-between; align-items: flex-end;
      padding: 28px 0 18px;
      border-bottom: 1px solid #1e1e30;
      margin-bottom: 20px;
      flex-wrap: wrap; gap: 10px;
    }}
    .topbar h1 {{ font-size: 1.35rem; font-weight: 700; color: #f1f5f9; letter-spacing: -.3px; }}
    .topbar p  {{ font-size: 0.78rem; color: #64748b; margin-top: 3px; }}
    .ts-label  {{ font-size: 0.7rem; color: #475569; text-align: right; }}
    .ts-value  {{ font-size: 0.82rem; color: #94a3b8; text-align: right; }}

    /* ── Risk Banner ── */
    .risk-banner {{
      background: #12121f;
      border: 1px solid #1e1e35;
      border-left: 3px solid #a78bfa;
      border-radius: 8px;
      padding: 10px 18px;
      margin-bottom: 24px;
      display: flex; flex-wrap: wrap; align-items: center; gap: 6px 16px;
    }}
    .rb-label  {{ font-size: 0.78rem; color: #64748b; font-weight: 600; margin-right: 4px; }}
    .rb-bot    {{ display: inline-flex; align-items: center; gap: 6px; }}
    .rb-name   {{ font-size: 0.85rem; font-weight: 700; }}
    .rb-score  {{ font-size: 0.85rem; color: #94a3b8; }}
    .rb-72     {{ font-size: 0.72rem; color: #475569; }}

    /* ── Section headers ── */
    .section-title {{
      font-size: 0.72rem; font-weight: 700; letter-spacing: .08em;
      text-transform: uppercase; color: #475569;
      margin-bottom: 14px; padding-bottom: 8px;
      border-bottom: 1px solid #1e1e30;
    }}

    /* ── Card grid ── */
    .card-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(175px, 1fr));
      gap: 12px;
      margin-bottom: 32px;
    }}
    .card-grid-wide {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
      gap: 12px;
      margin-bottom: 32px;
    }}
    .alerts-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
      gap: 12px;
      margin-bottom: 32px;
    }}

    /* ── Base Card ── */
    .card {{
      background: #12121f;
      border: 1px solid #1e1e30;
      border-radius: 10px;
      padding: 14px 16px;
      transition: border-color .15s;
    }}
    .card:hover {{ border-color: #2d2d50; }}

    /* ── CB Rate Cards ── */
    .cb-header {{ display: flex; align-items: center; gap: 6px; margin-bottom: 10px; }}
    .cb-flag   {{ font-size: 1.1rem; }}
    .cb-currency {{ font-size: 0.7rem; font-weight: 700; color: #64748b; }}
    .cb-code   {{ font-size: 0.7rem; font-weight: 800; color: #94a3b8; }}
    .cb-arrow  {{ margin-left: auto; font-size: 0.85rem; font-weight: 700; }}
    .trend-up .cb-arrow {{ color: #22c55e; }}
    .trend-down .cb-arrow {{ color: #ef4444; }}
    .trend-flat .cb-arrow {{ color: #64748b; }}
    .cb-rate   {{ font-size: 1.6rem; font-weight: 800; color: #f1f5f9; line-height: 1; margin-bottom: 4px; }}
    .trend-up .cb-rate {{ color: #4ade80; }}
    .trend-down .cb-rate {{ color: #f87171; }}
    .cb-name   {{ font-size: 0.72rem; color: #64748b; margin-bottom: 2px; }}
    .cb-prev   {{ font-size: 0.72rem; color: #475569; margin-bottom: 8px; }}
    .cb-spark  {{ display: flex; flex-direction: column; gap: 3px; }}
    .spark-label {{ font-size: 0.65rem; color: #334155; }}
    .spark-date  {{ font-size: 0.65rem; color: #334155; }}

    /* ── Sparklines ── */
    .spark {{ width: 100%; height: 28px; overflow: visible; }}
    .spark polyline {{ fill: none; stroke-width: 1.8; vector-effect: non-scaling-stroke; }}
    .spark-up   polyline {{ stroke: #22c55e; }}
    .spark-down polyline {{ stroke: #ef4444; }}
    .spark-flat polyline {{ stroke: #475569; }}

    /* ── Implied Rate Cards ── */
    .implied-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }}
    .implied-cb    {{ font-size: 0.9rem; font-weight: 800; color: #f1f5f9; }}
    .implied-label {{ font-size: 0.68rem; color: #475569; }}
    .implied-dir   {{ font-size: 1.15rem; font-weight: 800; margin-bottom: 6px; }}
    .implied-dir.hike {{ color: #4ade80; }}
    .implied-dir.cut  {{ color: #f87171; }}
    .implied-dir.hold {{ color: #94a3b8; }}
    .implied-rates {{ font-size: 0.72rem; color: #64748b; margin-bottom: 4px; }}
    .implied-meta  {{ font-size: 0.68rem; color: #334155; }}
    .imp-hike {{ border-left: 3px solid #22c55e !important; }}
    .imp-cut  {{ border-left: 3px solid #ef4444 !important; }}
    .imp-hold {{ border-left: 3px solid #334155 !important; }}

    /* ── Alert Cards ── */
    .alert-header {{
      display: flex; align-items: center; gap: 8px;
      margin-bottom: 10px; flex-wrap: wrap;
    }}
    .alert-cb    {{ font-size: 0.78rem; font-weight: 800; color: #f1f5f9; }}
    .alert-sev   {{ font-size: 0.65rem; font-weight: 700; padding: 2px 7px; border-radius: 4px; }}
    .alert-score {{ font-size: 0.68rem; font-weight: 700; margin-left: auto; }}
    .sev-critical .alert-sev {{ background: #7f1d1d; color: #fca5a5; }}
    .sev-high     .alert-sev {{ background: #78350f; color: #fcd34d; }}
    .sev-medium   .alert-sev {{ background: #1e293b; color: #94a3b8; }}
    .sev-critical {{ border-left: 3px solid #ef4444 !important; }}
    .sev-high     {{ border-left: 3px solid #f59e0b !important; }}
    .sev-medium   {{ border-left: 3px solid #334155 !important; }}
    .alert-title  {{ font-size: 0.85rem; font-weight: 600; color: #e2e8f0; margin-bottom: 5px; }}
    .alert-date   {{ font-size: 0.75rem; color: #64748b; margin-bottom: 5px; }}
    .alert-forecast {{ font-size: 0.72rem; color: #475569; margin-bottom: 6px; display: flex; gap: 12px; flex-wrap: wrap; }}
    .fc, .pr      {{ color: #64748b; }}
    .alert-pairs, .alert-bots {{ font-size: 0.72rem; color: #64748b; margin-bottom: 4px; display: flex; gap: 5px; align-items: center; flex-wrap: wrap; }}
    .pause-rec    {{ font-size: 0.68rem; font-weight: 700; color: #fcd34d; background: #451a03; padding: 3px 8px; border-radius: 4px; margin-bottom: 8px; display: inline-block; }}

    /* ── Tags ── */
    .pair-tag {{ background: #1e2a3a; color: #7dd3fc; font-size: 0.68rem; font-weight: 700; padding: 2px 6px; border-radius: 4px; }}
    .bot-tag  {{ background: #1a2030; color: #a78bfa; font-size: 0.68rem; font-weight: 700; padding: 2px 6px; border-radius: 4px; }}
    .cb-tag   {{ background: #1a1f2e; color: #94a3b8; font-size: 0.68rem; font-weight: 700; padding: 2px 6px; border-radius: 4px; }}

    /* ── Tables ── */
    .table-wrap {{ overflow-x: auto; margin-bottom: 32px; border-radius: 10px; border: 1px solid #1e1e30; }}
    table       {{ width: 100%; border-collapse: collapse; font-size: 0.8rem; }}
    th {{
      background: #0d0d1a; color: #475569; font-size: 0.68rem;
      font-weight: 700; text-transform: uppercase; letter-spacing: .06em;
      padding: 10px 14px; text-align: left; border-bottom: 1px solid #1e1e30;
    }}
    td {{ padding: 9px 14px; border-bottom: 1px solid #161625; color: #94a3b8; vertical-align: middle; }}
    tr:last-child td {{ border-bottom: none; }}
    tr:hover td {{ background: #12121f; }}
    .row-highlight td {{ background: #0f1421; }}
    .row-highlight:hover td {{ background: #141928; }}
    .risk-alert td:first-child {{ border-left: 3px solid #f59e0b; }}
    .risk-ok    td:first-child {{ border-left: 3px solid #1e293b; }}
    .pair-name {{ font-weight: 700; color: #e2e8f0; }}
    .alert-icon {{ font-size: 1rem; }}
    .imp-high {{ color: #fca5a5; font-weight: 700; }}
    .imp-med  {{ color: #fcd34d; }}
    .actual-val {{ color: #4ade80; font-weight: 600; }}
    .pending  {{ color: #334155; font-style: italic; }}
    .no-data  {{ color: #334155; font-style: italic; padding: 16px; display: block; }}
    .no-alerts {{ color: #22c55e; padding: 16px 0; font-size: 0.85rem; }}
  </style>
</head>
<body>
  <div class="layout">

    <header class="topbar">
      <div>
        <h1>Macro Intel Dashboard</h1>
        <p>Central bank policy &amp; macro risk overlay for your EA bot portfolio</p>
      </div>
      <div>
        <div class="ts-label">Last updated (UTC)</div>
        <div class="ts-value">{now_str}</div>
      </div>
    </header>

    {risk_banner_html}

    <div class="section-title">Central Bank Policy Rates</div>
    <div class="card-grid">
      {cb_cards_html}
    </div>

    <div class="section-title">📊 Market-Implied Rate Move Probability</div>
    <div class="card-grid-wide">
      {implied_html}
    </div>

    <div class="section-title">⚠ Portfolio Volatility Alerts — Next 7 Days</div>
    <div class="alerts-grid">
      {alert_html}
    </div>

    <div class="section-title">Portfolio Pair Exposure Map</div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Pair</th><th>Central Banks</th><th>Active Bots</th><th>Alert</th></tr></thead>
        <tbody>{pair_rows}</tbody>
      </table>
    </div>

    <div class="section-title">High-Impact Economic Calendar — Next 14 Days</div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Date</th><th>Time (UTC)</th><th>Country</th><th>Event</th>
            <th>Impact</th><th>Forecast</th><th>Previous</th><th>Actual</th>
          </tr>
        </thead>
        <tbody>{cal_rows}</tbody>
      </table>
    </div>

  </div>
</body>
</html>"""
    return html


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Starting macro dashboard generation...")

    print("  Fetching central bank rates from FRED...")
    cb_rates  = {}
    rp_slugs  = {"FED":"fed","ECB":"ecb","BOE":"boe","BOJ":"boj","BOC":"boc","RBA":"rba"}
    for cb, info in FRED_SERIES.items():
        result = None
        for sid in info["ids"]:
            print(f"  → {cb} trying {sid}")
            result = fetch_fred_series(sid)
            if result: print(f"  → ✓ {cb} from {sid}: {result['current']}%"); break
        if not result:
            slug = rp_slugs.get(cb)
            if slug:
                print(f"  → {cb} FRED failed — trying rateprobability.com...")
                rp = fetch_rateprobability(slug)
                result = _rp_to_cb_rate(rp)
                if result: print(f"  → ✓ {cb} from RP: {result['current']}%")
        cb_rates[cb] = result
    loaded = sum(1 for v in cb_rates.values() if v)
    print(f"  Loaded {loaded}/{len(FRED_SERIES)} CB rates")

    print("  Fetching implied rate probabilities...")
    implied_moves = fetch_implied_rate_changes(cb_rates)
    print(f"  Loaded {len(implied_moves)} implied estimates")

    print("  Fetching Forex Factory calendar...")
    events = fetch_forex_factory_calendar()
    print(f"  Found {len(events)} events")

    alerts = compute_alerts(cb_rates, events, implied_moves)
    print(f"  Generated {len(alerts)} alerts")

    bot_risk = _bot_risk_indices(alerts)
    print(f"  Bot risk indices: {bot_risk}")

    print("  Generating HTML dashboard...")
    html = generate_html(cb_rates, events, alerts, implied_moves, bot_risk)

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "index.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"  ✓ Dashboard written to {out_path}")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Done.")


if __name__ == "__main__":
    main()
