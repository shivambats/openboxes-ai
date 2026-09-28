"""Recall research using reusable model-directed web search and verified FDA reading."""

from __future__ import annotations

import re

from tools.model_web_search import OpenRouterWebSearchCapability
from tools.web_search import host_allowed


class RecallServerWebAgent:
    """Find a recall through model web search, then check the primary page."""

    def __init__(self, model, web):
        self.search = OpenRouterWebSearchCapability(model)
        self.web = web

    def research(self, query: str, *, firm_hint: str = "", allow_no_notice: bool = False):
        if not isinstance(query, str) or not 3 <= len(query) <= 250:
            raise ValueError("A product query is required")
        result = self.search.search(
            f"Search the web for the official FDA recall notice for {query}; "
            f"firm: {firm_hint or 'unspecified'}. Return one FDA recall notice URL for this "
            "specific product, or NONE if no matching notice is found. Do not propose inventory changes.",
            allowed_domains=("fda.gov",))
        if allow_no_notice and result["content"].strip().rstrip(".").upper() == "NONE":
            return {"notice": None, "tool_audit": {**result["audit"],
                    "cited_urls": result["cited_urls"], "selected_notice_url": None}}
        match = re.search(r"https://[^\s<>\]\[\"']+", result["content"])
        url = match.group(0).rstrip(".,);:") if match else ""
        if not host_allowed(url, ("fda.gov",)):
            raise ValueError("Model did not return an FDA URL")
        citations = result["cited_urls"]
        if citations and url not in citations:
            raise ValueError("Selected notice URL was absent from web-search citations")
        notice = self.web.read_primary({"url": url, "verification": "lead_only",
                                        "source_type": "regulator_notice", "title": "FDA recall notice"},
                                       trusted_domains=("fda.gov",))
        if notice.get("verification") != "primary_page_read":
            raise ValueError("Could not read the FDA notice selected by the model")
        if firm_hint and firm_hint.casefold() not in notice["source_excerpt"].casefold():
            raise ValueError("Selected notice does not mention the requested firm")
        return {"notice": notice, "tool_audit": {**result["audit"],
                "cited_urls": citations, "selected_notice_url": url}}
