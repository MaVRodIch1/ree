"""
Runs in its own subprocess. opentele's @extend_class decorator
monkeypatches telethon.TelegramClient globally and irreversibly on
import, corrupting api_id/api_hash handling for any TelegramClient
created afterward in the same process. Isolating the conversion here
keeps that patch from leaking into main.py's process.
"""
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SESSIONS_DIR = BASE_DIR / "sessions"
TDATA_DIR = BASE_DIR / "tdata_accounts"


def convert_one(tdata_dir: str, out_session_path: str):
    """Convert a single tdata folder to <out_session_path>.session."""
    try:
        from opentele.td import TDesktop
        from opentele.tl import TelegramClient as OpenteleClient
        from opentele.exception import OpenTeleException
    except ImportError:
        print("[tdata] opentele not installed, cannot convert")
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
        tdesk.ToTelethon(
            str(out_session_path),
            flag=OpenteleClient.Flag.UseCurrentSession,
        )
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

    try:
        from opentele.td import TDesktop
        from opentele.tl import TelegramClient as OpenteleClient
        from opentele.exception import OpenTeleException
    except ImportError:
        print("[tdata] opentele not installed, skipping tdata conversion")
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
            tdesk.ToTelethon(
                str(SESSIONS_DIR / session_name),
                flag=OpenteleClient.Flag.UseCurrentSession,
            )
            print(f"[tdata] Converted {entry.name} -> {session_name}.session")
        except (Exception, OpenTeleException) as e:
            print(f"[tdata] Error converting {entry.name}: {e}")


if __name__ == "__main__":
    # Single conversion: python tdata_convert.py <tdata_dir> <out_session_path>
    if len(sys.argv) >= 3:
        convert_one(sys.argv[1], sys.argv[2])
    else:
        main()
