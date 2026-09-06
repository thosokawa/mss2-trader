import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_dashboard_ok(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "ダッシュボード" in r.text


def test_symbol_set_crud(client):
    client.post("/symbol-sets", data={"name": "テストセット", "note": ""})
    r = client.get("/symbol-sets")
    assert "テストセット" in r.text
    # 直近で作られたセットの詳細に銘柄追加
    import re

    set_id = max(int(x) for x in re.findall(r"/symbol-sets/(\d+)\"", r.text))
    r = client.post(
        f"/symbol-sets/{set_id}/items",
        data={"code": "7203", "name": "トヨタ自動車"},
        follow_redirects=True,
    )
    assert "7203" in r.text and "トヨタ自動車" in r.text


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_ingest(client):
    r = client.post("/api/ingest", json={"quotes": [{"code": "7203", "price": 2810.0, "volume": 100}]})
    assert r.json()["received"] == 1
