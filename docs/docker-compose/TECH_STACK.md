# Docker Compose 技术栈

## 首版

| 技术 | 项目中的用途 | 状态 |
| --- | --- | --- |
| Docker Engine / Docker Desktop | 在 Windows 开发机上构建和运行 Linux 容器。Docker Desktop 是本地运行方式之一。 | 已在 Docker Engine `28.4.0` 验收 |
| Docker Compose V2 | 用 `docker compose` 统一构建、启动、查看状态和停止项目。根目录 `compose.yaml` 声明服务、端口、数据卷和健康检查。 | 已通过构建、启动与重建验证 |
| Python 3.12 / FastAPI / Uvicorn | 用 `python:3.12-slim` 构建镜像，Uvicorn 提供 HTTP 服务。健康检查使用 Python 标准库 `urllib.request`。 | 首页和健康接口验证通过 |
| SQLite / 本地文件 / Docker 命名卷 | SQLite 保存工作空间数据，本地文件保存任务与评测产物，`pilot-data` 让这些数据跨容器重建保留。 | 已在容器重建后查回记录 |
| 离线演示与内置评测用例 | 用镜像内的演示项目和评测数据验证工作台业务流程。构建阶段仍需获取基础镜像、Python 依赖和分词资源。 | 已完成一个 Agent 回合及 `note-read` 评测 |

## 扩展组件

| 技术 | 项目中的用途 |
| --- | --- |
| RabbitMQ 4.2 / Pika 1.3.2 | 离线评测使用持久队列、发布确认和手动消费确认。 |
| 独立 Python Worker | 每次处理一个评测，复用当前评测命令并将状态和报告写入共享卷。 |
| Prometheus 3.5.5 / prometheus-client 0.24.1 | 从 FastAPI `/metrics` 抓取队列数量、任务终态、重试和耗时分布，使用命名卷保存时间序列。 |
| Redis 7.4 / redis-py 6.4 | 批量读写文档向量，缓存有效期为 24 小时。通过 Lua 原子更新真实模型对话和评测共用的提交额度。 |

后续可用 GitHub Actions 自动执行镜像构建和冒烟验证。
