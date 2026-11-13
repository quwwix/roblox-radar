"""Roblox Radar — один запуск (GitHub Actions викликає це кожні ~10 хв).

Що робить за один запуск:
  1. збирає онлайн ігор з чартів Roblox;
  2. одразу пише в Telegram, якщо щось "вибухає";
  3. о 9:00 надсилає щоденний звіт, у неділю о 10:00 — графік;
  4. відповідає на команди, які ти написав боту з минулого запуску;
  5. оновлює data.json для менюшки (Mini App).

Локальний тест:  .venv/bin/python run.py   (бере токен з .env)
"""
import asyncio
import html
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

import analysis
import db
import details
import keywords
import roblox

ROOT = Path(__file__).parent
logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # щоб токен не потрапляв у логи
log = logging.getLogger("radar")


def load_env() -> dict:
    env = {}
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                env[key.strip()] = val.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if v})
    return env


ENV = load_env()
TOKEN = ENV.get("TELEGRAM_TOKEN", "")
CHAT = int(ENV.get("TELEGRAM_CHAT_ID", "0") or 0)
APP_URL = ENV.get("APP_URL", "")
TZ = ZoneInfo(ENV.get("TIMEZONE", "Europe/Kyiv"))
REPORT_HOUR = int(ENV.get("REPORT_HOUR", "9"))
WEEKLY_HOUR = int(ENV.get("WEEKLY_HOUR", "10"))

HELP = (
    "📡 <b>Roblox Radar</b>\n\n"
    "Нічого питати не треба — я пишу сам:\n"
    "🚀 одразу, якщо якась гра вибухає або зʼявився новий хіт;\n"
    f"🗞 щодня о {REPORT_HOUR}:00 — звіт: що росте, нові хіти, теми;\n"
    f"📊 у неділю о {WEEKLY_HOUR}:00 — графік за тиждень.\n\n"
    "Кнопка <b>«Відкрити»</b> внизу — меню з усіма списками, працює миттєво.\n\n"
    "<b>Ключові слова</b> (шукаю всі ігри зі словом і пишу про нові):\n"
    "/keyword <i>слово</i>, наприклад /keyword egg\n"
    "/unkeyword <i>слово</i>\n"
    "/keywords — мої слова\n\n"
    "<b>Стеження за грою</b> (відповідаю протягом ~10 хв):\n"
    "/watch <i>назва або посилання</i>\n"
    "/unwatch <i>назва</i>\n"
    "/list — список стеження"
)


# ---------------------------------------------------------------- Telegram

async def tg(c: httpx.AsyncClient, method: str, files=None, **params):
    try:
        if files:
            r = await c.post(f"https://api.telegram.org/bot{TOKEN}/{method}", data=params, files=files, timeout=60)
        else:
            r = await c.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=params, timeout=30)
        j = r.json()
    except Exception as e:  # не друкуємо сам виняток: у ньому може бути URL з токеном
        log.warning("Telegram %s: помилка мережі (%s)", method, type(e).__name__)
        return None
    if not j.get("ok"):
        log.warning("Telegram %s: %s", method, j.get("description"))
        return None
    return j.get("result")


def menu_markup():
    if not APP_URL:
        return None
    return {"inline_keyboard": [[{"text": "📡 Відкрити меню", "web_app": {"url": APP_URL}}]]}


async def send(c, text: str, button=False):
    parts, cur = [], ""
    for block in text.split("\n\n"):
        if len(cur) + len(block) + 2 > 3900:
            parts.append(cur)
            cur = block
        else:
            cur = f"{cur}\n\n{block}" if cur else block
    if cur:
        parts.append(cur)
    for i, p in enumerate(parts):
        extra = {}
        if button and i == len(parts) - 1 and menu_markup():
            extra["reply_markup"] = menu_markup()
        await tg(c, "sendMessage", chat_id=CHAT, text=p, parse_mode="HTML",
                 link_preview_options={"is_disabled": True}, **extra)


async def set_menu_button(c):
    if APP_URL:
        await tg(c, "setChatMenuButton", chat_id=CHAT,
                 menu_button={"type": "web_app", "text": "Відкрити", "web_app": {"url": APP_URL}})


# ---------------------------------------------------------------- термінові повідомлення

def new_hit_alerts(con, min_players=15000, max_age_days=30):
    """Молоді ігри, що вперше перейшли поріг онлайну."""
    rows = analysis.new_hits(con, max_age_days=max_age_days, min_players=min_players, limit=50)
    fresh = [(uid, p, age) for uid, p, age in rows
             if not con.execute("SELECT 1 FROM alerts WHERE uid=? AND kind='newhit'", (uid,)).fetchone()]
    if db.kv_get(con, "newhit_seeded") is None:
        # перший запуск: запамʼятовуємо поточні хіти без спаму
        for uid, _, _ in fresh:
            db.mark_alert(con, uid, "newhit")
        db.kv_set(con, "newhit_seeded", 1)
        return []
    return fresh


async def urgent(c, con):
    info = db.game_info(con)
    for uid, p, prev, per in analysis.explosions(con)[:3]:
        was = f"з {analysis.k(prev)}" if prev else "з майже нуля"
        await send(c, f"🚀 <b>Гра вибухає!</b>\n{analysis.link(info, uid)}\n"
                      f"Зараз <b>{analysis.k(p)}</b> онлайн — {was} за {analysis.period_s(per)}"
                      f"{analysis.age_s(info, uid)}")
        db.mark_alert(con, uid, "boom")
    for uid, p, age in new_hit_alerts(con)[:3]:
        await send(c, f"🆕 <b>Новий хіт!</b>\n{analysis.link(info, uid)}\n"
                      f"Грі лише {age:.0f} дн., а вже <b>{analysis.k(p)}</b> онлайн")
        db.mark_alert(con, uid, "newhit")
    for word, uid, p in keywords.fresh(con):
        await send(c, f"🔎 <b>Нова гра за словом «{html.escape(word)}»</b>\n{analysis.link(info, uid)}\n"
                      f"Зараз <b>{analysis.k(p)}</b> онлайн{analysis.age_s(info, uid)}")
        keywords.mark(con, word, uid)
    for uid, p, prev, pct in analysis.watch_changes(con):
        arrow = "📈" if pct > 0 else "📉"
        await send(c, f"{arrow} <b>Список стеження</b>\n{analysis.link(info, uid)}: "
                      f"{analysis.k(prev)} → <b>{analysis.k(p)}</b> ({analysis.pct_s(pct)} за добу)")
        db.mark_alert(con, uid, "watch")


# ---------------------------------------------------------------- за розкладом

async def scheduled(c, con):
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    if REPORT_HOUR <= now.hour < REPORT_HOUR + 4 and db.kv_get(con, "last_daily") != today:
        await send(c, analysis.daily_report(con), button=True)
        db.kv_set(con, "last_daily", today)
    if now.weekday() == 6 and WEEKLY_HOUR <= now.hour < WEEKLY_HOUR + 4 and db.kv_get(con, "last_weekly") != today:
        await send_chart(c, con)
        db.kv_set(con, "last_weekly", today)


async def send_chart(c, con):
    path = analysis.weekly_chart(con, ROOT / "data" / "weekly.png")
    if not path:
        await send(c, "Ще мало даних для графіка.")
        return
    with open(path, "rb") as f:
        await tg(c, "sendPhoto", files={"photo": ("weekly.png", f, "image/png")},
                 chat_id=str(CHAT), caption="📊 Roblox Radar — тиждень")


# ---------------------------------------------------------------- команди

async def cmd_watch(c, rc, con, q):
    if not q:
        await send(c, "Напиши так: /watch Steal An Egg\n(або встав посилання на гру)")
        return
    uid = await roblox.find_game(rc, q)
    det = await roblox.fetch_details(rc, [uid]) if uid else {}
    if not uid or uid not in det:
        await send(c, "Не знайшов таку гру 🤷 Спробуй точнішу назву або посилання.")
        return
    g = det[uid]
    db.upsert_game(con, uid, g, 0)
    con.execute("INSERT OR IGNORE INTO watch VALUES(?,?)", (uid, int(time.time())))
    con.execute("INSERT OR REPLACE INTO snaps VALUES(?,?,?)", (analysis.latest(con)[0] or int(time.time()), uid, g["players"]))
    con.commit()
    await send(c, f"👀 Стежу за <b>{html.escape(g['name'])}</b> — зараз {analysis.k(g['players'])} онлайн.\n"
                  "Напишу, якщо онлайн зміниться на 50%+ за добу.")


async def cmd_unwatch(c, con, q):
    q = q.lower().strip()
    info = db.game_info(con)
    rows = [u for (u,) in con.execute("SELECT uid FROM watch")]
    hit = [u for u in rows if q and (q in (info.get(u, {}).get("name") or "").lower() or q == str(u))]
    if not hit:
        await send(c, "Такої гри нема в списку. Подивись /list")
        return
    for u in hit:
        con.execute("DELETE FROM watch WHERE uid=?", (u,))
    con.commit()
    await send(c, "Прибрав: " + ", ".join(html.escape(info.get(u, {}).get("name") or str(u)) for u in hit))


async def commands(c, rc, con):
    offset = int(db.kv_get(con, "tg_offset", 0))
    updates = await tg(c, "getUpdates", offset=offset, timeout=0, allowed_updates=["message"]) or []
    for u in updates:
        offset = max(offset, u["update_id"] + 1)
        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat = (msg.get("chat") or {}).get("id")
        if not text.startswith("/"):
            continue
        if chat != CHAT:
            await tg(c, "sendMessage", chat_id=chat, text="Це приватний бот 🔒")
            continue
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        info = db.game_info(con)
        try:
            if cmd in ("/start", "/help"):
                await set_menu_button(c)
                await send(c, HELP, button=True)
            elif cmd == "/watch":
                await cmd_watch(c, rc, con, arg)
            elif cmd == "/unwatch":
                await cmd_unwatch(c, con, arg)
            elif cmd == "/keyword":
                res = keywords.add(con, arg) if arg else "empty"
                w = html.escape(" ".join(arg.lower().split()))
                await send(c, {
                    "ok": f"🔎 Додав слово «{w}». За кілька хвилин у меню на вкладці «Слова» зʼявляться всі ігри з ним, "
                          "а про нові я писатиму одразу.",
                    "exists": f"Слово «{w}» вже є. Список: /keywords",
                    "full": f"Забагато слів (максимум {keywords.MAX_WORDS}). Прибери якесь: /unkeyword слово",
                    "short": "Слово закоротке, треба хоча б 2 літери.",
                    "empty": "Напиши так: /keyword egg",
                }[res])
                if res == "ok":
                    await keywords.scan(rc, con)
            elif cmd == "/unkeyword":
                ok = keywords.remove(con, arg)
                await send(c, "Прибрав." if ok else "Такого слова нема. Список: /keywords")
            elif cmd == "/keywords":
                ws = keywords.words(con)
                await send(c, "🔎 <b>Мої слова</b>\n" + "\n".join("• " + html.escape(w) for w in ws)
                           if ws else "Слів ще нема. Додай: /keyword egg")
            elif cmd == "/list":
                await send(c, analysis.section_watch(con, info))
            elif cmd == "/report":
                await send(c, analysis.daily_report(con), button=True)
            elif cmd == "/grow":
                await send(c, analysis.section_growth(con, info, limit=15))
            elif cmd == "/new":
                await send(c, analysis.section_new(con, info, limit=15))
            elif cmd == "/themes":
                await send(c, analysis.section_themes(con, n=10))
            elif cmd == "/top":
                await send(c, analysis.section_top(con, info, n=15))
            elif cmd == "/chart":
                await send_chart(c, con)
        except Exception:
            log.exception("Команда %s не вдалась", cmd)
    db.kv_set(con, "tg_offset", offset)


# ---------------------------------------------------------------- дані для менюшки

async def export(rc, con):
    info = db.game_info(con)
    now_ts, now = analysis.latest(con)
    per, grow = analysis.growth(con, limit=40)
    tper, themes = analysis.theme_stats(con)
    new = analysis.new_hits(con, limit=40)
    top = analysis.top_now(con, 50)
    watch = analysis.watch_list(con)
    base_ts, base = analysis.baseline(con, now_ts) if now_ts else (None, {})

    def game(uid, players, **extra):
        g = info.get(uid, {})
        created = g.get("created")
        age = round((time.time() - analysis._parse(created)) / 86400) if created else None
        prev = base.get(uid)
        pct = round((players / max(prev, 300) - 1) * 100) if (players is not None and prev) else None
        d = {"id": uid, "name": g.get("name") or str(uid), "place": g.get("place_id"),
             "players": players, "pct": pct, "age": age}
        d.update(extra)
        return d

    theme_rows = []
    by_theme: dict[str, list] = {}
    for uid, p in now.items():
        for t in analysis.themes_of(info.get(uid, {}).get("name", "")):
            by_theme.setdefault(t, []).append((uid, p))
    if themes:
        src = [(t, n, pct, cnt) for t, n, b, pct, cnt in themes]
    else:  # історії ще нема: показуємо теми без відсотків
        src = sorted(((t, sum(p for _, p in gs), None, len(gs)) for t, gs in by_theme.items()),
                     key=lambda r: r[1], reverse=True)
    for t, n, pct, cnt in src:
        games = sorted(by_theme.get(t, []), key=lambda x: x[1], reverse=True)[:10]
        theme_rows.append({"name": t, "players": n, "pct": None if pct is None else round(pct), "count": cnt,
                           "games": [game(u, p) for u, p in games]})

    alerts = []
    for uid, kind, ts in con.execute(
            "SELECT uid, kind, ts FROM alerts WHERE kind IN ('boom','newhit') AND ts>=? ORDER BY ts DESC LIMIT 15",
            (int(time.time()) - 3 * 86400,)):
        if uid in now and db.kv_get(con, "newhit_seeded") and ts > int(db.kv_get(con, "seed_ts", 0)) + 60:
            alerts.append(game(uid, now[uid], kind=kind, ts=ts))

    data = {
        "updated": now_ts,
        "period_h": round(per / 3600) if per else None,
        "tracked": len(now),
        "total_players": sum(now.values()),
        "alerts": alerts,
        "grow": [game(uid, p, gain=gain) for uid, p, prev, gain, pct in grow],
        "new": [game(uid, p) for uid, p, age in new],
        "themes": theme_rows,
        "top": [game(uid, p) for uid, p in top],
        "watch": [game(uid, p) for uid, p, prev in watch],
        "keywords": [{"word": w, "total": total, "games": [game(u, p) for u, p in gs]}
                     for w, gs, total in keywords.groups(con)],
    }
    ids = set()
    for key in ("alerts", "grow", "new", "top", "watch"):
        ids |= {g["id"] for g in data[key]}
    for t in theme_rows:
        ids |= {g["id"] for g in t["games"][:10]}
    for kwg in data["keywords"]:
        ids |= {g["id"] for g in kwg["games"]}
    icons = await details.icons(rc, con, ids, set(now))
    data["details"] = details.export(con, ids, set(now) - ids)
    # усі ігри для пошуку в меню (компактно)
    data["all"] = [game(uid, p) for uid, p in sorted(now.items(), key=lambda x: x[1], reverse=True)]
    data["keywords_max"] = keywords.MAX_WORDS

    def put(g):
        g["icon"] = icons.get(g["id"])
    for key in ("alerts", "grow", "new", "top", "watch"):
        for g in data[key]:
            put(g)
    for t in theme_rows:
        for g in t["games"]:
            put(g)
    for kwg in data["keywords"]:
        for g in kwg["games"]:
            put(g)
    for g in data["all"]:
        put(g)
    (ROOT / "data" / "data.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    log.info("data.json: %d ростуть, %d нових, %d тем", len(data["grow"]), len(data["new"]), len(theme_rows))


# ---------------------------------------------------------------- головне

async def main():
    if not TOKEN or not CHAT:
        print("❌ Немає TELEGRAM_TOKEN або TELEGRAM_CHAT_ID")
        sys.exit(1)
    await analysis.collect()
    con = db.connect()
    if db.kv_get(con, "seed_ts") is None:
        db.kv_set(con, "seed_ts", int(time.time()))
    async with httpx.AsyncClient() as c, roblox.client() as rc:
        if db.kv_get(con, "menu_set") != APP_URL and APP_URL:
            await set_menu_button(c)
            db.kv_set(con, "menu_set", APP_URL)
        for name, step in (("термінові", urgent(c, con)), ("розклад", scheduled(c, con)),
                           ("команди", commands(c, rc, con)), ("слова", keywords.scan(rc, con)),
                           ("характеристики", details.refresh(rc, con)),
                           ("експорт", export(rc, con))):
            try:
                await step
            except Exception:
                log.exception("Крок «%s» не вдався", name)
    con.close()


if __name__ == "__main__":
    asyncio.run(main())
