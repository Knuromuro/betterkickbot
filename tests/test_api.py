import pytest
import bcrypt
import time

from backend import create_app
from backend.models import db


@pytest.fixture
def client(tmp_path):
    password_hash = bcrypt.hashpw(b"admin", bcrypt.gensalt()).decode()
    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path}/test.db",
            "CACHE_TYPE": "SimpleCache",
            "ADMIN_PASSWORD_HASH": password_hash,
            "BOT_LOG_DIR": str(tmp_path / "logs"),
            "LOCAL_KICK_MOCK_FILE": str(tmp_path / "logs" / "local_kick_mock.jsonl"),
            "BOT_FORCE_LOCAL_TEST": True,
        }
    )
    with app.test_client() as client:
        with app.app_context():
            db.create_all()
        yield client


def test_create_group(client):
    res = client.post(
        "/dashboard/api/groups", json={"name": "grp", "target": "chan", "interval": 60}
    )
    assert res.status_code == 201
    gid = res.get_json()["id"]

    res = client.get("/dashboard/api/groups")
    assert res.status_code == 200
    data = res.get_json()
    assert any(g["id"] == gid for g in data["items"]) and data["total"] == 1

    res = client.get("/dashboard/api/groups?search=grp")
    assert res.get_json()["total"] == 1

    # duplicate name should fail
    res = client.post("/dashboard/api/groups", json={"name": "grp", "target": "chan2"})
    assert res.status_code == 400


def test_create_account(client):
    gid = client.post(
        "/dashboard/api/groups", json={"name": "g2", "target": "t"}
    ).get_json()["id"]
    res = client.post(
        "/dashboard/api/accounts",
        json={"username": "user", "password": "pass", "group_id": gid},
    )
    assert res.status_code == 201
    aid = res.get_json()["id"]

    res = client.get("/dashboard/api/accounts")
    data = res.get_json()
    assert any(a["id"] == aid for a in data["items"]) and data["total"] == 1

    # creating via /bots endpoint
    res = client.post(
        "/dashboard/api/bots",
        json={"username": "userb", "password": "pass", "group_id": gid},
    )
    assert res.status_code == 201

    # invalid group
    res = client.post(
        "/dashboard/api/accounts",
        json={"username": "bad", "password": "p", "group_id": 9999},
    )
    assert res.status_code == 400


def test_stats_endpoint(client):
    res = client.get("/dashboard/api/stats")
    assert res.status_code == 200
    data = res.get_json()
    assert "runs" in data and "errors" in data


def test_metrics_and_auth(client):
    res = client.post(
        "/auth/token",
        json={"username": "admin", "password": "admin", "totp": "123456"},
    )
    assert res.status_code == 200
    token = res.get_json()["access_token"]
    assert token
    metrics = client.get("/metrics")
    assert metrics.status_code == 200


def test_bot_start_stop(client, tmp_path):
    gid = client.post(
        "/dashboard/api/groups", json={"name": "g3", "target": "t"}
    ).get_json()["id"]
    aid = client.post(
        "/dashboard/api/accounts",
        json={"username": "user2", "password": "pass", "group_id": gid},
    ).get_json()["id"]

    res = client.post(f"/dashboard/api/bots/{aid}/start")
    assert res.status_code == 200
    assert "pid" in res.get_json()
    assert res.get_json()["mode"] == "local_test"

    res = client.get(f"/dashboard/api/bots/{aid}/status")
    assert res.status_code == 200

    res = client.post(f"/dashboard/api/bots/{aid}/stop")
    assert res.status_code == 200


def test_cookie_token_starts_as_local_test(client):
    gid = client.post(
        "/dashboard/api/groups", json={"name": "g4", "target": "chan", "interval": 1}
    ).get_json()["id"]
    aid = client.post(
        "/dashboard/api/accounts",
        json={
            "username": "cookie_user",
            "password": "KP_UIDz-ssn=fake-session-value; cf_clearance=fake",
            "group_id": gid,
        },
    ).get_json()["id"]

    accounts = client.get("/dashboard/api/accounts").get_json()["items"]
    account = next(a for a in accounts if a["id"] == aid)
    assert account["token_kind"] == "cookie"
    assert account["token_mode"] == "local_cookie_test"

    res = client.post(f"/dashboard/api/bots/{aid}/start")
    assert res.status_code == 200
    assert res.get_json()["mode"] == "local_cookie_test"

    lines = []
    for _ in range(20):
        lines = client.get(f"/dashboard/api/bots/{aid}/logs").get_json()
        if any("stage=send_result" in line for line in lines):
            break
        time.sleep(0.1)

    client.post(f"/dashboard/api/bots/{aid}/stop")

    assert any("mode=local_cookie_test" in line for line in lines)
    assert any("stage=send_result" in line for line in lines)
    assert any("simulated=True" in line for line in lines)


def test_bot_command_logs_manual_message(client):
    gid = client.post(
        "/dashboard/api/groups", json={"name": "g5", "target": "chan", "interval": 1}
    ).get_json()["id"]
    aid = client.post(
        "/dashboard/api/accounts",
        json={
            "username": "cmd_user",
            "password": "KP_UIDz-ssn=fake-session-value",
            "group_id": gid,
        },
    ).get_json()["id"]

    res = client.post(
        f"/dashboard/api/bots/{aid}/command",
        json={"cmd": "send_message", "args": {"message": "test"}},
    )

    assert res.status_code == 200
    assert res.get_json()["status"] == "ok"
    lines = client.get(f"/dashboard/api/bots/{aid}/logs").get_json()
    assert any("stage=local_action" in line for line in lines)
    events = client.get("/dashboard/api/local/events").get_json()["items"]
    assert any(e["action"] == "send_message" and e["content"] == "test" for e in events)


def test_bot_command_logs_follow_test(client):
    gid = client.post(
        "/dashboard/api/groups", json={"name": "g6", "target": "chan", "interval": 1}
    ).get_json()["id"]
    aid = client.post(
        "/dashboard/api/accounts",
        json={
            "username": "follow_user",
            "password": "KP_UIDz-ssn=fake-session-value",
            "group_id": gid,
        },
    ).get_json()["id"]

    res = client.post(
        f"/dashboard/api/bots/{aid}/command",
        json={"cmd": "follow_channel", "args": {}},
    )

    assert res.status_code == 200
    body = res.get_json()
    assert body["status"] == "ok"
    assert body["channel"] == "chan"
    lines = client.get(f"/dashboard/api/bots/{aid}/logs").get_json()
    assert any("stage=local_action" in line for line in lines)
    events = client.get("/dashboard/api/local/events").get_json()["items"]
    assert any(
        e["action"] == "follow_channel" and e["channel"] == "chan" for e in events
    )


def test_local_events_can_be_cleared(client):
    gid = client.post(
        "/dashboard/api/groups", json={"name": "g7", "target": "chan", "interval": 1}
    ).get_json()["id"]
    aid = client.post(
        "/dashboard/api/accounts",
        json={
            "username": "clear_user",
            "password": "KP_UIDz-ssn=fake-session-value",
            "group_id": gid,
        },
    ).get_json()["id"]
    client.post(
        f"/dashboard/api/bots/{aid}/command",
        json={"cmd": "send_message", "args": {"message": "to-clear"}},
    )
    assert client.get("/dashboard/api/local/events").get_json()["items"]

    res = client.delete("/dashboard/api/local/events")

    assert res.status_code == 200
    assert client.get("/dashboard/api/local/events").get_json()["items"] == []
