import httpx

from recall_web_search import RecallDiscoveryCapability


def test_search_keeps_news_as_lead_and_reads_primary_notice():
    calls = []

    def respond(request):
        calls.append(str(request.url))
        if request.url.host == "api.fda.gov":
            if "/food/" in request.url.path:
                return httpx.Response(200, json={"results": [{
                    "recall_number": "F-123-2026", "product_description": "Acme peanut butter 16 oz",
                    "code_info": "Lot A17", "report_date": "20260920"}]})
            return httpx.Response(404, json={"error": "No matches"})
        if request.url.host == "api.search.brave.com":
            return httpx.Response(200, json={"web": {"results": [
                {"url": "https://www.fda.gov/safety/recalls/acme", "title": "Acme notice", "description": "Lot A17"},
                {"url": "https://news.example/story", "title": "Recall grows", "description": "Lot B18"},
                {"url": "http://127.0.0.1/private", "title": "Local", "description": "Ignore"}]}})
        if request.url.host == "www.fda.gov":
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  text="<html><body><main>Acme peanut butter recall. " +
                                       "Affected package is 16 oz and affected lot is A17. " * 3 +
                                       "</main></body></html>")
        raise AssertionError(request.url)

    client = httpx.Client(transport=httpx.MockTransport(respond))
    agent = RecallDiscoveryCapability(client=client, brave_key="test-key", include_fda_feed=False)
    result = agent.search("Acme peanut butter", categories=("food",), since="2026-09-01")
    by_type = {row["source_type"]: row for row in result["evidence"]}
    assert by_type["regulator_record"]["fields"]["code_info"] == "Lot A17"
    assert by_type["regulator_record"]["verification"] == "primary_record"
    assert by_type["regulator_notice"]["verification"] == "primary_page_read"
    assert by_type["web_search"]["verification"] == "lead_only"
    assert by_type["web_search"]["url"] == "https://news.example/story"
    assert result["next_step"] == "quality_review"
    assert all("127.0.0.1" not in url for url in calls)
    assert len([url for url in calls if "www.fda.gov" in url]) == 1


def test_redirected_official_page_is_not_verified():
    def respond(request):
        if request.url.host == "api.fda.gov":
            return httpx.Response(404)
        if request.url.host == "api.search.brave.com":
            return httpx.Response(200, json={"web": {"results": [
                {"url": "https://www.fda.gov/notice", "title": "Notice"}]}})
        return httpx.Response(302, headers={"location": "https://elsewhere.example"})

    agent = RecallDiscoveryCapability(client=httpx.Client(transport=httpx.MockTransport(respond)),
                                      brave_key="test-key", include_fda_feed=False)
    result = agent.search("Acme", categories=("food",))
    assert len(result["evidence"]) == 1
    assert result["evidence"][0]["verification"] == "lead_only"


def test_missing_web_key_uses_fda_only_and_no_matching_records():
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404)))
    result = RecallDiscoveryCapability(client=client, include_fda_feed=False).search("Acme", categories=("drug", "device"))
    assert result["evidence"] == []
    assert result["errors"] == []


def test_fda_feed_finds_recent_notice_without_web_search_key():
    def respond(request):
        if request.url.host == "api.fda.gov":
            return httpx.Response(404)
        if request.url.path.endswith("rss.xml"):
            return httpx.Response(200, text="""<rss><channel><item>
                <title>Par Health Recalls Dexmedetomidine</title>
                <link>http://www.fda.gov/safety/recalls/par-dex</link>
                <pubDate>Fri, 18 Sep 2026 10:00:00 GMT</pubDate>
                </item></channel></rss>""")
        return httpx.Response(200, headers={"content-type": "text/html"},
                              text="<main>Par Health has recalled Dexmedetomidine. " +
                                   "Affected lot 87558, NDC 42023-187-01. " * 3 + "</main>")

    agent = RecallDiscoveryCapability(client=httpx.Client(transport=httpx.MockTransport(respond)))
    result = agent.search("Dexmedetomidine", categories=("drug",), since="2026-09-01")
    assert result["coverage"]["web_search_enabled"] is False
    assert result["evidence"][0]["verification"] == "primary_page_read"
    assert result["evidence"][0]["source_type"] == "regulator_notice"
