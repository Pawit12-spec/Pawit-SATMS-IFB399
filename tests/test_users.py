from unittest.mock import MagicMock
import pytest
from types import SimpleNamespace
import psycopg2

@pytest.fixture(autouse=True)
def pg(monkeypatch):
    """Mock PostgreSQL connection for tests."""
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchall.return_value = []

    conn.cursor.return_value.__enter__.return_value = cur

    # Ensure that we're gaining access to fake/test database instead of actual :P
    # Pretty cool! https://docs.pytest.org/en/stable/how-to/monkeypatch.html
    monkeypatch.setattr("app.routes._get_pg_conn", lambda: conn)

    return SimpleNamespace(conn=conn, cur=cur)

def test_users_page_returns_200(client):
    resp = client.get("/assets?tab=users")
    assert resp.status_code == 200


def test_create_user_redirects_on_success(client):
    resp = client.post("/users/create", data={
        "full_name": "William Hocking",
        "email": "william@eql.com.au",
        "password": "supersecret",
        "role": "user",
    })
    assert resp.status_code == 302
    assert "success" in resp.headers["Location"]


def test_create_user_missing_fields_shows_error(client):
    resp = client.post("/users/create", data={
        "full_name": "",
        "email": "a@b.com",
        "password": "epicpassword",
    })
    assert resp.status_code == 302
    assert "error" in resp.headers["Location"]


def test_create_user_duplicate_email_shows_error(pg, client):
    pg.cur.execute.side_effect = psycopg2.errors.UniqueViolation("duplicate")
    resp = client.post("/users/create", data={
        "full_name": "Pawit Singh",
        "email": "pawit@eql.com.au",
        "password": "pawit",
        "role": "user",
    })
    assert resp.status_code == 302
    assert "error" in resp.headers["Location"]

def test_delete_user_redirects_on_success(client):
    resp = client.post("/users/bbbb-2222/delete")
    assert resp.status_code == 302
    assert "success" in resp.headers["Location"]

def test_update_role_redirects_on_success(client):
    resp = client.post("/users/bbbb-2222/role", data={"role": "asset_owner"})
    assert resp.status_code == 302
    assert "success" in resp.headers["Location"]

def test_update_role_invalid_role_defaults_to_user(pg, client):
    resp = client.post("/users/bbbb-2222/role", data={"role": "superadmin"})
    assert resp.status_code == 302
    # pg.cur.execute.assert_called_once()
    assert pg.cur.execute.call_count == 2
    assert pg.cur.execute.call_args[0][1][0] == "user"
    

def test_update_user_duplicate_email_shows_error(pg, client):
    pg.cur.execute.side_effect = psycopg2.errors.UniqueViolation("duplicate")
    resp = client.post("/users/5/update", data={
        "full_name": "Pawit Singh",
        "email": "pawit@eql.com.au",
        "password": "pawit",
        "role": "user",
    })
    assert resp.status_code == 302
    assert "error" in resp.headers["Location"]    
    
def test_update_user_redirects_on_success(client):
    resp = client.post("/users/bbbb-2222/update", data={
                "full_name": "Pawit Test",
                "email": "pawit@eql.com.au",
                "role": "user",
            })
    assert resp.status_code == 302
    assert "success" in resp.headers["Location"]
    
def test_update_user_assets_success(client):
    resp = client.post("/users/bbbb-2222/assets/add", data={
        "site_id": "b2b2b2-1234"
    })
    
    assert resp.status_code == 200
    
    resp = client.post("/users/bbbb-2222/assets/remove", data={
        "site_id": "b2b2b2-1234"
    })
    
    assert resp.status_code == 200
    
def test_update_user_assets_bad_request(client):
    resp = client.post("/users/bbbb-2222/assets/add", data={
        "site_id": ""
    })
    
    assert resp.status_code == 400
