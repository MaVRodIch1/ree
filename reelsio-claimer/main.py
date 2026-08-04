import asyncio
import json
import logging
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import aiohttp
from telethon import TelegramClient, errors, events, functions, types

BASE_DIR = Path(__file__).resolve().parent
SESSIONS_DIR = BASE_DIR / "sessions"
NEW_SESSIONS_DIR = BASE_DIR / "new_sessions"  # freshly bought accounts to secure
TDATA_DIR = BASE_DIR / "tdata_accounts"
TDATA_ZIPS_DIR = BASE_DIR / "tdata_zips"  # drop tdata .zip archives here to import
AVATARS_DIR = BASE_DIR / "avatars"  # profile photos for warming
AVATARS_USED_DIR = AVATARS_DIR / "_used"  # used photos moved here, never reused
NICKS_FILE = BASE_DIR / "nicknames.txt"  # pool of usernames for warming
NAMES_FILE = BASE_DIR / "names.txt"  # pool of display names (First Last) for warming
CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH = BASE_DIR / "claims.log"

SESSIONS_DIR.mkdir(exist_ok=True)
NEW_SESSIONS_DIR.mkdir(exist_ok=True)
TDATA_DIR.mkdir(exist_ok=True)
TDATA_ZIPS_DIR.mkdir(exist_ok=True)
AVATARS_DIR.mkdir(exist_ok=True)
AVATARS_USED_DIR.mkdir(exist_ok=True)


def _bootstrap_pool(working: Path, example: Path):
    # Working pools are consumed locally and gitignored; seed them from the
    # tracked *.example.txt template on first run so a fresh clone has data.
    if not working.exists() and example.exists():
        working.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")


_bootstrap_pool(NICKS_FILE, BASE_DIR / "nicknames.example.txt")
_bootstrap_pool(NAMES_FILE, BASE_DIR / "names.example.txt")

logger = logging.getLogger("reelsio-claimer")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("[%(asctime)s] [%(name)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

_ch = logging.StreamHandler()
_ch.setFormatter(_fmt)
logger.addHandler(_ch)

_fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
_fh.setFormatter(_fmt)
logger.addHandler(_fh)

# Telethon logs every reconnect attempt at WARNING; that floods stderr when a
# connection flaps, so keep only real errors from the library.
logging.getLogger("telethon").setLevel(logging.ERROR)

# Sessions held open by a long-running task (e.g. the sniper). Farm cycles skip
# them: two clients sharing one auth_key make Telegram drop the connection, and
# Telethon then reconnect-loops forever.
BUSY_SESSIONS = set()

shutdown_event = asyncio.Event()

ZONTIQ_BASE = "https://api.zontiq.io/api/v1"
SPLIT_API_BASE = "https://api.split.tg"

# Asteroid Shiba farming
ASTEROID_BOT = "AsteroidShiba_app_bot"
ASTEROID_REF = "6128719325"
ASTEROID_CHANNELS = ["asteroidshiba_p2e", "asteroidshiba_game"]

# Channel whose fresh posts get viewed once a day
VIEWS_CHANNEL = "prosadin"

# Comment sniper: first paid comment under every new post of a channel
SNIPER_CHANNEL = "durov_russia"
SNIPER_TEXT = "@prosadin - легенда ТОНА"
SNIPER_SESSION = "380992273859"
SNIPER_POLL_SEC = 1.0  # backup poll interval for the sniper (push is instant)
SNIPER_AUDIO_DIR = BASE_DIR / "sniper_audio"
SNIPER_AUDIO_DIR.mkdir(exist_ok=True)
# Keep the sniper's own session here so farm cycles never touch it.
SNIPER_SESSION_DIR = BASE_DIR / "sniper_session"
SNIPER_SESSION_DIR.mkdir(exist_ok=True)
SPLIT_KEY_FILE = BASE_DIR / "split_api_key.txt"


def load_split_key():
    if SPLIT_KEY_FILE.exists():
        for ln in SPLIT_KEY_FILE.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                return ln
    return ""


# ── Progress tracking (resume after a crash/restart) ─────────────────────────

PROGRESS_FILE = BASE_DIR / "progress.json"


def _load_progress():
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def progress_done(task, window):
    """Labels already processed within the current window (else empty)."""
    entry = _load_progress().get(task) or {}
    return set(entry.get("done", [])) if entry.get("window") == window else set()


def progress_mark(task, window, label):
    data = _load_progress()
    entry = data.get(task) or {}
    if entry.get("window") != window:
        entry = {"window": window, "done": []}
    if label not in entry["done"]:
        entry["done"].append(label)
    data[task] = entry
    try:
        PROGRESS_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Could not save progress: {e}")


def daily_window():
    """Window key for once-a-day tasks (asteroids reset at 00:00 UTC)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def cycle_window(interval_hours):
    """Window key for the recurring Reels cycle."""
    return f"slot-{int(time.time() // (interval_hours * 3600))}"


def fmt_duration(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}ч {m}м"
    if m:
        return f"{m}м {s}с"
    return f"{s}с"


def log_eta(idx, total, elapsed, delay_range):
    """Log 'account i/N, remaining, ETA' using measured pace."""
    left = total - idx
    if left <= 0:
        return
    per = (elapsed / idx) if idx else (sum(delay_range) / 2 + 3)
    logger.info(f"Progress: {idx}/{total} done, {left} left, ~{fmt_duration(per * left)} remaining")


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

    all_sessions = get_session_files()
    if not all_sessions:
        logger.warning("No session files found in sessions/")
        return

    # Resume: skip accounts already done in this cycle window.
    window = cycle_window(config.get("interval_hours", 6))
    done = progress_done("reels", window)
    sessions = [sp for sp in all_sessions
                if sp.stem not in done and sp.stem not in BUSY_SESSIONS]
    if done:
        logger.info(f"Resuming cycle: {len(done)} already done, {len(sessions)} left")

    if not sessions:
        logger.info("Cycle already complete for this window")
        return

    total = len(sessions)
    logger.info(f"Starting '{mode}' cycle for {total} account(s)")
    started = time.time()
    alive = dead = 0

    for i, session_path in enumerate(sessions, 1):
        if shutdown_event.is_set():
            break

        account_label = session_path.stem
        client = TelegramClient(str(session_path.with_suffix("")), int(api_id), str(api_hash))

        try:
            await client.connect()
            if not await client.is_user_authorized():
                logger.warning(f"[{account_label}] Not authorized, skipping")
                dead += 1
                progress_mark("reels", window, account_label)
                continue

            alive += 1
            await process_account(client, bot_username, account_label, mode)
            progress_mark("reels", window, account_label)
        except Exception as e:
            logger.error(f"[{account_label}] Connection error: {e}")
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

        log_eta(i, total, time.time() - started, delay_range)

        if not shutdown_event.is_set() and session_path != sessions[-1]:
            delay = random.uniform(*delay_range)
            logger.info(f"Waiting {delay:.1f}s before next account...")
            await asyncio.sleep(delay)

    logger.info(f"Claim cycle complete — сессий живо: {alive}/{alive + dead}")


# ── Account securing: reset other sessions + change 2FA ──────────────────────

async def secure_account(client, account_label, old_pw, new_pw, hint, do_reset, do_2fa, keep_device=""):
    account_logger = logging.getLogger(f"reelsio-claimer.{account_label}")
    account_logger.handlers = logger.handlers
    account_logger.propagate = False
    account_logger.setLevel(logging.INFO)

    if do_reset:
        try:
            if keep_device:
                # Terminate every session except the script's own (current)
                # and any whose device/app name matches keep_device.
                auths = await client(functions.account.GetAuthorizationsRequest())
                keep = keep_device.lower()
                killed = kept = 0
                for a in auths.authorizations:
                    dev = (a.device_model or "") + " " + (a.app_name or "")
                    if a.current or keep in dev.lower():
                        kept += 1
                        continue
                    try:
                        await client(functions.account.ResetAuthorizationRequest(hash=a.hash))
                        killed += 1
                    except Exception as e:
                        account_logger.warning(f"Could not terminate '{a.device_model}': {e}")
                account_logger.info(f"Sessions: {killed} terminated, {kept} kept")
            else:
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


async def secure_cycle(config, old_pw, new_pw, hint, do_reset, do_2fa, keep_device=""):
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
            await secure_account(client, account_label, old_pw, new_pw, hint, do_reset, do_2fa, keep_device)
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


# ── Login-code listener (log in to an account by its session) ────────────────

TELEGRAM_SERVICE_ID = 777000  # official "Telegram" service notifications


def extract_login_code(text: str) -> str:
    if not text:
        return None
    # Login codes are 5-6 digits; prefer a standalone group near "code"/"код".
    m = re.search(r"(?:code|код)\D{0,20}(\d{5,6})", text, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"\b(\d{5,6})\b", text)
    return m.group(1) if m else None


async def listen_login_code(config):
    c = _C
    api_id, api_hash = config["api_id"], config["api_hash"]

    session_path = pick_session("📲 Прослушка кода входа")
    if session_path is None:
        return

    client = TelegramClient(str(session_path.with_suffix("")), int(api_id), str(api_hash))
    await client.connect()
    if not await client.is_user_authorized():
        print(f"{c['yel']}Сессия {session_path.stem} не авторизована.{c['reset']}")
        await client.disconnect()
        return

    me = await client.get_me()
    print(f"\n{c['grn']}Слушаю коды для {me.first_name} "
          f"(+{getattr(me, 'phone', session_path.stem)}).{c['reset']}")
    print(f"{c['dim']}Заходи в официальный Telegram по этому аккаунту — "
          f"код появится здесь. Ctrl+C для выхода.{c['reset']}")

    # Show any code already sitting in the service chat.
    recent = await client.get_messages(TELEGRAM_SERVICE_ID, limit=3)
    for m in reversed(recent):
        code = extract_login_code(m.message or "")
        if code:
            print(f"{c['dim']}Последний код в чате:{c['reset']} {c['bold']}{code}{c['reset']}")

    @client.on(events.NewMessage(from_users=TELEGRAM_SERVICE_ID))
    async def _handler(event):
        text = event.message.message or ""
        code = extract_login_code(text)
        if code:
            print(f"\n{c['grn']}{c['bold']}>>> КОД ВХОДА: {code}{c['reset']}\n")
        else:
            print(f"{c['dim']}[Telegram] {text}{c['reset']}")

    try:
        await client.run_until_disconnected()
    finally:
        await client.disconnect()


# ── tdata zip import → new_sessions/ ─────────────────────────────────────────

def _find_tdata_dir(root: Path):
    """Locate the actual tdata folder inside an extracted archive."""
    if (root / "key_datas").exists():
        return root
    for p in root.rglob("tdata"):
        if p.is_dir():
            return p
    # tdata is identified by its key_datas file at any depth (the containing
    # folder may be named by a MEGA handle rather than "tdata").
    kd = next(root.rglob("key_datas"), None)
    if kd:
        return kd.parent
    return None


async def import_tdata_zips(config):
    c = _C
    zips = sorted(TDATA_ZIPS_DIR.glob("*.zip"))
    print(f"\n{c['cyan']}{c['bold']}📦 Импорт tdata из zip → new_sessions/{c['reset']}")
    if not zips:
        print(f"{c['yel']}В папке tdata_zips/ нет .zip архивов. Кинь туда "
              f"tdata-архивы и запусти снова.{c['reset']}")
        return

    print(f"{c['dim']}Найдено архивов: {len(zips)}{c['reset']}")
    imported = 0
    for zpath in zips:
        name = zpath.stem
        out_session = NEW_SESSIONS_DIR / name
        if (NEW_SESSIONS_DIR / f"{name}.session").exists():
            print(f"{c['dim']}{name}: .session уже существует, пропуск.{c['reset']}")
            continue

        tmp = TDATA_ZIPS_DIR / f"_extract_{name}"
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            with zipfile.ZipFile(zpath) as zf:
                zf.extractall(tmp)
        except Exception as e:
            print(f"{c['yel']}{name}: не смог распаковать zip: {e}{c['reset']}")
            shutil.rmtree(tmp, ignore_errors=True)
            continue

        tdata_dir = _find_tdata_dir(tmp)
        if not tdata_dir:
            print(f"{c['yel']}{name}: внутри zip не найдена папка tdata.{c['reset']}")
            shutil.rmtree(tmp, ignore_errors=True)
            continue

        # opentele monkeypatches Telethon, so convert in a subprocess.
        result = subprocess.run(
            [sys.executable, str(BASE_DIR / "tdata_convert.py"), str(tdata_dir), str(out_session)],
            capture_output=True, text=True,
        )
        for line in (result.stdout + result.stderr).splitlines():
            if line.strip():
                print(f"  {c['dim']}{line}{c['reset']}")

        if (NEW_SESSIONS_DIR / f"{name}.session").exists():
            imported += 1
            print(f"{c['grn']}{name}: импортирован в new_sessions/{c['reset']}")
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{c['grn']}Готово: импортировано {imported} из {len(zips)}.{c['reset']}")


# ── Warming: set avatar (unique) + username ──────────────────────────────────

_AVATAR_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def next_avatar():
    for p in sorted(AVATARS_DIR.iterdir()):
        if p.is_file() and p.suffix.lower() in _AVATAR_EXTS:
            return p
    return None


def count_avatars():
    return sum(1 for p in AVATARS_DIR.iterdir()
               if p.is_file() and p.suffix.lower() in _AVATAR_EXTS)


def load_nicks():
    if not NICKS_FILE.exists():
        return []
    return [ln.strip() for ln in NICKS_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()]


def save_nicks(nicks):
    NICKS_FILE.write_text("\n".join(nicks) + ("\n" if nicks else ""), encoding="utf-8")


def load_names():
    if not NAMES_FILE.exists():
        return []
    return [ln.strip() for ln in NAMES_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()]


def save_names(names):
    NAMES_FILE.write_text("\n".join(names) + ("\n" if names else ""), encoding="utf-8")


async def warm_account(client, account_label, set_avatar, set_name, set_username):
    account_logger = logging.getLogger(f"reelsio-claimer.{account_label}")
    account_logger.handlers = logger.handlers
    account_logger.propagate = False
    account_logger.setLevel(logging.INFO)

    if set_avatar:
        avatar = next_avatar()
        if avatar is None:
            account_logger.warning("No avatars left in avatars/")
        else:
            try:
                uploaded = await client.upload_file(str(avatar))
                await client(functions.photos.UploadProfilePhotoRequest(file=uploaded))
                # Move it out of the pool so it's never reused.
                dest = AVATARS_USED_DIR / avatar.name
                if dest.exists():
                    dest = AVATARS_USED_DIR / f"{avatar.stem}_{random.randint(1000,9999)}{avatar.suffix}"
                shutil.move(str(avatar), str(dest))
                account_logger.info(f"Avatar set from {avatar.name}")
            except Exception as e:
                account_logger.error(f"Avatar failed: {e}")

    if set_name:
        names = load_names()
        if not names:
            account_logger.warning("names.txt is empty")
        else:
            full = names[0]  # take first; consume on success so it's never reused
            first, _, last = full.partition(" ")
            try:
                await client(functions.account.UpdateProfileRequest(
                    first_name=first, last_name=last
                ))
                account_logger.info(f"Name set: {full}")
                save_names(names[1:])
            except Exception as e:
                account_logger.error(f"Name failed: {e}")

    if set_username:
        nicks = load_nicks()
        if not nicks:
            account_logger.warning("nicknames.txt is empty")
            return
        # Telegram rate-limits username changes hard, and every occupied
        # attempt still counts. So try only a few high-entropy candidates
        # and bail on large FloodWaits instead of sleeping for ~an hour.
        MAX_TRIES = 3
        FLOOD_CAP = 120  # seconds; longer than this → give up for this run
        consumed, done, tries = [], False, 0
        for name in nicks:
            if tries >= MAX_TRIES:
                break
            tries += 1
            try:
                await client(functions.account.UpdateUsernameRequest(username=name))
                account_logger.info(f"Username set: @{name}")
                consumed.append(name)
                done = True
                break
            except errors.FloodWaitError as e:
                if e.seconds > FLOOD_CAP:
                    account_logger.warning(
                        f"FloodWait {e.seconds}s too long — skip username for now "
                        f"(keep @{name} in pool, try later)"
                    )
                    break  # do NOT consume this name; retry it next run
                account_logger.warning(f"FloodWait {e.seconds}s, waiting")
                await asyncio.sleep(e.seconds)
                try:
                    await client(functions.account.UpdateUsernameRequest(username=name))
                    account_logger.info(f"Username set: @{name}")
                    consumed.append(name)
                    done = True
                    break
                except Exception as e2:
                    account_logger.info(f"@{name} failed ({type(e2).__name__}), next")
                    consumed.append(name)
                    continue
            except Exception as e:
                # occupied / invalid → consume and try the next one
                account_logger.info(f"@{name} unavailable ({type(e).__name__}), next")
                consumed.append(name)
                continue
        save_nicks([n for n in nicks if n not in consumed])
        if not done:
            account_logger.warning("Could not set a username this run")


async def warm_cycle(config, set_avatar, set_name, set_username):
    api_id = config["api_id"]
    api_hash = config["api_hash"]
    delay_range = config.get("delay_between_accounts_sec", [5, 30])

    sessions = get_session_files(NEW_SESSIONS_DIR)
    if not sessions:
        logger.warning("No session files found in new_sessions/")
        return

    logger.info(f"Warming {len(sessions)} account(s) from new_sessions/")

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
            await warm_account(client, account_label, set_avatar, set_name, set_username)
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

    logger.info("Warming complete")


# ── Channel post views ───────────────────────────────────────────────────────

async def view_channel_posts(client, account_label, channel, hours=24):
    alog = logging.getLogger(f"reelsio-claimer.{account_label}")
    alog.handlers = logger.handlers
    alog.propagate = False
    alog.setLevel(logging.INFO)

    entity = await client.get_entity(channel)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    ids = []
    async for msg in client.iter_messages(entity, limit=100):
        if msg.date < cutoff:
            break
        ids.append(msg.id)

    if not ids:
        alog.info(f"@{channel}: no posts in the last {hours}h")
        return 0

    try:
        await client(functions.messages.GetMessagesViewsRequest(
            peer=entity, id=ids, increment=True))
        await client(functions.channels.ReadHistoryRequest(
            channel=entity, max_id=max(ids)))
        alog.info(f"@{channel}: viewed {len(ids)} post(s)")
        return len(ids)
    except errors.FloodWaitError as e:
        alog.warning(f"FloodWait {e.seconds}s on views")
    except Exception as e:
        alog.error(f"Views failed: {e}")
    return 0


async def views_cycle(config, sessions, channel=VIEWS_CHANNEL, hours=24):
    api_id, api_hash = config["api_id"], config["api_hash"]
    delay_range = config.get("delay_between_accounts_sec", [5, 30])

    task = f"views:{channel}"
    window = daily_window()
    done = progress_done(task, window)
    sessions = [sp for sp in sessions
                if sp.stem not in done and sp.stem not in BUSY_SESSIONS]
    if done:
        logger.info(f"Views: {len(done)} already done today, {len(sessions)} left")
    if not sessions:
        logger.info(f"Views @{channel}: already done for today")
        return

    total = len(sessions)
    logger.info(f"Views @{channel}: {total} account(s)")
    started = time.time()
    alive = dead = 0

    for i, session_path in enumerate(sessions, 1):
        if shutdown_event.is_set():
            break
        label = session_path.stem
        client = TelegramClient(str(session_path.with_suffix("")), int(api_id), str(api_hash))
        try:
            await client.connect()
            if not await client.is_user_authorized():
                logger.warning(f"[{label}] Not authorized, skipping")
                dead += 1
                progress_mark(task, window, label)
                continue
            alive += 1
            await view_channel_posts(client, label, channel, hours)
            progress_mark(task, window, label)
        except Exception as e:
            logger.error(f"[{label}] error: {e}")
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

        log_eta(i, total, time.time() - started, delay_range)

        if not shutdown_event.is_set() and session_path != sessions[-1]:
            delay = random.uniform(*delay_range)
            logger.info(f"Waiting {delay:.1f}s before next account...")
            await asyncio.sleep(delay)

    logger.info(f"Views cycle complete — сессий живо: {alive}/{alive + dead}")


async def run_views(config):
    c = _C
    print(f"\n{c['cyan']}{c['bold']}👁 Просмотры постов канала{c['reset']}")
    ch = input(f"Канал без @ [{VIEWS_CHANNEL}]: ").strip().lstrip("@") or VIEWS_CHANNEL

    target = prompt_menu("Аккаунты:", [
        ("✅ Выбрать вручную", "select"),
        ("📂 Все из папки", "all"),
    ])
    if target is None:
        return
    sessions = (await multi_pick_sessions("Просмотры")) if target == "select" \
        else (await choose_folder_sessions("Просмотры"))
    if not sessions:
        print(f"{c['yel']}В выбранной папке нет сессий.{c['reset']}")
        return

    print(f"\n{c['yel']}Аккаунтов: {len(sessions)} → посты @{ch} за сутки.{c['reset']}")
    if input("Продолжить? (yes/n): ").strip().lower() not in ("yes", "y", "да"):
        print(f"{c['dim']}Отменено.{c['reset']}")
        return

    await views_cycle(config, sessions, ch)


# ── Comment sniper: first paid comment under each new post ───────────────────

_SNIPER_AUDIO_EXTS = {".mp3", ".m4a", ".ogg", ".oga", ".wav", ".flac"}


def find_sniper_audio():
    for p in sorted(SNIPER_AUDIO_DIR.iterdir()):
        if p.is_file() and p.suffix.lower() in _SNIPER_AUDIO_EXTS:
            return p
    return None


def find_session_by_name(name):
    """Look in sniper_session/ first — a session kept there is invisible to the
    farm cycles, so the sniper can hold it open without connection clashes."""
    digits = "".join(ch for ch in name if ch.isdigit())
    pools = (get_session_files(SNIPER_SESSION_DIR)
             + get_session_files(SESSIONS_DIR)
             + get_session_files(NEW_SESSIONS_DIR))
    for p in pools:
        if p.stem == digits:
            return p
    # Any single session dropped into sniper_session/ is used as-is.
    lone = get_session_files(SNIPER_SESSION_DIR)
    return lone[0] if lone else None


async def _sniper_send(client, channel_entity, post_id, text, media, slog):
    """Post the paid comment under `post_id` as fast as possible."""
    disc = await client(functions.messages.GetDiscussionMessageRequest(
        peer=channel_entity, msg_id=post_id))
    if not disc.messages:
        slog.warning(f"post {post_id}: no discussion message")
        return
    disc_msg = disc.messages[0]
    group = await client.get_entity(disc_msg.peer_id)
    stars = getattr(group, "send_paid_messages_stars", None)

    await client(functions.messages.SendMediaRequest(
        peer=group,
        media=media,
        message=text,
        random_id=random.randrange(-2**63, 2**63),
        reply_to=types.InputReplyToMessage(reply_to_msg_id=disc_msg.id),
        allow_paid_stars=stars,
    ))
    slog.info(f"post {post_id}: commented"
              f"{f' for {stars}★' if stars else ''}")


async def sniper_task(config, channel=SNIPER_CHANNEL, text=SNIPER_TEXT,
                      session_name=SNIPER_SESSION, audio=None):
    """Watch `channel` and instantly comment under every new post."""
    slog = logging.getLogger(f"reelsio-claimer.sniper")
    slog.handlers = logger.handlers
    slog.propagate = False
    slog.setLevel(logging.INFO)

    session_path = find_session_by_name(session_name)
    if not session_path:
        slog.error(f"session {session_name} not found in sessions/ or new_sessions/")
        return
    audio = audio or find_sniper_audio()
    if not audio:
        slog.error("no audio file in sniper_audio/ — put an .mp3 there")
        return

    client = TelegramClient(str(session_path.with_suffix("")),
                            int(config["api_id"]), str(config["api_hash"]))
    await client.connect()
    if not await client.is_user_authorized():
        slog.error(f"session {session_path.stem} not authorized")
        await client.disconnect()
        return

    channel_entity = await client.get_entity(channel)

    # Pre-upload the audio once so the comment goes out with no upload delay.
    uploaded = await client.upload_file(str(audio))
    media = types.InputMediaUploadedDocument(
        file=uploaded,
        mime_type="audio/mpeg",
        attributes=[
            types.DocumentAttributeAudio(duration=0, title=audio.stem, performer=""),
            types.DocumentAttributeFilename(file_name=audio.name),
        ],
    )

    slog.info(f"Watching @{channel} — will comment as {session_path.stem} "
              f"with {audio.name} (push + poll {SNIPER_POLL_SEC}s)")
    # Keep farm cycles off this session while the sniper holds it open.
    BUSY_SESSIONS.add(session_path.stem)

    handled: set[int] = set()  # post ids we've already reacted to (dedup)

    async def fire(post_id: int):
        # Deduped, non-blocking: whichever source (push/poll) sees the post
        # first grabs it; the other becomes a no-op. Runs as its own task so a
        # comment never holds up the next detection.
        if post_id in handled:
            return
        handled.add(post_id)
        try:
            await _sniper_send(client, channel_entity, post_id, text, media, slog)
        except Exception as e:
            slog.error(f"comment failed: {e} — retrying with fresh upload")
            try:
                up = await client.upload_file(str(audio))
                m2 = types.InputMediaUploadedDocument(
                    file=up, mime_type="audio/mpeg",
                    attributes=[
                        types.DocumentAttributeAudio(duration=0, title=audio.stem, performer=""),
                        types.DocumentAttributeFilename(file_name=audio.name),
                    ])
                await _sniper_send(client, channel_entity, post_id, text, m2, slog)
            except Exception as e2:
                slog.error(f"retry failed: {e2}")

    # Real-time push updates — fire instantly, without blocking the update loop.
    @client.on(events.NewMessage(chats=channel_entity))
    async def _on_post(event):
        asyncio.create_task(fire(event.message.id))

    async def poll_loop():
        # Backup path: even if a push update is delayed while farm cycles keep
        # the event loop busy, this catches the new post within SNIPER_POLL_SEC.
        # Baseline = current latest id, so we only hit posts published from now.
        try:
            latest = await client.get_messages(channel_entity, limit=1)
            last_id = latest[0].id if latest else 0
        except Exception:
            last_id = 0
        while client.is_connected() and not shutdown_event.is_set():
            try:
                msgs = await client.get_messages(channel_entity, limit=5)
                for m in sorted(msgs, key=lambda x: x.id):
                    if m.id > last_id and m.id not in handled:
                        asyncio.create_task(fire(m.id))
                    last_id = max(last_id, m.id)
            except Exception as e:
                slog.debug(f"poll error: {e}")
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=SNIPER_POLL_SEC)
            except asyncio.TimeoutError:
                pass

    try:
        await asyncio.gather(client.run_until_disconnected(), poll_loop())
    finally:
        BUSY_SESSIONS.discard(session_path.stem)
        try:
            await client.disconnect()
        except Exception:
            pass


async def run_sniper(config):
    c = _C
    print(f"\n{c['cyan']}{c['bold']}🎯 Снайпер комментариев{c['reset']}")
    ch = input(f"Канал без @ [{SNIPER_CHANNEL}]: ").strip().lstrip("@") or SNIPER_CHANNEL
    sess = input(f"Сессия (номер) [{SNIPER_SESSION}]: ").strip() or SNIPER_SESSION
    txt = input(f"Текст [{SNIPER_TEXT}]: ").strip() or SNIPER_TEXT

    audio = find_sniper_audio()
    if not audio:
        print(f"{c['yel']}Положи музыкальный файл в sniper_audio/ и запусти снова.{c['reset']}")
        return
    print(f"{c['dim']}Файл: {audio.name}. Ctrl+C для остановки.{c['reset']}")
    await sniper_task(config, ch, txt, sess, audio)


# ── MEGA import: pull .session files from public folder links ────────────────

MEGA_LINKS_FILE = BASE_DIR / "mega_links.txt"
MEGA_2FA_FILE = BASE_DIR / "mega_2fa.txt"
MEGA_FAILED_FILE = BASE_DIR / "mega_failed.txt"
MEGA_TDATA_DIR = BASE_DIR / "mega_tdata"
MEGA_TDATA_DIR.mkdir(exist_ok=True)


def _mega_account_name(files, folder_id):
    """Pick an account name from the folder: a phone-looking .session/.json
    stem if present, else the MEGA folder id."""
    for f in files:
        stem = f["name"].rsplit(".", 1)[0]
        if f["name"].lower().endswith((".session", ".json")) and stem.isdigit():
            return stem
    return folder_id


async def import_from_mega(config):
    c = _C
    print(f"\n{c['cyan']}{c['bold']}📥 Импорт tdata с MEGA{c['reset']}")

    try:
        import mega_dl
    except ImportError as e:
        print(f"{c['yel']}Нужны зависимости: pip install requests pycryptodome ({e}){c['reset']}")
        return

    if not MEGA_LINKS_FILE.exists():
        print(f"{c['yel']}Создай mega_links.txt и вставь туда ссылки "
              f"(по одной в строке).{c['reset']}")
        return

    links = [l.strip() for l in MEGA_LINKS_FILE.read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.strip().startswith("#")]
    if not links:
        print(f"{c['yel']}mega_links.txt пуст.{c['reset']}")
        return

    print(f"{c['dim']}Ссылок: {len(links)}. tdata целиком качается в mega_tdata/<номер>/, "
          f"2FA-пароли — в mega_2fa.txt. Потом конвертируй пунктом "
          f"«Конвертировать mega_tdata → сессии».{c['reset']}")
    if input("Начать? (yes/n): ").strip().lower() not in ("yes", "y", "да"):
        print(f"{c['dim']}Отменено.{c['reset']}")
        return

    session = None
    got = skipped = 0
    bad_links = []  # (link, reason)
    started = time.time()

    for i, link in enumerate(links, 1):
        if shutdown_event.is_set():
            break
        try:
            files, session = await asyncio.to_thread(mega_dl.list_folder, link, session)
        except Exception as e:
            logger.error(f"[{i}/{len(links)}] listing failed: {e}\n    ↳ {link}")
            bad_links.append((link, f"listing failed: {e}"))
            continue

        if not files:
            logger.warning(f"[{i}/{len(links)}] empty folder\n    ↳ {link}")
            bad_links.append((link, "empty folder"))
            continue

        folder_id = mega_dl.parse_folder_link(link)[0]
        acct = _mega_account_name(files, folder_id)
        acct_dir = MEGA_TDATA_DIR / acct
        if acct_dir.exists() and any(acct_dir.rglob("key_datas")):
            logger.info(f"[{i}/{len(links)}] {acct} already downloaded, skip")
            skipped += 1
            continue

        try:
            for f in files:
                # Drop a redundant leading dir equal to the account name so we
                # get mega_tdata/<acct>/tdata/... rather than a double nest.
                rel = f.get("path", f["name"])
                parts = rel.split("/")
                if parts and parts[0] == acct:
                    rel = "/".join(parts[1:]) or f["name"]
                f2 = dict(f, path=rel)
                await asyncio.to_thread(mega_dl.download_file, f2, acct_dir, session)
            got += 1
            logger.info(f"[{i}/{len(links)}] downloaded tdata for {acct}")
        except Exception as e:
            logger.error(f"[{i}/{len(links)}] {acct}: {e}\n    ↳ {link}")
            bad_links.append((link, f"{acct}: {e}"))
            continue

        # Pull the 2FA password out of the downloaded tree.
        pw = next((p for p in acct_dir.rglob("*") if p.is_file()
                   and "2fa" in p.name.lower() and p.suffix.lower() == ".txt"), None)
        if pw:
            try:
                with open(MEGA_2FA_FILE, "a", encoding="utf-8") as fh:
                    fh.write(f"{acct}:{pw.read_text(encoding='utf-8', errors='ignore').strip()}\n")
            except Exception:
                pass

        log_eta(i, len(links), time.time() - started, [1, 2])

    print(f"\n{c['grn']}Готово: скачано {got}, пропущено {skipped}, "
          f"ошибок {len(bad_links)}.{c['reset']}")
    if MEGA_2FA_FILE.exists():
        print(f"{c['dim']}2FA-пароли сохранены в {MEGA_2FA_FILE.name}{c['reset']}")
    if got:
        print(f"{c['dim']}Теперь: меню → «Конвертировать mega_tdata → сессии».{c['reset']}")

    if bad_links:
        MEGA_FAILED_FILE.write_text(
            "\n".join(f"{link}  # {reason}" for link, reason in bad_links) + "\n",
            encoding="utf-8")
        print(f"\n{c['yel']}Проблемные ссылки ({len(bad_links)}) — "
              f"сохранены в {MEGA_FAILED_FILE.name}:{c['reset']}")
        for link, reason in bad_links:
            print(f"  {c['dim']}{reason}{c['reset']}\n  {link}")


async def convert_mega_tdata(config):
    """Convert every downloaded tdata in mega_tdata/ into new_sessions/."""
    c = _C
    print(f"\n{c['cyan']}{c['bold']}🔄 Конвертация mega_tdata → new_sessions/{c['reset']}")

    accounts = [d for d in sorted(MEGA_TDATA_DIR.iterdir()) if d.is_dir()]
    if not accounts:
        print(f"{c['yel']}Папка mega_tdata/ пуста — сначала импортируй с MEGA.{c['reset']}")
        return

    print(f"{c['dim']}Найдено tdata: {len(accounts)}. Конвертация идёт в отдельном "
          f"процессе (opentele), сессии → new_sessions/.{c['reset']}")
    if input("Начать? (yes/n): ").strip().lower() not in ("yes", "y", "да"):
        print(f"{c['dim']}Отменено.{c['reset']}")
        return

    script = BASE_DIR / "tdata_convert.py"
    done = skipped = 0
    bad = []  # (name, reason)
    for acct_dir in accounts:
        if shutdown_event.is_set():
            break
        name = acct_dir.name
        out_session = NEW_SESSIONS_DIR / name
        if (NEW_SESSIONS_DIR / f"{name}.session").exists():
            logger.info(f"{name}: session already exists, skip")
            skipped += 1
            continue

        tdata_dir = _find_tdata_dir(acct_dir)
        if not tdata_dir:
            logger.warning(f"{name}: no tdata folder found")
            bad.append((name, "no tdata folder"))
            continue

        # Isolated subprocess: opentele monkeypatches telethon on import.
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, str(script), str(tdata_dir), str(out_session)],
            capture_output=True, text=True,
        )
        out_lines = [l.strip() for l in (result.stdout + result.stderr).splitlines() if l.strip()]
        for line in out_lines:
            logger.info(f"  {line}")
        if (NEW_SESSIONS_DIR / f"{name}.session").exists():
            done += 1
            logger.info(f"{name}: converted → new_sessions/")
        else:
            reason = next((l for l in out_lines if "Skipping" in l or "Error" in l
                           or "Failed" in l or "failed" in l), "unknown error")
            bad.append((name, reason))

    print(f"\n{c['grn']}Готово: сконвертировано {done}, пропущено {skipped}, "
          f"ошибок {len(bad)}.{c['reset']}")
    if bad:
        report = BASE_DIR / "convert_failed.txt"
        report.write_text("\n".join(f"{n}  # {r}" for n, r in bad) + "\n", encoding="utf-8")
        print(f"{c['yel']}Не сконвертировались ({len(bad)}) — в convert_failed.txt:{c['reset']}")
        for n, r in bad:
            print(f"  {c['dim']}{n}: {r}{c['reset']}")


YANDEX_LINKS_FILE = BASE_DIR / "yandex_links.txt"
YANDEX_2FA_FILE = BASE_DIR / "yandex_2fa.txt"
YANDEX_FAILED_FILE = BASE_DIR / "yandex_failed.txt"
YANDEX_TMP_DIR = BASE_DIR / "yandex_tmp"
YANDEX_API = "https://cloud-api.yandex.net/v1/disk/public/resources"


def _yandex_meta(link, path=None):
    import requests
    params = {"public_key": link, "limit": 1000}
    if path:
        params["path"] = path
    r = requests.get(YANDEX_API, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _yandex_download(link, dest_path: Path, path=None):
    """Download a public Yandex.Disk file (or a whole folder as a zip)."""
    import requests
    params = {"public_key": link}
    if path:
        params["path"] = path
    href = requests.get(YANDEX_API + "/download", params=params,
                        timeout=30).json()["href"]
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(href, stream=True, timeout=300) as resp:
        resp.raise_for_status()
        with open(dest_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                fh.write(chunk)


def _grab_2fa_from_json(root: Path, acct: str, out_file: Path):
    """Pull the twoFA password out of any account .json in the tree."""
    for jp in root.rglob("*.json"):
        try:
            data = json.loads(jp.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        pw = data.get("twoFA") or data.get("two_fa") or data.get("2fa")
        if pw:
            try:
                with open(out_file, "a", encoding="utf-8") as fh:
                    fh.write(f"{acct}:{pw}\n")
            except Exception:
                pass
            return True
    return False


def _import_session_or_tdata(root: Path, fallback_name: str, twofa_file: Path):
    """From an extracted archive: prefer a ready .session, else convert tdata.
    Returns (status, name): status ∈ session|tdata|skip|fail|none."""
    fallback_name = fallback_name.lstrip("+")
    # 1) A ready .session anywhere — just copy it in, no conversion needed.
    sess = next((p for p in root.rglob("*.session")), None)
    if sess:
        name = sess.stem.lstrip("+") or fallback_name
        dest = NEW_SESSIONS_DIR / f"{name}.session"
        if dest.exists():
            return ("skip", name)
        NEW_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy(sess, dest)
        _grab_2fa_from_json(root, name, twofa_file)
        return ("session", name)

    # 2) No session — fall back to converting tdata in an isolated subprocess.
    tdata_dir = _find_tdata_dir(root)
    if tdata_dir:
        name = fallback_name
        out_session = NEW_SESSIONS_DIR / name
        if (NEW_SESSIONS_DIR / f"{name}.session").exists():
            return ("skip", name)
        subprocess.run(
            [sys.executable, str(BASE_DIR / "tdata_convert.py"),
             str(tdata_dir), str(out_session)],
            capture_output=True, text=True,
        )
        if (NEW_SESSIONS_DIR / f"{name}.session").exists():
            _grab_2fa_from_json(root, name, twofa_file)
            return ("tdata", name)
        return ("fail", name)
    return ("none", fallback_name)


async def import_from_yandex(config):
    c = _C
    print(f"\n{c['cyan']}{c['bold']}📥 Импорт сессий/tdata с Яндекс.Диска{c['reset']}")
    try:
        import requests  # noqa: F401
    except ImportError:
        print(f"{c['yel']}Нужен пакет requests: pip install requests{c['reset']}")
        return

    # Either paste one link, or keep a list in yandex_links.txt.
    pasted = input("Вставь ссылку Я.Диска (Enter — читать yandex_links.txt): ").strip()
    if pasted:
        links = [pasted]
    else:
        if not YANDEX_LINKS_FILE.exists():
            print(f"{c['yel']}Создай yandex_links.txt и вставь туда ссылки "
                  f"(по одной в строке), либо вставь ссылку прямо сейчас.{c['reset']}")
            return
        links = [l.strip() for l in YANDEX_LINKS_FILE.read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.strip().startswith("#")]
    if not links:
        print(f"{c['yel']}Нет ссылок для импорта.{c['reset']}")
        return

    print(f"{c['dim']}Ссылок: {len(links)}. Готовые .session кладутся сразу в "
          f"new_sessions/, tdata — конвертируются. 2FA (twoFA) → {YANDEX_2FA_FILE.name}.{c['reset']}")
    if input("Начать? (yes/n): ").strip().lower() not in ("yes", "y", "да"):
        print(f"{c['dim']}Отменено.{c['reset']}")
        return

    got = skipped = 0
    bad = []  # (link, reason)
    started = time.time()

    for i, link in enumerate(links, 1):
        if shutdown_event.is_set():
            break
        try:
            meta = await asyncio.to_thread(_yandex_meta, link)
        except Exception as e:
            logger.error(f"[{i}/{len(links)}] meta failed: {e}\n    ↳ {link}")
            bad.append((link, f"meta failed: {e}"))
            continue

        # A public link is either a single file (zip/session) or a folder of them.
        if meta.get("type") == "file":
            items = [{"name": meta.get("name", f"acct{i}"), "path": None}]
        else:
            items = [{"name": it["name"], "path": it["path"]}
                     for it in meta.get("_embedded", {}).get("items", [])
                     if it.get("type") == "file"
                     and it["name"].lower().endswith((".zip", ".session"))]
            if not items:
                logger.warning(f"[{i}/{len(links)}] no zip/session inside\n    ↳ {link}")
                bad.append((link, "no zip/session inside"))
                continue

        for it in items:
            fname = it["name"]
            base = fname.rsplit(".", 1)[0]
            tmp = YANDEX_TMP_DIR / f"_{i}_{base}"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True, exist_ok=True)
            local = tmp / fname
            try:
                await asyncio.to_thread(_yandex_download, link, local, it["path"])
            except Exception as e:
                logger.error(f"[{i}/{len(links)}] {fname}: download failed: {e}")
                bad.append((link, f"{fname}: download failed: {e}"))
                shutil.rmtree(tmp, ignore_errors=True)
                continue

            extract_root = tmp
            if local.suffix.lower() == ".zip":
                try:
                    with zipfile.ZipFile(local) as zf:
                        zf.extractall(tmp / "unz")
                    extract_root = tmp / "unz"
                except Exception as e:
                    logger.error(f"[{i}/{len(links)}] {fname}: bad zip: {e}")
                    bad.append((link, f"{fname}: bad zip: {e}"))
                    shutil.rmtree(tmp, ignore_errors=True)
                    continue

            status, name = _import_session_or_tdata(extract_root, base, YANDEX_2FA_FILE)
            if status in ("session", "tdata"):
                got += 1
                logger.info(f"[{i}/{len(links)}] {name}: импортирован ({status}) → new_sessions/")
            elif status == "skip":
                skipped += 1
                logger.info(f"[{i}/{len(links)}] {name}: уже есть, пропуск")
            else:
                reason = "нет .session и tdata" if status == "none" else "конвертация не удалась"
                logger.warning(f"[{i}/{len(links)}] {name}: {reason}")
                bad.append((link, f"{name}: {reason}"))
            shutil.rmtree(tmp, ignore_errors=True)

        log_eta(i, len(links), time.time() - started, [0, 1])

    print(f"\n{c['grn']}Готово: импортировано {got}, пропущено {skipped}, "
          f"ошибок {len(bad)}.{c['reset']}")
    if YANDEX_2FA_FILE.exists():
        print(f"{c['dim']}2FA-пароли (где были) → {YANDEX_2FA_FILE.name}{c['reset']}")
    if bad:
        YANDEX_FAILED_FILE.write_text(
            "\n".join(f"{link}  # {reason}" for link, reason in bad) + "\n",
            encoding="utf-8")
        print(f"\n{c['yel']}Проблемные ({len(bad)}) — в {YANDEX_FAILED_FILE.name}:{c['reset']}")
        for link, reason in bad:
            print(f"  {c['dim']}{reason}{c['reset']}\n  {link}")


async def run_add_sessions(config):
    c = _C
    print(f"\n{c['cyan']}{c['bold']}➕ Добавить сессии (телефон + код){c['reset']}")

    target = prompt_menu("Куда сохранять сессии?", [
        ("📁 new_sessions/ (на прогрев)", NEW_SESSIONS_DIR),
        ("📁 sessions/ (сразу в фарм)", SESSIONS_DIR),
    ])
    if target is None:
        return
    out_dir = target
    api_id, api_hash = int(config["api_id"]), str(config["api_hash"])
    added = 0

    while not shutdown_event.is_set():
        phone = input(f"\n{c['grn']}Номер телефона (+79001234567), Enter — выход:{c['reset']} ").strip()
        if not phone:
            break
        name = phone.replace("+", "").replace(" ", "")
        if (out_dir / f"{name}.session").exists():
            print(f"{c['yel']}Сессия {name} уже есть в {out_dir.name}/ — пропуск.{c['reset']}")
            continue

        client = TelegramClient(str(out_dir / name), api_id, api_hash)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.send_code_request(phone)
                code = input(f"{c['grn']}Код из Telegram:{c['reset']} ").strip()
                try:
                    await client.sign_in(phone, code)
                except errors.SessionPasswordNeededError:
                    pw = input(f"{c['grn']}Пароль 2FA:{c['reset']} ").strip()
                    await client.sign_in(password=pw)
            me = await client.get_me()
            added += 1
            print(f"{c['grn']}✓ {me.first_name} ({me.id}) → {out_dir.name}/{name}.session{c['reset']}")
        except Exception as e:
            print(f"{c['yel']}Ошибка: {e}{c['reset']}")
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    print(f"\n{c['grn']}Добавлено сессий: {added}.{c['reset']}")


async def run_check_sessions(config):
    """Validate .session files: connect + is_authorized + get_me, and bucket
    each account into alive / not-authorized / banned / error so it's clear
    whether the sessions are dead ('AuthKeyUnregistered' → авторизация убита)
    or the accounts themselves are gone ('UserDeactivatedBan' → бан)."""
    c = _C
    print(f"\n{c['cyan']}{c['bold']}🩺 Проверка сессий{c['reset']}")

    folder = prompt_menu("Какую папку проверить?", [
        ("📁 new_sessions/ (новые)", "new"),
        ("📁 sessions/ (рабочие)", "old"),
        ("📁 обе папки", "both"),
    ])
    if folder is None:
        return
    if folder == "new":
        files = get_session_files(NEW_SESSIONS_DIR)
    elif folder == "old":
        files = get_session_files(SESSIONS_DIR)
    else:
        files = get_session_files(NEW_SESSIONS_DIR) + get_session_files(SESSIONS_DIR)

    if not files:
        print(f"{c['yel']}В выбранной папке нет сессий.{c['reset']}")
        return

    total = len(files)
    print(f"{c['dim']}Проверяю {total} сессий… (connect + get_me по каждой){c['reset']}")

    api_id, api_hash = int(config["api_id"]), str(config["api_hash"])
    alive, not_auth, banned, err = [], [], [], []

    for i, path in enumerate(files, 1):
        name = path.stem
        client = TelegramClient(str(path.with_suffix("")), api_id, api_hash)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                not_auth.append(name)
                print(f"  {c['yel']}[{i}/{total}] ❌ {name} — не авторизована{c['reset']}")
            else:
                me = await client.get_me()
                if me is None:
                    not_auth.append(name)
                    print(f"  {c['yel']}[{i}/{total}] ❌ {name} — get_me пустой{c['reset']}")
                else:
                    tag = f"@{me.username}" if me.username else str(me.id)
                    alive.append(name)
                    print(f"  {c['grn']}[{i}/{total}] ✅ {name} — {me.first_name or ''} ({tag}){c['reset']}")
        except Exception as e:
            etype = type(e).__name__
            if "Deactivated" in etype or "Banned" in etype or "UserDeactivated" in etype:
                banned.append(name)
                print(f"  {c['mag']}[{i}/{total}] 🚫 {name} — забанен/удалён ({etype}){c['reset']}")
            elif "AuthKey" in etype or "Unauthorized" in etype or "SessionRevoked" in etype:
                not_auth.append(name)
                print(f"  {c['yel']}[{i}/{total}] ❌ {name} — авторизация убита ({etype}){c['reset']}")
            else:
                err.append(name)
                print(f"  {c['dim']}[{i}/{total}] ⚠️  {name} — ошибка: {etype}: {e}{c['reset']}")
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    print(f"\n{c['bold']}Итог по {total} сессиям:{c['reset']}")
    print(f"  {c['grn']}✅ живых:            {len(alive)}{c['reset']}")
    print(f"  {c['yel']}❌ не авторизованы:  {len(not_auth)}{c['reset']}")
    print(f"  {c['mag']}🚫 забанены/удалены: {len(banned)}{c['reset']}")
    print(f"  {c['dim']}⚠️  ошибки сети/др.:  {len(err)}{c['reset']}")

    # Write dead session names to a file so they can be pulled out of the folder.
    dead = not_auth + banned
    if dead:
        report = BASE_DIR / "dead_sessions.txt"
        report.write_text("\n".join(dead) + "\n", encoding="utf-8")
        print(f"\n{c['dim']}Список мёртвых сохранён в {report.name} "
              f"({len(dead)} шт.).{c['reset']}")

    # Interpretation hint.
    if total and len(not_auth) == total:
        print(f"\n{c['yel']}Все до одной «не авторизованы» — это почти всегда "
              f"массовый сброс авторизаций телеграмом при заходе с одного "
              f"IP/региона. Попробуй прокси (SOCKS5 на аккаунт).{c['reset']}")
    elif banned and not alive:
        print(f"\n{c['mag']}Аккаунты забанены — это уже сам товар, а не сессии; "
              f"прокси тут не поможет.{c['reset']}")


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


def pick_session(title):
    """Choose a .session file: pick folder, optional name search, then select."""
    c = _C
    folder = prompt_menu(f"{title} — из какой папки?", [
        ("📁 new_sessions/ (новые)", "new"),
        ("📁 sessions/ (рабочие)", "old"),
        ("📁 обе папки", "both"),
    ])
    if folder is None:
        return None

    if folder == "new":
        files = get_session_files(NEW_SESSIONS_DIR)
    elif folder == "old":
        files = get_session_files(SESSIONS_DIR)
    else:
        files = get_session_files(NEW_SESSIONS_DIR) + get_session_files(SESSIONS_DIR)

    if not files:
        print(f"{c['yel']}В выбранной папке нет сессий.{c['reset']}")
        return None

    query = input(f"{c['grn']}Поиск по имени/номеру (Enter — показать все):{c['reset']} ").strip().lower()
    if query:
        files = [f for f in files if query in f.stem.lower()]
    if not files:
        print(f"{c['yel']}Ничего не найдено по '{query}'.{c['reset']}")
        return None

    for i, p in enumerate(files, 1):
        print(f"  {c['yel']}{i}{c['reset']}) {p.stem}  {c['dim']}({p.parent.name}/){c['reset']}")
    raw = input(f"{c['grn']}Выбери номер:{c['reset']} ").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= len(files)):
        print(f"{c['dim']}Неверный выбор.{c['reset']}")
        return None
    return files[int(raw) - 1]


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
                ("🪐 Asteroid Shiba", "asteroid"),
                ("🔁 Комбо: Рилс 6ч + Астероиды 24ч", "combo"),
            ])
            if sub:
                return sub
        elif category == "topup_warm":
            sub = prompt_menu("Пополняшки и прогрев:", [
                ("✍️  Написать боту", "write_bot"),
                ("⭐ Пополнить старс", "topup_stars"),
                ("👁 Просмотры постов канала", "views"),
                ("🎯 Снайпер комментариев (первый коммент)", "sniper"),
            ])
            if sub:
                return sub
        elif category == "secure_cat":
            sub = prompt_menu("Безопасность аккаунтов:", [
                ("➕ Добавить сессии (телефон + код)", "add_sessions"),
                ("🩺 Проверить сессии (живые/мёртвые)", "check_sessions"),
                ("🔐 Обезопасить (2FA и/или сброс сессий)", "secure"),
                ("🔥 Прогрев (аватар + юзернейм)", "warm"),
                ("📲 Прослушка кода входа (зайти по сессии)", "listen_code"),
                ("📦 Импорт tdata из zip → new_sessions/", "import_tdata"),
                ("📥 Импорт tdata с MEGA (по ссылкам)", "import_mega"),
                ("📥 Импорт сессий/tdata с Яндекс.Диска", "import_yandex"),
                ("🔄 Конвертировать mega_tdata → сессии", "convert_mega"),
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

    # Pick which operations to run — they're independent.
    print(f"{c['dim']}Отметь, что делать (сессии сбрасывать не обязательно — "
          f"они уже твои; часто нужен только новый пароль):{c['reset']}")

    def ask(q, default_yes=True):
        d = "y" if default_yes else "n"
        ans = input(f"{c['grn']}{q} (y/n) [{d}]:{c['reset']} ").strip().lower()
        if not ans:
            return default_yes
        return ans in ("y", "yes", "да")

    do_2fa = ask("🔑 Сменить 2FA пароль", default_yes=True)
    do_reset = ask("🧹 Сбросить чужие сессии", default_yes=False)

    if not (do_2fa or do_reset):
        print(f"{c['yel']}Ничего не выбрано — отмена.{c['reset']}")
        return

    old_pw = new_pw = hint = ""
    if do_2fa:
        old_pw = input("Текущий 2FA пароль (Enter — если 2FA не стоит): ").strip()
        new_pw = input("Новый 2FA пароль: ").strip()
        if not new_pw:
            print(f"{c['yel']}Новый пароль пустой — отмена.{c['reset']}")
            return
        hint = input("Подсказка к паролю (Enter — пропустить): ").strip()

    keep_device = ""
    if do_reset:
        print(f"{c['dim']}Чтобы НЕ выкинуть своё устройство при сбросе сессий, укажи "
              f"часть его названия (например 'Nitro' или 'Desktop').{c['reset']}")
        keep_device = input("Не трогать устройство с названием (Enter — сбросить все чужие): ").strip()

    ops = []
    if do_2fa:
        ops.append("сменён 2FA")
    if do_reset:
        ops.append("сброшены чужие сессии"
                   + (f" (кроме «{keep_device}»)" if keep_device else ""))
    print(f"\n{c['yel']}Будет обработано {count} аккаунт(ов) из new_sessions/: "
          f"{', '.join(ops)}.{c['reset']}")
    if input("Продолжить? (yes/n): ").strip().lower() not in ("yes", "y", "да"):
        print(f"{c['dim']}Отменено.{c['reset']}")
        return

    await secure_cycle(config, old_pw, new_pw, hint, do_reset=do_reset, do_2fa=do_2fa, keep_device=keep_device)


async def run_warm(config):
    c = _C
    print(f"\n{c['cyan']}{c['bold']}🔥 Прогрев аккаунтов{c['reset']}")
    print(f"{c['dim']}Работает с папкой new_sessions/. Аватар берётся из avatars/ "
          f"(каждый раз новый, использованные уходят в avatars/_used/), "
          f"юзернейм — из nicknames.txt.{c['reset']}")

    count = len(get_session_files(NEW_SESSIONS_DIR))
    if count == 0:
        print(f"{c['yel']}Папка new_sessions/ пуста.{c['reset']}")
        return

    def ask(q):
        return input(f"{c['grn']}{q} (y/n) [y]:{c['reset']} ").strip().lower() in ("", "y", "yes", "да")

    print(f"{c['dim']}Отметь, что менять при прогреве:{c['reset']}")
    set_avatar = ask("🖼️  Ставить аватар")
    set_name = ask("📝 Менять имя профиля")
    set_username = ask("🏷️  Менять юзернейм")

    if not (set_avatar or set_name or set_username):
        print(f"{c['yel']}Ничего не выбрано — отмена.{c['reset']}")
        return
    if set_avatar and count_avatars() == 0:
        print(f"{c['yel']}В папке avatars/ нет фото — положи туда картинки "
              f"(.jpg/.png) и запусти снова.{c['reset']}")
        return
    if set_name and not load_names():
        print(f"{c['yel']}names.txt пуст.{c['reset']}")
        return
    if set_username and not load_nicks():
        print(f"{c['yel']}nicknames.txt пуст.{c['reset']}")
        return

    avatars_n = count_avatars()
    print(f"\n{c['yel']}Аккаунтов: {count}. "
          f"{'Аватарок в пуле: ' + str(avatars_n) + '. ' if set_avatar else ''}"
          f"{'Имён: ' + str(len(load_names())) + '. ' if set_name else ''}"
          f"{'Ников: ' + str(len(load_nicks())) + '. ' if set_username else ''}{c['reset']}")
    if set_avatar and avatars_n < count:
        print(f"{c['dim']}Аватарок меньше, чем аккаунтов — на часть не хватит.{c['reset']}")
    if set_name and len(load_names()) < count:
        print(f"{c['dim']}Имён меньше, чем аккаунтов — на часть не хватит.{c['reset']}")
    if set_username and len(load_nicks()) < count:
        print(f"{c['dim']}Ников меньше, чем аккаунтов — на часть может не хватить.{c['reset']}")
    if input("Продолжить? (yes/n): ").strip().lower() not in ("yes", "y", "да"):
        print(f"{c['dim']}Отменено.{c['reset']}")
        return

    await warm_cycle(config, set_avatar, set_name, set_username)


def _folder_files(title):
    folder = prompt_menu(f"{title} — из какой папки?", [
        ("📁 new_sessions/ (новые)", "new"),
        ("📁 sessions/ (рабочие)", "old"),
        ("📁 обе папки", "both"),
    ])
    if folder is None:
        return None
    if folder == "new":
        return get_session_files(NEW_SESSIONS_DIR)
    if folder == "old":
        return get_session_files(SESSIONS_DIR)
    return get_session_files(NEW_SESSIONS_DIR) + get_session_files(SESSIONS_DIR)


async def choose_folder_sessions(title):
    return _folder_files(title)


def _parse_selection(raw, n):
    picked = set()
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            if a.isdigit() and b.isdigit():
                for i in range(int(a), int(b) + 1):
                    if 1 <= i <= n:
                        picked.add(i)
        elif part.isdigit() and 1 <= int(part) <= n:
            picked.add(int(part))
    return sorted(picked)


async def multi_pick_sessions(title):
    """Choose folder, optional search, then multi-select by numbers/ranges."""
    c = _C
    files = _folder_files(title)
    if not files:
        return None
    query = input(f"{c['grn']}Поиск по имени/номеру (Enter — все):{c['reset']} ").strip().lower()
    if query:
        files = [f for f in files if query in f.stem.lower()]
    if not files:
        print(f"{c['yel']}Ничего не найдено.{c['reset']}")
        return None

    for i, p in enumerate(files, 1):
        print(f"  {c['yel']}{i}{c['reset']}) {p.stem}  {c['dim']}({p.parent.name}/){c['reset']}")
    raw = input(f"{c['grn']}Номера через запятую (1,3,5-8) или 'all':{c['reset']} ").strip().lower()
    if raw in ("all", "все", "*"):
        return files
    idx = _parse_selection(raw, len(files))
    if not idx:
        print(f"{c['yel']}Ничего не выбрано.{c['reset']}")
        return None
    return [files[i - 1] for i in idx]


async def run_stars(config):
    c = _C
    print(f"\n{c['cyan']}{c['bold']}⭐ Пополнение Stars (Split){c['reset']}")

    key = load_split_key()
    if not key:
        print(f"{c['yel']}Нет ключа. Скопируй split_api_key.example.txt → "
              f"split_api_key.txt и вставь свой токен с split.tg/partner.{c['reset']}")
        return

    headers = {"Authorization": f"Bearer {key}"}
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
        # Show current Split balance.
        try:
            async with s.get(f"{SPLIT_API_BASE}/balance/") as r:
                bal = (await r.json()).get("message", {})
            ton = bal.get("ton_balance", {}).get("real", {}).get("available", 0) or 0
            usdt = bal.get("usdt_balance", {}).get("available", 0) or 0
            print(f"{c['dim']}Баланс Split: ~{ton/1e9:.4f} TON, ~{usdt/1e6:.2f} USDT{c['reset']}")
        except Exception as e:
            print(f"{c['yel']}Не удалось получить баланс: {e}{c['reset']}")

        action = prompt_menu("Что делаем?", [
            ("⭐ Купить Stars на аккаунты", "buy"),
            ("💵 Пополнить баланс Split (перевод из Tonkeeper)", "topup"),
        ])
        if action is None:
            return

        if action == "topup":
            try:
                amt = float(input("Сколько TON пополнить: ").strip())
            except ValueError:
                print(f"{c['yel']}Некорректная сумма.{c['reset']}")
                return
            nano = int(amt * 1e9)
            try:
                async with s.post(f"{SPLIT_API_BASE}/balance/ton/top-up",
                                  json={"amount_nanoton": nano}) as r:
                    data = await r.json()
            except Exception as e:
                print(f"{c['yel']}Ошибка: {e}{c['reset']}")
                return
            tx = (data.get("message") or {}).get("transaction") or {}
            msgs = tx.get("messages") or []
            if msgs:
                m = msgs[0]
                addr = m.get("address")
                amount_nano = int(m.get("amount", 0))
                payload = m.get("payload")
                # Build a Tonkeeper deeplink so the payload (which identifies
                # your deposit) is attached — a plain manual send WITHOUT this
                # payload will NOT be credited by Split.
                link = f"https://app.tonkeeper.com/transfer/{addr}?amount={amount_nano}"
                if payload:
                    link += f"&bin={quote(payload, safe='')}"

                # Save as a single line (terminal wrapping breaks copy-paste).
                link_file = BASE_DIR / "topup_link.txt"
                link_file.write_text(link, encoding="utf-8")

                print(f"\n{c['grn']}Оплати пополнение {amount_nano/1e9:.4f} TON — "
                      f"через ссылку или QR ниже (Tonkeeper подставит всё сам):{c['reset']}")
                # Show a scannable QR in the terminal if qrcode is installed.
                try:
                    import qrcode
                    qr = qrcode.QRCode(border=1)
                    qr.add_data(link)
                    qr.make(fit=True)
                    qr.print_ascii(invert=True)
                    print(f"{c['dim']}Отсканируй QR телефоном с Tonkeeper.{c['reset']}")
                except ImportError:
                    print(f"{c['dim']}(QR отключён — установи 'qrcode' для показа QR){c['reset']}")

                print(f"{c['dim']}Ссылка сохранена одной строкой в topup_link.txt "
                      f"(открой на телефоне с Tonkeeper).{c['reset']}")
                print(f"{c['dim']}Payload обязателен — без него Split не зачислит депозит. "
                      f"Баланс обновится через ~1-2 мин (перезайди в раздел).{c['reset']}")
            else:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            return

        # action == buy
        try:
            qty = int(input("Сколько Stars (мин. 50): ").strip())
        except ValueError:
            print(f"{c['yel']}Некорректное число.{c['reset']}")
            return
        if qty < 50:
            print(f"{c['yel']}Минимум 50 Stars.{c['reset']}")
            return

        # Price estimate.
        try:
            async with s.post(f"{SPLIT_API_BASE}/buy/estimate",
                              json={"quantity": qty, "product": "stars", "method": "balance"}) as r:
                price = (await r.json()).get("message", {})
            print(f"{c['dim']}Цена за {qty}★: {price.get('amount')} {price.get('currency')} "
                  f"(~${price.get('usd_amount')}){c['reset']}")
        except Exception as e:
            print(f"{c['dim']}Оценка цены недоступна: {e}{c['reset']}")

        async def buy_for(uname):
            body = {"username": uname, "quantity": qty, "payment_method": "balance"}
            async with s.post(f"{SPLIT_API_BASE}/buy/stars", json=body) as r:
                return await r.json()

        target = prompt_menu("Кому покупаем?", [
            ("👤 Одному — ввести @username вручную", "single"),
            ("✅ Выбрать аккаунты из папки", "select"),
            ("📂 Все аккаунты из папки", "all"),
        ])
        if target is None:
            return

        # Single: buy for a manually typed username, no sessions needed.
        if target == "single":
            uname = input("@username получателя: ").strip().lstrip("@")
            if not uname:
                print(f"{c['yel']}Пустой username.{c['reset']}")
                return
            if input(f"Купить {qty}★ для @{uname}? (yes/n): ").strip().lower() not in ("yes", "y", "да"):
                return
            try:
                res = await buy_for(uname)
                if res.get("ok"):
                    print(f"{c['grn']}@{uname}: куплено {qty}★{c['reset']}")
                else:
                    print(f"{c['yel']}@{uname}: {res.get('error_message') or res}{c['reset']}")
            except Exception as e:
                print(f"{c['yel']}Ошибка: {e}{c['reset']}")
            return

        # Batch: selected or all sessions from a folder.
        if target == "select":
            sessions = await multi_pick_sessions("Купить Stars")
        else:
            sessions = await choose_folder_sessions("Купить Stars")
        if not sessions:
            return

        print(f"\n{c['yel']}Аккаунтов: {len(sessions)}, по {qty}★ каждому "
              f"(оплата с баланса Split).{c['reset']}")
        if input("Продолжить? (yes/n): ").strip().lower() not in ("yes", "y", "да"):
            print(f"{c['dim']}Отменено.{c['reset']}")
            return

        api_id, api_hash = config["api_id"], config["api_hash"]
        delay_range = config.get("delay_between_accounts_sec", [5, 30])
        bought = 0

        for session_path in sessions:
            if shutdown_event.is_set():
                break
            label = session_path.stem
            client = TelegramClient(str(session_path.with_suffix("")), int(api_id), str(api_hash))
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    logger.warning(f"[{label}] Not authorized, skipping")
                    continue
                me = await client.get_me()
                uname = getattr(me, "username", None)
                if not uname:
                    logger.warning(f"[{label}] no @username — прогрей аккаунт сначала, пропуск")
                    continue
                res = await buy_for(uname)
                if res.get("ok"):
                    logger.info(f"[{label}] @{uname}: bought {qty}★")
                    bought += 1
                else:
                    logger.error(f"[{label}] @{uname}: {res.get('error_message') or res}")
            except Exception as e:
                logger.error(f"[{label}] error: {e}")
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass

            if not shutdown_event.is_set() and session_path != sessions[-1]:
                await asyncio.sleep(random.uniform(*delay_range))

        print(f"\n{c['grn']}Готово: Stars куплены на {bought} аккаунт(ов).{c['reset']}")


# ── Asteroid Shiba farming ───────────────────────────────────────────────────

ASTEROID_WEB = "https://www.asteroidshiba.online"


async def asteroid_init_data(client):
    bot = await client.get_entity(ASTEROID_BOT)
    full = await client(functions.users.GetFullUserRequest(bot))
    mb = full.full_user.bot_info.menu_button if full.full_user.bot_info else None
    url = mb.url if isinstance(mb, types.BotMenuButton) else None
    if not url:
        # Fall back to a web_app button on the bot's recent messages.
        for m in await client.get_messages(bot, limit=5):
            if not m.buttons:
                continue
            for row in m.buttons:
                for b in row:
                    bb = getattr(b, "button", None)
                    if isinstance(bb, (types.KeyboardButtonWebView,
                                       types.KeyboardButtonSimpleWebView)):
                        url = bb.url
                        break
    if not url:
        raise RuntimeError("no web app URL for the bot")

    res = await client(functions.messages.RequestWebViewRequest(
        peer=bot, bot=bot, platform="android", url=url, from_bot_menu=True))
    frag = parse_qs(urlparse(res.url).fragment)
    init_data = frag.get("tgWebAppData", [None])[0]
    if not init_data:
        raise RuntimeError("tgWebAppData not found")
    return init_data


def _asteroid_body(init_data):
    user = json.loads(parse_qs(init_data).get("user", ["{}"])[0])
    return {
        "firstName": user.get("first_name", ""),
        "initData": init_data,
        "photoUrl": user.get("photo_url", ""),
        "startParam": parse_qs(init_data).get("start_param", [""])[0],
        "telegramId": user.get("id"),
        "username": user.get("username", ""),
    }


ASTEROID_SKIP_FILE = BASE_DIR / "asteroid_skip.txt"


def load_asteroid_skip():
    if ASTEROID_SKIP_FILE.exists():
        return {l.strip() for l in ASTEROID_SKIP_FILE.read_text(encoding="utf-8").splitlines() if l.strip()}
    return set()


def add_asteroid_skip(label):
    with open(ASTEROID_SKIP_FILE, "a", encoding="utf-8") as f:
        f.write(label + "\n")


async def _asteroid_onboard(client, alog):
    """Fresh account: referral start + join channels + subscriptions."""
    try:
        await client.send_message(ASTEROID_BOT, f"/start {ASTEROID_REF}")
        alog.info("Started bot with referral")
    except errors.FloodWaitError as e:
        alog.warning(f"FloodWait {e.seconds}s on /start")
    except Exception as e:
        alog.error(f"/start failed: {e}")
    await asyncio.sleep(random.uniform(2, 4))

    for ch in ASTEROID_CHANNELS:  # joins are the most flood-sensitive → ~10s gap
        try:
            await client(functions.channels.JoinChannelRequest(ch))
            alog.info(f"Joined @{ch}")
        except errors.FloodWaitError as e:
            alog.warning(f"FloodWait {e.seconds}s joining @{ch}")
        except Exception as e:
            alog.info(f"@{ch}: {type(e).__name__} ({e})")
        await asyncio.sleep(random.uniform(9, 12))


async def asteroid_account(client, account_label):
    account_logger = logging.getLogger(f"reelsio-claimer.{account_label}")
    account_logger.handlers = logger.handlers
    account_logger.propagate = False
    account_logger.setLevel(logging.INFO)

    init_data = await asteroid_init_data(client)
    body = _asteroid_body(init_data)
    headers = {
        "Content-Type": "application/json",
        "Origin": ASTEROID_WEB,
        "Referer": ASTEROID_WEB + "/",
    }
    async with aiohttp.ClientSession(headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=30)) as s:
        async def post(path, extra=None):
            async with s.post(f"{ASTEROID_WEB}{path}", json={**body, **(extra or {})}) as r:
                try:
                    return await r.json(content_type=None)
                except Exception:
                    return {"raw": (await r.text())[:200]}

        # 1) Is this account already claimed (has the Lv0 shiba)?
        claim = await post("/api/prelaunch/claim")
        has_shiba = (claim.get("status") or {}).get("has_shiba_lv0")

        # 2) If not, onboard once (referral + channels + subs) and re-claim.
        if not has_shiba:
            account_logger.info("No shiba yet — onboarding")
            await _asteroid_onboard(client, account_logger)
            await post("/api/prelaunch/subscriptions")
            await asyncio.sleep(random.uniform(2, 4))
            claim = await post("/api/prelaunch/claim")
            has_shiba = (claim.get("status") or {}).get("has_shiba_lv0")
            if not has_shiba:
                account_logger.warning("Still no shiba after onboarding — skip-listing")
                add_asteroid_skip(account_label)
                return
            account_logger.info("Shiba claimed")

        # 3) Farm the daily asteroid discoveries (up to ~5/day).
        farmed = 0
        for _ in range(6):
            if shutdown_event.is_set():
                break
            res = await post("/api/prelaunch/status", {
                "action": "perform_discovery",
                "idempotencyKey": f"discovery-{uuid.uuid4()}",
            })
            if not res.get("ok"):
                account_logger.info(f"Discovery stopped: {res}")
                break
            disc = res.get("discovery") or {}
            farmed += 1
            account_logger.info(
                f"Discovery {farmed}: {disc.get('outcome')} +{disc.get('reward_astro')} ASTRO "
                f"(balance {disc.get('astro_balance')}, signals left {disc.get('ready_signals')})"
            )
            if (disc.get("ready_signals") or 0) <= 0:
                break
            await asyncio.sleep(random.uniform(1, 3))

        if farmed == 0:
            account_logger.info("No signals ready (daily limit done / resetting)")
        else:
            account_logger.info(f"Farmed {farmed} asteroid(s)")

        # 4) Collect accrued asteroid income, if any.
        await asyncio.sleep(random.uniform(1, 3))
        st = await post("/api/prelaunch/status", {"action": "season1_status"})
        status = st.get("status") or {}
        collectable = status.get("collectable_astro") or 0
        account_logger.info(
            f"Portfolio: {status.get('asteroid_count')} asteroid(s), "
            f"balance {status.get('astro_balance')}, "
            f"{status.get('daily_income_astro')}/day, collectable {collectable}"
        )
        if collectable > 0:
            await asyncio.sleep(random.uniform(1, 3))
            col = await post("/api/prelaunch/status", {
                "action": "collect_asteroid_income",
                "idempotencyKey": f"collect-all-{uuid.uuid4()}",
                "userAsteroidId": None,
            })
            if col.get("ok"):
                cc = col.get("collection") or {}
                account_logger.info(
                    f"Collected +{cc.get('collected_astro')} ASTRO from "
                    f"{cc.get('collected_asteroids')} asteroid(s) "
                    f"(balance {cc.get('astro_balance')})"
                )
            else:
                account_logger.warning(f"Collect failed: {col}")


async def run_asteroid(config):
    c = _C
    print(f"\n{c['cyan']}{c['bold']}🪐 Asteroid Shiba — фарм{c['reset']}")
    print(f"{c['dim']}Клейм шибы (для новых) + ежедневная охота на 5 астероидов. "
          f"Аккаунты без шибы уходят в asteroid_skip.txt и больше не фармятся.{c['reset']}")

    target = prompt_menu("Аккаунты:", [
        ("✅ Выбрать вручную", "select"),
        ("📂 Все из папки", "all"),
    ])
    if target is None:
        return
    sessions = (await multi_pick_sessions("Asteroid Shiba")) if target == "select" \
        else (await choose_folder_sessions("Asteroid Shiba"))
    if not sessions:
        print(f"{c['yel']}В выбранной папке нет сессий — выбери sessions/ или обе папки.{c['reset']}")
        return

    # Drop accounts already known to be unclaimable.
    skip = load_asteroid_skip()
    before = len(sessions)
    sessions = [sp for sp in sessions if sp.stem not in skip]
    skipped = before - len(sessions)
    if skipped:
        print(f"{c['dim']}Пропущено {skipped} из скип-листа (без шибы).{c['reset']}")
    if not sessions:
        print(f"{c['yel']}Все выбранные аккаунты в скип-листе.{c['reset']}")
        return

    print(f"\n{c['yel']}Аккаунтов к фарму: {len(sessions)}.{c['reset']}")
    if input("Продолжить? (yes/n): ").strip().lower() not in ("yes", "y", "да"):
        print(f"{c['dim']}Отменено.{c['reset']}")
        return

    await asteroid_cycle(config, sessions)


async def asteroid_cycle(config, sessions):
    """Non-interactive: run the Asteroid flow over a list of sessions."""
    api_id, api_hash = config["api_id"], config["api_hash"]
    delay_range = config.get("delay_between_accounts_sec", [5, 30])

    # Resume: asteroids reset daily, so the window is the UTC date.
    window = daily_window()
    done = progress_done("asteroid", window)
    sessions = [sp for sp in sessions
                if sp.stem not in done and sp.stem not in BUSY_SESSIONS]
    if done:
        logger.info(f"Asteroid: {len(done)} already done today, {len(sessions)} left")
    if not sessions:
        logger.info("Asteroid: already done for today")
        return

    total = len(sessions)
    logger.info(f"Asteroid Shiba: {total} account(s)")
    started = time.time()
    alive = dead = 0

    for i, session_path in enumerate(sessions, 1):
        if shutdown_event.is_set():
            break
        label = session_path.stem
        client = TelegramClient(str(session_path.with_suffix("")), int(api_id), str(api_hash))
        try:
            await client.connect()
            if not await client.is_user_authorized():
                logger.warning(f"[{label}] Not authorized, skipping")
                dead += 1
                progress_mark("asteroid", window, label)
                continue
            alive += 1
            await asteroid_account(client, label)
            progress_mark("asteroid", window, label)
        except Exception as e:
            logger.error(f"[{label}] error: {e}")
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

        log_eta(i, total, time.time() - started, delay_range)

        if not shutdown_event.is_set() and session_path != sessions[-1]:
            delay = random.uniform(*delay_range)
            logger.info(f"Waiting {delay:.1f}s before next account...")
            await asyncio.sleep(delay)

    logger.info(f"Asteroid Shiba cycle complete — сессий живо: {alive}/{alive + dead}")


async def run_combo(config):
    c = _C
    print(f"\n{c['cyan']}{c['bold']}🔁 Комбо: Рилс (каждые 6ч) + Астероиды/просмотры (раз в сутки){c['reset']}")
    print(f"{c['dim']}Работает бесконечно по аккаунтам из sessions/. "
          f"Снайпер (если включён) стреляет мгновенно, не мешая циклам. "
          f"Ctrl+C для остановки.{c['reset']}")

    reels_mode = prompt_menu("Рилс — режим:", [
        ("Фарм — только собрать/показать фриспины", "farm"),
        ("Спин — прокрутить все фриспины", "spin"),
    ], back=False)
    if reels_mode is None:
        return

    first_task = prompt_menu("С чего начинать каждый цикл?", [
        (f"👁 Сначала просмотры @{VIEWS_CHANNEL} (потом Рилс)", "views"),
        ("🎡 Сначала круг Рилс (потом просмотры)", "reels"),
    ], back=False)
    if first_task is None:
        return

    skip_first_reels = input(
        "Пропустить первый цикл Рилс (сразу к астероидам, для теста)? (y/n) [n]: "
    ).strip().lower() in ("y", "yes", "да")

    want_sniper = input(
        f"Держать снайпер комментариев @{SNIPER_CHANNEL} параллельно? (y/n) [y]: "
    ).strip().lower() in ("", "y", "yes", "да")

    if input("Запустить комбо? (yes/n): ").strip().lower() not in ("yes", "y", "да"):
        print(f"{c['dim']}Отменено.{c['reset']}")
        return

    # The sniper runs as a background task so it can fire mid-cycle.
    sniper = asyncio.create_task(sniper_task(config)) if want_sniper else None

    interval_hours = config.get("interval_hours", 6)
    random_delay_minutes = config.get("random_delay_minutes", 30)
    asteroid_every = 24 * 3600  # seconds
    last_asteroid = None  # None → run on the very first cycle
    first_cycle = True

    async def daily_block():
        # Views of @prosadin, then the daily Asteroid run — gated to once/24h.
        nonlocal last_asteroid
        now = asyncio.get_event_loop().time()
        if last_asteroid is None or now - last_asteroid >= asteroid_every:
            all_sessions = get_session_files()
            if all_sessions:
                logger.info(f"Combo: daily views of @{VIEWS_CHANNEL}")
                await views_cycle(config, all_sessions)
            skip = load_asteroid_skip()
            ast_sessions = [sp for sp in get_session_files() if sp.stem not in skip]
            if ast_sessions and not shutdown_event.is_set():
                logger.info("Combo: daily Asteroid run")
                await asteroid_cycle(config, ast_sessions)
            last_asteroid = now

    async def reels_block():
        nonlocal first_cycle
        if first_cycle and skip_first_reels:
            logger.info("Combo: skipping first Reels cycle (test mode)")
        else:
            await run_cycle(config, reels_mode)
        first_cycle = False

    while not shutdown_event.is_set():
        # Run the two blocks in the order the user picked at startup.
        blocks = [daily_block, reels_block] if first_task == "views" \
            else [reels_block, daily_block]
        for block in blocks:
            if shutdown_event.is_set():
                break
            await block()
        if shutdown_event.is_set():
            break

        jitter = random.uniform(0, random_delay_minutes) * 60
        total_sleep = interval_hours * 3600 + jitter
        logger.info(f"Combo: sleeping {total_sleep/3600:.2f}h until next Reels cycle")
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=total_sleep)
        except asyncio.TimeoutError:
            pass

    if sniper:
        sniper.cancel()
    logger.info("Combo stopped")


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
        if action == "add_sessions":
            await run_add_sessions(config)
            return
        if action == "check_sessions":
            await run_check_sessions(config)
            return
        if action == "secure":
            await run_secure(config)
            return
        if action == "warm":
            await run_warm(config)
            return
        if action == "listen_code":
            await listen_login_code(config)
            return
        if action == "import_tdata":
            await import_tdata_zips(config)
            return
        if action == "import_mega":
            await import_from_mega(config)
            return
        if action == "import_yandex":
            await import_from_yandex(config)
            return
        if action == "convert_mega":
            await convert_mega_tdata(config)
            return
        if action == "topup_stars":
            await run_stars(config)
            return
        if action == "asteroid":
            await run_asteroid(config)
            return
        if action == "combo":
            await run_combo(config)
            return
        if action == "views":
            await run_views(config)
            return
        if action == "sniper":
            await run_sniper(config)
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
