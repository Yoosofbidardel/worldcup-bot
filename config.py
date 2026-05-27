import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY", "")
COMPETITION_CODE = os.getenv("COMPETITION_CODE", "WC")
POLL_INTERVAL_MINUTES = int(os.getenv("POLL_INTERVAL_MINUTES", "5"))
DISPLAY_TIMEZONE = os.getenv("DISPLAY_TIMEZONE", "Europe/Amsterdam")

FOOTBALL_API_BASE = "https://api.football-data.org/v4"

# Scoring
PTS_EXACT = 10
PTS_CORRECT_DIFF = 7   # correct winner + correct goal difference (or draw with wrong score)
PTS_CORRECT_OUTCOME = 5 # correct winner, wrong goal difference
PTS_TOP_SCORER = 10
PTS_CHAMPION = 20

# Match statuses from football-data.org
STATUS_SCHEDULED = ("SCHEDULED", "TIMED")
STATUS_LIVE = ("IN_PLAY", "PAUSED", "EXTRA_TIME", "PENALTY_SHOOTOUT")
STATUS_FINISHED = ("FINISHED",)

DB_PATH = "worldcup_bot.db"
EXCEL_DIR = "excel_exports"
