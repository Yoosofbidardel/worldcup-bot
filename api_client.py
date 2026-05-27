import logging
import requests
from config import FOOTBALL_API_KEY, FOOTBALL_API_BASE, COMPETITION_CODE

logger = logging.getLogger(__name__)

HEADERS = {"X-Auth-Token": FOOTBALL_API_KEY}


def fetch_matches() -> list[dict]:
    """Fetch all matches for the competition from football-data.org."""
    url = f"{FOOTBALL_API_BASE}/competitions/{COMPETITION_CODE}/matches"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("matches", [])
    except Exception as e:
        logger.error("Failed to fetch matches: %s", e)
        return []


def fetch_scorers() -> list[dict]:
    """Fetch top scorers for the competition."""
    url = f"{FOOTBALL_API_BASE}/competitions/{COMPETITION_CODE}/scorers"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("scorers", [])
    except Exception as e:
        logger.error("Failed to fetch scorers: %s", e)
        return []


def parse_match(m: dict) -> dict:
    """Normalize a match object from the API into our internal format."""
    ft = m.get("score", {}).get("fullTime", {})
    # For knockout rounds, use the final result (incl. extra time / penalties)
    # football-data.org 'fullTime' already reflects the final score after ET.
    # Penalty result is in score.penalties but the fullTime score is the ET score.
    # If penalties were played, fullTime is the ET score (e.g. 1-1) and penalties gives shoot-out.
    # Per user requirement: we use fullTime as the prediction target.
    return {
        "api_id": m["id"],
        "home": m["homeTeam"]["name"],
        "away": m["awayTeam"]["name"],
        "date": m["utcDate"],
        "stage": m.get("stage", ""),
        "group": m.get("group"),
        "matchday": m.get("matchday"),
        "status": m["status"],
        "home_score": ft.get("home"),
        "away_score": ft.get("away"),
    }
