"""
database.py — SQLite persistence layer for the CoC Player Tracker Bot.

Tables:
  tracked_players  — one row per Telegram user; each user tracks exactly 1 player
  player_snapshots — last known state of each tracked player (JSON blob)
  known_groups     — every group that has interacted with the bot (for /botlog)
  seen_battles     — battle timestamps already notified (deduplication)
"""

import sqlite3
import json
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "tracking.db")


# ─── Connection ──────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")   # better concurrent read/write
    return con


# ─── Initialization ──────────────────────────────────────────────────────────

def init_db():
    con = _conn()
    c = con.cursor()

    c.executescript("""
        CREATE TABLE IF NOT EXISTS tracked_players (
            user_id       INTEGER PRIMARY KEY,
            username      TEXT,
            player_tag    TEXT NOT NULL,
            player_name   TEXT,
            log_chat_id   INTEGER DEFAULT NULL,
            log_chat_name TEXT DEFAULT NULL,
            added_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (player_tag)         -- only one user may track any given tag
        );

        CREATE TABLE IF NOT EXISTS player_snapshots (
            player_tag    TEXT PRIMARY KEY,
            snapshot      TEXT NOT NULL,
            last_updated  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS known_groups (
            chat_id       INTEGER PRIMARY KEY,
            chat_name     TEXT,
            first_seen    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS seen_battles (
            player_tag    TEXT NOT NULL,
            battle_time   TEXT NOT NULL,
            PRIMARY KEY (player_tag, battle_time)
        );
    """)

    con.commit()
    con.close()


# ─── Tracked Players ─────────────────────────────────────────────────────────

def is_tag_taken(player_tag: str, by_user_id: int = None) -> bool:
    """
    Returns True if player_tag is already tracked by a DIFFERENT user.
    If by_user_id matches the existing tracker it returns False (same user re-tracking).
    """
    con = _conn()
    row = con.execute(
        "SELECT user_id FROM tracked_players WHERE player_tag = ?", (player_tag,)
    ).fetchone()
    con.close()
    if not row:
        return False
    return row["user_id"] != by_user_id


def add_tracked_player(
    user_id: int,
    username: str,
    player_tag: str,
    player_name: str,
) -> bool:
    """
    Track a player for a user.
    - Returns False if the tag is already tracked by someone else.
    - Replaces the user's existing tracking if they switch to a new tag.
    """
    con = _conn()
    c = con.cursor()

    # 1. Reject if the tag is owned by a DIFFERENT user
    existing_owner = c.execute(
        "SELECT user_id FROM tracked_players WHERE player_tag = ?", (player_tag,)
    ).fetchone()
    if existing_owner and existing_owner["user_id"] != user_id:
        con.close()
        return False

    # 2. If this user already tracks a DIFFERENT tag, clean up old data
    old_row = c.execute(
        "SELECT player_tag FROM tracked_players WHERE user_id = ?", (user_id,)
    ).fetchone()
    if old_row and old_row["player_tag"] != player_tag:
        old_tag = old_row["player_tag"]
        c.execute("DELETE FROM player_snapshots WHERE player_tag = ?", (old_tag,))
        c.execute("DELETE FROM seen_battles WHERE player_tag = ?", (old_tag,))

    # 3. Upsert the tracking record
    c.execute(
        """
        INSERT INTO tracked_players (user_id, username, player_tag, player_name,
                                     log_chat_id, log_chat_name)
        VALUES (?, ?, ?, ?, NULL, NULL)
        ON CONFLICT(user_id) DO UPDATE SET
            player_tag    = excluded.player_tag,
            player_name   = excluded.player_name,
            username      = excluded.username,
            log_chat_id   = NULL,
            log_chat_name = NULL,
            added_at      = CURRENT_TIMESTAMP
        """,
        (user_id, username, player_tag, player_name),
    )
    con.commit()
    con.close()
    return True


def remove_tracked_player(user_id: int) -> str | None:
    """
    Delete all tracking data for a user.
    Returns the player_tag that was removed, or None if the user had nothing.
    """
    con = _conn()
    c = con.cursor()

    row = c.execute(
        "SELECT player_tag FROM tracked_players WHERE user_id = ?", (user_id,)
    ).fetchone()
    if not row:
        con.close()
        return None

    tag = row["player_tag"]
    c.execute("DELETE FROM tracked_players   WHERE user_id    = ?", (user_id,))
    c.execute("DELETE FROM player_snapshots  WHERE player_tag = ?", (tag,))
    c.execute("DELETE FROM seen_battles      WHERE player_tag = ?", (tag,))
    con.commit()
    con.close()
    return tag


def get_tracked_player_by_user(user_id: int) -> dict | None:
    con = _conn()
    row = con.execute(
        "SELECT * FROM tracked_players WHERE user_id = ?", (user_id,)
    ).fetchone()
    con.close()
    return dict(row) if row else None


def get_tracked_player_by_tag(player_tag: str) -> dict | None:
    con = _conn()
    row = con.execute(
        "SELECT * FROM tracked_players WHERE player_tag = ?", (player_tag,)
    ).fetchone()
    con.close()
    return dict(row) if row else None


def get_all_tracked_tags() -> list[str]:
    con = _conn()
    rows = con.execute("SELECT player_tag FROM tracked_players").fetchall()
    con.close()
    return [r["player_tag"] for r in rows]


def get_log_chat_for_tag(player_tag: str) -> int | None:
    con = _conn()
    row = con.execute(
        "SELECT log_chat_id FROM tracked_players WHERE player_tag = ?", (player_tag,)
    ).fetchone()
    con.close()
    return row["log_chat_id"] if row else None


def set_log_chat(user_id: int, chat_id: int, chat_name: str):
    con = _conn()
    con.execute(
        "UPDATE tracked_players SET log_chat_id = ?, log_chat_name = ? WHERE user_id = ?",
        (chat_id, chat_name, user_id),
    )
    con.commit()
    con.close()


# ─── Snapshots ───────────────────────────────────────────────────────────────

def save_snapshot(player_tag: str, snapshot: dict):
    con = _conn()
    con.execute(
        """
        INSERT INTO player_snapshots (player_tag, snapshot, last_updated)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(player_tag) DO UPDATE SET
            snapshot     = excluded.snapshot,
            last_updated = CURRENT_TIMESTAMP
        """,
        (player_tag, json.dumps(snapshot)),
    )
    con.commit()
    con.close()


def get_snapshot(player_tag: str) -> dict | None:
    con = _conn()
    row = con.execute(
        "SELECT snapshot FROM player_snapshots WHERE player_tag = ?", (player_tag,)
    ).fetchone()
    con.close()
    return json.loads(row["snapshot"]) if row else None


# ─── Groups ──────────────────────────────────────────────────────────────────

def register_group(chat_id: int, chat_name: str):
    con = _conn()
    con.execute(
        "INSERT OR IGNORE INTO known_groups (chat_id, chat_name) VALUES (?, ?)",
        (chat_id, chat_name),
    )
    con.commit()
    con.close()


# ─── Battle Deduplication ────────────────────────────────────────────────────

def is_battle_seen(player_tag: str, battle_time: str) -> bool:
    con = _conn()
    row = con.execute(
        "SELECT 1 FROM seen_battles WHERE player_tag = ? AND battle_time = ?",
        (player_tag, battle_time),
    ).fetchone()
    con.close()
    return row is not None


def mark_battle_seen(player_tag: str, battle_time: str):
    con = _conn()
    con.execute(
        "INSERT OR IGNORE INTO seen_battles (player_tag, battle_time) VALUES (?, ?)",
        (player_tag, battle_time),
    )
    con.commit()
    con.close()


# ─── Admin / Stats ───────────────────────────────────────────────────────────

def get_all_tracked_info() -> list[dict]:
    """For /adminlist — returns every tracked player with their tracker's identity."""
    con = _conn()
    rows = con.execute(
        """
        SELECT user_id, username, player_tag, player_name, log_chat_name, added_at
        FROM tracked_players
        ORDER BY added_at DESC
        """
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_bot_stats() -> dict:
    """For /botlog — aggregate counts."""
    con = _conn()
    users  = con.execute("SELECT COUNT(*) FROM tracked_players").fetchone()[0]
    groups = con.execute("SELECT COUNT(*) FROM known_groups").fetchone()[0]
    con.close()
    return {"users": users, "groups": groups, "players": users}
