"""mDNS 广播 IP 探测逻辑测试。

特别是确保不会选错 IP (如 Docker Desktop 的 198.18.0.1)。
"""
from __future__ import annotations

from unittest import mock

import pytest

from coop_server.discovery import MDNSAdvertiser


class TestFindLanIP:
    """_find_lan_ip 静态方法的纯逻辑测试 (不依赖真实网络)。"""

    def _patch_ifconfig(self, output: str):
        """返回一个 patch 上下文,让 subprocess.run('ifconfig') 返回指定输出。"""
        result = mock.MagicMock(returncode=0, stdout=output)
        return mock.patch("subprocess.run", return_value=result)

    def test_picks_192_168_over_198_18(self):
        """同时有真 LAN 和 Docker 虚拟 IP 时,选真 LAN。"""
        ifconfig = """
lo0: flags=8049 mtu 16384
        inet 127.0.0.1 netmask 0xff000000
en0: flags=8863 mtu 1500
        inet 192.168.50.252 netmask 0xffffff00 broadcast 192.168.50.255
utun5: flags=8051 mtu 1380
        inet 198.18.0.1 --> 198.18.0.1 netmask 0xfffffffc
"""
        with self._patch_ifconfig(ifconfig):
            assert MDNSAdvertiser._find_lan_ip() == "192.168.50.252"

    def test_no_lan_returns_none(self):
        """只有虚拟接口没有真 LAN 时,返回 None,让 caller fallback。"""
        ifconfig = """
lo0: flags=8049 mtu 16384
        inet 127.0.0.1 netmask 0xff000000
utun5: flags=8051 mtu 1380
        inet 198.18.0.1 netmask 0xfffffffc
"""
        with self._patch_ifconfig(ifconfig):
            assert MDNSAdvertiser._find_lan_ip() is None

    def test_priority_192_over_10_over_172(self):
        """优先级: 192.168 > 10.x > 172.16-31。"""
        # 模拟一台机器同时连了三种私有网段
        ifconfig = """
lo0: flags=8049 mtu 16384
        inet 127.0.0.1 netmask 0xff000000
en0: flags=8863 mtu 1500
        inet 10.0.5.42 netmask 0xff000000
en1: flags=8863 mtu 1500
        inet 172.16.0.5 netmask 0xfff00000
en2: flags=8863 mtu 1500
        inet 192.168.1.100 netmask 0xffffff00
"""
        with self._patch_ifconfig(ifconfig):
            # 应该选 192.168.x.x
            assert MDNSAdvertiser._find_lan_ip() == "192.168.1.100"

    def test_filters_link_local(self):
        """169.254.x.x 是自分配地址,不能用。"""
        ifconfig = """
lo0: flags=8049 mtu 16384
        inet 127.0.0.1 netmask 0xff000000
en0: flags=8863 mtu 1500
        inet 169.254.42.1 netmask 0xffff0000
"""
        with self._patch_ifconfig(ifconfig):
            assert MDNSAdvertiser._find_lan_ip() is None

    def test_filters_cgnat(self):
        """100.64.0.0/10 是运营商级 NAT, 一些 VPN (如 Tailscale) 用。"""
        ifconfig = """
lo0: flags=8049 mtu 16384
        inet 127.0.0.1 netmask 0xff000000
utun3: flags=8051 mtu 1380
        inet 100.64.10.5 netmask 0xffc00000
"""
        with self._patch_ifconfig(ifconfig):
            assert MDNSAdvertiser._find_lan_ip() is None

    def test_only_192_168(self):
        """只有真 LAN, 简单场景。"""
        ifconfig = """
en0: flags=8863 mtu 1500
        inet 192.168.1.42 netmask 0xffffff00
"""
        with self._patch_ifconfig(ifconfig):
            assert MDNSAdvertiser._find_lan_ip() == "192.168.1.42"

    def test_ifconfig_failure(self):
        """ifconfig 命令失败时,返回 None。"""
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            assert MDNSAdvertiser._find_lan_ip() is None

    def test_filters_198_18(self):
        """198.18.0.0/15 是基准测试保留段, Docker Desktop 用,过滤掉。"""
        ifconfig = """
lo0: flags=8049 mtu 16384
        inet 127.0.0.1 netmask 0xff000000
utun5: flags=8051 mtu 1380
        inet 198.18.0.1 netmask 0xfffffffc
utun6: flags=8051 mtu 1380
        inet 198.19.255.250 netmask 0xfffffffc
"""
        with self._patch_ifconfig(ifconfig):
            # 都被过滤,应返回 None
            assert MDNSAdvertiser._find_lan_ip() is None
