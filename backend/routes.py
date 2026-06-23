from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time

from flask import Blueprint, request, current_app, Response
from flask_restx import Api, Resource
from flask_jwt_extended import (
    create_access_token,
    create_refresh_token,
    jwt_required,
    get_jwt,
)
from marshmallow import ValidationError
from prometheus_client import generate_latest

from shared.cache import cache
from shared.kick_tokens import token_info
from shared.local_kick_mock import (
    ACCOUNT_STATUSES,
    LocalKickMockAdapter,
    clear_events,
    read_events,
)
from shared.logger import logger
from .models import db, Group, Account, GroupSchema, AccountSchema, SyncEvent
from .utils import role_required
from . import scheduler
from .scheduler import sched, schedule_all, log_sync_event
import bcrypt

api_bp = Blueprint("api", __name__)
api = Api(api_bp, doc="/docs")
ns = api.namespace("api", path="/dashboard/api")

auth_bp = Blueprint("auth", __name__)


def _bot_log_dir() -> Path:
    path = Path(current_app.config.get("BOT_LOG_DIR", "logs"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _bot_log_path(bot_id: int) -> Path:
    return _bot_log_dir() / f"bot_{bot_id}.log"


def _local_mock_path() -> Path:
    path = Path(
        current_app.config.get("LOCAL_KICK_MOCK_FILE", "logs/local_kick_mock.json")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _local_adapter() -> LocalKickMockAdapter:
    return LocalKickMockAdapter(path=_local_mock_path())


def _append_bot_log(bot_id: int, message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    with _bot_log_path(bot_id).open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _bot_is_running(bot_id: int) -> bool:
    proc = scheduler.processes.get(bot_id)
    return proc is not None and proc.poll() is None


def _account_payload(account: Account) -> dict:
    info = token_info(account.password)
    return {
        "id": account.id,
        "username": account.username,
        "group_id": account.group_id,
        "token_kind": info.kind,
        "token_mode": info.mode,
        "token_mask": info.mask,
    }


def _start_mode(account: Account) -> tuple[str, str]:
    info = token_info(account.password)
    if info.kind == "cookie":
        return "local_cookie_test", "local"
    if current_app.config.get("BOT_FORCE_LOCAL_TEST") or current_app.config.get(
        "TESTING"
    ):
        return "local_test", "local"
    return "live_kick", "auto"


def _first_message(account: Account) -> str:
    msg = "Hello from KickBot"
    msg_path = Path(account.messages_file or "")
    if msg_path.is_file():
        lines = msg_path.read_text(errors="ignore").splitlines()
        if lines:
            msg = lines[0]
    return msg


def _launch_bot(account: Account, group: Group) -> dict:
    existing = scheduler.processes.get(account.id)
    mode, test_mode = _start_mode(account)
    info = token_info(account.password)
    if existing and existing.poll() is None:
        return {
            "pid": existing.pid,
            "mode": mode,
            "token_kind": info.kind,
            "already_running": True,
        }

    cmd = [
        sys.executable,
        str(Path(__file__).resolve().parent.parent / "scripts" / "run_bot.py"),
        "--channel",
        group.target,
        "--message",
        _first_message(account),
        "--interval",
        str(group.interval),
        "--bot-id",
        str(account.id),
        "--log-dir",
        str(_bot_log_dir()),
        "--test-mode",
        test_mode,
    ]
    env = {**os.environ, "KICK_BOT_TOKEN": account.password}
    env["KICK_BOT_ID"] = str(account.id)
    env["LOCAL_KICK_MOCK_FILE"] = str(_local_mock_path())
    _append_bot_log(
        account.id,
        f"stage=launch mode={mode} token_kind={info.kind}",
    )
    proc = subprocess.Popen(cmd, env=env)
    scheduler.processes[account.id] = proc
    scheduler.running_gauge.inc()
    current_app.extensions["socketio"].emit(
        "bot_started", {"id": account.id, "mode": mode}
    )
    return {"pid": proc.pid, "mode": mode, "token_kind": info.kind}


@auth_bp.route("/auth/token", methods=["POST"])
def get_token():
    data = request.get_json() or {}
    user = data.get("username")
    password = data.get("password")
    totp = data.get("totp")
    role = None
    if user in {"admin", "operator"}:
        hashed = current_app.config.get(f"{user.upper()}_PASSWORD_HASH")
        if hashed:
            try:
                valid = bcrypt.checkpw(password.encode(), hashed.encode())
            except Exception:
                valid = False
        else:
            env_plain = current_app.config.get(f"{user.upper()}_PASSWORD", user)
            valid = password == env_plain
        if valid:
            role = user
    if role is None:
        return {"msg": "bad credentials"}, 401
    secret = current_app.config.get("TOTP_SECRET")
    if secret:
        import pyotp

        if not pyotp.TOTP(secret).verify(str(totp)):
            return {"msg": "invalid token"}, 401
    claims = {"role": role}
    access = create_access_token(identity=user, additional_claims=claims)
    refresh = create_refresh_token(identity=user, additional_claims=claims)
    return {"access_token": access, "refresh_token": refresh}


@auth_bp.route("/auth/refresh", methods=["POST"])
@jwt_required(refresh=True)
def refresh_token():
    claims = get_jwt()
    identity = claims["sub"]
    access = create_access_token(
        identity=identity, additional_claims={"role": claims.get("role")}
    )
    return {"access_token": access}


@ns.route("/groups", methods=["GET", "POST"], endpoint="groups")
class GroupResource(Resource):
    @jwt_required(optional=True)
    def get(self):
        search = (request.args.get("search") or "").strip()
        page = int(request.args.get("page") or 1)
        per_page = int(request.args.get("per_page") or 50)
        if not search and page == 1 and per_page == 50:
            cached = cache.get("groups")
            if cached is not None:
                return cached
        query = Group.query
        if search:
            query = query.filter(Group.name.ilike(f"%{search}%"))
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)
        groups = [
            {
                "id": g.id,
                "name": g.name,
                "target": g.target,
                "interval": g.interval,
                "bots": [{"id": a.id, "username": a.username} for a in g.accounts],
            }
            for g in pagination.items
        ]
        result = {"items": groups, "total": pagination.total}
        if not search and page == 1 and per_page == 50:
            cache.set("groups", result, timeout=60)
        return result

    @role_required("operator", "admin")
    def post(self):
        try:
            data = GroupSchema().load(request.get_json(silent=True) or {})
        except ValidationError as err:
            logger.warning("invalid group payload: %s", err.messages)
            return {"errors": err.messages}, 400
        if Group.query.filter_by(name=data["name"]).first():
            logger.warning("duplicate group name %s", data["name"])
            return {"error": "Group name already exists."}, 400
        group = Group(**data)
        db.session.add(group)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.error("database error creating group", exc_info=True)
            return {"error": "database error"}, 400
        logger.info("created group %s", group.name)
        log_sync_event(
            "group",
            "create",
            {
                "id": group.id,
                "name": group.name,
                "target": group.target,
                "interval": group.interval,
            },
            current_app.extensions["socketio"],
        )
        return {"id": group.id}, 201


@ns.route("/accounts", methods=["GET", "POST"], endpoint="accounts")
class AccountResource(Resource):
    @jwt_required(optional=True)
    def get(self):
        search = (request.args.get("search") or "").strip()
        page = int(request.args.get("page") or 1)
        per_page = int(request.args.get("per_page") or 50)
        query = Account.query
        if search:
            query = query.filter(Account.username.ilike(f"%{search}%"))
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)
        accounts = [_account_payload(a) for a in pagination.items]
        return {"items": accounts, "total": pagination.total}

    @role_required("operator", "admin")
    def post(self):
        try:
            data = AccountSchema().load(request.get_json(silent=True) or {})
        except ValidationError as err:
            logger.warning("invalid account payload: %s", err.messages)
            return {"errors": err.messages}, 400
        group_id = data.get("group_id")
        group = Group.query.get(group_id)
        if not group:
            logger.warning(
                "invalid group_id %s for account %s", group_id, data.get("username")
            )
            return {"error": "Invalid group_id"}, 400
        if Account.query.filter_by(username=data["username"]).first():
            logger.warning("duplicate account username %s", data["username"])
            return {"error": "account already exists"}, 400
        account = Account(**data)
        db.session.add(account)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.error("database error creating account", exc_info=True)
            return {"error": "database error"}, 400
        logger.info("created account %s in group %s", account.username, group_id)
        log_sync_event(
            "account",
            "create",
            {
                "id": account.id,
                "username": account.username,
                "group_id": account.group_id,
            },
            current_app.extensions["socketio"],
        )
        return {"id": account.id, "group_id": account.group_id}, 201


@ns.route("/bots", methods=["GET", "POST"], endpoint="bots")
class BotListResource(Resource):
    @jwt_required(optional=True)
    def get(self):
        search = (request.args.get("search") or "").strip()
        page = int(request.args.get("page") or 1)
        per_page = int(request.args.get("per_page") or 50)
        query = Account.query
        if search:
            query = query.filter(Account.username.ilike(f"%{search}%"))
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)
        bots = []
        for acc in pagination.items:
            info = token_info(acc.password)
            bots.append(
                {
                    "id": acc.id,
                    "username": acc.username,
                    "group_id": acc.group_id,
                    "status": "online" if _bot_is_running(acc.id) else "offline",
                    "token_kind": info.kind,
                    "mode": info.mode,
                }
            )
        return {"items": bots, "total": pagination.total}

    @role_required("operator", "admin")
    def post(self):
        # same as account creation for convenience
        try:
            data = AccountSchema().load(request.get_json(silent=True) or {})
        except ValidationError as err:
            return {"errors": err.messages}, 400
        if not Group.query.get(data["group_id"]):
            return {"error": "Invalid group_id"}, 400
        if Account.query.filter_by(username=data["username"]).first():
            return {"error": "account already exists"}, 400
        account = Account(**data)
        db.session.add(account)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            return {"error": "database error"}, 400
        log_sync_event(
            "account",
            "create",
            {
                "id": account.id,
                "username": account.username,
                "group_id": account.group_id,
            },
            current_app.extensions["socketio"],
        )
        return {"id": account.id, "group_id": account.group_id}, 201


@ns.route("/scheduler/start", methods=["POST"], endpoint="scheduler_start")
class SchedulerStart(Resource):
    @role_required("operator", "admin")
    def post(self):
        if not sched.running:
            sched.start()
        schedule_all(current_app.extensions["socketio"])
        return {"status": "started"}


@ns.route("/status", methods=["GET"], endpoint="status")
class AppStatus(Resource):
    @jwt_required(optional=True)
    def get(self):
        return {
            "redis_online": bool(
                getattr(current_app, "redis_online", scheduler.redis_online)
            )
        }


@ns.route("/bots/<int:bot_id>/start", methods=["POST"], endpoint="bot_start")
class BotStart(Resource):
    @role_required("operator", "admin")
    def post(self, bot_id: int):
        account = Account.query.get(bot_id)
        if not account:
            return {"error": "bot not found"}, 404
        group = Group.query.get(account.group_id)
        if not group:
            return {"error": "bot not found"}, 404
        result = _launch_bot(account, group)
        return result


@ns.route("/bots/<int:bot_id>/stop", methods=["POST"], endpoint="bot_stop")
class BotStop(Resource):
    @role_required("operator", "admin")
    def post(self, bot_id: int):
        proc = scheduler.processes.get(bot_id)
        if proc and proc.poll() is None:
            proc.terminate()
            scheduler.running_gauge.dec()
            _append_bot_log(bot_id, "stage=stop_requested source=dashboard")
            current_app.extensions["socketio"].emit("bot_stopped", {"id": bot_id})
            return {"stopped": True, "running": False}
        return {"stopped": False}


@ns.route("/bots/<int:bot_id>/status", methods=["GET"], endpoint="bot_status")
class BotStatus(Resource):
    @jwt_required(optional=True)
    def get(self, bot_id: int):
        proc = scheduler.processes.get(bot_id)
        running = proc is not None and proc.poll() is None
        account = Account.query.get(bot_id)
        info = token_info(account.password) if account else None
        return {
            "running": running,
            "pid": proc.pid if proc else None,
            "returncode": proc.poll() if proc else None,
            "mode": info.mode if info else "missing",
            "token_kind": info.kind if info else "missing",
        }


@ns.route("/bots/<int:bot_id>/logs", methods=["GET"], endpoint="bot_logs")
class BotLogs(Resource):
    """Return last 50 log lines for a bot."""

    @jwt_required(optional=True)
    def get(self, bot_id: int):
        log_path = _bot_log_path(bot_id)
        if not log_path.exists():
            return []
        lines = log_path.read_text(errors="ignore").splitlines()[-50:]
        return lines


@ns.route("/bots/<int:bot_id>/command", methods=["POST"], endpoint="bot_command")
class BotCommand(Resource):
    @role_required("operator", "admin")
    def post(self, bot_id: int):
        account = Account.query.get(bot_id)
        if not account:
            return {"error": "bot not found"}, 404
        group = Group.query.get(account.group_id)
        if not group:
            return {"error": "bot not found"}, 404

        payload = request.get_json(silent=True) or {}
        command = (payload.get("cmd") or "").strip()
        args = payload.get("args") or {}
        info = token_info(account.password)

        if command == "status_check":
            _append_bot_log(
                bot_id,
                "stage=command command=status_check "
                f"running={_bot_is_running(bot_id)} mode={info.mode}",
            )
            return {
                "status": "ok",
                "running": _bot_is_running(bot_id),
                "mode": info.mode,
                "token_kind": info.kind,
            }
        if command == "send_message":
            message = str(args.get("message") or "Hello from KickBot").strip()
            result = _run_local_action(account, group, "send_message", message)
            _append_bot_log(bot_id, _format_local_result(result))
            if not result.ok:
                return {
                    "error": result.code,
                    "retry_after": result.retry_after,
                    "event_id": result.event_id,
                }, _status_code_for_local_result(result)
            current_app.extensions["socketio"].emit(
                "status", {"message": f"manual message logged for {bot_id}"}
            )
            return {
                "status": "ok",
                "mode": "local_kick_mock",
                "event_id": result.event_id,
                "logged": True,
            }
        if command == "follow_channel":
            result = _run_local_action(account, group, "follow_channel")
            _append_bot_log(bot_id, _format_local_result(result))
            if not result.ok:
                return {
                    "error": result.code,
                    "retry_after": result.retry_after,
                    "event_id": result.event_id,
                }, _status_code_for_local_result(result)
            current_app.extensions["socketio"].emit(
                "status", {"message": f"follow test logged for {bot_id}"}
            )
            return {
                "status": "ok",
                "mode": "local_kick_mock",
                "channel": group.target,
                "event_id": result.event_id,
                "logged": True,
            }
        if command == "restart":
            proc = scheduler.processes.get(bot_id)
            if proc and proc.poll() is None:
                proc.terminate()
                scheduler.running_gauge.dec()
                _append_bot_log(bot_id, "stage=restart stop_previous=true")
            result = _launch_bot(account, group)
            return {"status": "ok", **result}
        if command == "screenshot":
            _append_bot_log(
                bot_id,
                "stage=command command=screenshot status=not_available",
            )
            return {"status": "ok", "available": False}

        return {"error": "unknown command"}, 400


def _run_local_action(
    account: Account,
    group: Group,
    action: str,
    message: str = "Hello from KickBot",
):
    adapter = _local_adapter()
    adapter.ensure_account(str(account.id), account.username)
    if action == "send_message":
        return adapter.send_message(str(account.id), group.target, message)
    if action == "follow_channel":
        return adapter.follow_channel(str(account.id), group.target)
    raise ValueError(f"unsupported local action: {action}")


def _format_local_result(result) -> str:
    retry = f" retry_after={result.retry_after}" if result.retry_after else ""
    event_id = f" event_id={result.event_id}" if result.event_id else ""
    error = f" error={result.error}" if result.error else ""
    return (
        f"stage=local_action action={result.action} ok={result.ok} "
        f"status={result.status} code={result.code} "
        f"transport=local_kick_mock simulated=True "
        f"channel={result.channel}{event_id}{retry}{error}"
    )


def _status_code_for_local_result(result) -> int:
    if result.code == "rate_limited":
        return 429
    if result.code in {"no_session", "session_expired"}:
        return 401
    if result.code in {"blocked", "forbidden"}:
        return 403
    if result.code == "not_found":
        return 404
    return 400


def _platform_result_payload(result) -> dict:
    return {
        "ok": result.ok,
        "status": result.status,
        "code": result.code,
        "action": result.action,
        "account_id": result.account_id,
        "channel": result.channel,
        "event_id": result.event_id,
        "retry_after": result.retry_after,
        "error": result.error,
        "event": result.data.get("event") if result.data else None,
    }


@ns.route("/local/events", methods=["GET", "DELETE"], endpoint="local_events")
class LocalEvents(Resource):
    @jwt_required(optional=True)
    def get(self):
        limit = int(request.args.get("limit") or 100)
        return {"items": read_events(limit=limit, path=_local_mock_path())}

    @role_required("operator", "admin")
    def delete(self):
        clear_events(path=_local_mock_path())
        return {"status": "ok"}


@ns.route("/local/settings", methods=["GET", "PATCH"], endpoint="local_settings")
class LocalSettings(Resource):
    @jwt_required(optional=True)
    def get(self):
        return _local_adapter().store.load()["settings"]

    @role_required("operator", "admin")
    def patch(self):
        payload = request.get_json(silent=True) or {}
        allowed = {
            "session_ttl_seconds",
            "rate_window_seconds",
            "per_account_limit",
            "global_limit",
            "max_attempts",
            "backoff_seconds",
        }
        state = _local_adapter().store.load()
        for key in allowed:
            if key not in payload:
                continue
            try:
                value = int(payload[key])
            except (TypeError, ValueError):
                return {"error": f"{key} must be an integer"}, 400
            if value < 1:
                return {"error": f"{key} must be positive"}, 400
            state["settings"][key] = value
        _local_adapter().store.save(state)
        return state["settings"]


@ns.route("/local/accounts", methods=["GET", "POST"], endpoint="local_accounts")
class LocalAccounts(Resource):
    @jwt_required(optional=True)
    def get(self):
        items = _local_adapter().list_accounts()
        return {"items": items, "total": len(items)}

    @role_required("operator", "admin")
    def post(self):
        payload = request.get_json(silent=True) or {}
        username = str(payload.get("username") or "").strip()
        if not username:
            return {"error": "username is required"}, 400
        status = str(payload.get("status") or "active")
        if status not in ACCOUNT_STATUSES:
            return {
                "error": "unsupported status",
                "allowed": sorted(ACCOUNT_STATUSES),
            }, 400
        account_id = payload.get("account_id") or payload.get("id")
        adapter = _local_adapter()
        if account_id and adapter.get_user(str(account_id)):
            return {"error": "account already exists"}, 409
        account = adapter.create_account(
            username,
            account_id=str(account_id) if account_id else None,
            status=status,
            session_ttl_seconds=payload.get("session_ttl_seconds"),
        )
        return account, 201


@ns.route(
    "/local/accounts/<string:account_id>",
    methods=["GET", "PATCH"],
    endpoint="local_account",
)
class LocalAccount(Resource):
    @jwt_required(optional=True)
    def get(self, account_id: str):
        account = _local_adapter().get_user(account_id)
        if not account:
            return {"error": "account not found"}, 404
        return account

    @role_required("operator", "admin")
    def patch(self, account_id: str):
        payload = request.get_json(silent=True) or {}
        adapter = _local_adapter()
        if payload.get("refresh_session"):
            account = adapter.refresh_session(
                account_id, ttl_seconds=payload.get("session_ttl_seconds")
            )
        else:
            status = payload.get("status")
            if status and status not in ACCOUNT_STATUSES:
                return {
                    "error": "unsupported status",
                    "allowed": sorted(ACCOUNT_STATUSES),
                }, 400
            account = adapter.update_account(
                account_id,
                status=status,
                session_ttl_seconds=payload.get("session_ttl_seconds"),
            )
        if not account:
            return {"error": "account not found"}, 404
        return account


@ns.route(
    "/local/channels/<path:channel>/followers",
    methods=["GET"],
    endpoint="local_followers",
)
class LocalFollowers(Resource):
    @jwt_required(optional=True)
    def get(self, channel: str):
        followers = _local_adapter().get_followers(channel)
        return {"items": followers, "total": len(followers)}


@ns.route("/local/channels/<path:channel>", methods=["GET"], endpoint="local_channel")
class LocalChannel(Resource):
    @jwt_required(optional=True)
    def get(self, channel: str):
        return _local_adapter().get_channel(channel)


@ns.route(
    "/local/actions/sendMessage",
    methods=["POST"],
    endpoint="local_action_send_message",
)
class LocalActionSendMessage(Resource):
    @role_required("operator", "admin")
    def post(self):
        payload = request.get_json(silent=True) or {}
        account_id = str(payload.get("account_id") or "").strip()
        channel = str(payload.get("channel") or "").strip()
        message = str(payload.get("message") or payload.get("content") or "")
        if not account_id or not channel:
            return {"error": "account_id and channel are required"}, 400
        result = _local_adapter().send_message(account_id, channel, message)
        return _platform_result_payload(result), (
            _status_code_for_local_result(result) if not result.ok else 200
        )


@ns.route("/local/actions/follow", methods=["POST"], endpoint="local_action_follow")
class LocalActionFollow(Resource):
    @role_required("operator", "admin")
    def post(self):
        payload = request.get_json(silent=True) or {}
        account_id = str(payload.get("account_id") or "").strip()
        channel = str(payload.get("channel") or "").strip()
        if not account_id or not channel:
            return {"error": "account_id and channel are required"}, 400
        result = _local_adapter().follow_channel(account_id, channel)
        return _platform_result_payload(result), (
            _status_code_for_local_result(result) if not result.ok else 200
        )


@ns.route("/local/actions/unfollow", methods=["POST"], endpoint="local_action_unfollow")
class LocalActionUnfollow(Resource):
    @role_required("operator", "admin")
    def post(self):
        payload = request.get_json(silent=True) or {}
        account_id = str(payload.get("account_id") or "").strip()
        channel = str(payload.get("channel") or "").strip()
        if not account_id or not channel:
            return {"error": "account_id and channel are required"}, 400
        result = _local_adapter().unfollow_channel(account_id, channel)
        return _platform_result_payload(result), (
            _status_code_for_local_result(result) if not result.ok else 200
        )


@ns.route("/local/queue", methods=["GET", "POST"], endpoint="local_queue")
class LocalQueue(Resource):
    @jwt_required(optional=True)
    def get(self):
        items = _local_adapter().list_queue()
        return {"items": items, "total": len(items)}

    @role_required("operator", "admin")
    def post(self):
        payload = request.get_json(silent=True) or {}
        adapter = _local_adapter()
        if isinstance(payload.get("actions"), list):
            try:
                jobs = adapter.enqueue_many(payload["actions"])
            except KeyError as exc:
                return {"error": f"missing field: {exc.args[0]}"}, 400
            return {"items": jobs, "total": len(jobs)}, 201

        account_id = str(payload.get("account_id") or "").strip()
        action = str(payload.get("action") or "").strip()
        channel = str(payload.get("channel") or "").strip()
        if not account_id or not action or not channel:
            return {"error": "account_id, action and channel are required"}, 400
        job = adapter.enqueue_action(
            account_id=account_id,
            action=action,
            channel=channel,
            content=payload.get("content") or payload.get("message"),
            max_attempts=payload.get("max_attempts"),
        )
        return job, 201


@ns.route(
    "/local/queue/process",
    methods=["POST"],
    endpoint="local_queue_process",
)
class LocalQueueProcess(Resource):
    @role_required("operator", "admin")
    def post(self):
        payload = request.get_json(silent=True) or {}
        try:
            limit = int(payload.get("limit") or 100)
        except (TypeError, ValueError):
            return {"error": "limit must be an integer"}, 400
        return _local_adapter().process_queue(limit=limit)


@ns.route("/local/report", methods=["GET"], endpoint="local_report")
class LocalReport(Resource):
    @jwt_required(optional=True)
    def get(self):
        return _local_adapter().report()


@ns.route("/local/mass-test", methods=["POST"], endpoint="local_mass_test")
class LocalMassTest(Resource):
    @role_required("operator", "admin")
    def post(self):
        payload = request.get_json(silent=True) or {}
        try:
            action_count = int(
                payload.get("action_count") or payload.get("actions") or 1000
            )
            account_count = int(
                payload.get("account_count") or payload.get("accounts") or 20
            )
        except (TypeError, ValueError):
            return {"error": "action_count and account_count must be integers"}, 400
        if action_count < 1 or account_count < 1:
            return {"error": "action_count and account_count must be positive"}, 400
        action_count = min(action_count, 10000)
        account_count = min(account_count, 1000)
        result = _local_adapter().mass_test(
            action_count=action_count,
            account_count=account_count,
            channel=str(payload.get("channel") or "local-channel"),
            process=bool(payload.get("process", True)),
        )
        return result


@ns.route("/stats", methods=["GET"], endpoint="stats")
class Stats(Resource):
    @jwt_required(optional=True)
    def get(self):
        return {
            "runs": int(scheduler.runs_counter._value.get()),
            "errors": int(scheduler.errors_counter._value.get()),
        }


@api_bp.route("/sync/pull")
@jwt_required(optional=True)
def sync_pull():
    events = SyncEvent.query.filter_by(synced=False).all()
    data = []
    for e in events:
        data.append(
            {
                "event_id": e.event_id,
                "entity": e.entity,
                "action": e.action,
                "payload": e.payload,
                "timestamp": e.timestamp.isoformat(),
            }
        )
        e.synced = True
    db.session.commit()
    return {"events": data}


@api_bp.route("/sync/push", methods=["POST"])
@jwt_required(optional=True)
def sync_push():
    payload = request.get_json(silent=True) or []
    if not isinstance(payload, list):
        return {"error": "invalid payload"}, 400
    for item in payload:
        if (
            not item.get("event_id")
            or SyncEvent.query.filter_by(event_id=item["event_id"]).first()
        ):
            continue
        se = SyncEvent(
            event_id=item["event_id"],
            entity=item.get("entity", ""),
            action=item.get("action", ""),
            payload=item.get("payload"),
            synced=True,
        )
        db.session.add(se)
        if se.entity == "group" and se.action == "create":
            if not Group.query.filter_by(name=se.payload.get("name")).first():
                db.session.add(
                    Group(
                        name=se.payload.get("name"),
                        target=se.payload.get("target"),
                        interval=se.payload.get("interval", 600),
                    )
                )
        elif se.entity == "account" and se.action == "create":
            if not Account.query.filter_by(
                username=se.payload.get("username")
            ).first() and Group.query.get(se.payload.get("group_id")):
                db.session.add(
                    Account(
                        username=se.payload.get("username"),
                        password=se.payload.get("password", ""),
                        proxy=se.payload.get("proxy"),
                        messages_file=se.payload.get("messages_file"),
                        group_id=se.payload.get("group_id"),
                    )
                )
    db.session.commit()
    return {"status": "ok"}


@api_bp.route("/metrics")
@jwt_required(optional=True)
def metrics():
    from .scheduler import registry

    data = generate_latest(registry)
    return Response(data, mimetype="text/plain")
