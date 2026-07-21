import asyncio
import json
import logging
import os
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
NEW_SESSIONS_DIR = BASE_DIR / "new_sessions"  # freshly bought accounts to secure
TDATA_DIR = BASE_DIR / "tdata_accounts"
CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH = BASE_DIR / "claims.log"

SESSIONS_DIR.mkdir(exist_ok=True)
NEW_SESSIONS_DIR.mkdir(exist_ok=True)
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


def get_session_files(directory: Path = SESSIONS_DIR) -> list[Path]:
    return sorted(directory.glob("*.session"))


async def get_webapp_init_data(client: TelegramClient, bot_username: str) -> str:
    bot = await client.get_entity(bot_username)
    full = await client(functions.users.GetFullUserRequest(bot))
    menu_button = full.full_user.bot_info.menu_button if full.full_user.bot_info else None

    if not isinstance(menu_button, types.BotMenuButton):
        raise RuntimeError("Bot has no menu button web app configured")

    # menu_button.url is a direct-link mini app: t.me/<bot>/<short_name>?startapp=<param>
    parsed_menu_url = urlparse(menu_button.url)
    path_parts = [p for p in parsed_menu_url.path.split("/") if p]
    if len(path_parts) < 2:
        raise RuntimeError(f"Unexpected menu button URL format: {menu_button.url}")
    app_bot_username, app_short_name = path_parts[0], path_parts[1]
    start_param = parse_qs(parsed_menu_url.query).get("startapp", [None])[0]

    app_bot = await client.get_entity(app_bot_username)
    input_bot_user = types.InputUser(user_id=app_bot.id, access_hash=app_bot.access_hash)

    result = await client(functions.messages.RequestAppWebViewRequest(
        peer=app_bot,
        app=types.InputBotAppShortName(bot_id=input_bot_user, short_name=app_short_name),
        platform="android",
        start_param=start_param,
        write_allowed=True,
    ))

    params = parse_qs(urlparse(result.url).fragment)
    init_data = params.get("tgWebAppData", [None])[0]
    if not init_data:
        raise RuntimeError("tgWebAppData not found in webview URL")
    return init_data


async def authenticate(session: aiohttp.ClientSession, init_data: str) -> str:
    async with session.post(
        f"{ZONTIQ_BASE}/miniapp/auth",
        json={"initData": init_data},
        timeout=aiohttp.ClientTimeout(total=20),
    ) as resp:
        resp.raise_for_status()
        return (await resp.json())["token"]


async def get_state(session: aiohttp.ClientSession, token: str) -> dict:
    async with session.get(
        f"{ZONTIQ_BASE}/roulette/state",
        headers={"Authorization": f"Bearer {token}"},
        timeout=aiohttp.ClientTimeout(total=20),
    ) as resp:
        resp.raise_for_status()
        return await resp.json()


async def spin_wheel(session: aiohttp.ClientSession, token: str) -> dict:
    # POST /roulette/start with an empty body spins the wheel once.
    async with session.post(
        f"{ZONTIQ_BASE}/roulette/start",
        headers={"Authorization": f"Bearer {token}"},
        timeout=aiohttp.ClientTimeout(total=20),
    ) as resp:
        resp.raise_for_status()
        try:
            return await resp.json()
        except Exception:
            return {}


async def process_account(client, bot_username, account_label, mode):
    account_logger = logging.getLogger(f"reelsio-claimer.{account_label}")
    account_logger.handlers = logger.handlers
    account_logger.propagate = False
    account_logger.setLevel(logging.INFO)

    try:
        init_data = await get_webapp_init_data(client, bot_username)
        async with aiohttp.ClientSession() as session:
            token = await authenticate(session, init_data)
            state = await get_state(session, token)
            free_spins = state.get("freeSpinsAvailable") or 0
            max_spins = state.get("maxSpins")
            account_logger.info(f"State: {free_spins}/{max_spins} free spins available")

            if mode != "spin":
                return

            if free_spins <= 0:
                account_logger.info("No free spins to use, skipping")
                return

            spun = 0
            SAFETY_CAP = 1000  # absolute guard against a genuine infinite loop
            while free_spins > 0 and spun < SAFETY_CAP and not shutdown_event.is_set():
                result = await spin_wheel(session, token)
                spun += 1
                sector = result.get("sectorType") if isinstance(result, dict) else None
                won = result.get("result") if isinstance(result, dict) else None
                # The spin response already carries the updated count; a
                # FreeSpin sector legitimately raises it, so we just keep
                # spinning until it truly reaches zero.
                if isinstance(result, dict) and "freeSpinsAvailable" in result:
                    free_spins = result.get("freeSpinsAvailable") or 0
                else:
                    free_spins -= 1
                account_logger.info(
                    f"Spin #{spun}: {sector} -> {won} | {free_spins} spins left"
                )
                await asyncio.sleep(random.uniform(1.5, 4))

            account_logger.info(f"Finished spinning: {spun} spin(s) used, {free_spins} left")
    except errors.FloodWaitError as e:
        account_logger.warning(f"FloodWait: sleeping {e.seconds}s")
        await asyncio.sleep(e.seconds)
        await process_account(client, bot_username, account_label, mode)
    except Exception as e:
        account_logger.error(f"Error: {e}")


async def run_cycle(config: dict, mode: str):
    api_id = config["api_id"]
    api_hash = config["api_hash"]
    bot_username = config["bot_username"]
    delay_range = config.get("delay_between_accounts_sec", [5, 30])

    sessions = get_session_files()
    if not sessions:
        logger.warning("No session files found in sessions/")
        return

    logger.info(f"Starting '{mode}' cycle for {len(sessions)} account(s)")

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

            await process_account(client, bot_username, account_label, mode)
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


# ── Account securing: reset other sessions + change 2FA ──────────────────────

async def secure_account(client, account_label, old_pw, new_pw, hint, do_reset, do_2fa):
    account_logger = logging.getLogger(f"reelsio-claimer.{account_label}")
    account_logger.handlers = logger.handlers
    account_logger.propagate = False
    account_logger.setLevel(logging.INFO)

    if do_reset:
        try:
            await client(functions.auth.ResetAuthorizationsRequest())
            account_logger.info("Other sessions terminated")
        except Exception as e:
            account_logger.error(f"Reset sessions failed: {e}")

    if do_2fa:
        try:
            pwd = await client(functions.account.GetPasswordRequest())
            if pwd.has_password:
                await client.edit_2fa(
                    current_password=old_pw or None, new_password=new_pw, hint=hint
                )
            else:
                await client.edit_2fa(new_password=new_pw, hint=hint)
            account_logger.info("2FA password updated")
        except Exception as e:
            account_logger.error(f"2FA change failed: {e}")


async def secure_cycle(config, old_pw, new_pw, hint, do_reset, do_2fa):
    api_id = config["api_id"]
    api_hash = config["api_hash"]
    delay_range = config.get("delay_between_accounts_sec", [5, 30])

    sessions = get_session_files(NEW_SESSIONS_DIR)
    if not sessions:
        logger.warning("No session files found in new_sessions/")
        return

    logger.info(f"Securing {len(sessions)} account(s) from new_sessions/")

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
            await secure_account(client, account_label, old_pw, new_pw, hint, do_reset, do_2fa)
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

    logger.info("Securing complete")


# ── Terminal navigation menu ────────────────────────────────────────────────

_C = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "cyan": "\033[96m", "mag": "\033[95m", "yel": "\033[93m",
    "grn": "\033[92m", "blu": "\033[94m",
}


def _enable_ansi():
    # Turns on ANSI escape processing in legacy Windows consoles.
    if sys.platform == "win32":
        os.system("")


def _print_banner():
    c = _C
    print(f"""{c['mag']}{c['bold']}
╔════════════════════════════════════════════╗
║            R E E L S   S O F T             ║
║        мультиаккаунт • автоматизация        ║
╚════════════════════════════════════════════╝{c['reset']}
{c['dim']}Привет! Что будем делать сегодня?{c['reset']}""")


def prompt_menu(title, options, back=True):
    """options: list of (label, value). Accepts a number or a text match."""
    c = _C
    while True:
        print(f"\n{c['cyan']}{c['bold']}{title}{c['reset']}")
        for i, (label, _) in enumerate(options, 1):
            print(f"  {c['yel']}{i}{c['reset']}) {label}")
        if back:
            print(f"  {c['dim']}0) назад{c['reset']}")
        raw = input(f"{c['grn']}Выбор:{c['reset']} ").strip().lower()

        if raw.isdigit():
            n = int(raw)
            if back and n == 0:
                return None
            if 1 <= n <= len(options):
                return options[n - 1][1]
        else:
            for label, value in options:
                if raw and (raw in label.lower() or raw == str(value).lower()):
                    return value
        print(f"{c['dim']}Не понял выбор, попробуй ещё раз.{c['reset']}")


def choose_action():
    """Walk the category tree; returns a selected action value (or None)."""
    _enable_ansi()
    _print_banner()
    while True:
        category = prompt_menu("Категории:", [
            ("🤖 Фарм ботов", "farm_bots"),
            ("💰 Пополняшки и прогрев", "topup_warm"),
            ("🔐 Рега / безопасность аккаунтов", "secure_cat"),
        ], back=False)

        if category == "farm_bots":
            sub = prompt_menu("Фарм ботов — выбери проект:", [
                ("🎡 Рилс (Reels.io)", "reels"),
            ])
            if sub:
                return sub
        elif category == "topup_warm":
            sub = prompt_menu("Пополняшки и прогрев:", [
                ("✍️  Написать боту", "write_bot"),
                ("⭐ Пополнить старс", "topup_stars"),
            ])
            if sub:
                return sub
        elif category == "secure_cat":
            sub = prompt_menu("Безопасность аккаунтов:", [
                ("🔐 Обезопасить (сброс сессий + смена 2FA)", "secure"),
            ])
            if sub:
                return sub


async def run_secure(config):
    c = _C
    print(f"\n{c['cyan']}{c['bold']}🔐 Безопасность аккаунтов{c['reset']}")
    print(f"{c['dim']}Работает только с папкой new_sessions/ — рабочие "
          f"фарм-сессии в sessions/ не трогаются.{c['reset']}")

    count = len(get_session_files(NEW_SESSIONS_DIR))
    if count == 0:
        print(f"{c['yel']}Папка new_sessions/ пуста — положи туда купленные "
              f".session файлы и запусти снова.{c['reset']}")
        return

    old_pw = input("Текущий 2FA пароль (Enter — если 2FA не стоит): ").strip()
    new_pw = input("Новый 2FA пароль: ").strip()
    if not new_pw:
        print(f"{c['yel']}Новый пароль пустой — отмена.{c['reset']}")
        return
    hint = input("Подсказка к паролю (Enter — пропустить): ").strip()

    print(f"\n{c['yel']}Будет обработано {count} аккаунт(ов) из new_sessions/: "
          f"сброшены чужие сессии и установлен новый 2FA.{c['reset']}")
    if input("Продолжить? (yes/n): ").strip().lower() not in ("yes", "y", "да"):
        print(f"{c['dim']}Отменено.{c['reset']}")
        return

    await secure_cycle(config, old_pw, new_pw, hint, do_reset=True, do_2fa=True)


async def main():
    config = load_config()
    interactive = sys.stdin.isatty()
    cli_mode = next((a.lower() for a in sys.argv[1:] if a.lower() in ("farm", "spin")), None)

    # Show the navigation menu only in a real terminal without an explicit
    # farm/spin CLI arg. Non-TTY (systemd) and arg runs go straight to Reels.
    if interactive and not cli_mode:
        action = choose_action()
        if action is None:
            return
        if action == "secure":
            await run_secure(config)
            return
        if action != "reels":
            print(f"\n{_C['yel']}[{action}] — этот раздел ещё в разработке. Скоро будет!{_C['reset']}\n")
            return

    convert_tdata_sessions()

    sessions = get_session_files()

    if not interactive:
        logger.info("No TTY detected (running under systemd/cron) — skipping interactive prompts")
    elif sessions:
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

    logger.info(f"{len(sessions)} session(s) ready (dead ones are skipped per cycle)")

    # Choose mode: "farm" just reports free spins, "spin" spends them on
    # the wheel. Priority: command-line arg > interactive prompt >
    # config["mode"] (default "farm"). cli_mode/interactive are computed
    # at the top of main().
    if cli_mode:
        mode = cli_mode
    elif interactive:
        mode = prompt_menu("🎡 Рилс — что делаем?", [
            ("Фарм — только собрать/показать фриспины", "farm"),
            ("Спин — прокрутить все фриспины на колесе", "spin"),
        ], back=False)
    else:
        mode = str(config.get("mode", "farm")).lower()
    logger.info(f"Mode: {mode}")

    if sys.platform != "win32":
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: shutdown_event.set())

    interval_hours = config.get("interval_hours", 6)
    random_delay_minutes = config.get("random_delay_minutes", 30)

    while not shutdown_event.is_set():
        await run_cycle(config, mode)

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
