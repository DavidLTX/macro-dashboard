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
    "FX InControl":  {"pairs": ["EURJPY", "USDCAD", "EURGBP"], "strategy": "Fibonacci Grid", "color": "#a78bfa"},
    "FX JetBot":     {"pairs": ["EURUSD", "EURJPY", "USDCAD", "AUDUSD", "EURGBP"], "strategy": "Dual-Dir Grid", "color": "#22c55e"},
    "Happy Gold":    {"pairs": ["XAUUSD"], "strategy": "Fixed-Lot Scalper", "color": "#fcd34d"},
    "Happy Power":   {"pairs": ["EURCHF"], "strategy": "Grid Scalper", "color": "#f97316"},
    "Hedge EA":      {"pairs": ["AUDCAD"], "strategy": "Dual-Dir Grid", "color": "#2dd4bf"},
}

# All unique pairs across portfolio
ALL_PAIRS = sorted(set(p for bot in PORTFOLIO.values() for p in bot["pairs"]))

# Pair → central banks that drive volatility
PAIR_CB_MAP = {
    "EURJPY":  ["ECB", "BOJ"],
    "USDCAD":  ["FED", "BOC"],
    "EURGBP":  ["ECB", "BOE"],
    "EURUSD":  ["ECB", "FED"],
    "AUDUSD":  ["RBA", "FED"],
    "AUDCAD":  ["RBA", "BOC"],
    "XAUUSD":  ["FED"],
    "EURCHF":  ["ECB", "SNB"],
}

# FRED series for central bank policy rates
FRED_SERIES = {
    "FED": {"id": "FEDFUNDS",   "name": "Fed Funds Rate",    "currency": "USD", "flag": "🇺🇸"},
    "ECB": {"id": "ECBDFR",     "name": "ECB Deposit Rate",  "currency": "EUR", "flag": "🇪🇺"},
    "BOE": {"id": "BOEBR",      "name": "BOE Base Rate",     "currency": "GBP", "flag": "🇬🇧"},
    "BOJ": {"id": "IRSTCB01JPM156N", "name": "BOJ Policy Rate", "currency": "JPY", "flag": "🇯🇵"},
    "BOC": {"id": "IRSTCB01CAM156N", "name": "BOC Policy Rate", "currency": "CAD", "flag": "🇨🇦"},
    "RBA": {"id": "IRSTCB01AUM156N", "name": "RBA Cash Rate", "currency": "AUD", "flag": "🇦🇺"},
    "SNB": {"id": "IRSTCB01CHM156N", "name": "SNB Policy Rate", "currency": "CHF", "flag": "🇨🇭"},
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

def fetch_forex_factory_calendar():
    """Fetch Forex Factory RSS feed for high-impact events."""
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
    events = []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read()
        root = ET.fromstring(content)
        channel = root.find("channel")
        if channel is None:
            return events
        for item in channel.findall("item"):
            title    = item.findtext("title", "")
            date_str = item.findtext("date", "")
            country  = item.findtext("country", "").upper()
            impact   = item.findtext("impact", "").lower()
            forecast = item.findtext("forecast", "")
            previous = item.findtext("previous", "")
            actual   = item.findtext("actual", "")

            if impact not in ("high", "medium"):
                continue

            # Parse date
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S%z")
            except:
                try:
                    dt = datetime.strptime(date_str[:16], "%Y-%m-%dT%H:%M")
                    dt = dt.replace(tzinfo=timezone.utc)
                except:
                    dt = None

            # Map country to CB
            cb_map = {"US":"FED","EU":"ECB","GB":"BOE","JP":"BOJ","CA":"BOC","AU":"RBA","CH":"SNB","NZ":"RBNZ"}
            cb = cb_map.get(country, country)

            events.append({
                "title": title, "date": dt, "date_str": date_str[:10] if date_str else "",
                "country": country, "cb": cb, "impact": impact,
                "forecast": forecast, "previous": previous, "actual": actual,
            })
    except Exception as e:
        print(f"  Forex Factory error: {e}")
    return sorted(events, key=lambda x: x["date"] or datetime.min.replace(tzinfo=timezone.utc))

def compute_alerts(cb_rates, upcoming_events):
    """Generate volatility alerts by correlating events with portfolio pairs."""
    alerts = []
    now = datetime.now(timezone.utc)
    next_7d = now + timedelta(days=7)

    # Events in next 7 days
    soon = [e for e in upcoming_events if e["date"] and now <= e["date"] <= next_7d]

    for event in soon:
        affected_pairs = []
        affected_bots  = []
        for pair, cbs in PAIR_CB_MAP.items():
            if event["cb"] in cbs:
                affected_pairs.append(pair)
        for bot, cfg in PORTFOLIO.items():
            if any(p in affected_pairs for p in cfg["pairs"]):
                affected_bots.append(bot)

        if not affected_pairs:
            continue

        days_away = (event["date"] - now).days
        severity = "critical" if days_away <= 1 else ("high" if days_away <= 3 else "medium")

        alerts.append({
            "event": event["title"],
            "cb": event["cb"],
            "date": event["date"].strftime("%a %b %d, %H:%M UTC") if event["date"] else "TBD",
            "days_away": days_away,
            "pairs": affected_pairs,
            "bots": affected_bots,
            "severity": severity,
            "impact": event["impact"],
            "forecast": event["forecast"],
            "previous": event["previous"],
        })

    # Rate divergence alerts
    loaded = {k: v for k, v in cb_rates.items() if v}
    for pair, cbs in PAIR_CB_MAP.items():
        if len(cbs) == 2 and all(c in loaded for c in cbs):
            a, b = loaded[cbs[0]], loaded[cbs[1]]
            if a["trend"] != b["trend"] and "holding" not in [a["trend"], b["trend"]]:
                affected_bots = [bot for bot, cfg in PORTFOLIO.items() if pair in cfg["pairs"]]
                alerts.append({
                    "event": f"Policy Divergence: {cbs[0]} {a['trend']} vs {cbs[1]} {b['trend']}",
                    "cb": f"{cbs[0]}/{cbs[1]}",
                    "date": "Structural",
                    "days_away": 999,
                    "pairs": [pair],
                    "bots": affected_bots,
                    "severity": "medium",
                    "impact": "structural",
                    "forecast": "",
                    "previous": "",
                })

    return alerts

# ── HTML Generation ────────────────────────────────────────────────────────────

def trend_arrow(trend):
    if trend == "hiking":  return '<span class="arrow up">▲</span>'
    if trend == "cutting": return '<span class="arrow down">▼</span>'
    return '<span class="arrow flat">◆</span>'

def severity_class(s):
    return {"critical": "sev-critical", "high": "sev-high", "medium": "sev-medium"}.get(s, "sev-medium")

def generate_html(cb_rates, events, alerts):
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
            sparkline = generate_sparkline(rate.get("history", []))
            as_of = rate.get("date", "")
        else:
            val = "N/A"; prev = "–"; arrow = ""; trend_cls = "trend-flat"; sparkline = ""; as_of = ""

        cb_cards_html += f"""
        <div class="cb-card {trend_cls}">
          <div class="cb-header">
            <span class="cb-flag">{info['flag']}</span>
            <span class="cb-name">{cb}</span>
            {arrow}
          </div>
          <div class="cb-rate">{val}</div>
          <div class="cb-sub">{info['name']}</div>
          <div class="cb-prev">Prev: {prev}</div>
          {sparkline}
          <div class="cb-asof">as of {as_of}</div>
        </div>"""

    # ── Alert Cards ──
    alert_html = ""
    if not alerts:
        alert_html = '<div class="no-alerts">✓ No high-impact events in the next 7 days affecting your portfolio.</div>'
    else:
        for a in sorted(alerts, key=lambda x: x["days_away"]):
            pairs_str = " ".join(f'<span class="pair-tag">{p}</span>' for p in a["pairs"])
            bots_str  = " ".join(f'<span class="bot-tag">{b}</span>' for b in a["bots"])
            fc_str    = f'<span class="forecast">Forecast: {a["forecast"]}</span>' if a["forecast"] else ""
            pr_str    = f'<span class="forecast-prev">Prev: {a["previous"]}</span>' if a["previous"] else ""
            alert_html += f"""
            <div class="alert-card {severity_class(a['severity'])}">
              <div class="alert-top">
                <span class="alert-cb">{a['cb']}</span>
                <span class="alert-sev">{a['severity'].upper()}</span>
              </div>
              <div class="alert-event">{a['event']}</div>
              <div class="alert-date">📅 {a['date']}</div>
              <div class="alert-meta">{fc_str}{pr_str}</div>
              <div class="alert-pairs">Pairs at risk: {pairs_str}</div>
              <div class="alert-bots">Exposed bots: {bots_str}</div>
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
        cal_rows = '<tr><td colspan="8" class="no-data">No events loaded — check Forex Factory connectivity</td></tr>'

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
    def mini_spark(history):
        if len(history) < 2:
            return ""
        vals = [v for _, v in history]
        mn, mx = min(vals), max(vals)
        rng = mx - mn or 0.01
        w, h = 60, 20
        pts = " ".join(
            f"{int(i*(w/(len(vals)-1)))},{int(h - (v-mn)/rng*h)}"
            for i, (_, v) in enumerate(history)
        )
        return f'<svg class="spark" viewBox="0 0 {w} {h}"><polyline points="{pts}" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>'

    spark_lookup = {cb: mini_spark(cb_rates[cb]["history"]) if cb_rates.get(cb) else "" for cb in FRED_SERIES}

    # ── Full HTML ──────────────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="3600">
<title>Macro Dashboard — FX Bot Portfolio</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:ital,wght@0,400;0,700;1,400&family=Syne:wght@400;600;800&display=swap" rel="stylesheet">
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
  --dim:      #7777aa;
  --dimmer:   #44445a;
}}
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
html {{ scroll-behavior: smooth; }}
body {{
  background: var(--bg);
  color: var(--text);
  font-family: 'Space Mono', monospace;
  font-size: 13px;
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
  font-family: 'Syne', sans-serif;
  font-size: 28px;
  font-weight: 800;
  letter-spacing: -0.5px;
  color: #fff;
}}
.header-left h1 span {{ color: var(--purple); }}
.header-sub {{
  color: var(--dim);
  font-size: 11px;
  margin-top: 6px;
  letter-spacing: 0.05em;
}}
.header-right {{
  text-align: right;
}}
.updated-label {{
  font-size: 10px;
  color: var(--dimmer);
  letter-spacing: 0.08em;
  text-transform: uppercase;
}}
.updated-time {{
  font-size: 12px;
  color: var(--dim);
  margin-top: 2px;
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
  font-family: 'Syne', sans-serif;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.15em;
  text-transform: uppercase;
  color: var(--dim);
  margin-bottom: 16px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--border);
}}

/* ── CB Rate Cards ── */
.cb-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 12px;
}}
.cb-card {{
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 16px;
  position: relative;
  overflow: hidden;
  transition: border-color 0.2s;
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
.cb-card:hover {{ border-color: var(--border2); }}
.cb-header {{
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
}}
.cb-flag {{ font-size: 16px; }}
.cb-name {{
  font-family: 'Syne', sans-serif;
  font-size: 13px;
  font-weight: 700;
  color: #fff;
  flex: 1;
}}
.arrow {{ font-size: 11px; }}
.arrow.up   {{ color: var(--red); }}
.arrow.down {{ color: var(--green); }}
.arrow.flat {{ color: var(--blue); font-size: 8px; }}
.cb-rate {{
  font-family: 'Syne', sans-serif;
  font-size: 26px;
  font-weight: 800;
  color: #fff;
  letter-spacing: -1px;
}}
.cb-sub  {{ font-size: 10px; color: var(--dim); margin-top: 2px; }}
.cb-prev {{ font-size: 10px; color: var(--dimmer); margin-top: 4px; }}
.cb-asof {{ font-size: 9px; color: var(--dimmer); margin-top: 6px; }}
.spark {{
  width: 60px; height: 20px;
  margin-top: 8px;
  color: var(--purple);
  opacity: 0.7;
}}

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
  font-family: 'Syne', sans-serif;
  font-weight: 700;
  color: #fff;
  font-size: 13px;
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
.alert-event {{ font-size: 13px; color: var(--text); margin-bottom: 6px; line-height: 1.4; }}
.alert-date  {{ font-size: 11px; color: var(--dim); margin-bottom: 8px; }}
.alert-meta  {{ font-size: 11px; color: var(--dim); margin-bottom: 8px; display: flex; gap: 12px; }}
.forecast      {{ color: var(--blue); }}
.forecast-prev {{ color: var(--dimmer); }}
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
  font-size: 10px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--dim);
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  font-weight: 400;
}}
td {{
  padding: 9px 12px;
  border-bottom: 1px solid var(--border);
  font-size: 12px;
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
.pair-name {{ font-family: 'Syne', sans-serif; font-weight: 700; color: #fff; font-size: 14px; }}
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
  color: var(--dimmer);
  font-size: 10px;
  flex-wrap: wrap;
  gap: 8px;
}}
.footer a {{ color: var(--dim); text-decoration: none; }}
.footer a:hover {{ color: var(--text); }}

@media (max-width: 600px) {{
  .header, .main, .footer {{ padding-left: 16px; padding-right: 16px; }}
  .cb-grid {{ grid-template-columns: repeat(2, 1fr); }}
}}
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

  <!-- ── Section 2: Alerts ── -->
  <section>
    <div class="section-title">⚠ Portfolio Volatility Alerts — Next 7 Days</div>
    <div class="alerts-grid">
      {alert_html}
    </div>
  </section>

  <!-- ── Section 3: Portfolio Pair Map ── -->
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

  <!-- ── Section 4: Event Calendar ── -->
  <section>
    <div class="section-title">High-Impact Economic Calendar — This Week</div>
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


def generate_sparkline(history):
    if len(history) < 2:
        return ""
    vals = [v for _, v in history]
    mn, mx = min(vals), max(vals)
    rng = mx - mn or 0.01
    w, h = 60, 18
    pts = " ".join(
        f"{int(i*(w/max(len(vals)-1,1)))},{int(h - (v-mn)/rng*(h-2)+1)}"
        for i, (_, v) in enumerate(vals)
    )
    return f'<svg class="spark" viewBox="0 0 {w} {h}"><polyline points="{pts}" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>'


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

    # 2. Fetch calendar
    print("  Fetching Forex Factory calendar...")
    events = fetch_forex_factory_calendar()
    print(f"  Found {len(events)} high/medium-impact events")

    # 3. Compute alerts
    alerts = compute_alerts(cb_rates, events)
    print(f"  Generated {len(alerts)} portfolio alerts")

    # 4. Generate HTML
    print("  Generating HTML dashboard...")
    html = generate_html(cb_rates, events, alerts)

    # 5. Write output
    out_path = os.path.join(os.path.dirname(__file__), "docs", "index.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"  ✓ Dashboard written to {out_path}")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Done.")

if __name__ == "__main__":
    main()
