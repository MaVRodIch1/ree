"""
Six Seven Club (@Sixsevenclub_bot) backend client — fishing automation.

Pure HTTP (the caller passes the mini-app initData obtained via Telethon).
Auth: POST {fingerprint, initData} -> Bearer JWT (valid ~1h), also set as the
`accessToken` cookie. All other calls send that Bearer + an x-session-id header.

Confirmed endpoints (from live captures):
    GET  /fishing/state    -> current fishing state ("N/5" casts)
    GET  /user/balance     -> balance
Still TODO (need one more capture each — see AUTH_PATH / CAST_PATH):
    auth, cast (the request that returns {"cast_id": ...}), collect/reward.
"""
import uuid

import requests

BASE = "https://prod.6sixseven7.club/api/gateway/v1/public"

# Fingerprint the web app sends with auth. Tweak per-account if we want variety.
DEFAULT_FINGERPRINT = {
    "language": "ru",
    "platform": "tdesktop",
    "screen_width": 1920,
    "user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0"),
    "version": "9.6",
}

# Confirmed endpoints (all live-verified):
AUTH_PATH = "/auth"            # POST {fingerprint, initData} -> {"data":{"token":...}}
STATE_PATH = "/fishing/state"  # GET -> attempts + sources
CAST_PATH = "/fishing/casts"   # POST {} -> one catch (spends one attempt)
BALANCE_PATH = "/user/balance"
WALLET_BIND_PATH = "/wallet/bind"  # POST {address, network, proof, public_key, wallet_app}


def _headers(token: str, session_id: str) -> dict:
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "x-session-id": session_id,
        "origin": "https://prod.6sixseven7.club",
        "referer": "https://prod.6sixseven7.club/",
        "user-agent": DEFAULT_FINGERPRINT["user_agent"],
    }


class SixSeven:
    def __init__(self, init_data: str, fingerprint: dict | None = None):
        self.init_data = init_data
        self.fingerprint = fingerprint or DEFAULT_FINGERPRINT
        self.session_id = str(uuid.uuid4())
        self.token = None
        self.s = requests.Session()

    def auth(self) -> str:
        """POST {fingerprint, initData} -> Bearer token."""
        r = self.s.post(
            BASE + AUTH_PATH,
            json={"fingerprint": self.fingerprint, "initData": self.init_data},
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "origin": "https://prod.6sixseven7.club",
                "referer": "https://prod.6sixseven7.club/",
                "x-session-id": self.session_id,
                "user-agent": self.fingerprint["user_agent"],
            },
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        # Confirmed shape: {"data": {"token": "..."}, "success": true}
        self.token = ((data.get("data") or {}).get("token")
                      or data.get("token")
                      or self.s.cookies.get("accessToken"))
        if not self.token:
            raise RuntimeError(f"auth: no token in response: {data}")
        return self.token

    def _get(self, path: str):
        r = self.s.get(BASE + path, headers=_headers(self.token, self.session_id), timeout=30)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict | None = None):
        r = self.s.post(BASE + path, json=body or {},
                        headers=_headers(self.token, self.session_id), timeout=30)
        r.raise_for_status()
        return r.json() if r.text else {}

    def fishing_state(self):
        return self._get(STATE_PATH).get("data", {})

    def balance(self):
        return self._get(BALANCE_PATH).get("data", {})

    def cast(self):
        """Do one fishing cast (spends one attempt). Returns the catch data."""
        return self._post(CAST_PATH, {}).get("data", {})

    @staticmethod
    def attempts_left(state: dict) -> int:
        return int(state.get("free_attempts", 0)) + int(state.get("premium_attempts", 0))

    def bind_wallet(self, address_raw: str, public_key_hex: str, proof: dict,
                    network: str = "-239", wallet_app: str = "tonkeeper"):
        """Bind a generated TON wallet to the account (extra fishing attempts via
        the `hold` source). `proof` is a TON Connect ton_proof; its domain must be
        prod.6sixseven7.club. The backend uses `length_bytes` (snake_case)."""
        dom = proof.get("domain", {})
        proof_out = {
            "timestamp": proof["timestamp"],
            "domain": {
                "length_bytes": dom.get("lengthBytes", dom.get("length_bytes")),
                "value": dom.get("value"),
            },
            "signature": proof["signature"],
            "payload": proof["payload"],
        }
        return self._post(WALLET_BIND_PATH, {
            "address": address_raw,
            "network": network,
            "proof": proof_out,
            "public_key": public_key_hex,
            "wallet_app": wallet_app,
        }).get("data", {})
