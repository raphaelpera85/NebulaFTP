from __future__ import annotations

import argparse
import asyncio
from os import environ
from os.path import exists

if exists(".env"):
    from dotenv import load_dotenv

    load_dotenv()


def _parse_args():
    parser = argparse.ArgumentParser(description="Print a Telegram channel ID using a bot command")
    parser.add_argument("--command", default="id", help="command to listen for (default: id)")
    return parser.parse_args()


async def main():
    args = _parse_args()

    api_id = environ.get("API_ID")
    api_hash = environ.get("API_HASH")
    token_str = environ.get("BOT_TOKENS") or environ.get("BOT_TOKEN")

    if not api_id or not api_hash or not token_str:
        raise SystemExit("FATAL: API_ID, API_HASH and BOT_TOKENS/BOT_TOKEN are required")

    try:
        api_id_int = int(api_id)
    except ValueError as exc:
        raise SystemExit(f"FATAL: invalid API_ID: {api_id!r}") from exc

    token = token_str.split(",")[0].strip()
    if not token:
        raise SystemExit("FATAL: empty bot token")

    try:
        from pyrogram import Client, filters
    except Exception as exc:
        raise SystemExit(f"FATAL: pyrogram unavailable: {exc}") from exc

    bot = Client("Nebula_ChannelId_Helper", api_id=api_id_int, api_hash=api_hash, bot_token=token)
    got_id = asyncio.Event()

    @bot.on_message(filters.command(args.command) & filters.channel)
    async def _show_channel_id(_, message):
        print(f"CHAT_ID={message.chat.id}")
        try:
            await message.reply_text(f"CHAT_ID={message.chat.id}")
        except Exception:
            pass
        got_id.set()

    await bot.start()
    print(f"Bot ready. Send /{args.command} in the target channel.")
    try:
        await got_id.wait()
    finally:
        try:
            await bot.stop()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
