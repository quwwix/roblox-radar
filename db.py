"""Локальна база (SQLite) з історією онлайну."""
import sqlite3
import time
from pathlib import Path

PATH = Path(__file__).parent / "data" / "radar.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS games(uid INTEGER PRIMARY KEY, name TEXT, place_id INTEGER,
    created TEXT, genre TEXT, last_seen INTEGER);
CREATE TABLE IF NOT EXISTS snaps(ts INTEGER, uid INTEGER, players INTEGER, PRIMARY KEY(ts, uid));
CREATE INDEX IF NOT EXISTS snaps_uid ON snaps(uid, ts);
CREATE TABLE IF NOT EXISTS watch(uid INTEGER PRIMARY KEY, added INTEGER);
CREATE TABLE IF NOT EXISTS alerts(uid INTEGER, kind TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""


def connect() -> sqlite3.Connection:
    PATH.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(PATH)
    con.executescript(SCHEMA)
    return con


def kv_get(con, k, default=None):
    row = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def kv_set(con, k, v):
    con.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, str(v)))
    con.commit()


def upsert_game(con, uid, info, ts):
    con.execute(
        """INSERT INTO games(uid, name, place_id, created, genre, last_seen) VALUES(?,?,?,?,?,?)
           ON CONFLICT(uid) DO UPDATE SET name=excluded.name,
             place_id=COALESCE(excluded.place_id, games.place_id),
             created=COALESCE(excluded.created, games.created),
             genre=COALESCE(NULLIF(excluded.genre,''), games.genre),
             last_seen=MAX(games.last_seen, excluded.last_seen)""",
        (uid, info.get("name"), info.get("place_id"), info.get("created"), info.get("genre", ""), ts),
    )


def tracked_uids(con, since_ts) -> list[int]:
    rows = con.execute(
        "SELECT uid FROM games WHERE last_seen >= ? UNION SELECT uid FROM watch", (since_ts,)
    ).fetchall()
    return [r[0] for r in rows]


def sweeps(con) -> list[int]:
    return [r[0] for r in con.execute("SELECT DISTINCT ts FROM snaps ORDER BY ts")]


def sweep_near(con, target, tol) -> int | None:
    row = con.execute(
        "SELECT ts FROM snaps WHERE ts BETWEEN ? AND ? GROUP BY ts ORDER BY ABS(ts-?) LIMIT 1",
        (target - tol, target + tol, target),
    ).fetchone()
    return row[0] if row else None


def players_at(con, ts) -> dict[int, int]:
    return dict(con.execute("SELECT uid, players FROM snaps WHERE ts=?", (ts,)).fetchall())


def game_info(con) -> dict[int, dict]:
    out = {}
    for uid, name, place_id, created, genre in con.execute(
        "SELECT uid, name, place_id, created, genre FROM games"
    ):
        out[uid] = {"name": name, "place_id": place_id, "created": created, "genre": genre}
    return out


def recently_alerted(con, uid, kind, within) -> bool:
    row = con.execute(
        "SELECT 1 FROM alerts WHERE uid=? AND kind=? AND ts>=?", (uid, kind, int(time.time()) - within)
    ).fetchone()
    return row is not None


def mark_alert(con, uid, kind):
    con.execute("INSERT INTO alerts VALUES(?,?,?)", (uid, kind, int(time.time())))
    con.commit()


def cleanup(con, keep_days=9):
    """Чистить стару історію і проріджує знімки, щоб база лишалась малою."""
    now = int(time.time())
    con.execute("DELETE FROM snaps WHERE ts < ?", (now - keep_days * 86400,))
    con.execute("DELETE FROM alerts WHERE ts < ?", (now - 45 * 86400,))
    s = sweeps(con)
    drop = []
    # свіжі знімки: лишаємо приблизно кожні 30 хв (і завжди найновіший)
    if len(s) >= 3 and s[-2] - s[-3] < 25 * 60:
        drop.append(s[-2])
    # старші за 48 год: лишаємо приблизно кожні 3 год
    kept = None
    for ts in s:
        if ts >= now - 48 * 3600:
            break
        if kept is None or ts - kept >= 2.9 * 3600:
            kept = ts
        else:
            drop.append(ts)
    con.executemany("DELETE FROM snaps WHERE ts=?", [(t,) for t in drop])
    con.commit()
