import asyncio
import json
import logging
import os
import random
import signal
import sys
from glob import glob
from pathlib import Path

from telethon import TelegramClient, errors

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


def load_config() -> dict:
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def convert_tdata_sessions(api_id: int, api_hash: str):
    try:
        from opentele.td import TDesktop
        from opentele.tl import TelegramClient as OpenteleClient
        from opentele.exception import OpenTeleException
    except ImportError:
        logger.warning("opentele not installed, skipping tdata conversion")
        return

    for entry in sorted(TDATA_DIR.iterdir()):
        if not entry.is_dir():
            continue
        session_name = entry.name
        session_path = SESSIONS_DIR / f"{session_name}.session"
        if session_path.exists():
            continue
        try:
            tdesk = TDesktop(str(entry))
        except (Exception, OpenTeleException) as e:
            logger.warning(f"[tdata] Skipping {entry.name}: {e}")
            continue
        if not tdesk.isLoaded():
            logger.warning(f"[tdata] Failed to load tdata from {entry.name}")
            continue
        try:
            client = tdesk.ToTelethon(
                str(SESSIONS_DIR / session_name),
                flag=OpenteleClient.Flag.UseCurrentSession,
            )
            logger.info(f"[tdata] Converted {entry.name} -> {session_name}.session")
        except (Exception, OpenTeleException) as e:
            logger.error(f"[tdata] Error converting {entry.name}: {e}")


async def authorize_new_account(api_id: int, api_hash: str):
    phone = input("Enter phone number (with country code, e.g. +79001234567): ").strip()
    if not phone:
        return
    session_name = phone.replace("+", "").replace(" ", "")
    session_path = str(SESSIONS_DIR / session_name)
    client = TelegramClient(session_path, api_id, api_hash)
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
    await client.disconnect()


def get_session_files() -> list[Path]:
    return sorted(SESSIONS_DIR.glob("*.session"))


async def claim_spins(client: TelegramClient, bot_username: str, account_label: str):
    account_logger = logging.getLogger(f"reelsio-claimer.{account_label}")
    account_logger.handlers = logger.handlers
    account_logger.setLevel(logging.INFO)

    try:
        await client.send_message(bot_username, "/start")
        await asyncio.sleep(random.uniform(2, 5))

        messages = await client.get_messages(bot_username, limit=5)

        clicked = False
        for msg in messages:
            if msg.buttons:
                for row in msg.buttons:
                    for button in row:
                        btn_text = button.text.lower() if button.text else ""
                        if any(kw in btn_text for kw in [
                            "spin", "спин", "claim", "клейм", "забрать",
                            "получить", "бонус", "bonus", "крутить",
                        ]):
                            account_logger.info(f"Clicking button: '{button.text}'")
                            result = await button.click()
                            if result and hasattr(result, "message") and result.message:
                                account_logger.info(f"Claim result: {result.message}")
                            else:
                                await asyncio.sleep(2)
                                new_msgs = await client.get_messages(bot_username, limit=1)
                                if new_msgs:
                                    account_logger.info(f"Claim result: {new_msgs[0].message}")
                            clicked = True
                            break
                    if clicked:
                        break
            if clicked:
                break

        if not clicked:
            all_buttons = []
            for msg in messages:
                if msg.buttons:
                    for row in msg.buttons:
                        for button in row:
                            all_buttons.append(button.text)
            account_logger.warning(f"Spin button not found. Available buttons: {all_buttons}")

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
        client = TelegramClient(str(session_path.with_suffix("")), api_id, api_hash)

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

    convert_tdata_sessions(config["api_id"], config["api_hash"])

    sessions = get_session_files()
    if not sessions:
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
