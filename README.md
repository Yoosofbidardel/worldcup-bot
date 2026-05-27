# ⚽ World Cup 2026 Prediction Bot

A Telegram bot for group prediction games during FIFA World Cup 2026. Each group chat gets its own isolated leaderboard, predictions, and admin controls.

## Features

- **Match predictions** — predict scores for all 104 World Cup matches
- **Auto-lock** — predictions lock at kickoff (fetched in real time)
- **Auto-scoring** — results fetched from football-data.org API and points calculated automatically
- **Special predictions** — top scorer (10 pts) and champion (20 pts) bonuses
- **Leaderboard** — public rankings in-chat
- **Excel export** — admin-only full report with leaderboard, all predictions, results, and specials
- **24h reminders** — bot posts upcoming matches so nobody forgets
- **Multi-group** — works in multiple group chats simultaneously with separate data

## Scoring System

| Prediction accuracy | Points |
|---|---|
| Exact score (2-1 → 2-1) | 10 |
| Correct winner + correct goal difference (1-0 → 2-1) | 7 |
| Draw predicted + draw actual, wrong score (1-1 → 3-3) | 7 |
| Correct winner, wrong goal difference (3-1 → 2-1) | 5 |
| Wrong outcome | 0 |

**Specials:**
- Top scorer prediction (locks before matchday 3): **10 pts**
- Champion prediction (locks after matchday 1): **20 pts**

## Setup

### 1. Create a Telegram Bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow the prompts
3. Copy the bot token

### 2. Get a Football-Data.org API Key

1. Register free at [football-data.org](https://www.football-data.org/client/register)
2. Copy your API key (free tier: 10 requests/minute — more than enough)

### 3. Configure

```bash
cp .env.example .env
# Edit .env with your tokens:
#   TELEGRAM_BOT_TOKEN=...
#   FOOTBALL_API_KEY=...
```

### 4. Install & Run

```bash
pip install -r requirements.txt
python main.py
```

### 5. Activate in a Group

1. Add the bot to your Telegram group
2. Send `/start` — the person who sends this becomes the admin
3. The bot will auto-sync matches and start posting reminders

## Commands

### Everyone
| Command | Description |
|---|---|
| `/start` | Activate bot in group |
| `/help` | Show all commands |
| `/matches` | List upcoming matches with IDs |
| `/predict <id> <home>-<away>` | Predict a match score |
| `/mypredictions` | View your predictions |
| `/results` | Recent match results |
| `/leaderboard` | Group rankings |
| `/topscorer <player>` | Predict the top scorer |
| `/champion <team>` | Predict the champion |

### Admin Only
| Command | Description |
|---|---|
| `/excel` | Download full Excel report |
| `/syncmatches` | Force-refresh matches from API |
| `/awardtopscorer <player>` | Award top scorer points |
| `/awardchampion <team>` | Award champion points |

## How It Works

1. **Every 5 minutes** (configurable), the bot polls football-data.org for match updates
2. **24 hours before kickoff**, a reminder is posted to all groups
3. **At kickoff**, predictions lock automatically (based on the match's UTC kickoff time)
4. **When a match finishes**, the bot scores all predictions and posts a summary
5. Users who write any message in the group are auto-registered as players

## Knockout Rounds

For knockout matches, predictions cover the **final result including extra time and penalties**. The football-data.org API's `fullTime` score reflects the result after extra time. The bot uses this score for all calculations.

## Notes

- The admin is whoever first runs `/start` in a group
- The Excel file is regenerated on every `/excel` request with the latest data
- Team names come directly from the API — use the same spelling when predicting champion
- Top scorer and champion awards are manual (admin runs `/awardtopscorer` / `/awardchampion` at the end)
- The bot automatically handles timezone display using the `DISPLAY_TIMEZONE` env var

## File Structure

```
worldcup_bot/
├── main.py          # Entry point
├── bot.py           # Telegram handlers + scheduler
├── config.py        # Environment config
├── database.py      # SQLite operations
├── scoring.py       # Points calculation
├── api_client.py    # football-data.org client
├── excel_export.py  # Admin Excel reports
├── requirements.txt
├── .env.example
└── README.md
```
