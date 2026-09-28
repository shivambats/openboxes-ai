import httpx

from tools.web_search import WebSearchCapability


def test_general_search_returns_cited_leads_and_rejects_private_urls():
    def respond(request):
        assert request.headers["X-Subscription-Token"] == "test-key"
        return httpx.Response(200, json={"web": {"results": [
            {"url": "https://example.org/article", "title": "Example", "description": "Finding"},
            {"url": "https://127.0.0.1/private", "title": "Private"},
            {"url": "https://localhost/private", "title": "Local"}]}})

    capability = WebSearchCapability(brave_key="test-key",
                                     client=httpx.Client(transport=httpx.MockTransport(respond)))
    leads = capability.search("medical device recall")
    assert len(leads) == 1
    assert leads[0]["verification"] == "lead_only"
    assert leads[0]["url"] == "https://example.org/article"


def test_read_primary_requires_explicit_domain_and_does_not_follow_redirect():
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://elsewhere.example"})

    capability = WebSearchCapability(client=httpx.Client(transport=httpx.MockTransport(respond)))
    lead = {"url": "https://news.example/article", "verification": "lead_only"}
    assert capability.read_primary(lead, trusted_domains=("fda.gov",)) == lead
    assert calls == []
    official = {"url": "https://www.fda.gov/notice", "verification": "lead_only"}
    assert capability.read_primary(official, trusted_domains=("fda.gov",)) == official
    assert calls == ["https://www.fda.gov/notice"]
