import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from radar import compute_financials
from site_builder import build_site, site_base_url


NCT = "NCT01234567"
STUDY = {
    "nct_id": NCT, "title": "Illustrative Phase 2 study <test>",
    "status": "RECRUITING", "phase": "PHASE2", "relationship": "Lead sponsor",
    "completion": "2027-09", "completion_type": "ESTIMATED",
    "enrollment": 120, "enrollment_type": "ESTIMATED",
    "endpoint": "Illustrative primary endpoint",
}
EVENT = {
    "id": "aaaa1111bbbb2222cc", "ticker": "CRSP", "nct_id": NCT,
    "field": "Estimated primary completion moved later", "before": "2027-03",
    "after": "2027-09", "detected_at": "2026-10-31T10:00:00Z",
    "importance": "high", "source_url": f"https://clinicaltrials.gov/study/{NCT}",
}


def fixture():
    return {
        "last_scan_at": "2026-10-31T11:00:00Z", "companies": {
            "CRSP": {
                "ticker": "CRSP", "name": "CRISPR Therapeutics",
                "clinical": {"last_success_at": "2026-10-31T11:00:00Z", "trials": {NCT: STUDY}},
                "financial": {"value_usd": 210_000_000, "as_of": "2026-06-30",
                              "filed": "2026-08-04", "kind": "cash and cash equivalents",
                              "status": "reported",
                              "filing_url": "https://www.sec.gov/Archives/edgar/data/123/0001/filing-index.htm"},
                "source_errors": {},
            },
            "NONE": {"ticker": "NONE", "name": "No Data Company",
                     "clinical": {"trials": {}}, "financial": {}, "source_errors": {}},
        }
    }


class StaticSiteTests(unittest.TestCase):
    def test_pages_and_sitemap_under_github_repository_prefix(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "GITHUB_REPOSITORY": "biocatalystradar/biotech-radar", "SITE_URL": "",
            "DEPLOY_TARGET": "", "CLOUDFLARE_PROJECT_NAME": "",
        }):
            out = Path(tmp)
            build_site(out, fixture(), [EVENT])
            crsp = (out / "company/crsp.html").read_text()
            homepage = (out / "index.html").read_text()
            rss = (out / "feed.xml").read_text()
            sitemap = (out / "sitemap.xml").read_text()
            robots = (out / "robots.txt").read_text()
            self.assertIn('href="company/crsp.html"', homepage)
            self.assertIn('id="company-crsp"', homepage)  # preserve existing hash links
            self.assertIn('href="https://biocatalystradar.github.io/biotech-radar/"', homepage)
            self.assertIn('href="https://biocatalystradar.github.io/biotech-radar/company/crsp.html"', crsp)
            self.assertIn('id="event-aaaa1111bbbb2222cc"', crsp)
            self.assertIn('Illustrative Phase 2 study &lt;test&gt;', crsp)
            self.assertIn('No changes recorded', (out / "company/none.html").read_text())
            self.assertIn('name="robots" content="noindex,follow"', (out / "company/none.html").read_text())
            self.assertIn('company/crsp.html#event-aaaa1111bbbb2222cc', rss)
            self.assertIn('Sitemap: https://biocatalystradar.github.io/biotech-radar/sitemap.xml', robots)
            root = ET.fromstring(sitemap)
            locations = [e.text for e in root.iter() if e.tag.endswith('loc')]
            self.assertEqual(locations, [
                'https://biocatalystradar.github.io/biotech-radar/',
                'https://biocatalystradar.github.io/biotech-radar/company/crsp.html',
            ])
            ET.fromstring(rss)

    def test_site_url_override(self):
        with patch.dict(os.environ, {"SITE_URL": "https://radar.example.com/base/"}):
            self.assertEqual(site_base_url(), "https://radar.example.com/base/")

    def test_security_rejects_unsafe_ticker_filename(self):
        state = fixture()
        state['companies']['CRSP']['ticker'] = '../bad'
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'Unsafe ticker'):
                build_site(Path(tmp), state, [EVENT])

    def test_does_not_expose_unreconciled_cash_breakdown(self):
        facts = {"us-gaap": {
            "CashCashEquivalentsAndShortTermInvestments": {"units": {"USD": [
                {"end": "2026-06-30", "filed": "2026-08-01", "form": "10-Q",
                 "accn": "0001234567-26-000001", "val": 500}]}}
        }}
        result = compute_financials({"facts": facts}, "0001234567")
        self.assertEqual(result["value_usd"], 500)
        self.assertIsNone(result['components'])

    def test_reconciled_components(self):
        entry = {"end": "2026-06-30", "filed": "2026-08-01", "form": "10-Q",
                 "accn": "0001234567-26-000001"}
        payload = {"facts": {"us-gaap": {
            "CashCashEquivalentsAndShortTermInvestments": {"units": {"USD": [{**entry,"val": 500}]}},
            "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [{**entry,"val": 350}]}},
            "ShortTermInvestments": {"units": {"USD": [{**entry,"val": 150}]}},
        }}}
        result = compute_financials(payload, "0001234567")
        self.assertEqual(result["components"]["cash_equivalents_usd"],350)
        self.assertEqual(result["components"]["short_term_investments_usd"],150)

if __name__ == '__main__':
    unittest.main()
