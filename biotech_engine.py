import json
import os
import time
import requests
from datetime import datetime, timezone

# SEC requires a descriptive User-Agent header
SEC_HEADERS = {
    "User-Agent": "BioCatalystRadar contact@biocatalystradar.com",
    "Accept-Encoding": "gzip, deflate"
}

# Starter Watchlist of high-volatility clinical biotechs
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
    "SUMO": {"cik": "0001645469", "sponsor": "Summit Therapeutics"}
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
        "filter.overallStatus": "RECRUITING,ACTIVE_NOT_RECRUITING,NOT_YET_RECRUITING,SUSPENDED,TERMINATED",
        "pageSize": 35
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
                "last_updated": stat.get("lastUpdatePostDateStruct", {}).get("date", "N/A"),
                "enrollment": des.get("enrollmentInfo", {}).get("count", 0),
                "endpoint": endpoint
            }
    except Exception as e:
        print(f"Trials error for {sponsor}: {e}")
    return trials


def diff_trials(ticker: str, old_trials: dict, new_trials: dict) -> list:
    diffs = []
    if not old_trials:
        return diffs  # Skip noisy diffs on very first baseline run
    for nct_id, curr in new_trials.items():
        if nct_id not in old_trials:
            diffs.append({
                "ticker": ticker,
                "nct_id": nct_id,
                "type": "NEW TRIAL",
                "detail": f"Added {curr['phase']} study ({curr['status']}) — Target Readout: {curr['completion']}"
            })
            continue
        prev = old_trials[nct_id]
        for field, label in [("status", "Status"), ("completion", "Primary Completion"), ("enrollment", "Enrollment")]:
            if prev.get(field) != curr.get(field):
                diffs.append({
                    "ticker": ticker,
                    "nct_id": nct_id,
                    "type": f"{label.upper()} SHIFT",
                    "detail": f"{label} changed from '{prev.get(field)}' to '{curr.get(field)}' ({curr['title'][:60]}...)"
                })
    return diffs


def build_html(companies: list, diffs: list, updated_str: str) -> str:
    diff_html = ""
    if diffs:
        items = "".join([
            f"<div class='diff-item'><span class='badge-alert'>${d['ticker']} • {d['type']}</span> "
            f"<a href='https://clinicaltrials.gov/study/{d['nct_id']}' target='_blank'>{d['nct_id']}</a>: {d['detail']}</div>"
            for d in diffs
        ])
        diff_html = f"<div class='diff-box'><h3>🚨 Recent Protocol & Timeline Shifts Detected</h3>{items}</div>"
    else:
        diff_html = (
            "<div class='diff-box'><h3>✅ 24-Hour Protocol Diff Scanner Active</h3>"
            "<p style='margin:0;color:#94a3b8;font-size:13px;'>No silent ClinicalTrials.gov protocol amendments or readout delays detected across tracked tickers in the last 24h cycle.</p></div>"
        )

    cards = ""
    for c in companies:
        liq = f"${c['cash_usd']/1e6:,.1f}M" if c["cash_usd"] else "N/A"
        sorted_trials = sorted(c["trials"].values(), key=lambda x: x["completion"])
        rows = ""
        for t in sorted_trials[:6]:
            rows += f"""
            <tr>
              <td><a href="https://clinicaltrials.gov/study/{t['nct_id']}" target="_blank" class="nct-link">{t['nct_id']}</a><div class="sub">{t['phase']}</div></td>
              <td><div class="trial-title">{t['title']}</div><div class="sub">Endpoint: {t['endpoint'][:70]}...</div></td>
              <td><span class="status-pill">{t['status']}</span></td>
              <td class="readout">{t['completion']}</td>
              <td class="num">{t['enrollment']:,}</td>
            </tr>"""

        cards += f"""
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
            <span>📡 Auto-monitored nightly via SEC EDGAR XBRL & ClinicalTrials.gov v2</span>
            <a href="https://www.tradingview.com/symbols/{c['ticker']}/" target="_blank" class="cta-btn">Chart & SEC Deep Dive (${c['ticker']}) →</a>
          </div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>BioCatalyst Diff & Liquidity Radar | Automated FDA Trial & SEC Cash Tracker</title>
  <style>
    body {{ background:#020617; color:#f8fafc; font-family:system-ui,-apple-system,sans-serif; margin:0; padding:20px; }}
    .container {{ max-width:960px; margin:0 auto; }}
    header {{ border-bottom:1px solid #1e293b; padding-bottom:18px; margin-bottom:20px; display:flex; justify-content:space-between; align-items:flex-end; flex-wrap:wrap; gap:12px; }}
    h1 {{ margin:0; font-size:26px; color:#f8fafc; }}
    .subtitle {{ color:#94a3b8; font-size:14px; margin-top:4px; }}
    .search-box {{ background:#0f172a; border:1px solid #334155; color:#fff; padding:10px 14px; border-radius:8px; width:260px; font-size:14px; }}
    .banner-nl {{ background:linear-gradient(90deg,#1e3a8a,#0f172a); border:1px solid #3b82f6; border-radius:12px; padding:16px 20px; margin-bottom:20px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px; }}
    .diff-box {{ background:#0f172a; border:1px solid #334155; border-left:4px solid #38bdf8; border-radius:10px; padding:14px 18px; margin-bottom:22px; }}
    .diff-box h3 {{ margin:0 0 8px 0; font-size:15px; }}
    .diff-item {{ font-size:13px; padding:6px 0; border-bottom:1px solid #1e293b; }}
    .badge-alert {{ background:#7f1d1d; color:#fca5a5; padding:2px 8px; border-radius:4px; font-weight:700; font-size:11px; margin-right:6px; }}
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
    .cta-btn {{ background:#2563eb; color:#fff; padding:6px 12px; border-radius:6px; text-decoration:none; font-weight:600; }}
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
    <div class="banner-nl">
      <div>
        <strong style="font-size:15px;">📬 Free Sunday Biotech Protocol Diff Dispatch</strong>
        <div style="font-size:13px;color:#cbd5e1;">Get alerted when small/mid-cap biotechs quietly delay primary readout dates or cut enrollment before an 8-K drops.</div>
      </div>
      <a href="#newsletter-signup" class="cta-btn" style="background:#10b981;">Subscribe Free →</a>
    </div>
    {diff_html}
    <div id="cards-container">{cards}</div>
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
    all_diffs = []
    companies = []

    for ticker, meta in BIOTECH_WATCHLIST.items():
        print(f"Scanning ${ticker}...")
        fin = get_sec_financials(meta["cik"])
        time.sleep(0.2)
        trials = get_active_trials(meta["sponsor"])

        prev_trials = old_state.get(ticker, {}).get("trials", {})
        all_diffs.extend(diff_trials(ticker, prev_trials, trials))

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
    html_out = build_html(companies, all_diffs, now_str)
    with open(HTML_OUTPUT, "w", encoding="utf-8") as f:
        f.write(html_out)
    print("Successfully generated index.html and updated state.")


if __name__ == "__main__":
    main()
