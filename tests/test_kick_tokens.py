from shared.kick_tokens import looks_like_cookie_token, mask_token, token_info
from shared.local_kick_mock import read_events
from scripts.bot_transports import (
    BotRateLimited,
    BotUnauthorized,
    LiveKickBot,
    LocalCookieBot,
)
from scripts.run_bot import resolve_token, resolve_transport_mode


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_cookie_token_is_detected_without_exposing_secret():
    token = "KP_UIDz-ssn=fake-session-value; cf_clearance=fake-clearance"

    info = token_info(token)

    assert looks_like_cookie_token(token)
    assert info.kind == "cookie"
    assert info.mode == "local_cookie_test"
    assert info.mask == "KP_UIDz-ssn=..."
    assert "fake-session-value" not in info.mask


def test_cookie_token_cannot_be_forced_live():
    token = "KP_UIDz-ssn=fake-session-value"

    assert resolve_transport_mode(token, "real") == "local_cookie_test"


def test_regular_token_masking():
    token = "oauth-token-example-123456789"

    assert not looks_like_cookie_token(token)
    assert token_info(token).mode == "live_kick"
    assert mask_token(token) == "oauth-...6789"


def test_runner_token_can_come_from_environment(monkeypatch):
    monkeypatch.setenv("KICK_BOT_TOKEN", "env-token")

    assert resolve_token(None) == "env-token"


def test_live_kick_transport_posts_to_official_chat_api():
    session = FakeSession(
        FakeResponse(payload={"data": {"is_sent": True, "message_id": "message-123"}})
    )
    transport = LiveKickBot(
        "oauth-token",
        api_url="https://api.kick.com/public/v1/chat",
        session=session,
    )

    result = transport.send_message("ignored-for-bot-type", "hello")

    assert result.ok is True
    assert result.simulated is False
    assert result.transport == "live_kick"
    assert result.message_id == "message-123"
    args, kwargs = session.calls[0]
    assert args == ("https://api.kick.com/public/v1/chat",)
    assert kwargs["json"] == {"content": "hello", "type": "bot"}
    assert kwargs["headers"]["Authorization"] == "Bearer oauth-token"


def test_live_kick_transport_maps_auth_and_rate_limit_errors():
    unauthorized = LiveKickBot("token", session=FakeSession(FakeResponse(401)))
    rate_limited = LiveKickBot(
        "token", session=FakeSession(FakeResponse(429, headers={"Retry-After": "30"}))
    )

    try:
        unauthorized.send_message("chan", "hello")
        assert False, "expected unauthorized"
    except BotUnauthorized:
        pass

    try:
        rate_limited.send_message("chan", "hello")
        assert False, "expected rate limit"
    except BotRateLimited as exc:
        assert exc.retry_after == "30"


def test_local_cookie_transport_expires_session():
    clock = FakeClock()
    transport = LocalCookieBot(
        "KP_UIDz-ssn=fake-session-value",
        ttl_seconds=5,
        min_interval_seconds=1,
        now_func=clock,
    )

    result = transport.send_message("chan", "hello")
    assert result.ok is True
    clock.advance(6)

    try:
        transport.send_message("chan", "hello")
        assert False, "expected local session expiry"
    except BotUnauthorized as exc:
        assert "local_session_expired" in str(exc)


def test_local_cookie_transport_rate_limits_actions():
    clock = FakeClock()
    transport = LocalCookieBot(
        "KP_UIDz-ssn=fake-session-value",
        ttl_seconds=60,
        min_interval_seconds=10,
        now_func=clock,
    )

    transport.send_message("chan", "hello")

    try:
        transport.follow_channel("chan")
        assert False, "expected local rate limit"
    except BotRateLimited as exc:
        assert exc.retry_after == "10"

    clock.advance(10)
    result = transport.follow_channel("chan")
    assert result.ok is True
    assert result.action == "follow_channel"


def test_local_cookie_transport_records_events(tmp_path):
    store_path = tmp_path / "mock.jsonl"
    transport = LocalCookieBot(
        "KP_UIDz-ssn=fake-session-value",
        ttl_seconds=60,
        min_interval_seconds=1,
        actor="tester",
        store_path=str(store_path),
    )

    result = transport.send_message("chan", "hello")

    events = read_events(path=store_path)
    assert result.message_id == events[0]["id"]
    assert events[0]["action"] == "send_message"
    assert events[0]["channel"] == "chan"
    assert events[0]["actor"] == "tester"
    assert events[0]["content"] == "hello"
