"""Seed isolated OpenBoxes recall fixtures and verify live, read-only discovery."""

from __future__ import annotations

import argparse
import json
import os
from urllib.parse import quote, urlsplit

from config import Config
from email_orchestrator import OpenRouter, load_local_env
from recall_interpreter import RecallNoticeInterpreter
from recall_matching import affected_lot_match
from recall_server_web_agent import RecallServerWebAgent
from recall_web_search import RecallDiscoveryCapability
from tools.openboxes import OpenBoxesTool
from tools.web_search import WebSearchCapability


NOTICE_URL = ("https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/"
              "par-health-issues-voluntary-nationwide-recall-two-lots-dexmedetomidine-hcl-09-sodium-chloride")
LABEL_URL = "https://www.dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=99e45e83-37a3-44df-9a2a-98ba7457a5bb"
CATEGORY_NAME = "Clinical Supplies"
PRODUCTS = (
    {"productCode": "RECALL-TEST-PAR-DEX-400", "name": "Par Pharmaceutical Dexmedetomidine HCl in 0.9% Sodium Chloride Injection 400 mcg/100 mL",
     "brandName": "Par Pharmaceutical", "manufacturer": "Par Health", "ndc": "42023-187-01",
     "lots": (("87558", True), ("88802", True), ("TEST-UNLISTED-001", False))},
    {"productCode": "RECALL-TEST-TYLENOL-500", "name": "Tylenol Extra Strength Acetaminophen 500 mg Tablets",
     "brandName": "Tylenol", "manufacturer": "Johnson & Johnson", "ndc": "70264-027-01",
     "lots": (("87558", False), ("TEST-TYLENOL-001", False))},
    {"productCode": "RECALL-TEST-BAXTER-DEX-400", "name": "Baxter Dexmedetomidine Hydrochloride in 0.9% Sodium Chloride Injection 400 mcg/100 mL",
     "brandName": "Baxter", "manufacturer": "Baxter Healthcare Corporation", "ndc": "0338-9557-12",
     "lots": (("87558", False),)},
)


def _local_only(url):
    if urlsplit(url).hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("Fixture writes are limited to a local OpenBoxes instance")


def _product(wms, code):
    rows = wms.request("GET", "api/products", params={"q": code, "max": 100})
    matches = [row for row in rows if row.get("productCode") == code]
    if len(matches) > 1:
        raise RuntimeError("Duplicate test product code: " + code)
    return matches[0] if matches else None


def _ndc_product_ids(wms, ndc):
    rows = wms.request("POST", "api/generic/product/search", json={"searchAttributes": [
        {"property": "ndc", "operator": "eq", "value": ndc}]})
    return {row["id"] for row in rows}


def _lot(wms, product_id, number):
    path = f"api/products/{quote(product_id, safe='')}/inventoryItems/{quote(number, safe='')}"
    return wms.request("GET", path).get("inventoryItem")


def seed(wms, *, apply=False):
    categories = wms.request("GET", "api/categories")
    category = next((row for row in categories if row.get("name") == CATEGORY_NAME), None)
    if not category:
        raise RuntimeError(f"Missing category: {CATEGORY_NAME}")
    actions = []
    for fixture in PRODUCTS:
        product = _product(wms, fixture["productCode"])
        if product:
            if product.get("name") != fixture["name"]:
                raise RuntimeError(f"Existing {fixture['productCode']} has a different name")
            if product["id"] not in _ndc_product_ids(wms, fixture["ndc"]):
                raise RuntimeError(f"Existing {fixture['productCode']} has a different NDC")
        else:
            actions.append({"action": "create_product", "code": fixture["productCode"]})
            if apply:
                payload = {key: fixture[key] for key in ("productCode", "name", "brandName", "manufacturer", "ndc")}
                payload.update({"category": {"id": category["id"]}, "productType": {"id": "DEFAULT"},
                                "lotControl": True, "active": True,
                                "description": "RECALL TEST DATA ONLY — no physical stock or dispensing."})
                result = wms.request("POST", "api/generic/product/", json=payload)
                product = result.get("product", result)
                if not product.get("id"):
                    raise RuntimeError("Product creation did not return an ID")
        for number, _ in fixture["lots"]:
            existing = _lot(wms, product["id"], number) if product else None
            if existing:
                continue
            actions.append({"action": "create_zero_stock_lot", "code": fixture["productCode"], "lot": number})
            if apply:
                wms.request("POST", "api/generic/inventoryItem/", json={
                    "product": {"id": product["id"]}, "lotNumber": number,
                    "comments": "RECALL TEST DATA ONLY — no physical stock."})
                if not _lot(wms, product["id"], number):
                    raise RuntimeError("Lot creation did not persist: " + number)
    return {"applied": apply, "actions": actions}


def verify(wms, *, brave_key="", fda_key="", llm_key="", llm_model="", use_llm=False):
    web = WebSearchCapability(brave_key=brave_key)
    recall = RecallDiscoveryCapability(web_search=web, fda_key=fda_key)
    model = OpenRouter(llm_key, llm_model) if use_llm and llm_key and llm_model else None
    try:
        tool_audit = None
        if use_llm:
            if not model:
                raise ValueError("OPENROUTER_API_KEY and a recall model are required for --llm")
            research = RecallServerWebAgent(model, web).research(
                "Dexmedetomidine", firm_hint="Par Health")
            notice, tool_audit = research["notice"], research["tool_audit"]
            discovery = {"evidence": [notice], "errors": [],
                         "coverage": {"web_search_enabled": True}}
        else:
            discovery = recall.search("Dexmedetomidine", categories=("drug",), since="2026-09-01")
            notice = next((item for item in discovery["evidence"] if item["url"] == NOTICE_URL), None)
        if discovery["errors"]:
            raise RuntimeError("Recall discovery failed: " + json.dumps(discovery["errors"]))
        if not notice or notice["verification"] != "primary_page_read" or notice["url"] != NOTICE_URL:
            raise RuntimeError("FDA recall notice was not found and read")
        notice_text = notice["source_excerpt"]
        for value in ("42023-187-01", "87558", "88802", "Dexmedetomidine"):
            if value.lower() not in notice_text.lower():
                raise RuntimeError("FDA notice did not contain expected identifier: " + value)
        model_audit = None
        if use_llm:
            scope = RecallNoticeInterpreter(model).interpret(notice)
            model_audit = scope.pop("model_audit")
            if scope["ndc"] != "42023-187-01" or set(scope["lots"]) != {"87558", "88802"}:
                raise AssertionError("Model extraction did not match the FDA notice fixture")
        else:
            scope = {"verification": notice["verification"], "ndc": "42023-187-01",
                     "lots": ("87558", "88802"), "source_url": notice["url"]}
        label = web.read_primary({"url": LABEL_URL, "verification": "lead_only"},
                                 trusted_domains=("dailymed.nlm.nih.gov",))
        if label["verification"] != "primary_page_read" or "acetaminophen" not in label["source_excerpt"].lower():
            raise RuntimeError("Comparison drug label could not be read")
        cases = []
        for fixture in PRODUCTS:
            product = _product(wms, fixture["productCode"])
            if not product:
                raise RuntimeError("Missing test product: " + fixture["productCode"])
            if product["id"] not in _ndc_product_ids(wms, fixture["ndc"]):
                raise RuntimeError("Test product NDC changed: " + fixture["productCode"])
            for number, expected in fixture["lots"]:
                lot = _lot(wms, product["id"], number)
                if not lot:
                    raise RuntimeError(f"Missing test lot {fixture['productCode']} / {number}")
                # The recall is for one NDC and two exact lots. Product names and lot alone are insufficient.
                matched = affected_lot_match(scope, product_ndc=fixture["ndc"], lot_number=number)
                if matched != expected:
                    raise AssertionError(f"Unexpected match: {fixture['productCode']} / {number}")
                cases.append({"product_code": fixture["productCode"], "lot": number,
                              "expected_affected": expected, "actual_affected": matched,
                              "product_id": product["id"], "inventory_item_id": lot.get("id")})
        return {"result": "pass", "cases": cases,
                "recall_search": {"evidence_count": len(discovery["evidence"]),
                                  "fda_notice_discovered": True,
                                  "openfda_record_ids": [item["record_id"] for item in discovery["evidence"]
                                                         if item["source_type"] == "regulator_record"],
                                  "web_search_enabled": discovery["coverage"]["web_search_enabled"]},
                "sources": [NOTICE_URL, LABEL_URL], "wms_write": False,
                "llm_used": use_llm,
                "model_web_search_used": bool(tool_audit and tool_audit["web_search_requests"] > 0),
                "tool_audit": tool_audit, "model_audit": model_audit,
                "notice_extraction": {key: scope.get(key) for key in
                                      ("brand", "product", "ndc", "lots", "action_quote")},
                "scope": "Exact-match fixture check for this notice only; not a global drug safety determination"}
    finally:
        if model:
            model.client.close()
        recall.close()
        web.close()


def main():
    parser = argparse.ArgumentParser(description="Local OpenBoxes recall fixture and live verification")
    parser.add_argument("command", choices=("seed", "verify"))
    parser.add_argument("--apply", action="store_true", help="Create missing local fixtures; seed only")
    parser.add_argument("--llm", action="store_true", help="Use OpenRouter model-directed web search and notice extraction; verify only")
    parser.add_argument("--model", help="Explicit OpenRouter model ID for --llm")
    args = parser.parse_args()
    if args.apply and args.command != "seed":
        parser.error("--apply is only valid for seed")
    if args.llm and args.command != "verify":
        parser.error("--llm is only valid for verify")
    load_local_env()
    if os.getenv("SHIPMENT_EMAIL_ENV_FILE"):
        load_local_env(os.environ["SHIPMENT_EMAIL_ENV_FILE"], overwrite=True)
    llm_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_KEY", "")
    llm_model = args.model or os.getenv("RECALL_MODEL") or "deepseek/deepseek-v4-flash-0731"
    if args.llm and (not llm_key or not llm_model):
        parser.error("--llm requires OPENROUTER_API_KEY")
    config = Config(username=os.getenv("OPENBOXES_ADMIN_USERNAME", "admin"),
                    password=os.getenv("OPENBOXES_ADMIN_PASSWORD", ""), api_key="")
    _local_only(config.openboxes_url)
    if not config.password:
        raise ValueError("OPENBOXES_ADMIN_PASSWORD is required")
    wms = OpenBoxesTool(config)
    try:
        wms.login()
        outcome = seed(wms, apply=args.apply) if args.command == "seed" else verify(
            wms, brave_key=os.getenv("BRAVE_SEARCH_API_KEY", ""), fda_key=os.getenv("OPENFDA_API_KEY", ""),
            llm_key=llm_key, llm_model=llm_model,
            use_llm=args.llm)
        print(json.dumps(outcome, indent=2))
    finally:
        wms.close()


if __name__ == "__main__":
    main()
