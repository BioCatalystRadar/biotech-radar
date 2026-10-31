import json
import os
import time
import requests
from datetime import datetime, timezone

SEC_HEADERS = {
    "User-Agent": "BioCatalystRadar contact@biocatalystradar.com",
    "Accept-Encoding": "gzip, deflate"
}

# Expanded Master Watchlist of High-Volatility & Clinical-Stage Biotechs
BIOTECH_WATCHLIST = {
    "IBRX": {"cik": "0001326110", "sponsor": "ImmunityBio"},
    "VKTX": {"cik": "0001607678", "sponsor": "Viking Therapeutics"},
    "CRSP": {"cik": "0001674416", "sponsor": "CRISPR Therapeutics"},
    "BEAM": {"cik": "0001745999", "sponsor": "Beam Therapeutics"},
    "IOVA": {"cik": "0001425205", "sponsor": "Iovance Biotherapeutics"},
    "BDTX": {"cik": "0001701605", "sponsor": "Black Diamond Therapeutics"},
    "JANX": {"cik": "0001818382", "sponsor": "Janux Therapeutics"},
    "CGON": {"cik": "0001993748", "sponsor": "CG Oncology"},
    "NUVL": {"cik": "0001861560", "sponsor": "Nuvalent"},
    "SUMO": {"cik": "0001645469", "sponsor": "Summit Therapeutics"},
    "MRNA": {"cik": "0001682852", "sponsor": "Moderna"},
    "BNTX": {"cik": "0001776985", "sponsor": "BioNTech"},
    "SAVA": {"cik": "0001274494", "sponsor": "Cassava Sciences"},
    "RXRX": {"cik": "0001579588", "sponsor": "Recursion Pharmaceuticals"},
    "ABOS": {"cik": "0001815159", "sponsor": "Acumen Pharmaceuticals"}
}

STATE_FILE = "previous_state.json"
HTML_OUTPUT = "index.html"


def get_latest_xbrl_val(facts: dict, tags: list) -> tuple:
    for tag in tags:
        if tag in facts:
            units = facts[tag].get("units", {}).get("USD", [])
            valid = [u for u in units if u.get("form") in ("10-Q", "10-K") and "val" in u]
            if valid:
                latest = sorted(valid, key=lambda x: x.get("end", ""))[-1]
                return latest["val"], latest.get("end")
    return 0, None


def get_sec_financials(cik: str) -> dict:
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik.zfill(10)}.json"
    try:
        resp = requests.get(url, headers=SEC_HEADERS, timeout=15)
        if resp.status_code != 200:
            return {"total_liquidity_usd": 0, "as_of": "N/A"}
        facts = resp.json().get("facts", {}).get("us-gaap", {})

        combined, dt = get_latest_xbrl_val(facts, ["CashCashEquivalentsAndShortTermInvestments"])
        if combined > 0:
            total_cash, as_of = combined, dt
        else:
            cash, dt1 = get_latest_xbrl_val(facts, ["CashAndCashEquivalentsAtCarryingValue"])
            mkt, dt2 = get_latest_xbrl_val(facts, [
                "MarketableSecuritiesCurrent",
                "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
                "ShortTermInvestments"
            ])
            total_cash = cash + (mkt if dt1 == dt2 else 0)
            as_of = dt1 or "N/A"
        return {"total_liquidity_usd": total_cash, "as_of": as_of}
    except Exception as e:
        print(f"SEC error for {cik}: {e}")
        return {"total_liquidity_usd": 0, "as_of": "N/A"}


def get_active_trials(sponsor: str) -> dict:
    url = "https://clinicaltrials.gov/api/v2/studies"
    params = {
        "query.spons": sponsor,
        "filter.overallStatus": "RECRUITING,ACTIVE_NOT_RECRUITING,NOT_YET_RECRUITING",
        "pageSize": 25
    }
    trials = {}
    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code != 200:
            return trials
        for s in resp.json().get("studies", []):
            p = s.get("protocolSection", {})
            ident = p.get("identificationModule", {})
            stat = p.get("statusModule", {})
            des = p.get("designModule", {})
            out_mod = p.get("outcomesModule", {})

            nct_id = ident.get("nctId")
            if not nct_id:
                continue

            phases = des.get("phases", ["N/A"])
            prim = out_mod.get("primaryOutcomes", [])
            endpoint = prim[0].get("measure", "N/A") if prim else "N/A"

            trials[nct_id] = {
                "nct_id": nct_id,
                "title": ident.get("briefTitle", ""),
                "status": stat.get("overallStatus", ""),
                "phase": "/".join(phases),
                "completion": stat.get("primaryCompletionDateStruct", {}).get("date", "N/A"),
                "enrollment": des.get("enrollmentInfo", {}).get("count", 0),
                "endpoint": endpoint
            }
    except Exception as e:
        print(f"Trials error for {sponsor}: {e}")
    return trials


def build_html(companies: list, updated_str: str) -> str:
    # Build featured spotlight cards for high-profile tickers (e.g., IBRX, VKTX, MRNA)
    spotlight_tickers = ["ibrx", "vktx", "mrna", "crsp"]
    spotlight_html = ""
    cards_html = ""

    for c in companies:
        liq = f"${c['cash_usd']/1e6:,.1f}M" if c["cash_usd"] else "N/A"
        sorted_trials = sorted(c["trials"].values(), key=lambda x: x["completion"])
        
        rows = ""
        for t in sorted_trials[:5]:
            rows += f"""
            <tr>
              <td><a href="https://clinicaltrials.gov/study/{t['nct_id']}" target="_blank" class="nct-link">{t['nct_id']}</a><div class="sub">{t['phase']}</div></td>
              <td><div class="trial-title">{t['title']}</div><div class="sub">Endpoint: {t['endpoint'][:65]}...</div></td>
              <td><span class="status-pill">{t['status']}</span></td>
              <td class="readout">{t['completion']}</td>
              <td class="num">{t['enrollment']:,}</td>
            </tr>"""

        card_block = f"""
        <div class="card" data-ticker="{c['ticker'].lower()} {c['sponsor'].lower()}">
          <div class="card-header">
            <div>
              <span class="ticker">${c['ticker']}</span>
              <span class="sponsor">{c['sponsor']}</span>
            </div>
            <div class="cash-pill">💰 SEC Liquidity: {liq} <span class="sub-light">({c['cash_as_of']})</span></div>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr><th>Trial ID</th><th>Study & Primary Endpoint</th><th>Status</th><th>Est. Readout</th><th style="text-align:right">Target N</th></tr>
              </thead>
              <tbody>{rows}</tbody>
            </table>
          </div>
          <div class="card-footer">
            <span>📡 Live SEC XBRL & ClinicalTrials.gov sync</span>
            <div>
              <a href="https://clinicaltrials.gov/search?term={c['sponsor']}" target="_blank" class="cta-btn secondary">View All Trials ↗</a>
              <a href="https://www.tradingview.com/symbols/{c['ticker']}/" target="_blank" class="cta-btn">Chart (${c['ticker']}) ↗</a>
            </div>
          </div>
        </div>"""

        cards_html += card_block
        if c['ticker'].lower() in spotlight_tickers:
            spotlight_html += card_block

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>BioCatalyst Diff & Liquidity Radar | Master Watchlist</title>
  <style>
    body {{ background:#020617; color:#f8fafc; font-family:system-ui,-apple-system,sans-serif; margin:0; padding:20px; }}
    .container {{ max-width:1000px; margin:0 auto; }}
    header {{ border-bottom:1px solid #1e293b; padding-bottom:18px; margin-bottom:20px; display:flex; justify-content:space-between; align-items:flex-end; flex-wrap:wrap; gap:12px; }}
    h1 {{ margin:0; font-size:26px; color:#f8fafc; }}
    .subtitle {{ color:#94a3b8; font-size:14px; margin-top:4px; }}
    .search-box {{ background:#0f172a; border:1px solid #334155; color:#fff; padding:10px 14px; border-radius:8px; width:280px; font-size:14px; }}
    .section-title {{ font-size:18px; font-weight:700; color:#38bdf8; margin:24px 0 12px 0; border-left:4px solid #38bdf8; padding-left:10px; }}
    .card {{ background:#0f172a; border:1px solid #1e293b; border-radius:12px; padding:18px; margin-bottom:18px; }}
    .card-header {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; flex-wrap:wrap; gap:8px; }}
    .ticker {{ font-size:22px; font-weight:800; color:#38bdf8; }}
    .sponsor {{ color:#94a3b8; font-size:15px; margin-left:8px; }}
    .cash-pill {{ background:#064e3b; color:#34d399; padding:6px 12px; border-radius:99px; font-size:13px; font-weight:700; }}
    .table-wrap {{ overflow-x:auto; }}
    table {{ width:100%; border-collapse:collapse; text-align:left; font-size:13px; }}
    th {{ color:#64748b; font-size:11px; text-transform:uppercase; padding:8px 6px; border-bottom:1px solid #1e293b; }}
    td {{ padding:10px 6px; border-bottom:1px solid #1e293b; vertical-align:top; }}
    .nct-link {{ color:#38bdf8; font-weight:700; text-decoration:none; }}
    .trial-title {{ color:#e2e8f0; font-weight:500; }}
    .sub {{ color:#64748b; font-size:11px; margin-top:2px; }}
    .status-pill {{ background:#1e293b; color:#cbd5e1; padding:3px 7px; border-radius:4px; font-size:11px; }}
    .readout {{ color:#fbbf24; font-weight:700; white-space:nowrap; }}
    .num {{ text-align:right; color:#cbd5e1; }}
    .card-footer {{ margin-top:12px; padding-top:12px; display:flex; justify-content:space-between; align-items:center; font-size:12px; color:#64748b; flex-wrap:wrap; gap:8px; }}
    .cta-btn {{ background:#2563eb; color:#fff; padding:6px 12px; border-radius:6px; text-decoration:none; font-weight:600; font-size:12px; }}
    .cta-btn.secondary {{ background:#334155; margin-right:6px; }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div>
        <h1>🧬 BioCatalyst Diff & Liquidity Radar</h1>
        <div class="subtitle">Automated ClinicalTrials.gov Protocol Diffs vs. SEC EDGAR XBRL Cash Runway • Updated: {updated_str}</div>
      </div>
      <input type="text" id="search" class="search-box" placeholder="Filter by $TICKER or company..." onkeyup="filterCards()">
    </header>

    <div class="section-title">🔥 Featured High-Conviction Catalysts</div>
    <div id="spotlight-container">{spotlight_html}</div>

    <div class="section-title">📋 Master Biotech Watchlist ({len(companies)} Tracked Tickers)</div>
    <div id="cards-container">{cards_html}</div>
  </div>
  <script>
    function filterCards() {{
      const q = document.getElementById('search').value.toLowerCase();
      document.querySelectorAll('.card').forEach(c => {{
        c.style.display = c.getAttribute('data-ticker').includes(q) ? 'block' : 'none';
      }});
    }}
  </script>
</body>
</html>"""


def main():
    old_state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            old_state = json.load(f)

    new_state = {}
    companies = []

    for ticker, meta in BIOTECH_WATCHLIST.items():
        print(f"Scanning ${ticker}...")
        fin = get_sec_financials(meta["cik"])
        time.sleep(0.2)
        trials = get_active_trials(meta["sponsor"])

        new_state[ticker] = {"cash": fin, "trials": trials}
        companies.append({
            "ticker": ticker,
            "sponsor": meta["sponsor"],
            "cash_usd": fin["total_liquidity_usd"],
            "cash_as_of": fin["as_of"],
            "trials": trials
        })

    with open(STATE_FILE, "w") as f:
        json.dump(new_state, f, indent=2)

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html_out = build_html(companies, now_str)
    with open(HTML_OUTPUT, "w", encoding="utf-8") as f:
        f.write(html_out)
    print("Successfully generated upgraded index.html with featured spotlight and expanded watchlist.")


if __name__ == "__main__":
    main()
