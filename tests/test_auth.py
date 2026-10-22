from conftest import PASSWORD, register


def test_register_login_and_use_token(client):
    headers = register(client, "alice")
    assert client.get("/events", params={"start": "2026-01-01", "end": "2026-01-02"}, headers=headers).status_code == 200

    resp = client.post("/auth/login", json={"username": "alice", "password": PASSWORD})
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    resp = client.get("/events", params={"start": "2026-01-01", "end": "2026-01-02"},
                      headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


def test_duplicate_registration_does_not_overwrite(client):
    register(client, "alice")
    resp = client.post("/auth/register", json={"username": "alice", "password": "another password"})
    assert resp.status_code == 409
    assert "access_token" not in resp.json()
    assert client.post("/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 200
    assert client.post("/auth/login", json={"username": "alice", "password": "another password"}).status_code == 401


def test_incorrect_credentials_issue_no_token(client):
    register(client, "alice")
    for body in ({"username": "alice", "password": "wrong password"},
                 {"username": "nobody", "password": PASSWORD}):
        resp = client.post("/auth/login", json=body)
        assert resp.status_code == 401
        assert "access_token" not in resp.json()
        assert body["password"] not in resp.text


def test_protected_operations_reject_bad_tokens(client):
    register(client, "alice")
    bad_headers = [
        {},
        {"Authorization": "Bearer"},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer not-a-jwt"},
        {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.invalidsig"},
    ]
    for headers in bad_headers:
        assert client.get("/events", params={"start": "2026-01-01", "end": "2026-01-02"}, headers=headers).status_code == 401
        assert client.post("/events", json={"title": "x", "start": "2026-01-01T09:00", "end": "2026-01-01T10:00"},
                           headers=headers).status_code == 401
        assert client.get("/events/1", headers=headers).status_code == 401
        assert client.delete("/events/1", headers=headers).status_code == 401


def test_responses_never_expose_passwords_or_hashes(client):
    resp = client.post("/auth/register", json={"username": "alice", "password": PASSWORD})
    login = client.post("/auth/login", json={"username": "alice", "password": PASSWORD})
    for r in (resp, login):
        assert PASSWORD not in r.text
        assert "argon2" not in r.text
        assert "password" not in r.text
