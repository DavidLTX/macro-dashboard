# Macro Intel Dashboard

Auto-updating forex macro dashboard for monitoring central bank policy and volatility risk across an active EA bot portfolio.

## What It Does

- Pulls **live central bank rates** from FRED (Fed, ECB, BOE, BOJ, BOC, RBA, SNB)
- Fetches **this week's high-impact economic events** from Forex Factory
- Generates **portfolio volatility alerts** — correlating upcoming CB decisions with your active bot pairs
- Publishes a **dark-theme HTML dashboard** to GitHub Pages, auto-refreshing hourly

## Setup

See the step-by-step guide in the project README or follow these steps:

1. Fork or clone this repo
2. Get a free FRED API key at https://fred.stlouisfed.org/docs/api/api_key.html
3. Add it as a GitHub Secret named `FRED_API_KEY`
4. Enable GitHub Pages (Settings → Pages → Source: Deploy from branch `main`, folder `/docs`)
5. Trigger a manual run from Actions tab to generate the first dashboard

## Schedule

Runs automatically at **06:00 UTC** and **13:00 UTC** daily (before London and New York opens).

## Data Sources

- [FRED API](https://fred.stlouisfed.org) — Central bank policy rates (free, no rate limit issues)
- [Forex Factory](https://www.forexfactory.com) — Economic calendar RSS feed (free, public)
