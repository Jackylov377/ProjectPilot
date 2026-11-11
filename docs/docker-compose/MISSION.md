# Docker Compose 本次任务

## 目标

让 My_Agent（ProjectPilot）能够从仓库根目录用 Docker Compose 启动，在宿主机打开工作台，执行离线 Agent 和评测，并在容器重建后找回已有记录。仓库已经有 `Dockerfile` 和 `compose.yaml`，本轮工作从补齐健康检查和验证实际运行开始。

## 首版交付

- [x] `docker compose config` 能解析包含健康检查的配置。
- [x] 现有镜像构建成功。
- [x] `docker compose up --build --wait` 启动成功，`projectpilot` 进入 `healthy`，宿主机能打开首页，`GET /api/health` 返回 `200` 和 `{"status":"ok"}`。
- [x] 通过任务接口创建项目和任务，完成一个离线 Agent 回合，查询回答和运行记录。
- [x] 通过评测接口提交离线 `note-read` 用例，执行结束后查询评测报告。
- [x] 执行普通 `docker compose down` 后重新启动，仍能查到前两项生成的记录。验收使用同一 Compose 项目和数据卷。
- [x] 在 README 补充启动、状态检查、日志查看和停机方式。

## 第二阶段交付 · 独立评测 Worker

- [x] Compose 启动 RabbitMQ 和独立 Python Worker，复用现有评测命令。
- [x] 离线评测提交后返回任务 ID，运行记录支持排队、运行、取消和结果导出。
- [x] 共享卷中的 SQLite 保存评测状态，发布确认后标记已入队，Worker 在终态落库后确认投递。
- [x] 在 Docker 中验证连续提交、Broker 恢复、Worker 中断后的重投、排队与运行取消、重复投递和容器重建后的结果保留。

本阶段迁移离线评测。真实模型评测、仓库导入和 Agent 对话继续使用当前本地执行路径。

## 第三阶段交付 · Prometheus 评测监控

- [x] 通过 `/metrics` 暴露排队和运行数量、终态计数、重试次数、等待和执行耗时。
- [x] Compose 增加 Prometheus，使用内部服务名抓取指标，指标历史使用命名卷保留。
- [x] 在 Docker 中验证抓取成功、Worker 停止时队列积压、恢复后积压回落和完成数增长，并核对取消、重试和耗时样本。
- [x] 重启 Prometheus 后仍能查询重启前的积压指标。

## Redis 扩展 · 向量缓存与限流

- [x] Compose 接入 Redis，文档向量可跨运行复用，保留已有本地缓存路径。
- [x] 真实模型对话和评测共用 Redis 限流额度，返回 `429` 和 `Retry-After`。Redis 暂时不可用时，真实模型提交返回 `503`。
- [x] 在 Docker 中验证跨进程命中、文档版本和模型隔离、缓存有效期、原子限流、窗口恢复和 Redis 停止后的降级。

## 当前进度

2026-11-11 在 Windows Docker Desktop（Docker Engine `28.4.0`）上完成首版验收。独立 Compose 项目通过构建、启动、离线 Agent、离线评测和容器重建后的持久化检查。验收结束后已清理临时容器和数据卷。

同日在相同环境完成第二阶段验收。RabbitMQ 停止时连续提交三条离线评测，取消其中一条，恢复后继续执行其余两条。Worker 在首次尝试中被停止，原任务在第二次尝试完成。另一次运行中的取消和已完成任务的重复投递均未产生额外报告。评测报告可以导出，容器重建后仍可查询。独立验收项目的容器和数据卷已清理。

同日在相同环境完成 Prometheus 验收。Worker 停止时排队数从 0 升到 2，恢复后回到 0。完成、取消和重试计数分别增加 2、1 和 1，等待与执行直方图各记录两个样本。Prometheus 重启后仍可查询积压记录。临时验收容器和数据卷已清理。

同日在相同环境完成 Redis 验收。独立进程复用同一文档向量，文档版本或 Embedding 模型改变时重新生成向量。以 2 次额度发起 12 个并发请求，只有 2 个获得额度。对话与评测共用额度，重复的对话请求 ID 复用原任务，超限返回 `429` 和 `Retry-After`，窗口过期后恢复受理。停止 Redis 后，向量缓存回退到本地文件，真实模型提交返回 `503`，离线评测仍能完成。验收使用本地模拟模型配置，临时容器和数据卷已清理。

## 真实模型复测

2026-11-12 在 Compose 工作台对同一份资料发起两次真实模型语义检索。两个问题分别查询验收编号和项目负责人。两次运行均正常完成，检索方式均为 `vector`。

| 运行 | `redis_cache_hits` | `cache_misses` | Embedding 请求数 |
| --- | ---: | ---: | ---: |
| 首次查询 | 0 | 1 | 2 |
| 第二次查询 | 1 | 0 | 1 |

同日完成限流复测。临时将 `PROJECTPILOT_LIVE_RATE_LIMIT` 设为 `1`，将 `PROJECTPILOT_LIVE_RATE_WINDOW_SECONDS` 设为 `120`，再执行 `docker compose up --no-deps --wait projectpilot`。旧计数自然过期后，首条真实模型提交正常执行。等首条结束后在同一窗口内提交第二条，工作台显示超限提示。复测结束后，移除临时配置并再次执行相同的 Compose 命令即可恢复日常额度。

部署结构见 [ROADMAP.md](ROADMAP.md)，依赖与工具见 [TECH_STACK.md](TECH_STACK.md)。
