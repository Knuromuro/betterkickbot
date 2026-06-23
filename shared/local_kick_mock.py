from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import tempfile
import time
from uuid import uuid4

from shared.social_platform import PlatformActionResult, SocialPlatformAdapter

DEFAULT_LOCAL_MOCK_FILE = "logs/local_kick_mock.json"
DEFAULT_SESSION_TTL_SECONDS = 300
DEFAULT_RATE_WINDOW_SECONDS = 60
DEFAULT_PER_ACCOUNT_LIMIT = 30
DEFAULT_GLOBAL_LIMIT = 300
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 2

ACTIVE = "active"
BLOCKED = "blocked"
NO_SESSION = "no_session"
RATE_LIMITED = "rate_limited"
ACCOUNT_STATUSES = {ACTIVE, BLOCKED, NO_SESSION, RATE_LIMITED}
RETRYABLE_CODES = {"rate_limited", "timeout", "session_expired"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_mock_path() -> Path:
    return Path(os.getenv("LOCAL_KICK_MOCK_FILE", DEFAULT_LOCAL_MOCK_FILE))


def resolve_mock_path(path: str | Path | None = None) -> Path:
    return Path(path) if path is not None else default_mock_path()


def _now_epoch() -> float:
    return time.time()


def _default_state() -> dict:
    return {
        "version": 2,
        "settings": {
            "session_ttl_seconds": DEFAULT_SESSION_TTL_SECONDS,
            "rate_window_seconds": DEFAULT_RATE_WINDOW_SECONDS,
            "per_account_limit": DEFAULT_PER_ACCOUNT_LIMIT,
            "global_limit": DEFAULT_GLOBAL_LIMIT,
            "max_attempts": DEFAULT_MAX_ATTEMPTS,
            "backoff_seconds": DEFAULT_BACKOFF_SECONDS,
        },
        "accounts": {},
        "channels": {},
        "actions": [],
        "queue": [],
        "global_rate_timestamps": [],
    }


def _read_json_or_legacy_events(path: Path) -> dict:
    if not path.exists() or path.stat().st_size == 0:
        return _default_state()
    text = path.read_text(encoding="utf-8", errors="ignore").strip()
    if not text:
        return _default_state()
    if text.startswith("{"):
        try:
            state = json.loads(text)
            return _normalize_state(state)
        except json.JSONDecodeError:
            return _default_state()

    state = _default_state()
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            state["actions"].append(event)
    return state


def _normalize_state(state: dict) -> dict:
    base = _default_state()
    if not isinstance(state, dict):
        return base
    for key in ("settings", "accounts", "channels", "actions", "queue"):
        if key in state:
            base[key] = state[key]
    base["version"] = 2
    base["global_rate_timestamps"] = state.get("global_rate_timestamps", [])
    return base


def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=True, indent=2)
    Path(tmp_name).replace(path)


def _prune_timestamps(
    timestamps: list[float], now: float, window: float
) -> list[float]:
    cutoff = now - window
    return [ts for ts in timestamps if ts >= cutoff]


def _retry_after(timestamps: list[float], now: float, window: float) -> int:
    if not timestamps:
        return 1
    return max(1, int(round(window - (now - min(timestamps)))))


class LocalKickMockStore:
    def __init__(self, path: str | Path | None = None, now_func=_now_epoch):
        self.path = resolve_mock_path(path)
        self.now_func = now_func

    def load(self) -> dict:
        return _read_json_or_legacy_events(self.path)

    def save(self, state: dict) -> None:
        _write_state(self.path, _normalize_state(state))

    def reset(self) -> None:
        self.save(_default_state())


class LocalKickMockAdapter(SocialPlatformAdapter):
    def __init__(
        self,
        store: LocalKickMockStore | None = None,
        *,
        path: str | Path | None = None,
        now_func=_now_epoch,
    ):
        self.store = store or LocalKickMockStore(path=path, now_func=now_func)
        self.now_func = now_func

    def create_account(
        self,
        username: str,
        *,
        account_id: str | None = None,
        status: str = ACTIVE,
        session_ttl_seconds: int | None = None,
    ) -> dict:
        if status not in ACCOUNT_STATUSES:
            raise ValueError(f"unsupported account status: {status}")
        state = self.store.load()
        settings = state["settings"]
        ttl = int(session_ttl_seconds or settings["session_ttl_seconds"])
        now = self.now_func()
        account_id = str(account_id or uuid4())
        account = {
            "id": account_id,
            "username": username,
            "status": status,
            "session_token": str(uuid4()) if status != NO_SESSION else None,
            "session_expires_at": now + ttl if status != NO_SESSION else None,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "rate_timestamps": [],
            "history": [],
        }
        state["accounts"][account_id] = account
        self.store.save(state)
        return _public_account(account)

    def ensure_account(self, account_id: str, username: str) -> dict:
        state = self.store.load()
        if account_id in state["accounts"]:
            return _public_account(state["accounts"][account_id])
        self.store.save(state)
        return self.create_account(username, account_id=account_id)

    def update_account(
        self,
        account_id: str,
        *,
        status: str | None = None,
        session_ttl_seconds: int | None = None,
    ) -> dict | None:
        state = self.store.load()
        account = state["accounts"].get(str(account_id))
        if not account:
            return None
        if status:
            if status not in ACCOUNT_STATUSES:
                raise ValueError(f"unsupported account status: {status}")
            account["status"] = status
            if status == NO_SESSION:
                account["session_token"] = None
                account["session_expires_at"] = None
            elif not account.get("session_token"):
                account["session_token"] = str(uuid4())
        if session_ttl_seconds is not None:
            account["session_expires_at"] = self.now_func() + int(session_ttl_seconds)
        account["updated_at"] = utc_now()
        self.store.save(state)
        return _public_account(account)

    def refresh_session(
        self, account_id: str, ttl_seconds: int | None = None
    ) -> dict | None:
        state = self.store.load()
        account = state["accounts"].get(str(account_id))
        if not account:
            return None
        ttl = int(ttl_seconds or state["settings"]["session_ttl_seconds"])
        account["status"] = ACTIVE
        account["session_token"] = str(uuid4())
        account["session_expires_at"] = self.now_func() + ttl
        account["updated_at"] = utc_now()
        self.store.save(state)
        return _public_account(account)

    def list_accounts(self) -> list[dict]:
        state = self.store.load()
        return [_public_account(a) for a in state["accounts"].values()]

    def get_user(self, account_id: str) -> dict | None:
        state = self.store.load()
        account = state["accounts"].get(str(account_id))
        return _public_account(account) if account else None

    def get_channel(self, channel: str) -> dict:
        state = self.store.load()
        channel_state = _ensure_channel(state, channel)
        self.store.save(state)
        return _public_channel(channel_state)

    def get_followers(self, channel: str) -> list[str]:
        state = self.store.load()
        channel_state = _ensure_channel(state, channel)
        self.store.save(state)
        return list(channel_state["followers"])

    def send_message(
        self, account_id: str, channel: str, message: str
    ) -> PlatformActionResult:
        return self._execute(account_id, "send_message", channel, content=message)

    def follow_channel(self, account_id: str, channel: str) -> PlatformActionResult:
        return self._execute(account_id, "follow_channel", channel)

    def unfollow_channel(self, account_id: str, channel: str) -> PlatformActionResult:
        return self._execute(account_id, "unfollow_channel", channel)

    def enqueue_action(
        self,
        *,
        account_id: str,
        action: str,
        channel: str,
        content: str | None = None,
        max_attempts: int | None = None,
    ) -> dict:
        state = self.store.load()
        job = self._build_job(
            state,
            account_id=str(account_id),
            action=action,
            channel=channel,
            content=content,
            max_attempts=max_attempts,
        )
        state["queue"].append(job)
        self.store.save(state)
        return job

    def enqueue_many(self, actions: list[dict]) -> list[dict]:
        state = self.store.load()
        jobs = []
        for action in actions:
            job = self._build_job(
                state,
                account_id=str(action["account_id"]),
                action=action["action"],
                channel=action["channel"],
                content=action.get("content"),
                max_attempts=action.get("max_attempts"),
            )
            state["queue"].append(job)
            jobs.append(job)
        self.store.save(state)
        return jobs

    def process_queue(self, limit: int = 100) -> dict:
        processed = []
        now = self.now_func()
        state = self.store.load()
        for job in state["queue"]:
            if len(processed) >= max(1, int(limit)):
                break
            if job["status"] != "pending":
                continue
            if float(job.get("next_run_at", 0)) > now:
                continue
            result = self._process_job_in_state(state, job)
            processed.append(result)
        self.store.save(state)
        return {"processed": processed, "count": len(processed)}

    def list_queue(self) -> list[dict]:
        return list(self.store.load()["queue"])

    def list_actions(self, limit: int = 100) -> list[dict]:
        actions = self.store.load()["actions"]
        return actions[-max(1, limit) :]

    def clear_actions(self) -> None:
        state = self.store.load()
        state["actions"] = []
        for account in state["accounts"].values():
            account["history"] = []
        for channel in state["channels"].values():
            channel["messages"] = []
        self.store.save(state)

    def report(self) -> dict:
        state = self.store.load()
        actions = state["actions"]
        queue = state["queue"]
        counts = {}
        codes = {}
        for action in actions:
            counts[action["status"]] = counts.get(action["status"], 0) + 1
            codes[action["code"]] = codes.get(action["code"], 0) + 1
        queue_counts = {}
        for job in queue:
            queue_counts[job["status"]] = queue_counts.get(job["status"], 0) + 1
        return {
            "actions": {
                "total": len(actions),
                "by_status": counts,
                "by_code": codes,
                "success": counts.get("success", 0),
                "failed": counts.get("failed", 0),
                "rate_limited": codes.get("rate_limited", 0),
            },
            "queue": {
                "total": len(queue),
                "by_status": queue_counts,
            },
            "accounts": {
                "total": len(state["accounts"]),
                "by_status": _count_by(state["accounts"].values(), "status"),
            },
            "channels": {"total": len(state["channels"])},
        }

    def mass_test(
        self,
        *,
        action_count: int = 1000,
        account_count: int = 10,
        channel: str = "local-channel",
        process: bool = True,
    ) -> dict:
        existing = {a["username"]: a for a in self.list_accounts()}
        accounts = []
        for idx in range(account_count):
            username = f"local_user_{idx + 1}"
            account = existing.get(username) or self.create_account(username)
            accounts.append(account)

        actions = []
        for idx in range(action_count):
            account = accounts[idx % len(accounts)]
            if idx % 5 == 0:
                actions.append(
                    {
                        "account_id": account["id"],
                        "action": "follow_channel",
                        "channel": channel,
                    }
                )
            else:
                actions.append(
                    {
                        "account_id": account["id"],
                        "action": "send_message",
                        "channel": channel,
                        "content": f"local message {idx + 1}",
                    }
                )
        jobs = self.enqueue_many(actions)
        result = {"queued": len(jobs), "processed": 0, "report": self.report()}
        if process:
            processed_total = 0
            while True:
                batch = self.process_queue(limit=200)
                processed_total += batch["count"]
                if batch["count"] == 0:
                    break
            result["processed"] = processed_total
            result["report"] = self.report()
        return result

    def _build_job(
        self,
        state: dict,
        *,
        account_id: str,
        action: str,
        channel: str,
        content: str | None = None,
        max_attempts: int | None = None,
    ) -> dict:
        return {
            "id": str(uuid4()),
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "account_id": str(account_id),
            "action": action,
            "channel": channel,
            "content": content,
            "status": "pending",
            "attempts": 0,
            "max_attempts": int(max_attempts or state["settings"]["max_attempts"]),
            "next_run_at": self.now_func(),
            "last_error": None,
            "result_event_id": None,
        }

    def _process_job(self, job_id: str) -> dict:
        state = self.store.load()
        job = next((j for j in state["queue"] if j["id"] == job_id), None)
        result = self._process_job_in_state(state, job)
        self.store.save(state)
        return result

    def _process_job_in_state(self, state: dict, job: dict | None) -> dict:
        job_id = job.get("id") if job else None
        if not job or job["status"] != "pending":
            return {"job_id": job_id, "status": "skipped", "reason": "not_pending"}
        job["status"] = "running"
        job["attempts"] += 1
        job["updated_at"] = utc_now()

        if job["action"] in {"send_message", "follow_channel", "unfollow_channel"}:
            content = job.get("content")
            if job["action"] == "send_message" and content is None:
                content = ""
            result = self._execute_in_state(
                state,
                job["account_id"],
                job["action"],
                job["channel"],
                content=content,
            )
        else:
            result = self._record_result(
                state,
                action=job["action"],
                account_id=job["account_id"],
                channel=job["channel"],
                status="failed",
                code="unsupported_action",
                content=job.get("content"),
                error="unsupported_action",
                persist=False,
            )

        if result.ok:
            job["status"] = "success"
            job["result_event_id"] = result.event_id
            job["last_error"] = None
        elif result.code in RETRYABLE_CODES and job["attempts"] < job["max_attempts"]:
            backoff = self._backoff_seconds(state, job, result)
            job["status"] = "pending"
            job["next_run_at"] = self.now_func() + backoff
            job["last_error"] = result.code
        elif result.code in {"blocked", "no_session", "forbidden"}:
            job["status"] = "skipped"
            job["last_error"] = result.code
        else:
            job["status"] = "failed"
            job["last_error"] = result.code
        job["updated_at"] = utc_now()
        return {"job_id": job_id, "status": job["status"], "code": result.code}

    def _backoff_seconds(
        self, state: dict, job: dict, result: PlatformActionResult
    ) -> int:
        base = int(state["settings"]["backoff_seconds"])
        retry_after = result.retry_after or 0
        return max(retry_after, base * (2 ** max(0, job["attempts"] - 1)))

    def _execute(
        self,
        account_id: str,
        action: str,
        channel: str,
        content: str | None = None,
    ) -> PlatformActionResult:
        state = self.store.load()
        result = self._execute_in_state(state, account_id, action, channel, content)
        self.store.save(state)
        return result

    def _execute_in_state(
        self,
        state: dict,
        account_id: str,
        action: str,
        channel: str,
        content: str | None = None,
    ) -> PlatformActionResult:
        account = state["accounts"].get(str(account_id))
        channel_state = _ensure_channel(state, channel)
        now = self.now_func()
        if action == "timeout":
            return self._record_result(
                state,
                action=action,
                account_id=str(account_id),
                channel=channel,
                status="failed",
                code="timeout",
                content=content,
                error="simulated_timeout",
                persist=False,
            )
        preflight = self._preflight(state, account, now)
        if preflight is not None:
            return self._record_result(
                state,
                action=action,
                account_id=str(account_id),
                channel=channel,
                status="failed",
                code=preflight["code"],
                content=content,
                error=preflight["error"],
                retry_after=preflight.get("retry_after"),
                persist=False,
            )

        if action == "send_message":
            event_id = str(uuid4())
            channel_state["messages"].append(
                {
                    "id": event_id,
                    "account_id": str(account_id),
                    "content": content or "",
                    "timestamp": utc_now(),
                }
            )
        elif action == "follow_channel":
            followers = channel_state["followers"]
            if str(account_id) not in followers:
                followers.append(str(account_id))
        elif action == "unfollow_channel":
            channel_state["followers"] = [
                follower
                for follower in channel_state["followers"]
                if follower != str(account_id)
            ]
        else:
            return self._record_result(
                state,
                action=action,
                account_id=str(account_id),
                channel=channel,
                status="failed",
                code="unsupported_action",
                content=content,
                error="unsupported_action",
                persist=False,
            )

        _record_rate_usage(state, account, now)
        return self._record_result(
            state,
            action=action,
            account_id=str(account_id),
            channel=channel,
            status="success",
            code="ok",
            content=content,
            persist=False,
        )

    def _preflight(self, state: dict, account: dict | None, now: float) -> dict | None:
        if account is None:
            return {"code": "not_found", "error": "account_not_found"}
        status = account.get("status", ACTIVE)
        if status == BLOCKED:
            return {"code": "blocked", "error": "account_blocked"}
        if status == NO_SESSION or not account.get("session_token"):
            return {"code": "no_session", "error": "account_has_no_session"}
        if status == RATE_LIMITED:
            return {
                "code": "rate_limited",
                "error": "account_rate_limited",
                "retry_after": 60,
            }
        expires_at = account.get("session_expires_at")
        if expires_at is not None and float(expires_at) <= now:
            return {"code": "session_expired", "error": "session_expired"}
        rate = self._rate_limit_check(state, account, now)
        if rate:
            return rate
        return None

    def _rate_limit_check(self, state: dict, account: dict, now: float) -> dict | None:
        settings = state["settings"]
        window = float(settings["rate_window_seconds"])
        account["rate_timestamps"] = _prune_timestamps(
            account.get("rate_timestamps", []), now, window
        )
        state["global_rate_timestamps"] = _prune_timestamps(
            state.get("global_rate_timestamps", []), now, window
        )
        if len(account["rate_timestamps"]) >= int(settings["per_account_limit"]):
            return {
                "code": "rate_limited",
                "error": "per_account_rate_limited",
                "retry_after": _retry_after(account["rate_timestamps"], now, window),
            }
        if len(state["global_rate_timestamps"]) >= int(settings["global_limit"]):
            return {
                "code": "rate_limited",
                "error": "global_rate_limited",
                "retry_after": _retry_after(
                    state["global_rate_timestamps"], now, window
                ),
            }
        return None

    def _record_result(
        self,
        state: dict,
        *,
        action: str,
        account_id: str,
        channel: str,
        status: str,
        code: str,
        content: str | None = None,
        error: str | None = None,
        retry_after: int | None = None,
        persist: bool = True,
    ) -> PlatformActionResult:
        event = {
            "id": str(uuid4()),
            "timestamp": utc_now(),
            "action": action,
            "account_id": account_id,
            "actor": state["accounts"].get(account_id, {}).get("username", account_id),
            "channel": channel,
            "transport": "local_kick_mock",
            "simulated": True,
            "status": status,
            "code": code,
        }
        if content is not None:
            event["content"] = content
        if error is not None:
            event["error"] = error
        if retry_after is not None:
            event["retry_after"] = retry_after
        state["actions"].append(event)
        account = state["accounts"].get(account_id)
        if account is not None:
            account.setdefault("history", []).append(event["id"])
            account["updated_at"] = utc_now()
        if persist:
            self.store.save(state)
        return PlatformActionResult(
            ok=status == "success",
            status=status,
            code=code,
            action=action,
            account_id=account_id,
            channel=channel,
            event_id=event["id"],
            retry_after=retry_after,
            error=error,
            data={"event": event},
        )


def _record_rate_usage(state: dict, account: dict, now: float) -> None:
    account.setdefault("rate_timestamps", []).append(now)
    state.setdefault("global_rate_timestamps", []).append(now)


def _ensure_channel(state: dict, channel: str) -> dict:
    key = str(channel)
    if key not in state["channels"]:
        state["channels"][key] = {
            "name": key,
            "title": key,
            "created_at": utc_now(),
            "followers": [],
            "messages": [],
        }
    return state["channels"][key]


def _public_account(account: dict) -> dict:
    public = {key: value for key, value in account.items() if key != "session_token"}
    public["has_session"] = bool(account.get("session_token"))
    return public


def _public_channel(channel: dict) -> dict:
    return {
        "name": channel["name"],
        "title": channel.get("title", channel["name"]),
        "followers_count": len(channel.get("followers", [])),
        "messages_count": len(channel.get("messages", [])),
        "followers": list(channel.get("followers", [])),
        "messages": list(channel.get("messages", []))[-100:],
    }


def _count_by(items, key: str) -> dict:
    result = {}
    for item in items:
        value = item.get(key)
        result[value] = result.get(value, 0) + 1
    return result


def record_event(
    *,
    action: str,
    channel: str,
    actor: str,
    transport: str,
    simulated: bool,
    content: str | None = None,
    detail: str | None = None,
    path: str | Path | None = None,
) -> dict:
    adapter = LocalKickMockAdapter(path=path)
    account = adapter.ensure_account(actor, actor)
    if action == "send_message":
        result = adapter.send_message(account["id"], channel, content or "")
    elif action == "follow_channel":
        result = adapter.follow_channel(account["id"], channel)
    elif action == "unfollow_channel":
        result = adapter.unfollow_channel(account["id"], channel)
    else:
        state = adapter.store.load()
        result = adapter._record_result(
            state,
            action=action,
            account_id=account["id"],
            channel=channel,
            status="success",
            code="ok",
            content=content,
        )
    event = result.data.get("event", {})
    state = adapter.store.load()
    for action_event in state["actions"]:
        if action_event["id"] == event.get("id"):
            action_event["transport"] = transport
            action_event["simulated"] = simulated
            if detail:
                action_event["detail"] = detail
            event = action_event
            break
    adapter.store.save(state)
    return event


def read_events(limit: int = 100, path: str | Path | None = None) -> list[dict]:
    return LocalKickMockAdapter(path=path).list_actions(limit=limit)


def clear_events(path: str | Path | None = None) -> None:
    LocalKickMockAdapter(path=path).clear_actions()
