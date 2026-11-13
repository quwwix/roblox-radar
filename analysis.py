"""Збір, аналіз росту, теми, нові хіти, звіти і графік."""
import html
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import db
import roblox

log = logging.getLogger("analysis")

H = 3600
DAY = 86400
TRACK_WINDOW = 2 * DAY      # скільки стежимо за грою після того, як вона випала з чартів

# Теми: назва -> шаблони (шукаються в назві гри, без урахування регістру)
THEMES = {
    "🥷 Steal / крадіжки": [r"\bsteal", r"\brob\b", r"\bheist", r"\bthie"],
    "🥚 Яйця": [r"\begg"],
    "🧠 Brainrot": [r"brainrot"],
    "🧗 Обі": [r"\bobby", r"\bparkour", r"\btower of"],
    "🏭 Тайкун": [r"tycoon"],
    "👻 Хорор": [r"horror", r"\bscary", r"backrooms", r"\bdoors\b", r"nights?\b.*\bforest|\bnights\b"],
    "🌲 Виживання": [r"surviv", r"\bforest\b", r"\bisland"],
    "🐾 Пети / тварини": [r"\bpets?\b", r"\banimal", r"\bdragon"],
    "🌱 Ферма / сад": [r"garden", r"\bfarm", r"\bgrow\b", r"\bplant"],
    "⚔️ Бої / PvP": [r"\bbattle", r"\bfight", r"\brivals\b", r"\bpvp\b", r"\bwar\b", r"\bduel", r"\bboxing"],
    "🏎️ Машини / гонки": [r"\brace", r"\bcar\b|\bcars\b", r"\bdrive", r"\bdrag\b", r"\bmoped"],
    "🎣 Риболовля": [r"\bfish"],
    "💪 Симулятор": [r"simulator", r"\bsim\b"],
    "🍥 Аніме": [r"anime", r"\bpiece\b", r"\bblox fruits"],
    "🏰 Tower Defense": [r"tower defen", r"\btd\b"],
    "🏠 Рольові (RP)": [r"\brp\b", r"roleplay", r"brookhaven", r"\blife\b"],
    "🔪 Мардер / мафія": [r"murder", r"\bmm2\b", r"\bkiller", r"\bimpostor"],
    "🎰 Удача / RNG": [r"\brng\b", r"\bluck", r"\bspin\b", r"\bgacha", r"\bcase"],
    "🍔 Їжа / готування": [r"\bfood", r"\bcook", r"\brestaurant", r"\bpizza", r"\bburger"],
}
_THEME_RE = {t: [re.compile(p, re.I) for p in ps] for t, ps in THEMES.items()}


def themes_of(name: str) -> list[str]:
    return [t for t, pats in _THEME_RE.items() if any(p.search(name or "") for p in pats)]


# ---------------------------------------------------------------- збір

async def collect() -> int:
    """Один збір: чарти + всі ігри, за якими стежимо. Повертає ts збору."""
    ts = int(time.time())
    con = db.connect()
    async with roblox.client() as c:
        charts = await roblox.fetch_charts(c)
        for uid, g in charts.items():
            db.upsert_game(con, uid, g, ts)
        tracked = set(db.tracked_uids(con, ts - TRACK_WINDOW))
        missing = [u for u in tracked if u not in charts]
        no_created = [r[0] for r in con.execute("SELECT uid FROM games WHERE created IS NULL")]
        need = sorted(set(missing) | set(no_created))
        details = await roblox.fetch_details(c, need) if need else {}
    players = {uid: g["players"] for uid, g in charts.items()}
    for uid, g in details.items():
        # last_seen не оновлюємо для тих, хто випав з чартів, щоб колись перестати стежити
        con.execute(
            "UPDATE games SET created=COALESCE(created, ?), place_id=COALESCE(place_id, ?), name=COALESCE(name, ?) WHERE uid=?",
            (g["created"], g["place_id"], g["name"], uid),
        )
        if con.execute("SELECT 1 FROM games WHERE uid=?", (uid,)).fetchone() is None:
            db.upsert_game(con, uid, g, ts)
        if uid in missing:
            players[uid] = g["players"]
    last = db.sweeps(con)
    if last:
        prev_n = con.execute("SELECT COUNT(*) FROM snaps WHERE ts=?", (last[-1],)).fetchone()[0]
        if len(players) < 0.6 * prev_n:
            # Roblox віддав неповні дані: такий замір зіпсував би списки, пропускаємо його
            con.commit()
            con.close()
            raise RuntimeError(f"неповні дані від Roblox: {len(players)} ігор замість ~{prev_n}")
    con.executemany("INSERT OR REPLACE INTO snaps VALUES(?,?,?)", [(ts, u, p) for u, p in players.items()])
    con.commit()
    db.cleanup(con)
    con.close()
    log.info("Зібрано %d ігор (%d з чартів)", len(players), len(charts))
    return ts


# ---------------------------------------------------------------- аналіз

def baseline(con, now_ts, period=DAY, tol=3 * H, min_age=H):
    """Знімок ~period тому. Якщо історії ще мало - найстаріший доступний."""
    ts = db.sweep_near(con, now_ts - period, tol)
    if ts is None:
        all_ts = db.sweeps(con)
        if all_ts and now_ts - all_ts[0] >= min_age:
            ts = all_ts[0]
    if ts is None or ts >= now_ts:
        return None, {}
    base = db.players_at(con, ts)
    # Ігри, яких тоді ще не було під наглядом: беремо їхній ПЕРШИЙ відомий онлайн.
    # Інакше гра, що просто вперше потрапила в чарти, виглядала б як ріст "з нуля".
    for uid, players, _ in con.execute(
            "SELECT uid, players, MIN(ts) FROM snaps WHERE ts > ? AND ts < ? GROUP BY uid", (ts, now_ts)):
        base.setdefault(uid, players)
    return ts, base


def latest(con):
    s = db.sweeps(con)
    if not s:
        return None, {}
    return s[-1], db.players_at(con, s[-1])


def growth(con, limit=10, min_now=1500, min_gain=1000):
    now_ts, now = latest(con)
    base_ts, base = baseline(con, now_ts) if now_ts else (None, {})
    if not base_ts:
        return None, []
    rows = []
    for uid, p in now.items():
        prev = base.get(uid)
        if prev is None:          # гру бачимо вперше: рости їй ще нема від чого
            continue
        gain = p - prev
        if p < min_now or gain < min_gain:
            continue
        pct = (p / max(prev or 0, 300) - 1) * 100
        rows.append((uid, p, prev, gain, pct))
    rows.sort(key=lambda r: r[4], reverse=True)
    return (now_ts - base_ts), rows[:limit]


def theme_stats(con, period=DAY):
    now_ts, now = latest(con)
    base_ts, base = baseline(con, now_ts, period=period, tol=max(3 * H, period // 8)) if now_ts else (None, {})
    if not base_ts:
        return None, []
    info = db.game_info(con)
    agg: dict[str, list] = {}
    for uid in now:
        if uid not in base:      # порівнюємо лише ігри, які є в обох замірах
            continue
        for t in themes_of(info.get(uid, {}).get("name", "")):
            a = agg.setdefault(t, [0, 0, 0])
            a[0] += now[uid]
            a[1] += base[uid]
            a[2] += 1
    rows = []
    for t, (n, b, cnt) in agg.items():
        if n + b < 2000:
            continue
        pct = (n / max(b, 1) - 1) * 100
        rows.append((t, n, b, pct, cnt))
    rows.sort(key=lambda r: r[3], reverse=True)
    return (now_ts - base_ts), rows


def new_hits(con, max_age_days=30, min_players=1000, limit=10):
    now_ts, now = latest(con)
    info = db.game_info(con)
    rows = []
    for uid, p in now.items():
        created = info.get(uid, {}).get("created")
        if not created or p < min_players:
            continue
        age = (time.time() - _parse(created)) / DAY
        if age <= max_age_days:
            rows.append((uid, p, age))
    rows.sort(key=lambda r: r[1], reverse=True)
    return rows[:limit]


def top_now(con, limit=5):
    _, now = latest(con)
    return sorted(now.items(), key=lambda x: x[1], reverse=True)[:limit]


def explosions(con, min_now=5000, factor=3.0):
    """Ігри, що виросли в factor+ разів за ~добу. Повертає ще не оголошені."""
    now_ts, now = latest(con)
    base_ts, base = baseline(con, now_ts, min_age=6 * H) if now_ts else (None, {})
    if not base_ts:
        return []
    out = []
    for uid, p in now.items():
        if uid not in base:
            continue
        prev = base[uid]
        if p >= min_now and p >= factor * max(prev, 500) and not db.recently_alerted(con, uid, "boom", 3 * DAY):
            out.append((uid, p, prev, now_ts - base_ts))
    out.sort(key=lambda r: r[1], reverse=True)
    return out


def watch_changes(con, threshold=50):
    now_ts, now = latest(con)
    base_ts, base = baseline(con, now_ts, min_age=6 * H) if now_ts else (None, {})
    out = []
    for (uid,) in con.execute("SELECT uid FROM watch").fetchall():
        p, prev = now.get(uid), base.get(uid)
        if p is None or not prev or max(p, prev) < 100:
            continue
        pct = (p / prev - 1) * 100
        if abs(pct) >= threshold and not db.recently_alerted(con, uid, "watch", 20 * H):
            out.append((uid, p, prev, pct))
    return out


def watch_list(con):
    now_ts, now = latest(con)
    base_ts, base = baseline(con, now_ts) if now_ts else (None, {})
    rows = []
    for (uid,) in con.execute("SELECT uid FROM watch ORDER BY added").fetchall():
        rows.append((uid, now.get(uid), base.get(uid)))
    return rows


# ---------------------------------------------------------------- форматування

def _parse(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def k(n) -> str:
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}K"
    return str(n)


def pct_s(p) -> str:
    return f"{p:+.0f}%"


def link(info, uid) -> str:
    g = info.get(uid, {})
    name = html.escape((g.get("name") or str(uid))[:45])
    if g.get("place_id"):
        return f'<a href="https://www.roblox.com/games/{g["place_id"]}">{name}</a>'
    return name


def age_s(info, uid) -> str:
    c = info.get(uid, {}).get("created")
    if not c:
        return ""
    d = (time.time() - _parse(c)) / DAY
    return f" · {d:.0f} дн." if d < 365 else f" · {d / 365:.1f} р."


def period_s(sec) -> str:
    h = round(sec / H)
    return "24 год" if 22 <= h <= 26 else f"{h} год (історія ще збирається)"


def section_growth(con, info, limit=10):
    per, rows = growth(con, limit)
    if per is None:
        return "🚀 <b>Ростуть найшвидше</b>\nЩе збираю історію — перші цифри будуть за годину-дві."
    lines = [f"🚀 <b>Ростуть найшвидше</b> (за {period_s(per)})"]
    for i, (uid, p, prev, gain, pct) in enumerate(rows, 1):
        tag = pct_s(pct)
        lines.append(f"{i}. {link(info, uid)} — <b>{k(p)}</b> ({tag}, +{k(gain)}){age_s(info, uid)}")
    if not rows:
        lines.append("Поки нічого не росте помітно.")
    return "\n".join(lines)


def section_new(con, info, limit=8):
    rows = new_hits(con, limit=limit)
    lines = ["🆕 <b>Нові хіти</b> (молодші за 30 днів)"]
    for i, (uid, p, age) in enumerate(rows, 1):
        lines.append(f"{i}. {link(info, uid)} — <b>{k(p)}</b> · {age:.0f} дн.")
    if not rows:
        lines.append("Нових ігор з 1K+ онлайну зараз нема.")
    return "\n".join(lines)


def section_themes(con, n=5):
    per, rows = theme_stats(con)
    if per is None:
        return "🔥 <b>Теми</b>\nЩе збираю історію."
    up = [r for r in rows if r[3] > 0][:n]
    down = [r for r in reversed(rows) if r[3] < 0][:3]
    lines = [f"🔥 <b>Теми</b> (за {period_s(per)})"]
    for t, now, base, pct, cnt in up:
        lines.append(f"↗️ {t} — {k(now)} онлайну, <b>{pct_s(pct)}</b> ({cnt} ігор)")
    for t, now, base, pct, cnt in down:
        lines.append(f"↘️ {t} — {k(now)}, {pct_s(pct)}")
    return "\n".join(lines)


def section_top(con, info, n=5):
    lines = ["🏆 <b>Топ за онлайном зараз</b>"]
    for i, (uid, p) in enumerate(top_now(con, n), 1):
        lines.append(f"{i}. {link(info, uid)} — {k(p)}")
    return "\n".join(lines)


def section_watch(con, info):
    rows = watch_list(con)
    if not rows:
        return "👀 <b>Список стеження</b>\nПорожньо. Додай гру: /watch назва гри"
    lines = ["👀 <b>Список стеження</b>"]
    for uid, p, prev in rows:
        ch = f" ({pct_s((p / prev - 1) * 100)} за добу)" if p is not None and prev else ""
        lines.append(f"• {link(info, uid)} — <b>{k(p)}</b>{ch}")
    return "\n".join(lines)


def daily_report(con) -> str:
    info = db.game_info(con)
    head = f"📡 <b>Roblox Radar</b> · {datetime.now().strftime('%d.%m %H:%M')}"
    return "\n\n".join([
        head,
        section_growth(con, info),
        section_new(con, info),
        section_themes(con),
        section_watch(con, info),
        section_top(con, info),
    ])


# ---------------------------------------------------------------- тижневий графік

_EMOJI = re.compile(r"[^\w\s\-\.,:!?&'()/+]", re.UNICODE)


def _clean(name: str) -> str:
    s = _EMOJI.sub("", name or "")
    s = re.sub(r"\s+", " ", s).strip(" -[]")
    return s[:28] or "?"


def weekly_chart(con, path: Path) -> Path | None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    now_ts, now = latest(con)
    if not now_ts:
        return None
    info = db.game_info(con)
    since = now_ts - 7 * DAY
    # топ-5 за середнім онлайном за тиждень
    top = con.execute(
        "SELECT uid, AVG(players) a FROM snaps WHERE ts>=? GROUP BY uid ORDER BY a DESC LIMIT 5", (since,)
    ).fetchall()
    per, themes = theme_stats(con, period=7 * DAY)

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10})
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 10), gridspec_kw={"height_ratios": [3, 2]})
    fig.patch.set_facecolor("white")
    colors = ["#2a6fdb", "#e8590c", "#2b8a3e", "#ae3ec9", "#c92a2a"]
    for (uid, _), col in zip(top, colors):
        pts = con.execute("SELECT ts, players FROM snaps WHERE uid=? AND ts>=? ORDER BY ts", (uid, since)).fetchall()
        xs = [datetime.fromtimestamp(t) for t, _ in pts]
        ax1.plot(xs, [p / 1000 for _, p in pts], color=col, lw=2, label=_clean(info.get(uid, {}).get("name")))
    ax1.set_title("Топ-5 ігор за тиждень (тисяч гравців онлайн)", loc="left", fontweight="bold")
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    ax1.grid(alpha=0.25)
    ax1.legend(frameon=False, fontsize=9, loc="upper left")
    for s in ("top", "right"):
        ax1.spines[s].set_visible(False)

    if themes:
        shown = themes[:6] + [r for r in themes[-4:] if r not in themes[:6]]
        shown.sort(key=lambda r: r[3])
        names = [_clean(r[0]) for r in shown]
        vals = [max(min(r[3], 500), -100) for r in shown]
        ax2.barh(names, vals, color=["#2b8a3e" if v >= 0 else "#c92a2a" for v in vals])
        for i, v in enumerate(vals):
            ax2.text(v, i, f" {shown[i][3]:+.0f}% ", va="center", ha="left" if v >= 0 else "right", fontsize=9)
        ax2.axvline(0, color="#444", lw=0.8)
        days = round(per / DAY)
        ax2.set_title(f"Теми: зміна онлайну за {days} дн.", loc="left", fontweight="bold")
        for s in ("top", "right"):
            ax2.spines[s].set_visible(False)
    else:
        ax2.axis("off")
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path
