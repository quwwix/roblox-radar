"""Ключові слова: шукає ігри за словом через пошук Roblox і стежить за новими."""
import logging
import time
import uuid

import analysis
import db
import roblox

log = logging.getLogger("keywords")

MAX_WORDS = 15
MIN_TRACK = 100     # від якого онлайну гру зі пошуку беремо під нагляд
MIN_ALERT = 300     # від якого онлайну пишемо про нову гру
PAGES = 2           # сторінок пошуку на слово (по ~40 ігор)


def ensure(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS kw(word TEXT PRIMARY KEY, added INTEGER, seeded INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS kwgame(word TEXT, uid INTEGER, first_seen INTEGER, alerted INTEGER DEFAULT 0,
        PRIMARY KEY(word, uid));
    """)


def words(con) -> list[str]:
    ensure(con)
    return [r[0] for r in con.execute("SELECT word FROM kw ORDER BY added")]


def add(con, word: str) -> str:
    ensure(con)
    word = " ".join(word.lower().split())[:30]
    if len(word) < 2:
        return "short"
    if word in words(con):
        return "exists"
    if len(words(con)) >= MAX_WORDS:
        return "full"
    con.execute("INSERT INTO kw VALUES(?,?,0)", (word, int(time.time())))
    con.commit()
    return "ok"


def remove(con, word: str) -> bool:
    ensure(con)
    word = " ".join(word.lower().split())
    n = con.execute("DELETE FROM kw WHERE word=?", (word,)).rowcount
    con.execute("DELETE FROM kwgame WHERE word=?", (word,))
    con.commit()
    return n > 0


async def search(rc, word: str) -> dict[int, dict]:
    out, token, session = {}, None, str(uuid.uuid4())
    for _ in range(PAGES):
        params = {"searchQuery": word, "sessionId": session, "pageType": "all"}
        if token:
            params["pageToken"] = token
        data = await roblox._get(rc, roblox.SEARCH, params)
        if not data:
            break
        for group in data.get("searchResults", []):
            if group.get("contentGroupType") != "Game":
                continue
            for g in group.get("contents", []):
                uid = g.get("universeId")
                text = f"{g.get('name', '')} {g.get('description', '')}".lower()
                if not uid or g.get("isSponsored") or word not in text:
                    continue
                out[uid] = {"name": g.get("name", "?"), "place_id": g.get("rootPlaceId"),
                            "players": int(g.get("playerCount") or 0), "genre": ""}
        token = data.get("nextPageToken")
        if not token:
            break
    return out


async def scan(rc, con):
    """Оновлює списки ігор для кожного слова. Викликається кожен запуск."""
    ensure(con)
    now_ts, now = analysis.latest(con)
    if not now_ts:
        return
    t = int(time.time())
    for word, seeded in con.execute("SELECT word, seeded FROM kw").fetchall():
        found = await search(rc, word)
        for uid, g in found.items():
            if uid in now or g["players"] >= MIN_TRACK:
                db.upsert_game(con, uid, g, t)          # гра лишається під наглядом
                if uid not in now:
                    con.execute("INSERT OR REPLACE INTO snaps VALUES(?,?,?)", (now_ts, uid, g["players"]))
                    now[uid] = g["players"]
        info = db.game_info(con)
        members = {uid for uid in found if uid in now}
        members |= {uid for uid in now if word in (info.get(uid, {}).get("name") or "").lower()}
        con.executemany("INSERT OR IGNORE INTO kwgame VALUES(?,?,?,0)", [(word, uid, t) for uid in members])
        if not seeded:   # перший раз: запамʼятовуємо те, що вже є, без повідомлень
            con.execute("UPDATE kwgame SET alerted=1 WHERE word=?", (word,))
            con.execute("UPDATE kw SET seeded=1 WHERE word=?", (word,))
        con.commit()
        log.info("Слово «%s»: %d ігор", word, len(members))


def fresh(con, limit=5):
    """Ігри за словами, про які ще не писали і які набрали онлайн."""
    ensure(con)
    _, now = analysis.latest(con)
    rows = []
    for word, uid in con.execute("SELECT word, uid FROM kwgame WHERE alerted=0").fetchall():
        p = now.get(uid, 0)
        if p >= MIN_ALERT:
            rows.append((word, uid, p))
    rows.sort(key=lambda r: r[2], reverse=True)
    return rows[:limit]


def mark(con, word, uid):
    con.execute("UPDATE kwgame SET alerted=1 WHERE uid=?", (uid,))   # одна гра — одне повідомлення
    con.commit()


def groups(con, per_word=40):
    """Для меню: [(слово, [(uid, players), ...])]"""
    ensure(con)
    _, now = analysis.latest(con)
    out = []
    for word in words(con):
        uids = [r[0] for r in con.execute("SELECT uid FROM kwgame WHERE word=?", (word,))]
        games = sorted(((u, now[u]) for u in uids if u in now), key=lambda x: x[1], reverse=True)
        out.append((word, games[:per_word], len(games)))
    return out
