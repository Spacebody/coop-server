"""测试 mDNS 客户端的地址过滤和优先级排序。"""
import pytest

from cli.discovery import _address_priority, _pick_best_address


class TestAddressPriority:
    def test_lan_192_preferred(self):
        # 192.168.x.x 应该最优先
        assert _address_priority("192.168.1.42") == 0
        assert _address_priority("192.168.50.252") == 0

    def test_lan_10_x_x_x(self):
        assert _address_priority("10.0.0.5") == 1
        assert _address_priority("10.255.0.1") == 1

    def test_lan_172_16_to_31(self):
        assert _address_priority("172.16.0.1") == 2
        assert _address_priority("172.31.255.1") == 2
        # 172.32.x.x 不在 RFC 1918 范围内, 不应该被识别为 LAN
        assert _address_priority("172.32.0.1") != 2

    def test_198_18_filtered(self):
        # 这是 ClashX/Surge 透明代理常用的网段, 必须降权
        assert _address_priority("198.18.0.1") == 100
        assert _address_priority("198.19.0.1") == 100

    def test_100_64_filtered(self):
        # CGNAT/Tailscale
        assert _address_priority("100.64.0.1") == 100
        assert _address_priority("100.127.255.255") == 100

    def test_169_254_filtered(self):
        # link-local
        assert _address_priority("169.254.1.1") == 100

    def test_loopback(self):
        assert _address_priority("127.0.0.1") == 50

    def test_invalid_address(self):
        assert _address_priority("not-an-ip") == 999


class TestPickBestAddress:
    def test_prefers_lan_over_198_18(self):
        # 模拟用户场景: server 同时广播了真实 LAN IP 和 ClashX 的 fake-ip
        addrs = ["198.18.0.1", "192.168.50.252"]
        assert _pick_best_address(addrs) == "192.168.50.252"

    def test_prefers_lan_over_loopback(self):
        addrs = ["127.0.0.1", "10.0.0.5"]
        assert _pick_best_address(addrs) == "10.0.0.5"

    def test_only_one_address(self):
        # 只有一个候选时直接返回它
        assert _pick_best_address(["192.168.1.1"]) == "192.168.1.1"
        assert _pick_best_address(["198.18.0.1"]) == "198.18.0.1"

    def test_empty_list_raises(self):
        with pytest.raises(ValueError):
            _pick_best_address([])

    def test_multiple_lans_picks_192(self):
        # 192.168 优先于 10.0
        addrs = ["10.0.0.5", "192.168.1.1"]
        assert _pick_best_address(addrs) == "192.168.1.1"

    def test_real_world_scenario(self):
        # 用户的真实场景: 用 ClashX 的 Mac 上 server 广播了多个 IP
        addrs = ["198.18.0.1", "192.168.50.252", "169.254.123.45"]
        assert _pick_best_address(addrs) == "192.168.50.252"
