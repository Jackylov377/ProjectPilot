# 来源与第三方组件

ProjectPilot 的新增代码采用 [MIT 许可证](LICENSE)。本文件说明复用关系，不改变各依赖自身的许可证。

## nanobot

- 项目：[HKUDS/nanobot](https://github.com/HKUDS/nanobot)
- 固定依赖：`nanobot-ai==0.3.5`
- 本项目最初核对的上游提交：`1bb712d3488915ca4ed9ccc1a93067ff722f5ab9`
- 上游许可证：MIT；原文保存在 [licenses/nanobot-MIT.txt](licenses/nanobot-MIT.txt)。

ProjectPilot 通过 Python 依赖复用 nanobot 的 Agent 执行循环、Provider 接口、工具注册和基础 Session 存储。本项目在其上扩展了受限文件工具与 GitHub 源码导入，实现文档检索、记忆有效性管理、上下文策略、运行追踪、独立评测和网页工作台。

本仓库不打包本地虚拟环境、上游参考源码缓存或 nanobot 自带的网页与原生 TUI。安装 `nanobot-ai` 时，该发行包会携带自己的许可证与第三方声明；如另行分发完整依赖环境或可执行程序，应同时保留对应发行物中的声明。

## 其他依赖与资料

直接依赖及版本范围在 [pyproject.toml](pyproject.toml) 中声明，由安装工具分别获取；依赖不因被 ProjectPilot 调用而变更许可证。

`examples/demo_project/` 是默认启动的示例项目，`evals/datasets/agent_dev/tasks_v2.json` 为网页评测中心提供内置题目。用户通过网页导入的 GitHub 源码与知识资料属于本地运行数据，不随本仓库发布，也不会因为被导入而自动采用本项目许可证。
