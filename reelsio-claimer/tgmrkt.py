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


def _headers() -> dict:
    return {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": "https://cdn.tgmrkt.io",
        "referer": "https://cdn.tgmrkt.io/",
        "user-agent": UA,
    }


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
                        headers=_headers(), timeout=30)
        r.raise_for_status()
        self.token = r.json().get("token")
        if self.token:
            self.s.headers["App-Token"] = self.token
            self.s.headers["Authorization"] = self.token
        return self.token

    def _get(self, path: str, **params):
        r = self.s.get(BASE + path, headers=_headers(),
                       params=params or None, timeout=30)
        r.raise_for_status()
        return r.json()

    def active_events(self):
        return self._get("/team-events/active")

    def ranking(self, leaderboard_id):
        return self._get("/team-events/ranking", leaderboardId=leaderboard_id)

    def top_leaderboard(self):
        """Auth-scoped: find the active event, return its Top-50 ranking."""
        lid = _find_leaderboard_id(self.active_events())
        if not lid:
            raise RuntimeError("no active leaderboard event")
        return self.ranking(lid)



def format_top(payload, limit: int = 50, title: str = "🏆 Топ лидерборда") -> str:
    """Format the leaderboard into a Telegram message. Tolerant of field names
    until the real response shape is confirmed (rank/name/score variants)."""
    # Find the list of entries inside the payload.
    rows = payload
    if isinstance(payload, dict):
        for key in ("items", "leaderboard", "list", "data", "top", "results", "users"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
    if not isinstance(rows, list):
        return f"{title}\n(не удалось разобрать ответ лидерборда)"

    lines = [title]
    for i, e in enumerate(rows[:limit], 1):
        if not isinstance(e, dict):
            continue
        rank = e.get("rank") or e.get("position") or e.get("place") or i
        name = (e.get("username") or e.get("name") or e.get("userName")
                or e.get("title") or e.get("displayName") or "—")
        score = (e.get("score") or e.get("points") or e.get("gram")
                 or e.get("amount") or e.get("value") or "")
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(int(rank) if str(rank).isdigit() else 0, "")
        lines.append(f"{medal or str(rank) + '.'} {name} — {score}")
    return "\n".join(lines)
