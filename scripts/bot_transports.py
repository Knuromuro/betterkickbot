from __future__ import annotations

from dataclasses import dataclass
import os
import time
from typing import Any

import requests

from shared.local_kick_mock import record_event
from shared.kick_tokens import looks_like_cookie_token, token_info

DEFAULT_CHAT_API_URL = "https://api.kick.com/public/v1/chat"
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_LOCAL_SESSION_TTL_SECONDS = 300
DEFAULT_LOCAL_MIN_INTERVAL_SECONDS = 2


class BotTransportError(RuntimeError):
    """Base error for transport failures that are safe to log."""


class BotUnauthorized(BotTransportError):
    """The access token is missing, expired, or invalid."""


class BotForbidden(BotTransportError):
    """The access token is valid but lacks permission for the action."""


class BotRateLimited(BotTransportError):
    """The remote API rejected the request because of rate limiting."""

    def __init__(self, retry_after: str | None = None):
        detail = f" retry_after={retry_after}" if retry_after else ""
        super().__init__(f"rate_limited{detail}")
        self.retry_after = retry_after


@dataclass(frozen=True)
class BotActionResult:
    ok: bool
    transport: str
    action: str
    channel: str
    simulated: bool
    message_id: str | None = None
    detail: str | None = None


class BaseBotTransport:
    name = "base"
    simulated = True

    def send_message(self, channel: str, message: str) -> BotActionResult:
        raise NotImplementedError

    def follow_channel(self, channel: str) -> BotActionResult:
        return BotActionResult(
            ok=False,
            transport=self.name,
            action="follow_channel",
            channel=channel,
            simulated=self.simulated,
            detail="unsupported",
        )


class LocalCookieBot(BaseBotTransport):
    name = "local_cookie_test"
    simulated = True

    def __init__(
        self,
        token: str,
        mode: str = "local_cookie_test",
        ttl_seconds: float | None = None,
        min_interval_seconds: float | None = None,
        now_func=time.monotonic,
        actor: str = "local-bot",
        store_path: str | None = None,
    ):
        self.token = token
        self.mode = mode
        self.info = token_info(token)
        self.actor = actor
        self.store_path = store_path
        self.created_at = now_func()
        self.now_func = now_func
        self.ttl_seconds = ttl_seconds or float(
            os.getenv(
                "LOCAL_COOKIE_TEST_TTL_SECONDS", DEFAULT_LOCAL_SESSION_TTL_SECONDS
            )
        )
        self.min_interval_seconds = min_interval_seconds or float(
            os.getenv(
                "LOCAL_COOKIE_TEST_MIN_INTERVAL_SECONDS",
                DEFAULT_LOCAL_MIN_INTERVAL_SECONDS,
            )
        )
        self.last_action_at: float | None = None

    def _check_session(self) -> None:
        age = self.now_func() - self.created_at
        if age > self.ttl_seconds:
            raise BotUnauthorized("local_session_expired")

    def _check_rate_limit(self) -> None:
        now = self.now_func()
        if self.last_action_at is None:
            self.last_action_at = now
            return
        elapsed = now - self.last_action_at
        if elapsed < self.min_interval_seconds:
            retry_after = max(1, int(round(self.min_interval_seconds - elapsed)))
            raise BotRateLimited(str(retry_after))
        self.last_action_at = now

    def send_message(self, channel: str, message: str) -> BotActionResult:
        self._check_session()
        self._check_rate_limit()
        detail = (
            f"token_kind={self.info.kind} size={len(message)} "
            f"ttl={int(self.ttl_seconds)}"
        )
        event = self._record_event(
            action="send_message",
            channel=channel,
            content=message,
            detail=detail,
        )
        return BotActionResult(
            ok=True,
            transport=self.mode,
            action="send_message",
            channel=channel,
            simulated=True,
            message_id=event["id"],
            detail=detail,
        )

    def follow_channel(self, channel: str) -> BotActionResult:
        self._check_session()
        self._check_rate_limit()
        detail = f"token_kind={self.info.kind} ttl={int(self.ttl_seconds)}"
        event = self._record_event(
            action="follow_channel",
            channel=channel,
            detail=detail,
        )
        return BotActionResult(
            ok=True,
            transport=self.mode,
            action="follow_channel",
            channel=channel,
            simulated=True,
            message_id=event["id"],
            detail=detail,
        )

    def _record_event(
        self,
        *,
        action: str,
        channel: str,
        content: str | None = None,
        detail: str | None = None,
    ) -> dict:
        return record_event(
            action=action,
            channel=channel,
            actor=self.actor,
            transport=self.mode,
            simulated=True,
            content=content,
            detail=detail,
            path=self.store_path,
        )


class LiveKickBot(BaseBotTransport):
    name = "live_kick"
    simulated = False

    def __init__(
        self,
        token: str,
        api_url: str | None = None,
        chat_type: str | None = None,
        timeout_seconds: float | None = None,
        session: requests.Session | None = None,
    ):
        self.token = token
        self.api_url = api_url or os.getenv("KICK_CHAT_API_URL", DEFAULT_CHAT_API_URL)
        self.chat_type = chat_type or os.getenv("KICK_CHAT_TYPE", "bot")
        self.timeout_seconds = timeout_seconds or float(
            os.getenv("KICK_API_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
        )
        self.session = session or requests.Session()

    def _payload(self, channel: str, message: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "content": message,
            "type": self.chat_type,
        }
        broadcaster_id = os.getenv("KICK_BROADCASTER_USER_ID")
        if self.chat_type == "user" and broadcaster_id:
            payload["broadcaster_user_id"] = int(broadcaster_id)
        elif self.chat_type == "user" and channel.isdigit():
            payload["broadcaster_user_id"] = int(channel)
        return payload

    def send_message(self, channel: str, message: str) -> BotActionResult:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        response = self.session.post(
            self.api_url,
            json=self._payload(channel, message),
            headers=headers,
            timeout=self.timeout_seconds,
        )
        if response.status_code == 401:
            raise BotUnauthorized("unauthorized")
        if response.status_code == 403:
            raise BotForbidden("forbidden_missing_scope_or_bot_access")
        if response.status_code == 429:
            raise BotRateLimited(response.headers.get("Retry-After"))
        if response.status_code >= 400:
            raise BotTransportError(f"http_status={response.status_code}")

        data = _safe_json(response)
        message_id = None
        if isinstance(data.get("data"), dict):
            message_id = data["data"].get("message_id")
        return BotActionResult(
            ok=True,
            transport=self.name,
            action="send_message",
            channel=channel,
            simulated=False,
            message_id=message_id,
            detail="sent",
        )


def _safe_json(response: requests.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def resolve_transport_mode(token: str, requested_mode: str) -> str:
    if looks_like_cookie_token(token):
        return "local_cookie_test"
    if requested_mode == "local":
        return "local_test"
    return "live_kick"


def create_transport(
    token: str,
    requested_mode: str,
    actor: str = "local-bot",
    local_store_path: str | None = None,
) -> BaseBotTransport:
    mode = resolve_transport_mode(token, requested_mode)
    if mode in {"local_cookie_test", "local_test"}:
        return LocalCookieBot(
            token,
            mode=mode,
            actor=actor,
            store_path=local_store_path or os.getenv("LOCAL_KICK_MOCK_FILE"),
        )
    return LiveKickBot(token)
