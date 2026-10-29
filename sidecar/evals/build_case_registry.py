"""Build a machine-readable inventory from the reviewed-in-progress case catalog."""

import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "docs/agent-eval-case-catalog.md"
RESPONSE = Path(__file__).with_name("response_cases.json")
RECALL = Path(__file__).with_name("recall_interpretation_cases.json")
OUTPUT = Path(__file__).with_name("case_registry.json")


def main():
    response = json.loads(RESPONSE.read_text())
    recall = json.loads(RECALL.read_text())
    fixture_sources = {case["catalog_id"]: "response_cases.json"
                       for case in response["cases"]}
    fixture_sources.update({case["id"]: "recall_interpretation_cases.json"
                            for case in recall["cases"]})
    cases = []
    for line in CATALOG.read_text().splitlines():
        match = re.match(r"^\| ((?:S1|E1|E2|C1|I1|I2|G1|R1|R2|R3|X1)-\d{2}) \|", line)
        if not match:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 4:
            raise ValueError("Malformed case row: " + match.group(1))
        case_id, evidence, expected, oracle = cells
        cases.append({"id": case_id, "capability": case_id.split("-")[0],
                      "trigger_evidence_design": evidence,
                      "expected_decision_effect": expected,
                      "output_oracle_design": oracle,
                      "fixture_file": fixture_sources.get(case_id),
                      "status": "executable_synthetic" if case_id in fixture_sources
                                else "designed_needs_fixture"})
    if len(cases) != 110 or len({case["id"] for case in cases}) != 110:
        raise ValueError("Expected 110 unique case IDs")
    OUTPUT.write_text(json.dumps({"catalog_version": "agent-cases-v1",
                                  "label_status": "domain_review_pending",
                                  "case_count": len(cases), "cases": cases}, indent=2) + "\n")
    print(f"Wrote {OUTPUT}: {sum(c['fixture_file'] is not None for c in cases)} "
          f"fixture-backed / {len(cases)} designed")


if __name__ == "__main__":
    main()
