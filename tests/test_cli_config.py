"""cli/config.py 配置加载测试 (含客户端配置文件)。"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from cli.config import (
    DEFAULT_TOKEN_PATHS,
    ENV_HOST,
    ENV_PORT,
    ENV_TOKEN_FILE,
    ClientConfig,
    CoordinatorAddr,
    describe_token_lookup,
    load_client_config,
    load_token,
    resolve_coordinator,
)


# ===========================================================
# 客户端配置文件加载
# ===========================================================

class TestLoadClientConfig:
    def test_no_config_file_returns_empty(self, tmp_path, monkeypatch):
        """没有配置文件时返回空 ClientConfig。"""
        monkeypatch.setattr(
            "cli.config._client_config_paths",
            lambda: [tmp_path / "nonexistent.yaml"],
        )
        cfg = load_client_config()
        assert cfg.token_file is None
        assert cfg.coordinator.host is None
        assert cfg.source_path is None

    def test_load_full_config(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "client.yaml"
        cfg_path.write_text("""
token_file: "/some/path/token"
coordinator:
  host: "coop.lan"
  port: 8888
workspace_dir: "~/coop"
""")
        monkeypatch.setattr("cli.config._client_config_paths", lambda: [cfg_path])

        cfg = load_client_config()
        assert cfg.token_file == "/some/path/token"
        assert cfg.coordinator.host == "coop.lan"
        assert cfg.coordinator.port == 8888
        assert cfg.workspace_dir == "~/coop"
        assert cfg.source_path == str(cfg_path)

    def test_partial_config(self, tmp_path, monkeypatch):
        """配置文件只填一部分字段也能工作。"""
        cfg_path = tmp_path / "client.yaml"
        cfg_path.write_text('token_file: "/path/token"\n')
        monkeypatch.setattr("cli.config._client_config_paths", lambda: [cfg_path])

        cfg = load_client_config()
        assert cfg.token_file == "/path/token"
        assert cfg.coordinator.host is None
        assert cfg.coordinator.port == 7777

    def test_invalid_yaml_raises(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "client.yaml"
        cfg_path.write_text("not a mapping just a string")
        monkeypatch.setattr("cli.config._client_config_paths", lambda: [cfg_path])

        with pytest.raises(RuntimeError, match="mapping"):
            load_client_config()

    def test_priority_first_existing(self, tmp_path, monkeypatch):
        """配置文件按列表顺序找,先找到的优先。"""
        first = tmp_path / "first.yaml"
        second = tmp_path / "second.yaml"
        first.write_text('token_file: "/from-first"\n')
        second.write_text('token_file: "/from-second"\n')
        monkeypatch.setattr(
            "cli.config._client_config_paths", lambda: [first, second]
        )

        cfg = load_client_config()
        assert cfg.token_file == "/from-first"

    def test_coop_prefix_takes_priority(self, tmp_path, monkeypatch):
        """COOP_PREFIX/client/client.yaml 优先于 ~/.coop/client.yaml。"""
        # 模拟 PREFIX 部署目录
        prefix = tmp_path / "coop-server"
        prefix_client_dir = prefix / "client"
        prefix_client_dir.mkdir(parents=True)
        (prefix_client_dir / "client.yaml").write_text(
            'token_file: "/from-prefix"\n'
        )

        # 同时也"假装"存在 ~/.coop/client.yaml,但不应该被读到
        home = tmp_path / "fake-home"
        coop_dir = home / ".coop"
        coop_dir.mkdir(parents=True)
        (coop_dir / "client.yaml").write_text(
            'token_file: "/from-home"\n'
        )

        monkeypatch.setenv("COOP_PREFIX", str(prefix))
        monkeypatch.setenv("HOME", str(home))

        cfg = load_client_config()
        # 应该读到 prefix 那个,不是 home 那个
        assert cfg.token_file == "/from-prefix"
        assert cfg.source_path == str(prefix_client_dir / "client.yaml")

    def test_auto_detect_prefix_from_executable(self, tmp_path, monkeypatch):
        """CLI 装在 $PREFIX/.venv/bin/python 时,自动反推出 PREFIX。"""
        # 模拟一个 PREFIX 目录结构
        prefix = tmp_path / "fake-deploy"
        venv_bin = prefix / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        # 假的 python 文件
        fake_python = venv_bin / "python"
        fake_python.touch()

        # 必须有 client/ 子目录才能被识别为 coop PREFIX
        client_dir = prefix / "client"
        client_dir.mkdir()
        (client_dir / "client.yaml").write_text(
            'token_file: "/from-auto-detected"\n'
        )

        # mock sys.executable 指向假的 python
        monkeypatch.setattr("sys.executable", str(fake_python))
        # 确保没有环境变量干扰
        monkeypatch.delenv("COOP_PREFIX", raising=False)
        # 用 tmp_path 当 home 避免污染
        monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))

        cfg = load_client_config()
        assert cfg.token_file == "/from-auto-detected"

    def test_no_auto_detect_when_no_client_dir(
        self, tmp_path, monkeypatch
    ):
        """sys.executable 路径正确但没 client/ 目录时,不应该误判。"""
        random_venv = tmp_path / "some" / ".venv" / "bin"
        random_venv.mkdir(parents=True)
        fake_python = random_venv / "python"
        fake_python.touch()
        # 注意没有 tmp_path/some/client/

        monkeypatch.setattr("sys.executable", str(fake_python))
        monkeypatch.delenv("COOP_PREFIX", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))

        # 应该退化到 ~/.coop, 但那里也没文件,返回空
        cfg = load_client_config()
        assert cfg.token_file is None

    def test_auto_detect_worker_layout(self, tmp_path, monkeypatch):
        """worker 端布局: $PREFIX/client.yaml (没有 client/ 子目录)。"""
        prefix = tmp_path / "worker-deploy"
        venv_bin = prefix / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        fake_python = venv_bin / "python"
        fake_python.touch()

        # 注意是 $PREFIX/client.yaml,不是 $PREFIX/client/client.yaml
        (prefix / "client.yaml").write_text(
            'token_file: "/worker-token"\n'
            'coordinator:\n'
            '  host: "192.168.1.42"\n'
        )

        monkeypatch.setattr("sys.executable", str(fake_python))
        monkeypatch.delenv("COOP_PREFIX", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))

        cfg = load_client_config()
        assert cfg.token_file == "/worker-token"
        assert cfg.coordinator.host == "192.168.1.42"

    def test_explicit_path_overrides_all(self, tmp_path, monkeypatch):
        """显式传入路径,绕过所有默认查找。"""
        # 在不同位置都放假配置 (检测显式路径不会被它们影响)
        prefix = tmp_path / "prefix-deploy"
        (prefix / "client").mkdir(parents=True)
        (prefix / "client" / "client.yaml").write_text(
            'token_file: "/from-prefix"\n'
        )
        monkeypatch.setenv("COOP_PREFIX", str(prefix))

        explicit = tmp_path / "my-custom-config.yaml"
        explicit.write_text('token_file: "/from-explicit"\n')

        cfg = load_client_config(explicit_path=explicit)
        assert cfg.token_file == "/from-explicit"

    def test_explicit_path_not_found_raises(self, tmp_path):
        """显式路径不存在时抛错,不静默降级。"""
        with pytest.raises(RuntimeError, match="不存在"):
            load_client_config(explicit_path=tmp_path / "nope.yaml")

    def test_env_config_used(self, tmp_path, monkeypatch):
        """COOP_CONFIG 环境变量指定配置文件。"""
        cfg_file = tmp_path / "env-cfg.yaml"
        cfg_file.write_text('token_file: "/from-env-config"\n')

        monkeypatch.setenv("COOP_CONFIG", str(cfg_file))
        monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
        # PREFIX 也设了, 看 COOP_CONFIG 是否赢
        prefix = tmp_path / "prefix2"
        (prefix / "client").mkdir(parents=True)
        (prefix / "client" / "client.yaml").write_text(
            'token_file: "/from-prefix"\n'
        )
        monkeypatch.setenv("COOP_PREFIX", str(prefix))

        cfg = load_client_config()
        assert cfg.token_file == "/from-env-config"

    def test_env_config_not_found_raises(self, tmp_path, monkeypatch):
        """COOP_CONFIG 指向不存在的文件,抛错。"""
        monkeypatch.setenv("COOP_CONFIG", str(tmp_path / "nope.yaml"))
        with pytest.raises(RuntimeError, match="不存在"):
            load_client_config()

    def test_explicit_overrides_env_config(self, tmp_path, monkeypatch):
        """显式参数优先级高于 COOP_CONFIG 环境变量。"""
        env_cfg = tmp_path / "env.yaml"
        env_cfg.write_text('token_file: "/from-env"\n')
        explicit_cfg = tmp_path / "explicit.yaml"
        explicit_cfg.write_text('token_file: "/from-explicit"\n')

        monkeypatch.setenv("COOP_CONFIG", str(env_cfg))
        cfg = load_client_config(explicit_path=explicit_cfg)
        assert cfg.token_file == "/from-explicit"


# ===========================================================
# Token 加载 (整合所有来源)
# ===========================================================

class TestLoadToken:
    @pytest.fixture(autouse=True)
    def _no_default_paths(self, monkeypatch):
        """每个测试默认禁用 DEFAULT_TOKEN_PATHS,避免污染。"""
        monkeypatch.setattr("cli.config.DEFAULT_TOKEN_PATHS", [])
        monkeypatch.delenv(ENV_TOKEN_FILE, raising=False)
        # 也禁用配置文件查找
        monkeypatch.setattr("cli.config._client_config_paths", lambda: [])

    def test_explicit_path_priority_1(self, tmp_path):
        token_file = tmp_path / "token"
        token_file.write_text("explicit")
        result = load_token(str(token_file))
        assert result == "explicit"

    def test_env_priority_2(self, tmp_path, monkeypatch):
        token_file = tmp_path / "env-token"
        token_file.write_text("from-env")
        monkeypatch.setenv(ENV_TOKEN_FILE, str(token_file))

        result = load_token()
        assert result == "from-env"

    def test_config_file_priority_3(self, tmp_path):
        """没有命令行/环境变量时用配置文件。"""
        token_file = tmp_path / "cfg-token"
        token_file.write_text("from-config")
        cfg = ClientConfig(token_file=str(token_file))

        result = load_token(config=cfg)
        assert result == "from-config"

    def test_default_paths_priority_4(self, tmp_path, monkeypatch):
        """命令行/环境变量/配置文件都没有时 fallback 到默认路径。"""
        token_file = tmp_path / "default"
        token_file.write_text("from-default")
        monkeypatch.setattr(
            "cli.config.DEFAULT_TOKEN_PATHS", [token_file]
        )
        cfg = ClientConfig()  # 空配置

        result = load_token(config=cfg)
        assert result == "from-default"

    def test_explicit_overrides_all(self, tmp_path, monkeypatch):
        """命令行参数覆盖一切。"""
        env_file = tmp_path / "env"
        env_file.write_text("env-value")
        cfg_file = tmp_path / "cfg"
        cfg_file.write_text("cfg-value")
        cli_file = tmp_path / "cli"
        cli_file.write_text("cli-value")

        monkeypatch.setenv(ENV_TOKEN_FILE, str(env_file))
        cfg = ClientConfig(token_file=str(cfg_file))

        result = load_token(token_file=str(cli_file), config=cfg)
        assert result == "cli-value"

    def test_env_overrides_config(self, tmp_path, monkeypatch):
        env_file = tmp_path / "env"
        env_file.write_text("env-value")
        cfg_file = tmp_path / "cfg"
        cfg_file.write_text("cfg-value")

        monkeypatch.setenv(ENV_TOKEN_FILE, str(env_file))
        cfg = ClientConfig(token_file=str(cfg_file))

        result = load_token(config=cfg)
        assert result == "env-value"

    def test_config_file_path_with_tilde(self, tmp_path, monkeypatch):
        """配置文件里 ~ 应该被展开。"""
        monkeypatch.setenv("HOME", str(tmp_path))
        token_file = tmp_path / "home-token"
        token_file.write_text("expanded")

        cfg = ClientConfig(token_file="~/home-token")
        result = load_token(config=cfg)
        assert result == "expanded"

    def test_symlink_followed(self, tmp_path):
        real = tmp_path / "real"
        real.write_text("via-symlink")
        link = tmp_path / "link"
        link.symlink_to(real)

        result = load_token(str(link))
        assert result == "via-symlink"


# ===========================================================
# 协调者地址解析
# ===========================================================

class TestResolveCoordinator:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        monkeypatch.delenv(ENV_HOST, raising=False)
        monkeypatch.delenv(ENV_PORT, raising=False)

    def test_explicit_priority(self):
        cfg = ClientConfig(coordinator=CoordinatorAddr(host="cfg-host", port=8888))
        host, port = resolve_coordinator(host="cli-host", port=9999, config=cfg)
        assert host == "cli-host"
        assert port == 9999

    def test_env_priority_over_config(self, monkeypatch):
        cfg = ClientConfig(coordinator=CoordinatorAddr(host="cfg-host", port=8888))
        monkeypatch.setenv(ENV_HOST, "env-host")
        monkeypatch.setenv(ENV_PORT, "9999")

        host, port = resolve_coordinator(config=cfg)
        assert host == "env-host"
        assert port == 9999

    def test_config_used_when_others_absent(self):
        cfg = ClientConfig(coordinator=CoordinatorAddr(host="cfg-host", port=8888))
        host, port = resolve_coordinator(config=cfg)
        assert host == "cfg-host"
        assert port == 8888

    def test_no_config_returns_none_host(self):
        cfg = ClientConfig()
        host, port = resolve_coordinator(config=cfg)
        assert host is None
        assert port == 7777  # 默认端口

    def test_partial_cli_args(self):
        """只传 host 不传 port。"""
        cfg = ClientConfig(coordinator=CoordinatorAddr(host="cfg-host", port=8888))
        host, port = resolve_coordinator(host="cli-host", config=cfg)
        assert host == "cli-host"
        assert port == 8888  # 来自 config


# ===========================================================
# 描述函数
# ===========================================================

class TestDescribe:
    def test_token_lookup_with_config(self, tmp_path):
        token_file = tmp_path / "tok"
        token_file.write_text("x")
        cfg = ClientConfig(
            token_file=str(token_file),
            source_path="/test/client.yaml",
        )
        desc = describe_token_lookup(cfg)
        assert "/test/client.yaml" in desc
        assert str(token_file) in desc
        assert "存在" in desc

    def test_token_lookup_without_config(self):
        cfg = ClientConfig()
        desc = describe_token_lookup(cfg)
        assert "未指定" in desc
