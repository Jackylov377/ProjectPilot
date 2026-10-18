# ProjectPilot

基于 [nanobot](https://github.com/HKUDS/nanobot) 扩展的本地 AI 项目分析工作台。可以导入公开 GitHub 仓库，让模型读取源码并保存分析报告，也可以根据本地文档提问，在不同会话间使用已保存的项目决定。

当前按单机单用户运行。离线演示无需模型密钥；真实分析需要自行配置兼容的模型服务。

## 功能

| 功能 | 使用方式 |
|---|---|
| 仓库分析 | 导入公开 GitHub 默认分支快照，或指定本地目录，让模型搜索和读取源码、生成报告 |
| 知识问答 | 选择 TXT、Markdown 或带文字层的 PDF，根据检索片段回答并引用来源 |
| 项目记忆 | 保存项目模型决定和回答语言偏好，支持跨会话召回、替代与撤回 |
| 上下文管理 | 使用完整历史、近期窗口或摘要策略，控制请求预算并保留原始会话 |
| 运行记录 | 查看工具过程，停止任务，下载报告和结果 ZIP |
| 评测中心 | 使用内置题目比较配置，查看失败和待复核项 |

## 安装与启动

建议使用 Python 3.12。目前已验证 Windows / Python 3.12 的源码安装与离线流程；其他平台和容器部署仍需实际验收。

先获取源码：

```bash
git clone https://github.com/Jackylov377/ProjectPilot.git
cd ProjectPilot
```

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe scripts/prepare_tokenizer.py
.\.venv\Scripts\python.exe -m app.doctor
.\.venv\Scripts\python.exe -m app.api
```

### macOS / Linux

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python scripts/prepare_tokenizer.py
.venv/bin/python -m app.doctor
.venv/bin/python -m app.api
```

首次安装需要联网下载依赖和固定的分词资源。打开 [本地工作台](http://127.0.0.1:8765)，保留启动终端，按 `Ctrl+C` 停止服务。

第一次可以保持“本地演示”，在“项目对话”输入“读取 overview，说明项目用途并引用来源”。离线模式会执行工具与状态保存，但回答来自预设逻辑，用于体验流程。

## 接入真实模型

将 [.env.example](.env.example) 复制为本地 `.env`，填写服务地址、可用模型标识和自己的密钥。已有配置时直接编辑，不要覆盖。

| 配置项 | 内容 |
|---|---|
| `PROJECTPILOT_API_BASE` | 模型服务的兼容 API 地址 |
| `PROJECTPILOT_MODEL` | 账号实际可用的模型标识 |
| `PROJECTPILOT_API_KEY` | 本地保存的服务密钥 |

停止原服务，再运行：

```powershell
.\.venv\Scripts\python.exe -m app.doctor --live
.\.venv\Scripts\python.exe -m app.api --allow-live
```

macOS / Linux 将 Python 路径换成 `.venv/bin/python`。doctor 只检查配置，不请求模型。网页仍默认选择“本地演示”，需要手动切换为“真实模型”。分析时，问题及被读取的源码或资料片段会发送到配置的服务，并可能产生费用。

## 使用核心功能

**分析仓库：** 在“工作对话”选择“仓库分析与报告”，填入 `https://github.com/owner/repo` 并点击“获取源码”。完成后选择真实模型，可以问：“这个项目解决什么问题？请找到入口与核心实现，引用源码位置并保存一份简短报告。”导入只下载快照，不调用模型，也不执行仓库代码。

当前支持公开仓库的默认分支，可接受 `.git` 后缀。下载上限 20 MB，解压上限 60 MB，最多 8000 个条目；私有认证和指定分支尚未接入。

也可以启动时指定本地仓库：

```powershell
.\.venv\Scripts\python.exe -m app.api --allow-live --workspace C:\path\to\repository --data-dir runs/my-workbench --port 8767
```

此时打开 [8767 端口工作台](http://127.0.0.1:8767)。同一数据目录只运行一个服务。

**根据文档回答：** 在“知识资料”导入文件，预览并勾选后点击“用选中资料提问”。单文件最多 4 MB，未选中的资料不用于本次检索。扫描 PDF 的 OCR 尚未实现。

**体验项目记忆：** 在“项目对话”开启记忆，先说“我们决定使用模型 A”，再说“我们决定改用模型 B”。换新会话后询问当前决定及来源，在“项目记忆”查看有效值或撤回。当前自动记忆主要支持模型选择与回答语言；撤回 B 不会自动恢复 A。

**使用评测中心：** 先选择少量内置题目、一个配置、重复一次。离线结果用于观察流程，执行结束与评测通过分别判断，内容质量仍需要人工复核。

## 数据与限制

运行数据默认保存在 `runs/web/`，分词缓存位于 `.cache/tiktoken/`。关闭网页不会自动取消任务，需要在运行记录中停止。报告及最终回答以保存的结果为准，生成预览可能被后续纠正。

模型可能遗漏逻辑或作出过强推断；引用位置正确不代表结论正确。知识问答默认使用 BM25，尚未实现通用的无答案判断。自动记忆和摘要可能超时。当前没有多用户认证，按本地工作台使用。

## 仓库内容与许可证

`app/` 包含后端与网页资源，`examples/demo_project/` 提供默认启动示例，`evals/datasets/agent_dev/tasks_v2.json` 为网页评测中心提供题目。`scripts/prepare_tokenizer.py` 用于准备分词资源。

ProjectPilot 复用 nanobot 的 Agent 循环、Provider 接口、工具注册与基础 Session，在此基础上实现应用工具、检索、记忆策略和工作台。新增代码采用 [MIT](LICENSE) 许可证，上游及依赖保留各自许可证，详见[第三方说明](THIRD_PARTY_NOTICES.md)。
