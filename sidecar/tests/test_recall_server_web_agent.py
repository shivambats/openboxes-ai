import json

import httpx
import pytest

from recall_server_web_agent import RecallServerWebAgent


URL = "https://www.fda.gov/safety/recalls/par-health-notice"


class FakeWeb:
    def read_primary(self, lead, *, trusted_domains):
        assert trusted_domains == ("fda.gov",)
        return {**lead, "verification": "primary_page_read",
                "source_excerpt": "Par Health recalled Dexmedetomidine lots 87558 and 88802"}


class FakeModel:
    key = "fake-key"
    model = "deepseek/deepseek-v4-flash-0731"

    def __init__(self, use_count, content=URL):
        self.requests = []

        def respond(request):
            self.requests.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "response-1", "usage": {
                "server_tool_use": {"web_search_requests": use_count}}, "choices": [{"message": {
                "content": content, "annotations": [{"type": "url_citation",
                    "url_citation": {"url": URL}}]}}]})

        self.client = httpx.Client(transport=httpx.MockTransport(respond))


def test_model_directed_server_web_search_and_primary_read():
    model = FakeModel(1)
    result = RecallServerWebAgent(model, FakeWeb()).research("Dexmedetomidine", firm_hint="Par Health")
    request = model.requests[0]
    assert request["tools"][0]["type"] == "openrouter:web_search"
    assert request["tools"][0]["parameters"]["allowed_domains"] == ["fda.gov"]
    assert request["max_tool_calls"] == 1
    assert result["tool_audit"]["web_search_requests"] == 1
    assert result["notice"]["verification"] == "primary_page_read"
    model.client.close()


def test_fails_closed_when_model_did_not_search():
    model = FakeModel(0)
    with pytest.raises(RuntimeError, match="did not report"):
        RecallServerWebAgent(model, FakeWeb()).research("Dexmedetomidine")
    model.client.close()


def test_no_matching_notice_is_only_allowed_for_product_case_search():
    model = FakeModel(1, content="NONE")
    result = RecallServerWebAgent(model, FakeWeb()).research(
        "Tylenol Extra Strength NDC 70264-027-01", allow_no_notice=True)
    assert result["notice"] is None
    assert result["tool_audit"]["web_search_requests"] == 1
    model.client.close()
