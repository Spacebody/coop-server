"""客户端发现协调者 (mDNS 浏览)。"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any

import httpx
from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf

SERVICE_TYPE = "_coop._tcp.local."


def _address_priority(addr: str) -> int:
    """给 mDNS 返回的地址打分,小的优先。

    避免选到代理软件 / VPN 创建的虚拟接口 IP。
    """
    try:
        ip = ipaddress.IPv4Address(addr)
    except (ValueError, ipaddress.AddressValueError):
        return 999  # 解析失败排最后

    # 1. 常见 LAN 网段最优先 (用户网络一般是这些)
    if ip in ipaddress.IPv4Network("192.168.0.0/16"):
        return 0
    if ip in ipaddress.IPv4Network("10.0.0.0/8"):
        return 1
    if ip in ipaddress.IPv4Network("172.16.0.0/12"):
        return 2

    # 2. 已知"几乎肯定不是真实 LAN"的特殊段 (排在最后)
    #    198.18.0.0/15: RFC 2544 网络性能测试 (常见于 ClashX/Surge 透明代理)
    #    100.64.0.0/10: RFC 6598 运营商 NAT (Tailscale 也用这个)
    #    169.254.0.0/16: link-local (DHCP 失败时的自动地址)
    if ip in ipaddress.IPv4Network("198.18.0.0/15"):
        return 100
    if ip in ipaddress.IPv4Network("100.64.0.0/10"):
        return 100
    if ip in ipaddress.IPv4Network("169.254.0.0/16"):
        return 100

    # 3. loopback (127.0.0.1) - 跨机器没用,但本机调试可能有效
    if ip.is_loopback:
        return 50

    # 4. 公网 IP (理论上不应该出现在 mDNS, 但 fallback 处理)
    return 10


def _pick_best_address(addresses: list[str]) -> str:
    """从一组 mDNS 地址里挑最可能是真实局域网 IP 的那个。"""
    if not addresses:
        raise ValueError("空地址列表")
    return min(addresses, key=_address_priority)


async def discover_coordinator(
    timeout_sec: float = 5,
    service_type: str = SERVICE_TYPE,
    collect_all: bool = False,
) -> dict[str, Any] | None:
    """通过 mDNS 在局域网搜索协调者。

    多个地址时优先选普通 LAN 网段 (192.168/10/172.16) 的, 避免选到代理/VPN
    虚拟接口创建的 fake-ip。

    Args:
        timeout_sec: 等待时间
        collect_all: True 时等满 timeout 收集所有响应者; False (默认) 找到一个就返回。
                     用 True 时调用方可以拿到 'all_servers' 字段查看所有 server,
                     便于检测局域网里跑了多个 coop server 的异常情况。

    Returns:
        {"name", "host", "port", "all_addresses", "properties",
         "all_servers": [...] (仅 collect_all=True)} 或 None
    """
    found: dict[str, Any] = {}
    all_servers: list[dict[str, Any]] = []
    found_event = asyncio.Event()

    def handler(zeroconf, service_type, name, state_change):
        if state_change != ServiceStateChange.Added:
            return
        # 在异步上下文里查询服务详情
        async def _resolve():
            info = await aiozc.async_get_service_info(service_type, name)
            if info is None:
                return
            addresses = [
                socket.inet_ntoa(addr) for addr in info.addresses
            ]
            if not addresses:
                return
            properties = {}
            for k, v in (info.properties or {}).items():
                try:
                    properties[k.decode()] = v.decode() if v else ""
                except (AttributeError, UnicodeDecodeError):
                    pass
            entry = {
                "name": name,
                "host": _pick_best_address(addresses),
                "port": info.port,
                "all_addresses": addresses,
                "properties": properties,
            }
            all_servers.append(entry)
            if not found:
                found.update(entry)
            if not collect_all:
                found_event.set()

        asyncio.create_task(_resolve())

    aiozc = AsyncZeroconf(ip_version=IPVersion.V4Only)
    try:
        browser = AsyncServiceBrowser(
            aiozc.zeroconf, [service_type], handlers=[handler]
        )
        try:
            if collect_all:
                # 等满 timeout 收集所有响应
                await asyncio.sleep(timeout_sec)
            else:
                await asyncio.wait_for(found_event.wait(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            return None
        finally:
            await browser.async_cancel()
    finally:
        await aiozc.async_close()

    if not found:
        return None

    if collect_all:
        # 去重: 同一 server 可能被广播多次 (多接口), 按 (name, port) 去重
        seen = set()
        unique = []
        for s in all_servers:
            key = (s["name"], s["port"], s["host"])
            if key not in seen:
                seen.add(key)
                unique.append(s)
        found["all_servers"] = unique

    return found


async def ping_coordinator(base_url: str, token: str) -> tuple[bool, str]:
    """测试与 Coop Server 的连接。

    1. 先 ping /health (无需 token)
    2. 再用 MCP 调一个工具验证 token 有效

    Returns:
        (是否成功, 描述消息)
    """
    base_url = base_url.rstrip("/")

    # 1. health
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{base_url}/health")
            if r.status_code != 200:
                return False, f"/health 返回 {r.status_code}"
    except httpx.HTTPError as e:
        return False, f"/health 请求失败: {e}"

    # 2. MCP 调用测试 token
    from .mcp_call import call_tool
    try:
        result = await call_tool(
            f"{base_url}/mcp/", token, "list_workers", {}
        )
        if result.get("ok") is True:
            n = len(result.get("workers", []))
            return True, f"Coop Server 响应正常,当前 {n} 个 worker 在线"
        else:
            return False, f"MCP 调用失败: {result}"
    except Exception as e:
        return False, f"MCP 调用异常: {e}"
