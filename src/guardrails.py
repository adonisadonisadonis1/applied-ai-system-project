"""
Guardrails for the VibeCheck agent.

Three kinds of checks live here:
  1. Input guardrails: reject empty, oversized, or non-text requests before any AI runs.
  2. Profile guardrails: clamp numbers into range and flag genres/moods the catalog does not have.
  3. Output verification: confirm every recommended song is real, unique, and obeys the
     listener's constraints ("nothing sad", "no metal", "low energy").

Both planners (Claude and the offline rule-based one) go through the same checks, so the
system's safety does not depend on which planner ran.
"""

from typing import Dict, List, Optional, Tuple

MAX_REQUEST_CHARS = 500
ENERGY_TOLERANCE = 0.10

PROFILE_NUMERIC_KEYS = ("target_energy", "target_valence")
PROFILE_KEYS = ("favorite_genre", "favorite_mood", "target_energy", "target_valence", "likes_acoustic")


class GuardrailError(ValueError):
    """Raised when a request is unsafe or unusable and the agent must not run."""


def validate_request(request: object) -> str:
    """Return a cleaned request string, or raise GuardrailError explaining why it was rejected."""
    if not isinstance(request, str):
        raise GuardrailError("Request must be text.")
    cleaned = " ".join(request.split())
    if not cleaned:
        raise GuardrailError("Request is empty. Describe the music you want, e.g. 'calm music for studying'.")
    if len(cleaned) > MAX_REQUEST_CHARS:
        raise GuardrailError(f"Request is {len(cleaned)} characters; the limit is {MAX_REQUEST_CHARS}.")
    return cleaned


def catalog_vocabulary(songs: List[Dict]) -> Tuple[List[str], List[str]]:
    """Return the sorted genres and moods that actually exist in the catalog."""
    genres = sorted({song["genre"] for song in songs})
    moods = sorted({song["mood"] for song in songs})
    return genres, moods


def sanitize_profile(profile: Dict, songs: List[Dict]) -> Tuple[Dict, List[str]]:
    """
    Return (clean_profile, warnings).

    Unknown keys are dropped, numbers are clamped to 0-1, and a genre or mood the catalog does
    not contain is removed (it could never match, so keeping it only distorts scores) with a warning.
    """
    genres, moods = catalog_vocabulary(songs)
    clean: Dict = {}
    warnings: List[str] = []

    for key, value in profile.items():
        if key not in PROFILE_KEYS:
            warnings.append(f"ignored unknown profile field '{key}'")
            continue
        if value is None:
            continue
        if key in PROFILE_NUMERIC_KEYS:
            try:
                number = float(value)
            except (TypeError, ValueError):
                warnings.append(f"ignored non-numeric {key}={value!r}")
                continue
            clamped = min(1.0, max(0.0, number))
            if clamped != number:
                warnings.append(f"clamped {key} from {number} to {clamped}")
            clean[key] = clamped
        elif key == "likes_acoustic":
            clean[key] = bool(value)
        else:
            text = str(value).strip().lower()
            vocabulary = genres if key == "favorite_genre" else moods
            if text in vocabulary:
                clean[key] = text
            else:
                warnings.append(f"'{text}' is not a {key.split('_')[1]} in the catalog, so it was dropped")

    return clean, warnings


def empty_constraints() -> Dict:
    """Return constraints with nothing ruled out."""
    return {"avoid_moods": [], "avoid_genres": [], "energy_range": None}


def sanitize_constraints(constraints: Optional[Dict]) -> Dict:
    """Normalize a constraints dict: lowercase lists, and an energy_range of two ordered floats in 0-1."""
    clean = empty_constraints()
    if not constraints:
        return clean
    for key in ("avoid_moods", "avoid_genres"):
        values = constraints.get(key) or []
        clean[key] = sorted({str(v).strip().lower() for v in values if str(v).strip()})
    energy_range = constraints.get("energy_range")
    if energy_range and len(energy_range) == 2:
        low, high = sorted(min(1.0, max(0.0, float(x))) for x in energy_range)
        clean["energy_range"] = [low, high]
    return clean


def verify_recommendations(
    song_ids: List[int],
    constraints: Dict,
    songs: List[Dict],
    k: int,
) -> List[Dict]:
    """
    Check a final list of song ids against the catalog and the listener's constraints.

    Returns a list of issues; an empty list means the recommendations passed. Each issue is
    {"song_id": int | None, "problem": str}, so the agent knows which song to replace.
    """
    by_id = {song["id"]: song for song in songs}
    issues: List[Dict] = []
    seen = set()
    energy_range = constraints.get("energy_range")

    for song_id in song_ids:
        song = by_id.get(song_id)
        if song is None:
            issues.append({"song_id": song_id, "problem": "not in the catalog (possible hallucination)"})
            continue
        if song_id in seen:
            issues.append({"song_id": song_id, "problem": "duplicate recommendation"})
            continue
        seen.add(song_id)
        if song["mood"] in constraints.get("avoid_moods", []):
            issues.append({"song_id": song_id, "problem": f"mood '{song['mood']}' was ruled out by the listener"})
        if song["genre"] in constraints.get("avoid_genres", []):
            issues.append({"song_id": song_id, "problem": f"genre '{song['genre']}' was ruled out by the listener"})
        if energy_range:
            low, high = energy_range
            if not (low - ENERGY_TOLERANCE <= song["energy"] <= high + ENERGY_TOLERANCE):
                issues.append({
                    "song_id": song_id,
                    "problem": f"energy {song['energy']:.2f} is outside the requested {low:.2f}-{high:.2f}",
                })

    if len(seen) < k:
        issues.append({"song_id": None, "problem": f"only {len(seen)} valid songs, expected {k}"})

    return issues
