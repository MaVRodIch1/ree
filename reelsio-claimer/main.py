import asyncio
import json
import logging
import random
import signal
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import aiohttp
from telethon import TelegramClient, errors, functions, types

BASE_DIR = Path(__file__).resolve().parent
SESSIONS_DIR = BASE_DIR / "sessions"
TDATA_DIR = BASE_DIR / "tdata_accounts"
CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH = BASE_DIR / "claims.log"

SESSIONS_DIR.mkdir(exist_ok=True)
TDATA_DIR.mkdir(exist_ok=True)

logger = logging.getLogger("reelsio-claimer")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("[%(asctime)s] [%(name)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

_ch = logging.StreamHandler()
_ch.setFormatter(_fmt)
logger.addHandler(_ch)

_fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
_fh.setFormatter(_fmt)
logger.addHandler(_fh)

shutdown_event = asyncio.Event()

ZONTIQ_BASE = "https://api.zontiq.io/api/v1"


def load_config() -> dict:
    with open(CONFIG_PATH, "r") as f:
        cfg = json.load(f)
    cfg["api_id"] = int(cfg["api_id"])
    cfg["api_hash"] = str(cfg["api_hash"])
    return cfg


def convert_tdata_sessions():
    dirs_to_convert = [
        entry for entry in sorted(TDATA_DIR.iterdir())
        if entry.is_dir() and not (SESSIONS_DIR / f"{entry.name}.session").exists()
    ]
    if not dirs_to_convert:
        return

    # Run in a subprocess: opentele monkeypatches telethon.TelegramClient
    # globally and irreversibly on import, which corrupts api_id/api_hash
    # handling for every TelegramClient created afterward in this process.
    script = BASE_DIR / "tdata_convert.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        logger.info(line)
    for line in result.stderr.splitlines():
        logger.error(line)


async def authorize_new_account(api_id: int, api_hash: str):
    phone = input("Enter phone number (with country code, e.g. +79001234567): ").strip()
    if not phone:
        return
    session_name = phone.replace("+", "").replace(" ", "")
    session_file = SESSIONS_DIR / f"{session_name}.session"
    if session_file.exists():
        logger.info(f"Session {session_name} already exists, skipping")
        return
    session_path = str(SESSIONS_DIR / session_name)
    api_id = int(api_id)
    api_hash = str(api_hash)
    client = TelegramClient(session_path, api_id, api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.send_code_request(phone)
            code = input("Enter the code you received: ").strip()
            try:
                await client.sign_in(phone, code)
            except errors.SessionPasswordNeededError:
                password = input("2FA password required. Enter password: ").strip()
                await client.sign_in(password=password)
        me = await client.get_me()
        logger.info(f"Authorized as {me.first_name} ({me.id}), session saved to {session_name}.session")
    except Exception as e:
        logger.error(f"Authorization failed: {e}")
    finally:
        await client.disconnect()


def get_session_files() -> list[Path]:
    return sorted(SESSIONS_DIR.glob("*.session"))


async def validate_sessions(api_id: int, api_hash: str) -> list[Path]:
    sessions = get_session_files()
    if not sessions:
        return []

    valid = []
    for session_path in sessions:
        label = session_path.stem
        client = TelegramClient(str(session_path.with_suffix("")), int(api_id), str(api_hash))
        try:
            await client.connect()
            if not await client.is_user_authorized():
                logger.warning(f"[{label}] Session expired / not authorized — removing")
                await client.disconnect()
                session_path.unlink(missing_ok=True)
                continue
            me = await client.get_me()
            logger.info(f"[{label}] Valid — {me.first_name} (id={me.id})")
            valid.append(session_path)
        except Exception as e:
            logger.warning(f"[{label}] Invalid session ({e}) — removing")
            session_path.unlink(missing_ok=True)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    return valid


async def get_webapp_init_data(client: TelegramClient, bot_username: str) -> str:
    bot = await client.get_entity(bot_username)
    full = await client(functions.users.GetFullUserRequest(bot))
    menu_button = full.full_user.bot_info.menu_button if full.full_user.bot_info else None

    logger.info(
        f"[debug] bot id={getattr(bot, 'id', None)} is_bot={getattr(bot, 'bot', None)} "
        f"menu_button_type={type(menu_button).__name__} "
        f"menu_button_url={getattr(menu_button, 'url', None)}"
    )

    if not isinstance(menu_button, types.BotMenuButton):
        raise RuntimeError("Bot has no menu button web app configured")

    result = await client(functions.messages.RequestSimpleWebViewRequest(
        bot=bot,
        platform="android",
        url=menu_button.url,
        from_side_menu=True,
    ))

    params = parse_qs(urlparse(result.url).fragment)
    init_data = params.get("tgWebAppData", [None])[0]
    if not init_data:
        raise RuntimeError("tgWebAppData not found in webview URL")
    return init_data


async def fetch_spin_state(init_data: str) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{ZONTIQ_BASE}/miniapp/auth",
            json={"initData": init_data},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            resp.raise_for_status()
            auth_data = await resp.json()

        token = auth_data["token"]
        headers = {"Authorization": f"Bearer {token}"}
        async with session.get(
            f"{ZONTIQ_BASE}/roulette/state",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            resp.raise_for_status()
            return await resp.json()


async def claim_spins(client: TelegramClient, bot_username: str, account_label: str):
    account_logger = logging.getLogger(f"reelsio-claimer.{account_label}")
    account_logger.handlers = logger.handlers
    account_logger.propagate = False
    account_logger.setLevel(logging.INFO)

    try:
        init_data = await get_webapp_init_data(client, bot_username)
        state = await fetch_spin_state(init_data)
        free_spins = state.get("freeSpinsAvailable")
        max_spins = state.get("maxSpins")
        account_logger.info(f"Claim result: {free_spins}/{max_spins} free spins available")
    except errors.FloodWaitError as e:
        account_logger.warning(f"FloodWait: sleeping {e.seconds}s")
        await asyncio.sleep(e.seconds)
        await claim_spins(client, bot_username, account_label)
    except Exception as e:
        account_logger.error(f"Error during claim: {e}")


async def run_cycle(config: dict):
    api_id = config["api_id"]
    api_hash = config["api_hash"]
    bot_username = config["bot_username"]
    delay_range = config.get("delay_between_accounts_sec", [5, 30])

    sessions = get_session_files()
    if not sessions:
        logger.warning("No session files found in sessions/")
        return

    logger.info(f"Starting claim cycle for {len(sessions)} account(s)")

    for session_path in sessions:
        if shutdown_event.is_set():
            break

        account_label = session_path.stem
        client = TelegramClient(str(session_path.with_suffix("")), int(api_id), str(api_hash))

        try:
            await client.connect()
            if not await client.is_user_authorized():
                logger.warning(f"[{account_label}] Not authorized, skipping")
                continue

            await claim_spins(client, bot_username, account_label)
        except Exception as e:
            logger.error(f"[{account_label}] Connection error: {e}")
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

        if not shutdown_event.is_set() and session_path != sessions[-1]:
            delay = random.uniform(*delay_range)
            logger.info(f"Waiting {delay:.1f}s before next account...")
            await asyncio.sleep(delay)

    logger.info("Claim cycle complete")


async def main():
    config = load_config()

    convert_tdata_sessions()

    sessions = get_session_files()
    if sessions:
        logger.info(f"Found {len(sessions)} session(s): {[s.stem for s in sessions]}")
        answer = input("Add more accounts before starting? (y/n): ").strip().lower()
        if answer == "y":
            while True:
                await authorize_new_account(config["api_id"], config["api_hash"])
                again = input("Add another account? (y/n): ").strip().lower()
                if again != "y":
                    break
            sessions = get_session_files()
    else:
        logger.info("No sessions found. Starting authorization mode.")
        while True:
            await authorize_new_account(config["api_id"], config["api_hash"])
            answer = input("Add another account? (y/n): ").strip().lower()
            if answer != "y":
                break
        sessions = get_session_files()

    if not sessions:
        logger.error("No sessions created. Exiting.")
        return

    logger.info("Validating sessions...")
    valid_sessions = await validate_sessions(config["api_id"], config["api_hash"])
    invalid_count = len(sessions) - len(valid_sessions)
    if invalid_count > 0:
        logger.info(f"Removed {invalid_count} invalid session(s)")
    if not valid_sessions:
        logger.error("No valid sessions remaining. Exiting.")
        return
    logger.info(f"{len(valid_sessions)} valid session(s) ready")

    if sys.platform != "win32":
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: shutdown_event.set())

    interval_hours = config.get("interval_hours", 6)
    random_delay_minutes = config.get("random_delay_minutes", 30)

    while not shutdown_event.is_set():
        await run_cycle(config)

        if shutdown_event.is_set():
            break

        jitter = random.uniform(0, random_delay_minutes) * 60
        total_sleep = interval_hours * 3600 + jitter
        logger.info(
            f"Sleeping {total_sleep / 3600:.2f} hours "
            f"(base {interval_hours}h + {jitter / 60:.1f}min jitter) until next cycle"
        )

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=total_sleep)
        except asyncio.TimeoutError:
            pass

    logger.info("Shutting down gracefully")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user, exiting")
