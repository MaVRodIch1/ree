"""
Runs in its own subprocess. opentele's @extend_class decorator
monkeypatches telethon.TelegramClient globally and irreversibly on
import, corrupting api_id/api_hash handling for any TelegramClient
created afterward in the same process. Isolating the conversion here
keeps that patch from leaking into main.py's process.
"""
import importlib.util
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SESSIONS_DIR = BASE_DIR / "sessions"
TDATA_DIR = BASE_DIR / "tdata_accounts"


def _patch_opentele_py313():
    """opentele's @extend_class chokes on the __firstlineno__ /
    __static_attributes__ dunders that Python 3.13 adds to every class,
    raising BaseException('err') at import. Add them to its skip-list in
    the installed utils.py (idempotent, done before importing opentele)."""
    try:
        spec = importlib.util.find_spec("opentele")
        if not spec or not spec.origin:
            return
        utils = Path(spec.origin).parent / "utils.py"
        src = utils.read_text(encoding="utf-8")
        if "__firstlineno__" in src:
            return
        patched = src.replace(
            '["__abstractmethods__", "__module__", "_abc_impl", "__doc__"]',
            '["__abstractmethods__", "__module__", "_abc_impl", "__doc__", '
            '"__firstlineno__", "__static_attributes__"]',
        )
        if patched != src:
            utils.write_text(patched, encoding="utf-8")
            print("[tdata] patched opentele for Python 3.13")
    except Exception as e:
        print(f"[tdata] could not patch opentele: {e}")


def _to_telethon(tdesk, out_session_path):
    """Run opentele's async ToTelethon and make sure the session is saved."""
    import asyncio
    from opentele.api import UseCurrentSession

    async def _run():
        client = await tdesk.ToTelethon(str(out_session_path), UseCurrentSession)
        try:
            await client.disconnect()
        except Exception:
            pass

    asyncio.run(_run())


def convert_one(tdata_dir: str, out_session_path: str):
    """Convert a single tdata folder to <out_session_path>.session."""
    _patch_opentele_py313()
    try:
        from opentele.td import TDesktop
        from opentele.exception import OpenTeleException
    except Exception as e:
        print(f"[tdata] opentele import failed: {e}")
        return

    name = Path(out_session_path).name
    try:
        tdesk = TDesktop(str(tdata_dir))
    except (Exception, OpenTeleException) as e:
        print(f"[tdata] Skipping {name}: {e}")
        return
    if not tdesk.isLoaded():
        print(f"[tdata] Failed to load tdata for {name}")
        return
    try:
        _to_telethon(tdesk, out_session_path)
        print(f"[tdata] Converted -> {name}.session")
    except (Exception, OpenTeleException) as e:
        print(f"[tdata] Error converting {name}: {e}")


def main():
    dirs_to_convert = [
        entry for entry in sorted(TDATA_DIR.iterdir())
        if entry.is_dir() and not (SESSIONS_DIR / f"{entry.name}.session").exists()
    ]
    if not dirs_to_convert:
        return

    _patch_opentele_py313()
    try:
        from opentele.td import TDesktop
        from opentele.exception import OpenTeleException
    except Exception as e:
        print(f"[tdata] opentele import failed: {e}")
        return

    for entry in dirs_to_convert:
        session_name = entry.name
        try:
            tdesk = TDesktop(str(entry))
        except (Exception, OpenTeleException) as e:
            print(f"[tdata] Skipping {entry.name}: {e}")
            continue
        if not tdesk.isLoaded():
            print(f"[tdata] Failed to load tdata from {entry.name}")
            continue
        try:
            _to_telethon(tdesk, SESSIONS_DIR / session_name)
            print(f"[tdata] Converted {entry.name} -> {session_name}.session")
        except (Exception, OpenTeleException) as e:
            print(f"[tdata] Error converting {entry.name}: {e}")


if __name__ == "__main__":
    # Single conversion: python tdata_convert.py <tdata_dir> <out_session_path>
    if len(sys.argv) >= 3:
        convert_one(sys.argv[1], sys.argv[2])
    else:
        main()
