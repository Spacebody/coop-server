"""coop CLI - 命令行工具。

子命令:
  discover            扫描局域网内的 Coop Server
  doctor              诊断本机环境(Coop Server 地址、token、Claude Code 是否可用)
  ping                测试到 Coop Server 的连接
  setup-mcp           生成 .mcp.json 配置文件
  list-workers        通过 MCP 调用查看在线 worker
  list-tasks          通过 MCP 调用查看任务列表
  publish             派一个任务给指定 worker (协调者用,一次性)
  prune-workers       清理已失联的 worker 记录
  init-coordinator    在指定目录初始化协调者工作环境
  init-worker         在指定目录初始化 worker 工作环境
  init-client-config  生成客户端配置 (主要用于 worker 机器)

  smoke-test          一键端到端通信验证 (无需 Claude)
  test-clarification  测试 clarification 双向通信
  test-blocked        测试 blocked 反馈式派单
  simulate-worker     启动一个模拟 worker (无 Claude)
  simulate-coordinator 启动一个模拟协调者 (无 Claude, 交互式派任务)
  stress-test         稳定性压测 (反复跑测试,失败时输出到日志)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import sys
import time
from pathlib import Path

from .discovery import discover_coordinator, ping_coordinator
from .config import load_token


def cmd_discover(args: argparse.Namespace) -> int:
    timeout = args.timeout
    print(f"在局域网扫描 Coop Server (等满 {timeout}s 收集所有响应)...")
    result = asyncio.run(discover_coordinator(
        timeout_sec=timeout, collect_all=True
    ))
    if result is None:
        print("未发现 Coop Server")
        print()
        print("可能原因:")
        print("  1. Coop Server 还没启动")
        print("  2. Coop Server 所在机器不在同一局域网")
        print("  3. mDNS 被防火墙阻挡")
        print("  4. 协调者配置 discovery.enabled=false")
        print()
        print("可以直接通过 --host 指定Coop Server 地址绕过 mDNS")
        return 1

    all_servers = result.get("all_servers", [result])

    if len(all_servers) > 1:
        # 警告: 局域网内有多个 coop server, 这通常是部署错误
        print()
        print(f"⚠ 警告: 在局域网内发现 {len(all_servers)} 个 coop server!")
        print(f"  正常情况一个团队/局域网应该只跑一个 server。")
        print(f"  数据不在多个 server 之间共享,worker 连错了会接不到协调者派的任务。")
        print()
        print(f"发现的 server 列表:")
        for i, s in enumerate(all_servers, 1):
            extras = (
                f" (其他候选地址: {[a for a in s['all_addresses'] if a != s['host']]})"
                if len(s.get("all_addresses", [])) > 1 else ""
            )
            print(f"  {i}. {s['name']}")
            print(f"     地址: {s['host']}:{s['port']}{extras}")
            if s.get("properties"):
                print(f"     属性: {s['properties']}")
        print()
        print(f"建议:")
        print(f"  - 只保留一个,在多余的机器上 coopctl stop")
        print(f"  - 或者各 worker 用 --host 显式指定要连哪个 server")
        return 1

    # 唯一 server, 正常输出
    s = all_servers[0]
    print(f"找到 Coop Server: {s['name']}")
    print(f"  地址: {s['host']}:{s['port']}")
    if len(s.get("all_addresses", [])) > 1:
        others = [a for a in s["all_addresses"] if a != s["host"]]
        print(f"  其他候选地址: {others}")
        print(f"  (已自动选择最像 LAN IP 的那个,如果选错了用 --host 显式指定)")
    if s.get("properties"):
        print(f"  属性: {s['properties']}")
    return 0


def cmd_ping(args: argparse.Namespace) -> int:
    host, port = _resolve_host_port(args)
    if host is None:
        return 1

    token = _load_token_or_exit(args)
    if token is None:
        return 1

    url = f"http://{host}:{port}"
    print(f"ping {url} (token=***)...")
    ok, msg = asyncio.run(ping_coordinator(url, token))
    if ok:
        print(f"✓ {msg}")
        return 0
    else:
        print(f"✗ {msg}")
        return 1


def cmd_doctor(args: argparse.Namespace) -> int:
    """全面诊断,挨个检查关键依赖。"""
    from .config import (
        describe_config_lookup,
        describe_coordinator_lookup,
        describe_token_lookup,
        load_client_config,
        resolve_coordinator,
    )

    print("=" * 50)
    print("Coop 客户端环境诊断")
    print("=" * 50)
    failures = 0

    # 1. 客户端配置文件
    print()
    print("[1/5] 检查客户端配置文件...")
    try:
        client_cfg = load_client_config(args.config)
    except RuntimeError as e:
        print(f"  ✗ 配置文件错误: {e}")
        client_cfg = None
        failures += 1
    if client_cfg is not None:
        if client_cfg.source_path:
            print(f"  ✓ 已加载: {client_cfg.source_path}")
            if client_cfg.token_file:
                print(f"    - token_file: {client_cfg.token_file}")
            if client_cfg.coordinator.host:
                print(
                    f"    - coordinator: {client_cfg.coordinator.host}:"
                    f"{client_cfg.coordinator.port}"
                )
        else:
            print("  - 未找到配置文件 (将使用环境变量/命令行参数/默认路径)")
            for line in describe_config_lookup().splitlines():
                print(f"    {line}")

    # 2. AI agent (Claude Code / Codex CLI / Gemini CLI 等都行,有就行)
    print()
    print("[2/5] 检查 AI agent...")
    detected_agents = []
    for cmd, name in [
        ("claude", "Claude Code"),
        ("codex", "Codex CLI"),
        ("gemini", "Gemini CLI"),
    ]:
        path = shutil.which(cmd)
        if path:
            detected_agents.append(f"{name} ({cmd} -> {path})")

    if detected_agents:
        for agent in detected_agents:
            print(f"  ✓ 检测到: {agent}")
    else:
        print("  - 未检测到常见 AI agent (claude / codex / gemini)")
        print("    Coop Server 协议无关, 装任意 MCP 兼容的 agent 都能用")
        print("    Claude Code: https://docs.claude.com/en/docs/claude-code/quickstart")
        # 不算 fail——纯协议测试 (smoke-test / stress-test) 不需要 agent

    # 3. Coop Server 地址
    print()
    print("[3/5] 解析 Coop Server 地址...")
    coord_host, coord_port = resolve_coordinator(
        host=args.host, port=args.port, config=client_cfg
    )
    if coord_host:
        print(f"  ✓ Coop Server: {coord_host}:{coord_port}")
    else:
        # 尝试 mDNS
        print("  - 静态配置无 Coop Server 地址,尝试 mDNS 发现...")
        result = asyncio.run(discover_coordinator(
            timeout_sec=3, collect_all=True
        ))
        if result:
            all_servers = result.get("all_servers", [result])
            if len(all_servers) > 1:
                print(f"  ⚠ 发现 {len(all_servers)} 个 coop server (异常):")
                for s in all_servers:
                    print(f"    - {s['host']}:{s['port']} ({s['name']})")
                print(f"  按第一个连: {result['host']}:{result['port']}")
                print(f"  建议用 --host 显式指定要连哪个,或者关掉多余 server")
                # 不算 failure, 让用户继续看下一步,但提示了风险
            coord_host = result["host"]
            coord_port = result["port"]
            print(f"  ✓ mDNS 发现: {coord_host}:{coord_port}")
        else:
            print(f"  ✗ 未找到Coop Server 地址")
            for line in describe_coordinator_lookup(client_cfg).splitlines():
                print(f"    {line}")
            failures += 1

    # 4. token
    print()
    print("[4/5] 检查 token...")
    token = load_token(args.token_file, config=client_cfg)
    if token is None:
        print("  ✗ 未找到 token")
        for line in describe_token_lookup(client_cfg).splitlines():
            print(f"    {line}")
        failures += 1
    elif len(token) < 16:
        print(f"  ✗ token 太短 ({len(token)} 字符)")
        failures += 1
        token = None
    else:
        source = _detect_token_source(args.token_file, client_cfg)
        print(f"  ✓ token 已加载 (长度 {len(token)})")
        print(f"    来源: {source}")

    # 5. 连接测试
    print()
    print("[5/5] 测试与 Coop Server 连接...")
    if coord_host and token:
        url = f"http://{coord_host}:{coord_port}"
        ok, msg = asyncio.run(ping_coordinator(url, token))
        if ok:
            print(f"  ✓ {msg}")
        else:
            print(f"  ✗ {msg}")
            failures += 1
    else:
        print("  - 跳过 (依赖前面的检查)")

    print()
    print("=" * 50)
    if failures == 0:
        print("全部检查通过 ✓")
        return 0
    else:
        print(f"有 {failures} 项检查未通过 ✗")
        return 1


def cmd_setup_mcp(args: argparse.Namespace) -> int:
    """生成 .mcp.json 文件。"""
    host, port = _resolve_host_port(args)
    if host is None:
        return 1

    token = _load_token_or_exit(args)
    if token is None:
        return 1

    out_dir = Path(args.dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    mcp_file = out_dir / ".mcp.json"

    config = {
        "mcpServers": {
            "coop": {
                "type": "http",
                "url": f"http://{host}:{port}/mcp/",
                "headers": {
                    "Authorization": f"Bearer {token}",
                },
            }
        }
    }

    mcp_file.write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(f"已写入: {mcp_file}")
    print()
    print("接下来在 Claude Code 中:")
    print(f"  cd {out_dir}")
    print(f"  claude")
    return 0


def cmd_list_workers(args: argparse.Namespace) -> int:
    host, port = _resolve_host_port(args)
    if host is None:
        return 1
    token = _load_token_or_exit(args)
    if token is None:
        return 1

    from .mcp_call import call_tool

    url = f"http://{host}:{port}/mcp/"
    # 默认只显示在线; --all 时也显示 OFFLINE
    online_only = not args.all
    result = asyncio.run(call_tool(
        url, token, "list_workers", {"online_only": online_only}
    ))
    if not result.get("ok"):
        print(f"调用失败: {result}")
        return 1
    workers = result["workers"]
    if not workers:
        if args.all:
            print("没有 worker 记录")
        else:
            print("没有 worker 在线 (用 --all 看所有,含 OFFLINE)")
        return 0
    print(f"{'Worker ID':<20} {'Hostname':<25} {'Status':<10} {'Current Task':<15} {'Last HB':<25}")
    print("-" * 100)
    for w in workers:
        print(
            f"{w['worker_id']:<20} {w['hostname']:<25} {w['status']:<10} "
            f"{w.get('current_task_id') or '-':<15} {w['last_heartbeat']:<25}"
        )
    return 0


def cmd_prune_workers(args: argparse.Namespace) -> int:
    """清理所有 OFFLINE worker 记录。"""
    host, port = _resolve_host_port(args)
    if host is None:
        return 1
    token = _load_token_or_exit(args)
    if token is None:
        return 1

    from .mcp_call import call_tool

    url = f"http://{host}:{port}/mcp/"
    result = asyncio.run(call_tool(url, token, "prune_offline_workers", {}))
    if not result.get("ok"):
        print(f"调用失败: {result}")
        return 1
    n = result.get("deleted_count", 0)
    if n == 0:
        print("没有需要清理的 OFFLINE worker")
    else:
        print(f"已清理 {n} 个 OFFLINE worker")
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    """派一个任务给指定 worker (一次性,立即返回)。"""
    host, port = _resolve_host_port(args)
    if host is None:
        return 1
    token = _load_token_or_exit(args)
    if token is None:
        return 1

    from .mcp_call import call_tool

    url = f"http://{host}:{port}/mcp/"
    # 自动生成 task_id (如果没指定)
    task_id = args.task_id or f"T-{int(time.time())}"

    params = {
        "task_id": task_id,
        "assignee": args.to,
        "description": args.description,
    }
    if args.priority:
        params["priority"] = args.priority

    print(f"派任务 {task_id} → {args.to}")
    print(f"  描述: {args.description}")
    if args.priority and args.priority != "normal":
        print(f"  优先级: {args.priority}")

    result = asyncio.run(call_tool(url, token, "publish_task", params))
    if not result.get("ok"):
        print(f"  ✗ 派单失败: {result.get('error', result)}", file=sys.stderr)
        return 1
    print(f"  ✓ 已派发,task_id = {task_id}")
    print(f"  用 'coop list-tasks' 查看进展")
    return 0


def cmd_list_tasks(args: argparse.Namespace) -> int:
    host, port = _resolve_host_port(args)
    if host is None:
        return 1
    token = _load_token_or_exit(args)
    if token is None:
        return 1

    from .mcp_call import call_tool

    url = f"http://{host}:{port}/mcp/"
    params = {}
    if args.status:
        params["status"] = args.status
    result = asyncio.run(call_tool(url, token, "list_tasks", params))
    if not result.get("ok"):
        print(f"调用失败: {result}")
        return 1
    tasks = result["tasks"]
    if not tasks:
        print("没有任务")
        return 0
    print(f"{'Task ID':<15} {'Assignee':<15} {'Status':<12} {'Priority':<8} {'Dispatched':<25}")
    print("-" * 80)
    for t in tasks:
        print(
            f"{t['task_id']:<15} {t['assignee']:<15} {t['status']:<12} "
            f"{t['priority']:<8} {t['dispatched_at']:<25}"
        )
    return 0


def cmd_init_client_config(args: argparse.Namespace) -> int:
    """生成 ~/.coop/client.yaml 配置文件模板。

    交互式询问 token 文件路径和Coop Server 地址,写入文件。
    """
    # 默认写到 ~/.coop/client.yaml,不用 COOP_PREFIX 那个
    # 因为这命令面向 worker 端 (没装 server 的机器),用户级路径更直观
    DEFAULT_PATH = Path.home() / ".coop" / "client.yaml"

    target = Path(args.path) if args.path else DEFAULT_PATH
    target = target.expanduser()

    if target.exists() and not args.force:
        print(f"配置文件已存在: {target}")
        print("用 --force 覆盖,或手动编辑")
        return 1

    target.parent.mkdir(parents=True, exist_ok=True)

    # 用户提供的或默认值
    token_file = args.token_file or "/请改成你这台机器上的 token 路径"
    host = args.host or ""
    port = args.port or 7777

    content = "# Coop 客户端配置\n"
    content += "# 这个文件每台机器各自一份,记录本机相关的路径和地址\n"
    content += "\n"
    content += "# token 文件位置 (你这台机器上能访问到的路径)\n"
    if args.token_file:
        content += f'token_file: "{token_file}"\n'
    else:
        content += f'token_file: "{token_file}"  # ← 改这里\n'
    content += "\n"
    content += "# Coop Server 地址 (留空则用 mDNS 自动发现)\n"
    if host:
        content += "coordinator:\n"
        content += f'  host: "{host}"\n'
        content += f"  port: {port}\n"
    else:
        content += "# coordinator:\n"
        content += '#   host: "coop.lan"\n'
        content += "#   port: 7777\n"

    target.write_text(content, encoding="utf-8")
    print(f"已写入: {target}")
    print()
    print("接下来:")
    if "请改成" in token_file:
        print(f"  1. 编辑 {target},把 token_file 改成你这台机器上的实际路径")
    print(f"  验证配置: coop doctor")
    return 0


def cmd_smoke_test(args: argparse.Namespace) -> int:
    """一键端到端烟雾测试。"""
    from .simulate import run_smoke_test

    host, port = _resolve_host_port(args)
    if host is None:
        return 1
    token = _load_token_or_exit(args)
    if token is None:
        return 1

    url = f"http://{host}:{port}/mcp/"
    ok = asyncio.run(run_smoke_test(url, token, timeout_sec=args.timeout))
    return 0 if ok else 1


def cmd_test_clarification(args: argparse.Namespace) -> int:
    """测试 clarification 双向通信。"""
    from .simulate import run_clarification_test

    host, port = _resolve_host_port(args)
    if host is None:
        return 1
    token = _load_token_or_exit(args)
    if token is None:
        return 1

    url = f"http://{host}:{port}/mcp/"
    ok = asyncio.run(run_clarification_test(url, token))
    return 0 if ok else 1


def cmd_test_blocked(args: argparse.Namespace) -> int:
    """测试 blocked 反馈。"""
    from .simulate import run_blocked_test

    host, port = _resolve_host_port(args)
    if host is None:
        return 1
    token = _load_token_or_exit(args)
    if token is None:
        return 1

    url = f"http://{host}:{port}/mcp/"
    ok = asyncio.run(run_blocked_test(url, token))
    return 0 if ok else 1


def cmd_simulate_worker(args: argparse.Namespace) -> int:
    """启动一个模拟 worker,持续接活直到 Ctrl+C。"""
    from .simulate import SimulatedWorker

    host, port = _resolve_host_port(args)
    if host is None:
        return 1
    token = _load_token_or_exit(args)
    if token is None:
        return 1

    url = f"http://{host}:{port}/mcp/"
    worker = SimulatedWorker(
        url=url,
        token=token,
        worker_id=args.worker_id,
        auto_submit=not args.no_submit,
        force_blocked_reason=args.force_blocked,
        force_clarification=args.force_clarification,
    )

    print(f"启动模拟 worker (id={args.worker_id})")
    print(f"按 Ctrl+C 停止")
    print()

    try:
        asyncio.run(worker.run())
    except KeyboardInterrupt:
        print()
        print(f"停止。统计:")
        print(f"  接收任务: {worker.stats.tasks_received}")
        print(f"  提交完成: {worker.stats.tasks_submitted}")
        print(f"  发起 clarification: {worker.stats.clarifications_requested}")
        print(f"  上报 blocked: {worker.stats.blocks_reported}")
    return 0


def cmd_simulate_coordinator(args: argparse.Namespace) -> int:
    """启动模拟协调者:监听事件 + 接受手动派单命令。"""
    from .simulate import run_simulate_coordinator

    host, port = _resolve_host_port(args)
    if host is None:
        return 1
    token = _load_token_or_exit(args)
    if token is None:
        return 1

    url = f"http://{host}:{port}/mcp/"

    print(f"启动模拟协调者")
    print(f"  连接: {url}")
    print(f"  自动 cleanup: {'是' if not args.no_auto_cleanup else '否'}")
    print(
        f"  自动答 clarification: "
        f"{repr(args.auto_answer) if args.auto_answer else '关'}"
    )
    print()
    print("输入 'help' 查看可用命令, 'quit' 退出")
    print()

    try:
        asyncio.run(run_simulate_coordinator(
            url=url,
            token=token,
            auto_request_cleanup=not args.no_auto_cleanup,
            auto_answer=args.auto_answer,
        ))
    except KeyboardInterrupt:
        print()
        print("已停止")
    return 0


def cmd_stress_test(args: argparse.Namespace) -> int:
    """稳定性压测,失败时输出到日志文件。"""
    from .simulate import run_stress_test, _STRESS_TESTS

    host, port = _resolve_host_port(args)
    if host is None:
        return 1
    token = _load_token_or_exit(args)
    if token is None:
        return 1

    # 解析要跑的测试
    if args.tests == "all":
        tests = list(_STRESS_TESTS.keys())
    else:
        tests = [t.strip() for t in args.tests.split(",")]
        for t in tests:
            if t not in _STRESS_TESTS:
                print(
                    f"未知测试 '{t}',可选: {list(_STRESS_TESTS.keys())} 或 all",
                    file=sys.stderr,
                )
                return 1

    # 解析迭代次数 (支持 'inf' 或负数表示无限)
    if args.iterations.lower() in ("inf", "infinite", "-1"):
        iterations = 10**9  # 实际不会跑这么多,Ctrl+C 会停
    else:
        try:
            iterations = int(args.iterations)
            if iterations <= 0:
                raise ValueError
        except ValueError:
            print(f"--iterations 必须是正整数或 'inf'", file=sys.stderr)
            return 1

    url = f"http://{host}:{port}/mcp/"
    try:
        fail_count = asyncio.run(run_stress_test(
            url=url,
            token=token,
            tests=tests,
            iterations=iterations,
            log_dir=args.log_dir,
        ))
    except KeyboardInterrupt:
        # asyncio.run 内部捕获不到的退出信号
        print()
        print("用户中断")
        return 130
    return 1 if fail_count > 0 else 0


def cmd_init_coordinator(args: argparse.Namespace) -> int:
    """初始化 Coordinator 工作目录: 写 CLAUDE.md 和 .mcp.json"""
    return _init_workspace(args, role="coordinator")


def cmd_init_worker(args: argparse.Namespace) -> int:
    """初始化 Worker 工作目录: 写 CLAUDE.md 和 .mcp.json"""
    return _init_workspace(args, role="worker")


def _init_workspace(args: argparse.Namespace, role: str) -> int:
    out_dir = Path(args.dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. setup-mcp
    rc = cmd_setup_mcp(args)
    if rc != 0:
        return rc

    # 2. 写 CLAUDE.md (从 personas 模板)
    persona_src = Path(__file__).parent.parent / "personas" / f"{role}.md"
    persona_dst = out_dir / "CLAUDE.md"
    if persona_src.exists():
        if persona_dst.exists():
            print(f"已存在 {persona_dst}, 不覆盖")
        else:
            shutil.copy2(persona_src, persona_dst)
            print(f"已写入: {persona_dst}")
    else:
        print(f"警告: 找不到 persona 模板 {persona_src}")

    print()
    print(f"{role} 工作目录初始化完成: {out_dir}")
    print(f"现在可以启动 Claude Code:")
    print(f"  cd {out_dir}")
    print(f"  claude")
    return 0


# ===========================================================
# 辅助函数
# ===========================================================

def _load_client_config_from_args(
    args: argparse.Namespace,
) -> "ClientConfig":
    """从命令行参数加载客户端配置。失败抛 RuntimeError。"""
    from .config import load_client_config
    return load_client_config(getattr(args, "config", None))


def _resolve_host_port(args: argparse.Namespace) -> tuple[str | None, int]:
    """解析Coop Server 地址。优先级: 命令行 → 环境变量 → 配置文件 → mDNS。"""
    from .config import resolve_coordinator

    try:
        client_cfg = _load_client_config_from_args(args)
    except RuntimeError as e:
        print(f"配置错误: {e}", file=sys.stderr)
        return None, 7777

    # 先尝试静态配置(命令行/环境变量/配置文件)
    host, port = resolve_coordinator(
        host=args.host, port=args.port, config=client_cfg
    )
    if host:
        return host, port

    # 静态配置都没有 → 用 mDNS
    print("未配置Coop Server 地址,通过 mDNS 发现...", file=sys.stderr)
    result = asyncio.run(discover_coordinator(timeout_sec=3))
    if result:
        return result["host"], result["port"]
    print(
        "未发现 Coop Server。请用 --host 指定,或在 client.yaml 配置 "
        "coordinator.host",
        file=sys.stderr,
    )
    return None, port


def _detect_token_source(
    explicit_path: str | None,
    config: "ClientConfig | None" = None,
) -> str:
    """检测 token 实际从哪个来源加载,用于显示。"""
    from .config import (
        DEFAULT_TOKEN_PATHS,
        ENV_TOKEN_FILE,
        load_client_config,
    )

    if explicit_path:
        return f"--token-file {explicit_path}"

    env = os.environ.get(ENV_TOKEN_FILE)
    if env and Path(env).expanduser().exists():
        return f"环境变量 {ENV_TOKEN_FILE}={env}"

    if config is None:
        try:
            config = load_client_config()
        except RuntimeError:
            config = None
    if config and config.token_file:
        p = Path(config.token_file).expanduser()
        if p.exists():
            return f"配置文件 {config.source_path} 中的 token_file={config.token_file}"

    for p in DEFAULT_TOKEN_PATHS:
        if p.exists():
            if p.is_symlink():
                target = os.readlink(p)
                return f"{p} -> {target}"
            return str(p)
    return "未知"


def _load_token_or_exit(args: argparse.Namespace) -> str | None:
    """加载 token。"""
    if args.token:
        return args.token

    try:
        client_cfg = _load_client_config_from_args(args)
    except RuntimeError as e:
        print(f"配置错误: {e}", file=sys.stderr)
        return None

    token = load_token(args.token_file, config=client_cfg)
    if token is None:
        from .config import describe_token_lookup
        print("找不到 token", file=sys.stderr)
        print("", file=sys.stderr)
        print(describe_token_lookup(client_cfg), file=sys.stderr)
        print("", file=sys.stderr)
        print("配置 token 的 3 种方式 (推荐用第一种):", file=sys.stderr)
        print("", file=sys.stderr)
        print("  方式 1 (推荐): 写入 ~/.coop/client.yaml", file=sys.stderr)
        print("    mkdir -p ~/.coop", file=sys.stderr)
        print("    cat > ~/.coop/client.yaml <<EOF", file=sys.stderr)
        print("    token_file: \"/你这台机器上的网盘路径/coop-token\"", file=sys.stderr)
        print("    EOF", file=sys.stderr)
        print("", file=sys.stderr)
        print("  方式 2: 软链接", file=sys.stderr)
        print(
            "    mkdir -p ~/.coop && ln -s '/网盘路径/coop-token' ~/.coop/token",
            file=sys.stderr,
        )
        print("", file=sys.stderr)
        print("  方式 3: 环境变量", file=sys.stderr)
        print(
            "    export COOP_TOKEN_FILE=/网盘路径/coop-token",
            file=sys.stderr,
        )
        return None
    return token


# ===========================================================
# argparse
# ===========================================================

def _add_connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config", default=None,
        help="客户端配置文件路径 (默认查找 $COOP_PREFIX/client/client.yaml,"
             " 或自动反推,或 ~/.coop/client.yaml,"
             " 也可用环境变量 COOP_CONFIG)"
    )
    parser.add_argument(
        "--host", default=None,
        help="Coop Server 地址 (优先级: 命令行 > 环境变量 > 配置文件 > mDNS)"
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="协调者端口 (默认 7777,也可在 client.yaml 中配置)"
    )
    parser.add_argument(
        "--token", default=None, help="鉴权 token (优先级最高)"
    )
    parser.add_argument(
        "--token-file", default=None,
        help="token 文件路径 (默认查找 ~/.coop/token, /etc/coop/token)"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="coop",
        description="Coop 协作系统客户端工具"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # discover
    p = sub.add_parser("discover", help="扫描局域网内的 Coop Server")
    p.add_argument("--timeout", type=int, default=5)
    p.set_defaults(func=cmd_discover)

    # doctor
    p = sub.add_parser("doctor", help="诊断本机环境")
    _add_connection_args(p)
    p.set_defaults(func=cmd_doctor)

    # ping
    p = sub.add_parser("ping", help="测试与 Coop Server 的连接")
    _add_connection_args(p)
    p.set_defaults(func=cmd_ping)

    # setup-mcp
    p = sub.add_parser("setup-mcp", help="生成 .mcp.json 配置")
    _add_connection_args(p)
    p.add_argument(
        "--dir", default=".",
        help="目标目录 (默认当前目录)"
    )
    p.set_defaults(func=cmd_setup_mcp)

    # list-workers
    p = sub.add_parser("list-workers", help="查看在线 worker")
    _add_connection_args(p)
    p.add_argument(
        "--all", action="store_true",
        help="显示所有 worker (含 OFFLINE 失联的),默认只显示在线"
    )
    p.set_defaults(func=cmd_list_workers)

    # prune-workers
    p = sub.add_parser(
        "prune-workers",
        help="清理所有 OFFLINE 失联 worker 记录"
    )
    _add_connection_args(p)
    p.set_defaults(func=cmd_prune_workers)

    # list-tasks
    p = sub.add_parser("list-tasks", help="查看任务列表")
    _add_connection_args(p)
    p.add_argument("--status", default=None, help="按状态过滤")
    p.set_defaults(func=cmd_list_tasks)

    # publish (协调者派任务,一次性)
    p = sub.add_parser(
        "publish",
        help="派一个任务给指定 worker (协调者用,一次性)"
    )
    _add_connection_args(p)
    p.add_argument(
        "--to", required=True,
        help="任务派给哪个 worker (worker_id)"
    )
    p.add_argument(
        "--description", "-d", required=True,
        help="任务描述 (自然语言, 应包含工程/分支/目标/验收标准)"
    )
    p.add_argument(
        "--task-id", default=None,
        help="任务 ID (默认自动生成 T-<时间戳>)"
    )
    p.add_argument(
        "--priority", default="normal",
        choices=["high", "normal", "low"],
        help="优先级 (默认 normal)"
    )
    p.set_defaults(func=cmd_publish)

    # smoke-test
    p = sub.add_parser(
        "smoke-test",
        help="一键端到端通信验证 (无需 Claude)"
    )
    _add_connection_args(p)
    p.add_argument(
        "--timeout", type=int, default=30,
        help="单个步骤的超时秒数 (默认 30)"
    )
    p.set_defaults(func=cmd_smoke_test)

    # test-clarification
    p = sub.add_parser(
        "test-clarification",
        help="测试 clarification 双向通信"
    )
    _add_connection_args(p)
    p.set_defaults(func=cmd_test_clarification)

    # test-blocked
    p = sub.add_parser(
        "test-blocked",
        help="测试 blocked 反馈式派单"
    )
    _add_connection_args(p)
    p.set_defaults(func=cmd_test_blocked)

    # simulate-worker
    p = sub.add_parser(
        "simulate-worker",
        help="启动一个不带 Claude 的模拟 worker (用于压测/调试)"
    )
    _add_connection_args(p)
    p.add_argument(
        "--worker-id", default="sim-worker-1",
        help="worker_id (默认 sim-worker-1)"
    )
    p.add_argument(
        "--no-submit", action="store_true",
        help="收到任务不自动提交 (任务挂起,用于测试 timeout 等)"
    )
    p.add_argument(
        "--force-blocked", default=None,
        help="收到任务后立即上报 blocked,值是 reason 字符串"
    )
    p.add_argument(
        "--force-clarification", default=None,
        help="收到任务后先发 clarification,值是 question 字符串"
    )
    p.set_defaults(func=cmd_simulate_worker)

    # simulate-coordinator
    p = sub.add_parser(
        "simulate-coordinator",
        help="启动一个不带 Claude 的模拟协调者 (交互式派任务)"
    )
    _add_connection_args(p)
    p.add_argument(
        "--no-auto-cleanup", action="store_true",
        help="收到 work_submitted 不自动 request_cleanup (默认自动)"
    )
    p.add_argument(
        "--auto-answer", default=None,
        help="收到 clarification 自动回复内容 (默认不自动回, 手动 stdin 输入)"
    )
    p.set_defaults(func=cmd_simulate_coordinator)

    # stress-test
    p = sub.add_parser(
        "stress-test",
        help="稳定性压测 (反复跑通信测试,失败时把完整输出写到日志)"
    )
    _add_connection_args(p)
    p.add_argument(
        "--tests", default="all",
        help="跑哪些测试,逗号分隔 (smoke,clarification,blocked) 或 all (默认 all)"
    )
    p.add_argument(
        "--iterations", default="10",
        help="跑多少轮 (每轮跑 --tests 里所有测试,可写数字或 'inf' 持续跑直到 Ctrl+C)"
    )
    p.add_argument(
        "--log-dir", default="./coop-stress-logs",
        help="失败日志写入目录,自动创建 (默认 ./coop-stress-logs)"
    )
    p.set_defaults(func=cmd_stress_test)

    # init-client-config
    p = sub.add_parser(
        "init-client-config",
        help="生成客户端配置 (主要用于 worker 机器,server 机器 install.sh 已自动生成)",
        description=(
            "生成客户端配置文件模板,默认写到 ~/.coop/client.yaml。\n\n"
            "使用场景:\n"
            "  - worker 机器: 没有部署 server,需要手动建 ~/.coop/client.yaml\n"
            "  - 自定义场景: 不用 PREFIX/client/ 那个,而是用 home 配置\n\n"
            "server 机器不需要跑这个 - install.sh 已自动在 \n"
            "$PREFIX/client/client.yaml 生成,coop CLI 会自动找到。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--path", default=None,
        help="自定义路径 (默认 ~/.coop/client.yaml)"
    )
    p.add_argument(
        "--token-file", default=None,
        help="预填的 token 文件路径"
    )
    p.add_argument(
        "--host", default=None,
        help="预填的Coop Server 地址"
    )
    p.add_argument(
        "--port", type=int, default=None,
        help="预填的协调者端口"
    )
    p.add_argument(
        "--force", action="store_true",
        help="如果已存在则覆盖"
    )
    p.set_defaults(func=cmd_init_client_config)

    # init-coordinator
    p = sub.add_parser(
        "init-coordinator",
        help="在指定目录初始化协调者工作环境"
    )
    _add_connection_args(p)
    p.add_argument("--dir", required=True, help="目标目录")
    p.set_defaults(func=cmd_init_coordinator)

    # init-worker
    p = sub.add_parser(
        "init-worker",
        help="在指定目录初始化 worker 工作环境"
    )
    _add_connection_args(p)
    p.add_argument("--dir", required=True, help="目标目录")
    p.set_defaults(func=cmd_init_worker)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
