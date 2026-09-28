"""Read-only recall discovery. Search results are leads until a primary source is read."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode, urlsplit
from xml.etree import ElementTree

import httpx
from tools.web_search import WebSearchCapability, clean_text, host_allowed, public_web_url


FDA_CATEGORIES = ("food", "drug", "device")
FDA_API = "https://api.fda.gov"
FDA_RSS = "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/recalls/rss.xml"


class RecallDiscoveryCapability:
    """Find regulator records and web leads; never infer a lot match or place a hold."""

    def __init__(self, *, brave_key="", fda_key="", supplier_domains=(), client=None, clock=None,
                 web_search=None, include_fda_feed=True):
        self.fda_key = fda_key
        self.supplier_domains = tuple(d.lower().rstrip(".") for d in supplier_domains)
        if any(not public_web_url("https://" + domain) for domain in self.supplier_domains):
            raise ValueError("Supplier domains must be public DNS names")
        self.client = client or httpx.Client(timeout=15, follow_redirects=False)
        self.owns_client = client is None
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.web_search = web_search or WebSearchCapability(brave_key=brave_key, client=self.client,
                                                             clock=self.clock)
        self.web_search_enabled = bool(brave_key or (web_search and getattr(web_search, "brave_key", True)))
        self.include_fda_feed = include_fda_feed

    def close(self):
        if self.owns_client:
            self.client.close()

    def _json_get(self, url, *, params=None, headers=None):
        response = self.client.get(url, params=params, headers=headers)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def _fda(self, query, category, since, limit):
        terms = [word for word in re.findall(r"[\w-]+", query) if len(word) >= 3]
        if not terms:
            return []
        # Search text, then retain the FDA's complete product and code fields for review.
        search = " AND ".join(f'product_description:"{word}"' for word in terms[:3])
        if since:
            search += f" AND report_date:[{since.replace('-', '')} TO 99991231]"
        endpoint = f"{FDA_API}/{category}/enforcement.json"
        params = {"search": search, "limit": min(limit, 100), "sort": "report_date:desc"}
        if self.fda_key:
            params["api_key"] = self.fda_key
        data = self._json_get(endpoint, params=params)
        found = []
        for record in (data or {}).get("results", []):
            number = clean_text(record.get("recall_number"), 100)
            if not number:
                continue
            citation = endpoint + "?" + urlencode({"search": f'recall_number:"{number}"', "limit": 1})
            fields = {key: clean_text(record.get(key)) for key in (
                "product_description", "code_info", "reason_for_recall", "distribution_pattern",
                "recalling_firm", "report_date", "recall_initiation_date", "classification", "status")}
            found.append({"id": f"fda:{category}:{number}", "source_type": "regulator_record",
                          "verification": "primary_record", "category": category,
                          "title": f"FDA enforcement record {number}", "url": citation,
                          "retrieved_at": self.clock().isoformat(), "fields": fields,
                          "record_id": number})
        return found

    def _search_web(self, query, count):
        if not self.web_search_enabled:
            return []
        searches = [f"{query} recall site:fda.gov", f"{query} recall expanded lots"]
        searches += [f"{query} recall site:{domain}" for domain in self.supplier_domains]
        results = []
        for search in searches:
            results.extend(self.web_search.search(search, count=count))
        return results

    def _fda_feed(self, query, since, limit):
        if not self.include_fda_feed:
            return []
        response = self.client.get(FDA_RSS)
        response.raise_for_status()
        if len(response.content) > 1_000_000:
            raise ValueError("FDA recall feed exceeded size limit")
        root = ElementTree.fromstring(response.content)
        terms = [term.lower() for term in re.findall(r"[\w-]+", query) if len(term) >= 4]
        results = []
        for item in root.findall("./channel/item"):
            title = clean_text(item.findtext("title"), 500)
            if not terms or not all(term in title.lower() for term in terms):
                continue
            published = item.findtext("pubDate", "")
            try:
                published_date = parsedate_to_datetime(published).date().isoformat()
            except (TypeError, ValueError):
                continue
            if since and published_date < since:
                continue
            raw_url = item.findtext("link", "")
            parsed = urlsplit(raw_url)
            url = parsed._replace(scheme="https").geturl() if parsed.scheme == "http" else raw_url
            if not host_allowed(url, ("fda.gov",)):
                continue
            results.append({"id": "fda-feed:" + hashlib.sha256(url.encode()).hexdigest()[:16],
                            "source_type": "regulator_feed_entry", "verification": "lead_only",
                            "title": title, "url": url, "published_date": published_date,
                            "retrieved_at": self.clock().isoformat()})
            if len(results) >= limit:
                break
        return results

    def _verify_primary_page(self, lead):
        url = lead["url"]
        result = self.web_search.read_primary(lead, trusted_domains=("fda.gov", *self.supplier_domains))
        if result["verification"] == "primary_page_read":
            result["source_type"] = "regulator_notice" if host_allowed(url, ("fda.gov",)) else "supplier_notice"
        return result

    def search(self, query, *, categories=FDA_CATEGORIES, since=None, limit=10):
        query = clean_text(query, 160)
        if len(query) < 3 or limit < 1 or limit > 20:
            raise ValueError("Provide a product or brand query and a limit between 1 and 20")
        if since:
            datetime.strptime(since, "%Y-%m-%d")
        if not categories or any(category not in FDA_CATEGORIES for category in categories):
            raise ValueError("Categories must be food, drug, or device")
        evidence, errors = [], []
        for category in categories:
            try:
                evidence.extend(self._fda(query, category, since, limit))
            except (httpx.HTTPError, ValueError, KeyError) as error:
                errors.append({"source": f"openFDA:{category}", "error_type": type(error).__name__})
        try:
            leads = self._fda_feed(query, since, limit)
            seen = {item["url"] for item in evidence}
            for lead in leads:
                if lead["url"] not in seen:
                    evidence.append(self._verify_primary_page(lead))
                    seen.add(lead["url"])
        except (httpx.HTTPError, ValueError, ElementTree.ParseError) as error:
            errors.append({"source": "FDA recall RSS", "error_type": type(error).__name__})
        try:
            leads = self._search_web(query, limit)
            seen = {item["url"] for item in evidence}
            for lead in leads:
                if lead["url"] not in seen:
                    evidence.append(self._verify_primary_page(lead))
                    seen.add(lead["url"])
        except (httpx.HTTPError, ValueError, KeyError) as error:
            errors.append({"source": "Brave Search", "error_type": type(error).__name__})
        return {"query": query, "searched_at": self.clock().isoformat(), "evidence": evidence,
                "errors": errors, "coverage": {"openfda_categories": list(categories),
                                              "fda_feed_enabled": self.include_fda_feed,
                                              "web_search_enabled": self.web_search_enabled},
                "next_step": "quality_review",
                "note": "Web leads require an underlying regulator or supplier notice. Product and lot matches require separate exact checks before an approved hold."}


def main():
    parser = argparse.ArgumentParser(description="Read-only recall web search")
    parser.add_argument("query", help="Product or brand to investigate")
    parser.add_argument("--category", choices=FDA_CATEGORIES, action="append")
    parser.add_argument("--since", help="Earliest FDA report date, YYYY-MM-DD")
    parser.add_argument("--supplier-domain", action="append", default=[], help="Trusted supplier notice domain")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    from email_orchestrator import load_local_env
    load_local_env()
    if os.getenv("SHIPMENT_EMAIL_ENV_FILE"):
        load_local_env(os.environ["SHIPMENT_EMAIL_ENV_FILE"], overwrite=True)
    agent = RecallDiscoveryCapability(brave_key=os.getenv("BRAVE_SEARCH_API_KEY", ""),
                                      fda_key=os.getenv("OPENFDA_API_KEY", ""),
                                      supplier_domains=args.supplier_domain)
    try:
        print(json.dumps(agent.search(args.query, categories=args.category or FDA_CATEGORIES,
                                      since=args.since, limit=args.limit), indent=2))
    finally:
        agent.close()


if __name__ == "__main__":
    main()
