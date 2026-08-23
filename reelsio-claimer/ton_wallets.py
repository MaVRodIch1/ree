"""
TON wallet generation + secure local storage, one wallet per account.

Uses tonsdk to make STANDARD TON 24-word mnemonics (not BIP39) so the seeds
restore cleanly in Tonkeeper / Tonhub later. Seeds are the keys to real funds —
they are stored only in wallets.json (git-ignored) and never committed or sent
anywhere.

    pip install tonsdk
"""
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
WALLETS_FILE = BASE_DIR / "wallets.json"
# Plain-text backup laid out for easy manual import into a wallet app.
WALLETS_BACKUP = BASE_DIR / "wallets_backup.txt"


def load_wallets() -> dict:
    if WALLETS_FILE.exists():
        try:
            return json.loads(WALLETS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_wallets(wallets: dict):
    WALLETS_FILE.write_text(json.dumps(wallets, ensure_ascii=False, indent=2),
                            encoding="utf-8")


def write_backup(wallets: dict):
    """Human-readable backup: phone | address | 24 words."""
    lines = []
    for acct, w in sorted(wallets.items()):
        lines.append(f"{acct}\n  address: {w['address']}\n  seed:    {w['mnemonic']}\n")
    WALLETS_BACKUP.write_text("\n".join(lines) + "\n", encoding="utf-8")


def create_wallet(version: str = "v4r2"):
    """Generate one fresh TON wallet. Returns a dict with mnemonic/address/pubkey."""
    from tonsdk.crypto import mnemonic_new
    from tonsdk.contract.wallet import Wallets, WalletVersionEnum

    ver = {
        "v3r2": WalletVersionEnum.v3r2,
        "v4r2": WalletVersionEnum.v4r2,
    }.get(version, WalletVersionEnum.v4r2)

    mnemonic = mnemonic_new()
    mn, pub, priv, wallet = Wallets.from_mnemonics(mnemonic, ver, 0)
    return {
        "mnemonic": " ".join(mn),
        # Non-bounceable UQ… form — the right one for receiving to a fresh wallet.
        "address": wallet.address.to_string(True, True, False),
        "address_bounceable": wallet.address.to_string(True, True, True),
        "address_raw": wallet.address.to_string(False, False, False),
        "pubkey": pub.hex(),
        "version": version,
    }


def generate_for_accounts(account_names, version: str = "v4r2", overwrite: bool = False):
    """Ensure every account in `account_names` has a wallet. Existing wallets are
    kept (unless overwrite). Returns (wallets, created_count)."""
    wallets = load_wallets()
    created = 0
    for acct in account_names:
        if acct in wallets and not overwrite:
            continue
        wallets[acct] = create_wallet(version)
        created += 1
    if created:
        save_wallets(wallets)
        write_backup(wallets)
    return wallets, created
