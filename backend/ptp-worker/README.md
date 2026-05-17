# ptp-worker (Celery Worker)

## 说明

ptp-worker 是 Celery Worker 服务，**完全复用 ptp-admin 的应用代码**。这是通过 Docker 镜像复用实现的，而不是独立的代码目录。

## 架构

- **代码复用**：ptp-worker 与 ptp-admin 使用同一个 Docker 镜像 `ptp-admin:v2.0.0`
- **不同入口**：ptp-admin 启动 FastAPI，ptp-worker 启动 Celery Worker
- **共享代码**：所有应用代码来自 `../ptp-admin/app/`
- **独立目录**：ptp-worker 仅包含启动脚本和配置，不包含应用代码
- **任务执行**：负责执行异步任务（测试执行、报告生成等）
- **计划调度**：默认通过 embedded beat 周期扫描 `fixed/cron` 计划；如需独立 beat，可设置 `PTP_WORKER_ENABLE_BEAT=0` 后改为单独启动 Celery beat

## 启动方式

### 开发环境

```bash
cd backend/ptp-worker
python celery_worker.py
```

默认会带 `--beat`，让 `scan_scheduled_plans_task` 周期扫描 `olh_plan` 并为 `exec_type=fixed|cron` 生成 `plan_run`。如当前环境已有独立 beat，请显式关闭：

```bash
PTP_WORKER_ENABLE_BEAT=0 python celery_worker.py
```

### Docker

```bash
# ptp-worker 使用与 ptp-admin 相同的镜像，但不同入口
docker build -t ptp-admin:v2.0.0 backend/ptp-admin
docker run -it --rm ptp-admin:v2.0.0 python /app/ptp-worker/celery_worker.py
```

### Docker Compose

```bash
# ptp-worker 在 docker-compose.yml 中配置
docker-compose up ptp-worker
```

**重要说明**：ptp-worker 不是独立的服务，而是 ptp-admin 镜像的另一种启动方式。

## 任务列表

- `app.tasks.test_executor.execute_test_task`：执行测试任务
- `app.tasks.report_generator.generate_report_task`：生成测试报告

## 配置

ptp-worker 使用与 ptp-admin 相同的配置文件：
- `../ptp-admin/app/core/config.py`
- `../ptp-admin/.env`

## 依赖

ptp-worker 与 ptp-admin 使用相同的依赖，通过共享文件 `../requirements-common.txt` 定义；本目录 `requirements.txt` 仅包含引用。

**注意**：ptp-worker 本身不包含应用代码或测试代码，所有测试都在 ptp-admin 中进行。

## 测试

ptp-worker 没有独立的测试，所有 Celery 任务的测试都在 `../ptp-admin/tests/` 中。

## 目录结构

```
backend/ptp-worker/
├── celery_worker.py      # Celery Worker 启动脚本
├── requirements.txt      # 依赖（与 ptp-admin 相同）
├── Dockerfile           # 复用 ptp-admin 镜像
└── README.md            # 本文件

# 注意：没有 app/ 或 tests/ 目录
# 所有应用代码来自 ../ptp-admin/app/
```
