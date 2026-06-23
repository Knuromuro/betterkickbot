from shared.local_kick_mock import (
    ACTIVE,
    BLOCKED,
    NO_SESSION,
    RATE_LIMITED,
    LocalKickMockAdapter,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_adapter(tmp_path, clock):
    return LocalKickMockAdapter(path=tmp_path / "local_mock.json", now_func=clock)


def update_settings(adapter, **settings):
    state = adapter.store.load()
    state["settings"].update(settings)
    adapter.store.save(state)


def test_rate_limit_per_account_and_global(tmp_path):
    clock = FakeClock()
    adapter = make_adapter(tmp_path, clock)
    update_settings(
        adapter, per_account_limit=1, global_limit=1, rate_window_seconds=60
    )
    first = adapter.create_account("first")
    second = adapter.create_account("second")

    assert adapter.send_message(first["id"], "chan", "one").ok is True

    per_account = adapter.send_message(first["id"], "chan", "two")
    assert per_account.ok is False
    assert per_account.code == "rate_limited"
    assert per_account.retry_after >= 1

    global_limit = adapter.send_message(second["id"], "chan", "three")
    assert global_limit.ok is False
    assert global_limit.code == "rate_limited"
    assert global_limit.error == "global_rate_limited"


def test_queue_processes_follow_and_message(tmp_path):
    clock = FakeClock()
    adapter = make_adapter(tmp_path, clock)
    update_settings(adapter, per_account_limit=100, global_limit=100)
    account = adapter.create_account("queue-user")

    adapter.enqueue_action(
        account_id=account["id"],
        action="send_message",
        channel="queue-chan",
        content="hello",
    )
    adapter.enqueue_action(
        account_id=account["id"],
        action="follow_channel",
        channel="queue-chan",
    )

    processed = adapter.process_queue(limit=10)

    assert processed["count"] == 2
    assert {job["status"] for job in adapter.list_queue()} == {"success"}
    assert account["id"] in adapter.get_followers("queue-chan")
    assert adapter.report()["actions"]["success"] == 2


def test_multiple_accounts_and_status_failures(tmp_path):
    clock = FakeClock()
    adapter = make_adapter(tmp_path, clock)
    active = adapter.create_account("active", status=ACTIVE)
    blocked = adapter.create_account("blocked", status=BLOCKED)
    no_session = adapter.create_account("no-session", status=NO_SESSION)
    rate_limited = adapter.create_account("limited", status=RATE_LIMITED)

    assert adapter.send_message(active["id"], "chan", "ok").ok is True
    assert adapter.send_message(blocked["id"], "chan", "blocked").code == "blocked"
    assert (
        adapter.send_message(no_session["id"], "chan", "no session").code
        == "no_session"
    )
    assert (
        adapter.send_message(rate_limited["id"], "chan", "limited").code
        == "rate_limited"
    )

    report = adapter.report()
    assert report["accounts"]["total"] == 4
    assert report["accounts"]["by_status"]["active"] == 1
    assert report["accounts"]["by_status"]["blocked"] == 1
    assert report["accounts"]["by_status"]["no_session"] == 1
    assert report["accounts"]["by_status"]["rate_limited"] == 1
    assert report["actions"]["success"] == 1
    assert report["actions"]["failed"] == 3


def test_mass_test_can_run_1000_local_actions(tmp_path):
    clock = FakeClock()
    adapter = make_adapter(tmp_path, clock)
    update_settings(adapter, per_account_limit=1000, global_limit=2000)

    result = adapter.mass_test(action_count=1000, account_count=50, process=True)

    assert result["queued"] == 1000
    assert result["processed"] == 1000
    assert result["report"]["actions"]["total"] == 1000
    assert result["report"]["actions"]["success"] == 1000
    assert result["report"]["queue"]["by_status"]["success"] == 1000


def test_session_expiry_retries_after_refresh(tmp_path):
    clock = FakeClock()
    adapter = make_adapter(tmp_path, clock)
    update_settings(adapter, per_account_limit=100, global_limit=100, backoff_seconds=2)
    account = adapter.create_account("expiring", session_ttl_seconds=1)
    job = adapter.enqueue_action(
        account_id=account["id"],
        action="send_message",
        channel="retry-chan",
        content="after refresh",
    )

    clock.advance(2)
    first = adapter.process_queue(limit=10)
    queued_job = adapter.list_queue()[0]

    assert first["processed"][0]["code"] == "session_expired"
    assert queued_job["id"] == job["id"]
    assert queued_job["status"] == "pending"
    assert queued_job["attempts"] == 1
    assert queued_job["last_error"] == "session_expired"

    adapter.refresh_session(account["id"], ttl_seconds=60)
    clock.advance(2)
    second = adapter.process_queue(limit=10)
    finished_job = adapter.list_queue()[0]

    assert second["processed"][0]["code"] == "ok"
    assert finished_job["status"] == "success"
    assert finished_job["attempts"] == 2
