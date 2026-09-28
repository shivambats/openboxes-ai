import json

import httpx
import pytest

from recall_tool_agent import RecallToolCallingAgent


class FakeDiscovery:
    def __init__(self):
        self.calls = []

    def search(self, query, *, categories, since):
        self.calls.append((query, categories, since))
        return {"errors": [], "evidence": [{"id": "fda-feed:par", "title": "Par Dexmedetomidine recall",
            "url": "https://www.fda.gov/notice", "verification": "primary_page_read",
            "source_type": "regulator_notice", "source_excerpt": "Par Health recall notice"}]}


class FakeModel:
    key = "fake-key"
    model = "test/model"

    def __init__(self, transport):
        self.client = httpx.Client(transport=httpx.MockTransport(transport))


def test_model_requests_one_search_tool_and_selects_primary_notice():
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            assert body["tool_choice"]["function"]["name"] == "search_recall_sources"
            return httpx.Response(200, json={"id": "search-response", "choices": [{"message": {
                "role": "assistant", "content": None, "tool_calls": [{"id": "call-1", "type": "function",
                "function": {"name": "search_recall_sources", "arguments": json.dumps({
                    "query": "Dexmedetomidine", "category": "drug"})}}]}}], "usage": {"total_tokens": 50}})
        assert body["messages"][-2]["role"] == "tool"
        assert body["messages"][-2]["tool_call_id"] == "call-1"
        return httpx.Response(200, json={"id": "select-response", "choices": [{"message": {
            "content": '{"notice_id":"fda-feed:par"}'}}], "usage": {"total_tokens": 40}})

    model = FakeModel(respond)
    discovery = FakeDiscovery()
    result = RecallToolCallingAgent(model, discovery).research(
        "Dexmedetomidine", category="drug", since="2026-09-01", firm_hint="Par Health")
    assert discovery.calls == [("Dexmedetomidine", ("drug",), "2026-09-01")]
    assert result["notice"]["id"] == "fda-feed:par"
    assert result["tool_audit"]["tool_call_id"] == "call-1"
    assert len(requests) == 2
    model.client.close()


def test_out_of_scope_model_tool_arguments_are_rejected_before_search():
    def respond(request):
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "tool_calls": [{"id": "call-1", "type": "function",
            "function": {"name": "search_recall_sources", "arguments":
                         '{"query":"unrelated product","category":"drug"}'}}]}}]})

    model = FakeModel(respond)
    discovery = FakeDiscovery()
    with pytest.raises(ValueError):
        RecallToolCallingAgent(model, discovery).research("Dexmedetomidine", category="drug")
    assert discovery.calls == []
    model.client.close()
