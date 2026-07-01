"""
One-off research tool: opens the @reelsio_ru Mini App (WebView) using one
of the existing sessions and dumps its rendered HTML + visible text to
inspect_output/ so we can find where the spin count lives in the DOM.

Requires: pip install playwright && playwright install chromium
"""
import asyncio
import json
import sys
from pathlib import Path

from telethon import TelegramClient, types, functions

BASE_DIR = Path(__file__).resolve().parent
SESSIONS_DIR = BASE_DIR / "sessions"
CONFIG_PATH = BASE_DIR / "config.json"
OUTPUT_DIR = BASE_DIR / "inspect_output"
OUTPUT_DIR.mkdir(exist_ok=True)


def load_config() -> dict:
    with open(CONFIG_PATH, "r") as f:
        cfg = json.load(f)
    cfg["api_id"] = int(cfg["api_id"])
    cfg["api_hash"] = str(cfg["api_hash"])
    return cfg


async def get_webview_url(client: TelegramClient, bot_username: str) -> str:
    bot = await client.get_entity(bot_username)

    full = await client(functions.users.GetFullUserRequest(bot))
    menu_button = full.full_user.bot_info.menu_button if full.full_user.bot_info else None

    if isinstance(menu_button, types.BotMenuButton):
        url = menu_button.url
    else:
        # Fall back: look for a web_app button on the /start reply
        await client.send_message(bot, "/start")
        await asyncio.sleep(2)
        messages = await client.get_messages(bot, limit=3)
        url = None
        for msg in messages:
            if msg.buttons:
                for row in msg.buttons:
                    for btn in row:
                        if isinstance(btn.button, types.KeyboardButtonSimpleWebView):
                            url = btn.button.url
                            break
        if not url:
            raise RuntimeError("No menu button web app and no web_app inline button found")

    result = await client(functions.messages.RequestSimpleWebViewRequest(
        bot=bot,
        platform="android",
        url=url,
        from_side_menu=True,
    ))
    return result.url


async def main():
    config = load_config()
    sessions = sorted(SESSIONS_DIR.glob("*.session"))
    if not sessions:
        print("No sessions found in sessions/")
        return

    session_path = sessions[0]
    print(f"Using session: {session_path.stem}")

    client = TelegramClient(str(session_path.with_suffix("")), config["api_id"], config["api_hash"])
    await client.connect()
    if not await client.is_user_authorized():
        print(f"Session {session_path.stem} is not authorized")
        await client.disconnect()
        return

    try:
        webview_url = await get_webview_url(client, config["bot_username"])
    finally:
        await client.disconnect()

    print(f"WebView URL obtained (truncated): {webview_url[:80]}...")
    (OUTPUT_DIR / "webview_url.txt").write_text(webview_url, encoding="utf-8")

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Playwright not installed. Run: pip install playwright && playwright install chromium")
        print(f"Full URL saved to {OUTPUT_DIR / 'webview_url.txt'} — you can open it manually in a browser.")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(webview_url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(3000)

        html = await page.content()
        (OUTPUT_DIR / "page.html").write_text(html, encoding="utf-8")

        body_text = await page.inner_text("body")
        (OUTPUT_DIR / "page_text.txt").write_text(body_text, encoding="utf-8")

        await page.screenshot(path=str(OUTPUT_DIR / "page.png"), full_page=True)

        await browser.close()

    print(f"Saved: {OUTPUT_DIR / 'page.html'}, page_text.txt, page.png")


if __name__ == "__main__":
    asyncio.run(main())
