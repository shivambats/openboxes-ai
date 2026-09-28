"""Exact product and lot scope check; never makes a WMS change."""

import re


def affected_lot_match(scope: dict, *, product_ndc: str, lot_number: str) -> bool:
    """Return true only for a verified notice with the same NDC and exact lot."""
    if scope.get("verification") not in {"primary_page_read", "primary_record"}:
        return False
    cited_ndc = re.sub(r"\D", "", scope.get("ndc", ""))
    candidate_ndc = re.sub(r"\D", "", product_ndc or "")
    if not cited_ndc or cited_ndc != candidate_ndc:
        return False
    lot = (lot_number or "").strip().upper()
    return bool(lot) and lot in {(value or "").strip().upper() for value in scope.get("lots", ())}
