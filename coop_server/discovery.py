"""mDNS / Bonjour 服务广播。

让 worker 不需要手动配置 IP 就能发现Coop Server 地址。

广播服务类型: _coop._tcp.local.
"""
from __future__ import annotations

import logging
import socket
from typing import Any

from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from .config import DiscoveryConfig

logger = logging.getLogger(__name__)


class MDNSAdvertiser:
    """协调者机器在局域网广播自己的服务地址。"""

    def __init__(
        self,
        config: DiscoveryConfig,
        port: int,
        properties: dict[str, str] | None = None,
    ) -> None:
        self._config = config
        self._port = port
        self._properties = properties or {}
        self._zeroconf: AsyncZeroconf | None = None
        self._service_info: ServiceInfo | None = None

    def _detect_advertise_ip(self) -> str:
        """探测本机 IP。

        优先级:
            1. 配置文件 discovery.advertise_host (用户显式指定)
            2. 枚举所有非 loopback 网络接口,挑 RFC1918 私有地址,
               过滤掉虚拟/NAT 接口 (Docker, VPN 的 198.18.x 等)
            3. UDP socket connect 8.8.8.8 探测 (fallback,可能不准)
            4. 127.0.0.1 (兜底)
        """
        if self._config.advertise_host:
            return self._config.advertise_host

        # 方法 1: 枚举所有接口找 RFC1918 地址
        ip = self._find_lan_ip()
        if ip:
            return ip

        # 方法 2: UDP 探测 (可能被 Docker/VPN 干扰)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        except OSError:
            ip = "127.0.0.1"
        finally:
            s.close()
        return ip

    @staticmethod
    def _find_lan_ip() -> str | None:
        """枚举本机网络接口,返回第一个看起来像真实 LAN 的 IPv4 地址。

        优先 192.168.x.x, 然后 10.x.x.x, 最后 172.16-31.x.x。
        过滤掉:
        - 127.0.0.0/8     loopback
        - 169.254.0.0/16  link-local
        - 198.18.0.0/15   benchmark testing (Docker Desktop / 一些 VPN 用)
        - 100.64.0.0/10   carrier-grade NAT (一些 VPN 用)
        - 224.0.0.0/4     multicast
        """
        try:
            # macOS / Linux 都支持
            import ipaddress
            import subprocess
            result = subprocess.run(
                ["ifconfig"], capture_output=True, text=True, timeout=2
            )
            if result.returncode != 0:
                return None
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None

        # 解析 ifconfig 输出, 拣出所有 'inet x.x.x.x' 行的 IP
        candidates: list[str] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line.startswith("inet "):
                continue
            # 形如 "inet 192.168.50.252 netmask 0xffffff00 ..."
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                ip_obj = ipaddress.IPv4Address(parts[1])
            except (ValueError, ipaddress.AddressValueError):
                continue
            ip_str = str(ip_obj)

            # 黑名单
            if ip_obj.is_loopback:
                continue
            if ip_obj.is_link_local:
                continue
            if ip_obj.is_multicast:
                continue
            # 198.18.0.0/15 (RFC2544 benchmark - Docker Desktop on macOS 用这个)
            if ip_obj in ipaddress.IPv4Network("198.18.0.0/15"):
                continue
            # 100.64.0.0/10 (CGNAT - 一些 VPN 用)
            if ip_obj in ipaddress.IPv4Network("100.64.0.0/10"):
                continue

            # 必须是 RFC1918 私有地址 (真实 LAN)
            if ip_obj.is_private:
                candidates.append(ip_str)

        if not candidates:
            return None

        # 优先级: 192.168 > 10.x > 172.16-31
        def _priority(ip: str) -> int:
            if ip.startswith("192.168."):
                return 0
            if ip.startswith("10."):
                return 1
            return 2

        candidates.sort(key=_priority)
        return candidates[0]

    async def start(self) -> None:
        if not self._config.enabled:
            logger.info("mDNS 广播已禁用")
            return
        if self._zeroconf is not None:
            logger.warning("MDNSAdvertiser 已在运行")
            return

        ip = self._detect_advertise_ip()
        if self._config.advertise_host:
            logger.info(f"mDNS 广播 IP (来自配置): {ip}")
        else:
            logger.info(f"mDNS 广播 IP (自动探测): {ip}")
        instance_name = (
            f"{self._config.service_name}.{self._config.service_type}"
        )

        # zeroconf properties 必须是 bytes
        props = {k.encode(): str(v).encode() for k, v in self._properties.items()}

        self._service_info = ServiceInfo(
            type_=self._config.service_type,
            name=instance_name,
            addresses=[socket.inet_aton(ip)],
            port=self._port,
            properties=props,
            server=f"{self._config.service_name}.local.",
        )

        self._zeroconf = AsyncZeroconf(ip_version=IPVersion.V4Only)
        await self._zeroconf.async_register_service(self._service_info)
        logger.info(
            f"mDNS 广播已启动: {self._config.service_name}.{self._config.service_type} "
            f"-> {ip}:{self._port}"
        )

    async def stop(self) -> None:
        if self._zeroconf is None:
            return
        try:
            if self._service_info:
                await self._zeroconf.async_unregister_service(self._service_info)
            await self._zeroconf.async_close()
        except Exception:
            logger.exception("mDNS 关闭出错")
        finally:
            self._zeroconf = None
            self._service_info = None
        logger.info("mDNS 广播已停止")
