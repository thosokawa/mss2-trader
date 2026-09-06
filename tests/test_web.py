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


def test_strategy_crud(client):
    import re

    client.post("/symbol-sets", data={"name": "戦略用セット", "note": ""})
    r = client.get("/symbol-sets")
    set_id = max(int(x) for x in re.findall(r"/symbol-sets/(\d+)\"", r.text))

    client.post(
        "/strategies",
        data={
            "name": "SMAテスト戦略",
            "class_path": "app.strategy.examples.sma_cross:SmaCross",
            "symbol_set_id": str(set_id),
            "timeframe": "5m",
            "params_json": '{"fast": 5, "slow": 20, "qty": 100}',
            "mode": "notify",
        },
        follow_redirects=True,
    )
    r = client.get("/strategies")
    assert "SMAテスト戦略" in r.text
    assert "停止" in r.text  # 既定は無効

    strategy_id = max(int(x) for x in re.findall(r"/strategies/(\d+)/toggle", r.text))
    r = client.post(f"/strategies/{strategy_id}/toggle", follow_redirects=True)
    assert "稼働中" in r.text

    r = client.post(f"/strategies/{strategy_id}/delete", follow_redirects=True)
    assert "SMAテスト戦略" not in r.text


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_ingest(client):
    r = client.post("/api/ingest", json={"quotes": [{"code": "7203", "price": 2810.0, "volume": 100}]})
    assert r.json()["received"] == 1
