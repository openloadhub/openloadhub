from __future__ import annotations

import argparse
import json
import os
import resource
import signal
import threading
import time
from concurrent import futures
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import grpc


SERVICE_NAME = "openloadhub-demo-target"
GRPC_SERVICE_NAME = "hello.Hello"
GRPC_SERVICE_ALIASES = (GRPC_SERVICE_NAME, "Hello")
STARTED_AT = time.time()

_STATS_LOCK = threading.Lock()
_RESOURCE_LOCK = threading.Lock()
_STATS: dict[str, int] = {
    "http_requests_total": 0,
    "grpc_requests_total": 0,
}
_HTTP_ROUTE_STATS: dict[tuple[str, str, str], dict[str, float]] = {}
_GRPC_METHOD_STATS: dict[tuple[str, str], dict[str, float]] = {}
_HISTOGRAM_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)


def _process_cpu_seconds(usage: resource.struct_rusage | None = None) -> float:
    sample = usage or resource.getrusage(resource.RUSAGE_SELF)
    return max(0.0, float(sample.ru_utime) + float(sample.ru_stime))


_CPU_SAMPLE = {
    "wall": time.monotonic(),
    "cpu": _process_cpu_seconds(),
    "percent": 0.0,
}

_ORDERS = [
    {
        "id": "demo-order-1001",
        "status": "paid",
        "amount": 128.50,
        "currency": "USD",
        "customer": "demo-buyer-a",
    },
    {
        "id": "demo-order-1002",
        "status": "processing",
        "amount": 64.00,
        "currency": "USD",
        "customer": "demo-buyer-b",
    },
    {
        "id": "demo-order-1003",
        "status": "shipped",
        "amount": 256.75,
        "currency": "USD",
        "customer": "demo-buyer-c",
    },
]


def _increment(metric: str) -> None:
    with _STATS_LOCK:
        _STATS[metric] += 1


def _snapshot_stats() -> dict[str, int]:
    with _STATS_LOCK:
        return dict(_STATS)


def _observe_http(route: str, method: str, status: str, started_at: float) -> None:
    elapsed = max(0.0001, time.perf_counter() - started_at)
    with _STATS_LOCK:
        stats = _HTTP_ROUTE_STATS.setdefault((route, method, status), {"count": 0.0, "sum": 0.0})
        stats["count"] += 1
        stats["sum"] += elapsed


def _observe_grpc(method: str, status: str, started_at: float) -> None:
    elapsed = max(0.0001, time.perf_counter() - started_at)
    with _STATS_LOCK:
        stats = _GRPC_METHOD_STATS.setdefault((method, status), {"count": 0.0, "sum": 0.0})
        stats["count"] += 1
        stats["sum"] += elapsed


def _snapshot_route_stats() -> tuple[dict[tuple[str, str, str], dict[str, float]], dict[tuple[str, str], dict[str, float]]]:
    with _STATS_LOCK:
        return (
            {key: dict(value) for key, value in _HTTP_ROUTE_STATS.items()},
            {key: dict(value) for key, value in _GRPC_METHOD_STATS.items()},
        )


def _sample_process_cpu_percent(usage: resource.struct_rusage | None = None) -> float:
    now = time.monotonic()
    cpu_seconds = _process_cpu_seconds(usage)
    with _RESOURCE_LOCK:
        previous_wall = float(_CPU_SAMPLE["wall"])
        previous_cpu = float(_CPU_SAMPLE["cpu"])
        elapsed = now - previous_wall
        delta_cpu = cpu_seconds - previous_cpu
        if elapsed > 0:
            _CPU_SAMPLE["percent"] = max(0.0, delta_cpu / elapsed * 100.0)
            _CPU_SAMPLE["wall"] = now
            _CPU_SAMPLE["cpu"] = cpu_seconds
        return float(_CPU_SAMPLE["percent"])


def _json_bytes(payload: dict[str, Any] | list[Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _prom_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _encode_varint(value: int) -> bytes:
    encoded = bytearray()
    remaining = int(value)
    while True:
        current = remaining & 0x7F
        remaining >>= 7
        encoded.append(current | 0x80 if remaining else current)
        if not remaining:
            return bytes(encoded)


def _decode_varint(payload: bytes, index: int = 0) -> tuple[int, int]:
    shift = 0
    result = 0
    cursor = index
    while cursor < len(payload):
        current = payload[cursor]
        cursor += 1
        result |= (current & 0x7F) << shift
        if not (current & 0x80):
            return result, cursor
        shift += 7
        if shift > 63:
            raise ValueError("varint is too long")
    raise ValueError("truncated varint")


def _encode_string_field(field_number: int, value: str) -> bytes:
    text = value.encode("utf-8")
    key = (field_number << 3) | 2
    return _encode_varint(key) + _encode_varint(len(text)) + text


def _decode_string_fields(payload: bytes) -> dict[int, str]:
    fields: dict[int, str] = {}
    cursor = 0
    while cursor < len(payload):
        key, cursor = _decode_varint(payload, cursor)
        field_number = key >> 3
        wire_type = key & 0x07
        if wire_type != 2:
            raise ValueError(f"unsupported wire type {wire_type}")
        length, cursor = _decode_varint(payload, cursor)
        end = cursor + length
        if end > len(payload):
            raise ValueError("truncated string field")
        fields[field_number] = payload[cursor:end].decode("utf-8")
        cursor = end
    return fields


def _build_hello_response(payload: bytes, context: grpc.ServicerContext, *, again: bool) -> bytes:
    started_at = time.perf_counter()
    method_name = "SayHelloAgain" if again else "SayHello"
    status = "ok"
    try:
        fields = _decode_string_fields(payload) if payload else {}
        name = fields.get(1) or "world"
        greeting = "Hello again" if again else "Hello"
        return _encode_string_field(1, f"{greeting}, {name}")
    except Exception as exc:
        status = "invalid_argument"
        context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        return b""
    finally:
        _increment("grpc_requests_total")
        _observe_grpc(f"/{GRPC_SERVICE_NAME}/{method_name}", status, started_at)
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        print(
            json.dumps(
                {
                    "event": "grpc_request",
                    "method": f"/{GRPC_SERVICE_NAME}/{method_name}",
                    "duration_ms": round(elapsed_ms, 3),
                },
                sort_keys=True,
            ),
            flush=True,
        )


def _say_hello(payload: bytes, context: grpc.ServicerContext) -> bytes:
    return _build_hello_response(payload, context, again=False)


def _say_hello_again(payload: bytes, context: grpc.ServicerContext) -> bytes:
    return _build_hello_response(payload, context, again=True)


def _build_grpc_server(max_workers: int = 4) -> grpc.Server:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    rpc_handler = grpc.unary_unary_rpc_method_handler(
        _say_hello,
        request_deserializer=lambda payload: payload,
        response_serializer=lambda payload: payload,
    )
    rpc_handler_again = grpc.unary_unary_rpc_method_handler(
        _say_hello_again,
        request_deserializer=lambda payload: payload,
        response_serializer=lambda payload: payload,
    )
    generic_handlers = [
        grpc.method_handlers_generic_handler(
            service_name,
            {"SayHello": rpc_handler, "SayHelloAgain": rpc_handler_again},
        )
        for service_name in GRPC_SERVICE_ALIASES
    ]
    server.add_generic_rpc_handlers(tuple(generic_handlers))
    return server


def _render_metrics() -> bytes:
    stats = _snapshot_stats()
    http_route_stats, grpc_method_stats = _snapshot_route_stats()
    uptime = max(0.0, time.time() - STARTED_AT)
    usage = resource.getrusage(resource.RUSAGE_SELF)
    cpu_percent = _sample_process_cpu_percent(usage)
    rss_bytes = max(0, int(usage.ru_maxrss)) * 1024
    try:
        fs = os.statvfs("/")
        fs_total = float(fs.f_blocks * fs.f_frsize)
        fs_free = float(fs.f_bavail * fs.f_frsize)
        fs_used = max(0.0, fs_total - fs_free)
    except OSError:
        fs_total = 1.0
        fs_free = 0.0
        fs_used = 1.0
    dependency_count = max(1, int(uptime // 15))
    lines = [
        "# HELP openloadhub_demo_target_http_requests_total Total HTTP requests handled by the demo target.",
        "# TYPE openloadhub_demo_target_http_requests_total counter",
        f"openloadhub_demo_target_http_requests_total {stats['http_requests_total']}",
        "# HELP openloadhub_demo_target_grpc_requests_total Total gRPC requests handled by the demo target.",
        "# TYPE openloadhub_demo_target_grpc_requests_total counter",
        f"openloadhub_demo_target_grpc_requests_total {stats['grpc_requests_total']}",
        "# HELP openloadhub_demo_target_uptime_seconds Demo target process uptime.",
        "# TYPE openloadhub_demo_target_uptime_seconds gauge",
        f"openloadhub_demo_target_uptime_seconds {uptime:.3f}",
    ]
    lines.extend([
        "# HELP target_service_http_requests_total Total HTTP requests handled by the demo target.",
        "# TYPE target_service_http_requests_total counter",
    ])
    for (route, method, status), item in sorted(http_route_stats.items()):
        labels = f'route="{_prom_label(route)}",method="{_prom_label(method)}",status="{_prom_label(status)}",target_instance="openloadhub-demo-target"'
        lines.append(f"target_service_http_requests_total{{{labels}}} {item['count']:.0f}")
    lines.extend([
        "# HELP target_service_http_request_duration_seconds HTTP request duration for demo target routes.",
        "# TYPE target_service_http_request_duration_seconds histogram",
    ])
    for (route, method, status), item in sorted(http_route_stats.items()):
        count = item["count"]
        total = item["sum"]
        labels = f'route="{_prom_label(route)}",method="{_prom_label(method)}",status="{_prom_label(status)}",target_instance="openloadhub-demo-target"'
        for bucket in _HISTOGRAM_BUCKETS:
            bucket_count = count if bucket >= 0.25 else max(0.0, count * min(1.0, bucket / 0.25))
            lines.append(f'target_service_http_request_duration_seconds_bucket{{{labels},le="{bucket:g}"}} {bucket_count:.0f}')
        lines.append(f'target_service_http_request_duration_seconds_bucket{{{labels},le="+Inf"}} {count:.0f}')
        lines.append(f"target_service_http_request_duration_seconds_count{{{labels}}} {count:.0f}")
        lines.append(f"target_service_http_request_duration_seconds_sum{{{labels}}} {total:.6f}")
    lines.extend([
        "# HELP target_service_grpc_requests_total Total gRPC requests handled by the demo target.",
        "# TYPE target_service_grpc_requests_total counter",
    ])
    for (method, status), item in sorted(grpc_method_stats.items()):
        labels = f'method="{_prom_label(method)}",status="{_prom_label(status)}",target_instance="openloadhub-demo-target"'
        lines.append(f"target_service_grpc_requests_total{{{labels}}} {item['count']:.0f}")
    lines.extend([
        "# HELP target_service_grpc_request_duration_seconds gRPC request duration for demo target methods.",
        "# TYPE target_service_grpc_request_duration_seconds histogram",
    ])
    for (method, status), item in sorted(grpc_method_stats.items()):
        count = item["count"]
        total = item["sum"]
        labels = f'method="{_prom_label(method)}",status="{_prom_label(status)}",target_instance="openloadhub-demo-target"'
        for bucket in _HISTOGRAM_BUCKETS:
            bucket_count = count if bucket >= 0.05 else max(0.0, count * min(1.0, bucket / 0.05))
            lines.append(f'target_service_grpc_request_duration_seconds_bucket{{{labels},le="{bucket:g}"}} {bucket_count:.0f}')
        lines.append(f'target_service_grpc_request_duration_seconds_bucket{{{labels},le="+Inf"}} {count:.0f}')
        lines.append(f"target_service_grpc_request_duration_seconds_count{{{labels}}} {count:.0f}")
        lines.append(f"target_service_grpc_request_duration_seconds_sum{{{labels}}} {total:.6f}")
    lines.extend([
        "# HELP target_service_dependency_checks_total Total dependency checks observed by the demo target.",
        "# TYPE target_service_dependency_checks_total counter",
        f'target_service_dependency_checks_total{{kind="redis",status="ok",target_instance="openloadhub-demo-target"}} {dependency_count}',
        f'target_service_dependency_checks_total{{kind="mysql",status="ok",target_instance="openloadhub-demo-target"}} {dependency_count}',
        "# HELP target_service_dependency_last_latency_seconds Last observed dependency latency for the demo target.",
        "# TYPE target_service_dependency_last_latency_seconds gauge",
        'target_service_dependency_last_latency_seconds{kind="redis",target_instance="openloadhub-demo-target"} 0.003',
        'target_service_dependency_last_latency_seconds{kind="mysql",target_instance="openloadhub-demo-target"} 0.006',
        "# HELP target_service_process_cpu_percent Process CPU percent for the demo target.",
        "# TYPE target_service_process_cpu_percent gauge",
        f'target_service_process_cpu_percent{{target_instance="openloadhub-demo-target"}} {cpu_percent:.6f}',
        "# HELP target_service_process_resident_memory_bytes Resident memory for the demo target process.",
        "# TYPE target_service_process_resident_memory_bytes gauge",
        f'target_service_process_resident_memory_bytes{{target_instance="openloadhub-demo-target"}} {rss_bytes}',
        "# HELP target_service_runtime_memory_total_bytes Runtime memory total bytes visible to the demo target.",
        "# TYPE target_service_runtime_memory_total_bytes gauge",
        f'target_service_runtime_memory_total_bytes{{target_instance="openloadhub-demo-target"}} {max(float(rss_bytes) * 8, 1.0):.0f}',
        "# HELP target_service_network_receive_bytes_total Total network receive bytes observed from demo target runtime.",
        "# TYPE target_service_network_receive_bytes_total counter",
        f'target_service_network_receive_bytes_total{{target_instance="openloadhub-demo-target"}} {stats["http_requests_total"] * 512 + stats["grpc_requests_total"] * 256}',
        "# HELP target_service_network_transmit_bytes_total Total network transmit bytes observed from demo target runtime.",
        "# TYPE target_service_network_transmit_bytes_total counter",
        f'target_service_network_transmit_bytes_total{{target_instance="openloadhub-demo-target"}} {stats["http_requests_total"] * 768 + stats["grpc_requests_total"] * 512}',
        "# HELP target_service_disk_read_bytes_total Total disk read bytes observed from demo target process.",
        "# TYPE target_service_disk_read_bytes_total counter",
        f'target_service_disk_read_bytes_total{{target_instance="openloadhub-demo-target"}} {stats["http_requests_total"] * 16}',
        "# HELP target_service_disk_write_bytes_total Total disk write bytes observed from demo target process.",
        "# TYPE target_service_disk_write_bytes_total counter",
        f'target_service_disk_write_bytes_total{{target_instance="openloadhub-demo-target"}} {stats["grpc_requests_total"] * 16}',
        "# HELP target_service_filesystem_total_bytes Filesystem total bytes for demo target runtime root path.",
        "# TYPE target_service_filesystem_total_bytes gauge",
        f'target_service_filesystem_total_bytes{{target_instance="openloadhub-demo-target"}} {fs_total:.0f}',
        "# HELP target_service_filesystem_used_bytes Filesystem used bytes for demo target runtime root path.",
        "# TYPE target_service_filesystem_used_bytes gauge",
        f'target_service_filesystem_used_bytes{{target_instance="openloadhub-demo-target"}} {fs_used:.0f}',
        "# HELP target_service_filesystem_free_bytes Filesystem free bytes for demo target runtime root path.",
        "# TYPE target_service_filesystem_free_bytes gauge",
        f'target_service_filesystem_free_bytes{{target_instance="openloadhub-demo-target"}} {fs_free:.0f}',
        "# HELP target_service_http_requests_total Total HTTP requests handled by demo-target.",
        "# TYPE target_service_http_requests_total counter",
    ])
    for (route, method, status), item in sorted(http_route_stats.items()):
        labels = f'route="{_prom_label(route)}",method="{_prom_label(method)}",status="{_prom_label(status)}"'
        lines.append(f"target_service_http_requests_total{{{labels}}} {item['count']:.0f}")
    lines.extend([
        "# HELP target_service_http_request_duration_seconds HTTP request duration for demo-target routes.",
        "# TYPE target_service_http_request_duration_seconds histogram",
    ])
    for (route, method, status), item in sorted(http_route_stats.items()):
        count = item["count"]
        total = item["sum"]
        labels = f'route="{_prom_label(route)}",method="{_prom_label(method)}",status="{_prom_label(status)}"'
        for bucket in _HISTOGRAM_BUCKETS:
            bucket_count = count if bucket >= 0.25 else max(0.0, count * min(1.0, bucket / 0.25))
            lines.append(f'target_service_http_request_duration_seconds_bucket{{{labels},le="{bucket:g}"}} {bucket_count:.0f}')
        lines.append(f'target_service_http_request_duration_seconds_bucket{{{labels},le="+Inf"}} {count:.0f}')
        lines.append(f"target_service_http_request_duration_seconds_count{{{labels}}} {count:.0f}")
        lines.append(f"target_service_http_request_duration_seconds_sum{{{labels}}} {total:.6f}")
    lines.extend([
        "# HELP target_service_grpc_requests_total Total gRPC requests handled by demo-target.",
        "# TYPE target_service_grpc_requests_total counter",
    ])
    for (method, status), item in sorted(grpc_method_stats.items()):
        labels = f'method="{_prom_label(method)}",status="{_prom_label(status)}"'
        lines.append(f"target_service_grpc_requests_total{{{labels}}} {item['count']:.0f}")
    lines.extend([
        "# HELP target_service_grpc_request_duration_seconds gRPC request duration for demo-target methods.",
        "# TYPE target_service_grpc_request_duration_seconds histogram",
    ])
    for (method, status), item in sorted(grpc_method_stats.items()):
        count = item["count"]
        total = item["sum"]
        labels = f'method="{_prom_label(method)}",status="{_prom_label(status)}"'
        for bucket in _HISTOGRAM_BUCKETS:
            bucket_count = count if bucket >= 0.05 else max(0.0, count * min(1.0, bucket / 0.05))
            lines.append(f'target_service_grpc_request_duration_seconds_bucket{{{labels},le="{bucket:g}"}} {bucket_count:.0f}')
        lines.append(f'target_service_grpc_request_duration_seconds_bucket{{{labels},le="+Inf"}} {count:.0f}')
        lines.append(f"target_service_grpc_request_duration_seconds_count{{{labels}}} {count:.0f}")
        lines.append(f"target_service_grpc_request_duration_seconds_sum{{{labels}}} {total:.6f}")
    lines.extend([
        "# HELP target_service_dependency_checks_total Total dependency checks observed by demo-target.",
        "# TYPE target_service_dependency_checks_total counter",
        f'target_service_dependency_checks_total{{kind="redis",status="ok"}} {dependency_count}',
        f'target_service_dependency_checks_total{{kind="mysql",status="ok"}} {dependency_count}',
        "# HELP target_service_dependency_last_latency_seconds Last observed dependency latency for demo-target.",
        "# TYPE target_service_dependency_last_latency_seconds gauge",
        'target_service_dependency_last_latency_seconds{kind="redis"} 0.003',
        'target_service_dependency_last_latency_seconds{kind="mysql"} 0.006',
        "# HELP target_service_process_cpu_percent Process CPU percent for demo-target.",
        "# TYPE target_service_process_cpu_percent gauge",
        f"target_service_process_cpu_percent {cpu_percent:.6f}",
        "# HELP target_service_process_resident_memory_bytes Resident memory for demo-target process.",
        "# TYPE target_service_process_resident_memory_bytes gauge",
        f"target_service_process_resident_memory_bytes {rss_bytes}",
        "# HELP target_service_runtime_memory_total_bytes Runtime memory total bytes visible to demo-target.",
        "# TYPE target_service_runtime_memory_total_bytes gauge",
        f"target_service_runtime_memory_total_bytes {max(float(rss_bytes) * 8, 1.0):.0f}",
        "# HELP target_service_network_receive_bytes_total Total network receive bytes observed from demo-target runtime.",
        "# TYPE target_service_network_receive_bytes_total counter",
        f"target_service_network_receive_bytes_total {stats['http_requests_total'] * 512 + stats['grpc_requests_total'] * 256}",
        "# HELP target_service_network_transmit_bytes_total Total network transmit bytes observed from demo-target runtime.",
        "# TYPE target_service_network_transmit_bytes_total counter",
        f"target_service_network_transmit_bytes_total {stats['http_requests_total'] * 768 + stats['grpc_requests_total'] * 512}",
        "# HELP target_service_disk_read_bytes_total Total disk read bytes observed from demo-target process.",
        "# TYPE target_service_disk_read_bytes_total counter",
        f"target_service_disk_read_bytes_total {stats['http_requests_total'] * 16}",
        "# HELP target_service_disk_write_bytes_total Total disk write bytes observed from demo-target process.",
        "# TYPE target_service_disk_write_bytes_total counter",
        f"target_service_disk_write_bytes_total {stats['grpc_requests_total'] * 16}",
        "# HELP target_service_filesystem_total_bytes Filesystem total bytes for demo-target runtime root path.",
        "# TYPE target_service_filesystem_total_bytes gauge",
        f"target_service_filesystem_total_bytes {fs_total:.0f}",
        "# HELP target_service_filesystem_used_bytes Filesystem used bytes for demo-target runtime root path.",
        "# TYPE target_service_filesystem_used_bytes gauge",
        f"target_service_filesystem_used_bytes {fs_used:.0f}",
        "# HELP target_service_filesystem_free_bytes Filesystem free bytes for demo-target runtime root path.",
        "# TYPE target_service_filesystem_free_bytes gauge",
        f"target_service_filesystem_free_bytes {fs_free:.0f}",
        "",
    ])
    return "\n".join(lines).encode("utf-8")


class DemoTargetHandler(BaseHTTPRequestHandler):
    server_version = "OpenLoadHubDemoTarget/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(
            json.dumps(
                {
                    "event": "http_access",
                    "client": self.client_address[0],
                    "message": fmt % args,
                },
                sort_keys=True,
            ),
            flush=True,
        )

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any] | list[Any]) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        started_at = time.perf_counter()
        _increment("http_requests_total")
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            _observe_http("/health", "GET", "200", started_at)
            self._send_json(
                HTTPStatus.OK,
                {
                    "service": SERVICE_NAME,
                    "status": "ok",
                    "uptime_seconds": round(max(0.0, time.time() - STARTED_AT), 3),
                },
            )
            return
        if parsed.path == "/api/ping":
            _observe_http("/api/ping", "GET", "200", started_at)
            self._send_json(
                HTTPStatus.OK,
                {
                    "message": "pong",
                    "service": SERVICE_NAME,
                    "timestamp": int(time.time()),
                },
            )
            return
        if parsed.path == "/api/orders":
            _observe_http("/api/orders", "GET", "200", started_at)
            self._send_json(HTTPStatus.OK, {"items": self._filtered_orders(parsed.query)})
            return
        if parsed.path == "/metrics":
            self._send_text(HTTPStatus.OK, _render_metrics(), "text/plain; version=0.0.4")
            return
        _observe_http(parsed.path or "/", "GET", "404", started_at)
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:
        started_at = time.perf_counter()
        _increment("http_requests_total")
        parsed = urlparse(self.path)
        if parsed.path != "/api/orders":
            _observe_http(parsed.path or "/", "POST", "404", started_at)
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return

        content_length = int(self.headers.get("Content-Length", "0") or "0")
        raw_body = self.rfile.read(content_length) if content_length else b"{}"
        try:
            body = json.loads(raw_body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            _observe_http("/api/orders", "POST", "400", started_at)
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
            return

        item = {
            "id": "demo-order-created",
            "status": body.get("status") or "accepted",
            "amount": float(body.get("amount") or 1.0),
            "currency": body.get("currency") or "USD",
            "customer": body.get("customer") or "demo-buyer-new",
        }
        _observe_http("/api/orders", "POST", "201", started_at)
        self._send_json(HTTPStatus.CREATED, item)

    def _filtered_orders(self, query: str) -> list[dict[str, Any]]:
        params = parse_qs(query)
        status = (params.get("status") or [""])[0]
        limit_text = (params.get("limit") or ["3"])[0]
        try:
            limit = max(1, min(20, int(limit_text)))
        except ValueError:
            limit = 3
        orders = [order for order in _ORDERS if not status or order["status"] == status]
        return orders[:limit]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OpenLoadHub public demo target service.")
    parser.add_argument(
        "--http-port",
        type=int,
        default=int(os.getenv("DEMO_TARGET_HTTP_PORT", "8080")),
    )
    parser.add_argument(
        "--grpc-port",
        type=int,
        default=int(os.getenv("DEMO_TARGET_GRPC_PORT", "50051")),
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    stop_event = threading.Event()

    http_server = ThreadingHTTPServer(("0.0.0.0", args.http_port), DemoTargetHandler)
    http_thread = threading.Thread(
        target=http_server.serve_forever,
        name="http-server",
        daemon=True,
    )
    http_thread.start()

    grpc_server = _build_grpc_server()
    bound_port = grpc_server.add_insecure_port(f"0.0.0.0:{args.grpc_port}")
    if bound_port != args.grpc_port:
        raise RuntimeError(f"failed to bind gRPC port {args.grpc_port}")
    grpc_server.start()

    def _stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    print(
        json.dumps(
            {
                "event": "started",
                "service": SERVICE_NAME,
                "http_port": args.http_port,
                "grpc_port": args.grpc_port,
            },
            sort_keys=True,
        ),
        flush=True,
    )

    stop_event.wait()
    grpc_server.stop(grace=2).wait()
    http_server.shutdown()
    http_server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
