"""
VibeCheck agent: turns a plain-English music request into verified recommendations.

The agent follows a plan -> act -> check -> revise loop:
  PLAN    Turn the request into a taste profile plus constraints ("nothing sad" -> avoid sad moods).
  ACT     Call the original VibeCheck scoring engine (src/recommender.py) as a tool.
  CHECK   Verify the top songs against the catalog and the constraints (src/guardrails.py).
  REVISE  Exclude songs that failed the check and ask the engine again, up to a round limit.

Two planners can drive this loop:
  * ClaudePlanner uses the Claude API with tool use. Claude decides what to call and when.
  * RuleBasedPlanner is a deterministic, offline fallback, used when no API key is set or the
    API call fails. It runs the same loop with keyword parsing in place of the model.

Whichever planner runs, the final answer is re-verified by code before it is returned, and
every step is written to the log.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from src.guardrails import (
    GuardrailError,
    catalog_vocabulary,
    empty_constraints,
    sanitize_constraints,
    sanitize_profile,
    validate_request,
    verify_recommendations,
)
from src.recommender import recommend_songs

logger = logging.getLogger("vibecheck.agent")

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_K = 5
MAX_REVISION_ROUNDS = 3
MAX_CLAUDE_TURNS = 10


@dataclass
class AgentResult:
    """Everything the agent decided, plus the trail of how it got there."""

    request: str
    planner: str
    profile: Dict
    constraints: Dict
    recommendations: List[Dict]
    summary: str
    steps: List[Dict] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    issues: List[Dict] = field(default_factory=list)
    needs_review: bool = False


# ---------------------------------------------------------------------------
# Tools: the actions the agent can take. Both planners use these same functions.
# ---------------------------------------------------------------------------

class CatalogTools:
    """Wraps the original recommender so the agent can call it as a tool."""

    def __init__(self, songs: List[Dict], k: int = DEFAULT_K):
        self.songs = songs
        self.k = k
        self.by_id = {song["id"]: song for song in songs}
        self.seen_ids: Set[int] = set()  # every id a recommend call returned (used for grounding)

    def catalog_options(self) -> Dict:
        genres, moods = catalog_vocabulary(self.songs)
        return {"genres": genres, "moods": moods, "song_count": len(self.songs)}

    def recommend(self, profile: Dict, exclude_ids: Optional[List[int]] = None) -> List[Dict]:
        """Score the catalog with the original engine, skipping excluded ids, and return the top k."""
        excluded = set(exclude_ids or [])
        candidates = [song for song in self.songs if song["id"] not in excluded]
        ranked = recommend_songs(profile, candidates, k=self.k)
        results = []
        for song, score, reasons in ranked:
            self.seen_ids.add(song["id"])
            results.append({
                "id": song["id"],
                "title": song["title"],
                "artist": song["artist"],
                "genre": song["genre"],
                "mood": song["mood"],
                "energy": song["energy"],
                "valence": song["valence"],
                "score": round(score, 2),
                "reasons": reasons.split("; ") if reasons else [],
            })
        return results

    def check(self, song_ids: List[int], constraints: Dict) -> List[Dict]:
        return verify_recommendations(song_ids, constraints, self.songs, self.k)

    def details(self, song_ids: List[int], profile: Dict) -> List[Dict]:
        """Rebuild full recommendation rows (with scores and reasons) for a final list of ids."""
        rows = []
        for song_id in song_ids:
            song = self.by_id[song_id]
            ranked = recommend_songs(profile, [song], k=1)
            _, score, reasons = ranked[0]
            rows.append({**{k: song[k] for k in ("id", "title", "artist", "genre", "mood", "energy", "valence")},
                         "score": round(score, 2), "reasons": reasons.split("; ") if reasons else []})
        return rows


# ---------------------------------------------------------------------------
# Offline planner: keyword parsing, used when Claude is unavailable.
# ---------------------------------------------------------------------------

# Words in a request -> the catalog mood they point to.
MOOD_WORDS = {
    "happy": "happy", "upbeat": "happy", "cheerful": "happy", "feel-good": "happy", "feel good": "happy",
    "sad": "sad", "heartbroken": "sad", "crying": "sad", "depressing": "sad",
    "melancholic": "melancholic", "melancholy": "melancholic",
    "chill": "chill", "mellow": "chill", "laid-back": "chill", "laid back": "chill",
    "relax": "relaxed", "relaxed": "relaxed", "relaxing": "relaxed", "unwind": "relaxed",
    "calm": "peaceful", "peaceful": "peaceful", "sleep": "peaceful", "meditate": "peaceful",
    "focus": "focused", "focused": "focused", "study": "focused", "studying": "focused", "concentrate": "focused",
    "angry": "angry", "rage": "angry",
    "intense": "intense", "aggressive": "intense",
    "romantic": "romantic", "love": "romantic", "date": "romantic",
    "nostalgic": "nostalgic", "throwback": "nostalgic",
    "euphoric": "euphoric", "party": "euphoric", "rave": "euphoric",
    "confident": "confident", "hype": "confident",
    "carefree": "carefree", "summer": "carefree", "beach": "carefree",
    "moody": "moody", "dark": "moody",
    "passionate": "passionate",
}
# Moods that travel together when a listener rules one out ("nothing sad" also means no melancholic).
RELATED_MOODS = {"sad": ["melancholic"], "melancholic": ["sad"], "angry": ["intense"], "intense": ["angry"]}

LOW_ENERGY_WORDS = {"calm", "sleep", "study", "studying", "focus", "chill", "relax", "relaxing", "mellow",
                    "quiet", "soft", "peaceful", "meditate", "unwind"}
HIGH_ENERGY_WORDS = {"workout", "gym", "run", "running", "party", "hype", "energetic", "intense", "pump",
                     "dance", "dancing", "rave", "loud", "banger", "bangers"}
HAPPY_WORDS = {"happy", "upbeat", "cheerful", "feel-good", "summer", "carefree", "party"}
DARK_WORDS = {"sad", "dark", "moody", "heartbroken", "melancholic", "melancholy", "angry"}

FILLER_WORDS = {"some", "any", "good", "new", "more", "the", "my", "background", "nice", "great", "fun", "cool",
                 "acoustic", "electronic", "instrumental", "loud", "soft", "quiet", "slow", "fast",
                 "energy", "high", "low", "workout", "gym", "study", "background", "road", "trip"}

GENRE_ALIASES = {"hip-hop": "hip hop", "hiphop": "hip hop", "rap": "hip hop", "rnb": "r&b",
                 "lo-fi": "lofi", "dance music": "edm", "indie": "indie pop"}

NEGATION = re.compile(r"\b(?:no|not|nothing|without|avoid|skip|except|hate|don't want|dont want)\s+"
                      r"(?:too\s+|any\s+|more\s+)?([a-z&-]+(?:\s[a-z&-]+)?)")


def _find(text: str, phrase: str) -> int:
    """Position of phrase as a whole word in text (hyphens count as part of a word, so 'k-pop' != 'pop'), or -1."""
    match = re.search(rf"(?<![a-z-]){re.escape(phrase)}(?![a-z-])", text)
    return match.start() if match else -1


def _contains(text: str, phrase: str) -> bool:
    return _find(text, phrase) >= 0


class RuleBasedPlanner:
    """Turns a request into (profile, constraints, notes) with keyword rules. Deterministic and offline."""

    def __init__(self, songs: List[Dict]):
        self.genres, self.moods = catalog_vocabulary(songs)

    def _genre_in(self, phrase: str) -> Optional[str]:
        for alias, genre in GENRE_ALIASES.items():
            if _contains(phrase, alias):
                return genre
        for genre in sorted(self.genres, key=len, reverse=True):
            if _contains(phrase, genre):
                return genre
        return None

    def _mood_in(self, phrase: str) -> Optional[str]:
        """The mood word that appears first wins ('chill music for studying' -> chill, not focused)."""
        hits = [(_find(phrase, word), -len(word), MOOD_WORDS[word]) for word in MOOD_WORDS]
        hits = [hit for hit in hits if hit[0] >= 0]
        return min(hits)[2] if hits else None

    def plan(self, request: str) -> Tuple[Dict, Dict, List[str]]:
        text = request.lower()
        constraints = empty_constraints()
        notes: List[str] = []

        # 1. Pull out negated phrases first so "nothing sad" is not read as "sad".
        for match in NEGATION.finditer(text):
            phrase = match.group(1)
            genre = self._genre_in(phrase)
            mood = self._mood_in(phrase)
            if genre:
                constraints["avoid_genres"].append(genre)
                notes.append(f"'{match.group(0)}' -> avoid genre {genre}")
            elif mood:
                constraints["avoid_moods"].extend([mood] + RELATED_MOODS.get(mood, []))
                notes.append(f"'{match.group(0)}' -> avoid mood {mood}")
            text = text.replace(match.group(0), " ")

        words = set(re.findall(r"[a-z&-]+", text))
        profile: Dict = {}

        # 2. Positive preferences from what is left.
        genre = self._genre_in(text)
        if genre:
            profile["favorite_genre"] = genre
        mood = self._mood_in(text)
        if mood:
            profile["favorite_mood"] = mood

        if words & HIGH_ENERGY_WORDS:
            profile["target_energy"] = 0.85
            constraints["energy_range"] = [0.65, 1.0]
        elif words & LOW_ENERGY_WORDS:
            profile["target_energy"] = 0.35
            constraints["energy_range"] = [0.15, 0.55]

        if words & HAPPY_WORDS:
            profile["target_valence"] = 0.80
        elif words & DARK_WORDS:
            profile["target_valence"] = 0.30

        if {"acoustic", "unplugged"} & words:
            profile["likes_acoustic"] = True
        elif {"electronic", "synth", "edm"} & words:
            profile["likes_acoustic"] = False

        # Unknown genre the listener named explicitly (e.g. "k-pop"): record it so the guardrail can flag it.
        named = re.search(r"\b([a-z&-]+)\s+(?:music|songs|tracks)\b", text)
        if not genre and named:
            word = named.group(1)
            descriptive = set(MOOD_WORDS) | LOW_ENERGY_WORDS | HIGH_ENERGY_WORDS | HAPPY_WORDS | DARK_WORDS
            if word not in descriptive and word not in FILLER_WORDS:
                profile["favorite_genre"] = word

        if constraints["avoid_moods"] and profile.get("favorite_mood") in constraints["avoid_moods"]:
            profile.pop("favorite_mood")

        notes.append(f"planned profile {profile}")
        return profile, constraints, notes


# ---------------------------------------------------------------------------
# Claude planner: the model drives the loop with tool use.
# ---------------------------------------------------------------------------

CLAUDE_SYSTEM = """You are the planning agent for VibeCheck, a music recommender with a small fixed catalog.

Your job: turn the listener's request into recommendations that the catalog can actually deliver, and check them before you answer.

Work in this order:
1. Call get_catalog_options to see which genres and moods exist. Only use those exact values.
2. Build a taste profile (favorite_genre, favorite_mood, target_energy 0-1, target_valence 0-1, likes_acoustic) and constraints (avoid_moods, avoid_genres, energy_range). Leave a field out when the request does not imply it. If the listener names a genre the catalog lacks, leave favorite_genre out and say so in your summary.
3. Call recommend_songs with the profile. It runs the scoring engine and returns the top 5 with scores and reasons.
4. Call check_recommendations with those song ids and your constraints. If it reports problems, call recommend_songs again with the failing ids in exclude_song_ids, then check again.
5. When the check passes (or you have tried 3 times), call submit_recommendations with the final 5 song ids, the profile, the constraints, and a short summary for the listener that explains the choices and mentions anything the catalog could not satisfy.

Only recommend song ids that recommend_songs returned. Never invent songs."""

PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "favorite_genre": {"type": "string"},
        "favorite_mood": {"type": "string"},
        "target_energy": {"type": "number"},
        "target_valence": {"type": "number"},
        "likes_acoustic": {"type": "boolean"},
    },
}
CONSTRAINTS_SCHEMA = {
    "type": "object",
    "properties": {
        "avoid_moods": {"type": "array", "items": {"type": "string"}},
        "avoid_genres": {"type": "array", "items": {"type": "string"}},
        "energy_range": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
    },
}
CLAUDE_TOOLS = [
    {
        "name": "get_catalog_options",
        "description": "List the genres and moods that exist in the song catalog.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "recommend_songs",
        "description": "Run the VibeCheck scoring engine on a taste profile and return the top 5 songs "
                       "with scores and the reasons behind each score.",
        "input_schema": {
            "type": "object",
            "properties": {
                "profile": PROFILE_SCHEMA,
                "exclude_song_ids": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["profile"],
        },
    },
    {
        "name": "check_recommendations",
        "description": "Verify song ids against the catalog and the listener's constraints. "
                       "Returns a list of problems; an empty list means the songs pass.",
        "input_schema": {
            "type": "object",
            "properties": {
                "song_ids": {"type": "array", "items": {"type": "integer"}},
                "constraints": CONSTRAINTS_SCHEMA,
            },
            "required": ["song_ids", "constraints"],
        },
    },
    {
        "name": "submit_recommendations",
        "description": "Submit the final 5 song ids with the profile, constraints, and a short listener-facing summary.",
        "input_schema": {
            "type": "object",
            "properties": {
                "song_ids": {"type": "array", "items": {"type": "integer"}},
                "profile": PROFILE_SCHEMA,
                "constraints": CONSTRAINTS_SCHEMA,
                "summary": {"type": "string"},
            },
            "required": ["song_ids", "profile", "constraints", "summary"],
        },
    },
]


class PlannerUnavailable(RuntimeError):
    """Raised when the Claude planner cannot finish, so the agent should fall back."""


class ClaudePlanner:
    """Runs a manual tool-use loop with Claude. Code validates every tool call before acting on it."""

    def __init__(self, client=None, model: Optional[str] = None):
        if client is None:
            import anthropic  # imported here so the offline path never needs the package
            client = anthropic.Anthropic()
        self.client = client
        self.model = model or os.environ.get("VIBECHECK_MODEL", DEFAULT_MODEL)

    def run(self, request: str, tools: CatalogTools, record) -> Tuple[List[int], Dict, Dict, str]:
        import anthropic

        messages: List[Dict] = [{"role": "user", "content": request}]
        for turn in range(1, MAX_CLAUDE_TURNS + 1):
            try:
                response = self.client.beta.messages.create(
                    model=self.model,
                    max_tokens=16000,
                    system=CLAUDE_SYSTEM,
                    tools=CLAUDE_TOOLS,
                    messages=messages,
                    output_config={"effort": "medium"},
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
            except anthropic.APIConnectionError as exc:
                raise PlannerUnavailable(f"could not reach the Claude API: {exc}") from exc
            except anthropic.APIStatusError as exc:
                raise PlannerUnavailable(f"Claude API error {exc.status_code}: {exc.message}") from exc

            if response.stop_reason == "refusal":
                raise PlannerUnavailable("Claude declined the request")
            # Keep the full content (including thinking blocks) so the conversation stays valid.
            messages.append({"role": "assistant", "content": response.content})

            tool_uses = [block for block in response.content if block.type == "tool_use"]
            if not tool_uses:
                raise PlannerUnavailable(f"Claude stopped without submitting (stop_reason={response.stop_reason})")

            results = []
            submitted = None
            for call in tool_uses:
                output, is_error, final = self._run_tool(call.name, call.input, tools, record, turn)
                results.append({"type": "tool_result", "tool_use_id": call.id,
                                "content": json.dumps(output), "is_error": is_error})
                if final is not None:
                    submitted = final
            if submitted is not None:
                return submitted
            messages.append({"role": "user", "content": results})

        raise PlannerUnavailable(f"no valid submission after {MAX_CLAUDE_TURNS} turns")

    def _run_tool(self, name: str, raw_input: Dict, tools: CatalogTools, record, turn: int):
        """Execute one tool call. Returns (output, is_error, final_submission_or_None)."""
        args = raw_input if isinstance(raw_input, dict) else {}
        if name == "get_catalog_options":
            output = tools.catalog_options()
            record("act", f"Claude looked up catalog options (turn {turn})", output)
            return output, False, None

        if name == "recommend_songs":
            profile, warnings = sanitize_profile(args.get("profile") or {}, tools.songs)
            exclude = [int(x) for x in args.get("exclude_song_ids") or [] if str(x).lstrip("-").isdigit()]
            output = {"songs": tools.recommend(profile, exclude), "profile_used": profile, "warnings": warnings}
            record("act", f"Claude called the scoring engine (turn {turn})",
                   {"profile": profile, "excluded": exclude, "returned": [s["id"] for s in output["songs"]],
                    "warnings": warnings})
            return output, False, None

        if name == "check_recommendations":
            constraints = sanitize_constraints(args.get("constraints"))
            ids = [int(x) for x in args.get("song_ids") or []]
            issues = tools.check(ids, constraints)
            record("check", f"Claude checked its picks (turn {turn})",
                   {"song_ids": ids, "constraints": constraints, "issues": issues})
            return {"issues": issues, "passed": not issues}, False, None

        if name == "submit_recommendations":
            ids = [int(x) for x in args.get("song_ids") or []]
            profile, _ = sanitize_profile(args.get("profile") or {}, tools.songs)
            constraints = sanitize_constraints(args.get("constraints"))
            ungrounded = [i for i in ids if i not in tools.seen_ids]
            issues = tools.check(ids, constraints)
            if ungrounded:
                issues.append({"song_id": None,
                               "problem": f"ids {ungrounded} never came from recommend_songs"})
            record("check", f"Code verified Claude's submission (turn {turn})", {"song_ids": ids, "issues": issues})
            if issues:
                return {"accepted": False, "issues": issues}, True, None
            return {"accepted": True}, False, (ids, profile, constraints, str(args.get("summary", "")).strip())

        record("error", f"Claude called an unknown tool '{name}'", {})
        return {"error": f"unknown tool {name}"}, True, None


# ---------------------------------------------------------------------------
# The agent: orchestrates planner + tools + guardrails + logging.
# ---------------------------------------------------------------------------

class MusicAgent:
    """Orchestrates one request end to end and returns an AgentResult with the full step trace."""

    def __init__(self, songs: List[Dict], use_claude: Optional[bool] = None, client=None,
                 k: int = DEFAULT_K, model: Optional[str] = None):
        self.songs = songs
        self.k = k
        self.client = client
        self.model = model
        if use_claude is None:
            use_claude = client is not None or bool(os.environ.get("ANTHROPIC_API_KEY"))
        self.use_claude = use_claude

    def run(self, request: str) -> AgentResult:
        steps: List[Dict] = []

        def record(kind: str, message: str, data: Optional[Dict] = None) -> None:
            step = {"step": len(steps) + 1, "kind": kind, "message": message, "data": data or {}}
            steps.append(step)
            logger.info("%s | %s | %s", kind.upper(), message, json.dumps(data or {}, default=str))

        # Input guardrail: refuse before any planning happens.
        try:
            request = validate_request(request)
        except GuardrailError as exc:
            record("guardrail", f"request rejected: {exc}")
            return AgentResult(request=str(request), planner="none", profile={}, constraints=empty_constraints(),
                               recommendations=[], summary=f"Request rejected: {exc}", steps=steps,
                               warnings=[str(exc)], needs_review=True)
        record("input", "request accepted", {"request": request})

        tools = CatalogTools(self.songs, self.k)

        if self.use_claude:
            try:
                planner = ClaudePlanner(self.client, self.model)
                ids, profile, constraints, summary = planner.run(request, tools, record)
                return self._finish(request, "claude", ids, profile, constraints, summary, [], steps, tools, record)
            except PlannerUnavailable as exc:
                record("error", f"Claude planner failed, falling back to rule-based planner: {exc}")
                logger.warning("Claude planner failed: %s", exc)
            except Exception as exc:  # missing package, bad key format, etc. Never crash the app.
                record("error", f"Claude planner could not start, falling back: {type(exc).__name__}: {exc}")
                logger.warning("Claude planner could not start: %s", exc)
            tools = CatalogTools(self.songs, self.k)

        return self._run_rule_based(request, tools, steps, record)

    def _run_rule_based(self, request: str, tools: CatalogTools, steps: List[Dict], record) -> AgentResult:
        raw_profile, raw_constraints, notes = RuleBasedPlanner(self.songs).plan(request)
        profile, warnings = sanitize_profile(raw_profile, self.songs)
        constraints = sanitize_constraints(raw_constraints)
        record("plan", "rule-based planner built a profile",
               {"notes": notes, "profile": profile, "constraints": constraints, "warnings": warnings})

        if not profile:
            warnings.append("no clear music preferences found in the request; showing balanced picks")
            profile = {"target_energy": 0.5, "target_valence": 0.5}

        excluded: List[int] = []
        ids: List[int] = []
        for round_number in range(1, MAX_REVISION_ROUNDS + 1):
            picks = tools.recommend(profile, excluded)
            ids = [song["id"] for song in picks]
            record("act", f"called the scoring engine (round {round_number})",
                   {"excluded": list(excluded), "returned": ids})
            issues = tools.check(ids, constraints)
            record("check", f"verified round {round_number}", {"issues": issues})
            failing = [issue["song_id"] for issue in issues if issue["song_id"] is not None]
            if not issues or not failing:
                break
            excluded.extend(failing)
            record("revise", f"excluding {len(failing)} song(s) that broke a constraint", {"excluded": failing})

        summary = self._template_summary(profile, constraints, warnings, len(excluded))
        return self._finish(request, "rule-based", ids, profile, constraints, summary, warnings, steps, tools, record)

    def _finish(self, request, planner, ids, profile, constraints, summary, warnings, steps, tools, record):
        # Final verification runs in code no matter which planner produced the answer.
        issues = tools.check(ids, constraints)
        # Human-in-the-loop: flag runs where the catalog could not deliver what was asked.
        needs_review = bool(issues) or any(
            "no clear music preferences" in w or "not a genre in the catalog" in w for w in warnings)
        record("verify", "final verification", {"issues": issues, "needs_review": needs_review})
        real_ids = [song_id for song_id in ids if song_id in tools.by_id]  # never display a made-up song
        return AgentResult(request=request, planner=planner, profile=profile, constraints=constraints,
                           recommendations=tools.details(real_ids, profile),
                           summary=summary, steps=steps, warnings=warnings, issues=issues,
                           needs_review=needs_review)

    @staticmethod
    def _template_summary(profile: Dict, constraints: Dict, warnings: List[str], replaced: int) -> str:
        parts = []
        if profile.get("favorite_genre"):
            parts.append(f"{profile['favorite_genre']} songs")
        if profile.get("favorite_mood"):
            parts.append(f"a {profile['favorite_mood']} mood")
        if "target_energy" in profile:
            level = "low" if profile["target_energy"] < 0.45 else "high" if profile["target_energy"] > 0.7 else "medium"
            parts.append(f"{level} energy")
        text = "Looked for " + (", ".join(parts) if parts else "a balanced mix") + "."
        avoided = constraints["avoid_moods"] + constraints["avoid_genres"]
        if avoided:
            text += f" Ruled out: {', '.join(avoided)}."
        if replaced:
            text += f" Replaced {replaced} song(s) that broke your constraints."
        for warning in warnings:
            text += f" Note: {warning}."
        return text
