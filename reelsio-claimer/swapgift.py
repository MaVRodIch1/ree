"""
SwapGift (@trade mini-app, prod-do.swapgift.live) — read the «Обмен» (exchange)
listing so new gifts can be announced the moment they appear.

GraphQL over POST /api/graphql/query (batched: the body is a JSON array).
Auth (from the frontend bundle):
    mutation AuthTelegramInitData($initData: String!, $refCode: String)
      -> { success token }        token goes in `Authorization: Bearer <token>`
Exchange tab = GetAssets with filter {forExchange: true}.
"""
import requests

BASE = "https://prod-do.swapgift.live"
API = BASE + "/api/graphql/query"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36 Edg/155.0.0.0")

AUTH_MUTATION = """mutation AuthTelegramInitData($initData: String!, $refCode: String) {
  authTelegramInitData(initData: $initData, refCode: $refCode) {
    success
    token
  }
}"""

GET_ASSETS = """query GetAssets($cursor: String, $limit: Int64!, $filter: AssetsFilter, $sort: [AssetsSort!]) {
  assets(cursor: $cursor, limit: $limit, filter: $filter, sort: $sort) {
    items {
      id
      name
      giftNumber
      url
      exchangePriceTon
      exchangePriceUsdt
      modelName
      modelRarityPermille
      backgroundName
      backgroundRarityPermille
      symbolName
      symbolRarityPermille
      isLocked
      lockedUntil
    }
    nextCursor
  }
}"""


class Unauthorized(Exception):
    """Token missing/expired — re-auth with fresh initData."""


class SwapGift:
    def __init__(self, init_data: str):
        self.init_data = init_data
        self.token = None
        self.s = requests.Session()
        self.s.headers.update({
            "user-agent": UA,
            "accept": "*/*",
            "content-type": "application/json",
            "origin": BASE,
            "referer": BASE + "/exchange",
            "x-batch": "true",
        })
        try:   # DDoS-Guard sets its __ddg* cookies on the first page load
            self.s.get(BASE + "/exchange", timeout=20)
        except Exception:
            pass

    def _gql(self, op: str, query: str, variables: dict) -> dict:
        headers = {"authorization": f"Bearer {self.token}"} if self.token else {}
        r = self.s.post(API, json=[{"operationName": op, "query": query,
                                    "variables": variables}],
                        headers=headers, timeout=20)
        if r.status_code == 401:
            raise Unauthorized("HTTP 401")
        r.raise_for_status()
        data = r.json()
        res = data[0] if isinstance(data, list) else data
        errs = res.get("errors") or []
        if errs:
            if any((e.get("extensions") or {}).get("code") == "UNAUTHORIZED" for e in errs):
                raise Unauthorized(errs[0].get("message", "UNAUTHORIZED"))
            raise RuntimeError(f"{op}: {errs[0].get('message')}")
        return res.get("data") or {}

    def auth(self) -> str:
        d = self._gql("AuthTelegramInitData", AUTH_MUTATION,
                      {"initData": self.init_data, "refCode": ""})
        tok = (d.get("authTelegramInitData") or {}).get("token")
        if not tok:
            raise RuntimeError(f"auth: no token in response: {d}")
        self.token = tok
        return tok

    def exchange_assets(self, page: int = 50, max_pages: int = 20) -> list:
        """Every gift currently in the «Обмен» tab (paged via nextCursor)."""
        items, cursor = [], ""
        for _ in range(max_pages):
            d = self._gql("GetAssets", GET_ASSETS, {
                "cursor": cursor, "limit": page,
                "sort": [{"field": "PRICE", "direction": "ASC"}],
                "filter": {"forExchange": True},
            })
            a = d.get("assets") or {}
            items.extend(a.get("items") or [])
            cursor = a.get("nextCursor")
            if not cursor:
                break
        return items


def _pct(permille):
    return f"{permille / 10:g}%" if isinstance(permille, (int, float)) else "—"


def format_new_items(items, ton_usd: float = 0.0, limit: int = 10) -> str:
    """HTML announcement for freshly-listed exchange gifts."""
    import html as _h
    head = ("🔔 <b>Новый подарок в Обмене</b>" if len(items) == 1
            else f"🔔 <b>Новые подарки в Обмене: {len(items)}</b>")
    lines = [head]
    for it in sorted(items, key=lambda x: x.get("exchangePriceTon") or 0)[:limit]:
        name = _h.escape(f"{it.get('name') or '?'} #{it.get('giftNumber') or '?'}")
        url = it.get("url")
        title = f'<a href="{_h.escape(url)}">{name}</a>' if url else name
        price = it.get("exchangePriceTon") or 0
        usd = it.get("exchangePriceUsdt") or (price * ton_usd if ton_usd else 0)
        usd_s = f" <i>(~${usd:,.0f})</i>".replace(",", " ") if usd else ""
        lock = " · 🔒 заблокирован" if it.get("isLocked") else ""
        lines.append(
            f"\n🎁 {title}\n"
            f"💎 <b>{price:g} TON</b>{usd_s}{lock}\n"
            f"<i>{_h.escape(str(it.get('modelName') or '—'))} {_pct(it.get('modelRarityPermille'))} · "
            f"{_h.escape(str(it.get('backgroundName') or '—'))} {_pct(it.get('backgroundRarityPermille'))} · "
            f"{_h.escape(str(it.get('symbolName') or '—'))} {_pct(it.get('symbolRarityPermille'))}</i>")
    if len(items) > limit:
        lines.append(f"\n…и ещё {len(items) - limit}")
    lines.append("\n👉 Обмен в @trade")
    return "\n".join(lines)
