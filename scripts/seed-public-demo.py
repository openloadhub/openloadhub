#!/usr/bin/env python3
from __future__ import annotations

import json
import hashlib
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4


API_BASE_URL = os.getenv("PTP_API_BASE_URL", "http://ptp-admin:8000/api/v1").rstrip("/")
USERNAME = os.getenv("DEFAULT_TESTER_USERNAME", "demo_tester")
PASSWORD = os.getenv("DEFAULT_TESTER_PASSWORD", "ptp_demo_tester")
ADMIN_USERNAME = os.getenv("DEFAULT_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("DEFAULT_ADMIN_PASSWORD", "ptp_demo_admin")
ENV_NAME = os.getenv("OPENLOADHUB_DEMO_ENV", "demo")
HTTP_BASE_URL = os.getenv("DEMO_TARGET_HTTP_BASE_URL", "http://demo-target:8080")
GRPC_ADDRESS = os.getenv("DEMO_TARGET_GRPC_ADDRESS", "demo-target:50051")
GRPC_HOST, _, GRPC_PORT = GRPC_ADDRESS.partition(":")
PROTO_PATH = Path(os.getenv("OPENLOADHUB_DEMO_PROTO_PATH", "/app/demo/target-service/hello.proto"))
MAX_WAIT_SECONDS = int(os.getenv("OPENLOADHUB_DEMO_SEED_TIMEOUT_SECONDS", "180"))
DEMO_THREAD_COUNT = int(os.getenv("OPENLOADHUB_DEMO_THREAD_COUNT", "10"))
DEMO_JMETER_THREAD_COUNT = int(os.getenv("OPENLOADHUB_DEMO_JMETER_THREAD_COUNT", "1"))
DEMO_DURATION_SECONDS = int(os.getenv("OPENLOADHUB_DEMO_DURATION_SECONDS", "20"))
DEMO_TARGET_TPS = int(os.getenv("OPENLOADHUB_DEMO_TARGET_TPS", "5"))
DEMO_TASK_POD_COUNT = int(os.getenv("OPENLOADHUB_DEMO_TASK_POD_COUNT", "1"))
DEMO_AGENT_COUNT = 4
DEMO_PLAN_POD_COUNT = int(os.getenv("OPENLOADHUB_DEMO_PLAN_POD_COUNT", str(DEMO_AGENT_COUNT)))
DEMO_ADVANCED_PLAN_TASK_POD_COUNT = int(
    os.getenv(
        "OPENLOADHUB_DEMO_ADVANCED_PLAN_TASK_POD_COUNT",
        str(max(1, DEMO_PLAN_POD_COUNT // 2)),
    )
)
DEMO_DATA_SHARD_COUNT = max(DEMO_TASK_POD_COUNT, DEMO_PLAN_POD_COUNT)

DEMO_DATA_FILENAME = "test_data.csv"
DEMO_DATA_CONTENT = b"seq\n1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n"


def _public_url(port: int, path: str = "") -> str:
    base = os.getenv("OPENLOADHUB_PUBLIC_HOST", "http://127.0.0.1").rstrip("/")
    return f"{base}:{port}{path}"


GRAFANA_PUBLIC_BASE_URL = os.getenv(
    "GRAFANA_PUBLIC_BASE_URL", _public_url(13001)
).rstrip("/")
PROMETHEUS_PUBLIC_BASE_URL = os.getenv(
    "PROMETHEUS_PUBLIC_BASE_URL", _public_url(19090)
).rstrip("/")
SKYWALKING_PUBLIC_BASE_URL = os.getenv(
    "SKYWALKING_PUBLIC_BASE_URL", _public_url(18090)
).rstrip("/")
DEMO_TRACE_LINK = (
    f"{SKYWALKING_PUBLIC_BASE_URL}/?provider=skywalking&service=openloadhub-demo-target"
)


DEMO_RELATED_MONITORS = [
    {
        "title": "OpenLoadHub Demo Target - Demo Target",
        "url": (
            f"{GRAFANA_PUBLIC_BASE_URL}/d/demo-target-dashboard/demo-target-service-dashboard"
            "?orgId=1&var-job=demo-target&var-target_instance=.%2A&var-instance=.%2A&refresh=15s"
        ),
        "kind": "grafana",
        "embed_mode": "new_tab",
        "description": "Demo Target service dashboard for HTTP/gRPC demo target",
    },
    {
        "title": "Redis Exporter Dashboard",
        "url": f"{GRAFANA_PUBLIC_BASE_URL}/d/redis-dashboard/redis-exporter-dashboard?orgId=1",
        "kind": "grafana",
        "embed_mode": "new_tab",
        "description": "Demo runtime Redis dependency dashboard",
    },
    {
        "title": "MySQL Exporter Dashboard",
        "url": f"{GRAFANA_PUBLIC_BASE_URL}/d/mysql-dashboard/mysql-exporter-dashboard?orgId=1",
        "kind": "grafana",
        "embed_mode": "new_tab",
        "description": "Demo runtime MySQL dependency dashboard",
    },
]

DEMO_TOPOLOGY_DASHBOARDS = [
    {
        "title": "SkyWalking UI",
        "url": DEMO_TRACE_LINK,
        "kind": "skywalking",
        "embed_mode": "new_tab",
        "description": "Trace/topology entry for external APM integration examples",
    }
]

DEMO_ALERT_SUBSCRIPTIONS = ["ptp-alert-webhook"]
DEMO_ALERT_POLICIES = [
    {
        "name": "Demo target request risk",
        "source": "prometheus_alertmanager",
        "enabled": True,
        "match": {
            "subscription": "ptp-alert-webhook",
            "alertname": "OpenLoadHubDemoTargetHighErrorRate",
            "severity": ["warning", "critical"],
        },
        "actions": ["mark_risk", "notify_collaborators"],
        "observe_only": True,
        "auto_stop_enabled": False,
        "cooldown_seconds": 300,
        "for_seconds": 120,
    }
]

DEMO_VARIABLE_META = {
    "target_tps": {"label": "Total target TPS", "description": "Total throughput across agents"},
    "BASE_URL": {"label": "HTTP target base URL", "description": "Demo HTTP service base URL"},
    "GRPC_HOST": {"label": "gRPC target", "description": "Demo gRPC host:port"},
    "GRPC_PORT": {"label": "gRPC port", "description": "Demo gRPC service port"},
}


def _clone_monitor_entries() -> list[dict[str, Any]]:
    return [dict(item) for item in DEMO_RELATED_MONITORS]


def _clone_topology_entries() -> list[dict[str, Any]]:
    return [dict(item) for item in DEMO_TOPOLOGY_DASHBOARDS]


def _clone_alert_subscriptions() -> list[str]:
    return [str(item) for item in DEMO_ALERT_SUBSCRIPTIONS]


def _clone_alert_policies() -> list[dict[str, Any]]:
    return [dict(item) for item in DEMO_ALERT_POLICIES]


def _clone_variable_types(*, include_loops: bool, include_grpc_port: bool) -> dict[str, str]:
    variable_types = {
        "target_tps": "int",
        "BASE_URL": "string",
        "GRPC_HOST": "string",
    }
    if include_grpc_port:
        variable_types["GRPC_PORT"] = "int"
    return variable_types


def _clone_variable_meta(*, include_loops: bool, include_grpc_port: bool) -> dict[str, dict[str, str]]:
    variable_meta = {
        "target_tps": dict(DEMO_VARIABLE_META["target_tps"]),
        "BASE_URL": dict(DEMO_VARIABLE_META["BASE_URL"]),
        "GRPC_HOST": dict(DEMO_VARIABLE_META["GRPC_HOST"]),
    }
    if include_grpc_port:
        variable_meta["GRPC_PORT"] = dict(DEMO_VARIABLE_META["GRPC_PORT"])
    return variable_meta


def _shared_demo_task_properties(
    *,
    slug: str,
    include_loops: bool,
    include_grpc_port: bool,
    grpc_host: str,
    grpc_port: int | None = None,
) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "demo_seed_slug": slug,
        "BASE_URL": HTTP_BASE_URL,
        "run_by": "duration",
        "target_host": "demo-target",
        "target_port": 8080,
        "target_protocol": "http",
        "target_get_path": "/api/ping",
        "target_post_path": "/api/orders",
        "target_tps": DEMO_TARGET_TPS,
        "pod_count": DEMO_TASK_POD_COUNT,
        "pod_num": DEMO_TASK_POD_COUNT,
        "metrics_enabled": True,
        "duration": DEMO_DURATION_SECONDS,
        "business_line": "demo",
        "related_apps": ["openloadhub-demo-target"],
        "monitor_link": DEMO_RELATED_MONITORS[0]["url"],
        "monitor_dashboard_url": DEMO_RELATED_MONITORS[0]["url"],
        "related_monitors": _clone_monitor_entries(),
        "topology_dashboards": _clone_topology_entries(),
        "trace_link": DEMO_TRACE_LINK,
        "trace_link_embed_mode": "new_tab",
        "alert_subscriptions": _clone_alert_subscriptions(),
        "alert_policies": _clone_alert_policies(),
        "resource_type": "K8S",
        "cloud_vendor": "AWS",
        "data_distribution": "avg",
        "variables": _demo_variables(
            grpc_host=grpc_host,
            grpc_port=grpc_port,
        ),
        "variable_types": _clone_variable_types(
            include_loops=include_loops,
            include_grpc_port=include_grpc_port,
        ),
        "variable_meta": _clone_variable_meta(
            include_loops=include_loops,
            include_grpc_port=include_grpc_port,
        ),
    }
    if include_grpc_port:
        properties["GRPC_PORT"] = int(grpc_port or 50051)
    properties["GRPC_HOST"] = grpc_host
    return properties


K6_SCRIPT = r'''import grpc from "k6/net/grpc";
import http from "k6/http";
import { check, sleep } from "k6";
import exec from "k6/execution";
import { Counter } from "k6/metrics";

const HTTP_PING_ENDPOINT_NAME = "GET /api/ping";
const HTTP_ORDERS_ENDPOINT_NAME = "POST /api/orders";
const GRPC_SAY_HELLO_ENDPOINT_NAME = "hello.Hello/SayHello";
const GRPC_SAY_HELLO_AGAIN_ENDPOINT_NAME = "hello.Hello/SayHelloAgain";
const MIXED_WEIGHTS = [
  { key: "http_ping", weight: 10 },
  { key: "http_orders", weight: 10 },
  { key: "grpc_hello", weight: 40 },
  { key: "grpc_hello_again", weight: 40 },
];
const WEIGHT_TOTAL = MIXED_WEIGHTS.reduce((sum, item) => sum + item.weight, 0);

const client = new grpc.Client();
const protoDir = __ENV.PTP_PROTO_DIR || ".";
const baseUrl = (__ENV.BASE_URL || "http://demo-target:8080").replace(/\/$/, "");
const grpcHost = __ENV.GRPC_HOST || "demo-target:50051";
const dataFile = `${__ENV.PTP_DATA_DIR || "."}/${__ENV.DATA_FILE || "test_data.csv"}`;
const rows = open(dataFile)
  .trim()
  .split(/\r?\n/)
  .slice(1)
  .map(line => line.trim())
  .filter(Boolean);
const targetTps = Math.max(0, Number(__ENV.target_tps || __ENV.TARGET_TPS || "0"));
const durationSeconds = Math.max(0, Number(__ENV.duration || __ENV.DURATION || __ENV.PTP_DURATION_SECONDS || "0"));
const vus = Math.max(1, Number(__ENV.vus || __ENV.VUS || __ENV.PTP_THREAD_COUNT || "1"));
const loops = Math.max(0, Number(__ENV.loops || __ENV.LOOPS || __ENV.PTP_LOOPS || "0"));
const podCount = Math.max(1, Number(__ENV.pod_count || __ENV.POD_COUNT || "1"));
const grpcReqs = new Counter("grpc_reqs");

client.load([protoDir], "hello.proto");

export const options = {
  ...buildOptions(),
  thresholds: {
    http_req_failed: ["rate<0.10"],
    grpc_req_duration: ["p(95)<1000"],
    [`http_reqs{name:${HTTP_PING_ENDPOINT_NAME},endpoint_name:${HTTP_PING_ENDPOINT_NAME}}`]: ["count>=0"],
    [`http_reqs{name:${HTTP_ORDERS_ENDPOINT_NAME},endpoint_name:${HTTP_ORDERS_ENDPOINT_NAME}}`]: ["count>=0"],
    [`http_req_duration{name:${HTTP_PING_ENDPOINT_NAME},endpoint_name:${HTTP_PING_ENDPOINT_NAME}}`]: ["p(95)>=0"],
    [`http_req_duration{name:${HTTP_ORDERS_ENDPOINT_NAME},endpoint_name:${HTTP_ORDERS_ENDPOINT_NAME}}`]: ["p(95)>=0"],
    [`grpc_reqs{name:${GRPC_SAY_HELLO_ENDPOINT_NAME},endpoint_name:${GRPC_SAY_HELLO_ENDPOINT_NAME}}`]: ["count>=0"],
    [`grpc_reqs{name:${GRPC_SAY_HELLO_AGAIN_ENDPOINT_NAME},endpoint_name:${GRPC_SAY_HELLO_AGAIN_ENDPOINT_NAME}}`]: ["count>=0"],
    [`grpc_req_duration{name:${GRPC_SAY_HELLO_ENDPOINT_NAME},endpoint_name:${GRPC_SAY_HELLO_ENDPOINT_NAME}}`]: ["p(95)>=0"],
    [`grpc_req_duration{name:${GRPC_SAY_HELLO_AGAIN_ENDPOINT_NAME},endpoint_name:${GRPC_SAY_HELLO_AGAIN_ENDPOINT_NAME}}`]: ["p(95)>=0"],
  },
};

function buildOptions() {
  if (durationSeconds > 0 && loops > 0) {
    throw new Error("duration and loops are mutually exclusive for the mixed demo scenario");
  }
  if (targetTps > 0) {
    const perAgentTargetTps = targetTps / podCount;
    return {
      scenarios: Object.fromEntries(
        MIXED_WEIGHTS.map(item => [
          item.key,
          {
            executor: "constant-arrival-rate",
            rate: Math.max(1, Math.round(perAgentTargetTps * item.weight)),
            timeUnit: `${WEIGHT_TOTAL}s`,
            duration: `${Math.max(1, durationSeconds || 300)}s`,
            preAllocatedVUs: Math.max(vus, 4),
            maxVUs: Math.max(vus * 4, 8),
            exec: item.key,
          },
        ]),
      ),
    };
  }
  if (loops > 0) {
    return {
      vus,
      iterations: Math.max(loops, WEIGHT_TOTAL),
    };
  }
  return {
    vus,
    duration: `${Math.max(1, durationSeconds || 300)}s`,
  };
}

let connected = false;

function ensureConnected() {
  if (!connected) {
    client.connect(grpcHost, { plaintext: true, timeout: "3s" });
    connected = true;
  }
}

function buildName(prefix) {
  const seq = rows[exec.scenario.iterationInTest % rows.length] || String(exec.scenario.iterationInTest);
  const rand = Math.floor(Math.random() * 900000 + 100000);
  return `${prefix}-${seq}-${rand}`;
}

function runHttpPing() {
  const res = http.get(`${baseUrl}/api/ping`, {
    tags: { name: HTTP_PING_ENDPOINT_NAME, endpoint_name: HTTP_PING_ENDPOINT_NAME },
  });
  check(res, {
    "HTTP ping status 200": r => r.status === 200,
    "HTTP ping has pong": r => String(r.body || "").includes("pong"),
  });
  sleep(0.1);
}

function runHttpOrders() {
  const customer = buildName("k6-demo-customer");
  const res = http.post(
    `${baseUrl}/api/orders`,
    JSON.stringify({ customer, amount: 12.5, currency: "USD" }),
    {
      headers: { "Content-Type": "application/json" },
      tags: { name: HTTP_ORDERS_ENDPOINT_NAME, endpoint_name: HTTP_ORDERS_ENDPOINT_NAME },
    },
  );
  check(res, {
    "HTTP order status 201": r => r.status === 201,
    "HTTP order accepted": r => String(r.body || "").includes("accepted"),
  });
  sleep(0.1);
}

function invokeSayHello(name) {
  ensureConnected();
  const tags = { name: GRPC_SAY_HELLO_ENDPOINT_NAME, endpoint_name: GRPC_SAY_HELLO_ENDPOINT_NAME };
  grpcReqs.add(1, tags);
  return client.invoke(
    "hello.Hello/SayHello",
    { name },
    { tags },
  );
}

function invokeSayHelloAgain(name) {
  ensureConnected();
  const tags = { name: GRPC_SAY_HELLO_AGAIN_ENDPOINT_NAME, endpoint_name: GRPC_SAY_HELLO_AGAIN_ENDPOINT_NAME };
  grpcReqs.add(1, tags);
  return client.invoke(
    "hello.Hello/SayHelloAgain",
    { name },
    { tags },
  );
}

function runGrpcSayHello() {
  const name = buildName("openloadhub");
  const res = invokeSayHello(name);
  check(res, {
    "gRPC SayHello status OK": r => r && r.status === grpc.StatusOK,
    "gRPC SayHello echoes name": r => r && r.message && r.message.message === `Hello, ${name}`,
  });
  sleep(0.1);
}

function runGrpcSayHelloAgain() {
  const name = buildName("openloadhub-again");
  const res = invokeSayHelloAgain(name);
  check(res, {
    "gRPC SayHelloAgain status OK": r => r && r.status === grpc.StatusOK,
    "gRPC SayHelloAgain echoes name": r => r && r.message && r.message.message === `Hello again, ${name}`,
  });
  sleep(0.1);
}

export function runHttpPingScenario() {
  runHttpPing();
}

export function runHttpOrdersScenario() {
  runHttpOrders();
}

export function runGrpcSayHelloScenario() {
  runGrpcSayHello();
}

export function runGrpcSayHelloAgainScenario() {
  runGrpcSayHelloAgain();
}

export function http_ping() {
  runHttpPing();
}

export function http_orders() {
  runHttpOrders();
}

export function grpc_hello() {
  runGrpcSayHello();
}

export function grpc_hello_again() {
  runGrpcSayHelloAgain();
}

function resolveAction(iteration) {
  const normalized = iteration % WEIGHT_TOTAL;
  let cursor = 0;
  for (const item of MIXED_WEIGHTS) {
    cursor += item.weight;
    if (normalized < cursor) {
      return item.key;
    }
  }
  return MIXED_WEIGHTS[MIXED_WEIGHTS.length - 1].key;
}

export default function () {
  const route = resolveAction(exec.scenario.iterationInTest);
  if (route === "http_ping") {
    runHttpPing();
  } else if (route === "http_orders") {
    runHttpOrders();
  } else if (route === "grpc_hello") {
    runGrpcSayHello();
  } else {
    runGrpcSayHelloAgain();
  }
}

export function teardown() {
  if (connected) {
    client.close();
    connected = false;
  }
}
'''


JMETER_SCRIPT = r'''<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.4.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="OpenLoadHub Demo HTTP+gRPC" enabled="true">
      <stringProp name="TestPlan.comments">OpenLoadHub public alpha demo target smoke plan.</stringProp>
      <boolProp name="TestPlan.functional_mode">false</boolProp>
      <boolProp name="TestPlan.tearDown_on_shutdown">true</boolProp>
      <boolProp name="TestPlan.serialize_threadgroups">false</boolProp>
    </TestPlan>
    <hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="OpenLoadHub demo users" enabled="true">
        <stringProp name="ThreadGroup.on_sample_error">continue</stringProp>
        <stringProp name="ThreadGroup.num_threads">${__P(threads,1)}</stringProp>
        <stringProp name="ThreadGroup.ramp_time">${__P(rampup,0)}</stringProp>
        <boolProp name="ThreadGroup.scheduler">true</boolProp>
        <stringProp name="ThreadGroup.duration">${__P(duration,30)}</stringProp>
        <stringProp name="ThreadGroup.delay">0</stringProp>
        <elementProp name="ThreadGroup.main_controller" elementType="LoopController" guiclass="LoopControlPanel" testclass="LoopController" testname="Loop Controller" enabled="true">
          <boolProp name="LoopController.continue_forever">true</boolProp>
          <stringProp name="LoopController.loops">${__P(loops,1)}</stringProp>
        </elementProp>
      </ThreadGroup>
      <hashTree>
        <HeaderManager guiclass="HeaderPanel" testclass="HeaderManager" testname="JSON headers" enabled="true">
          <collectionProp name="HeaderManager.headers">
            <elementProp name="Content-Type" elementType="Header">
              <stringProp name="Header.name">Content-Type</stringProp>
              <stringProp name="Header.value">application/json</stringProp>
            </elementProp>
          </collectionProp>
        </HeaderManager>
        <hashTree/>
        <CSVDataSet guiclass="TestBeanGUI" testclass="CSVDataSet" testname="CSV data" enabled="true">
          <stringProp name="delimiter">${__P(DATA_DELIMITER,)}</stringProp>
          <stringProp name="fileEncoding">UTF-8</stringProp>
          <stringProp name="filename">${__P(PTP_DATA_DIR,.)}/${__P(DATA_FILE,test_data.csv)}</stringProp>
          <boolProp name="ignoreFirstLine">true</boolProp>
          <boolProp name="quotedData">false</boolProp>
          <boolProp name="recycle">true</boolProp>
          <stringProp name="shareMode">shareMode.all</stringProp>
          <boolProp name="stopThread">false</boolProp>
          <stringProp name="variableNames">seq</stringProp>
        </CSVDataSet>
        <hashTree/>
        <CounterConfig guiclass="CounterConfigGui" testclass="CounterConfig" testname="Demo sequence" enabled="true">
          <stringProp name="CounterConfig.start">1</stringProp>
          <stringProp name="CounterConfig.end"></stringProp>
          <stringProp name="CounterConfig.incr">1</stringProp>
          <stringProp name="CounterConfig.name">seq</stringProp>
          <stringProp name="CounterConfig.format"></stringProp>
          <boolProp name="CounterConfig.per_user">false</boolProp>
        </CounterConfig>
        <hashTree/>
        <HTTPSamplerProxy guiclass="HttpTestSampleGui" testclass="HTTPSamplerProxy" testname="GET /api/ping" enabled="true">
          <stringProp name="HTTPSampler.domain">${__P(target_host,demo-target)}</stringProp>
          <stringProp name="HTTPSampler.port">${__P(target_port,8080)}</stringProp>
          <stringProp name="HTTPSampler.protocol">${__P(target_protocol,http)}</stringProp>
          <stringProp name="HTTPSampler.path">${__P(target_get_path,/api/ping)}</stringProp>
          <stringProp name="HTTPSampler.method">GET</stringProp>
          <boolProp name="HTTPSampler.follow_redirects">true</boolProp>
          <boolProp name="HTTPSampler.use_keepalive">true</boolProp>
        </HTTPSamplerProxy>
        <hashTree>
          <ResponseAssertion guiclass="AssertionGui" testclass="ResponseAssertion" testname="Assert GET /api/ping" enabled="true">
            <collectionProp name="Asserion.test_strings">
              <stringProp name="0">pong</stringProp>
            </collectionProp>
            <stringProp name="Assertion.test_field">Assertion.response_data</stringProp>
            <intProp name="Assertion.test_type">2</intProp>
          </ResponseAssertion>
          <hashTree/>
        </hashTree>
        <HTTPSamplerProxy guiclass="HttpTestSampleGui" testclass="HTTPSamplerProxy" testname="POST /api/orders" enabled="true">
          <stringProp name="HTTPSampler.domain">${__P(target_host,demo-target)}</stringProp>
          <stringProp name="HTTPSampler.port">${__P(target_port,8080)}</stringProp>
          <stringProp name="HTTPSampler.protocol">${__P(target_protocol,http)}</stringProp>
          <stringProp name="HTTPSampler.path">${__P(target_post_path,/api/orders)}</stringProp>
          <stringProp name="HTTPSampler.method">POST</stringProp>
          <boolProp name="HTTPSampler.postBodyRaw">true</boolProp>
          <elementProp name="HTTPsampler.Arguments" elementType="Arguments">
            <collectionProp name="Arguments.arguments">
              <elementProp name="" elementType="HTTPArgument">
                <boolProp name="HTTPArgument.always_encode">false</boolProp>
                <stringProp name="Argument.value">{"customer":"jmeter-demo-${seq}","amount":12.5,"currency":"USD"}</stringProp>
                <stringProp name="Argument.metadata">=</stringProp>
              </elementProp>
            </collectionProp>
          </elementProp>
        </HTTPSamplerProxy>
        <hashTree>
          <ResponseAssertion guiclass="AssertionGui" testclass="ResponseAssertion" testname="Assert POST /api/orders" enabled="true">
            <collectionProp name="Asserion.test_strings">
              <stringProp name="0">accepted</stringProp>
            </collectionProp>
            <stringProp name="Assertion.test_field">Assertion.response_data</stringProp>
            <intProp name="Assertion.test_type">2</intProp>
          </ResponseAssertion>
          <hashTree/>
        </hashTree>
        <GenericController guiclass="LogicControllerGui" testclass="GenericController" testname="gRPC demo calls" enabled="true"/>
        <hashTree>
        <vn.zalopay.benchmark.GRPCSampler guiclass="vn.zalopay.benchmark.GRPCSamplerGui" testclass="vn.zalopay.benchmark.GRPCSampler" testname="GRPC SayHello" enabled="true">
          <stringProp name="GRPCSampler.protoFolder">__PTP_PROTO_DIR__</stringProp>
          <stringProp name="GRPCSampler.libFolder">__PTP_PROTO_DIR__</stringProp>
          <stringProp name="GRPCSampler.metadata"></stringProp>
          <stringProp name="GRPCSampler.host">${__P(GRPC_HOST,demo-target)}</stringProp>
          <stringProp name="GRPCSampler.port">${__P(GRPC_PORT,50051)}</stringProp>
          <stringProp name="GRPCSampler.fullMethod">Hello/SayHello</stringProp>
          <stringProp name="GRPCSampler.deadline">${__P(grpc_deadline_ms,3000)}</stringProp>
          <boolProp name="GRPCSampler.tls">false</boolProp>
          <boolProp name="GRPCSampler.tlsDisableVerification">false</boolProp>
          <stringProp name="GRPCSampler.requestJson">{"name":"jmeter-demo-${seq}"}</stringProp>
        </vn.zalopay.benchmark.GRPCSampler>
        <hashTree>
          <ResponseAssertion guiclass="AssertionGui" testclass="ResponseAssertion" testname="Assert gRPC SayHello" enabled="true">
            <collectionProp name="Asserion.test_strings">
              <stringProp name="0">Hello, jmeter-demo-</stringProp>
            </collectionProp>
            <stringProp name="Assertion.test_field">Assertion.response_data</stringProp>
            <intProp name="Assertion.test_type">2</intProp>
          </ResponseAssertion>
          <hashTree/>
        </hashTree>
        <vn.zalopay.benchmark.GRPCSampler guiclass="vn.zalopay.benchmark.GRPCSamplerGui" testclass="vn.zalopay.benchmark.GRPCSampler" testname="GRPC SayHelloAgain" enabled="true">
          <stringProp name="GRPCSampler.protoFolder">__PTP_PROTO_DIR__</stringProp>
          <stringProp name="GRPCSampler.libFolder">__PTP_PROTO_DIR__</stringProp>
          <stringProp name="GRPCSampler.metadata"></stringProp>
          <stringProp name="GRPCSampler.host">${__P(GRPC_HOST,demo-target)}</stringProp>
          <stringProp name="GRPCSampler.port">${__P(GRPC_PORT,50051)}</stringProp>
          <stringProp name="GRPCSampler.fullMethod">Hello/SayHelloAgain</stringProp>
          <stringProp name="GRPCSampler.deadline">${__P(grpc_deadline_ms,3000)}</stringProp>
          <boolProp name="GRPCSampler.tls">false</boolProp>
          <boolProp name="GRPCSampler.tlsDisableVerification">false</boolProp>
          <stringProp name="GRPCSampler.requestJson">{"name":"jmeter-demo-again-${seq}"}</stringProp>
        </vn.zalopay.benchmark.GRPCSampler>
        <hashTree>
          <ResponseAssertion guiclass="AssertionGui" testclass="ResponseAssertion" testname="Assert gRPC SayHelloAgain" enabled="true">
            <collectionProp name="Asserion.test_strings">
              <stringProp name="0">Hello again, jmeter-demo-again-</stringProp>
            </collectionProp>
            <stringProp name="Assertion.test_field">Assertion.response_data</stringProp>
            <intProp name="Assertion.test_type">2</intProp>
          </ResponseAssertion>
          <hashTree/>
        </hashTree>
        <ConstantThroughputTimer guiclass="TestBeanGUI" testclass="ConstantThroughputTimer" testname="Target throughput" enabled="true">
          <intProp name="calcMode">4</intProp>
          <stringProp name="throughput">${__P(target_tps_per_agent_per_minute,120)}</stringProp>
        </ConstantThroughputTimer>
        <hashTree/>
        </hashTree>
      </hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
'''


@dataclass(frozen=True)
class DemoTaskSpec:
    slug: str
    name: str
    filename: str
    content: str
    engine_type: str
    thread_count: int
    duration: int
    properties: dict[str, Any]


@dataclass(frozen=True)
class DemoPlanSpec:
    slug: str
    name: str
    description: str
    builder: str


def _demo_variables(
    *,
    grpc_host: str,
    grpc_port: int | None = None,
) -> dict[str, str]:
    variables = {
        "target_tps": str(DEMO_TARGET_TPS),
        "BASE_URL": HTTP_BASE_URL,
        "GRPC_HOST": grpc_host,
    }
    if grpc_port is not None:
        variables["GRPC_PORT"] = str(grpc_port)
    return variables


DEMO_TASKS = (
    DemoTaskSpec(
        slug="openloadhub-demo-k6-http-grpc",
        name="OpenLoadHub Demo - k6 HTTP+gRPC",
        filename="openloadhub-demo-k6-http-grpc.js",
        content=K6_SCRIPT,
        engine_type="k6",
        thread_count=DEMO_THREAD_COUNT,
        duration=DEMO_DURATION_SECONDS,
        properties=_shared_demo_task_properties(
            slug="openloadhub-demo-k6-http-grpc",
            include_loops=True,
            include_grpc_port=False,
            grpc_host=GRPC_ADDRESS,
        ),
    ),
    DemoTaskSpec(
        slug="openloadhub-demo-jmeter-http-grpc",
        name="OpenLoadHub Demo - JMeter HTTP+gRPC",
        filename="openloadhub-demo-jmeter-http-grpc.jmx",
        content=JMETER_SCRIPT,
        engine_type="jmeter",
        thread_count=DEMO_JMETER_THREAD_COUNT,
        duration=DEMO_DURATION_SECONDS,
        properties={
            **_shared_demo_task_properties(
                slug="openloadhub-demo-jmeter-http-grpc",
                include_loops=False,
                include_grpc_port=True,
                grpc_host=GRPC_HOST or "demo-target",
                grpc_port=int(GRPC_PORT or "50051"),
            ),
            "grpc_deadline_ms": 30000,
            "target_tpm": DEMO_TARGET_TPS * 60,
            "scheduler_enabled": True,
        },
    ),
)

DEMO_PLANS = (
    DemoPlanSpec(
        slug="openloadhub-demo-plan-jmeter-simple",
        name="OpenLoadHub Demo Plan - JMeter Simple",
        description=(
            "Seeded public alpha demo plan. "
            "demo_seed_slug=openloadhub-demo-plan-jmeter-simple. "
            "Simple two-round JMeter HTTP+gRPC batch based on the stable plan template."
        ),
        builder="jmeter_simple",
    ),
    DemoPlanSpec(
        slug="openloadhub-demo-plan-k6-jmeter-advanced",
        name="OpenLoadHub Demo Plan - k6 + JMeter Advanced",
        description=(
            "Seeded public alpha demo plan. "
            "demo_seed_slug=openloadhub-demo-plan-k6-jmeter-advanced. "
            "Advanced two-round batch that runs k6 and JMeter HTTP+gRPC tasks in one stage "
            "while splitting the default four-agent demo capacity across both tasks."
        ),
        builder="k6_jmeter_advanced",
    ),
)


def _log(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True), flush=True)


def _request(
    method: str,
    path: str,
    *,
    token: str | None = None,
    json_body: dict[str, Any] | None = None,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> Any:
    url = f"{API_BASE_URL}{path}"
    request_headers = {"User-Agent": "openloadhub-demo-seed"}
    if headers:
        request_headers.update(headers)
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    data = body
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=request_headers, method=method)
    with urlopen(request, timeout=timeout) as response:  # nosec B310
        raw = response.read().decode("utf-8", errors="replace")
        if not raw:
            return None
        return json.loads(raw)


def _download_task_asset(token: str, asset_id: int) -> bytes:
    url = f"{API_BASE_URL}/task-assets/{asset_id}/download"
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "openloadhub-demo-seed",
        },
        method="GET",
    )
    with urlopen(request, timeout=20.0) as response:  # nosec B310
        return response.read()


def _api_data(payload: Any) -> Any:
    if isinstance(payload, dict) and "code" in payload:
        if payload.get("code") != 0:
            raise RuntimeError(f"api_error code={payload.get('code')} message={payload.get('message')}")
        return payload.get("data")
    return payload


def _login_with_retry() -> str:
    return _login_user_with_retry(USERNAME, PASSWORD)


def _login_user_with_retry(
    username: str,
    password: str,
    *,
    max_wait_seconds: int = MAX_WAIT_SECONDS,
) -> str:
    deadline = time.monotonic() + max_wait_seconds
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            payload = _request(
                "POST",
                "/auth/login",
                json_body={"username": username, "password": password},
                timeout=5.0,
            )
            token = payload.get("access_token") if isinstance(payload, dict) else None
            if token:
                _log("login_ok", username=username)
                return str(token)
            last_error = f"missing access_token: {payload}"
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            last_error = str(exc)
        time.sleep(3)
    raise RuntimeError(f"login_timeout username={username} last_error={last_error}")


def _get_current_user(token: str) -> dict[str, Any]:
    data = _api_data(_request("GET", "/auth/me", token=token, timeout=10.0))
    if not isinstance(data, dict) or not isinstance(data.get("id"), int):
        raise RuntimeError(f"current_user_missing_id response={data}")
    return data


def _resolve_admin_collaborator_ids() -> list[int] | None:
    if not ADMIN_USERNAME or not ADMIN_PASSWORD:
        _log("admin_collaborator_disabled", reason="missing_admin_credentials")
        return None
    try:
        token = _login_user_with_retry(
            ADMIN_USERNAME,
            ADMIN_PASSWORD,
            max_wait_seconds=min(15, MAX_WAIT_SECONDS),
        )
        current_user = _get_current_user(token)
        admin_id = int(current_user["id"])
        _log("admin_collaborator_resolved", admin_id=admin_id, username=current_user.get("username"))
        return [admin_id]
    except Exception as exc:
        _log("admin_collaborator_unavailable", error=str(exc))
        return None


def _multipart_file(field_name: str, filename: str, content_type: str, content: bytes) -> tuple[bytes, str]:
    boundary = f"----openloadhub-demo-seed-{uuid4().hex}"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            (
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{filename}"\r\n'
            ).encode("utf-8"),
            f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
            content,
            f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    return body, f"multipart/form-data; boundary={boundary}"


def _list_demo_tasks(token: str, name: str) -> list[dict[str, Any]]:
    query = urlencode({"name": name, "pageSize": "100"})
    data = _api_data(_request("GET", f"/tasks?{query}", token=token))
    items = data.get("items") if isinstance(data, dict) else []
    return [item for item in items if isinstance(item, dict) and item.get("name") == name]


def _get_task(token: str, task_id: int) -> dict[str, Any]:
    data = _api_data(_request("GET", f"/tasks/{task_id}", token=token))
    if not isinstance(data, dict):
        raise RuntimeError(f"unexpected task response for {task_id}: {data}")
    return data


def _find_existing_task(token: str, spec: DemoTaskSpec) -> dict[str, Any] | None:
    for task in _list_demo_tasks(token, spec.name):
        properties = task.get("properties") if isinstance(task.get("properties"), dict) else {}
        if properties.get("demo_seed_slug") in {None, spec.slug}:
            return task
    return None


def _list_demo_plans(token: str, name: str) -> list[dict[str, Any]]:
    query = urlencode({"name": name, "pageSize": "100"})
    data = _api_data(_request("GET", f"/plans?{query}", token=token))
    items = data.get("items") if isinstance(data, dict) else []
    return [item for item in items if isinstance(item, dict) and item.get("name") == name]


def _find_existing_plan(token: str, spec: DemoPlanSpec) -> dict[str, Any] | None:
    marker = f"demo_seed_slug={spec.slug}"
    for plan in _list_demo_plans(token, spec.name):
        if marker in str(plan.get("description") or ""):
            return plan
    return None


def _upload_script(token: str, spec: DemoTaskSpec) -> int:
    content_type = "text/javascript; charset=utf-8" if spec.filename.endswith(".js") else "application/xml"
    body, multipart_type = _multipart_file(
        "file",
        spec.filename,
        content_type,
        spec.content.encode("utf-8"),
    )
    data = _api_data(
        _request(
            "POST",
            "/scripts/upload",
            token=token,
            body=body,
            headers={"Content-Type": multipart_type},
            timeout=20.0,
        )
    )
    script_id = data.get("id") if isinstance(data, dict) else None
    if not isinstance(script_id, int):
        raise RuntimeError(f"script_upload_missing_id spec={spec.slug} response={data}")
    return script_id


def _create_task(
    token: str,
    spec: DemoTaskSpec,
    script_id: int,
    *,
    collaborator_ids: list[int] | None = None,
) -> dict[str, Any]:
    payload = {
        "name": spec.name,
        "description": "Seeded public alpha demo task for the OpenLoadHub local demo target.",
        "env": ENV_NAME,
        "script_id": script_id,
        "engine_type": spec.engine_type,
        "task_pattern": "script",
        "protocols": ["http", "grpc"],
        "thread_count": spec.thread_count,
        "duration": spec.duration,
        "ramp_up": 0,
        "properties": spec.properties,
        "collaborator_ids": collaborator_ids,
    }
    data = _api_data(_request("POST", "/tasks", token=token, json_body=payload, timeout=20.0))
    if not isinstance(data, dict) or not isinstance(data.get("id"), int):
        raise RuntimeError(f"task_create_missing_id spec={spec.slug} response={data}")
    return data


def _task_payload(
    spec: DemoTaskSpec,
    script_id: int,
    *,
    collaborator_ids: list[int] | None = None,
) -> dict[str, Any]:
    return {
        "name": spec.name,
        "description": "Seeded public alpha demo task for the OpenLoadHub local demo target.",
        "env": ENV_NAME,
        "script_id": script_id,
        "engine_type": spec.engine_type,
        "task_pattern": "script",
        "protocols": ["http", "grpc"],
        "thread_count": spec.thread_count,
        "duration": spec.duration,
        "ramp_up": 0,
        "properties": spec.properties,
        "collaborator_ids": collaborator_ids,
    }


def _update_task(
    token: str,
    task_id: int,
    spec: DemoTaskSpec,
    script_id: int,
    *,
    collaborator_ids: list[int] | None = None,
) -> dict[str, Any]:
    data = _api_data(
        _request(
            "PUT",
            f"/tasks/{task_id}",
            token=token,
            json_body=_task_payload(spec, script_id, collaborator_ids=collaborator_ids),
            timeout=20.0,
        )
    )
    if not isinstance(data, dict) or not isinstance(data.get("id"), int):
        raise RuntimeError(f"task_update_missing_id spec={spec.slug} response={data}")
    return data


def _delete_task_asset(token: str, asset_id: int) -> None:
    _api_data(_request("DELETE", f"/task-assets/{asset_id}", token=token, timeout=20.0))


def _asset_is_reusable(
    token: str,
    asset: dict[str, Any],
    *,
    expected_hash: str,
    expected_content: bytes,
    min_shard_count: int | None = None,
) -> bool:
    asset_id = asset.get("id")
    if not isinstance(asset_id, int):
        return False
    if asset.get("content_hash") != expected_hash:
        return False
    if min_shard_count is not None:
        if int(asset.get("shard_count") or 0) < min_shard_count:
            return False
    try:
        return _download_task_asset(token, asset_id) == expected_content
    except (HTTPError, URLError, TimeoutError, OSError):
        return False


def _ensure_proto_asset(token: str, task: dict[str, Any]) -> None:
    task_id = int(task["id"])
    detail = _get_task(token, task_id)
    proto_assets = detail.get("proto_assets") if isinstance(detail.get("proto_assets"), list) else []
    proto_content = PROTO_PATH.read_bytes()
    proto_hash = hashlib.sha256(proto_content).hexdigest()
    matching_assets = [
        asset
        for asset in proto_assets
        if isinstance(asset, dict) and asset.get("file_name") == "hello.proto"
    ]
    reusable_asset_ids = {
        int(asset["id"])
        for asset in matching_assets
        if isinstance(asset.get("id"), int)
        and _asset_is_reusable(
            token,
            asset,
            expected_hash=proto_hash,
            expected_content=proto_content,
        )
    }
    if reusable_asset_ids:
        for asset in matching_assets:
            asset_id = asset.get("id")
            if isinstance(asset_id, int) and asset_id not in reusable_asset_ids:
                _delete_task_asset(token, asset_id)
        return
    for asset in matching_assets:
        asset_id = asset.get("id")
        if isinstance(asset_id, int):
            _delete_task_asset(token, asset_id)
    body, multipart_type = _multipart_file(
        "file",
        "hello.proto",
        "text/plain; charset=utf-8",
        proto_content,
    )
    path = f"/task-assets/upload?{urlencode({'category': 'proto', 'task_id': str(task_id)})}"
    _api_data(
        _request(
            "POST",
            path,
            token=token,
            body=body,
            headers={"Content-Type": multipart_type},
            timeout=20.0,
        )
    )


def _ensure_data_asset(token: str, task: dict[str, Any]) -> None:
    task_id = int(task["id"])
    detail = _get_task(token, task_id)
    data_assets = detail.get("data_assets") if isinstance(detail.get("data_assets"), list) else []
    data_hash = hashlib.sha256(DEMO_DATA_CONTENT).hexdigest()
    matching_assets = [
        asset
        for asset in data_assets
        if isinstance(asset, dict) and asset.get("file_name") == DEMO_DATA_FILENAME
    ]
    reusable_asset_ids = {
        int(asset["id"])
        for asset in matching_assets
        if isinstance(asset.get("id"), int)
        and _asset_is_reusable(
            token,
            asset,
            expected_hash=data_hash,
            expected_content=DEMO_DATA_CONTENT,
            min_shard_count=DEMO_DATA_SHARD_COUNT,
        )
    }
    if reusable_asset_ids:
        for asset in matching_assets:
            asset_id = asset.get("id")
            if isinstance(asset_id, int) and asset_id not in reusable_asset_ids:
                _delete_task_asset(token, asset_id)
        return
    for asset in matching_assets:
        asset_id = asset.get("id")
        if isinstance(asset_id, int):
            _delete_task_asset(token, asset_id)
    body, multipart_type = _multipart_file(
        "file",
        DEMO_DATA_FILENAME,
        "text/csv; charset=utf-8",
        DEMO_DATA_CONTENT,
    )
    path = (
        "/task-assets/upload?"
        + urlencode(
            {
                "category": "data",
                "task_id": str(task_id),
                "shard_count": str(DEMO_DATA_SHARD_COUNT),
            }
        )
    )
    _api_data(
        _request(
            "POST",
            path,
            token=token,
            body=body,
            headers={"Content-Type": multipart_type},
            timeout=20.0,
        )
    )


def _prepare_task(token: str, task_id: int) -> None:
    _api_data(_request("POST", f"/tasks/{task_id}/prepare-run", token=token, json_body={}, timeout=20.0))


def _ensure_task(
    token: str,
    spec: DemoTaskSpec,
    collaborator_ids: list[int] | None = None,
) -> dict[str, Any]:
    existing = _find_existing_task(token, spec)
    script_id = _upload_script(token, spec)
    if existing is not None:
        task_id = int(existing["id"])
        _log("task_exists", slug=spec.slug, task_id=task_id)
        updated = _update_task(
            token,
            task_id,
            spec,
            script_id,
            collaborator_ids=collaborator_ids,
        )
        _ensure_proto_asset(token, updated)
        _ensure_data_asset(token, updated)
        _prepare_task(token, task_id)
        seeded = _get_task(token, task_id)
        _log("task_refreshed", slug=spec.slug, task_id=task_id, script_id=script_id)
        return seeded

    task = _create_task(token, spec, script_id, collaborator_ids=collaborator_ids)
    _ensure_proto_asset(token, task)
    _ensure_data_asset(token, task)
    _prepare_task(token, int(task["id"]))
    seeded = _get_task(token, int(task["id"]))
    _log("task_seeded", slug=spec.slug, task_id=seeded.get("id"), script_id=script_id)
    return seeded


def _run_params(
    *,
    duration: int,
    target_tps: int,
    thread_count: int,
    pod_count: int | None = None,
) -> dict[str, Any]:
    resolved_pod_count = max(1, int(pod_count or DEMO_PLAN_POD_COUNT))
    return {
        "pod_count": resolved_pod_count,
        "pod_num": resolved_pod_count,
        "run_mode": "duration",
        "duration": duration,
        "duration_seconds": duration,
        "target_tps": target_tps,
        "thread_count": thread_count,
    }


def _task_stage_item(
    *,
    task: dict[str, Any],
    run_params: dict[str, Any],
    round_overrides: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "type": "task",
        "task_id": int(task["id"]),
        "task_name": task.get("name"),
        "task_pattern": task.get("task_pattern") or "script",
        "run_params": run_params,
        "round_overrides": round_overrides or None,
        "retry_count": 0,
        "retry_delay_seconds": 10,
        "on_failure": "ABORT",
    }


def _sleep_stage_item(seconds: int, *, skip_final_round: bool = True) -> dict[str, Any]:
    return {
        "type": "postprocessor",
        "post_params": {"sleep_time": max(0, int(seconds))},
        "round_overrides": (
            [{"round": 2, "post_params": {"sleep_time": 0}, "run_params": None}]
            if skip_final_round
            else None
        ),
        "retry_count": 0,
        "retry_delay_seconds": 10,
        "on_failure": "ABORT",
    }


def _plan_payload(
    spec: DemoPlanSpec,
    task_by_slug: dict[str, dict[str, Any]],
    *,
    collaborator_ids: list[int] | None = None,
) -> dict[str, Any]:
    jmeter_task = task_by_slug["openloadhub-demo-jmeter-http-grpc"]
    k6_task = task_by_slug["openloadhub-demo-k6-http-grpc"]
    base_duration = DEMO_DURATION_SECONDS
    interval_seconds = int(os.getenv("OPENLOADHUB_DEMO_PLAN_INTERVAL_SECONDS", "5"))

    if spec.builder == "jmeter_simple":
        stages = [
            {
                "stage": 0,
                "items": [
                    _task_stage_item(
                        task=jmeter_task,
                        run_params=_run_params(
                            duration=base_duration,
                            target_tps=max(1, DEMO_TARGET_TPS),
                            thread_count=max(1, DEMO_JMETER_THREAD_COUNT),
                            pod_count=DEMO_PLAN_POD_COUNT,
                        ),
                        round_overrides=[
                            {"round": 2, "run_params": {"target_tps": max(2, DEMO_TARGET_TPS * 2)}, "post_params": None}
                        ],
                    )
                ],
            },
            {"stage": 1, "items": [_sleep_stage_item(interval_seconds)]},
        ]
    elif spec.builder == "k6_jmeter_advanced":
        stages = [
            {
                "stage": 0,
                "items": [
                    _task_stage_item(
                        task=k6_task,
                        run_params=_run_params(
                            duration=base_duration,
                            target_tps=max(1, DEMO_TARGET_TPS),
                            thread_count=max(1, DEMO_THREAD_COUNT),
                            pod_count=DEMO_ADVANCED_PLAN_TASK_POD_COUNT,
                        ),
                        round_overrides=[
                            {"round": 2, "run_params": {"target_tps": max(2, DEMO_TARGET_TPS * 2)}, "post_params": None}
                        ],
                    ),
                    _task_stage_item(
                        task=jmeter_task,
                        run_params=_run_params(
                            duration=base_duration,
                            target_tps=max(1, DEMO_TARGET_TPS),
                            thread_count=max(1, DEMO_JMETER_THREAD_COUNT),
                            pod_count=DEMO_ADVANCED_PLAN_TASK_POD_COUNT,
                        ),
                        round_overrides=[
                            {"round": 2, "run_params": {"target_tps": max(2, DEMO_TARGET_TPS * 2)}, "post_params": None}
                        ],
                    ),
                ],
            },
            {"stage": 1, "items": [_sleep_stage_item(interval_seconds)]},
        ]
    else:
        raise RuntimeError(f"unknown demo plan builder: {spec.builder}")

    return {
        "name": spec.name,
        "description": spec.description,
        "status": "ready",
        "exec_type": "manual",
        "enable_round": True,
        "total_round": 2,
        "business_lines": ["demo"],
        "collaborator_ids": collaborator_ids,
        "stages": stages,
    }


def _create_plan(token: str, spec: DemoPlanSpec, payload: dict[str, Any]) -> dict[str, Any]:
    data = _api_data(_request("POST", "/plans", token=token, json_body=payload, timeout=20.0))
    if not isinstance(data, dict) or not isinstance(data.get("plan_id"), int):
        raise RuntimeError(f"plan_create_missing_id spec={spec.slug} response={data}")
    return data


def _update_plan(token: str, plan_id: int, spec: DemoPlanSpec, payload: dict[str, Any]) -> dict[str, Any]:
    data = _api_data(
        _request(
            "PUT",
            f"/plans/{plan_id}",
            token=token,
            json_body=payload,
            timeout=20.0,
        )
    )
    if not isinstance(data, dict) or not isinstance(data.get("plan_id"), int):
        raise RuntimeError(f"plan_update_missing_id spec={spec.slug} response={data}")
    return data


def _ensure_plan(
    token: str,
    spec: DemoPlanSpec,
    task_by_slug: dict[str, dict[str, Any]],
    collaborator_ids: list[int] | None = None,
) -> dict[str, Any]:
    payload = _plan_payload(spec, task_by_slug, collaborator_ids=collaborator_ids)
    existing = _find_existing_plan(token, spec)
    if existing is not None:
        plan_id = int(existing["plan_id"])
        _log("plan_exists", slug=spec.slug, plan_id=plan_id)
        updated = _update_plan(token, plan_id, spec, payload)
        _log("plan_refreshed", slug=spec.slug, plan_id=updated.get("plan_id"))
        return updated

    created = _create_plan(token, spec, payload)
    _log("plan_seeded", slug=spec.slug, plan_id=created.get("plan_id"))
    return created


def main() -> int:
    if not PROTO_PATH.exists():
        raise RuntimeError(f"demo proto file not found: {PROTO_PATH}")
    token = _login_with_retry()
    admin_collaborator_ids = _resolve_admin_collaborator_ids()
    seeded = [_ensure_task(token, spec, admin_collaborator_ids) for spec in DEMO_TASKS]
    task_by_slug = {
        str(task.get("properties", {}).get("demo_seed_slug")): task
        for task in seeded
        if isinstance(task.get("properties"), dict)
    }
    missing_task_slugs = sorted(
        spec.slug for spec in DEMO_TASKS if spec.slug not in task_by_slug
    )
    if missing_task_slugs:
        raise RuntimeError(f"seeded task slug missing: {missing_task_slugs}")
    seeded_plans = [
        _ensure_plan(token, spec, task_by_slug, admin_collaborator_ids)
        for spec in DEMO_PLANS
    ]
    _log(
        "seed_complete",
        fixed_demo_agent_count=DEMO_AGENT_COUNT,
        default_plan_pod_count=DEMO_PLAN_POD_COUNT,
        tasks=[
            {
                "id": task.get("id"),
                "name": task.get("name"),
                "status": task.get("status"),
            }
            for task in seeded
        ],
        plans=[
            {
                "id": plan.get("plan_id"),
                "name": plan.get("name"),
                "status": plan.get("status"),
                "task_total": plan.get("task_total"),
                "postprocessor_total": plan.get("postprocessor_total"),
            }
            for plan in seeded_plans
        ],
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        _log("seed_failed", error=str(exc))
        raise
