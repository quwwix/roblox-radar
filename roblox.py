"""Збір даних з публічних серверів Roblox (без ключів)."""
import asyncio
import logging
import re
import uuid

import httpx

log = logging.getLogger("roblox")

EXPLORE = "https://apis.roblox.com/explore-api/v1/get-sorts"
GAMES = "https://games.roblox.com/v1/games"
SEARCH = "https://apis.roblox.com/search-api/omni-search"
ICONS = "https://thumbnails.roblox.com/v1/games/icons"
PLACE2UNI = "https://apis.roblox.com/universes/v1/places/{}/universe"

HEADERS = {"User-Agent": "Mozilla/5.0 (RobloxRadar personal bot)", "Accept": "application/json"}


async def _get(client: httpx.AsyncClient, url: str, params=None):
    for attempt in range(3):
        try:
            r = await client.get(url, params=params, timeout=20)
            if r.status_code == 429:
                await asyncio.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.warning("GET %s failed (%s), спроба %d", url, e, attempt + 1)
            await asyncio.sleep(2 * (attempt + 1))
    return None


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers=HEADERS, follow_redirects=True)


async def fetch_charts(c: httpx.AsyncClient) -> dict[int, dict]:
    """Всі ігри з чартів Roblox (Trending, Up-and-Coming, Top Playing...)."""
    games: dict[int, dict] = {}
    session = str(uuid.uuid4())
    token = None
    for _ in range(8):
        params = {"sessionId": session, "device": "computer", "country": "all"}
        if token:
            params["sortsPageToken"] = token
        data = await _get(c, EXPLORE, params)
        if not data:
            break
        for sort in data.get("sorts", []):
            for g in sort.get("games") or []:
                uid = g.get("universeId")
                if not uid or g.get("isSponsored"):
                    continue
                games[uid] = {
                    "name": g.get("name", "?"),
                    "place_id": g.get("rootPlaceId"),
                    "players": int(g.get("playerCount") or 0),
                    "genre": g.get("genreL1") or "",
                }
        token = data.get("nextSortsPageToken")
        if not token:
            break
    return games


async def fetch_details(c: httpx.AsyncClient, uids: list[int]) -> dict[int, dict]:
    """Онлайн, назва і дата створення для конкретних ігор (по 50 за запит)."""
    out: dict[int, dict] = {}
    for i in range(0, len(uids), 50):
        chunk = uids[i:i + 50]
        data = await _get(c, GAMES, {"universeIds": ",".join(map(str, chunk))})
        for g in (data or {}).get("data", []):
            out[g["id"]] = {
                "name": g.get("name", "?"),
                "place_id": g.get("rootPlaceId"),
                "players": int(g.get("playing") or 0),
                "created": g.get("created"),
            }
        await asyncio.sleep(0.3)
    return out


async def find_game(c: httpx.AsyncClient, query: str) -> int | None:
    """Назва, посилання або ID гри -> universeId."""
    query = query.strip()
    m = re.search(r"roblox\.com/games/(\d+)", query)
    if m:
        data = await _get(c, PLACE2UNI.format(m.group(1)))
        return (data or {}).get("universeId")
    if query.isdigit():
        uid = int(query)
        if await fetch_details(c, [uid]):
            return uid
        data = await _get(c, PLACE2UNI.format(uid))  # можливо це placeId
        return (data or {}).get("universeId")
    data = await _get(c, SEARCH, {"searchQuery": query, "sessionId": str(uuid.uuid4()), "pageType": "all"})
    for group in (data or {}).get("searchResults", []):
        if group.get("contentGroupType") != "Game":
            continue
        for g in group.get("contents", []):
            if g.get("universeId"):
                return g["universeId"]
    return None


async def fetch_icons(c: httpx.AsyncClient, uids: list[int]) -> dict[int, str]:
    """Іконки ігор (посилання на картинки)."""
    out: dict[int, str] = {}
    for i in range(0, len(uids), 50):
        chunk = uids[i:i + 50]
        data = await _get(c, ICONS, {"universeIds": ",".join(map(str, chunk)), "size": "150x150", "format": "Png"})
        for g in (data or {}).get("data", []):
            if g.get("imageUrl"):
                out[g["targetId"]] = g["imageUrl"]
    return out
