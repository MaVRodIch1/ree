"""
tgmrkt.io (@mrkt) client — read the PlayHub/contest leaderboard (Top 50).

Auth: POST /api/v1/auth  {data: <mini-app initData>, photo: <userpic url>, appId: null}
      -> {"token": "..."} ; the token goes in the access_token cookie AND the
      Authorization header (raw, no "Bearer").
Leaderboard (confirmed live):
      GET /api/v1/leaderboard/<slug>?offset=0&count=50&get-finished=false
      -> {top100:[{name,position,points,picture,isMe}], me:{...}, totalPoints, timeRange}
"""
import time

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

    def scan_pvp(self, since_iso: str | None = None, h2h_pair=None,
                 max_games: int = 20000, deadline_s: float | None = None):
        """Single pass over PvP history (newest→oldest, stops per room once older
        than since_iso). Bounded by max_games and an optional wall-clock
        deadline_s so it never blocks the hourly post. Returns
        (pnl_dict, h2h_dict_or_None); pnl carries "_scanned" and "_complete"."""
        started = time.time()
        try:
            rooms = _room_ids(self.game_rooms())
        except Exception:
            rooms = []
        agg = {}
        seen = 0
        complete = True
        a = b = None
        overall = duel = None
        if h2h_pair:
            a, b = h2h_pair
            overall, duel = _blank_h2h(), _blank_h2h()

        for rid in rooms:
            cursor, stop_room, pages = "", False, 0
            while seen < max_games and not stop_room and pages < 2000:
                if deadline_s and time.time() - started > deadline_s:
                    complete = False
                    stop_room = True
                    break
                pages += 1
                try:
                    data = self.pvp_history(rid, cursor)
                except Exception:
                    complete = False
                    break
                games = data.get("pvpGameHistoryDtos") or []
                if not games:
                    break
                for g in games:
                    if since_iso and (g.get("createdAt") or "") < since_iso:
                        stop_room = True
                        break
                    seen += 1
                    win = g.get("winner") or {}
                    wname = win.get("publicName")
                    pot = g.get("totalWinNanoTONs") or 0
                    parts = {}
                    for p in g.get("participants") or []:
                        nm = p.get("publicName")
                        if not nm:
                            continue
                        contrib = (p.get("totalBetNanoTONs") or 0) + (p.get("totalGiftBetsPrice") or 0)
                        parts[nm] = contrib
                        ag = agg.setdefault(nm, [0, 0, 0, 0])
                        ag[0] += contrib
                        ag[2] += 1
                    if wname:
                        w = agg.setdefault(wname, [0, 0, 0, 0])
                        w[1] += pot   # won amount
                        w[3] += 1     # win count
                    if h2h_pair and a in parts and b in parts:
                        _acc_h2h(overall, a, b, wname, pot, parts[a], parts[b])
                        if len(parts) == 2:
                            _acc_h2h(duel, a, b, wname, pot, parts[a], parts[b])
                    if seen >= max_games:
                        break
                cursor = data.get("cursor") or ""
                if not cursor:
                    break
            if not complete and deadline_s and time.time() - started > deadline_s:
                break  # out of time — stop scanning further rooms too

        pnl = {nm: {"bet_ton": bet / 1e9, "won_ton": won / 1e9,
                    "net_ton": (won - bet) / 1e9, "games": n, "wins": w,
                    "winrate": (100 * w / n) if n else 0}
               for nm, (bet, won, n, w) in agg.items()}
        pnl["_scanned"] = seen
        pnl["_complete"] = complete
        h2h = None
        if h2h_pair:
            h2h = {"me": a, "opp": b, "scanned": seen,
                   "overall": _h2h_ton(overall), "duel": _h2h_ton(duel)}
        return pnl, h2h

    def pvp_pnl(self, since_iso: str | None = None, max_games: int = 20000):
        return self.scan_pvp(since_iso, None, max_games)[0]

    def pvp_head_to_head(self, me: str, opp: str, since_iso: str | None = None,
                         max_games: int = 20000):
        return self.scan_pvp(since_iso, (me, opp), max_games)[1]








def _num(x):
    try:
        return int(float(str(x).replace(" ", "").replace(",", "")))
    except Exception:
        return 0


def _blank_h2h():
    return {"games": 0, "my_wins": 0, "opp_wins": 0, "other_wins": 0,
            "a_to_b": 0, "b_to_a": 0, "my_net": 0, "opp_net": 0}


def _acc_h2h(bucket, a, b, winner, pot, a_c, b_c):
    bucket["games"] += 1
    if winner == a:
        bucket["my_wins"] += 1
        bucket["b_to_a"] += b_c
    elif winner == b:
        bucket["opp_wins"] += 1
        bucket["a_to_b"] += a_c
    else:
        bucket["other_wins"] += 1
    bucket["my_net"] += (pot if winner == a else 0) - a_c
    bucket["opp_net"] += (pot if winner == b else 0) - b_c


def _h2h_ton(b):
    return {"games": b["games"], "my_wins": b["my_wins"],
            "opp_wins": b["opp_wins"], "other_wins": b["other_wins"],
            "a_to_b_ton": b["a_to_b"] / 1e9, "b_to_a_ton": b["b_to_a"] / 1e9,
            "my_net_ton": b["my_net"] / 1e9, "opp_net_ton": b["opp_net"] / 1e9}


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


def ton_usd_rate(fallback=3.0):
    """Live TON→USD price (CoinGecko), with a fallback constant."""
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "the-open-network", "vs_currencies": "usd"},
            timeout=15)
        v = r.json().get("the-open-network", {}).get("usd")
        return float(v) if v else fallback
    except Exception:
        return fallback


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
            wr = f"{p['winrate']:.0f}% из {p['games']}" if p.get("games") else "нет игр"
            tail = f" | PvP {wr} · {sign}{net:.1f} TON{usd}"
        elif usd_per_point > 0:
            tail = f" ~${_grp(round(r['score'] * usd_per_point))}"
        lines.append(f"{head} {r['name']} — {_grp(r['score'])}{delta}{tail}")
    if pnl:
        lines.append("\n(PvP: винрейт из N игр · нетто TON; + выиграл / − проиграл)")
    elif usd_per_point > 0:
        lines.append("\n(траты — грубая оценка по очкам)")
    return "\n".join(lines)


