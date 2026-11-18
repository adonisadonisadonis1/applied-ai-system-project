"""
Reliability evaluation for the VibeCheck agent.

Runs a fixed set of labeled requests (typical, adversarial, and malformed) through the full agent
and checks each result against written criteria. Prints a markdown table plus a summary line, and
can save the same results as JSON.

Run from the project root:
    python -m src.evaluate                      # rule-based planner (deterministic, no API key)
    python -m src.evaluate --json eval.json     # also save structured results
    python -m src.evaluate --planner auto       # use Claude if ANTHROPIC_API_KEY is set
"""

import argparse
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Tuple

from src.agent import AgentResult, MusicAgent
from src.recommender import load_songs

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "songs.csv"

# A criterion is (description, check). check(result) returns (passed, detail).
Criterion = Tuple[str, Callable[[AgentResult], Tuple[bool, str]]]


# --- Criterion builders -----------------------------------------------------

def rejected() -> Criterion:
    return ("rejected before planning, no songs",
            lambda r: (r.planner == "none" and not r.recommendations, f"planner={r.planner}"))


def five_songs() -> Criterion:
    return ("returns 5 songs", lambda r: (len(r.recommendations) == 5, f"{len(r.recommendations)} songs"))


def flagged(expected: bool = True) -> Criterion:
    label = "flagged for human review" if expected else "not flagged for review"
    return (label, lambda r: (r.needs_review == expected, f"needs_review={r.needs_review}"))


def no_mood(*moods: str) -> Criterion:
    def check(r):
        bad = [s["title"] for s in r.recommendations if s["mood"] in moods]
        return not bad, f"ruled-out mood in {bad}" if bad else "ok"
    return (f"no {'/'.join(moods)} songs", check)


def no_genre(*genres: str) -> Criterion:
    def check(r):
        bad = [s["title"] for s in r.recommendations if s["genre"] in genres]
        return not bad, f"ruled-out genre in {bad}" if bad else "ok"
    return (f"no {'/'.join(genres)} songs", check)


def energy_between(low: float, high: float) -> Criterion:
    def check(r):
        bad = [f"{s['title']} ({s['energy']:.2f})" for s in r.recommendations if not low <= s["energy"] <= high]
        return not bad, f"energy out of range: {bad}" if bad else "ok"
    return (f"all energy {low}-{high}", check)


def top_genre(genre: str) -> Criterion:
    def check(r):
        top = r.recommendations[0]["genre"] if r.recommendations else None
        return top == genre, f"top genre={top}"
    return (f"#1 is {genre}", check)


def top_mood(*moods: str) -> Criterion:
    def check(r):
        top = r.recommendations[0]["mood"] if r.recommendations else None
        return top in moods, f"top mood={top}"
    return (f"#1 mood is {'/'.join(moods)}", check)


def avg_valence(at_least: float = 0.0, at_most: float = 1.0) -> Criterion:
    def check(r):
        if not r.recommendations:
            return False, "no songs"
        avg = sum(s["valence"] for s in r.recommendations) / len(r.recommendations)
        return at_least <= avg <= at_most, f"avg valence={avg:.2f}"
    label = f"avg valence >= {at_least}" if at_least > 0 else f"avg valence <= {at_most}"
    return (label, check)


def profile_lacks_genre(genre: str) -> Criterion:
    return (f"'{genre}' not used as the genre",
            lambda r: (r.profile.get("favorite_genre") != genre, f"favorite_genre={r.profile.get('favorite_genre')}"))


# --- The evaluation set -----------------------------------------------------

@dataclass
class Case:
    request: object
    kind: str
    criteria: List[Criterion]

    @property
    def label(self) -> str:
        if not isinstance(self.request, str):
            return repr(self.request)
        text = self.request if len(self.request) <= 60 else f"{self.request[:20]}... ({len(self.request)} chars)"
        return f'"{text}"' if text.strip() else "(empty)"


CASES: List[Case] = [
    Case("chill music for studying, nothing sad", "typical",
         [five_songs(), no_mood("sad", "melancholic"), energy_between(0.0, 0.65), top_mood("chill")]),
    Case("high energy songs for the gym, no metal", "typical",
         [five_songs(), no_genre("metal"), energy_between(0.55, 1.0)]),
    Case("happy acoustic songs for a summer road trip", "typical",
         [five_songs(), top_mood("happy"), avg_valence(at_least=0.6)]),
    Case("sad songs for a rainy day", "typical",
         [five_songs(), top_mood("sad", "melancholic"), avg_valence(at_most=0.5)]),
    Case("romantic r&b for a date night", "typical",
         [five_songs(), top_genre("r&b")]),
    Case("angry metal for lifting weights", "typical",
         [five_songs(), top_genre("metal")]),
    Case("anything but country, please", "adversarial: negation phrasing",
         [five_songs(), no_genre("country")]),
    Case("I don't want anything too intense, just relaxing jazz", "adversarial: negation phrasing",
         [five_songs(), no_mood("intense", "angry"), top_genre("jazz")]),
    Case("no rock, no pop, something electronic and moody", "adversarial: multiple exclusions",
         [five_songs(), no_genre("rock", "pop"), top_genre("electronic")]),
    Case("k-pop music for dancing", "adversarial: genre not in catalog",
         [five_songs(), profile_lacks_genre("pop"), flagged(True)]),
    Case("music", "adversarial: no preferences",
         [five_songs(), flagged(True)]),
    Case("", "malformed: empty", [rejected(), flagged(True)]),
    Case("x" * 600, "malformed: too long", [rejected(), flagged(True)]),
]


# --- Runner -----------------------------------------------------------------

def run_evaluation(agent: MusicAgent, cases: List[Case] = CASES) -> List[Dict]:
    rows = []
    for number, case in enumerate(cases, start=1):
        result = agent.run(case.request)
        checks = []
        for description, check in case.criteria:
            passed, detail = check(result)
            checks.append({"criterion": description, "passed": passed, "detail": detail})
        rows.append({
            "id": number,
            "input": case.label,
            "kind": case.kind,
            "planner": result.planner,
            "criteria": checks,
            "passed": all(c["passed"] for c in checks),
            "confidence": result.confidence if result.recommendations else None,
            "needs_review": result.needs_review,
            "revisions": sum(1 for step in result.steps if step["kind"] == "revise"),
        })
    return rows


def summarize(rows: List[Dict]) -> Dict:
    confidences = [row["confidence"] for row in rows if row["confidence"] is not None]
    return {
        "cases": len(rows),
        "passed": sum(row["passed"] for row in rows),
        "criteria": sum(len(row["criteria"]) for row in rows),
        "criteria_passed": sum(c["passed"] for row in rows for c in row["criteria"]),
        "average_confidence": round(sum(confidences) / len(confidences), 2) if confidences else None,
        "flagged_for_review": sum(row["needs_review"] for row in rows),
        "revision_rounds": sum(row["revisions"] for row in rows),
    }


def to_markdown(rows: List[Dict], summary: Dict) -> str:
    lines = [
        "| # | Test input | Type | Evaluation criteria | Result | Confidence |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        criteria = "; ".join(c["criterion"] for c in row["criteria"])
        failures = [f"{c['criterion']} ({c['detail']})" for c in row["criteria"] if not c["passed"]]
        result = "Pass" if row["passed"] else "Fail: " + "; ".join(failures)
        confidence = "n/a" if row["confidence"] is None else f"{row['confidence']:.2f}"
        lines.append(f"| {row['id']} | {row['input']} | {row['kind']} | {criteria} | {result} | {confidence} |")
    lines.append("")
    lines.append(
        f"**{summary['passed']} of {summary['cases']} cases passed** "
        f"({summary['criteria_passed']} of {summary['criteria']} criteria). "
        f"Average confidence {summary['average_confidence']}; "
        f"{summary['flagged_for_review']} runs flagged for human review; "
        f"{summary['revision_rounds']} revision rounds used.")
    return "\n".join(lines)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Evaluate the VibeCheck agent on labeled requests.")
    parser.add_argument("--json", metavar="PATH", help="also write structured results to this JSON file")
    parser.add_argument("--planner", choices=["rules", "auto"], default="rules",
                        help="'rules' (default, deterministic) or 'auto' (Claude if ANTHROPIC_API_KEY is set)")
    args = parser.parse_args(argv)

    logging.getLogger("vibecheck").addHandler(logging.NullHandler())
    agent = MusicAgent(load_songs(str(DATA_PATH)), use_claude=False if args.planner == "rules" else None)
    rows = run_evaluation(agent)
    summary = summarize(rows)
    print(to_markdown(rows, summary))
    if args.json:
        Path(args.json).write_text(json.dumps({"summary": summary, "results": rows}, indent=2))
        print(f"\nSaved structured results to {args.json}")


if __name__ == "__main__":
    main()
