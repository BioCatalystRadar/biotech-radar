"""Server-side static HTML + RSS rendering; user-supplied API strings are escaped."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import format_datetime
from html import escape
from pathlib import Path
import os
import re
from urllib.parse import quote, urlsplit
from xml.sax.saxutils import escape as xml_escape

ACTIVE = {"RECRUITING", "ACTIVE_NOT_RECRUITING", "NOT_YET_RECRUITING"}


def h(value) -> str:
    return escape("—" if value is None or value == "" else str(value), quote=True)


def usd(value):
    if value is None:
        return "Not available"
    if abs(value) >= 1_000_000_000:
        return f"${value / 1e9:,.2f}B"
    return f"${value / 1e6:,.1f}M"


def trial_sort(t):
    date = t.get("completion") or "9999"
    return (date, t.get("nct_id", ""))


def count_active(c):
    return sum(t.get("status") in ACTIVE for t in c.get("clinical", {}).get("trials", {}).values())


def render_trial(t):
    nct = t["nct_id"]
    if not (len(nct) == 11 and nct[:3] == "NCT" and nct[3:].isdigit()):
        return ""
    url = f"https://clinicaltrials.gov/study/{nct}"
    endp = t.get("endpoint") or "Not reported"
    if len(endp) > 190:
        endp = endp[:187] + "…"
    date = t.get("completion") or "Not reported"
    enrollment = t.get("enrollment")
    n = f"{enrollment:,}" if isinstance(enrollment, int) else "—"
    role = "Collaborator" if t.get("relationship") == "Collaborator" else "Lead sponsor"
    date_type = (t.get("completion_type") or "unspecified").lower().replace("_", " ")
    n_type = (t.get("enrollment_type") or "unspecified").lower().replace("_", " ")
    return f'''<tr>
      <td><a href="{url}" target="_blank" rel="noopener">{h(nct)}</a><span class="cell-note">{h(t.get('phase'))} · {h(role)}</span></td>
      <td class="study-title">{h(t.get('title'))}<span class="cell-note">Primary outcome: {h(endp)}</span><span class="cell-note">Registry posted update: {h(t.get('registry_last_update') or 'Not available')}</span></td>
      <td><span class="mini-pill">{h(t.get('status', '').replace('_', ' ').title())}</span></td>
      <td class="date-cell">{h(date)}<span class="cell-note">{h(date_type)}</span></td>
      <td class="numeric">{n}<span class="cell-note">{h(n_type)}</span></td>
    </tr>'''


def render_company(c):
    ticker = c["ticker"]
    trials = sorted((t for t in c.get("clinical", {}).get("trials", {}).values()
                     if t.get("status") in ACTIVE), key=trial_sort)
    financial = c.get("financial", {})
    errors = c.get("source_errors", {})
    cash = usd(financial.get("value_usd"))
    as_of = financial.get("as_of") or "not reported"
    kind = financial.get("kind") or "No XBRL value"
    cash_badge = "warn" if financial.get("status") != "reported" or errors.get("sec") else ""
    if errors.get("sec"):
        cash_note = "SEC refresh failed; last successful observation may be stale"
    elif financial.get("status") == "stale":
        cash_note = "Underlying SEC reporting period is more than 180 days old"
    else:
        cash_note = "Financial reporting period, not a live balance"
    filing_url = financial.get("filing_url")
    filing_link = (f'<a href="{h(filing_url)}" target="_blank" rel="noopener">SEC filing ↗</a>'
                   if filing_url and filing_url.startswith("https://www.sec.gov/Archives/") else "")
    error_alert = ""
    if errors.get("clinical"):
        error_alert += '<p class="source-warning">ClinicalTrials.gov refresh failed; previously saved studies shown.</p>'
    if errors.get("sec"):
        error_alert += '<p class="source-warning">SEC data unavailable or not refreshed. Missing numbers are not zero.</p>'
    rows = "".join(render_trial(t) for t in trials[:6])
    remainder = "".join(render_trial(t) for t in trials[6:])
    if not rows:
        rows = '<tr><td colspan="5" class="empty">No matching active Phase 1–3 interventional studies found in this sponsor-matched snapshot. This does not establish an empty development pipeline.</td></tr>'
    extra = f'''<details class="more-studies"><summary>Show remaining {len(trials)-6} active studies</summary>
      <div class="table-scroll"><table><thead><tr><th>Trial</th><th>Study / outcome</th><th>Status</th><th>Est. completion</th><th>Enrollment</th></tr></thead><tbody>{remainder}</tbody></table></div></details>''' if remainder else ""
    search = h((ticker + " " + c.get("name", "")).lower())
    spotlight = " · Watchlist spotlight" if c.get("spotlight") else ""
    return f'''<article class="company-card" id="company-{h(ticker.lower())}" data-search="{search}">
      <div class="card-top">
        <div><h3><span class="ticker">{h(ticker)}</span> <span class="company-name">{h(c.get('name'))}</span></h3>
          <p class="muted">{len(trials)} active registry matches{h(spotlight)}</p></div>
        <div class="cash-box {cash_badge}"><div class="cash-value">{h(cash)}</div>
          <div class="cash-descriptor">{h(kind)} · as of {h(as_of)}</div>
          <div class="cash-note">{h(cash_note)}</div></div>
      </div>
      {error_alert}
      <div class="table-scroll"><table><thead><tr><th>Trial</th><th>Study / primary outcome</th><th>Status</th><th>Est. primary completion</th><th>Enrollment</th></tr></thead>
      <tbody>{rows}</tbody></table></div>{extra}
      <div class="card-foot"><span>Clinical updated: {h(c.get('clinical', {}).get('last_success_at') or 'never')} UTC</span>
      <div><a href="company/{h(ticker.lower())}.html">Company intelligence &amp; history ↗</a>
      <a href="https://clinicaltrials.gov/search?term={quote(c.get('name', '').split(' (')[0])}" target="_blank" rel="noopener">Registry search ↗</a>
      {filing_link}<a href="https://www.tradingview.com/symbols/NASDAQ-{h(ticker)}/" target="_blank" rel="noopener">Chart ↗</a></div></div>
    </article>'''


def render_event(e):
    url = e.get("source_url") or ""
    link = (f'<a href="{h(url)}" target="_blank" rel="noopener">Original source ↗</a>'
            if url.startswith(("https://clinicaltrials.gov/study/", "https://www.sec.gov/Archives/")) else "")
    old = str(e.get("before")) if e.get("before") is not None else "Not previously observed"
    new = str(e.get("after")) if e.get("after") is not None else "Not reported"
    return f'''<article class="event" id="event-{h(e.get('id', ''))}" data-search="{h((e.get('ticker','')+' '+(e.get('nct_id') or '')+' '+e.get('field','')).lower())}">
      <div class="event-meta"><strong>${h(e.get('ticker'))}</strong><span class="priority {h(e.get('importance'))}">{h(e.get('importance', 'review')).upper()}</span>
        <time>{h(e.get('detected_at','').replace('T',' ').replace('Z',' UTC'))}</time></div>
      <h3>{h(e.get('field'))}{' · '+h(e.get('nct_id')) if e.get('nct_id') else ''}</h3>
      <div class="delta"><div><span class="delta-label">Before</span><span>{h(old)}</span></div><div><span class="delta-label">After</span><span>{h(new)}</span></div></div>
      <div class="event-link">{link} <a class="history-link" href="company/{h(e.get('ticker','').lower())}.html#event-{h(e.get('id', ''))}">Company history ↗</a></div>
    </article>'''


STYLE = r'''
:root{color-scheme:dark;--bg:#070c18;--panel:#101a2b;--line:#263349;--text:#e9f0fb;--sub:#95a8c0;--blue:#57c8f7;--green:#72e4b4;--amber:#f4c46b}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif;color:var(--text);background:radial-gradient(ellipse 85% 42% at 55% -10%,#153554 0%,var(--bg) 78%);margin:0;line-height:1.5}
a{color:#7bd4fc;text-decoration:none}a:hover{text-decoration:underline}.shell{max-width:1250px;margin:auto;padding:0 22px 80px}
nav{padding:20px 0;display:flex;align-items:center;justify-content:space-between;gap:20px;border-bottom:1px solid var(--line)}.brand{font-size:18px;letter-spacing:-.5px;font-weight:850;color:white}.brand-mark{color:var(--blue);font-size:25px;margin-right:7px}.nav-links{display:flex;gap:18px;font-size:13px}
.hero{padding:48px 0 29px}.eyebrow{font-size:12px;letter-spacing:2px;font-weight:800;color:var(--green);text-transform:uppercase}.hero h1{font-size:clamp(34px,5vw,62px);line-height:1.05;letter-spacing:-2px;max-width:900px;margin:14px 0}.hero h1 em{font-style:normal;color:var(--blue)}.hero p{color:var(--sub);max-width:780px;font-size:16px;margin:10px 0 0}.timestamp{font-size:12px;color:var(--sub);margin:17px 0}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:20px 0 33px}.stat{background:linear-gradient(150deg,#142036,#0d1626);border:1px solid var(--line);border-radius:14px;padding:17px}.stat b{font-size:29px;font-variant-numeric:tabular-nums;display:block;line-height:1.25}.stat span{color:var(--sub);font-size:12px}.stat .green{color:var(--green)}
.heading-row{display:flex;justify-content:space-between;align-items:end;gap:12px;margin:32px 0 13px}.heading-row h2{font-size:21px;letter-spacing:-.4px;margin:0}.heading-row p{color:var(--sub);font-size:12px;margin:5px 0 0}
.search{background:#101c30;border:1px solid #34445c;color:white;border-radius:10px;padding:11px 14px;width:min(350px,100%);font:inherit;font-size:14px;outline:none}.search:focus{border-color:var(--blue)}.spotlights{display:flex;gap:9px;flex-wrap:wrap;margin:12px 0 24px}.spotlights a{border:1px solid #2a4b69;border-radius:40px;padding:7px 15px;background:#12253b;color:#b9e9fb;font-size:12px;font-weight:750}
.feed{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.event{background:#0e192b;border:1px solid var(--line);padding:17px;border-radius:12px;min-width:0}.event-meta{display:flex;align-items:center;gap:10px;color:var(--sub);font-size:11px}.event-meta strong{color:var(--blue);font-size:14px}.event-meta time{margin-left:auto;text-align:right}.priority{font-size:9px;letter-spacing:1px;font-weight:900;border-radius:20px;padding:3px 8px;background:#38465a;color:#c8d2e0}.priority.high{background:#5b3825;color:#ffd39a}.priority.medium{background:#214a43;color:#8af6c7}.event h3{font-size:15px;margin:11px 0 12px}.delta{display:grid;grid-template-columns:1fr 1fr;gap:9px}.delta>div{background:#091321;border:1px solid #1e3047;border-radius:8px;padding:9px;font-size:12px;min-width:0;overflow-wrap:anywhere}.delta-label{display:block;text-transform:uppercase;letter-spacing:1px;font-size:9px;color:var(--sub);font-weight:800;margin-bottom:5px}.event-link{margin-top:10px;font-size:12px}.no-events{border:1px dashed #38506c;padding:23px;color:var(--sub);border-radius:12px;grid-column:1/-1}
.company-card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px;margin-bottom:17px;overflow:hidden}.card-top{display:flex;align-items:start;justify-content:space-between;gap:17px;flex-wrap:wrap;margin-bottom:12px}.card-top h3{margin:0;font-size:17px}.ticker{color:var(--blue);font-size:24px;letter-spacing:-.6px;margin-right:9px}.company-name{color:#c2d0e1;font-weight:500;font-size:15px}.muted{color:var(--sub);font-size:12px;margin:3px 0}.cash-box{background:#11332f;border:1px solid #28584f;border-radius:10px;padding:10px 13px;text-align:right;max-width:340px}.cash-box.warn{background:#2f2a1d;border-color:#635333}.cash-value{font-weight:850;font-size:20px;color:var(--green)}.cash-box.warn .cash-value{color:var(--amber)}.cash-descriptor{font-size:11px;color:#cbdad7}.cash-note{font-size:10px;color:#adc1c3;margin-top:4px}
.table-scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;min-width:740px;font-size:12px}thead{background:#121f33}th{padding:10px 11px;text-transform:uppercase;letter-spacing:.7px;font-size:10px;color:#94a9c4;text-align:left}td{padding:12px 11px;border-bottom:1px solid #223047;vertical-align:top}td a{font-weight:750}.study-title{max-width:400px;color:#e8f0fc}.cell-note{display:block;color:#8c9eb9;margin-top:5px;font-size:10px;font-weight:400}.mini-pill{background:#213449;border:1px solid #30465d;border-radius:4px;font-size:10px;padding:4px 6px;white-space:nowrap}.numeric{text-align:right}.date-cell{white-space:nowrap;color:var(--amber)}.empty{text-align:center;color:var(--sub);padding:20px}.more-studies{margin:12px 0}.more-studies summary{cursor:pointer;color:var(--blue);font-size:12px}.card-foot{padding-top:13px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:10px;color:var(--sub);font-size:11px}.card-foot>div{display:flex;flex-wrap:wrap;gap:15px}.source-warning{color:#ffd08a;font-size:12px;margin:5px 0}
.explainer{margin-top:35px;border-top:1px solid var(--line);padding-top:25px;display:grid;grid-template-columns:1fr 1fr;gap:27px;color:var(--sub);font-size:13px}.explainer h2{font-size:16px;color:var(--text)}footer{border-top:1px solid var(--line);margin-top:35px;padding:20px 0;color:var(--sub);font-size:11px}.hide{display:none!important}.history-link{margin-left:14px}.breadcrumbs{margin-top:22px;font-size:13px;color:var(--sub)}.company-hero{padding:26px 0 14px}.company-hero h1{margin:7px 0;font-size:clamp(30px,4vw,44px);letter-spacing:-1.2px}.company-hero p{color:var(--sub);max-width:860px}.detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:17px 0 23px}.detail-panel{border:1px solid var(--line);border-radius:14px;background:var(--panel);padding:18px}.detail-panel h2{font-size:18px;margin:0 0 9px}.detail-panel p{margin:9px 0;color:var(--sub);font-size:13px}.detail-panel b{color:var(--text)}.detail-panel .metric{font-size:26px;color:var(--green);font-weight:850}.detail-panel a{overflow-wrap:anywhere}.history-feed{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.company-section{margin:26px 0}.company-section h2{font-size:21px}.source-disclosure{color:var(--sub);font-size:12px;border-left:3px solid #547389;padding-left:12px;margin-top:17px}.detail-table{padding:0;overflow:hidden}.detail-table .table-scroll{padding:0}.detail-table table{min-width:700px}.detail-table th,.detail-table td{padding:12px}.back-link{font-weight:700}
@media(max-width:750px){.detail-grid,.history-feed{grid-template-columns:1fr}.stats{grid-template-columns:repeat(2,1fr)}.feed{grid-template-columns:1fr}.heading-row{align-items:start;flex-direction:column}.search{width:100%}.hero{padding-top:34px}.explainer{grid-template-columns:1fr}.cash-box{text-align:left;width:100%;max-width:none}.nav-links{gap:9px;font-size:11px}.shell{padding:0 14px 50px}.company-card{padding:14px}.hero h1{letter-spacing:-1px}}
'''


def page(state, events):
    companies = list(state["companies"].values())
    total_active = sum(count_active(c) for c in companies)
    unverified = sum(bool(c.get("source_errors")) for c in companies)
    recent_events = events[:24]
    last_scan = state.get("last_scan_at", "Unavailable")
    spotlight = "".join(f'<a href="#company-{h(c["ticker"].lower())}">${h(c["ticker"])}</a>'
                        for c in companies if c.get("spotlight"))
    cards = "\n".join(render_company(c) for c in companies)
    event_html = "\n".join(render_event(e) for e in recent_events) if recent_events else (
        '<div class="no-events"><strong>No changes detected yet.</strong> The first successful observation establishes a baseline. Later scans will compare against it; this does not mean that no changes have ever occurred.</div>')
    canonical = canonical_tag(site_base_url())
    return f'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="description" content="Publicly sourced clinical trial registry change tracking and SEC-reported biotech cash and short-term investments. Source-linked and time-stamped."/>
{canonical}
<link rel="alternate" type="application/rss+xml" title="BioCatalyst Radar change feed" href="feed.xml"/>
<title>BioCatalyst Radar | Clinical Trial Changes & SEC Financial Snapshots</title><style>{STYLE}</style></head>
<body><div class="shell"><nav><a class="brand" href="#top"><span class="brand-mark">◈</span>BIOCATALYST RADAR</a>
<div class="nav-links"><a href="#changes">Change feed</a><a href="#watchlist">Watchlist</a><a href="feed.xml">RSS ↗</a></div></nav>
<main id="top"><section class="hero"><div class="eyebrow">Public-data intelligence · Clinical-stage biotech</div>
<h1>Track the changes.<br/><em>Not just the headlines.</em></h1>
<p>Follow changes in registered trial endpoints, estimated primary completion dates, enrollment, and phase—alongside reported SEC cash figures. Every alert links to its underlying source.</p>
<div class="timestamp">Last pipeline scan: {h(last_scan.replace('T', ' ').replace('Z', ' UTC'))} · Scheduled updates, not a live price feed</div></section>
<section class="stats" aria-label="Tracker status"><div class="stat"><b>{len(companies)}</b><span>Biotech companies tracked</span></div>
<div class="stat"><b>{total_active}</b><span>Active matching clinical studies</span></div>
<div class="stat"><b class="green">{len(events)}</b><span>Recorded changes (retained history)</span></div>
<div class="stat"><b>{unverified}</b><span>Companies with source warnings</span></div></section>
<section id="changes"><div class="heading-row"><div><h2>↗ Latest detected changes</h2><p>Observed differences from prior successful snapshots. These are not necessarily newly announced events.</p></div>
<a href="feed.xml">Subscribe via RSS ↗</a></div><div class="feed" id="event-feed">{event_html}</div></section>
<section id="watchlist"><div class="heading-row"><div><h2>Company intelligence</h2><p>Only active Phase 1–3 interventional studies matched to specified sponsors/collaborators.</p></div>
<input class="search" id="search" type="search" aria-label="Search watchlist" placeholder="Search by ticker or company..." autocomplete="off"/></div>
<div class="spotlights" aria-label="Selected spotlights">{spotlight}</div><div id="company-list">{cards}</div>
<p class="muted hide" id="no-results">No matching companies. Try a ticker symbol.</p></section>
<section class="explainer"><div><h2>What the numbers actually mean</h2><p>The primary completion date is an estimated or actual registry milestone—not a guaranteed efficacy readout or press-release date. Enrollment may be estimated or actual. SEC figures are reported as of the displayed financial period; they are not continuously updated cash balances.</p></div>
<div><h2>Data-quality policy</h2><p>Missing financial data is never treated as $0. Failed retrievals preserve prior snapshots and display warnings. Sponsor/collaborator matching is deliberately conservative, so studies run under unconfigured subsidiaries or partners may be absent. The feed highlights changes, not their investment significance.</p></div></section></main>
<footer>BioCatalyst Radar · Independent research utility · Sources: ClinicalTrials.gov and SEC EDGAR · Not affiliated with either agency · Informational only; not investment advice. No affiliate links or advertisements are currently active. <a href="feed.xml">RSS</a></footer></div>
<script>(function(){{const search=document.getElementById('search');const cards=[...document.querySelectorAll('.company-card')];search.addEventListener('input',function(){{const q=this.value.trim().toLowerCase();let visible=0;cards.forEach(el=>{{const show=el.dataset.search.includes(q);el.classList.toggle('hide',!show);if(show)visible++;}});document.getElementById('no-results').classList.toggle('hide',visible>0);}});}})();</script>
</body></html>'''


def safe_ticker(ticker: str) -> bool:
    'Only derive generated filenames from safe, unambiguous ticker symbols.'
    return isinstance(ticker, str) and bool(re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,11}", ticker))


def site_base_url() -> str | None:
    'Canonical deployment origin/prefix, including the GitHub Pages repo segment.'
    configured = os.environ.get("SITE_URL", "").strip()
    if configured:
        parts = urlsplit(configured)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("SITE_URL must be an absolute HTTPS site URL, without query or fragment")
        return configured.rstrip("/") + "/"
    if os.environ.get("DEPLOY_TARGET") == "cloudflare" and os.environ.get("CLOUDFLARE_PROJECT_NAME"):
        project = os.environ["CLOUDFLARE_PROJECT_NAME"]
        if not re.fullmatch(r"[a-z0-9-]+", project):
            raise ValueError("Invalid CLOUDFLARE_PROJECT_NAME")
        return f"https://{project}.pages.dev/"
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{quote(name)}/"
    return None


def canonical_tag(base: str | None, suffix: str = "") -> str:
    if not base:
        return ""
    return f'<link rel="canonical" href="{h(base + suffix)}"/>'


def describe_financial_components(fin: dict) -> str:
    'Never infer a cash/investment breakdown from an opaque aggregate.'
    components = fin.get("components") or {}
    cash = components.get("cash_equivalents_usd")
    invest = components.get("short_term_investments_usd")
    if (isinstance(cash, (int, float)) and isinstance(invest, (int, float))
            and components.get("verified_same_filing")):
        return (f'<p><b>Cash and equivalents:</b> {h(usd(cash))}</p>'
                f'<p><b>Short-term investments:</b> {h(usd(invest))}</p>'
                '<p>Components reported for the same period and filing accession.</p>')
    return '<p>Separate cash and investment components are not verified in this snapshot; no estimated breakdown is displayed.</p>'


def company_page(c: dict, company_events: list[dict], state: dict, base: str | None) -> str:
    ticker = c["ticker"]
    name = c.get("name") or ticker
    fin = c.get("financial") or {}
    clin = c.get("clinical") or {}
    errors = c.get("source_errors") or {}
    trials = sorted((t for t in (clin.get("trials") or {}).values()
                     if t.get("status") in ACTIVE), key=trial_sort)
    as_of = fin.get("as_of") or "Not available"
    filed = fin.get("filed") or "Not available"
    value = usd(fin.get("value_usd"))
    kind = fin.get("kind") or "Unavailable"
    status = fin.get("status") or "Unknown"
    scan = state.get("last_scan_at") or "Not available"
    canonical = canonical_tag(base, f"company/{ticker.lower()}.html")
    description = f"{ticker} ({name}) active clinical studies, SEC-reported liquidity context and a timestamped registry change history from BioCatalyst Radar."
    trial_rows = "".join(render_trial(t) for t in trials) or '<tr><td colspan="5" class="empty">No matching active clinical studies were observed under configured sponsor identities. This does not establish an empty development pipeline.</td></tr>'
    history = "".join(render_event(e).replace(
        f'href="company/{ticker.lower()}.html#event-{h(e.get("id", ""))}"',
        f'href="#event-{h(e.get("id", ""))}"')
        for e in company_events[:150]) or '<div class="no-events">No changes recorded for this company since monitoring began. This does not establish that its registry records have never changed.</div>'
    financial_url = fin.get("filing_url") or ""
    filing_link = (f'<a href="{h(financial_url)}" target="_blank" rel="noopener noreferrer">Open original SEC filing ↗</a>'
                   if financial_url.startswith("https://www.sec.gov/Archives/") else
                   '<span class="muted">Filing URL not verified in this snapshot</span>')
    warning = ""
    if errors.get("clinical"):
        warning += '<p class="source-warning">Clinical registry refresh failed; showing last successful snapshot.</p>'
    if errors.get("sec"):
        warning += '<p class="source-warning">SEC refresh failed or issuer could not be verified; last observation may be stale.</p>'
    # Do not index pages that provide no source-verified or historical intelligence.
    has_content = bool(trials or company_events or fin.get("value_usd") is not None)
    robots = '' if has_content else '<meta name="robots" content="noindex,follow"/>'
    return f'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{h(ticker)} Clinical Trial Changes &amp; SEC Financials | BioCatalyst Radar</title>
<meta name="description" content="{h(description)}"/>{canonical}{robots}
<link rel="alternate" type="application/rss+xml" href="../feed.xml" title="BioCatalyst Radar alerts"/>
<style>{STYLE}</style></head><body><div class="shell">
<nav><a class="brand" href="../index.html"><span class="brand-mark">◈</span>BIOCATALYST RADAR</a>
<div class="nav-links"><a href="../index.html#changes">Latest changes</a><a href="../index.html#watchlist">Watchlist</a><a href="../feed.xml">RSS ↗</a></div></nav>
<main><div class="breadcrumbs"><a href="../index.html">Home</a> / Companies / {h(ticker)}</div>
<section class="company-hero"><div class="eyebrow">Company intelligence · Source-linked</div>
<h1><span class="ticker">${h(ticker)}</span> {h(name)}</h1>
<p>Monitored clinical registry milestones, verified SEC-reported cash context and historical changes. Clinical associations include named sponsors and collaborators; they do not establish asset ownership.</p>
<p class="timestamp">Last pipeline scan: {h(scan)} · Observations are periodic, not real-time · {len(company_events)} retained company changes</p></section>
{warning}
<section class="detail-grid" aria-label="Company snapshot">
<div class="detail-panel"><h2>Clinical trial coverage</h2><div class="metric">{len(trials)}</div><p>Active Phase 1–3 interventional studies matched to configured names.</p>
<p><b>Last successful registry refresh:</b> {h(clin.get('last_success_at') or 'Not available')}</p>
<p><a href="https://clinicaltrials.gov/search?term={quote(name.split(' (')[0])}" target="_blank" rel="noopener noreferrer">Search ClinicalTrials.gov ↗</a></p></div>
<div class="detail-panel"><h2>SEC financial snapshot</h2><div class="metric">{h(value)}</div>
<p><b>Reported measure:</b> {h(kind)}</p><p><b>Financial period end:</b> {h(as_of)}</p>
<p><b>Filing date:</b> {h(filed)} · <b>Data status:</b> {h(status)}</p>
<p><b>Last successful SEC retrieval:</b> {h(fin.get('last_success_at') or 'Not available')}</p>
{describe_financial_components(fin)}<p>{filing_link}</p>
<p class="source-disclosure">An SEC-reported stock of cash/investments is not cash runway. Operating cash burn, financing changes and restricted balances are not inferred here.</p></div></section>
<section class="company-section"><h2>Active registry-matched trials</h2>
<p class="muted">Primary completion is a registry milestone, not a guaranteed clinical data readout. Estimated and actual classifications are shown separately.</p>
<div class="detail-panel detail-table"><div class="table-scroll"><table><thead><tr><th>NCT / relationship</th><th>Study / primary outcome</th><th>Status</th><th>Primary completion</th><th>Enrollment</th></tr></thead><tbody>{trial_rows}</tbody></table></div></div></section>
<section class="company-section" id="history"><h2>Recorded change history ({len(company_events)})</h2>
<p class="muted">Before/after comparisons from successful scans, latest first. Detection time is not necessarily the date the study changed or the market learned about it.</p>
<div class="history-feed">{history}</div></section>
<p class="source-disclosure">Methodology: data from ClinicalTrials.gov v2 and SEC EDGAR companyfacts. Named collaborators do not imply trial sponsorship, drug ownership or royalty exposure. No automated investment recommendation is made.</p>
<p><a class="back-link" href="../index.html#company-{h(ticker.lower())}">← Return to company watchlist</a></p>
</main><footer>BioCatalyst Radar · Independent public-data research utility · Not investment advice · Data may be delayed or incomplete.</footer></div></body></html>'''


def feed(events: list[dict]):
    base = site_base_url()
    items = []
    for e in events[:150]:
        when = e.get("detected_at") or ""
        try:
            pub = format_datetime(datetime.fromisoformat(when.replace("Z", "+00:00")))
        except ValueError:
            pub = format_datetime(datetime.now(timezone.utc))
        source = e.get("source_url") or ""
        ticker = e.get("ticker", "")
        if base and safe_ticker(ticker):
            destination = f"{base}company/{ticker.lower()}.html#event-{e.get('id', '')}"
        else:
            destination = source or base or "https://example.invalid/"
        description = (f"Before: {e.get('before')}; After: {e.get('after')}. "
                       f"Observed difference; verify primary source: {source}")
        title = f"${ticker} {e.get('field')} {e.get('nct_id') or ''}".strip()
        items.append(f'''<item><title>{xml_escape(title)}</title>
<guid isPermaLink="false">{xml_escape(str(e['id']))}</guid>
<link>{xml_escape(destination)}</link><description>{xml_escape(description)}</description>
<pubDate>{pub}</pubDate></item>''')
    return '''<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>BioCatalyst Radar Changes</title>
<description>Public registry and SEC XBRL snapshot differences; check primary sources.</description>
<link>''' + xml_escape(base or "https://example.invalid/") + '''</link>''' + "\n".join(items) + "</channel></rss>\n"


def sitemap(companies: list[dict], base: str) -> str:
    entries = [base]
    for c in companies:
        ticker = c.get("ticker", "")
        if not safe_ticker(ticker):
            continue
        has_content = (count_active(c) > 0 or c.get("financial", {}).get("value_usd") is not None
                       or c.get("_has_events", False))
        if has_content:
            entries.append(f"{base}company/{ticker.lower()}.html")
    locations = "".join(f"<url><loc>{xml_escape(url)}</loc></url>" for url in entries)
    return '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + locations + '</urlset>\n'


def build_site(output: Path, state: dict, events: list[dict]):
    output.mkdir(parents=True, exist_ok=True)
    base = site_base_url()
    (output / "index.html").write_text(page(state, events), encoding="utf-8")
    (output / "feed.xml").write_text(feed(events), encoding="utf-8")
    folder = output / "company"
    folder.mkdir(exist_ok=True)
    by_company = {}
    for e in events:
        by_company.setdefault(e.get("ticker", ""), []).append(e)
    companies = []
    expected = set()
    for c in state.get("companies", {}).values():
        ticker = c.get("ticker", "")
        if not safe_ticker(ticker):
            raise ValueError(f"Unsafe ticker for generated page: {ticker!r}")
        filename = ticker.lower() + ".html"
        if filename in expected:
            raise ValueError(f"Duplicate ticker page: {ticker}")
        expected.add(filename)
        company_events = sorted(by_company.get(ticker, []), key=lambda e: e.get("detected_at", ""), reverse=True)
        enriched = {**c, "_has_events": bool(company_events)}
        companies.append(enriched)
        (folder / filename).write_text(company_page(c, company_events, state, base), encoding="utf-8")
    # This is a generated-only directory; avoid stale company pages from removed tickers.
    for existing in folder.glob("*.html"):
        if existing.name not in expected:
            existing.unlink()
    if base:
        (output / "sitemap.xml").write_text(sitemap(companies, base), encoding="utf-8")
        robots = f"User-agent: *\nAllow: /\nSitemap: {base}sitemap.xml\n"
    else:
        (output / "sitemap.xml").unlink(missing_ok=True)
        robots = "User-agent: *\nAllow: /\n"
    (output / "robots.txt").write_text(robots, encoding="utf-8")
    (output / ".nojekyll").write_text("", encoding="utf-8")
