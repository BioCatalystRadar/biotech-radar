#!/usr/bin/env python3
"""BioCatalyst Radar: public clinical-registry and SEC XBRL snapshot monitor.

Output is informational, not investment advice. A change is detected when a
public record differs from the last *successful* snapshot; not necessarily on
the date the company made the change.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from site_builder import build_site

ROOT = Path(__file__).resolve().parent
CT_URL = "https://clinicaltrials.gov/api/v2/studies"
TICKER_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
ACTIVE = {"RECRUITING", "ACTIVE_NOT_RECRUITING", "NOT_YET_RECRUITING"}
PHASES = {"EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3"}
CHANGE_FIELDS = {
    "completion": "Estimated primary completion",
    "completion_type": "Completion date classification",
    "enrollment": "Enrollment target / count",
    "enrollment_type": "Enrollment classification",
    "endpoint": "Primary outcome measure",
    "endpoint_time_frame": "Primary outcome time frame",
    "phase": "Trial phase",
    "status": "Recruitment status",
}
# No tags imply all securities are readily available. We deliberately avoid
# broader cash labels containing restricted cash.
COMBINED_TAGS = ("CashCashEquivalentsAndShortTermInvestments",)
CASH_TAGS = ("CashAndCashEquivalentsAtCarryingValue",)
INVESTMENT_TAGS = (
    "ShortTermInvestments", "MarketableSecuritiesCurrent",
    "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
)
LOG = logging.getLogger("radar")


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3, backoff_factor=1.2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]), respect_retry_after_header=True,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({"Accept": "application/json", "Accept-Encoding": "gzip, deflate"})
    return s


def fetch_json(s: requests.Session, url: str, *, params=None, sec=False) -> dict:
    headers = {}
    if sec:
        agent = os.environ.get("SEC_USER_AGENT", "").strip()
        if not agent or "@" not in agent or "example." in agent:
            raise ValueError("SEC_USER_AGENT must identify you and include a real contact email")
        headers["User-Agent"] = agent
        # Conservative interval far below the SEC's published 10 requests/sec ceiling.
        time.sleep(0.3)
    resp = s.get(url, params=params, headers=headers, timeout=(8, 25))
    resp.raise_for_status()
    value = resp.json()
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def normalize_name(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    words = text.split()
    while words and words[-1] in {"inc", "incorporated", "corp", "corporation", "ltd", "limited", "llc", "plc", "se", "ag"}:
        words.pop()
    return " ".join(words)


def sponsor_relationship(section: dict, aliases: list[str]) -> str | None:
    sponsors = section.get("sponsorCollaboratorsModule") or {}
    alias_names = {normalize_name(x) for x in aliases}
    lead = (sponsors.get("leadSponsor") or {}).get("name", "")
    if normalize_name(lead) in alias_names:
        return "Lead sponsor"
    collaborators = sponsors.get("collaborators") or []
    if any(normalize_name(x.get("name", "")) in alias_names for x in collaborators):
        return "Collaborator"
    return None


def extract_trial(study: dict, aliases: list[str]) -> dict | None:
    p = study.get("protocolSection") or {}
    relationship = sponsor_relationship(p, aliases)
    if not relationship:
        return None  # Fuzzy sponsor search is not identity verification.
    ident = p.get("identificationModule") or {}
    nct = ident.get("nctId", "")
    if not re.fullmatch(r"NCT\d{8}", nct):
        return None
    status = p.get("statusModule") or {}
    design = p.get("designModule") or {}
    outcomes = p.get("outcomesModule") or {}
    phases = design.get("phases") or []
    if (p.get("designModule") or {}).get("studyType") not in (None, "INTERVENTIONAL"):
        return None
    if not any(ph in PHASES for ph in phases):
        return None
    date_info = status.get("primaryCompletionDateStruct") or {}
    enrollment = design.get("enrollmentInfo") or {}
    primary = outcomes.get("primaryOutcomes") or []
    primary_measures = [x.get("measure", "").strip() for x in primary if x.get("measure")]
    time_frames = [x.get("timeFrame", "").strip() for x in primary if x.get("timeFrame")]
    trial = {
        "nct_id": nct,
        "title": ident.get("briefTitle", ""),
        "relationship": relationship,
        "status": status.get("overallStatus", "UNKNOWN"),
        "phase": "/".join(phases),
        "completion": date_info.get("date") or None,
        "completion_type": date_info.get("type") or None,
        "enrollment": enrollment.get("count"),
        "enrollment_type": enrollment.get("type") or None,
        "endpoint": "; ".join(primary_measures) or None,
        "endpoint_time_frame": "; ".join(time_frames) or None,
        "registry_last_update": (status.get("studyFirstPostDateStruct") or {}).get("date"),
    }
    trial["registry_last_update"] = (
        (status.get("studyLastUpdatePostDateStruct") or {}).get("date")
        or (status.get("lastUpdatePostDateStruct") or {}).get("date")
    )
    return trial


def fetch_trials(s: requests.Session, config: dict) -> dict[str, dict]:
    aliases = config.get("aliases", [config["name"]])
    queries = config.get("queries", [aliases[0]])
    results = {}
    for query in queries:
        token = None
        for page_no in range(20):
            params = {"query.spons": query, "pageSize": 200, "format": "json"}
            if token:
                params["pageToken"] = token
            result = fetch_json(s, CT_URL, params=params)
            if not isinstance(result.get("studies"), list):
                raise ValueError(f"Unexpected ClinicalTrials.gov response for {config['ticker']}; keeping prior snapshot")
            for study in result["studies"]:
                trial = extract_trial(study, aliases)
                if trial:
                    results[trial["nct_id"]] = trial
            token = result.get("nextPageToken")
            if not token:
                break
        else:
            raise RuntimeError(f"Pagination cap exceeded for {config['ticker']}; refusing partial snapshot")
    return results


def fetch_ticker_map(s: requests.Session) -> dict[str, dict]:
    payload = fetch_json(s, TICKER_URL, sec=True)
    return {entry["ticker"].upper(): {"cik": str(entry["cik_str"]).zfill(10),
                                      "issuer": entry.get("title", "")}
            for entry in payload.values() if entry.get("ticker") and entry.get("cik_str")}


def verified_cik(ticker_map: dict, config: dict) -> str | None:
    issuer = ticker_map.get(config["ticker"])
    if not issuer:
        return None
    allowed = {normalize_name(x.split(" (formerly")[0]) for x in
               [config["name"], *config.get("aliases", [])]}
    actual = normalize_name(issuer.get("issuer", ""))
    if actual not in allowed:
        LOG.warning("SEC ticker %s currently belongs to %r, not configured %r; SEC skipped",
                    config["ticker"], issuer.get("issuer"), config["name"])
        return None
    return issuer["cik"]


def latest_instant(facts: dict, tags: tuple[str, ...], *, taxonomy="us-gaap") -> dict | None:
    candidates = []
    for rank, tag in enumerate(tags):
        for entry in (facts.get(taxonomy, {}).get(tag, {}).get("units", {}).get("USD", [])):
            if entry.get("form") not in {"10-Q", "10-K", "10-Q/A", "10-K/A", "20-F", "20-F/A", "6-K"}:
                continue
            if ("start" in entry or "end" not in entry or not isinstance(entry.get("val"), (int, float))
                    or entry["val"] < 0 or entry.get("end", "") > datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    or entry.get("filed", "") > datetime.now(timezone.utc).strftime("%Y-%m-%d")):
                continue
            # For facts repeated across reports prefer same-date recent filing, then tag priority.
            candidates.append({**entry, "tag": tag, "taxonomy": taxonomy, "_rank": rank})
    if not candidates:
        return None
    return max(candidates, key=lambda e: (e.get("end", ""), e.get("filed", ""), -e["_rank"]))


def compute_financials(payload: dict, cik: str) -> dict:
    facts = payload.get("facts")
    if not isinstance(facts, dict):
        raise ValueError("Unexpected SEC companyfacts response; keeping prior snapshot")
    combined = latest_instant(facts, COMBINED_TAGS)
    cash = latest_instant(facts, CASH_TAGS)
    # For foreign private issuers, IFRS data sometimes exists but is not assured.
    if not cash and not combined:
        cash = latest_instant(facts, ("CashAndCashEquivalents",), taxonomy="ifrs-full")
    newest_date = max((v["end"] for v in (combined, cash) if v), default=None)
    if newest_date is None:
        return {"value_usd": None, "as_of": None, "filed": None,
                "kind": "unavailable", "filing_url": None, "status": "unavailable"}
    use_combined = combined is not None and combined["end"] == newest_date
    chosen = combined if use_combined else cash
    if chosen is None:
        raise AssertionError("No selected XBRL figure")
    total = chosen["val"]
    kind = "cash and short-term investments" if use_combined else "cash and cash equivalents"
    components = None  # Never manufacture a breakdown when only an aggregate was published.
    investments = latest_instant(facts, INVESTMENT_TAGS) if chosen["taxonomy"] == "us-gaap" else None
    def same_filing(a, b):
        return bool(a and b and a.get("accn") and b.get("accn") == a.get("accn")
                    and b.get("end") == a.get("end") and b.get("val", -1) >= 0)
    if use_combined:
        # Only show components if they independently reconcile to the combined figure.
        if (same_filing(chosen, cash) and same_filing(chosen, investments)
                and abs(cash["val"] + investments["val"] - total) <= 1):
            components = {"cash_equivalents_usd": cash["val"],
                          "short_term_investments_usd": investments["val"],
                          "verified_same_filing": True}
    elif same_filing(chosen, investments):
        total += investments["val"]
        kind = "cash and short-term investments"
        components = {"cash_equivalents_usd": chosen["val"],
                      "short_term_investments_usd": investments["val"],
                      "verified_same_filing": True}
    accn = chosen.get("accn") or ""
    filing_url = None
    if re.fullmatch(r"\d{10}-\d{2}-\d{6}", accn):
        filing_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn.replace('-', '')}/{accn}-index.htm"
    try:
        stale = datetime.fromisoformat(chosen["end"]).date() < (datetime.now(timezone.utc).date() - timedelta(days=180))
    except ValueError:
        stale = True
    return {"value_usd": total, "as_of": chosen["end"],
            "filed": chosen.get("filed"), "kind": kind,
            "components": components, "filing_url": filing_url,
            "status": "stale" if stale else "reported"}


def fetch_financials(s: requests.Session, cik: str) -> dict:
    payload = fetch_json(s, SEC_FACTS.format(cik=cik), sec=True)
    return compute_financials(payload, cik)


def make_event(ticker: str, nct: str | None, label: str, before: Any, after: Any,
               at: str, importance: str, url: str | None = None) -> dict:
    import hashlib
    # Event identity is the observation, including when observed, not a prediction.
    signature = json.dumps([ticker, nct, label, before, after, at], sort_keys=True, default=str)
    return {"id": hashlib.sha256(signature.encode()).hexdigest()[:18],
            "ticker": ticker, "nct_id": nct, "field": label, "before": before,
            "after": after, "detected_at": at, "importance": importance, "source_url": url}


def completion_direction(before: str | None, after: str | None) -> str:
    """Only call a date a delay if non-overlapping precision windows prove it."""
    import calendar
    from datetime import date
    def bounds(value):
        if not value:
            return None
        try:
            parts = [int(p) for p in value.split("-")]
            if len(parts) == 1:
                return date(parts[0], 1, 1), date(parts[0], 12, 31)
            if len(parts) == 2:
                y, m = parts
                return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
            if len(parts) == 3:
                d = date(*parts)
                return d, d
        except (ValueError, TypeError):
            return None
        return None
    previous, current = bounds(before), bounds(after)
    if not previous or not current:
        return "changed"
    if current[0] > previous[1]:
        return "moved later"
    if current[1] < previous[0]:
        return "moved earlier"
    return "changed (date precision or overlapping interval)"


def diff_trials(ticker: str, old: dict, new: dict, at: str) -> list[dict]:
    changes = []
    for nct, current in new.items():
        previous = old.get(nct)
        if previous is None:
            if current["status"] in ACTIVE:
                changes.append(make_event(ticker, nct, "Newly observed active study", None,
                                          current["title"], at, "medium",
                                          f"https://clinicaltrials.gov/study/{nct}"))
            continue
        was_active = previous.get("status") in ACTIVE
        is_active = current.get("status") in ACTIVE
        if was_active and not is_active:
            changes.append(make_event(ticker, nct, "Exited active-trial set",
                                      previous.get("status"), current.get("status"), at, "high",
                                      f"https://clinicaltrials.gov/study/{nct}"))
            continue
        if not is_active:
            continue  # No historical noise from inactive-only studies.
        for key, label in CHANGE_FIELDS.items():
            if previous.get(key) != current.get(key):
                importance = "high" if key in {"completion", "endpoint", "status"} else "medium"
                if key == "completion":
                    label += " " + completion_direction(previous.get(key), current.get(key))
                changes.append(make_event(ticker, nct, label, previous.get(key),
                                          current.get(key), at, importance,
                                          f"https://clinicaltrials.gov/study/{nct}"))
    for nct in old.keys() - new.keys():
        if old[nct].get("status") in ACTIVE:
            changes.append(make_event(ticker, nct, "No longer matches sponsor search",
                                      "Previously observed", "Requires verification", at,
                                      "review", f"https://clinicaltrials.gov/study/{nct}"))
    return changes


def diff_financials(ticker: str, old: dict, new: dict, at: str) -> list[dict]:
    if old.get("value_usd") is None or new.get("value_usd") is None:
        return []
    if (old.get("value_usd"), old.get("as_of")) == (new.get("value_usd"), new.get("as_of")):
        return []
    return [make_event(ticker, None, "Reported cash / investments updated",
                       f"{old['value_usd']} USD @ {old.get('as_of')}",
                       f"{new['value_usd']} USD @ {new.get('as_of')}", at, "medium",
                       new.get("filing_url"))]


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def atomic_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = load_json(ROOT / "config.json", {})
    watchlist = config["watchlist"]
    old = load_json(ROOT / "data/state.json", {"companies": {}})
    prior_events = load_json(ROOT / "data/events.json", [])
    first_success = not bool(old.get("companies"))
    s = session()
    at = utcnow()
    try:
        ticker_map = fetch_ticker_map(s)
    except (requests.RequestException, ValueError) as exc:
        ticker_map = {}
        LOG.warning("Ticker map unavailable: %s. Will not guess CIK identifiers.", exc)
    companies = {}
    new_events = []
    success_counts = {"clinical": 0, "sec": 0}
    for meta in watchlist:
        ticker = meta["ticker"]
        LOG.info("Scanning %s", ticker)
        prev = old.get("companies", {}).get(ticker, {})
        current = {"name": meta["name"], "ticker": ticker,
                   "spotlight": bool(meta.get("spotlight")),
                   "clinical": prev.get("clinical", {}),
                   "financial": prev.get("financial", {}), "source_errors": {}}
        try:
            trials = fetch_trials(s, meta)
            if not first_success and prev.get("clinical", {}).get("last_success_at"):
                new_events.extend(diff_trials(ticker, prev["clinical"].get("trials", {}), trials, at))
            current["clinical"] = {"trials": trials, "last_success_at": at}
            success_counts["clinical"] += 1
        except (requests.RequestException, ValueError, RuntimeError, KeyError) as exc:
            current["source_errors"]["clinical"] = str(exc)[:180]
            LOG.warning("Clinical source %s failed: %s", ticker, exc)
        cik = verified_cik(ticker_map, meta)
        if not cik:
            current["source_errors"]["sec"] = "Ticker unavailable or SEC issuer identity does not match watchlist"
        else:
            try:
                financial = fetch_financials(s, cik)
                financial["cik"] = cik
                financial["last_success_at"] = at
                if not first_success and prev.get("financial", {}).get("last_success_at"):
                    new_events.extend(diff_financials(ticker, prev.get("financial", {}), financial, at))
                current["financial"] = financial
                success_counts["sec"] += 1
            except (requests.RequestException, ValueError) as exc:
                current["source_errors"]["sec"] = str(exc)[:180]
                LOG.warning("SEC source %s failed: %s", ticker, exc)
        companies[ticker] = current
    # No historical false-positives on first successful baseline.
    event_ids = {e["id"] for e in prior_events}
    merged = prior_events[:]
    for event in new_events:
        if event["id"] not in event_ids:
            merged.append(event)
            event_ids.add(event["id"])
    merged = sorted(merged, key=lambda x: x["detected_at"], reverse=True)[:1500]
    state = {"last_scan_at": at, "companies": companies}
    atomic_json(ROOT / "data/state.json", state)
    atomic_json(ROOT / "data/events.json", merged)
    build_site(ROOT / "site", state, merged)
    LOG.info("Finished: clinical %s/%s, SEC %s/%s, new changes %d",
             success_counts["clinical"], len(watchlist),
             success_counts["sec"], len(watchlist), len(new_events))
    # First-time build can proceed without SEC, but do not publish a misleading
    # fully empty site if the trial registry is entirely inaccessible.
    if success_counts["clinical"] == 0 and not any(c.get("clinical", {}).get("last_success_at") for c in companies.values()):
        raise RuntimeError("No usable trial data available; deployment aborted")


if __name__ == "__main__":
    run()
