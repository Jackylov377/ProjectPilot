# Docker Compose 设计结构

单服务部署、独立评测 Worker 和 Prometheus 评测监控均已通过 Docker 验收。验收范围见 [MISSION.md](MISSION.md)，技术选择见 [TECH_STACK.md](TECH_STACK.md)。

## 第一阶段 · 单服务部署

| 部分 | 设计 |
| --- | --- |
| 宿主机入口 | 浏览器访问 `http://127.0.0.1:8765`。沿用 `127.0.0.1:8765:8765` 的端口映射。 |
| Compose 服务 | 沿用 `projectpilot` 服务和根目录 `Dockerfile`。容器通过 `python -m app.api --host 0.0.0.0 --port 8765` 启动 FastAPI 和页面。 |
| 任务执行 | 评测仍通过现有 `JobManager` 启动本地子进程，沿用当前一次运行一个任务的约束。 |
| 持久化 | 沿用 `pilot-data` 命名卷，挂载到 `/app/runs/web`，保存工作空间、运行记录和评测结果。首版按单服务实例使用这个卷。 |
| 健康检查 | `compose.yaml` 中的 `healthcheck` 用镜像自带的 Python 请求容器内的 `http://127.0.0.1:8765/api/health`。健康检查验证 HTTP 服务可达，业务验收另跑离线 Agent 和评测。 |
| 配置 | 默认沿用本地演示模式。真实模型可作为独立验收项讨论，启用时通过现有 `--allow-live` 和运行时配置提供模型参数与密钥。 |

### 实现顺序

1. 验证现有 `Dockerfile` 可构建，并检查 `docker compose config` 渲染出的端口和卷。
2. 给 `projectpilot` 加健康检查，用 `docker compose up --build --wait` 启动，从宿主机访问页面和健康接口。失败时用 `docker compose ps` 和 `docker compose logs projectpilot` 查看状态。
3. 通过现有任务和评测接口执行一个离线 Agent 回合与一个离线评测用例，保存任务 ID 和结果作为对照。
4. 用 `docker compose down` 停止后重新 `up --wait`，确认先前的任务与评测结果仍可查看。通过后把启动、查看日志和停机命令补进根目录 README。

## 第二阶段 · 独立评测 Worker

| 部分 | 设计 | 验证点 |
| --- | --- | --- |
| 提交 | FastAPI 将离线评测写入 `evaluation-queue.sqlite3` 并返回任务 ID。后台发布到 RabbitMQ，收到确认后标记已发布。 | Broker 暂时离线时，提交保存在本地，恢复后继续发布。 |
| 执行 | Worker 一次消费一个任务，调用现有 `app.evaluation`。每次尝试在共享卷使用独立目录，提交终态时核对尝试编号。 | Worker 中断后重新投递，同一任务最多尝试三次，重复投递保留一个可查询的最终结果。 |
| 查询与取消 | 通过原有运行记录、评测报告和导出入口读取结果。取消写入共享状态，Worker 随后停止对应进程。 | 排队取消、运行取消和完成后的重复消息都可以通过任务 ID 核对。 |

## 第三阶段 · Prometheus 评测监控

Prometheus 通过 Compose 服务名每 5 秒抓取 FastAPI 的 `/metrics`，宿主机通过 `127.0.0.1:9090` 查询。内部服务名仅用于访问指标端点。应用从共享 SQLite 读取同一份评测状态，Prometheus 保存采样历史。

等待耗时从提交到首次认领，执行耗时从最后一次认领到完成或失败。Worker 重试不会覆盖首次认领时间。历史任务只有一次尝试时可以恢复首次认领时间，已经重试的历史任务在缺少首次认领时间时跳过等待耗时样本。

Docker 官方文档：[启动和等待服务](https://docs.docker.com/reference/cli/docker/compose/up/) · [停机和数据卷](https://docs.docker.com/reference/cli/docker/compose/down/)
