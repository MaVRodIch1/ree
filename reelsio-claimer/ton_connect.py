"""
TON Connect v2 — the WALLET side of the bridge handshake.

Given a `tc://?v=2&id=<dapp_pub>&r=<connect-request>` link (what a dApp's
"Connect wallet" QR encodes), this connects one of our generated wallets:
builds a ton_addr + ton_proof response, encrypts it to the dApp with NaCl box,
and POSTs it to the TON Connect bridge. Fishing on Six Seven asks for no
payment, so only the connection (ton_proof auth) is needed — no transactions.

Deps: pynacl, tonsdk, requests.
"""
import base64
import hashlib
import json
import time
from urllib.parse import urlparse, parse_qs, unquote

import requests
from nacl.public import PrivateKey, PublicKey, Box
from nacl.signing import SigningKey

BRIDGE = "https://connect.ton.org/bridge"
MAINNET = "-239"
TESTNET = "-3"


# ── link parsing ────────────────────────────────────────────────────────────

def parse_tc_link(link: str) -> dict:
    """Parse a tc:// / universal TON Connect link into its parts."""
    q = parse_qs(urlparse(link.replace("tc://", "tc://x")).query)
    if "id" not in q or "r" not in q:
        # universal links keep the params after a real host; fall back to split
        _, _, rest = link.partition("?")
        q = parse_qs(rest)
    dapp_id = q["id"][0]
    req = json.loads(unquote(q["r"][0]))
    payload = None
    for it in req.get("items", []):
        if it.get("name") == "ton_proof":
            payload = it.get("payload", "")
    return {
        "dapp_id": dapp_id,
        "manifest_url": req.get("manifestUrl"),
        "items": req.get("items", []),
        "proof_payload": payload,
    }


def manifest_domain(manifest_url: str) -> str:
    """Domain the ton_proof must be bound to = host of the manifest's `url`."""
    try:
        data = requests.get(manifest_url, timeout=20).json()
        return urlparse(data["url"]).netloc
    except Exception:
        # fall back to the manifest host itself
        return urlparse(manifest_url).netloc


# ── wallet material from a stored mnemonic ──────────────────────────────────

def wallet_material(mnemonic: str, version: str = "v4r2"):
    """From a 24-word seed → signing key + address parts + stateInit (base64)."""
    from tonsdk.crypto import mnemonic_to_wallet_key
    from tonsdk.contract.wallet import Wallets, WalletVersionEnum

    words = mnemonic.split()
    pub, priv = mnemonic_to_wallet_key(words)
    ver = {"v3r2": WalletVersionEnum.v3r2, "v4r2": WalletVersionEnum.v4r2}.get(
        version, WalletVersionEnum.v4r2)
    _mn, _pub, _priv, wallet = Wallets.from_mnemonics(words, ver, 0)

    state_init = wallet.create_state_init()["state_init"]
    state_init_b64 = base64.b64encode(state_init.to_boc(False)).decode()

    addr = wallet.address
    return {
        "signing_key": SigningKey(bytes(priv[:32])),  # ed25519 seed
        "public_key": pub.hex(),
        "wc": addr.wc,
        "hash": bytes(addr.hash_part),
        "address_raw": f"{addr.wc}:{addr.hash_part.hex()}",
        "state_init_b64": state_init_b64,
    }


# ── ton_proof ────────────────────────────────────────────────────────────────

def build_ton_proof(mat: dict, domain: str, payload: str, ts: int | None = None) -> dict:
    """Sign the TON Connect ton-proof-item-v2 message for this wallet/domain."""
    ts = ts or int(time.time())
    domain_bytes = domain.encode()

    msg = b"ton-proof-item-v2/"
    msg += int(mat["wc"]).to_bytes(4, "big", signed=True)
    msg += mat["hash"]
    msg += len(domain_bytes).to_bytes(4, "little")
    msg += domain_bytes
    msg += ts.to_bytes(8, "little")
    msg += payload.encode()

    full = b"\xff\xff" + b"ton-connect" + hashlib.sha256(msg).digest()
    digest = hashlib.sha256(full).digest()
    signature = mat["signing_key"].sign(digest).signature

    return {
        "timestamp": ts,
        "domain": {"lengthBytes": len(domain_bytes), "value": domain},
        "signature": base64.b64encode(signature).decode(),
        "payload": payload,
    }


def build_connect_event(mat: dict, proof: dict, network: str = MAINNET) -> dict:
    """The wallet→dApp `connect` success event."""
    return {
        "event": "connect",
        "id": 0,
        "payload": {
            "items": [
                {
                    "name": "ton_addr",
                    "address": mat["address_raw"],
                    "network": network,
                    "publicKey": mat["public_key"],
                    "walletStateInit": mat["state_init_b64"],
                },
                {"name": "ton_proof", "proof": proof},
            ],
            "device": {
                "platform": "windows",
                "appName": "tonkeeper",
                "appVersion": "4.0.0",
                "maxProtocolVersion": 2,
                "features": [{"name": "SendTransaction", "maxMessages": 4}],
            },
        },
    }


# ── bridge transport (NaCl box) ─────────────────────────────────────────────

class Session:
    """Ephemeral wallet-side bridge session (its own NaCl keypair)."""

    def __init__(self):
        self.sk = PrivateKey.generate()
        self.client_id = bytes(self.sk.public_key).hex()

    def encrypt_for(self, dapp_pub_hex: str, message: str) -> str:
        box = Box(self.sk, PublicKey(bytes.fromhex(dapp_pub_hex)))
        enc = box.encrypt(message.encode())  # nonce(24) + ciphertext
        return base64.b64encode(enc).decode()

    def send(self, dapp_pub_hex: str, message: str, topic: str = "connect", ttl: int = 300):
        body = self.encrypt_for(dapp_pub_hex, message)
        url = f"{BRIDGE}/message"
        params = {"client_id": self.client_id, "to": dapp_pub_hex,
                  "ttl": ttl, "topic": topic}
        r = requests.post(url, params=params, data=body,
                          headers={"Content-Type": "text/plain"}, timeout=30)
        r.raise_for_status()
        return r.text


def connect_wallet(link: str, mnemonic: str, version: str = "v4r2",
                   network: str = MAINNET) -> dict:
    """Full connect: parse link → sign proof → encrypt → POST to bridge.
    Returns a summary dict. Raises on transport error."""
    info = parse_tc_link(link)
    domain = manifest_domain(info["manifest_url"]) if info["manifest_url"] else ""
    mat = wallet_material(mnemonic, version)
    proof = build_ton_proof(mat, domain, info["proof_payload"] or "")
    event = build_connect_event(mat, proof, network)
    sess = Session()
    resp = sess.send(info["dapp_id"], json.dumps(event), topic="connect")
    return {
        "address": mat["address_raw"],
        "domain": domain,
        "wallet_client_id": sess.client_id,
        "bridge_response": resp,
    }
