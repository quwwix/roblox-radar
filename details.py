"""Характеристики ігор: візити, лайки, опис, оцінка середньої сесії, історія онлайну."""
import asyncio
import logging
import time

import analysis
import db
import roblox

log = logging.getLogger("details")

H = 3600
DAY = 86400
REFRESH_EVERY = 2 * H - 300       # як часто оновлюємо візити і лайки
VOTES = "https://games.roblox.com/v1/games/votes"

COLUMNS = {
    "visits": "INTEGER", "favs": "INTEGER", "up": "INTEGER", "down": "INTEGER",
    "max_players": "INTEGER", "updated": "TEXT", "creator": "TEXT", "descr": "TEXT",
    "genre2": "TEXT", "icon": "TEXT",
}


def ensure_schema(con):
    have = {r[1] for r in con.execute("PRAGMA table_info(games)")}
    for col, typ in COLUMNS.items():
        if col not in have:
            con.execute(f"ALTER TABLE games ADD COLUMN {col} {typ}")
    con.execute("CREATE TABLE IF NOT EXISTS vis(ts INTEGER, uid INTEGER, visits INTEGER, PRIMARY KEY(uid, ts))")
    con.commit()


async def refresh(rc, con, force=False):
    """Раз на ~2 год тягне візити, лайки й опис для всіх ігор, за якими стежимо."""
    ensure_schema(con)
    now = int(time.time())
    if not force and now - int(db.kv_get(con, "details_ts", 0)) < REFRESH_EVERY:
        return False
    uids = db.tracked_uids(con, now - analysis.TRACK_WINDOW)
    n = 0
    for i in range(0, len(uids), 50):
        if time.time() - now > 150:      # Roblox гальмує: решту доберемо наступного разу
            log.warning("Характеристики: не встиг усе, оновлено частину")
            break
        chunk = uids[i:i + 50]
        ids = ",".join(map(str, chunk))
        data = await roblox._get(rc, roblox.GAMES, {"universeIds": ids})
        for g in (data or {}).get("data", []):
            creator = (g.get("creator") or {}).get("name")
            genre = " / ".join(x for x in (g.get("genre_l1"), g.get("genre_l2")) if x)
            con.execute(
                """UPDATE games SET visits=?, favs=?, max_players=?, updated=?, creator=?, descr=?, genre2=?,
                   created=COALESCE(created, ?) WHERE uid=?""",
                (g.get("visits"), g.get("favoritedCount"), g.get("maxPlayers"), g.get("updated"), creator,
                 (g.get("description") or "")[:400], genre, g.get("created"), g["id"]),
            )
            if g.get("visits") is not None:
                con.execute("INSERT OR REPLACE INTO vis VALUES(?,?,?)", (now, g["id"], g["visits"]))
                n += 1
        votes = await roblox._get(rc, VOTES, {"universeIds": ids})
        for v in (votes or {}).get("data", []):
            con.execute("UPDATE games SET up=?, down=? WHERE uid=?", (v.get("upVotes"), v.get("downVotes"), v["id"]))
        await asyncio.sleep(0.25)
    con.execute("DELETE FROM vis WHERE ts < ?", (now - 3 * DAY,))
    con.commit()
    if n:
        db.kv_set(con, "details_ts", now)
    log.info("Оновлено характеристики %d ігор", n)
    return True


def sessions(con, uids):
    """Оцінка середньої сесії: (середній онлайн x час) / нові візити.

    Повертає {uid: (хвилин_сесії, візитів_за_добу)}. Це оцінка, а не точна цифра Roblox.
    """
    out = {}
    now = int(time.time())
    for uid in uids:
        rows = con.execute("SELECT ts, visits FROM vis WHERE uid=? AND ts>=? ORDER BY ts",
                           (uid, now - DAY - H)).fetchall()
        if len(rows) < 2:
            continue
        (t0, v0), (t1, v1) = rows[0], rows[-1]
        dt, dv = t1 - t0, v1 - v0
        if dt < 1.5 * H or dv < 200:
            continue
        avg = con.execute("SELECT AVG(players) FROM snaps WHERE uid=? AND ts BETWEEN ? AND ?",
                          (uid, t0 - 600, t1 + 600)).fetchone()[0]
        if not avg:
            continue
        minutes = avg * dt / 60 / dv
        if 0.5 <= minutes <= 600:
            out[uid] = (round(minutes, 1), round(dv * DAY / dt), round(dt / H))
    return out


def history(con, uids, points=48):
    """Історія онлайну за тиждень для графіка: {uid: [[хвилин_тому, гравців], ...]}"""
    now = int(time.time())
    out = {}
    for uid in uids:
        rows = con.execute("SELECT ts, players FROM snaps WHERE uid=? AND ts>=? ORDER BY ts",
                           (uid, now - 7 * DAY)).fetchall()
        if len(rows) > points:
            step = len(rows) / points
            rows = [rows[int(i * step)] for i in range(points - 1)] + [rows[-1]]
        out[uid] = [[round((now - ts) / 60), p] for ts, p in rows]
    return out


async def icons(rc, con, fresh_ids, all_ids):
    """Іконки: для ігор у списках беремо свіжі, для решти — лише відсутні (зберігаємо в базі)."""
    ensure_schema(con)
    have = dict(con.execute("SELECT uid, icon FROM games WHERE icon IS NOT NULL").fetchall())
    need = sorted(set(fresh_ids) | {u for u in all_ids if u not in have})
    got = await roblox.fetch_icons(rc, need)
    con.executemany("UPDATE games SET icon=? WHERE uid=?", [(url, uid) for uid, url in got.items()])
    con.commit()
    have.update(got)
    return have


def export(con, uids, light=()):
    """Повні картки для uids; для light — без графіка й опису (щоб файл був меншим)."""
    ensure_schema(con)
    light = set(light)
    uids = list(uids)
    sess = sessions(con, uids + list(light))
    hist = history(con, uids)
    out = {}
    for uid in uids + list(light):
        row = con.execute(
            "SELECT visits, favs, up, down, max_players, updated, creator, descr, genre2, created FROM games WHERE uid=?",
            (uid,)).fetchone()
        if not row:
            continue
        visits, favs, up, down, maxp, updated, creator, descr, genre, created = row
        peak = con.execute("SELECT MAX(players) FROM snaps WHERE uid=?", (uid,)).fetchone()[0]
        d = {"visits": visits, "favs": favs, "max": maxp, "creator": creator, "descr": None if uid in light else descr, "genre": genre,
             "created": created[:10] if created else None,
             "upd_days": round((time.time() - analysis._parse(updated)) / DAY, 1) if updated else None,
             "like": round(up * 100 / (up + down)) if up is not None and down is not None and up + down > 0 else None,
             "votes": (up or 0) + (down or 0) if up is not None else None,
             "peak": peak, "hist": hist.get(uid, [])}
        if uid in sess:
            d["session"], d["visits_day"], d["session_h"] = sess[uid]
        out[str(uid)] = d
    return out
