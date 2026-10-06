"""One-time login CLI for the personal-account Telegram ingest worker.

Walks you through the Telethon phone → code → optional 2FA flow and persists
the session to ``data/_tg/telethon.session``. Subsequent ``tg_user`` runs read
this file and start unattended.

Usage:
    python -m bot_squad_worker.tg_user_cli --config /path/to/config login
    python -m bot_squad_worker.tg_user_cli --config /path/to/config status
    python -m bot_squad_worker.tg_user_cli --config /path/to/config logout
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from getpass import getpass
from pathlib import Path

from bot_squad_worker.config import Config


log = logging.getLogger("tg_user_cli")


async def _login(cfg: Config) -> int:
    if not cfg.tg_user_api_id or not cfg.tg_user_api_hash:
        print(
            "error: api_id / api_hash missing.\n"
            "Edit config/secrets.toml → [telegram_user] and set both fields,\n"
            "then re-run this command. Get the values from https://my.telegram.org\n"
            "→ 'API development tools'."
        )
        return 2

    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError

    from bot_squad_worker.tg_proxy import telethon_proxy_from_env

    cfg.tg_user_dir.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(
        str(cfg.tg_user_session_path),
        cfg.tg_user_api_id,
        cfg.tg_user_api_hash,
        proxy=telethon_proxy_from_env(),
    )
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"already logged in as {getattr(me, 'username', None) or me.first_name} (id={me.id})")
        await client.disconnect()
        return 0

    phone = input("phone number (with country code, e.g. +12025550123): ").strip()
    sent = await client.send_code_request(phone)
    code = input(f"login code (sent to {phone}): ").strip()
    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
    except SessionPasswordNeededError:
        pw = getpass("2FA password: ")
        await client.sign_in(password=pw)

    me = await client.get_me()
    print(f"logged in as {getattr(me, 'username', None) or me.first_name} (id={me.id})")
    print(f"session saved to {cfg.tg_user_session_path}.session")
    await client.disconnect()
    return 0


async def _status(cfg: Config) -> int:
    session_file = Path(str(cfg.tg_user_session_path) + ".session")
    print(f"config_dir:   {cfg.config_dir}")
    print(f"api_id:       {cfg.tg_user_api_id or '(unset)'}")
    print(f"api_hash:     {'set' if cfg.tg_user_api_hash else '(unset)'}")
    print(f"session file: {session_file} ({'present' if session_file.exists() else 'missing'})")
    if not (cfg.tg_user_api_id and cfg.tg_user_api_hash and session_file.exists()):
        return 0

    from telethon import TelegramClient

    from bot_squad_worker.tg_proxy import telethon_proxy_from_env

    client = TelegramClient(
        str(cfg.tg_user_session_path),
        cfg.tg_user_api_id,
        cfg.tg_user_api_hash,
        proxy=telethon_proxy_from_env(),
    )
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"authorized:   yes — {getattr(me, 'username', None) or me.first_name} (id={me.id})")
    else:
        print("authorized:   no (run `login`)")
    await client.disconnect()
    return 0


async def _logout(cfg: Config) -> int:
    session_file = Path(str(cfg.tg_user_session_path) + ".session")
    if not session_file.exists():
        print("no session file to remove")
        return 0

    if cfg.tg_user_api_id and cfg.tg_user_api_hash:
        from telethon import TelegramClient

        from bot_squad_worker.tg_proxy import telethon_proxy_from_env

        client = TelegramClient(
            str(cfg.tg_user_session_path),
            cfg.tg_user_api_id,
            cfg.tg_user_api_hash,
            proxy=telethon_proxy_from_env(),
        )
        await client.connect()
        try:
            if await client.is_user_authorized():
                await client.log_out()
                print("told Telegram to revoke session")
        finally:
            await client.disconnect()

    session_file.unlink(missing_ok=True)
    print(f"removed {session_file}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="bot-squad-worker.tg_user_cli")
    parser.add_argument(
        "--config",
        default=os.environ.get("BOT_SQUAD_CONFIG", "/home/www/bot-squad/config"),
        help="path to bot-swarm config dir",
    )
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "warning"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="one-time phone+code+2FA login")
    sub.add_parser("status", help="show config + session state")
    sub.add_parser("logout", help="revoke session on server and delete local file")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    cfg = Config.load(Path(args.config))

    if args.cmd == "login":
        return asyncio.run(_login(cfg))
    if args.cmd == "status":
        return asyncio.run(_status(cfg))
    if args.cmd == "logout":
        return asyncio.run(_logout(cfg))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
