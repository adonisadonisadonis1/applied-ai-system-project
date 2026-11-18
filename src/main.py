"""
Command line entry point for VibeCheck 2.0, the agentic music recommender.

Examples (run from the project root):
    python -m src.main "chill music for studying, nothing sad"
    python -m src.main "gym bangers, no metal" --steps
    python -m src.main --offline "happy acoustic songs"
    python -m src.main                 # runs a set of example requests
    python -m src.main --profiles      # the original VibeCheck 1.0 profile demo

If ANTHROPIC_API_KEY is set, Claude plans the request; otherwise (or with --offline) the
deterministic rule-based planner does. Every run is logged to logs/agent.log.
"""

import argparse
import logging
import os
from pathlib import Path

from src.agent import AgentResult, MusicAgent
from src.recommender import (
    ACOUSTIC_WEIGHT,
    ENERGY_WEIGHT,
    GENRE_WEIGHT,
    MOOD_WEIGHT,
    VALENCE_WEIGHT,
    load_songs,
    recommend_songs,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = PROJECT_ROOT / "data" / "songs.csv"
LOG_PATH = PROJECT_ROOT / "logs" / "agent.log"
MAX_SCORE = GENRE_WEIGHT + MOOD_WEIGHT + ENERGY_WEIGHT + VALENCE_WEIGHT + ACOUSTIC_WEIGHT

EXAMPLE_REQUESTS = [
    "chill music for studying, nothing sad",
    "high energy songs for the gym, no metal",
    "happy acoustic songs for a summer road trip",
    "k-pop music for dancing",
]

# The original VibeCheck 1.0 profiles: three typical listeners, then adversarial ones.
PROFILES = {
    "High-Energy Pop": {"favorite_genre": "pop", "favorite_mood": "happy", "target_energy": 0.80,
                        "target_valence": 0.80, "likes_acoustic": False},
    "Chill Lofi": {"favorite_genre": "lofi", "favorite_mood": "chill", "target_energy": 0.40,
                   "target_valence": 0.60, "likes_acoustic": True},
    "Deep Intense Rock": {"favorite_genre": "rock", "favorite_mood": "intense", "target_energy": 0.90,
                          "target_valence": 0.40, "likes_acoustic": False},
    "Sad Banger (conflicting)": {"favorite_genre": "folk", "favorite_mood": "sad", "target_energy": 0.90,
                                 "target_valence": 0.20, "likes_acoustic": False},
    "K-Pop Fan (missing genre)": {"favorite_genre": "k-pop", "favorite_mood": "hype", "target_energy": 0.85,
                                  "target_valence": 0.85, "likes_acoustic": False},
    "Acoustic Headbanger": {"favorite_genre": "metal", "favorite_mood": "angry", "target_energy": 0.95,
                            "target_valence": 0.25, "likes_acoustic": True},
    "Dead Center (no labels)": {"target_energy": 0.50, "target_valence": 0.50},
}


def setup_logging(log_path: Path = LOG_PATH) -> None:
    """Send every agent step to a log file; only warnings reach the terminal."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    root = logging.getLogger("vibecheck")
    root.setLevel(logging.INFO)
    root.handlers = [file_handler, console]


def print_result(result: AgentResult, show_steps: bool = False) -> None:
    """Print one agent result: summary, songs with reasons, warnings, and review flag."""
    print("=" * 64)
    print(f"Request: {result.request}")
    print(f"Planner: {result.planner}")
    print(f"Profile: {result.profile}")
    avoided = result.constraints["avoid_moods"] + result.constraints["avoid_genres"]
    if avoided or result.constraints["energy_range"]:
        print(f"Constraints: avoid {avoided or 'nothing'}, energy range {result.constraints['energy_range']}")
    print("=" * 64)
    print(result.summary)
    for rank, song in enumerate(result.recommendations, start=1):
        print(f"\n{rank}. {song['title']} by {song['artist']}  [{song['genre']} / {song['mood']}]")
        print(f"   Score: {song['score']:.2f} / {MAX_SCORE:.2f}")
        for reason in song["reasons"]:
            print(f"     - {reason}")
    if result.issues:
        print("\nUnresolved issues:")
        for issue in result.issues:
            print(f"  - song {issue['song_id']}: {issue['problem']}")
    if result.needs_review:
        print("\n!! Flagged for human review: the agent could not fully satisfy this request.")
    if show_steps:
        print("\nAgent steps:")
        for step in result.steps:
            print(f"  {step['step']:>2}. [{step['kind']}] {step['message']}")
    print()


def run_profile_demo(songs: list) -> None:
    """The original VibeCheck 1.0 behavior: score fixed profiles, no agent."""
    for name, user_prefs in PROFILES.items():
        print(f"\n### {name}")
        print(f"User profile: {user_prefs}")
        for rank, (song, score, explanation) in enumerate(recommend_songs(user_prefs, songs, k=5), start=1):
            print(f"{rank}. {song['title']} ({score:.2f}) because {explanation}")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="VibeCheck 2.0: describe the music you want in plain English.")
    parser.add_argument("request", nargs="*", help="what you want to listen to")
    parser.add_argument("--offline", action="store_true", help="use the rule-based planner even if an API key is set")
    parser.add_argument("--steps", action="store_true", help="print the agent's plan/act/check steps")
    parser.add_argument("--profiles", action="store_true", help="run the original VibeCheck 1.0 profile demo")
    args = parser.parse_args(argv)

    setup_logging()
    songs = load_songs(str(DATA_PATH))

    if args.profiles:
        run_profile_demo(songs)
        return

    use_claude = False if args.offline else None
    agent = MusicAgent(songs, use_claude=use_claude)
    if agent.use_claude:
        print(f"Planner: Claude ({os.environ.get('VIBECHECK_MODEL', 'claude-opus-5-5')})")
    else:
        reason = "--offline" if args.offline else "no ANTHROPIC_API_KEY set"
        print(f"Planner: rule-based ({reason})")

    requests = [" ".join(args.request)] if args.request else EXAMPLE_REQUESTS
    for request in requests:
        print_result(agent.run(request), show_steps=args.steps)
    print(f"Full step log: {LOG_PATH.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
