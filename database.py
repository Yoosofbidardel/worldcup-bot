import sqlite3
import threading
from datetime import datetime, timezone
from config import DB_PATH

_local = threading.local()


def _conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
    return _local.conn


def init_db():
    c = _conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS groups (
        chat_id     INTEGER PRIMARY KEY,
        title       TEXT,
        admin_id    INTEGER,
        created_at  TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS users (
        user_id     INTEGER,
        chat_id     INTEGER,
        username    TEXT,
        first_name  TEXT,
        joined_at   TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (user_id, chat_id)
    );
    CREATE TABLE IF NOT EXISTS matches (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        api_match_id    INTEGER UNIQUE,
        home_team       TEXT,
        away_team       TEXT,
        match_date      TEXT,
        stage           TEXT,
        group_name      TEXT,
        matchday        INTEGER,
        status          TEXT DEFAULT 'SCHEDULED',
        home_score      INTEGER,
        away_score      INTEGER,
        notified_groups TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS predictions (
        user_id     INTEGER,
        chat_id     INTEGER,
        match_id    INTEGER,
        home_score  INTEGER,
        away_score  INTEGER,
        points      INTEGER,
        created_at  TEXT DEFAULT (datetime('now')),
        updated_at  TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (user_id, chat_id, match_id),
        FOREIGN KEY (match_id) REFERENCES matches(id)
    );
    CREATE TABLE IF NOT EXISTS special_predictions (
        user_id         INTEGER,
        chat_id         INTEGER,
        pred_type       TEXT,
        pred_value      TEXT,
        points          INTEGER DEFAULT 0,
        created_at      TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (user_id, chat_id, pred_type)
    );
    """)
    c.commit()


# ── Group management ─────────────────────────────────────────────

def upsert_group(chat_id: int, title: str, admin_id: int):
    _conn().execute(
        "INSERT INTO groups (chat_id, title, admin_id) VALUES (?,?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title",
        (chat_id, title, admin_id),
    )
    _conn().commit()


def get_group(chat_id: int):
    return _conn().execute("SELECT * FROM groups WHERE chat_id=?", (chat_id,)).fetchone()


def get_all_group_ids() -> list[int]:
    rows = _conn().execute("SELECT chat_id FROM groups").fetchall()
    return [r["chat_id"] for r in rows]


# ── User management ──────────────────────────────────────────────

def upsert_user(user_id: int, chat_id: int, username: str, first_name: str):
    _conn().execute(
        "INSERT INTO users (user_id, chat_id, username, first_name) VALUES (?,?,?,?) "
        "ON CONFLICT(user_id, chat_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
        (user_id, chat_id, username, first_name),
    )
    _conn().commit()


def get_users(chat_id: int):
    return _conn().execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchall()


def get_user_groups(user_id: int):
    """Return all groups a user is registered in."""
    return _conn().execute(
        "SELECT u.chat_id, g.title FROM users u JOIN groups g ON g.chat_id=u.chat_id WHERE u.user_id=?",
        (user_id,),
    ).fetchall()


def is_group_admin(user_id: int, chat_id: int) -> bool:
    group = get_group(chat_id)
    return bool(group and group["admin_id"] == user_id)


# ── Match management ─────────────────────────────────────────────

def upsert_match(api_id, home, away, date_str, stage, group_name, matchday, status, home_score, away_score):
    _conn().execute(
        """INSERT INTO matches (api_match_id, home_team, away_team, match_date, stage, group_name, matchday, status, home_score, away_score)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(api_match_id) DO UPDATE SET
             status=excluded.status, home_score=excluded.home_score, away_score=excluded.away_score,
             match_date=excluded.match_date, stage=excluded.stage, group_name=excluded.group_name, matchday=excluded.matchday""",
        (api_id, home, away, date_str, stage, group_name, matchday, status, home_score, away_score),
    )
    _conn().commit()


def get_match_by_api_id(api_id: int):
    return _conn().execute("SELECT * FROM matches WHERE api_match_id=?", (api_id,)).fetchone()


def get_match(match_id: int):
    return _conn().execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()


def get_upcoming_matches(limit=10):
    return _conn().execute(
        "SELECT * FROM matches WHERE status IN ('SCHEDULED','TIMED') ORDER BY match_date LIMIT ?",
        (limit,),
    ).fetchall()


def get_recently_finished():
    return _conn().execute(
        "SELECT * FROM matches WHERE status='FINISHED' ORDER BY match_date DESC LIMIT 10"
    ).fetchall()


def get_newly_finished_unscored():
    """Matches that finished but still have unscored predictions."""
    return _conn().execute(
        """SELECT DISTINCT m.* FROM matches m
           JOIN predictions p ON p.match_id = m.id
           WHERE m.status = 'FINISHED' AND p.points IS NULL"""
    ).fetchall()


def get_all_matches():
    return _conn().execute("SELECT * FROM matches ORDER BY match_date").fetchall()


def mark_notified(match_id: int, chat_id: int):
    row = _conn().execute("SELECT notified_groups FROM matches WHERE id=?", (match_id,)).fetchone()
    current = row["notified_groups"] if row else ""
    ids = set(current.split(",")) if current else set()
    ids.add(str(chat_id))
    _conn().execute("UPDATE matches SET notified_groups=? WHERE id=?", (",".join(ids), match_id))
    _conn().commit()


def was_notified(match_id: int, chat_id: int) -> bool:
    row = _conn().execute("SELECT notified_groups FROM matches WHERE id=?", (match_id,)).fetchone()
    if not row or not row["notified_groups"]:
        return False
    return str(chat_id) in row["notified_groups"].split(",")


def get_group_stage_max_matchday() -> int:
    row = _conn().execute(
        "SELECT MAX(matchday) as md FROM matches WHERE stage='GROUP_STAGE' AND status IN ('FINISHED','IN_PLAY','PAUSED')"
    ).fetchone()
    return row["md"] if row and row["md"] else 0


# ── Predictions ──────────────────────────────────────────────────

def upsert_prediction(user_id, chat_id, match_id, home_score, away_score):
    _conn().execute(
        """INSERT INTO predictions (user_id, chat_id, match_id, home_score, away_score)
           VALUES (?,?,?,?,?)
           ON CONFLICT(user_id, chat_id, match_id) DO UPDATE SET
             home_score=excluded.home_score, away_score=excluded.away_score,
             updated_at=datetime('now'), points=NULL""",
        (user_id, chat_id, match_id, home_score, away_score),
    )
    _conn().commit()


def get_prediction(user_id, chat_id, match_id):
    return _conn().execute(
        "SELECT * FROM predictions WHERE user_id=? AND chat_id=? AND match_id=?",
        (user_id, chat_id, match_id),
    ).fetchone()


def get_predictions_for_match(match_id, chat_id):
    return _conn().execute(
        "SELECT p.*, u.first_name, u.username FROM predictions p "
        "JOIN users u ON u.user_id=p.user_id AND u.chat_id=p.chat_id "
        "WHERE p.match_id=? AND p.chat_id=?",
        (match_id, chat_id),
    ).fetchall()


def set_prediction_points(user_id, chat_id, match_id, points):
    _conn().execute(
        "UPDATE predictions SET points=? WHERE user_id=? AND chat_id=? AND match_id=?",
        (points, user_id, chat_id, match_id),
    )
    _conn().commit()


def get_user_predictions(user_id, chat_id):
    return _conn().execute(
        "SELECT p.*, m.home_team, m.away_team, m.match_date, m.home_score as actual_home, m.away_score as actual_away, m.status "
        "FROM predictions p JOIN matches m ON m.id=p.match_id "
        "WHERE p.user_id=? AND p.chat_id=? ORDER BY m.match_date",
        (user_id, chat_id),
    ).fetchall()


# ── Special predictions ──────────────────────────────────────────

def upsert_special(user_id, chat_id, pred_type, value):
    _conn().execute(
        "INSERT INTO special_predictions (user_id, chat_id, pred_type, pred_value) VALUES (?,?,?,?) "
        "ON CONFLICT(user_id, chat_id, pred_type) DO UPDATE SET pred_value=excluded.pred_value, points=0",
        (user_id, chat_id, pred_type, value),
    )
    _conn().commit()


def get_special(user_id, chat_id, pred_type):
    return _conn().execute(
        "SELECT * FROM special_predictions WHERE user_id=? AND chat_id=? AND pred_type=?",
        (user_id, chat_id, pred_type),
    ).fetchone()


def set_special_points(user_id, chat_id, pred_type, points):
    _conn().execute(
        "UPDATE special_predictions SET points=? WHERE user_id=? AND chat_id=? AND pred_type=?",
        (points, user_id, chat_id, pred_type),
    )
    _conn().commit()


def get_all_specials(chat_id, pred_type):
    return _conn().execute(
        "SELECT sp.*, u.first_name, u.username FROM special_predictions sp "
        "JOIN users u ON u.user_id=sp.user_id AND u.chat_id=sp.chat_id "
        "WHERE sp.chat_id=? AND sp.pred_type=?",
        (chat_id, pred_type),
    ).fetchall()


# ── Leaderboard ──────────────────────────────────────────────────

def get_leaderboard(chat_id: int):
    return _conn().execute(
        """SELECT u.user_id, u.first_name, u.username,
                  COALESCE(SUM(p.points),0) + COALESCE(sp_pts.special_total, 0) as total_points,
                  COUNT(p.points) as matches_scored
           FROM users u
           LEFT JOIN predictions p ON p.user_id=u.user_id AND p.chat_id=u.chat_id AND p.points IS NOT NULL
           LEFT JOIN (
               SELECT user_id, chat_id, SUM(points) as special_total
               FROM special_predictions GROUP BY user_id, chat_id
           ) sp_pts ON sp_pts.user_id=u.user_id AND sp_pts.chat_id=u.chat_id
           WHERE u.chat_id=?
           GROUP BY u.user_id
           ORDER BY total_points DESC""",
        (chat_id,),
    ).fetchall()


def get_all_predictions_for_export(chat_id: int):
    return _conn().execute(
        """SELECT u.first_name, u.username, m.home_team, m.away_team, m.match_date, m.stage,
                  p.home_score as pred_home, p.away_score as pred_away,
                  m.home_score as actual_home, m.away_score as actual_away,
                  m.status, p.points
           FROM predictions p
           JOIN users u ON u.user_id=p.user_id AND u.chat_id=p.chat_id
           JOIN matches m ON m.id=p.match_id
           WHERE p.chat_id=?
           ORDER BY m.match_date, u.first_name""",
        (chat_id,),
    ).fetchall()
