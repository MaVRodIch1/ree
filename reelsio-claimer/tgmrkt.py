"""
tgmrkt.io (@mrkt) client — read the PlayHub/contest leaderboard (Top 50).

Auth: POST /api/v1/auth  {data: <mini-app initData>, photo: <userpic url>, appId: null}
      -> {"token": "..."} ; the token goes in the access_token cookie AND the
      Authorization header (raw, no "Bearer").
Leaderboard (confirmed live):
      GET /api/v1/leaderboard/<slug>?offset=0&count=50&get-finished=false
      -> {top100:[{name,position,points,picture,isMe}], me:{...}, totalPoints, timeRange}
"""
import requests

BASE = "https://api.tgmrkt.io/api/v1"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0")


def _headers(json_body: bool = False) -> dict:
    h = {
        "accept": "application/json, text/plain, */*",
        "origin": "https://cdn.tgmrkt.io",
        "referer": "https://cdn.tgmrkt.io/",
        "user-agent": UA,
    }
    if json_body:
        h["content-type"] = "application/json"
    return h


class TgMrkt:
    def __init__(self, init_data: str, photo: str | None = None):
        self.init_data = init_data
        self.photo = photo
        self.s = requests.Session()
        self.token = None

    def auth(self) -> str:
        r = self.s.post(BASE + "/auth",
                        json={"data": self.init_data, "photo": self.photo, "appId": None},
                        headers=_headers(json_body=True), timeout=30)
        r.raise_for_status()
        self.token = r.json().get("token")
        # The app sends the raw token in Authorization (no "Bearer") + cookie.
        if self.token:
            self.s.headers["Authorization"] = self.token
        return self.token

    def _get(self, path: str, params=None):
        r = self.s.get(BASE + path, headers=_headers(), params=params, timeout=30)
        if not r.ok:
            raise RuntimeError(f"GET {path} {r.status_code}: {r.text[:200]}")
        return r.json()

    def _post(self, path: str, body: dict):
        r = self.s.post(BASE + path, headers=_headers(json_body=True),
                        json=body, timeout=30)
        if not r.ok:
            raise RuntimeError(f"POST {path} {r.status_code}: {r.text[:200]}")
        return r.json()

    def leaderboard(self, slug: str, count: int = 50):
        return self._get(f"/leaderboard/{slug}",
                         params={"offset": 0, "count": count, "get-finished": "false"})

    # ── PvP win/loss (real net in TON) ──────────────────────────────────────
    def game_rooms(self):
        return self._get("/pvp/game-rooms")

    def pvp_history(self, game_room_id, cursor="", count=20):
        return self._post("/pvp/history", {
            "count": count, "cursor": cursor, "gameRoomId": game_room_id,
            "lowToHigh": False, "ordering": "FinishTime",
            "minWinnings": None, "maxWinnings": None,
        })

    def pvp_pnl(self, since_iso: str | None = None, max_games: int = 20000):
        """Aggregate real PvP net per player across game history.
        Scans newest→oldest and stops a room once games are older than
        `since_iso` (the contest start). Resilient: a failed page returns the
        partial aggregate instead of raising, so PvP data is never lost wholesale.
        Returns {name: {net_ton, bet_ton, won_ton, games}}, plus "_scanned"."""
        try:
            rooms = _room_ids(self.game_rooms())
        except Exception:
            rooms = []
        agg = {}
        seen = 0
        for rid in rooms:
            cursor = ""
            stop_room = False
            pages = 0
            while seen < max_games and not stop_room and pages < 2000:
                pages += 1
                try:
                    data = self.pvp_history(rid, cursor)
                except Exception:
                    break  # keep what we have, move to next room
                games = data.get("pvpGameHistoryDtos") or []
                if not games:
                    break
                for g in games:
                    if since_iso:
                        cad = g.get("createdAt") or ""
                        if cad and cad < since_iso:
                            stop_room = True
                            break
                    seen += 1
                    win = g.get("winner") or {}
                    pot = g.get("totalWinNanoTONs") or 0
                    wname = win.get("publicName")
                    if wname:
                        agg.setdefault(wname, [0, 0, 0])[1] += pot  # won
                    for p in g.get("participants") or []:
                        nm = p.get("publicName")
                        if not nm:
                            continue
                        contrib = (p.get("totalBetNanoTONs") or 0) + (p.get("totalGiftBetsPrice") or 0)
                        a = agg.setdefault(nm, [0, 0, 0])
                        a[0] += contrib  # bet
                        a[2] += 1        # games
                    if seen >= max_games:
                        break
                cursor = data.get("cursor") or ""
                if not cursor:
                    break
        out = {nm: {"bet_ton": bet / 1e9, "won_ton": won / 1e9,
                    "net_ton": (won - bet) / 1e9, "games": n}
               for nm, (bet, won, n) in agg.items()}
        out["_scanned"] = seen
        return out







def _num(x):
    try:
        return int(float(str(x).replace(" ", "").replace(",", "")))
    except Exception:
        return 0


def _room_ids(payload):
    """Extract game-room ids from /pvp/game-rooms (tolerant of shape)."""
    rooms = payload
    if isinstance(payload, dict):
        for key in ("gameRooms", "rooms", "items", "data", "results"):
            if isinstance(payload.get(key), list):
                rooms = payload[key]
                break
    ids = []
    if isinstance(rooms, list):
        for r in rooms:
            if isinstance(r, dict):
                rid = r.get("id") or r.get("gameRoomId") or r.get("roomId")
                if rid:
                    ids.append(rid)
            elif isinstance(r, str):
                ids.append(r)
    return ids


def extract_rows(payload):
    """Normalise the leaderboard payload to [{rank,id,name,score}]."""
    rows = payload
    if isinstance(payload, dict):
        for key in ("top100", "top", "items", "leaderboard", "ranking",
                    "list", "data", "results", "users", "members"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
    out = []
    if isinstance(rows, list):
        for i, e in enumerate(rows, 1):
            if not isinstance(e, dict):
                continue
            rank = e.get("position") or e.get("rank") or e.get("place") or i
            name = (e.get("name") or e.get("username") or e.get("userName")
                    or e.get("title") or "—")
            score = (e.get("points") or e.get("score") or e.get("gram")
                     or e.get("amount") or e.get("value") or 0)
            out.append({"rank": _num(rank), "id": str(name), "name": name,
                        "score": _num(score)})
    return out


def _grp(n):
    return f"{n:,}".replace(",", " ")


def format_leaderboard(rows, prev=None, usd_per_point=0.0,
                       title="🏆 Топ лидерборда", limit=50,
                       pnl=None, ton_usd=0.0) -> str:
    """Render the leaderboard with an hourly gain (vs `prev` by name) and either
    a real PvP net (from `pnl`, in TON/$) or a rough points-based spend."""
    prev = prev or {}
    pnl = pnl or {}
    lines = [title]
    for r in rows[:limit]:
        rk = r["rank"] if isinstance(r["rank"], int) else 0
        head = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rk, f"{r['rank']}.")
        delta = ""
        if r["id"] in prev:
            d = r["score"] - prev[r["id"]]
            delta = f" (+{_grp(d)}/ч)" if d > 0 else (" (0/ч)" if d == 0 else f" ({_grp(d)}/ч)")
        tail = ""
        p = pnl.get(r["name"])
        if p is not None:
            net = p["net_ton"]
            usd = f" (${_grp(round(net * ton_usd))})" if ton_usd else ""
            sign = "+" if net >= 0 else ""
            tail = f" | PvP {sign}{net:.1f} TON{usd}"
        elif usd_per_point > 0:
            tail = f" ~${_grp(round(r['score'] * usd_per_point))}"
        lines.append(f"{head} {r['name']} — {_grp(r['score'])}{delta}{tail}")
    if pnl:
        lines.append("\n(PvP — реальный нетто по истории игр; +выиграл / −проиграл)")
    elif usd_per_point > 0:
        lines.append("\n(траты — грубая оценка по очкам)")
    return "\n".join(lines)


