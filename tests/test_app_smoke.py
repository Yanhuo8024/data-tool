"""app 主链路冒烟测试：应用装配、公开端点、鉴权保护"""
import pytest


@pytest.fixture(scope="module")
def client():
    from app import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


class TestAppAssembly:
    """应用装配完整性"""

    def test_routes_registered(self, app):
        # 依赖 conftest 的 session-scope `app` fixture，确保所有 lazy 路由
        # 在后台线程跑完后再检查（routes 分阶段加载机制）。
        rules = {r.rule for r in app.url_map.iter_rules()}
        assert len(rules) > 200, f"仅 {len(rules)} 个路由注册（后台 lazy 路由可能还没完成）"
        assert "/api/version" in rules
        assert "/api/monitoring/health" in rules
        assert "/api/assistant/chat" in rules
        assert "/api/did/full-analysis" in rules
        assert "/api/ml/train" in rules
        assert "/api/nlq/query" in rules

    def test_index_serves_spa(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"<html" in resp.data.lower()

    def test_version_endpoint(self, client):
        resp = client.get("/api/version")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True and data["version"]

    def test_health_endpoint(self, client):
        resp = client.get("/api/monitoring/health")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_openapi_spec(self, client):
        resp = client.get("/api/openapi.json")
        assert resp.status_code == 200
        spec = resp.get_json()
        assert spec.get("openapi") and spec.get("paths")


class TestAuthContract:
    """鉴权契约：本地优先设计下
    - optional_auth 数据端点：匿名可达业务逻辑；带无效 API Key 必须 401；
    - 治理端点 require_auth_or_api_key_local：本地匿名回退 admin，无效 Key 401。
    """

    @pytest.mark.parametrize("path", [
        "/api/describe",
        "/api/ml/train",
        "/api/scheduler/tasks",
    ])
    def test_invalid_api_key_rejected(self, client, path):
        resp = client.post(path, json={}, headers={"X-API-Key": "invalid-key-xxx"})
        assert resp.status_code == 401, f"{path} 未拒绝无效 API Key: {resp.status_code}"

    def test_invalid_api_key_rejected_get(self, client):
        resp = client.get("/api/files", headers={"X-API-Key": "invalid-key-xxx"})
        assert resp.status_code == 401

    def test_local_anonymous_reaches_business_logic(self, client):
        # 本地匿名请求：治理端点回退 admin，数据端点匿名放行 —— 都应到达业务层（非 5xx/401）
        for path in ("/api/describe", "/api/scheduler/tasks"):
            resp = client.post(path, json={})
            assert resp.status_code < 500, f"{path} 服务端错误: {resp.status_code}"
            assert resp.status_code not in (401, 403), f"{path} 本地匿名被拒: {resp.status_code}"

    def test_login_rejects_bad_credentials(self, client):
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "definitely-wrong"})
        assert resp.status_code in (401, 403, 423)  # 401 拒绝 / 423 锁定
