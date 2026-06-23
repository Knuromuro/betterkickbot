from __future__ import annotations

import asyncio
import os
from pathlib import Path
from threading import Thread, Timer as _Timer  # Timer re-exported for tests
import subprocess
import sys
import time
from typing import Dict, Optional
from uuid import uuid4

import redis
from rq import Queue
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from flask import current_app, Flask
from shared.config import load_config
from shared.kick_tokens import token_info
from shared.local_kick_mock import LocalKickMockAdapter
from shared.logger import logger, notify_webhook
from bots.instance import BotInstance
from .models import db, Group, Account, Log, SyncEvent
from flask_socketio import SocketIO
from prometheus_client import Counter, Gauge, CollectorRegistry

Timer = _Timer

cfg = load_config()

WORKERS = cfg.WORKERS
MAX_INSTANCES = cfg.MAX_INSTANCES

sched = BackgroundScheduler(
    executors={"default": ThreadPoolExecutor(max_workers=WORKERS)},
    job_defaults={"max_instances": MAX_INSTANCES},
)

aio_loop = asyncio.new_event_loop()
_thread = Thread(target=lambda: aio_loop.run_forever(), daemon=True)
_thread.start()

redis_conn: Optional[redis.Redis] = None
queue = None
redis_online = True
APP: Optional[Flask] = None

registry = CollectorRegistry()
runs_counter = Counter("bot_runs", "Number of bot executions", registry=registry)
errors_counter = Counter("bot_errors", "Number of bot errors", registry=registry)
running_gauge = Gauge(
    "bots_running", "Currently running bot processes", registry=registry
)

bots: Dict[int, BotInstance] = {}
processes: Dict[int, subprocess.Popen] = {}


def _bot_log_dir(app: Flask) -> Path:
    path = Path(app.config.get("BOT_LOG_DIR", "logs"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _append_bot_log(app: Flask, bot_id: int, message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    with (_bot_log_dir(app) / f"bot_{bot_id}.log").open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _local_mock_path(app: Flask) -> Path:
    return Path(
        app.config.get(
            "LOCAL_KICK_MOCK_FILE", _bot_log_dir(app) / "local_kick_mock.json"
        )
    )


def init_redis() -> None:
    global redis_conn, queue, redis_online
    redis_conn = redis.from_url(cfg.REDIS_URL or "redis://localhost:6379/0")
    queue = Queue("bots", connection=redis_conn)
    try:
        redis_conn.ping()
        redis_online = True
        logger.info("Connected to Redis")
    except Exception:  # noqa: broad-except
        redis_online = False
        logger.warning("Redis unavailable, tasks will run inline")


def log_sync_event(entity: str, action: str, payload: dict, socketio: SocketIO) -> None:
    evt = SyncEvent(
        event_id=str(uuid4()),
        entity=entity,
        action=action,
        payload=payload,
    )
    db.session.add(evt)
    db.session.commit()
    socketio.emit(
        "sync_event",
        {
            "event_id": evt.event_id,
            "entity": evt.entity,
            "action": evt.action,
            "payload": evt.payload,
            "timestamp": evt.timestamp.isoformat(),
        },
    )


def run_bot_task(bot_id: int, socketio: SocketIO) -> None:
    logger.info("starting bot task %s", bot_id)
    app = APP or current_app
    with app.app_context():
        account = Account.query.get(bot_id)
        if not account:
            return
        group = Group.query.get(account.group_id)
        if not group:
            return
    msg = "Hello from KickBot"
    msg_path = Path(account.messages_file or "")
    if msg_path.is_file():
        lines = msg_path.read_text(errors="ignore").splitlines()
        if lines:
            msg = lines[0]
    cmd = [
        sys.executable,
        str(Path(__file__).resolve().parent.parent / "scripts" / "run_bot.py"),
        "--channel",
        group.target,
        "--message",
        msg,
        "--interval",
        str(group.interval),
        "--bot-id",
        str(account.id),
        "--log-dir",
        str(_bot_log_dir(app)),
        "--test-mode",
        (
            "local"
            if token_info(account.password).kind == "cookie"
            or app.config.get("TESTING")
            else "auto"
        ),
    ]
    env = {**os.environ, "KICK_BOT_TOKEN": account.password}
    env["KICK_BOT_ID"] = str(account.id)
    env["LOCAL_KICK_MOCK_FILE"] = str(_local_mock_path(app))
    runs_counter.inc()
    try:
        subprocess.run(cmd, check=True, env=env)
        socketio.emit("bot_stopped", {"id": bot_id})
    except Exception as exc:  # noqa: broad-except
        errors_counter.inc()
        logger.error("bot run failed: %s", exc)
        webhook = cfg.SLACK_WEBHOOK
        if webhook:
            notify_webhook(webhook, f"Bot {bot_id} failed: {exc}")
        socketio.emit("bot_error", {"id": bot_id})


def schedule_all(socketio: SocketIO) -> None:
    sched.remove_all_jobs()
    for acc in Account.query.all():
        group = Group.query.get(acc.group_id)
        if not group:
            continue
        try:
            sched.add_job(
                lambda aid=acc.id: asyncio.run_coroutine_threadsafe(
                    send_job(aid, socketio), aio_loop
                ),
                "interval",
                seconds=group.interval,
                id=str(acc.id),
                replace_existing=True,
            )
        except Exception as exc:  # noqa: broad-except
            logger.error("could not schedule job %s: %s", acc.id, exc)


async def send_job(account_id: int, socketio: SocketIO) -> None:
    app = APP or current_app
    with app.app_context():
        account = Account.query.get(account_id)
        if not account:
            return
        group = Group.query.get(account.group_id)
        if not group:
            return
        message = "Hello from KickBot"
        msg_path = Path(account.messages_file or "").expanduser()
        if msg_path.is_file():
            with open(msg_path) as fh:
                line = fh.readline().strip()
                if line:
                    message = line
        info = token_info(account.password)
        if info.kind == "cookie" or app.config.get("TESTING"):
            mode = "local_cookie_test" if info.kind == "cookie" else "local_test"
            adapter = LocalKickMockAdapter(path=_local_mock_path(app))
            adapter.ensure_account(str(account_id), account.username)
            result = adapter.send_message(str(account_id), group.target, message)
            socketio.emit("bot_started", {"id": account_id, "mode": mode})
            _append_bot_log(
                app,
                account_id,
                "stage=scheduler_simulated "
                f"mode={mode} token_kind={info.kind} "
                f"status={result.status} code={result.code} "
                f"event_id={result.event_id}",
            )
            log = Log(account_id=account_id, message="scheduler local test")
            db.session.add(log)
            db.session.commit()
            socketio.emit("bot_stopped", {"id": account_id, "mode": mode})
            socketio.emit(
                "status",
                {"message": f"local test scheduler tick for {account_id}"},
            )
            return
        if account_id not in bots:
            bots[account_id] = BotInstance(account, group)
            bots[account_id].login()
        bot = bots[account_id]
    socketio.emit("bot_started", {"id": account_id})
    try:
        await bot.send_message(message)
        socketio.emit("bot_stopped", {"id": account_id})
    except Exception as exc:  # noqa: broad-except
        errors_counter.inc()
        logger.error("send job failed: %s", exc)
        socketio.emit("bot_error", {"id": account_id})
    with app.app_context():
        log = Log(account_id=account_id, message=message)
        db.session.add(log)
        db.session.commit()
    socketio.emit("status", {"message": f"sent message for {account_id}"})


def process_unsent_events(socketio: SocketIO) -> None:
    app = APP or current_app
    with app.app_context():
        events = SyncEvent.query.filter_by(synced=False).all()
        for evt in events:
            socketio.emit(
                "sync_event",
                {
                    "event_id": evt.event_id,
                    "entity": evt.entity,
                    "action": evt.action,
                    "payload": evt.payload,
                    "timestamp": evt.timestamp.isoformat(),
                },
            )
            evt.synced = True
        db.session.commit()
