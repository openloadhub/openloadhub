import asyncio
import csv
import ctypes
import ctypes.util
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pathlib import Path
import json
import os
import shutil
import socket
import subprocess
import sys

try:
    import psutil
except ImportError:  # pragma: no cover - dependency guard
    psutil = None


class _RUsageInfoV4(ctypes.Structure):
    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64),
        ("ri_proc_exit_abstime", ctypes.c_uint64),
        ("ri_child_user_time", ctypes.c_uint64),
        ("ri_child_system_time", ctypes.c_uint64),
        ("ri_child_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_child_interrupt_wkups", ctypes.c_uint64),
        ("ri_child_pageins", ctypes.c_uint64),
        ("ri_child_elapsed_abstime", ctypes.c_uint64),
        ("ri_diskio_bytesread", ctypes.c_uint64),
        ("ri_diskio_byteswritten", ctypes.c_uint64),
        ("ri_cpu_time_qos_default", ctypes.c_uint64),
        ("ri_cpu_time_qos_maintenance", ctypes.c_uint64),
        ("ri_cpu_time_qos_background", ctypes.c_uint64),
        ("ri_cpu_time_qos_utility", ctypes.c_uint64),
        ("ri_cpu_time_qos_compat", ctypes.c_uint64),
        ("ri_cpu_time_qos_user_initiated", ctypes.c_uint64),
        ("ri_cpu_time_qos_user_interactive", ctypes.c_uint64),
        ("ri_billed_system_time", ctypes.c_uint64),
        ("ri_serviced_system_time", ctypes.c_uint64),
        ("ri_logical_writes", ctypes.c_uint64),
        ("ri_lifetime_max_phys_footprint", ctypes.c_uint64),
        ("ri_instructions", ctypes.c_uint64),
        ("ri_cycles", ctypes.c_uint64),
        ("ri_billed_energy", ctypes.c_uint64),
        ("ri_serviced_energy", ctypes.c_uint64),
        ("ri_interval_max_phys_footprint", ctypes.c_uint64),
        ("ri_runnable_time", ctypes.c_uint64),
    ]


_RUSAGE_INFO_V4 = 4
_LIBPROC: Optional[ctypes.CDLL] = None


def _is_host_runtime_identity(agent_ip: Optional[str], pod_name: Optional[str]) -> bool:
    normalized_ip = str(agent_ip or "").strip().lower()
    if normalized_ip in {"127.0.0.1", "::1", "localhost"}:
        return True
    normalized_name = str(pod_name or "").strip().lower()
    if "." in normalized_name:
        return True
    return False


def _build_agent_host_label(agent_ip: Optional[str]) -> Optional[str]:
    normalized_ip = str(agent_ip or "").strip()
    if not normalized_ip:
        return None
    if normalized_ip.count(":") == 1:
        host_part, port_part = normalized_ip.rsplit(":", 1)
        if host_part and port_part.isdigit():
            return normalized_ip
    port = str(os.getenv("AGENT_PORT") or os.getenv("PORT") or "").strip()
    if port.isdigit():
        return f"{normalized_ip}:{port}"
    return normalized_ip


def _collect_process_tree(root_pid: Optional[int]) -> list[Any]:
    if psutil is None or root_pid is None or root_pid <= 0:
        return []
    try:
        root = psutil.Process(root_pid)
    except Exception:
        return []
    processes = [root]
    try:
        processes.extend(root.children(recursive=True))
    except Exception:
        pass
    return processes


def _load_libproc() -> Optional[ctypes.CDLL]:
    global _LIBPROC
    if _LIBPROC is not None:
        return _LIBPROC
    library_path = ctypes.util.find_library("proc")
    if not library_path:
        return None
    try:
        _LIBPROC = ctypes.CDLL(library_path)
    except Exception:
        _LIBPROC = None
        return None
    _LIBPROC.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(_RUsageInfoV4)]
    _LIBPROC.proc_pid_rusage.restype = ctypes.c_int
    return _LIBPROC


def _read_process_disk_io_totals_darwin(pid: Optional[int]) -> tuple[Optional[float], Optional[float]]:
    if sys.platform != "darwin" or pid is None or pid <= 0:
        return (None, None)
    libproc = _load_libproc()
    if libproc is None:
        return (None, None)
    usage = _RUsageInfoV4()
    try:
        rc = libproc.proc_pid_rusage(int(pid), _RUSAGE_INFO_V4, ctypes.byref(usage))
    except Exception:
        return (None, None)
    if rc != 0:
        return (None, None)
    return (float(usage.ri_diskio_bytesread), float(usage.ri_diskio_byteswritten))


def _read_process_tree_disk_io_totals(root_pid: Optional[int]) -> tuple[Optional[float], Optional[float]]:
    processes = _collect_process_tree(root_pid)
    if not processes:
        return (None, None)

    total_read = 0.0
    total_write = 0.0
    matched = False
    for proc in processes:
        matched_this_process = False
        io_counters_getter = getattr(proc, "io_counters", None)
        if callable(io_counters_getter):
            try:
                counters = io_counters_getter()
            except Exception:
                counters = None
            if counters is not None:
                read_bytes = getattr(counters, "read_bytes", None)
                write_bytes = getattr(counters, "write_bytes", None)
                if isinstance(read_bytes, (int, float)):
                    total_read += float(read_bytes)
                    matched = True
                    matched_this_process = True
                if isinstance(write_bytes, (int, float)):
                    total_write += float(write_bytes)
                    matched = True
                    matched_this_process = True
                if matched_this_process:
                    continue

        pid = getattr(proc, "pid", None)
        read_bytes, write_bytes = _read_process_disk_io_totals_darwin(pid)
        if read_bytes is None and write_bytes is None:
            continue
        if read_bytes is not None:
            total_read += float(read_bytes)
            matched = True
        if write_bytes is not None:
            total_write += float(write_bytes)
            matched = True

    if not matched:
        return (None, None)
    return (total_read, total_write)


def _parse_nettop_process_identifier(raw_value: str) -> Optional[int]:
    value = str(raw_value or "").strip()
    if not value:
        return None
    _, _, pid_part = value.rpartition(".")
    if pid_part.isdigit():
        return int(pid_part)
    return None


def _read_process_tree_network_totals(
    root_pid: Optional[int],
) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    processes = _collect_process_tree(root_pid)
    if not processes or shutil.which("nettop") is None:
        return (None, None, None, None)

    process_pids = {
        int(proc.pid)
        for proc in processes
        if isinstance(getattr(proc, "pid", None), int) and proc.pid > 0
    }
    if not process_pids:
        return (None, None, None, None)

    command = [
        "nettop",
        "-P",
        "-x",
        "-L",
        "1",
        "-J",
        "bytes_in,bytes_out,packets_in,packets_out",
    ]
    for pid in sorted(process_pids):
        command.extend(["-p", str(pid)])

    try:
        output = subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return (None, None, None, None)

    reader = csv.reader(output.splitlines())
    try:
        header = next(row for row in reader if row)
    except StopIteration:
        return (None, None, None, None)

    column_names = [str(cell or "").strip() for cell in header]
    bytes_in_index = column_names.index("bytes_in") if "bytes_in" in column_names else -1
    bytes_out_index = column_names.index("bytes_out") if "bytes_out" in column_names else -1
    packets_in_index = column_names.index("packets_in") if "packets_in" in column_names else -1
    packets_out_index = column_names.index("packets_out") if "packets_out" in column_names else -1
    if min(bytes_in_index, bytes_out_index, packets_in_index, packets_out_index) < 0:
        return (None, None, None, None)

    total_bytes_in = 0.0
    total_bytes_out = 0.0
    total_packets_in = 0.0
    total_packets_out = 0.0
    matched = False

    for row in reader:
        if not row:
            continue
        pid = _parse_nettop_process_identifier(row[0] if row else "")
        if pid not in process_pids:
            continue

        def _read_value(index: int) -> float:
            if index >= len(row):
                return 0.0
            raw_value = str(row[index] or "").strip()
            return float(raw_value) if raw_value else 0.0

        total_bytes_in += _read_value(bytes_in_index)
        total_bytes_out += _read_value(bytes_out_index)
        total_packets_in += _read_value(packets_in_index)
        total_packets_out += _read_value(packets_out_index)
        matched = True

    if not matched:
        return (None, None, None, None)
    return (total_bytes_in, total_bytes_out, total_packets_in, total_packets_out)


def _read_process_tree_cpu_time_seconds(root_pid: Optional[int]) -> Optional[float]:
    processes = _collect_process_tree(root_pid)
    if not processes:
        return None
    total = 0.0
    matched = False
    for proc in processes:
        try:
            cpu_times = proc.cpu_times()
        except Exception:
            continue
        total += float(getattr(cpu_times, "user", 0.0) or 0.0)
        total += float(getattr(cpu_times, "system", 0.0) or 0.0)
        matched = True
    return total if matched else None


def _read_process_tree_memory_snapshot(
    root_pid: Optional[int],
) -> tuple[Optional[float], Optional[float]]:
    processes = _collect_process_tree(root_pid)
    if not processes:
        return (None, None)
    rss_total = 0.0
    matched = False
    for proc in processes:
        try:
            memory_info = proc.memory_info()
        except Exception:
            continue
        rss_total += float(getattr(memory_info, "rss", 0.0) or 0.0)
        matched = True
    if not matched:
        return (None, None)
    _, _, host_total = _read_host_memory_usage_snapshot()
    if host_total and host_total > 0:
        return (rss_total / host_total * 100.0, rss_total)
    return (None, rss_total)


def _read_process_tree_socket_count(root_pid: Optional[int]) -> Optional[float]:
    processes = _collect_process_tree(root_pid)
    if not processes:
        return None
    total = 0
    matched = False
    for proc in processes:
        try:
            getter = getattr(proc, "net_connections", None)
            if getter is None:
                getter = getattr(proc, "connections", None)
            if getter is None:
                continue
            total += len(getter(kind="inet"))
            matched = True
        except Exception:
            continue
    if not matched:
        return None
    return float(total)


def _read_disk_usage_snapshot(
    path: str = "/",
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return (None, None, None)
    used_bytes = float(usage.used)
    total_bytes = float(usage.total)
    if usage.total <= 0:
        return (None, used_bytes, total_bytes)
    return (float((usage.used / usage.total) * 100), used_bytes, total_bytes)


def _read_disk_usage_percent(path: str = "/") -> Optional[float]:
    percent, _, _ = _read_disk_usage_snapshot(path)
    return percent


def _read_network_packet_totals(proc_net_dev_path: str = "/proc/net/dev") -> tuple[Optional[float], Optional[float]]:
    try:
        content = Path(proc_net_dev_path).read_text(encoding="utf-8")
    except OSError:
        if psutil is None:
            return (None, None)
        try:
            counters = psutil.net_io_counters()
        except Exception:
            return (None, None)
        return (float(counters.packets_recv), float(counters.packets_sent))

    preferred_totals = [0.0, 0.0]
    fallback_totals = [0.0, 0.0]
    preferred_found = False
    fallback_found = False

    for raw_line in content.splitlines()[2:]:
        if ":" not in raw_line:
            continue
        interface, payload = raw_line.split(":", 1)
        fields = payload.split()
        if len(fields) < 10:
            continue
        try:
            rx_packets = float(fields[1])
            tx_packets = float(fields[9])
        except ValueError:
            continue

        fallback_totals[0] += rx_packets
        fallback_totals[1] += tx_packets
        fallback_found = True

        if interface.strip() == "lo":
            continue

        preferred_totals[0] += rx_packets
        preferred_totals[1] += tx_packets
        preferred_found = True

    if preferred_found:
        return tuple(preferred_totals)
    if fallback_found:
        return tuple(fallback_totals)
    return (None, None)


def _read_network_byte_totals(proc_net_dev_path: str = "/proc/net/dev") -> tuple[Optional[float], Optional[float]]:
    try:
        content = Path(proc_net_dev_path).read_text(encoding="utf-8")
    except OSError:
        if psutil is None:
            return (None, None)
        try:
            counters = psutil.net_io_counters()
        except Exception:
            return (None, None)
        return (float(counters.bytes_recv), float(counters.bytes_sent))

    preferred_totals = [0.0, 0.0]
    fallback_totals = [0.0, 0.0]
    preferred_found = False
    fallback_found = False

    for raw_line in content.splitlines()[2:]:
        if ":" not in raw_line:
            continue
        interface, payload = raw_line.split(":", 1)
        fields = payload.split()
        if len(fields) < 9:
            continue
        try:
            rx_bytes = float(fields[0])
            tx_bytes = float(fields[8])
        except ValueError:
            continue

        fallback_totals[0] += rx_bytes
        fallback_totals[1] += tx_bytes
        fallback_found = True

        if interface.strip() == "lo":
            continue

        preferred_totals[0] += rx_bytes
        preferred_totals[1] += tx_bytes
        preferred_found = True

    if preferred_found:
        return tuple(preferred_totals)
    if fallback_found:
        return tuple(fallback_totals)
    return (None, None)


def _read_disk_io_totals(
    diskstats_path: str = "/proc/diskstats",
) -> tuple[Optional[float], Optional[float]]:
    try:
        content = Path(diskstats_path).read_text(encoding="utf-8")
    except OSError:
        if psutil is None:
            return (None, None)
        try:
            counters = psutil.disk_io_counters()
        except Exception:
            return (None, None)
        if counters is None:
            return (None, None)
        return (float(counters.read_bytes), float(counters.write_bytes))

    read_bytes_total = 0.0
    write_bytes_total = 0.0
    matched = False

    for raw_line in content.splitlines():
        fields = raw_line.split()
        if len(fields) < 14:
            continue
        device = fields[2]
        if device.startswith(("loop", "ram", "fd", "sr", "dm-")):
            continue
        # Keep whole-disk devices only to avoid double-counting partitions.
        if device.startswith("nvme"):
            if "p" in device:
                continue
        elif device[-1:].isdigit():
            continue
        try:
            sectors_read = float(fields[5])
            sectors_written = float(fields[9])
        except ValueError:
            continue
        read_bytes_total += sectors_read * 512.0
        write_bytes_total += sectors_written * 512.0
        matched = True

    if not matched:
        return (None, None)
    return (read_bytes_total, write_bytes_total)


def _read_host_memory_usage_snapshot(
    meminfo_path: str = "/proc/meminfo",
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    try:
        content = Path(meminfo_path).read_text(encoding="utf-8")
    except OSError:
        if psutil is None:
            return (None, None, None)
        try:
            memory = psutil.virtual_memory()
        except Exception:
            return (None, None, None)
        return (float(memory.percent), float(memory.used), float(memory.total))

    values: Dict[str, float] = {}
    for raw_line in content.splitlines():
        if ":" not in raw_line:
            continue
        key, payload = raw_line.split(":", 1)
        fields = payload.strip().split()
        if not fields:
            continue
        try:
            values[key] = float(fields[0]) * 1024.0
        except ValueError:
            continue

    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if total is None or available is None or total <= 0:
        return (None, None, total)

    used = max(0.0, total - available)
    return (used / total * 100.0, used, total)


def _read_cgroup_memory_usage_snapshot(
    current_path: str = "/sys/fs/cgroup/memory.current",
    max_path: str = "/sys/fs/cgroup/memory.max",
) -> tuple[Optional[float], Optional[float]]:
    try:
        used_raw = Path(current_path).read_text(encoding="utf-8").strip()
        limit_raw = Path(max_path).read_text(encoding="utf-8").strip()
    except OSError:
        return (None, None)

    try:
        used = float(used_raw)
    except ValueError:
        return (None, None)

    if used < 0:
        return (None, None)

    if limit_raw and limit_raw.lower() != "max":
        try:
            limit = float(limit_raw)
        except ValueError:
            limit = None
        if limit and limit > 0:
            return (used / limit * 100.0, used)

    _, _, host_total = _read_host_memory_usage_snapshot()
    if host_total and host_total > 0:
        return (used / host_total * 100.0, used)
    return (None, used)


def _read_memory_usage_snapshot(
    meminfo_path: str = "/proc/meminfo",
) -> tuple[Optional[float], Optional[float]]:
    cgroup_percent, cgroup_used = _read_cgroup_memory_usage_snapshot()
    if cgroup_percent is not None or cgroup_used is not None:
        return (cgroup_percent, cgroup_used)
    host_percent, host_used, _ = _read_host_memory_usage_snapshot(meminfo_path)
    return (host_percent, host_used)


def _read_socket_count(sockstat_path: str = "/proc/net/sockstat") -> Optional[float]:
    try:
        content = Path(sockstat_path).read_text(encoding="utf-8")
    except OSError:
        if psutil is not None:
            try:
                return float(len(psutil.net_connections(kind="inet")))
            except Exception:
                pass
        try:
            output = subprocess.check_output(
                ["netstat", "-an"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            return None
        count = 0
        for raw_line in output.splitlines():
            if raw_line.strip().lower().startswith(("tcp", "udp")):
                count += 1
        return float(count) if count > 0 else None

    for raw_line in content.splitlines():
        if not raw_line.startswith("sockets:"):
            continue
        fields = raw_line.split()
        if len(fields) < 3 or fields[1] != "used":
            continue
        try:
            return float(fields[2])
        except ValueError:
            return None
    return None


def _read_cpu_load() -> Optional[float]:
    try:
        load1, _, _ = os.getloadavg()
    except (AttributeError, OSError):
        return None
    return float(load1)


def _read_cgroup_cpu_usage_counter(
    cpu_stat_path: str = "/sys/fs/cgroup/cpu.stat",
    cpu_max_path: str = "/sys/fs/cgroup/cpu.max",
) -> tuple[Optional[float], Optional[float]]:
    try:
        stat_content = Path(cpu_stat_path).read_text(encoding="utf-8")
    except OSError:
        return (None, None)

    usage_usec: Optional[float] = None
    for raw_line in stat_content.splitlines():
        parts = raw_line.split()
        if len(parts) != 2:
            continue
        if parts[0] != "usage_usec":
            continue
        try:
            usage_usec = float(parts[1])
        except ValueError:
            return (None, None)
        break

    if usage_usec is None:
        return (None, None)

    quota_cores: Optional[float] = None
    try:
        cpu_max = Path(cpu_max_path).read_text(encoding="utf-8").strip().split()
    except OSError:
        cpu_max = []
    if len(cpu_max) >= 2 and cpu_max[0].lower() != "max":
        try:
            quota = float(cpu_max[0])
            period = float(cpu_max[1])
        except ValueError:
            quota = period = 0.0
        if quota > 0 and period > 0:
            quota_cores = quota / period

    return (usage_usec, quota_cores)


def _read_cpu_usage_percent() -> Optional[float]:
    if psutil is None:
        return None
    try:
        return float(psutil.cpu_percent(interval=None))
    except Exception:
        return None


@dataclass
class RunLog:
    seq: int
    ts: datetime
    level: str
    message: str
    source: str = "ptp-agent"


@dataclass
class RunState:
    task_id: int
    run_id: Optional[int]
    engine_type: str
    status: str = "running"
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: Optional[datetime] = None
    logs: List[RunLog] = field(default_factory=list)
    last_seq: int = 0
    rps: float = 0.0
    rt_p95_ms: float = 0.0
    metric_history: List[Dict[str, float]] = field(default_factory=list)
    pid: Optional[int] = None
    error: Optional[str] = None
    log_path: Optional[Path] = None
    metrics_path: Optional[Path] = None
    pod_name: str = field(default_factory=socket.gethostname)
    agent_ip: str = field(default_factory=lambda: socket.gethostbyname(socket.gethostname()))
    s3_log_uri: Optional[str] = None
    s3_metrics_uri: Optional[str] = None
    jtl_path: Optional[Path] = None
    k6_summary_path: Optional[Path] = None
    jtl_summary: Optional[Dict[str, float]] = None
    k6_summary: Optional[Dict[str, Any]] = None
    k6_control_host: Optional[str] = None
    k6_control_port: Optional[int] = None
    k6_control_url: Optional[str] = None
    k6_control_available: bool = False
    k6_control_error: Optional[str] = None
    k6_control_last_synced_at: Optional[datetime] = None
    k6_control_mode: Optional[str] = None
    k6_status_patch_supported: bool = False
    k6_status_patch_reason: Optional[str] = None
    k6_status_patch_mode: Optional[str] = None
    k6_script_family: Optional[str] = None
    k6_preferred_control_path: Optional[str] = None
    k6_active_control_path: Optional[str] = None
    k6_scenario_patch_supported: bool = False
    k6_scenario_patch_reason: Optional[str] = None
    k6_scenario_configs: List[Dict[str, Any]] = field(default_factory=list)
    k6_runtime_properties: Dict[str, Any] = field(default_factory=dict)
    k6_script_path: Optional[str] = None
    k6_target_tps: Optional[float] = None
    k6_target_vus: Optional[int] = None
    k6_target_max_vus: Optional[int] = None
    k6_observed_tps: Optional[float] = None
    k6_metric_family: Optional[str] = None
    k6_last_metric_counts: Dict[str, float] = field(default_factory=dict)
    k6_last_metric_sampled_at: Optional[datetime] = None
    k6_controller_enabled: bool = False
    k6_controller_status: Optional[str] = None
    k6_controller_message: Optional[str] = None
    k6_scenario_direct_adjusting_local_target_tps: Optional[float] = None
    k6_scenario_direct_adjusting_deadline_at: Optional[datetime] = None
    k6_controller_thread: Optional[Any] = field(default=None, repr=False, compare=False)
    k6_controller_stop_event: Optional[Any] = field(default=None, repr=False, compare=False)
    jtl_failure_logs_emitted: bool = False
    cpu_usage_percent_warmup_done: bool = False
    pod_monitor_history: List[Dict[str, object]] = field(default_factory=list)
    pod_monitor_terminal_snapshot_recorded: bool = False
    async_task: Optional[asyncio.Task] = field(default=None, repr=False, compare=False)

    def build_pod_status_payload(self) -> Dict[str, object]:
        agent_host = _build_agent_host_label(self.agent_ip)
        pod_name = self.pod_name.strip() if isinstance(self.pod_name, str) and self.pod_name.strip() else None
        pod_ip = self.agent_ip.strip() if isinstance(self.agent_ip, str) and self.agent_ip.strip() else None
        status = str(self.status or "unknown").strip().lower() or "unknown"
        return {
            "agent_host": agent_host,
            "pod_ip": pod_ip,
            "pod_name": pod_name,
            "status": status,
            "cluster_name": None,
            "node_name": None,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }

    def append_log(self, level: str, message: str, source: str = "ptp-agent"):
        self.last_seq += 1
        log = RunLog(
            seq=self.last_seq,
            ts=datetime.now(timezone.utc),
            level=level,
            message=message,
            source=source,
        )
        self.logs.append(log)
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(
                    f"{log.seq}|{log.ts.isoformat()}|{log.level}|{log.source}|{log.message}\n"
                )

    def append_output_line(
        self, line: str, level: str = "INFO", source: str = "tool"
    ):
        if not line:
            return
        self.append_log(level, line, source=source)

    def append_metrics(self, rps: float, rt_p95_ms: float):
        self.metric_history.append(
            {
                "ts": datetime.now(timezone.utc),
                "rps": rps,
                "rt_p95_ms": rt_p95_ms,
            }
        )
        if self.metrics_path:
            self.metrics_path.parent.mkdir(parents=True, exist_ok=True)
            with self.metrics_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": self.metric_history[-1]["ts"].isoformat(), "rps": rps, "rt_p95_ms": rt_p95_ms}) + "\n")

    def append_pod_monitor_snapshot(
        self,
        cpu_usage_percent: Optional[float] = None,
        cpu_load: Optional[float] = None,
        memory_usage_percent: Optional[float] = None,
        network_rx_bytes: Optional[float] = None,
        network_tx_bytes: Optional[float] = None,
        socket_count: Optional[float] = None,
        disk_usage_percent: Optional[float] = None,
        disk_used_bytes: Optional[float] = None,
        disk_total_bytes: Optional[float] = None,
        network_rx_packets: Optional[float] = None,
        network_tx_packets: Optional[float] = None,
        disk_read_bytes: Optional[float] = None,
        disk_write_bytes: Optional[float] = None,
    ):
        host_runtime_scope = _is_host_runtime_identity(self.agent_ip, self.pod_name)
        detected_cpu_usage_percent: Optional[float] = None
        detected_cpu_usage_usec: Optional[float] = None
        detected_process_cpu_seconds: Optional[float] = None
        if cpu_usage_percent is None:
            detected_cpu_capacity: Optional[float] = None
            if host_runtime_scope and self.pid:
                detected_process_cpu_seconds = _read_process_tree_cpu_time_seconds(self.pid)
                detected_cpu_capacity = float(os.cpu_count() or 0)
            else:
                detected_cpu_usage_usec, detected_cpu_capacity = _read_cgroup_cpu_usage_counter()
            if detected_process_cpu_seconds is not None:
                cpu_capacity = detected_cpu_capacity or float(os.cpu_count() or 0)
                previous_cpu_seconds: Optional[float] = None
                previous_ts: Optional[datetime] = None
                for snapshot in reversed(self.pod_monitor_history):
                    raw_usage = snapshot.get("_cpu_process_seconds")
                    raw_ts = snapshot.get("ts")
                    if isinstance(raw_usage, (int, float)) and isinstance(raw_ts, datetime):
                        previous_cpu_seconds = float(raw_usage)
                        previous_ts = raw_ts
                        break
                if (
                    previous_cpu_seconds is not None
                    and previous_ts is not None
                    and cpu_capacity
                    and cpu_capacity > 0
                ):
                    elapsed_seconds = (datetime.now(timezone.utc) - previous_ts).total_seconds()
                    if elapsed_seconds >= 1.0:
                        delta_usage_seconds = max(
                            0.0,
                            detected_process_cpu_seconds - previous_cpu_seconds,
                        )
                        computed_cpu_usage_percent = max(
                            0.0,
                            min(100.0, delta_usage_seconds / (elapsed_seconds * cpu_capacity) * 100.0),
                        )
                        if self.cpu_usage_percent_warmup_done:
                            detected_cpu_usage_percent = computed_cpu_usage_percent
                        else:
                            self.cpu_usage_percent_warmup_done = True
                            detected_cpu_usage_percent = None
                    else:
                        detected_cpu_usage_percent = None
                else:
                    detected_cpu_usage_percent = None
            elif detected_cpu_usage_usec is not None:
                cpu_capacity = detected_cpu_capacity or float(os.cpu_count() or 0)
                previous_usage_usec: Optional[float] = None
                previous_ts: Optional[datetime] = None
                for snapshot in reversed(self.pod_monitor_history):
                    raw_usage = snapshot.get("_cpu_usage_usec")
                    raw_ts = snapshot.get("ts")
                    if isinstance(raw_usage, (int, float)) and isinstance(raw_ts, datetime):
                        previous_usage_usec = float(raw_usage)
                        previous_ts = raw_ts
                        break
                if (
                    previous_usage_usec is not None
                    and previous_ts is not None
                    and cpu_capacity
                    and cpu_capacity > 0
                ):
                    elapsed_seconds = (datetime.now(timezone.utc) - previous_ts).total_seconds()
                    if elapsed_seconds >= 1.0:
                        delta_usage_seconds = max(
                            0.0,
                            (detected_cpu_usage_usec - previous_usage_usec) / 1_000_000.0,
                        )
                        computed_cpu_usage_percent = max(
                            0.0,
                            min(100.0, delta_usage_seconds / (elapsed_seconds * cpu_capacity) * 100.0),
                        )
                        if self.cpu_usage_percent_warmup_done:
                            detected_cpu_usage_percent = computed_cpu_usage_percent
                        else:
                            self.cpu_usage_percent_warmup_done = True
                            detected_cpu_usage_percent = None
                    else:
                        detected_cpu_usage_percent = None
                else:
                    # The first cgroup sample only establishes the baseline. Do not mix
                    # host-wide CPU percent into cgroup-based runs until a real delta exists.
                    detected_cpu_usage_percent = None
            else:
                detected_cpu_usage_percent = _read_cpu_usage_percent()
        detected_cpu_load = _read_cpu_load() if cpu_load is None else None
        cpu_usage_percent_value = (
            float(cpu_usage_percent)
            if cpu_usage_percent is not None
            else (float(detected_cpu_usage_percent) if detected_cpu_usage_percent is not None else None)
        )
        cpu_load_value = (
            float(cpu_load)
            if cpu_load is not None
            else (float(detected_cpu_load) if detected_cpu_load is not None else None)
        )

        detected_memory_usage_percent: Optional[float] = None
        detected_memory_used_bytes: Optional[float] = None
        if memory_usage_percent is None:
            if host_runtime_scope and self.pid:
                detected_memory_usage_percent, detected_memory_used_bytes = _read_process_tree_memory_snapshot(self.pid)
            else:
                detected_memory_usage_percent, detected_memory_used_bytes = _read_memory_usage_snapshot()
        memory_usage_percent_value = (
            float(memory_usage_percent)
            if memory_usage_percent is not None
            else (float(detected_memory_usage_percent) if detected_memory_usage_percent is not None else None)
        )
        memory_used_bytes_value = (
            float(detected_memory_used_bytes)
            if detected_memory_used_bytes is not None
            else None
        )

        detected_disk_usage_percent: Optional[float] = None
        detected_disk_used_bytes: Optional[float] = None
        detected_disk_total_bytes: Optional[float] = None
        if (
            disk_usage_percent is None
            or disk_used_bytes is None
            or disk_total_bytes is None
        ):
            (
                detected_disk_usage_percent,
                detected_disk_used_bytes,
                detected_disk_total_bytes,
            ) = _read_disk_usage_snapshot()

        disk_usage_percent_value = (
            float(disk_usage_percent)
            if disk_usage_percent is not None
            else (float(detected_disk_usage_percent) if detected_disk_usage_percent is not None else None)
        )
        disk_used_bytes_value = (
            float(disk_used_bytes)
            if disk_used_bytes is not None
            else (float(detected_disk_used_bytes) if detected_disk_used_bytes is not None else None)
        )
        disk_total_bytes_value = (
            float(disk_total_bytes)
            if disk_total_bytes is not None
            else (float(detected_disk_total_bytes) if detected_disk_total_bytes is not None else None)
        )

        detected_rx_bytes: Optional[float] = None
        detected_tx_bytes: Optional[float] = None
        detected_rx_packets: Optional[float] = None
        detected_tx_packets: Optional[float] = None
        if host_runtime_scope and self.pid and (
            network_rx_bytes is None
            or network_tx_bytes is None
            or network_rx_packets is None
            or network_tx_packets is None
        ):
            (
                detected_rx_bytes,
                detected_tx_bytes,
                detected_rx_packets,
                detected_tx_packets,
            ) = _read_process_tree_network_totals(self.pid)
        if network_rx_bytes is None or network_tx_bytes is None:
            if not (host_runtime_scope and self.pid):
                detected_rx_bytes, detected_tx_bytes = _read_network_byte_totals()

        rx_bytes_value = (
            float(network_rx_bytes)
            if network_rx_bytes is not None
            else (float(detected_rx_bytes) if detected_rx_bytes is not None else None)
        )
        tx_bytes_value = (
            float(network_tx_bytes)
            if network_tx_bytes is not None
            else (float(detected_tx_bytes) if detected_tx_bytes is not None else None)
        )

        if network_rx_packets is None or network_tx_packets is None:
            if not (host_runtime_scope and self.pid):
                detected_rx_packets, detected_tx_packets = _read_network_packet_totals()

        rx_packets_value = (
            float(network_rx_packets)
            if network_rx_packets is not None
            else (float(detected_rx_packets) if detected_rx_packets is not None else None)
        )
        tx_packets_value = (
            float(network_tx_packets)
            if network_tx_packets is not None
            else (float(detected_tx_packets) if detected_tx_packets is not None else None)
        )

        detected_disk_read_bytes: Optional[float] = None
        detected_disk_write_bytes: Optional[float] = None
        if disk_read_bytes is None or disk_write_bytes is None:
            if host_runtime_scope and self.pid:
                detected_disk_read_bytes, detected_disk_write_bytes = _read_process_tree_disk_io_totals(self.pid)
            else:
                detected_disk_read_bytes, detected_disk_write_bytes = _read_disk_io_totals()

        disk_read_bytes_value = (
            float(disk_read_bytes)
            if disk_read_bytes is not None
            else (
                float(detected_disk_read_bytes)
                if detected_disk_read_bytes is not None
                else None
            )
        )
        disk_write_bytes_value = (
            float(disk_write_bytes)
            if disk_write_bytes is not None
            else (
                float(detected_disk_write_bytes)
                if detected_disk_write_bytes is not None
                else None
            )
        )

        detected_socket_count = (
            _read_process_tree_socket_count(self.pid)
            if socket_count is None and host_runtime_scope and self.pid
            else (_read_socket_count() if socket_count is None else None)
        )
        socket_count_value = (
            float(socket_count)
            if socket_count is not None
            else (float(detected_socket_count) if detected_socket_count is not None else None)
        )

        if (
            cpu_usage_percent_value is None
            and cpu_load_value is None
            and memory_usage_percent_value is None
            and memory_used_bytes_value is None
            and disk_usage_percent_value is None
            and disk_used_bytes_value is None
            and disk_total_bytes_value is None
            and rx_bytes_value is None
            and tx_bytes_value is None
            and rx_packets_value is None
            and tx_packets_value is None
            and disk_read_bytes_value is None
            and disk_write_bytes_value is None
            and socket_count_value is None
        ):
            return None

        snapshot = {
            "ts": datetime.now(timezone.utc),
        }
        if cpu_usage_percent_value is not None:
            snapshot["cpu_usage_percent"] = cpu_usage_percent_value
        if detected_process_cpu_seconds is not None:
            snapshot["_cpu_process_seconds"] = detected_process_cpu_seconds
        if detected_cpu_usage_usec is not None:
            snapshot["_cpu_usage_usec"] = detected_cpu_usage_usec
        if cpu_load_value is not None:
            snapshot["cpu_load"] = cpu_load_value
        if memory_usage_percent_value is not None:
            snapshot["memory_usage_percent"] = memory_usage_percent_value
        if memory_used_bytes_value is not None:
            snapshot["memory_used_bytes"] = memory_used_bytes_value
        if disk_usage_percent_value is not None:
            snapshot["disk_usage_percent"] = disk_usage_percent_value
        if disk_used_bytes_value is not None:
            snapshot["disk_used_bytes"] = disk_used_bytes_value
        if disk_total_bytes_value is not None:
            snapshot["disk_total_bytes"] = disk_total_bytes_value
        if rx_bytes_value is not None:
            snapshot["network_rx_bytes"] = rx_bytes_value
        if tx_bytes_value is not None:
            snapshot["network_tx_bytes"] = tx_bytes_value
        if rx_packets_value is not None:
            snapshot["network_rx_packets"] = rx_packets_value
        if tx_packets_value is not None:
            snapshot["network_tx_packets"] = tx_packets_value
        if disk_read_bytes_value is not None:
            snapshot["disk_read_bytes"] = disk_read_bytes_value
        if disk_write_bytes_value is not None:
            snapshot["disk_write_bytes"] = disk_write_bytes_value
        if socket_count_value is not None:
            snapshot["socket_count"] = socket_count_value
        self.pod_monitor_history.append(snapshot)
        if len(self.pod_monitor_history) > 720:
            self.pod_monitor_history = self.pod_monitor_history[-720:]
        return snapshot

    def ensure_terminal_pod_monitor_snapshot(self):
        if self.status == "running" and self.ended_at is None:
            return
        if self.pod_monitor_terminal_snapshot_recorded:
            return
        snapshot = self.append_pod_monitor_snapshot()
        if snapshot is not None or self.pod_monitor_history:
            self.pod_monitor_terminal_snapshot_recorded = True

    def get_latest_pod_monitor_metric(self, metric_name: str) -> Optional[float]:
        for snapshot in reversed(self.pod_monitor_history):
            value = snapshot.get(metric_name)
            if isinstance(value, (int, float)):
                return float(value)
        return None

    def get_pod_monitor_metric_aggregate(
        self, metric_name: str, agg: str
    ) -> Optional[float]:
        values: List[float] = []
        for snapshot in self.pod_monitor_history:
            value = snapshot.get(metric_name)
            if isinstance(value, (int, float)):
                values.append(float(value))

        if not values:
            return None
        if agg == "current":
            return values[-1]
        if agg == "max":
            return max(values)
        if agg == "avg":
            return sum(values) / len(values)
        raise ValueError(f"unsupported aggregate: {agg}")

    def build_pod_monitor_series(self, step_seconds: int = 10) -> List[Dict[str, object]]:
        try:
            resolved_step_seconds = max(1, int(step_seconds))
        except (TypeError, ValueError):
            resolved_step_seconds = 10

        filtered: List[Dict[str, object]] = []
        for snapshot in sorted(
            self.pod_monitor_history,
            key=lambda item: (
                item.get("ts")
                if isinstance(item.get("ts"), datetime)
                else datetime.min.replace(tzinfo=timezone.utc)
            ),
        ):
            ts = snapshot.get("ts")
            if not isinstance(ts, datetime):
                continue
            if filtered:
                last_ts = filtered[-1].get("ts")
                if (
                    isinstance(last_ts, datetime)
                    and (ts - last_ts).total_seconds() < resolved_step_seconds
                ):
                    merged_snapshot = dict(filtered[-1])
                    merged_snapshot.update(snapshot)
                    merged_snapshot["ts"] = ts
                    filtered[-1] = merged_snapshot
                    continue
            filtered.append(snapshot)

        metric_specs = (
            ("cpu_usage_percent", "percent"),
            ("cpu_load", "load"),
            ("memory_usage_percent", "percent"),
            ("memory_used_bytes", "bytes"),
            ("disk_usage_percent", "percent"),
            ("disk_used_bytes", "bytes"),
            ("disk_total_bytes", "bytes"),
            ("network_rx_bytes", "bytes"),
            ("network_tx_bytes", "bytes"),
            ("network_rx_packets", "packets"),
            ("network_tx_packets", "packets"),
            ("disk_read_bytes", "bytes"),
            ("disk_write_bytes", "bytes"),
            ("socket_count", "count"),
        )
        series: List[Dict[str, object]] = []
        for metric_name, unit in metric_specs:
            points: List[Dict[str, object]] = []
            for snapshot in filtered:
                ts = snapshot.get("ts")
                value = snapshot.get(metric_name)
                if not isinstance(ts, datetime) or not isinstance(value, (int, float)):
                    continue
                points.append({"ts": ts, "value": float(value)})

            if points:
                series.append(
                    {
                        "agent_host": _build_agent_host_label(self.agent_ip),
                        "pod_name": self.pod_name,
                        "pod_ip": self.agent_ip,
                        "metric": metric_name,
                        "unit": unit,
                        "points": points,
                    }
                )

        return series

    def append_output_lines(self, raw: bytes, level: str = "INFO", max_lines: int = 20):
        if not raw:
            return
        lines = raw.decode(errors="ignore").splitlines()
        for line in lines[:max_lines]:
            self.append_output_line(line, level=level)

    def load_logs_from_file(self):
        if not self.log_path or not self.log_path.exists():
            return
        existing_seqs = {log.seq for log in self.logs}
        with self.log_path.open("r", encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("|", 4)
                if len(parts) not in {4, 5}:
                    continue
                seq = int(parts[0])
                if seq in existing_seqs:
                    continue
                ts = datetime.fromisoformat(parts[1])
                if len(parts) == 4:
                    level, source, message = parts[2], "ptp-agent", parts[3]
                else:
                    level, source, message = parts[2], parts[3], parts[4]
                self.logs.append(
                    RunLog(
                        seq=seq,
                        ts=ts,
                        level=level,
                        message=message,
                        source=source or "ptp-agent",
                    )
                )
        self.logs.sort(key=lambda l: l.seq)
        if self.logs:
            self.last_seq = max(self.last_seq, self.logs[-1].seq)

    def load_metrics_from_file(self):
        if not self.metrics_path or not self.metrics_path.exists():
            return
        existing = {m["ts"].isoformat() for m in self.metric_history}
        with self.metrics_path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    data = json.loads(line)
                    ts_raw = data.get("ts")
                    if not ts_raw:
                        continue
                    if ts_raw in existing:
                        continue
                    ts = datetime.fromisoformat(ts_raw)
                    self.metric_history.append(
                        {"ts": ts, "rps": float(data.get("rps", 0)), "rt_p95_ms": float(data.get("rt_p95_ms", 0))}
                    )
                    existing.add(ts_raw)
                except Exception:
                    continue


class RuntimeStore:
    def __init__(self):
        self._runs: Dict[str, RunState] = {}

    def put(self, token: str, state: RunState):
        self._runs[token] = state

    def get(self, token: str) -> Optional[RunState]:
        return self._runs.get(token)

    def all(self) -> Dict[str, RunState]:
        return self._runs


store = RuntimeStore()


async def simulate_run(token: str, duration: int = 5):
    state = store.get(token)
    if not state:
        return
    try:
        state.append_log("INFO", f"run_started token={token}")
        steps = max(1, duration)
        for i in range(steps):
            if state.status == "stopped":
                state.ended_at = state.ended_at or datetime.now(timezone.utc)
                state.append_log("WARN", "run_stopped")
                return
            await asyncio.sleep(1)
            state.rps = 80 + 40 * (i / max(steps - 1, 1))
            state.rt_p95_ms = 150 + 20 * (i / max(steps - 1, 1))
            state.append_metrics(rps=state.rps, rt_p95_ms=state.rt_p95_ms)
            state.append_log("INFO", f"progress {i+1}/{steps}")
        if state.status != "stopped":
            state.status = "succeeded"
            state.ended_at = datetime.now(timezone.utc)
            state.append_log("INFO", "run_succeeded")
    except asyncio.CancelledError:
        state.status = "stopped"
        state.ended_at = datetime.now(timezone.utc)
        state.append_log("WARN", "run_cancelled")
