"""config 模块测试。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from coop_server.config import (
    Config,
    HeartbeatConfig,
    ServerConfig,
    load_config,
)


class TestConfigValidation:
    def test_default_config_is_valid(self):
        c = Config()
        c.validate()  # 不抛异常

    def test_invalid_port(self):
        c = ServerConfig(port=0)
        with pytest.raises(ValueError, match="port"):
            c.validate()

    def test_invalid_transport(self):
        c = ServerConfig(transport="websocket")
        with pytest.raises(ValueError, match="transport"):
            c.validate()

    def test_heartbeat_timeout_must_exceed_interval(self):
        c = HeartbeatConfig(worker_interval_sec=60, timeout_sec=30)
        with pytest.raises(ValueError, match="timeout_sec"):
            c.validate()


class TestLoadConfig:
    def test_load_minimal_yaml(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write("server:\n  port: 8888\n")
            path = f.name

        try:
            c = load_config(path)
            assert c.server.port == 8888
            # 其他用默认
            assert c.server.host == "0.0.0.0"
        finally:
            os.unlink(path)

    def test_load_empty_yaml(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write("")
            path = f.name

        try:
            c = load_config(path)
            # 全部默认值
            assert c.server.port == 7777
        finally:
            os.unlink(path)

    def test_load_unknown_field_rejected(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write("server:\n  unknown_field: 123\n")
            path = f.name

        try:
            with pytest.raises(ValueError, match="未知字段"):
                load_config(path)
        finally:
            os.unlink(path)

    def test_load_missing_file(self):
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/config.yaml")

    def test_load_full_yaml(self):
        content = """
server:
  host: "127.0.0.1"
  port: 9999
  transport: "sse"

auth:
  enabled: false
  token_file: "/tmp/token"

database:
  path: "/tmp/coop.db"

heartbeat:
  worker_interval_sec: 10
  timeout_sec: 60
  check_interval_sec: 15

logging:
  level: "DEBUG"
  file: "/tmp/coop.log"
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write(content)
            path = f.name

        try:
            c = load_config(path)
            assert c.server.host == "127.0.0.1"
            assert c.server.port == 9999
            assert c.server.transport == "sse"
            assert c.auth.enabled is False
            assert c.heartbeat.worker_interval_sec == 10
            assert c.logging.level == "DEBUG"
        finally:
            os.unlink(path)
