"""Tests for the VibeCheck agent, its guardrails, and the Claude tool-use loop (with a fake client)."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agent import MusicAgent, RuleBasedPlanner
from src.guardrails import (
    MAX_REQUEST_CHARS,
    GuardrailError,
    sanitize_profile,
    validate_request,
    verify_recommendations,
)
from src.recommender import load_songs

SONGS = load_songs(str(Path(__file__).resolve().parent.parent / "data" / "songs.csv"))
BY_ID = {song["id"]: song for song in SONGS}


def offline_agent() -> MusicAgent:
    return MusicAgent(SONGS, use_claude=False)


# --- Input guardrails -------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "   ", None, 42, "x" * (MAX_REQUEST_CHARS + 1)],
                         ids=["empty", "blank", "none", "number", "too-long"])
def test_validate_request_rejects_unusable_input(bad):
    with pytest.raises(GuardrailError):
        validate_request(bad)


def test_agent_rejects_empty_request_without_recommending():
    result = offline_agent().run("   ")
    assert result.recommendations == []
    assert result.needs_review
    assert result.steps[0]["kind"] == "guardrail"


# --- Profile guardrails -----------------------------------------------------

def test_sanitize_profile_clamps_numbers_and_drops_unknown_genre():
    profile, warnings = sanitize_profile(
        {"favorite_genre": "k-pop", "target_energy": 1.7, "target_valence": -0.2, "bogus": 1}, SONGS)
    assert profile == {"target_energy": 1.0, "target_valence": 0.0}
    assert any("k-pop" in w for w in warnings)
    assert any("bogus" in w for w in warnings)


# --- Output verification ----------------------------------------------------

def test_verify_flags_hallucinated_duplicate_and_ruled_out_songs():
    sad_id = next(s["id"] for s in SONGS if s["mood"] == "sad")
    issues = verify_recommendations(
        [999, sad_id, sad_id], {"avoid_moods": ["sad"], "avoid_genres": [], "energy_range": None}, SONGS, k=3)
    problems = " | ".join(issue["problem"] for issue in issues)
    assert "not in the catalog" in problems
    assert "duplicate" in problems
    assert "ruled out" in problems


# --- Rule-based planner + agent loop ---------------------------------------

def test_planner_reads_negation_as_constraint_not_preference():
    profile, constraints, _ = RuleBasedPlanner(SONGS).plan("chill music for studying, nothing sad")
    assert profile.get("favorite_mood") == "chill"
    assert "sad" in constraints["avoid_moods"]
    assert "melancholic" in constraints["avoid_moods"]


def test_agent_revises_until_constraints_pass():
    result = offline_agent().run("chill music for studying, nothing sad")
    assert len(result.recommendations) == 5
    assert all(song["mood"] not in ("sad", "melancholic") for song in result.recommendations)
    assert result.issues == []
    assert any(step["kind"] == "revise" for step in result.steps), "expected the agent to replace failing songs"


def test_agent_respects_genre_exclusion():
    result = offline_agent().run("high energy songs for the gym, no metal")
    assert all(song["genre"] != "metal" for song in result.recommendations)
    assert all(song["energy"] >= 0.55 for song in result.recommendations)


def test_agent_flags_genre_the_catalog_lacks_for_human_review():
    result = offline_agent().run("k-pop music for dancing")
    assert "favorite_genre" not in result.profile  # k-pop must not silently become pop
    assert result.needs_review


def test_every_recommendation_is_a_real_catalog_song():
    for request in ["happy acoustic songs", "dark moody night drive", "anything really"]:
        for song in offline_agent().run(request).recommendations:
            assert BY_ID[song["id"]]["title"] == song["title"]


# --- Claude planner (fake client: no network, no API key) ------------------

def tool_use(call_id, name, args):
    return SimpleNamespace(type="tool_use", id=call_id, name=name, input=args)


class FakeClaude:
    """Plays back a scripted tool-use conversation, reading real tool results from the agent."""

    def __init__(self):
        self.calls = 0
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls += 1
        messages = kwargs["messages"]
        if self.calls == 1:
            block = tool_use("t1", "recommend_songs",
                             {"profile": {"favorite_genre": "lofi", "favorite_mood": "chill", "target_energy": 0.4}})
        else:
            last_results = messages[-1]["content"]
            if self.calls == 2:
                ids = [s["id"] for s in json.loads(last_results[0]["content"])["songs"]]
                self.ids = ids
                # First submission sneaks in a made-up song id; the agent must reject it.
                block = tool_use("t2", "submit_recommendations", {
                    "song_ids": [999] + ids[:4], "profile": {"favorite_genre": "lofi"},
                    "constraints": {}, "summary": "draft"})
            else:
                assert last_results[0]["is_error"] is True
                block = tool_use("t3", "submit_recommendations", {
                    "song_ids": self.ids, "profile": {"favorite_genre": "lofi", "favorite_mood": "chill",
                                                      "target_energy": 0.4},
                    "constraints": {}, "summary": "Calm lofi picks."})
        return SimpleNamespace(content=[block], stop_reason="tool_use")


def test_claude_loop_rejects_hallucinated_song_then_accepts_valid_submission():
    fake = FakeClaude()
    result = MusicAgent(SONGS, client=fake).run("lofi to study to")
    assert result.planner == "claude"
    assert fake.calls == 3
    assert result.summary == "Calm lofi picks."
    assert 999 not in [song["id"] for song in result.recommendations]
    assert result.issues == []


def test_claude_failure_falls_back_to_rule_based_planner():
    def explode(**kwargs):
        raise RuntimeError("network down")

    broken = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=explode)))
    result = MusicAgent(SONGS, client=broken).run("chill music for studying")
    assert result.planner == "rule-based"
    assert len(result.recommendations) == 5
    assert any(step["kind"] == "error" for step in result.steps)


# --- Negation phrasings found by the evaluation harness ---------------------

@pytest.mark.parametrize("request_text, avoided_genres, avoided_moods", [
    ("anything but country, please", ["country"], []),
    ("I don't want anything too intense, just relaxing jazz", [], ["intense", "angry"]),
    ("party music other than edm", ["edm"], []),
    ("rock but not too angry", [], ["angry"]),
])
def test_planner_handles_negation_phrasings(request_text, avoided_genres, avoided_moods):
    _, constraints, _ = RuleBasedPlanner(SONGS).plan(request_text)
    for genre in avoided_genres:
        assert genre in constraints["avoid_genres"]
    for mood in avoided_moods:
        assert mood in constraints["avoid_moods"]


def test_plain_but_is_not_treated_as_negation():
    profile, constraints, _ = RuleBasedPlanner(SONGS).plan("chill but upbeat songs")
    assert constraints["avoid_moods"] == []
    assert profile["target_valence"] == 0.8  # "upbeat" is still a wish


# --- Confidence scoring -----------------------------------------------------

def test_confidence_is_full_for_perfect_matches_and_drops_with_problems():
    from src.agent import confidence_score
    profile = {"target_energy": 0.5}
    perfect = [{"score": 1.5}] * 5
    assert confidence_score(profile, perfect, [], []) == 1.0
    assert confidence_score(profile, perfect, [{"song_id": 1, "problem": "x"}], []) == 0.5
    assert confidence_score(profile, perfect, [], ["dropped genre"]) == 0.8
    assert confidence_score(profile, [], [], []) == 0.0


def test_low_confidence_is_flagged_for_human_review():
    # Only one r&b song exists, so four of the five picks are weak matches.
    result = offline_agent().run("romantic r&b for a date night")
    assert result.recommendations[0]["genre"] == "r&b"
    assert result.confidence < 0.5
    assert result.needs_review


# --- Evaluation harness as a regression test --------------------------------

def test_evaluation_suite_passes():
    from src.evaluate import run_evaluation, summarize
    rows = run_evaluation(offline_agent())
    failed = [(row["input"], [c for c in row["criteria"] if not c["passed"]]) for row in rows if not row["passed"]]
    assert not failed, failed
    assert summarize(rows)["passed"] == len(rows)
