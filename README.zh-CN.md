# OpenLoadHub

[English](README.md)

OpenLoadHub 是一个自托管压测控制面，用于统一管理、执行和观测 JMeter 与 k6 压测任务。

本地 demo 先看 [quickstart.md](docs/public/quickstart.md)。如果首次运行没有进入可用的 RunDetail 页面，看 [troubleshooting.md](docs/public/troubleshooting.md)。共享环境或传统云平台部署请看 [deployment.md](docs/public/deployment.md)。

如果你正在判断 OpenLoadHub 是否适合自己的场景，建议先看 [FAQ](FAQ.md)、[Roadmap](ROADMAP.md)、[Known Limitations](KNOWN_LIMITATIONS.md)、[Support](SUPPORT.md) 和 [Open-Core Boundary](docs/public/open-core.md)，不要把 alpha demo 直接当成生产平台承诺。

公开 alpha 版本先聚焦一条可落地的核心链路：

- 管理压测脚本和任务
- 发起单个 JMeter / k6 压测
- 查看 RunDetail、PlanRun、日志、报告和 dashboard 链接
- 查看已开放的轻量任务/批次配置 lint 提示；更深的确定性分析面板在 v0.1 alpha 中隐藏
- 通过 Grafana、InfluxDB、Prometheus 完成本地观测闭环
- 直接复用内置 demo task 和 demo plan 做开箱试跑
- 可选启用 PlanRun / 阈值事件 webhook 通知

## Alpha 范围

v0.1 alpha 包含：

- 核心压测工作流的 Web UI 和 API
- 通过本地 agent 执行 JMeter 和 k6
- RunDetail、PlanRun、报告和日志视图
- 轻量任务/批次配置 lint 提示；确定性 Run / 批次分析面板在 v0.1 alpha 中隐藏
- 包含本地 HTTP + gRPC target service 的最小 Docker demo 拓扑
- 初始化生成的 k6 HTTP+gRPC 与 JMeter HTTP+gRPC demo 任务
- 初始化生成的 JMeter 简版批次模板与 k6 + JMeter 高级批次模板
- 用于保持默认工作流聚焦 demo 范围的 public alpha feature flags

暂不进入首版或作为可选能力：

- 从零创建 mixed run 的执行工作流
- 趋势分析
- 平台自监控大盘
- 完整 Alertmanager 中心化告警工作流
- 高级分析、报告门禁和批次建议等扩展面板
- Nacos、Kafka、Redis 协议、xk6 扩展 demo
- k6 动态 TPS 调整。该能力计划在 OpenLoadHub k6 源码、许可证和可复现构建门禁通过后作为 v0.2 核心亮点发布

## 快速开始

从仓库根目录启动 demo：

```bash
cp .env.example .env
docker compose -f docker-compose.demo.yml up -d --build
```

打开：

- Web UI: `http://127.0.0.1:13000`
- API: `http://127.0.0.1:18000`
- Grafana: `http://127.0.0.1:13001`
- Prometheus: `http://127.0.0.1:19090`
- InfluxDB: `http://127.0.0.1:18086`
- Demo target HTTP: `http://127.0.0.1:18088`
- Demo target gRPC: `127.0.0.1:15051`

默认 demo 账号：

- OpenLoadHub admin: `admin` / `ptp_demo_admin`
- OpenLoadHub tester: `demo_tester` / `ptp_demo_tester`
- Grafana: `admin` / `ptp_demo_grafana`

这些账号只适合本地 demo，部署到共享环境前必须修改；默认不开放自注册。

首次启动后会自动生成两条 demo task 和两条 demo plan：

- `OpenLoadHub Demo - k6 HTTP+gRPC`
- `OpenLoadHub Demo - JMeter HTTP+gRPC`
- `OpenLoadHub Demo Plan - JMeter Simple`
- `OpenLoadHub Demo Plan - k6 + JMeter Advanced`

两条 demo task 默认都会带上关联监控、关联链路和脚本变量元数据；task 级默认 `pod_count=1`、`data_distribution=avg`，而 demo plan 可以在运行时覆盖更高节点数。登录后从 `关注任务` 或 `批次模板` 进入，可以直接试跑，也可以复制后按自己的目标服务修改脚本和参数。

首个公开 alpha 默认隐藏扩展分析面板。当前 demo 只保留核心结果、报告、日志、Grafana/dashboard 入口和轻量配置 lint 提示。

## 路线图

- v0.1 alpha：自托管 JMeter / k6 控制面，核心任务与执行链路，PlanRun，报告，本地观测。
- v0.2 目标：动态 k6 TPS 控制。该能力需要配套公开的 AGPL 兼容 OpenLoadHub k6 fork、源码 tag、构建命令和运行时证明。

## 仓库边界

本仓库聚焦 OpenLoadHub 开源 demo 与运行面，不包含：

- 私有过程文档和任务单
- 本地运行日志和报告
- 私有部署文件
- 完整第三方二进制分发包
- 自定义 k6 binary
- 非公开参考材料

源码披露与许可证边界见 [source-and-license.md](docs/public/source-and-license.md)。
运行时拓扑和可选组件决策见 [architecture.md](docs/public/architecture.md) 与 [deployment.md](docs/public/deployment.md)。
Webhook 配置、模板文件化和 clone 后如何调整，见 [webhooks.md](docs/public/webhooks.md)。

首次体验与参与入口：

- [Quickstart](docs/public/quickstart.md)
- [Demo Walkthrough](docs/public/demo-walkthrough.md)
- [Troubleshooting](docs/public/troubleshooting.md)
- [Examples](docs/public/examples/README.md)
- [First Contribution Guide](docs/public/first-contribution.md)

维护者发布准备：

- [Public Alpha Release Checklist](public-alpha-release-checklist.md)
