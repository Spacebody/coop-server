"""token 鉴权测试。"""
from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

import pytest

from coop_server.auth import load_or_generate_token


class TestTokenManagement:
    def test_generate_new_token(self, tmp_path):
        token_file = tmp_path / "token"
        assert not token_file.exists()

        token = load_or_generate_token(str(token_file), token_length=32)
        assert len(token) == 32
        assert all(c in "0123456789abcdef" for c in token)
        assert token_file.exists()
        # 文件内容应该和返回值一样
        assert token_file.read_text().strip() == token

    def test_reuse_existing_token(self, tmp_path):
        token_file = tmp_path / "token"
        # 第一次生成
        token1 = load_or_generate_token(str(token_file))
        # 第二次应该读到同一个
        token2 = load_or_generate_token(str(token_file))
        assert token1 == token2

    def test_file_permission_0600(self, tmp_path):
        token_file = tmp_path / "token"
        load_or_generate_token(str(token_file))

        # 在 Linux/macOS 检查权限位
        if os.name == "posix":
            mode = token_file.stat().st_mode
            # 只允许 owner 读写
            assert stat.S_IMODE(mode) == 0o600

    def test_empty_token_file_rejected(self, tmp_path):
        token_file = tmp_path / "token"
        token_file.write_text("")
        with pytest.raises(ValueError, match="为空"):
            load_or_generate_token(str(token_file))

    def test_short_token_rejected(self, tmp_path):
        token_file = tmp_path / "token"
        token_file.write_text("short")
        with pytest.raises(ValueError, match="太短"):
            load_or_generate_token(str(token_file))

    def test_creates_parent_dir(self, tmp_path):
        """父目录不存在时自动创建。"""
        token_file = tmp_path / "subdir" / "another" / "token"
        token = load_or_generate_token(str(token_file))
        assert token_file.exists()
        assert token


class TestBearerAuthMiddleware:
    """中间件需要在 ASGI app 上下文里测,用 starlette 的 TestClient。"""

    def _make_app(self, token: str):
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        from coop_server.auth import BearerAuthMiddleware

        async def homepage(request):
            return JSONResponse({"hello": "world"})

        async def health(request):
            return JSONResponse({"status": "ok"})

        app = Starlette(
            routes=[
                Route("/api", homepage),
                Route("/health", health),
            ]
        )
        app.add_middleware(BearerAuthMiddleware, expected_token=token)
        return app

    def test_health_path_skips_auth(self):
        from starlette.testclient import TestClient
        app = self._make_app("secret-token-1234")
        client = TestClient(app)
        r = client.get("/health")
        assert r.status_code == 200

    def test_missing_token_rejected(self):
        from starlette.testclient import TestClient
        app = self._make_app("secret-token-1234")
        client = TestClient(app)
        r = client.get("/api")
        assert r.status_code == 401
        assert "Authorization" in r.json()["error"] or "Bearer" in r.json()["error"]

    def test_wrong_token_rejected(self):
        from starlette.testclient import TestClient
        app = self._make_app("secret-token-1234")
        client = TestClient(app)
        r = client.get("/api", headers={"Authorization": "Bearer wrong-token"})
        assert r.status_code == 403

    def test_correct_token_accepted(self):
        from starlette.testclient import TestClient
        app = self._make_app("secret-token-1234")
        client = TestClient(app)
        r = client.get("/api", headers={"Authorization": "Bearer secret-token-1234"})
        assert r.status_code == 200
        assert r.json() == {"hello": "world"}

    def test_malformed_authorization_rejected(self):
        from starlette.testclient import TestClient
        app = self._make_app("secret-token-1234")
        client = TestClient(app)
        # 不带 Bearer 前缀
        r = client.get("/api", headers={"Authorization": "secret-token-1234"})
        assert r.status_code == 401

    def test_constant_time_compare(self):
        """用 secrets.compare_digest 防止 timing attack。这里只验证逻辑用对了。"""
        from coop_server.auth import BearerAuthMiddleware
        import inspect
        src = inspect.getsource(BearerAuthMiddleware.dispatch)
        assert "compare_digest" in src
