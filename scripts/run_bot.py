from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.bot_transports import (
    BotRateLimited,
    BotTransportError,
    create_transport,
    resolve_transport_mode,
)
from shared.kick_tokens import token_info

TOKEN_ENV = "KICK_BOT_TOKEN"
LogFn = Callable[[str], None]


def make_logger(bot_id: int | None, log_dir: str) -> LogFn:
    log_path = None
    if bot_id is not None:
        path = Path(log_dir)
        path.mkdir(parents=True, exist_ok=True)
        log_path = path / f"bot_{bot_id}.log"

    def log(message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
        print(line, flush=True)
        if log_path is not None:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    return log


def resolve_token(cli_token: str | None) -> str:
    token = (cli_token or os.getenv(TOKEN_ENV) or "").strip()
    if not token:
        raise SystemExit(f"Missing token. Pass --token or set {TOKEN_ENV}.")
    return token


def format_result(result) -> str:
    message_id = f" message_id={result.message_id}" if result.message_id else ""
    detail = f" detail={result.detail}" if result.detail else ""
    return (
        f"stage=send_result action={result.action} ok={result.ok} "
        f"transport={result.transport} simulated={result.simulated} "
        f"channel={result.channel}{message_id}{detail}"
    )


async def send_loop(
    channel: str,
    message: str,
    interval: int,
    token: str,
    requested_mode: str,
    log: LogFn,
):
    mode = resolve_transport_mode(token, requested_mode)
    info = token_info(token)
    actor = f"bot-{os.getenv('KICK_BOT_ID', 'local')}"
    transport = create_transport(token, requested_mode, actor=actor)
    delay = max(1, interval)

    log(f"stage=token_detected kind={info.kind} transport={transport.name}")
    log(f"stage=ready mode={mode} channel={channel} interval={delay}")

    while True:
        try:
            log(
                f"stage=send_attempt action=send_message "
                f"transport={transport.name} channel={channel}"
            )
            result = transport.send_message(channel, message)
            log(format_result(result))
        except BotRateLimited as exc:
            retry_after = exc.retry_after or delay
            log(
                "stage=send_error reason=rate_limited "
                f"transport={transport.name} retry_after={retry_after}"
            )
            try:
                retry_delay = int(retry_after)
            except (TypeError, ValueError):
                retry_delay = delay
            await asyncio.sleep(max(1, retry_delay))
            continue
        except BotTransportError as exc:
            log(f"stage=send_error reason={exc} transport={transport.name}")
        except Exception as exc:  # noqa: broad-except
            log(
                f"stage=send_error reason=unexpected "
                f"transport={transport.name} error={type(exc).__name__}"
            )
        await asyncio.sleep(delay)


def main():
    parser = argparse.ArgumentParser(description="BetterKickBot runner")
    parser.add_argument("--channel", required=True, help="Kick channel or channel id")
    parser.add_argument("--message", required=True, help="Message to send")
    parser.add_argument(
        "--interval", type=int, default=600, help="Send interval in seconds"
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Kick auth token. Prefer KICK_BOT_TOKEN to avoid command-line leaks.",
    )
    parser.add_argument("--bot-id", type=int, default=None, help="Dashboard bot id")
    parser.add_argument("--log-dir", default="logs", help="Directory for bot logs")
    parser.add_argument(
        "--test-mode",
        choices=("auto", "local", "real"),
        default="auto",
        help="Use local to simulate; cookie tokens always stay local.",
    )
    args = parser.parse_args()

    token = resolve_token(args.token)
    log = make_logger(args.bot_id, args.log_dir)
    mode = resolve_transport_mode(token, args.test_mode)
    log(f"stage=start mode={mode}")

    try:
        asyncio.run(
            send_loop(
                args.channel,
                args.message,
                args.interval,
                token,
                args.test_mode,
                log,
            )
        )
    except KeyboardInterrupt:
        log("stage=stopped reason=keyboard_interrupt")


if __name__ == "__main__":
    main()
