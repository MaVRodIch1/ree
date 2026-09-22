"""
tgmrkt.io (@mrkt) client — read the PlayHub/contest leaderboard (Top 50).

Auth: POST /api/v1/auth  {data: <mini-app initData>, photo: <userpic url>, appId: null}
      -> {"token": "..."} and an access_token cookie (kept in the session).
Leaderboard (found in the app bundle team-events.queries):
      GET /api/v1/team-events/active                     -> active events (leaderboardId)
      GET /api/v1/team-events/ranking?leaderboardId=<id> -> the Top-50 ranking
"""
import requests

BASE = "https://api.tgmrkt.io/api/v1"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0")


def _headers(json_body: bool = False) -> dict:
    h = {
        "accept": "*/*",
        "origin": "https://cdn.tgmrkt.io",
        "referer": "https://cdn.tgmrkt.io/",
        "user-agent": UA,
    }
    if json_body:
        h["content-type"] = "application/json"
    return h


def _find_leaderboard_id(active):
    """Pull a leaderboardId out of the /team-events/active response."""
    def from_item(it):
        if isinstance(it, dict):
            return (it.get("leaderboardId") or it.get("id")
                    or (it.get("leaderboard") or {}).get("id"))
        return None
    if isinstance(active, list):
        for it in active:
            lid = from_item(it)
            if lid:
                return lid
    elif isinstance(active, dict):
        lid = from_item(active)
        if lid:
            return lid
        for key in ("items", "events", "data", "active"):
            if isinstance(active.get(key), list):
                for it in active[key]:
                    lid = from_item(it)
                    if lid:
                        return lid
    return None


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
        # Auth is cookie-based (the app uses credentials:include). Do NOT set an
        # Authorization header — a raw token there makes the API 400.
        return self.token

    def _get(self, path: str, **params):
        r = self.s.get(BASE + path, headers=_headers(),
                       params=params or None, timeout=30)
        if not r.ok:
            raise RuntimeError(f"GET {path} {r.status_code}: {r.text[:200]}")
        return r.json()

    def active_events(self):
        return self._get("/team-events/active")

    def ranking(self, leaderboard_id):
        return self._get("/team-events/ranking", leaderboardId=leaderboard_id)

    def top_leaderboard(self, leaderboard_id=None):
        """Return the Top-50 ranking. If leaderboard_id is given, call /ranking
        directly (works even when /active is NOT_ALLOWED for this account);
        otherwise discover it via /team-events/active."""
        lid = leaderboard_id or _find_leaderboard_id(self.active_events())
        if not lid:
            raise RuntimeError("no leaderboardId (set TOP_LEADERBOARD_ID)")
        return self.ranking(lid)



def _num(x):
    try:
        return int(float(str(x).replace(" ", "").replace(",", "")))
    except Exception:
        return 0


def extract_rows(payload):
    """Normalise the ranking payload to [{rank,id,name,score}], tolerant of the
    exact field names until the real response shape is confirmed."""
    rows = payload
    if isinstance(payload, dict):
        for key in ("items", "leaderboard", "list", "data", "ranking",
                    "top", "results", "users", "members"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
    out = []
    if isinstance(rows, list):
        for i, e in enumerate(rows, 1):
            if not isinstance(e, dict):
                continue
            user = e.get("user") if isinstance(e.get("user"), dict) else {}
            rank = e.get("rank") or e.get("position") or e.get("place") or i
            uid = (e.get("userId") or e.get("id") or user.get("id")
                   or e.get("username") or user.get("username"))
            name = (e.get("username") or e.get("name") or user.get("username")
                    or user.get("name") or user.get("firstName") or str(uid))
            score = (e.get("score") or e.get("points") or e.get("gram")
                     or e.get("amount") or e.get("value") or e.get("balance") or 0)
            out.append({"rank": rank, "id": str(uid), "name": name, "score": _num(score)})
    return out


def _grp(n):
    return f"{n:,}".replace(",", " ")


def format_leaderboard(rows, prev=None, usd_per_point=0.0,
                       title="🏆 Топ лидерборда", limit=50) -> str:
    """Render the leaderboard with an hourly gain (vs `prev` scores by id) and an
    estimated spend (score * usd_per_point)."""
    prev = prev or {}
    lines = [title]
    for r in rows[:limit]:
        rk = r["rank"] if isinstance(r["rank"], int) else 0
        head = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rk, f"{r['rank']}.")
        delta = ""
        if r["id"] in prev:
            d = r["score"] - prev[r["id"]]
            delta = f" (+{_grp(d)}/ч)" if d > 0 else (" (0/ч)" if d == 0 else f" ({_grp(d)}/ч)")
        spend = f" ~${_grp(round(r['score'] * usd_per_point))}" if usd_per_point > 0 else ""
        lines.append(f"{head} {r['name']} — {_grp(r['score'])}{delta}{spend}")
    if usd_per_point > 0:
        lines.append("\n(траты — грубая оценка по очкам)")
    return "\n".join(lines)

