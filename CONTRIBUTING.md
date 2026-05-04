# 贡献指南

感谢考虑为 Coop 做贡献。本文档描述贡献流程和规范。

## 报告 Bug

提交 issue 前, 请先确认:

1. 已搜索现有 issue, 确认问题尚未被报告
2. 在最新版本上能够复现
3. 准备以下信息:
   - 操作系统 (`uname -a`)
   - Python 版本 (`python3 --version`)
   - Coop 版本 / commit hash (`git log -1 --oneline`)
   - 复现步骤 (越简洁越好)
   - 期望行为与实际行为对比
   - 完整错误信息和相关日志

## 提交建议

新功能或改进建议请通过 issue 讨论。**先讨论再实现**, 避免方向偏差导致返工。

涉及以下方面的改动建议尤其需要先讨论:

- MCP 协议 (工具签名、事件结构)
- 客户端配置 (`~/.coop/`, `$PREFIX/client/`)
- 部署脚本默认行为

希望先达成共识再投入实现。

## 提交 PR

### 流程

1. Fork 仓库
2. 基于 `main` 分支创建 feature 分支
3. 完成修改后运行测试: `pytest`
4. 运行端到端验证: `coop smoke-test` (需要本地启动 server)
5. 提交时清晰说明变更原因
6. 推送到个人 fork, 提交 PR

### 代码规范

- **Python 3.11+** 语法 (`match`, `|` 类型注解等)
- 模块和函数提供 docstring
- 公开 API 提供类型注解
- 注释可使用中文
- 项目未引入额外代码格式化工具, 请保持与现有代码风格一致

### 测试

新增功能必须包含相应测试:

- **单元测试**: 放置于 `tests/` 对应模块 (`tests/test_<module>.py`)
- **集成测试**: 放置于 `tests/test_e2e.py`
- 修改现有逻辑时, 确保 `pytest` 全部通过

### 文档

如果改动涉及:

- **MCP 协议** → 更新 `docs/protocol.md`
- **架构** → 更新 `docs/architecture.md`
- **运维操作** → 更新 `docs/operations.md`
- **用户可见行为** → 更新 `README.md`

### Commit 信息

简短且准确即可, 不强制 conventional commits 格式。示例:

```
修复 worker OFFLINE 状态被 list-workers 默认显示的问题

list_workers 默认 online_only=True, 排除 OFFLINE worker。
新增 --all 参数显示全部。同时新增 prune-workers 命令用于清理失联记录。
```

## 开发环境

```bash
git clone <repo>
cd coop-server
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 运行测试
pytest

# 启动 server (前台调试模式)
coop-server --config config.example.yaml

# 另开终端运行 CLI
coop doctor
coop smoke-test
```

## 设计原则

- **薄协议层**: MCP server 仅负责通信中转, 不规定业务流程
- **反馈式派单**: 任务派发不依赖 worker 能力声明, 由 worker 通过 `report_blocked` 反馈
- **持久化优先**: SQLite WAL 模式, server 重启不丢失状态
- **纯协议测试**: `simulate-*` 工具不依赖 LLM, 直接验证协议层
- **LLM 中立**: 协议层不绑定特定 agent (Claude Code / Codex CLI / Gemini CLI 等均可)

不符合这些原则的改动需要在 issue 中详细讨论。

## 优先关注的贡献方向

- **其他 AI agent 的接入示例**: Codex CLI / Gemini CLI / Cursor 等的接入教程, PR 至 `docs/` 或 README
- **agent 配置生成命令**: 例如 `setup-codex` / `setup-gemini`, 自动生成对应 agent 的 MCP 配置
- **persona 模板**: 在 `personas/` 添加针对其他 LLM 优化的 Coordinator / Worker 提示词
- **Linux / Windows 部署脚本**: 当前仅支持 macOS

## 行为准则

互相尊重, 技术讨论对事不对人。

## 许可

提交贡献即同意以 MIT 协议发布相关代码。
