import pytest
import httpx

from email_orchestrator import OpenRouter
from recall_interpreter import RecallNoticeInterpreter


NOTICE = {"verification": "primary_page_read", "url": "https://www.fda.gov/recall",
          "source_excerpt": "Par Pharmaceutical Dexmedetomidine HCl in 0.9% Sodium Chloride Injection "
                            "400 mcg/100 mL. NDC 42023-187-01. Lots 87558 and 88802. "
                            "Wholesale distributors should immediately discontinue use and stop distribution."}
ANSWER = {"brand": "Par Pharmaceutical",
          "product": "Dexmedetomidine HCl in 0.9% Sodium Chloride Injection 400 mcg/100 mL",
          "ndc": "42023-187-01", "lots": ["87558", "88802"],
          "lot_scope": "exact_list", "scope_quote": "Lots 87558 and 88802",
          "action_quote": "Wholesale distributors should immediately discontinue use and stop distribution."}


class FakeModel:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def extract(self, prompt, evidence):
        self.calls.append((prompt, evidence))
        return self.answer, {"model": "mock-recall", "usage": {"total_tokens": 100}}


def test_extracts_only_grounded_primary_notice_identifiers():
    model = FakeModel(ANSWER)
    result = RecallNoticeInterpreter(model).interpret(NOTICE)
    assert result["ndc"] == "42023-187-01"
    assert result["lots"] == ["87558", "88802"]
    assert result["model_audit"]["model"] == "mock-recall"
    assert model.calls[0][1]["notice_text"] == NOTICE["source_excerpt"]


@pytest.mark.parametrize("scope_text", [
    "all lots", "lot range A100 through A120", "lots A100 to A120",
    "lots A100–A120",
])
def test_non_enumerated_lot_scope_requires_review_after_model_extraction(scope_text):
    model = FakeModel(ANSWER | {'lot_scope': 'range' if 'range' in scope_text or
                               'to' in scope_text or 'through' in scope_text or
                               '–' in scope_text else 'all_lots',
                               'scope_quote': scope_text})
    notice = NOTICE | {"source_excerpt": NOTICE["source_excerpt"] + " " + scope_text}
    with pytest.raises(ValueError, match="requires human review"):
        RecallNoticeInterpreter(model).interpret(notice)
    assert len(model.calls) == 1


@pytest.mark.parametrize("notice,answer", [
    ({**NOTICE, "verification": "lead_only"}, ANSWER),
    (NOTICE, {**ANSWER, "lots": ["87558", "99999"]}),
    (NOTICE, {**ANSWER, "action_quote": "Place a WMS hold now."}),
])
def test_rejects_unverified_source_or_ungrounded_claims(notice, answer):
    with pytest.raises(ValueError):
        RecallNoticeInterpreter(FakeModel(answer)).interpret(notice)


def test_openrouter_adapter_sends_notice_and_returns_audited_extraction():
    def respond(request):
        assert request.url.path == "/api/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer fake-key"
        payload = __import__("json").loads(request.content)
        assert payload["model"] == "test/model"
        assert payload["messages"][1]["content"].find("42023-187-01") >= 0
        return httpx.Response(200, json={"id": "mock-response", "choices": [{
            "finish_reason": "stop", "message": {"content": __import__("json").dumps(ANSWER)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 60, "total_tokens": 160}})

    client = httpx.Client(transport=httpx.MockTransport(respond))
    model = OpenRouter("fake-key", "test/model", client=client)
    result = RecallNoticeInterpreter(model).interpret(NOTICE)
    assert result["lots"] == ["87558", "88802"]
    assert result["model_audit"]["response_id"] == "mock-response"
    client.close()
