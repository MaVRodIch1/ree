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

    def leaderboard(self, slug: str, count: int = 50):
        return self._get(f"/leaderboard/{slug}",
                         params={"offset": 0, "count": count, "get-finished": "false"})




def _num(x):
    try:
        return int(float(str(x).replace(" ", "").replace(",", "")))
    except Exception:
        return 0


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

