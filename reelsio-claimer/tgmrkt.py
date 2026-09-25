"""
tgmrkt.io (@mrkt) client — read the PlayHub/contest leaderboard (Top 50).

Auth: POST /api/v1/auth  {data: <mini-app initData>, photo: <userpic url>, appId: null}
      -> {"token": "..."} ; the token goes in the access_token cookie AND the
      Authorization header (raw, no "Bearer").
Leaderboard (confirmed live):
      GET /api/v1/leaderboard/<slug>?offset=0&count=50&get-finished=false
      -> {top100:[{name,position,points,picture,isMe}], me:{...}, totalPoints, timeRange}
"""
import html as _html
import time

import requests

BASE = "https://api.tgmrkt.io/api/v1"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0")

# Per-player PvP record layout (all nanoTON):
#   [0] staked      own bet (gift value + TON), every game
#   [1] won         pot received when this player is the winner
#   [2] games       games played
#   [3] wins        games won
#   [4] gift_took   gift value TAKEN FROM OPPONENTS when winning
#                   (= gift pot minus the winner's own gift bet — a returned
#                    own gift is NOT a win)
#   [5] ton_took    TON taken from opponents when winning (= ton pot − own ton bet)
#   [6] gift_bled   own gift value lost to the winner in games lost
#   [7] ton_bled    own TON lost to the winner in games lost
# Bump PVP_STATE_VERSION when the layout changes so old cache files rebuild
# cleanly instead of reading stale/short records.
#   v2 added gift_won; v3 split winnings into "taken from opponents" vs the
#   player's own returned stake, and tracks what each player bled.
PVP_STATE_VERSION = 3
PVP_RECORD_LEN = 8


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

    def active_leaderboard_slugs(self):
        """Best-effort discovery of the current contest's leaderboard slug(s).

        Slugs rotate each tournament (e.g. playhub_hot_week → dead next contest).
        The web app finds the live one via /team-events/active, whose events each
        carry the leaderboard key. We dig any string that looks like a slug out of
        that response so the tracker can self-heal when the slug changes. Returns a
        de-duplicated list, best-guess first; empty if discovery isn't available."""
        try:
            data = self._get("/team-events/active")
        except Exception:
            return []
        found, seen = [], set()

        def want(k):
            return any(t in k.lower() for t in
                       ("slug", "leaderboard", "key", "code", "name", "id"))

        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if isinstance(v, str) and want(k) and _looks_like_slug(v):
                        if v not in seen:
                            seen.add(v)
                            found.append(v)
                    else:
                        walk(v)
            elif isinstance(node, list):
                for it in node:
                    walk(it)

        walk(data)
        # Prefer slugs that look leaderboard-ish (contain a word separator and a
        # known hint) over bare ids.
        found.sort(key=lambda s: (0 if any(h in s.lower() for h in
                   ("playhub", "hot", "week", "contest", "season", "hub")) else 1))
        return found

    def leaderboard_auto(self, preferred: str | None = None, count: int = 50):
        """Fetch the leaderboard, healing a stale slug automatically.

        Tries `preferred` first; on failure, discovers active slugs and tries
        each. Returns (board, slug_used). Raises the last error if nothing works.
        """
        tried, last_err = [], None
        candidates = ([preferred] if preferred else []) + self.active_leaderboard_slugs()
        for slug in candidates:
            if not slug or slug in tried:
                continue
            tried.append(slug)
            try:
                return self.leaderboard(slug, count), slug
            except Exception as e:
                last_err = e
        raise RuntimeError(
            f"no working leaderboard slug (tried {tried or 'none'}): {last_err}")

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

    def accumulate_pvp(self, state: dict, since_iso: str | None = None,
                       deadline_s: float = 60, max_new: int = 8000) -> int:
        """Incrementally fold PvP history into a persistent `state`, counting
        each game exactly once. Per room we remember the counted id band
        [low, high]: new games (id>high) are added every run; older games
        (id<low) are backfilled a bit each run until `since_iso`. This keeps
        totals monotonic and fast (no full re-scan). Returns games added."""
        started = time.time()
        # Auto-heal older cache files: if the record layout changed, drop the
        # aggregates and re-scan the whole contest cleanly (the caller holds the
        # first post until the rebuild completes). This is what populates
        # gift_won for the whole tournament instead of leaving it at 0.
        if state.get("v") != PVP_STATE_VERSION:
            state["players"] = {}
            state["rooms"] = {}
            state["v"] = PVP_STATE_VERSION
        players = state.setdefault("players", {})  # name -> record (see layout above)
        # Belt-and-suspenders: pad any short record so no index ever IndexErrors.
        for _rec in players.values():
            while len(_rec) < PVP_RECORD_LEN:
                _rec.append(0)

        def rec(nm):
            r = players.setdefault(nm, [0] * PVP_RECORD_LEN)
            while len(r) < PVP_RECORD_LEN:
                r.append(0)
            return r

        rooms_state = state.setdefault("rooms", {})
        try:
            rooms = _room_ids(self.game_rooms())
        except Exception:
            rooms = []
        total_new = 0

        def agg(g):
            win = g.get("winner") or {}
            wname = win.get("publicName")
            pot = g.get("totalWinNanoTONs") or 0
            gift_pot = g.get("totalGiftWinNanoTONs") or 0
            ton_pot = g.get("totalTonWinNanoTONs") or 0
            parts = g.get("participants") or []
            # The winner's OWN stake returns to them on a win — not "won from
            # others". Find it so we can subtract it out.
            win_gift_bet = win_ton_bet = 0
            for p in parts:
                if p.get("publicName") == wname:
                    win_gift_bet = p.get("totalGiftBetsPrice") or 0
                    win_ton_bet = p.get("totalBetNanoTONs") or 0
            for p in parts:
                nm = p.get("publicName")
                if not nm:
                    continue
                gbet = p.get("totalGiftBetsPrice") or 0
                tbet = p.get("totalBetNanoTONs") or 0
                a = rec(nm)
                a[0] += gbet + tbet
                a[2] += 1
                if nm != wname:          # this player lost — their stake bled to the winner
                    a[6] += gbet
                    a[7] += tbet
            if wname:
                w = rec(wname)
                w[1] += pot
                w[3] += 1
                # Value actually taken FROM OPPONENTS (own returned stake excluded).
                w[4] += max(0, gift_pot - win_gift_bet)
                w[5] += max(0, ton_pot - win_ton_bet)

        for rid in rooms:
            rs = rooms_state.setdefault(rid, {"high": 0, "low": None, "done": False})
            high, low, done = rs["high"], rs["low"], rs.get("done", False)
            new_high, new_low = high, low
            cursor, pages, stop = "", 0, False
            while not stop and pages < 5000:
                if time.time() - started > deadline_s or total_new >= max_new:
                    break
                pages += 1
                try:
                    data = self.pvp_history(rid, cursor)
                except Exception:
                    break
                games = data.get("pvpGameHistoryDtos") or []
                if not games:
                    done = True
                    break
                for g in games:
                    gid = g.get("id") or 0
                    cad = g.get("createdAt") or ""
                    if since_iso and cad and cad < since_iso:
                        done = True
                        stop = True
                        break
                    in_band = (low is not None and low <= gid <= high)
                    if in_band:
                        if done:            # nothing new below → stop early
                            stop = True
                            break
                        continue            # page through counted band to backfill
                    agg(g)
                    new_high = gid if gid > new_high else new_high
                    new_low = gid if (new_low is None or gid < new_low) else new_low
                    total_new += 1
                    if total_new >= max_new:
                        stop = True
                        break
                cursor = data.get("cursor") or ""
                if not cursor:
                    done = True
                    break
            rs["high"], rs["low"], rs["done"] = new_high, new_low, done
        return total_new


def pnl_from_state(state: dict) -> dict:
    """Per-player PvP view, in TON:
      net_ton     overall profit/loss (won − staked)
      took_ton    value TAKEN FROM OPPONENTS when winning (free material) —
                  gift_took_ton + ton_took_ton, own returned stake excluded
      bled_ton    value LOST to opponents in games lost (gift_bled + ton_bled)
      gift_took_ton / ton_took_ton / gift_bled_ton / ton_bled_ton  breakdown
    """
    def g(v, i):
        return v[i] if len(v) > i else 0
    out = {}
    for nm, v in (state.get("players") or {}).items():
        bet, won, n, wins = v[0], v[1], v[2], v[3]
        gift_took, ton_took = g(v, 4), g(v, 5)
        gift_bled, ton_bled = g(v, 6), g(v, 7)
        out[nm] = {
            "bet_ton": bet / 1e9, "won_ton": won / 1e9,
            "net_ton": (won - bet) / 1e9, "games": n, "wins": wins,
            "winrate": (100 * wins / n) if n else 0,
            "took_ton": (gift_took + ton_took) / 1e9,
            "bled_ton": (gift_bled + ton_bled) / 1e9,
            "gift_took_ton": gift_took / 1e9, "ton_took_ton": ton_took / 1e9,
            "gift_bled_ton": gift_bled / 1e9, "ton_bled_ton": ton_bled / 1e9,
        }
    return out








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


def _looks_like_slug(v: str) -> bool:
    """A leaderboard slug is a short lowercase token like 'playhub_hot_week' — not
    a UUID (has dashes but also digits/hex in 8-4-4-4-12 form) and not a URL."""
    if not isinstance(v, str):
        return False
    s = v.strip()
    if not (3 <= len(s) <= 64) or "/" in s or " " in s:
        return False
    # reject UUIDs (…-…-…-…-…, all hex)
    if s.count("-") >= 4 and all(c in "0123456789abcdef-" for c in s.lower()):
        return False
    return all(c.isalnum() or c in "_-" for c in s) and any(c.isalpha() for c in s)


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
    """HTML message: a monospace table (rank · name · points · hourly gain ·
    PvP net TON · win%). Send with parse_mode='html'."""
    prev = prev or {}
    pnl = pnl or {}

    def row(rank, name, pts, delta, net, wr):
        return f"{rank:<2} {name:<13} {pts:>7} {delta:>6} {net:>6} {wr:>4}"

    table = [row("#", "Игрок", "Очки", "Δ/ч", "PvP", "WR%")]
    for r in rows[:limit]:
        name = r["name"]
        name = (name[:12] + "…") if len(name) > 13 else name
        delta = ""
        if r["id"] in prev:
            g = r["score"] - prev[r["id"]]
            delta = f"{g:+d}" if g else "0"
        p = pnl.get(r["name"])
        if p and p.get("games"):
            net = f"{round(p['net_ton']):+d}"
            wr = f"{round(p['winrate'])}%"
        else:
            net, wr = "—", "—"
        table.append(row(r["rank"], name, r["score"], delta, net, wr))

    body = "<pre>" + _html.escape("\n".join(table)) + "</pre>"
    note = ("<i>Очки — фарм заданий · Δ/ч — прирост за час · "
            "PvP — нетто в TON (+выиграл/−слил) · WR — винрейт</i>")
    return f"<b>{_html.escape(title)}</b>\n{body}\n{note}"



