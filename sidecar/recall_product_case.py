"""Run one product-and-lot recall case with model-directed live web search."""

from __future__ import annotations

import argparse
import json
import os

from config import Config
from email_orchestrator import OpenRouter, load_local_env
from recall_interpreter import RecallNoticeInterpreter
from recall_live_test import _local_only, _lot, _ndc_product_ids, _product
from recall_matching import affected_lot_match
from recall_server_web_agent import RecallServerWebAgent
from tools.openboxes import OpenBoxesTool
from tools.web_search import WebSearchCapability


def run_case(wms, model, *, product_code: str, ndc: str, lot_number: str,
             expected_affected: bool, mark_attention: bool = False):
    product = _product(wms, product_code)
    if not product:
        raise ValueError("OpenBoxes product was not found: " + product_code)
    if product["id"] not in _ndc_product_ids(wms, ndc):
        raise ValueError("Supplied NDC does not belong to product: " + product_code)
    lot = _lot(wms, product["id"], lot_number)
    if not lot:
        raise ValueError(f"OpenBoxes lot was not found: {product_code} / {lot_number}")

    web = WebSearchCapability()
    try:
        query = f"{product['name']} NDC {ndc}"
        research = RecallServerWebAgent(model, web).research(query, allow_no_notice=True)
        notice = research["notice"]
        scope = RecallNoticeInterpreter(model).interpret(notice) if notice else None
        model_audit = scope.pop("model_audit") if scope else None
        affected = affected_lot_match(scope, product_ndc=ndc, lot_number=lot_number) if scope else False
        outcome = {"result": "pass" if affected == expected_affected else "fail",
                "product_input": {"product_code": product_code, "name": product["name"],
                                  "ndc": ndc, "lot": lot_number},
                "expected_affected": expected_affected, "actual_affected": affected,
                "basis": "exact_ndc_and_lot_in_verified_fda_notice" if affected else
                         "verified_notice_does_not_match_exact_ndc_and_lot" if notice else
                         "no_matching_notice_in_bounded_search",
                "notice_url": notice["url"] if notice else None,
                "notice_extraction": {key: scope.get(key) for key in ("brand", "product", "ndc", "lots")}
                                     if scope else None,
                "tool_audit": research["tool_audit"], "model_audit": model_audit,
                "openboxes_inventory_item_id": lot.get("id"), "wms_write": False,
                "scope": "Bounded FDA web research; an unaffected result is not a global drug safety determination"}
        if mark_attention and outcome["result"] == "pass":
            reason = ("VERIFIED_MATCH_PENDING_APPROVAL" if affected else
                      "NOTICE_DOES_NOT_MATCH" if notice else "SEARCH_NO_MATCH")
            outcome["review_marker"] = wms.request("POST", "api/recallReviews/attention", json={
                "productCode": product_code, "ndc": ndc, "lotNumber": lot_number,
                "reason": reason, "sourceUrl": notice["url"] if notice else None,
                "modelResponseId": research["tool_audit"]["response_id"]})
            outcome["wms_write"] = True
        return outcome
    finally:
        web.close()


def main():
    parser = argparse.ArgumentParser(description="Live product-input recall search and exact lot check")
    parser.add_argument("--product-code", required=True)
    parser.add_argument("--ndc", required=True)
    parser.add_argument("--lot", required=True)
    parser.add_argument("--expect", choices=("affected", "unaffected"), required=True)
    parser.add_argument("--mark-attention", action="store_true",
                        help="Write a Needs attention review marker on the tested lot; does not hold stock")
    parser.add_argument("--model", default=None, help="OpenRouter model ID")
    args = parser.parse_args()
    load_local_env()
    if os.getenv("SHIPMENT_EMAIL_ENV_FILE"):
        load_local_env(os.environ["SHIPMENT_EMAIL_ENV_FILE"], overwrite=True)
    key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_KEY", "")
    model_id = args.model or os.getenv("RECALL_MODEL") or "deepseek/deepseek-v4-flash-0731"
    if not key:
        parser.error("OPENROUTER_API_KEY or OPENROUTER_KEY is required")
    config = Config(username=os.getenv("OPENBOXES_ADMIN_USERNAME", "admin"),
                    password=os.getenv("OPENBOXES_ADMIN_PASSWORD", ""), api_key="")
    _local_only(config.openboxes_url)
    if not config.password:
        parser.error("OPENBOXES_ADMIN_PASSWORD is required")
    wms = OpenBoxesTool(config)
    model = OpenRouter(key, model_id)
    try:
        wms.login()
        result = run_case(wms, model, product_code=args.product_code, ndc=args.ndc,
                          lot_number=args.lot, expected_affected=args.expect == "affected",
                          mark_attention=args.mark_attention)
        print(json.dumps(result, indent=2))
        if result["result"] != "pass":
            raise SystemExit(1)
    finally:
        model.client.close()
        wms.close()


if __name__ == "__main__":
    main()
