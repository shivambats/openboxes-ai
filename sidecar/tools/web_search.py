"""Reusable, read-only public web search and bounded primary-page retrieval."""

from __future__ import annotations

import hashlib
import ipaddress
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx


BRAVE_API = "https://api.search.brave.com/res/v1/web/search"
MAX_BODY = 1_000_000


def clean_text(value, limit=4000):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def host_allowed(url: str, domains: tuple[str, ...]) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https" or parsed.username or parsed.password or port not in (None, 443):
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return bool(host) and any(host == domain or host.endswith("." + domain) for domain in domains)


def public_web_url(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        ipaddress.ip_address(host)
        return False
    except ValueError:
        return "." in host and not host.endswith((".localhost", ".local", ".internal")) and host_allowed(url, (host,))


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "nav", "footer", "header"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "nav", "footer", "header"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


class WebSearchCapability:
    """Search the public web; fetch HTML only from caller-supplied trusted domains.

    Search snippets are untrusted leads. A read page proves only that content was
    fetched from an allowed source; the caller must interpret its meaning.
    """

    def __init__(self, *, brave_key="", client=None, clock=None):
        self.brave_key = brave_key
        self.client = client or httpx.Client(timeout=15, follow_redirects=False)
        self.owns_client = client is None
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def close(self):
        if self.owns_client:
            self.client.close()

    def search(self, query: str, *, count=10):
        query = clean_text(query, 600)
        if not query or not 1 <= count <= 20:
            raise ValueError("Search needs a query and count between 1 and 20")
        if not self.brave_key:
            raise ValueError("BRAVE_SEARCH_API_KEY is required for web search")
        response = self.client.get(BRAVE_API, params={"q": query, "count": count},
                                   headers={"X-Subscription-Token": self.brave_key})
        response.raise_for_status()
        results = []
        for item in response.json().get("web", {}).get("results", []):
            url = item.get("url", "")
            if not public_web_url(url):
                continue
            results.append({"id": "web:" + hashlib.sha256(url.encode()).hexdigest()[:16],
                            "source_type": "web_search", "verification": "lead_only",
                            "title": clean_text(item.get("title"), 300), "url": url,
                            "snippet": clean_text(item.get("description"), 1000),
                            "retrieved_at": self.clock().isoformat()})
        return results

    def read_primary(self, lead: dict, *, trusted_domains: tuple[str, ...]):
        url = lead["url"]
        domains = tuple(domain.lower().rstrip(".") for domain in trusted_domains)
        if any(not public_web_url("https://" + domain) for domain in domains):
            raise ValueError("Trusted domains must be public DNS names")
        if not host_allowed(url, domains):
            return lead
        try:
            with self.client.stream("GET", url, headers={"Accept": "text/html"}) as response:
                response.raise_for_status()
                if "text/html" not in response.headers.get("content-type", "").lower():
                    return lead
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BODY:
                        return lead
            parser = _VisibleText()
            parser.feed(body.decode("utf-8", errors="replace"))
            excerpt = clean_text(" ".join(parser.parts), 12000)
            if len(excerpt) < 80:
                return lead
            return {**lead, "verification": "primary_page_read", "source_excerpt": excerpt,
                    "content_sha256": hashlib.sha256(body).hexdigest()}
        except (httpx.HTTPError, ValueError):
            return lead
