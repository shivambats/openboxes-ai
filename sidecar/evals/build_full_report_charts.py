"""Build static SVG figures for the evaluation report from recorded run JSON."""

from collections import Counter
from html import escape
import json
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "docs" / "eval-runs"
FIGURES = RUNS / "figures"
REGISTRY = ROOT / "sidecar" / "evals" / "case_registry.json"

RUN_LABELS = [
    ("Identity / first six", "synthetic-case-graph-live-2026-09-28.json", "#4b70b2"),
    ("Identity / expanded 20", "synthetic-case-graph-live-v2-2026-09-28.json", "#4b70b2"),
    ("Identity / label check", "irrelevant-email-live-confirmation-2026-09-28.json", "#4b70b2"),
    ("Email / development", "live-email-response-development-2026-09-28.json", "#b5673a"),
    ("Email / review boundary", "live-email-response-review-boundary-2026-09-29.json", "#b5673a"),
    ("Email / targeted fixes", "live-email-response-after-fixes-2026-09-29.json", "#b5673a"),
    ("Email / before wording fix", "live-email-response-current-2026-09-29.json", "#b5673a"),
    ("Email / wording check", "live-email-response-wording-fix-2026-09-29.json", "#b5673a"),
    ("Email / final full run", "live-email-response-final-2026-09-29.json", "#b5673a"),
    ("Email / damage repeat", "live-email-damage-variability-2026-09-29.json", "#b5673a"),
    ("Recall / development", "live-recall-interpretation-development-2026-09-29.json", "#348877"),
    ("Recall / after guard", "live-recall-interpretation-after-guard-2026-09-29.json", "#348877"),
]


def load(name):
    return json.loads((RUNS / name).read_text())


def txt(x, y, value, size=14, fill="#263849", weight="normal", anchor="start"):
    return (f'<text x="{x}" y="{y}" font-family="Arial, sans-serif" font-size="{size}" '
            f'fill="{fill}" font-weight="{weight}" text-anchor="{anchor}">'
            f'{escape(str(value))}</text>')


def rect(x, y, w, h, fill, rx=0, stroke="none"):
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
            f'fill="{fill}" stroke="{stroke}"/>')


def line(x1, y1, x2, y2, stroke="#d5dce2", width=1):
    return (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
            f'stroke="{stroke}" stroke-width="{width}"/>')


def svg(width, height, title, description, pieces):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">'
            f'<title id="title">{escape(title)}</title>'
            f'<desc id="desc">{escape(description)}</desc>'
            + rect(0, 0, width, height, "#ffffff") + "".join(pieces) + "</svg>\n")


def coverage():
    registry = json.loads(REGISTRY.read_text())
    counts = Counter(c["capability"] for c in registry["cases"] if c["fixture_file"])
    capabilities = ["S1", "E1", "E2", "C1", "I1", "I2", "G1", "R1", "R2", "R3", "X1"]
    names = {"S1": "Overdue sweep", "E1": "Email identity", "E2": "Email facts/replies",
             "C1": "Carrier replies", "I1": "Image observation", "I2": "Image follow-up",
             "G1": "Case graph", "R1": "Recall discovery", "R2": "Recall interpretation",
             "R3": "Recall marker", "X1": "Effect boundary"}
    p = [txt(34, 42, "Executable catalog coverage", 24, "#142c3d", "bold"),
         txt(34, 68, "Each square represents one designed case; colored squares have frozen executable fixtures.", 13)]
    for i, cap in enumerate(capabilities):
        y = 100 + i * 36
        p.append(txt(34, y + 16, f"{cap}  {names[cap]}", 13))
        for j in range(10):
            p.append(rect(244 + j * 52, y, 44, 24,
                          "#348877" if j < counts[cap] else "#e8edf0", 4))
        p.append(txt(798, y + 17, f"{counts[cap]}/10", 14, "#142c3d", "bold"))
    p += [line(34, 505, 868, 505), txt(34, 533, "20 fixture-backed", 16, "#24725f", "bold"),
          txt(250, 533, "90 designed; fixtures and graders pending", 15)]
    return svg(900, 555, "Fixture coverage: 20 of 110 catalog cases",
               "E1 has 4, E2 has 6, R2 has 10; all other capabilities have zero executable catalog fixtures.", p)


def progression():
    groups = [
        ("Shipment identity", "#4b70b2", [
            ("First 6 cases", "synthetic-case-graph-live-2026-09-28.json"),
            ("Expanded 20 cases", "synthetic-case-graph-live-v2-2026-09-28.json")]),
        ("Email response + effects", "#b5673a", [
            ("Initial raw grader", "live-email-response-development-2026-09-28.json"),
            ("Before wording fix", "live-email-response-current-2026-09-29.json"),
            ("Final full run", "live-email-response-final-2026-09-29.json")]),
        ("Recall interpretation", "#348877", [
            ("Initial", "live-recall-interpretation-development-2026-09-29.json"),
            ("After range guard", "live-recall-interpretation-after-guard-2026-09-29.json")]),
    ]
    p = [txt(34, 40, "Observed passes in full live batches", 24, "#142c3d", "bold"),
         txt(34, 65, "Bars show the recorded raw score in each run; repeated trials and changed graders limit causal comparisons.", 13)]
    y = 104
    for title, color, entries in groups:
        p.append(txt(34, y, title, 17, color, "bold"))
        y += 18
        for label, filename in entries:
            s = load(filename)["summary"]
            ratio = s["passed"] / s["total"]
            p += [txt(54, y + 17, label, 13), rect(260, y, 440, 23, "#e8edf0", 4),
                  rect(260, y, round(440 * ratio), 23, color, 4),
                  txt(714, y + 17, f'{s["passed"]}/{s["total"]}  ({ratio:.0%})', 14,
                      "#142c3d", "bold")]
            y += 35
        y += 20
    p += [line(34, y + 2, 870, y + 2),
          txt(34, y + 25, "Initial email score used an earlier grader; the final run uses tightened reply wording labels.", 12)]
    return svg(900, y + 45, "Live evaluation pass counts by path and batch",
               "Identity first 17 of 18 and expanded 56 of 60; email initial 18 of 30, before wording fix 24 of 30, final 29 of 30; recall initial 25 of 30 and after guard 29 of 30.", p)


def costs():
    p = [txt(34, 40, "Provider-reported cost by live run", 24, "#142c3d", "bold"),
         txt(34, 65, "USD per batch; bars use a linear scale. Values include all recorded responses in each batch.", 13)]
    scale = 460 / max(load(name)["summary"]["provider_cost_usd"] for _, name, _ in RUN_LABELS)
    for i, (label, name, color) in enumerate(RUN_LABELS):
        y = 97 + i * 33
        cost = load(name)["summary"]["provider_cost_usd"]
        p += [txt(34, y + 16, label, 12), rect(245, y, 460, 21, "#edf1f3", 3),
              rect(245, y, max(3, round(cost * scale)), 21, color, 3),
              txt(718, y + 16, f"${cost:.9f}", 12, "#142c3d", "bold")]
    p += [line(34, 503, 870, 503),
          txt(34, 530, "Total across 12 recorded live batches: $0.196251908", 16, "#142c3d", "bold")]
    return svg(900, 550, "Observed provider cost by live evaluation batch",
               "Twelve batches totaled 0.196251908 US dollars in provider-reported usage cost, excluding a separate connectivity preflight and unreported billing.", p)


def latency():
    entries = [
        ("Identity, 20 cases", "synthetic-case-graph-live-v2-2026-09-28.json", "#4b70b2"),
        ("Email, initial", "live-email-response-development-2026-09-28.json", "#b5673a"),
        ("Email, before wording", "live-email-response-current-2026-09-29.json", "#b5673a"),
        ("Email, final", "live-email-response-final-2026-09-29.json", "#b5673a"),
        ("Recall, initial", "live-recall-interpretation-development-2026-09-29.json", "#348877"),
        ("Recall, after guard", "live-recall-interpretation-after-guard-2026-09-29.json", "#348877"),
    ]
    left, right = 270, 730
    def position(seconds):
        return left + (math.log10(max(1, seconds)) / math.log10(300)) * (right - left)
    p = [txt(34, 40, "Agent wall latency", 24, "#142c3d", "bold"),
         txt(34, 65, "Seconds on a logarithmic axis; circle = p50, diamond = p95.", 13)]
    for tick in (1, 3, 10, 30, 100, 300):
        x = round(position(tick), 1)
        p += [line(x, 90, x, 365, "#e4e9ec"), txt(x, 387, str(tick), 12, anchor="middle")]
    p.append(txt(500, 412, "Seconds", 12, anchor="middle"))
    for i, (label, name, color) in enumerate(entries):
        s = load(name)["summary"]
        p50 = s.get("p50_agent_latency_ms", s.get("p50_latency_ms")) / 1000
        p95 = s.get("p95_agent_latency_ms", s.get("p95_latency_ms")) / 1000
        y = 118 + i * 45
        x1, x2 = round(position(p50), 1), round(position(p95), 1)
        p += [txt(34, y + 5, label, 13), line(x1, y, x2, y, color, 3),
              f'<circle cx="{x1}" cy="{y}" r="6" fill="{color}"/>',
              f'<polygon points="{x2},{y-7} {x2+7},{y} {x2},{y+7} {x2-7},{y}" fill="{color}"/>',
              txt(758, y + 5, f"{p50:.1f} / {p95:.1f}s", 12)]
    p += [rect(34, 438, 832, 47, "#fff2e8", 5),
          txt(48, 458, "Tail event:", 13, "#8b4a22", "bold"),
          txt(130, 458, "one final email trial took 375.1s, beyond the plotted p95.", 13),
          txt(48, 477, "Two successful provider responses took 175.3s and 199.7s.", 12)]
    return svg(900, 505, "Agent latency p50 and p95 by selected full batches",
               "The final email batch median was 9.5 seconds, p95 36.5 seconds; one correct trial took 375.1 seconds. The initial email p95 was 211.5 seconds.", p)


def main():
    FIGURES.mkdir(parents=True, exist_ok=True)
    figures = {"coverage.svg": coverage(), "pass-progression.svg": progression(),
               "cost-by-run.svg": costs(), "latency.svg": latency()}
    for name, content in figures.items():
        (FIGURES / name).write_text(content)
    print("Wrote", ", ".join(figures))


if __name__ == "__main__":
    main()
