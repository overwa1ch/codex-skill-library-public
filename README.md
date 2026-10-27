# Codex Skill Library

本地 Skill 注册表与只读 MCP 加载器。将技能正文保存在本地，通过 MCP 工具按需发现和加载，并保留脚本、模板与参考资料的原始目录。

本仓发布独立工具源码、合成测试夹具和使用说明。技能内容由使用者自行注册，按各自许可证使用。

## 功能

- 稳定技能 ID、来源标识、分类标签、启停状态和常驻选择。
- 读取原始 `SKILL.md`，返回正文摘要、技能目录和附件的相对路径基准。
- 每个允许自动触发的技能提供一个加载工具；只允许点名的技能通过 `load_explicit_skill` 加载。
- 尊重 `agents/openai.yaml` 中的 `policy.allow_implicit_invocation`。
- 检查来源可用性，停用来源和已确认失效的插件来源拒绝加载。
- 配置修改先生成计划，应用时备份，回滚保留无关的并发改动。

加载工具只返回技能说明。具体工作由模型使用现有文件、终端或插件工具执行。

## 环境与安装

Python 3.9+，当前以 macOS 验证。文件锁依赖 POSIX `fcntl`，暂不支持 Windows。

```sh
git clone https://github.com/overwa1ch/codex-skill-library-public.git
cd codex-skill-library-public
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python library.py --registry runtime/registry.json init
```

## 注册与管理

将下面的路径替换成真实技能文件的绝对路径。`source` 是唯一且稳定的来源标识。

```sh
.venv/bin/python library.py --registry runtime/registry.json register /absolute/skill/SKILL.md --source personal:example --scope user --tag writing
.venv/bin/python library.py --registry runtime/registry.json list
.venv/bin/python library.py --registry runtime/registry.json status
```

将返回的技能 ID 代入以下命令：

```sh
.venv/bin/python library.py --registry runtime/registry.json load ID
.venv/bin/python library.py --registry runtime/registry.json edit ID --enabled false
.venv/bin/python library.py --registry runtime/registry.json edit ID --enabled true
.venv/bin/python library.py --registry runtime/registry.json edit ID --tags research writing
.venv/bin/python library.py --registry runtime/registry.json edit ID --path /absolute/new-version/skill/SKILL.md
```

注册和重绑路径不会复制或搬走技能文件。`scope` 记录来源类型；初次注册时 `system` 默认常驻，其余默认由 Library 提供。后续可用 `edit ID --placement global` 或 `--placement library` 调整。注册表的修改与宿主展示配置分别管理。

## 接入 Codex

先注册技能，再生成配置计划。将配置路径替换成实际 Codex 配置文件的绝对路径。

```sh
.venv/bin/python configure.py plan --config /absolute/codex-home/config.toml --registry runtime/registry.json --output runtime/plan.json
```

检查计划中的路径和变更。可重复传入 `--id ID`，先选择少量技能。应用计划及回滚命令：

```sh
.venv/bin/python configure.py apply --plan runtime/plan.json --transactions runtime/transactions
.venv/bin/python configure.py rollback runtime/transactions/TRANSACTION_ID
```

计划管理 `mcp_servers.skill_library` 和所选技能的原生展示配置。插件连接单独管理。配置保留虚拟环境 Python 的绝对路径，不解析该路径的符号链接。

也可以手动添加 MCP 服务：

```toml
[mcp_servers.skill_library]
command = "/absolute/codex-skill-library-public/.venv/bin/python"
args = ["/absolute/codex-skill-library-public/library.py", "--registry", "/absolute/codex-skill-library-public/runtime/registry.json", "serve"]
enabled = true
```

手动配置 MCP 只接入服务；原生技能目录的展示项需另行管理，避免重复发现。配置写入后，在宿主中核对工具列表和实际技能加载。

## 可选流程检查规则

`bootstrap.py` 可在 Codex 全局 `AGENTS.md` 添加一个独立管理的规则块，要求任务开始前发现并加载适用流程。先检查生成的规则和计划：

```sh
.venv/bin/python bootstrap.py plan --path /absolute/codex-home/AGENTS.md --output runtime/bootstrap-plan.json
.venv/bin/python bootstrap.py apply --plan runtime/bootstrap-plan.json --transactions runtime/bootstrap-transactions
.venv/bin/python bootstrap.py rollback runtime/bootstrap-transactions/TRANSACTION_ID
```

该规则包含短任务的发现检查；是否安装应结合使用场景决定。只允许用户点名的技能仍要求明确点名。

## 测试与适用范围

```sh
.venv/bin/python -m unittest discover -s tests -v
```

测试使用合成技能和临时配置。工具发现方式与初始上下文开销取决于宿主；本工具不保证固定的 token 降幅，也不能移除同一对话中已经加载的正文。`tools/app_server_probe.py` 读取独立 app-server 的库存，其结果不代表正在运行的 Desktop 已采用配置。

## 许可证

[MIT](LICENSE)。
