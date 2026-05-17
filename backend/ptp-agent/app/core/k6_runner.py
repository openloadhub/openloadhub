from dataclasses import dataclass
from fractions import Fraction
import logging
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_K6_TREND_STATS = "avg,min,max,med,p(90),p(95),p(99)"
_EXTERNALLY_CONTROLLED_PATTERN = re.compile(
    r'executor\s*:\s*["\']externally-controlled["\']'
)
_SCENARIOS_PATTERN = re.compile(r"\bscenarios\s*:")
_DEFAULT_EXPORT_PATTERN = re.compile(r"\bexport\s+default\b")
_SCENARIO_EXEC_PATTERN = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*\{[^{}]*?exec\s*:\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']",
    re.S,
)
_MIXED_WEIGHT_PATTERN = re.compile(
    r"key\s*:\s*[\"']([^\"']+)[\"']\s*,\s*weight\s*:\s*([0-9]+(?:\.[0-9]+)?)",
    re.S,
)
_STANDARD_SCENARIO_TOTAL_PRE_ALLOCATED_VUS_BASELINE_PER_POD = 200
_STANDARD_SCENARIO_TOTAL_PRE_ALLOCATED_VUS_HARD_CAP_PER_POD = 400
_STANDARD_SCENARIO_TOTAL_MIN_MAX_VUS_PER_POD = 200
_STANDARD_SCENARIO_TOTAL_MAX_VUS_CAP_PER_POD = 800
_STANDARD_SCENARIO_TARGET_QPS_PER_PRE_ALLOCATED_VU = 5


@dataclass(frozen=True)
class K6ScenarioConfig:
    scenario_name: str
    executor: str
    rate: Optional[int] = None
    time_unit: Optional[str] = None
    pre_allocated_vus: Optional[int] = None
    max_vus: Optional[int] = None
    vus: Optional[int] = None
    duration: Optional[str] = None
    exec_name: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_name": self.scenario_name,
            "executor": self.executor,
            "rate": self.rate,
            "time_unit": self.time_unit,
            "pre_allocated_vus": self.pre_allocated_vus,
            "max_vus": self.max_vus,
            "vus": self.vus,
            "duration": self.duration,
            "exec_name": self.exec_name,
        }

    def to_runtime_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "scenarioName": self.scenario_name,
        }
        if self.rate is not None:
            payload["rate"] = int(self.rate)
        if self.max_vus is not None:
            payload["maxVUs"] = int(self.max_vus)
        if self.vus is not None:
            payload["vus"] = int(self.vus)
        if self.time_unit:
            payload["timeUnit"] = self.time_unit
        if self.duration:
            payload["duration"] = self.duration
        if self.pre_allocated_vus is not None:
            payload["preAllocatedVUs"] = int(self.pre_allocated_vus)
        if self.executor:
            payload["executor"] = self.executor
        if self.exec_name:
            payload["exec"] = self.exec_name
        return payload


@dataclass(frozen=True)
class K6StandardControlAdapter:
    family: str
    scenario_configs: tuple[K6ScenarioConfig, ...]
    fallback_exec_sequence: tuple[str, ...]


@dataclass(frozen=True)
class K6RuntimeControlPlan:
    script_path: Path
    status_patch_supported: bool
    reason: Optional[str] = None
    mode: Optional[str] = None
    script_family: Optional[str] = None
    preferred_control_path: Optional[str] = None
    active_control_path: Optional[str] = None
    scenario_patch_supported: bool = False
    scenario_patch_reason: Optional[str] = None
    scenario_configs: tuple[K6ScenarioConfig, ...] = ()


class K6Runner:
    SCENARIO_CONTROLLED_FIXTURES = {
        "demo-grpc-k6.js",
        "demo-http-k6.js",
        "demo-mixed-k6-variable",
        "benchmark-http-k6.js",
        "benchmark-grpc-k6.js",
    }
    STANDARD_SCRIPT_FIXTURE_ALIASES = {
        "demo_http_standard": ("demo-http-k6.js",),
        "demo_grpc_standard": ("demo-grpc-k6.js",),
        "demo_mixed_standard": ("demo-mixed-k6-variable",),
        "curl_http_standard": (),
        "benchmark_http_standard": ("benchmark-http-k6.js",),
        "benchmark_grpc_standard": ("benchmark-grpc-k6.js",),
    }
    STANDARD_FIXED_SCENARIOS = {
        "demo_http_standard": (
            ("get_endpoint", "runGetScenario"),
            ("post_endpoint", "runPostScenario"),
        ),
        "benchmark_http_standard": (
            ("get_endpoint", "runGetScenario"),
            ("post_endpoint", "runPostScenario"),
        ),
        "demo_grpc_standard": (
            ("say_hello_endpoint", "runSayHelloScenario"),
            ("say_hello_again_endpoint", "runSayHelloAgainScenario"),
        ),
        "curl_http_standard": (
            ("request_endpoint", "runRequestScenario"),
        ),
        "benchmark_grpc_standard": (
            ("say_hello_endpoint", "runSayHelloScenario"),
            ("say_hello_again_endpoint", "runSayHelloAgainScenario"),
        ),
    }

    def __init__(self, k6_bin: Path):
        self.k6_bin = k6_bin
        self._version_output_cache: Optional[str] = None
        if not self.k6_bin.exists():
            raise FileNotFoundError(f"K6 binary not found at {self.k6_bin}")

    def run_test(
        self,
        script_path: Path,
        vus: int,
        duration: int,
        ramp_up: int = 0,
        iterations: Optional[int] = None,
        envs: Optional[Dict[str, Any]] = None,
        summary_path: Optional[Path] = None,
        prometheus_rw_url: Optional[str] = None,
        run_token: Optional[str] = None,
        run_id: Optional[int] = None,
        protocol: Optional[str] = None,
        control_address: Optional[str] = None,
        runtime_control_plan: Optional[K6RuntimeControlPlan] = None,
    ) -> subprocess.Popen:
        browser_protocol = self._is_browser_protocol(protocol)
        control_plan = runtime_control_plan or self.build_runtime_control_plan(
            script_path=script_path,
            vus=vus,
            duration=duration,
            envs=envs,
            protocol=protocol,
        )
        launch_script_path = control_plan.script_path
        if (
            control_plan.scenario_configs
            and not control_plan.status_patch_supported
            and launch_script_path == script_path
            and control_plan.active_control_path == "scenario_direct"
        ):
            launch_script_path = self._build_standard_scenario_direct_wrapper(
                script_path=script_path,
                scenario_configs=control_plan.scenario_configs,
            )
        runtime_controlled = bool(control_plan.status_patch_supported)
        scenario_controlled = (
            bool(control_plan.scenario_configs)
            and not runtime_controlled
            and control_plan.active_control_path == "scenario_direct"
        ) or (
            launch_script_path == script_path
            and self._uses_script_defined_scenario(script_path, envs)
            and not runtime_controlled
        )

        cmd = [
            str(self.k6_bin),
            "run",
            "--address",
            control_address or "127.0.0.1:0",
        ]
        if browser_protocol:
            if envs is None:
                envs = {}
            envs.setdefault("TARGET_VUS", str(vus))
            envs.setdefault("STAGE_DURATION", f"{duration}s")
        elif runtime_controlled:
            if envs is None:
                envs = {}
            envs.setdefault("PTP_K6_INITIAL_VUS", str(vus))
            envs.setdefault(
                "PTP_K6_INITIAL_MAX_VUS",
                str(
                    max(
                        vus,
                        self._coerce_positive_int(envs.get("PTP_K6_INITIAL_MAX_VUS"))
                        or 0,
                    )
                ),
            )
            if iterations and iterations > 0:
                envs.pop("PTP_DURATION_SECONDS", None)
            else:
                envs.setdefault("PTP_DURATION_SECONDS", str(duration))
        elif scenario_controlled:
            if envs is None:
                envs = {}
            envs.setdefault("PTP_THREAD_COUNT", str(vus))
            if iterations and iterations > 0:
                envs.pop("PTP_DURATION_SECONDS", None)
            else:
                envs.setdefault("PTP_DURATION_SECONDS", str(duration))
        else:
            cmd.extend(["--vus", str(vus)])
            if iterations and iterations > 0:
                cmd.extend(["--iterations", str(iterations)])
            else:
                cmd.extend(["--duration", f"{duration}s"])
            if ramp_up:
                cmd.extend(["--stage", f"{ramp_up}s:{vus}"])

        if summary_path:
            if envs is None:
                envs = {}
            envs.setdefault("K6_SUMMARY_TREND_STATS", DEFAULT_K6_TREND_STATS)
            cmd.extend(["--summary-export", str(summary_path)])

        if prometheus_rw_url:
            cmd.extend(["--out", "experimental-prometheus-rw"])
            if envs is None:
                envs = {}
            envs["K6_PROMETHEUS_RW_SERVER_URL"] = prometheus_rw_url
            if run_token:
                envs["K6_PROMETHEUS_RW_TREND_STATS"] = DEFAULT_K6_TREND_STATS
                envs["K6_PROMETHEUS_RW_TREND_AS_NATIVE_HISTOGRAM"] = "true"
            record_id = str(run_id) if run_id else run_token
            if record_id:
                cmd.extend(["--tag", f"recordId={record_id}"])
                additional_labels = [f"recordId={record_id}"]
                if run_token:
                    cmd.extend(["--tag", f"runToken={run_token}"])
                    additional_labels.append(f"runToken={run_token}")
                envs["K6_PROMETHEUS_RW_ADDITIONAL_LABELS"] = ",".join(
                    additional_labels
                )

        cmd.append(str(launch_script_path))

        env = os.environ.copy()
        if envs:
            env.update({str(k): str(v) for k, v in envs.items()})
        if browser_protocol:
            env.setdefault("K6_BROWSER_ARGS", os.getenv("K6_BROWSER_ARGS", "no-sandbox"))
            executable_path = os.getenv("K6_BROWSER_EXECUTABLE_PATH", "").strip()
            if executable_path:
                env.setdefault("K6_BROWSER_EXECUTABLE_PATH", executable_path)

        logger.info("Executing K6: %s", " ".join(cmd))
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        logger.info("K6 process started with PID %s", process.pid)
        return process

    def build_runtime_control_plan(
        self,
        script_path: Path,
        vus: int,
        duration: int,
        envs: Optional[Dict[str, Any]] = None,
        protocol: Optional[str] = None,
    ) -> K6RuntimeControlPlan:
        if self._is_browser_protocol(protocol):
            return K6RuntimeControlPlan(
                script_path=script_path,
                status_patch_supported=False,
                reason="browser_protocol_not_supported",
                mode="unsupported",
                preferred_control_path="unsupported",
                active_control_path="blocked",
            )

        if not script_path.exists():
            return K6RuntimeControlPlan(
                script_path=script_path,
                status_patch_supported=False,
                reason="script_file_missing",
                mode="unsupported",
                preferred_control_path="unsupported",
                active_control_path="blocked",
            )

        try:
            content = script_path.read_text(encoding="utf-8")
        except Exception:
            return K6RuntimeControlPlan(
                script_path=script_path,
                status_patch_supported=False,
                reason="script_file_unreadable",
                mode="unsupported",
                preferred_control_path="unsupported",
                active_control_path="blocked",
            )

        adapter = self._build_standard_control_adapter(
            script_path=script_path,
            content=content,
            vus=vus,
            duration=duration,
            envs=envs,
        )
        scenario_patch_supported, scenario_patch_reason = (
            self._resolve_standard_scenario_patch_capability()
        )
        has_target_tps = self._coerce_positive_float(
            (envs or {}).get("target_tps")
            or (envs or {}).get("TARGET_TPS")
            or (envs or {}).get("fixed_tps")
            or (envs or {}).get("FIXED_TPS")
        ) is not None

        if adapter:
            if scenario_patch_supported:
                return K6RuntimeControlPlan(
                    script_path=script_path,
                    status_patch_supported=False,
                    mode="standard_script_scenario_direct",
                    script_family=adapter.family,
                    preferred_control_path="scenario_direct",
                    active_control_path="scenario_direct",
                    scenario_patch_supported=True,
                    scenario_configs=adapter.scenario_configs,
                )

            if adapter.scenario_configs:
                return K6RuntimeControlPlan(
                    script_path=script_path,
                    status_patch_supported=False,
                    mode="standard_script_scenario_static",
                    script_family=adapter.family,
                    preferred_control_path="scenario_direct",
                    active_control_path="scenario_direct",
                    scenario_patch_supported=False,
                    scenario_patch_reason=scenario_patch_reason,
                    scenario_configs=adapter.scenario_configs,
                )

            if has_target_tps:
                return K6RuntimeControlPlan(
                    script_path=script_path,
                    status_patch_supported=False,
                    reason="standard_target_tps_requires_arrival_rate_scenarios",
                    mode="standard_script_target_tps_unresolved",
                    script_family=adapter.family,
                    preferred_control_path="scenario_direct",
                    active_control_path="blocked",
                    scenario_patch_supported=False,
                    scenario_patch_reason=scenario_patch_reason,
                )

            wrapper_path = self._build_dynamic_control_wrapper(
                script_path,
                vus,
                duration,
                fallback_exec_sequence=adapter.fallback_exec_sequence,
            )
            return K6RuntimeControlPlan(
                script_path=wrapper_path,
                status_patch_supported=True,
                mode=(
                    "wrapped_mixed_standard"
                    if adapter.family == "demo_mixed_standard"
                    else "standard_script_fallback_wrapper"
                ),
                script_family=adapter.family,
                preferred_control_path="scenario_direct",
                active_control_path="auto_tps_fallback",
                scenario_patch_supported=False,
                scenario_patch_reason=scenario_patch_reason,
                scenario_configs=adapter.scenario_configs,
            )

        if _EXTERNALLY_CONTROLLED_PATTERN.search(content):
            return K6RuntimeControlPlan(
                script_path=script_path,
                status_patch_supported=True,
                mode="native_externally_controlled",
                preferred_control_path="auto_tps_fallback",
                active_control_path="auto_tps_fallback",
            )

        if _SCENARIOS_PATTERN.search(content):
            return K6RuntimeControlPlan(
                script_path=script_path,
                status_patch_supported=False,
                reason="custom_scenarios_not_supported",
                mode="unsupported",
                preferred_control_path="scenario_direct",
                active_control_path="blocked",
            )

        if not _DEFAULT_EXPORT_PATTERN.search(content):
            return K6RuntimeControlPlan(
                script_path=script_path,
                status_patch_supported=False,
                reason="default_export_required_for_dynamic_control",
                mode="unsupported",
                preferred_control_path="unsupported",
                active_control_path="blocked",
            )

        wrapper_path = self._build_dynamic_control_wrapper(script_path, vus, duration)
        return K6RuntimeControlPlan(
            script_path=wrapper_path,
            status_patch_supported=True,
            mode="wrapped_default_export",
            preferred_control_path="auto_tps_fallback",
            active_control_path="auto_tps_fallback",
        )

    @classmethod
    def describe_standard_scenario_configs(
        cls,
        script_path: Path,
        vus: int,
        duration: int,
        envs: Optional[Dict[str, Any]] = None,
    ) -> tuple[Optional[str], tuple[K6ScenarioConfig, ...]]:
        if not script_path.exists():
            return None, ()
        try:
            content = script_path.read_text(encoding="utf-8")
        except Exception:
            return None, ()
        adapter = cls._build_standard_control_adapter(
            script_path=script_path,
            content=content,
            vus=vus,
            duration=duration,
            envs=envs,
        )
        if not adapter:
            return None, ()
        return adapter.family, adapter.scenario_configs

    @staticmethod
    def serialize_scenario_configs(
        configs: tuple[K6ScenarioConfig, ...] | list[K6ScenarioConfig],
    ) -> list[dict[str, Any]]:
        return [config.to_dict() for config in configs]

    @staticmethod
    def serialize_runtime_scenario_payload(
        configs: tuple[K6ScenarioConfig, ...] | list[K6ScenarioConfig],
    ) -> list[dict[str, Any]]:
        return [config.to_runtime_payload() for config in configs]

    @staticmethod
    def _is_browser_protocol(protocol: Optional[str]) -> bool:
        return str(protocol or "").strip().lower() == "browser"

    @staticmethod
    def _coerce_positive_int(value: Any) -> Optional[int]:
        if value is None or isinstance(value, bool):
            return None
        try:
            parsed = int(float(value))
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _coerce_positive_float(value: Any) -> Optional[float]:
        if value is None or isinstance(value, bool):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    def _resolve_standard_scenario_patch_capability(self) -> tuple[bool, Optional[str]]:
        raw = str(
            os.getenv("PTP_K6_SCENARIO_CONFIG_PATCH_ENABLED")
            or os.getenv("K6_SCENARIO_CONFIG_PATCH_ENABLED")
            or ""
        ).strip().lower()
        if raw in {"1", "true", "yes", "enabled"}:
            return True, None
        if raw in {"0", "false", "no", "disabled"}:
            return False, "k6_rest_api_scenario_patch_not_supported"

        version_output = self._get_k6_version_output()
        nacos_module_ok = any(
            module_name in version_output
            for module_name in (
                "github.com/ptp-open-source/ptp-xk6-nacos",
                "github.com/shlsky/xk6-nacos",
            )
        )
        grpc_module_ok = any(
            module_name in version_output
            for module_name in (
                "github.com/ptp-open-source/ptp-xk6-grpc",
                "github.com/shlsky/xk6-grpc",
            )
        )
        if (
            "k6 v0.54.0" in version_output
            and nacos_module_ok
            and grpc_module_ok
        ):
            return True, None
        return False, "k6_rest_api_scenario_patch_not_supported"

    def _get_k6_version_output(self) -> str:
        if self._version_output_cache is not None:
            return self._version_output_cache
        try:
            output = subprocess.check_output(
                [str(self.k6_bin), "version"],
                text=True,
                stderr=subprocess.STDOUT,
                timeout=5,
            )
        except Exception:
            output = ""
        self._version_output_cache = output
        return output

    @classmethod
    def _build_standard_control_adapter(
        cls,
        *,
        script_path: Path,
        content: str,
        vus: int,
        duration: int,
        envs: Optional[Dict[str, Any]],
    ) -> Optional[K6StandardControlAdapter]:
        family = cls._detect_standard_script_family(script_path, content)
        if family is None:
            return None
        if envs and any(
            cls._coerce_positive_int(envs.get(key)) is not None
            for key in ("loops", "LOOPS", "iterations", "ITERATIONS", "request_count")
        ):
            return None

        scenario_exec_map = cls._extract_scenario_exec_mapping(content)
        target_tps = cls._coerce_positive_float(
            (envs or {}).get("target_tps")
            or (envs or {}).get("TARGET_TPS")
            or (envs or {}).get("fixed_tps")
            or (envs or {}).get("FIXED_TPS")
        )
        pod_count = cls._coerce_positive_int(
            (envs or {}).get("pod_count")
            or (envs or {}).get("POD_COUNT")
            or 1
        ) or 1
        thread_count = cls._coerce_positive_int(
            (envs or {}).get("PTP_THREAD_COUNT")
            or (envs or {}).get("threads")
            or (envs or {}).get("THREADS")
            or vus
        ) or max(1, int(vus))
        duration_seconds = cls._coerce_positive_int(
            (envs or {}).get("PTP_DURATION_SECONDS")
            or (envs or {}).get("duration")
            or (envs or {}).get("DURATION")
            or duration
        ) or max(1, int(duration))

        if family == "demo_mixed_standard":
            scenario_configs, fallback_exec_sequence = (
                cls._build_weighted_mixed_adapter_details(
                    content=content,
                    scenario_exec_map=scenario_exec_map,
                    target_tps=target_tps,
                    pod_count=pod_count,
                    thread_count=thread_count,
                    duration_seconds=duration_seconds,
                )
            )
            return K6StandardControlAdapter(
                family=family,
                scenario_configs=scenario_configs,
                fallback_exec_sequence=fallback_exec_sequence,
            )

        scenario_pairs = cls.STANDARD_FIXED_SCENARIOS.get(family, ())
        if not scenario_pairs:
            return None

        fallback_exec_sequence = tuple(
            scenario_exec_map.get(name, exec_name)
            for name, exec_name in scenario_pairs
        )
        scenario_configs = cls._build_uniform_arrival_scenario_configs(
            scenario_pairs=tuple(
                (name, scenario_exec_map.get(name, exec_name))
                for name, exec_name in scenario_pairs
            ),
            target_tps=target_tps,
            pod_count=pod_count,
            thread_count=thread_count,
            duration_seconds=duration_seconds,
        )
        return K6StandardControlAdapter(
            family=family,
            scenario_configs=scenario_configs,
            fallback_exec_sequence=fallback_exec_sequence,
        )

    @classmethod
    def _detect_standard_script_family(
        cls,
        script_path: Path,
        content: str,
    ) -> Optional[str]:
        script_name = script_path.name.lower()
        script_stem = script_path.stem.lower()
        for family, aliases in cls.STANDARD_SCRIPT_FIXTURE_ALIASES.items():
            if any(
                alias in script_name
                or Path(alias).stem in script_stem
                or Path(alias).stem in script_name
                for alias in aliases
            ):
                return family

        if cls._looks_like_curl_http_standard_template(content):
            return "curl_http_standard"

        if "const MIXED_WEIGHTS =" in content and "constant-arrival-rate" in content:
            return "demo_mixed_standard"
        return None

    @staticmethod
    def _looks_like_curl_http_standard_template(content: str) -> bool:
        if not isinstance(content, str) or not content.strip():
            return False
        required_markers = (
            "function buildArrivalRateScenario(",
            "const totalTargetTps = Math.max(0, Number(__ENV.target_tps || __ENV.TARGET_TPS || '0'));",
            "const podCount = Math.max(1, Number(__ENV.pod_count || __ENV.POD_COUNT || '1'));",
            "request_endpoint",
            "exec: 'runRequestScenario'",
            "export function runRequestScenario()",
        )
        return all(marker in content for marker in required_markers)

    @staticmethod
    def _extract_scenario_exec_mapping(content: str) -> dict[str, str]:
        return {
            match.group(1): match.group(2)
            for match in _SCENARIO_EXEC_PATTERN.finditer(content)
        }

    @classmethod
    def _compute_standard_scenario_max_vus(
        cls,
        *,
        pre_allocated_vus: int,
        scenario_total: int,
    ) -> int:
        scenario_count = max(1, int(scenario_total))
        min_per_scenario_max_vus = math.ceil(
            _STANDARD_SCENARIO_TOTAL_MIN_MAX_VUS_PER_POD / scenario_count
        )
        return max(
            int(pre_allocated_vus),
            int(pre_allocated_vus) * 4,
            min_per_scenario_max_vus,
        )

    @classmethod
    def _resolve_standard_total_pre_allocated_vus_budget(
        cls,
        *,
        local_target_tps: float,
        scenario_total: int,
        thread_count: int,
    ) -> int:
        minimum_total = max(1, int(scenario_total) * max(1, int(thread_count)))
        adaptive_total = math.ceil(
            max(0.0, float(local_target_tps))
            / max(1, int(_STANDARD_SCENARIO_TARGET_QPS_PER_PRE_ALLOCATED_VU))
        )
        soft_total = max(
            minimum_total,
            _STANDARD_SCENARIO_TOTAL_PRE_ALLOCATED_VUS_BASELINE_PER_POD,
            adaptive_total,
        )
        return min(
            _STANDARD_SCENARIO_TOTAL_PRE_ALLOCATED_VUS_HARD_CAP_PER_POD,
            soft_total,
        )

    @staticmethod
    def _allocate_proportional_integers(
        values: list[int],
        *,
        target_total: int,
        minimums: Optional[list[int]] = None,
    ) -> list[int]:
        if not values:
            return []

        normalized_values = [max(0, int(value)) for value in values]
        base = (
            [max(0, int(value)) for value in minimums]
            if minimums is not None
            else [0 for _ in normalized_values]
        )
        target_total = max(0, int(target_total))

        base_total = sum(base)
        if base_total >= target_total:
            if base_total == target_total:
                return base
            normalized_base_total = sum(base)
            if normalized_base_total <= 0:
                return [0 for _ in base]
            scaled = [value * target_total / normalized_base_total for value in base]
            allocation = [int(math.floor(value)) for value in scaled]
            remainder = target_total - sum(allocation)
            order = sorted(
                range(len(base)),
                key=lambda index: (scaled[index] - allocation[index], base[index], -index),
                reverse=True,
            )
            for index in order[:remainder]:
                allocation[index] += 1
            return allocation

        if sum(normalized_values) <= target_total:
            return normalized_values

        remaining = target_total - base_total
        extras = [max(0, raw - floor) for raw, floor in zip(normalized_values, base)]
        extras_total = sum(extras)
        if remaining <= 0 or extras_total <= 0:
            return base

        scaled = [value * remaining / extras_total for value in extras]
        allocation = [int(math.floor(value)) for value in scaled]
        remainder = remaining - sum(allocation)
        order = sorted(
            range(len(extras)),
            key=lambda index: (scaled[index] - allocation[index], extras[index], -index),
            reverse=True,
        )
        for index in order[:remainder]:
            allocation[index] += 1
        return [base[index] + allocation[index] for index in range(len(base))]

    @classmethod
    def _build_uniform_arrival_scenario_configs(
        cls,
        *,
        scenario_pairs: tuple[tuple[str, str], ...],
        target_tps: Optional[float],
        pod_count: int,
        thread_count: int,
        duration_seconds: int,
    ) -> tuple[K6ScenarioConfig, ...]:
        normalized_tps = math.floor(target_tps) if target_tps is not None else 0
        if normalized_tps <= 0 or not scenario_pairs:
            return ()

        denominator = max(1, int(pod_count) * len(scenario_pairs))
        divisor = math.gcd(int(normalized_tps), denominator) or 1
        rate = max(1, int(normalized_tps) // divisor)
        time_unit_seconds = max(1, denominator // divisor)
        pre_allocated_vus = max(
            1,
            int(thread_count),
            math.ceil(int(normalized_tps) / denominator),
        )
        scenario_count = len(scenario_pairs)
        local_target_tps = normalized_tps / max(1, int(pod_count))
        pre_allocated_values = cls._allocate_proportional_integers(
            [pre_allocated_vus for _ in scenario_pairs],
            target_total=cls._resolve_standard_total_pre_allocated_vus_budget(
                local_target_tps=local_target_tps,
                scenario_total=scenario_count,
                thread_count=thread_count,
            ),
            minimums=[max(1, int(thread_count)) for _ in scenario_pairs],
        )
        raw_max_values = [
            cls._compute_standard_scenario_max_vus(
                pre_allocated_vus=value,
                scenario_total=scenario_count,
            )
            for value in pre_allocated_values
        ]
        total_pre_allocated_vus = sum(pre_allocated_values)
        total_max_vus_cap = min(
            _STANDARD_SCENARIO_TOTAL_MAX_VUS_CAP_PER_POD,
            max(
                _STANDARD_SCENARIO_TOTAL_MIN_MAX_VUS_PER_POD,
                total_pre_allocated_vus * 2,
            ),
        )
        max_vus_values = cls._allocate_proportional_integers(
            raw_max_values,
            target_total=total_max_vus_cap,
            minimums=pre_allocated_values,
        )
        duration_value = f"{max(1, int(duration_seconds))}s"
        return tuple(
            K6ScenarioConfig(
                scenario_name=scenario_name,
                executor="constant-arrival-rate",
                rate=rate,
                time_unit=f"{time_unit_seconds}s",
                pre_allocated_vus=pre_allocated_values[index],
                max_vus=max_vus_values[index],
                duration=duration_value,
                exec_name=exec_name,
            )
            for index, (scenario_name, exec_name) in enumerate(scenario_pairs)
        )

    @classmethod
    def _build_weighted_mixed_adapter_details(
        cls,
        *,
        content: str,
        scenario_exec_map: dict[str, str],
        target_tps: Optional[float],
        pod_count: int,
        thread_count: int,
        duration_seconds: int,
    ) -> tuple[tuple[K6ScenarioConfig, ...], tuple[str, ...]]:
        weights = cls._parse_mixed_weights(content)
        if not weights:
            return (), ()

        fallback_exec_sequence = cls._build_weighted_exec_sequence(
            weights=weights,
            scenario_exec_map=scenario_exec_map,
        )
        normalized_tps = math.floor(target_tps) if target_tps is not None else 0
        if normalized_tps <= 0:
            return (), fallback_exec_sequence

        total_weight = sum(weight for _name, weight in weights) or Fraction(1, 1)
        fractions: list[Fraction] = [
            Fraction(int(normalized_tps), max(1, int(pod_count)))
            * weight
            / total_weight
            for _name, weight in weights
        ]
        common_unit_seconds = 1
        for fraction in fractions:
            common_unit_seconds = math.lcm(common_unit_seconds, fraction.denominator)
        rates = [int(fraction * common_unit_seconds) for fraction in fractions]
        common_divisor = common_unit_seconds
        for rate in rates:
            common_divisor = math.gcd(common_divisor, rate) or common_divisor
        common_unit_seconds = max(1, common_unit_seconds // max(1, common_divisor))
        rates = [max(1, rate // max(1, common_divisor)) for rate in rates]
        duration_value = f"{max(1, int(duration_seconds))}s"
        scenario_total = len(weights)
        per_scenario_thread_floor = max(
            1,
            math.ceil(max(1, int(thread_count)) / max(1, scenario_total)),
        )

        raw_pre_allocated_vus = [
            max(
                1,
                per_scenario_thread_floor,
                math.ceil(max(1, rate) / max(1, common_unit_seconds)),
            )
            for rate in rates
        ]
        local_target_tps = sum(
            per_scenario_rate / max(1, common_unit_seconds)
            for per_scenario_rate in rates
        )
        pre_allocated_values = cls._allocate_proportional_integers(
            raw_pre_allocated_vus,
            target_total=cls._resolve_standard_total_pre_allocated_vus_budget(
                local_target_tps=local_target_tps,
                scenario_total=scenario_total,
                thread_count=per_scenario_thread_floor,
            ),
            minimums=[per_scenario_thread_floor for _ in raw_pre_allocated_vus],
        )
        raw_max_values = [
            cls._compute_standard_scenario_max_vus(
                pre_allocated_vus=value,
                scenario_total=scenario_total,
            )
            for value in pre_allocated_values
        ]
        total_pre_allocated_vus = sum(pre_allocated_values)
        total_max_vus_cap = min(
            _STANDARD_SCENARIO_TOTAL_MAX_VUS_CAP_PER_POD,
            max(
                _STANDARD_SCENARIO_TOTAL_MIN_MAX_VUS_PER_POD,
                total_pre_allocated_vus * 2,
            ),
        )
        max_vus_values = cls._allocate_proportional_integers(
            raw_max_values,
            target_total=total_max_vus_cap,
            minimums=pre_allocated_values,
        )

        scenario_configs: list[K6ScenarioConfig] = []
        for index, (scenario_name, _weight) in enumerate(weights):
            per_scenario_rate = max(1, rates[index])
            exec_name = scenario_exec_map.get(scenario_name, scenario_name)
            scenario_configs.append(
                K6ScenarioConfig(
                    scenario_name=scenario_name,
                    executor="constant-arrival-rate",
                    rate=per_scenario_rate,
                    time_unit=f"{common_unit_seconds}s",
                    pre_allocated_vus=pre_allocated_values[index],
                    max_vus=max_vus_values[index],
                    duration=duration_value,
                    exec_name=exec_name,
                )
            )
        return tuple(scenario_configs), fallback_exec_sequence

    @classmethod
    def _parse_mixed_weights(cls, content: str) -> tuple[tuple[str, Fraction], ...]:
        weights: list[tuple[str, Fraction]] = []
        for match in _MIXED_WEIGHT_PATTERN.finditer(content):
            scenario_name = match.group(1).strip()
            try:
                weight = Fraction(match.group(2)).limit_denominator(1000)
            except (ValueError, ZeroDivisionError):
                continue
            if scenario_name and weight > 0:
                weights.append((scenario_name, weight))
        return tuple(weights)

    @classmethod
    def _build_weighted_exec_sequence(
        cls,
        *,
        weights: tuple[tuple[str, Fraction], ...],
        scenario_exec_map: dict[str, str],
    ) -> tuple[str, ...]:
        if not weights:
            return ()
        common_denominator = 1
        for _name, weight in weights:
            common_denominator = math.lcm(common_denominator, weight.denominator)
        scaled_weights = [
            int(weight * common_denominator)
            for _name, weight in weights
        ]
        common_divisor = 0
        for value in scaled_weights:
            common_divisor = math.gcd(common_divisor, value)
        if common_divisor <= 0:
            common_divisor = 1
        sequence: list[str] = []
        for index, (scenario_name, _weight) in enumerate(weights):
            exec_name = scenario_exec_map.get(scenario_name, scenario_name)
            repeat = max(1, scaled_weights[index] // common_divisor)
            sequence.extend([exec_name] * repeat)
        return tuple(sequence)

    def _build_dynamic_control_wrapper(
        self,
        script_path: Path,
        vus: int,
        duration: int,
        *,
        fallback_exec_sequence: Optional[tuple[str, ...]] = None,
    ) -> Path:
        wrapper_path = script_path.with_name(
            f"{script_path.stem}-ptp-dynamic-control-wrapper{script_path.suffix}"
        )
        relative_import = os.path.relpath(script_path, wrapper_path.parent).replace(
            os.sep, "/"
        )
        if not relative_import.startswith("."):
            relative_import = f"./{relative_import}"

        exec_sequence_literal = ", ".join(
            f'"{handler_name}"' for handler_name in (fallback_exec_sequence or ())
        )
        wrapper_path.write_text(
            (
                "import exec from \"k6/execution\";\n"
                f'import * as base from "{relative_import}";\n\n'
                "function parsePositiveInt(value, fallbackValue) {\n"
                "  const parsed = Number.parseInt(String(value ?? \"\"), 10);\n"
                "  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallbackValue;\n"
                "}\n\n"
                "const baseOptions = base.options && typeof base.options === \"object\" ? base.options : {};\n"
                f"const initialVus = parsePositiveInt(__ENV.PTP_K6_INITIAL_VUS, {max(1, int(vus))});\n"
                "const initialMaxVUs = Math.max(\n"
                "  initialVus,\n"
                "  parsePositiveInt(__ENV.PTP_K6_INITIAL_MAX_VUS, initialVus),\n"
                ");\n"
                f"const initialDurationSeconds = parsePositiveInt(__ENV.PTP_DURATION_SECONDS, {max(1, int(duration))});\n"
                f"const ptpDynamicExecSequence = [{exec_sequence_literal}];\n\n"
                "function pickDynamicHandlerName(iterationInTest) {\n"
                "  if (!ptpDynamicExecSequence.length) {\n"
                "    return null;\n"
                "  }\n"
                "  const normalizedIteration = Math.abs(Number(iterationInTest || 0));\n"
                "  return ptpDynamicExecSequence[normalizedIteration % ptpDynamicExecSequence.length] || null;\n"
                "}\n\n"
                "export const options = {\n"
                "  ...baseOptions,\n"
                "  scenarios: {\n"
                "    ptp_dynamic_control: {\n"
                "      executor: \"externally-controlled\",\n"
                "      vus: initialVus,\n"
                "      maxVUs: initialMaxVUs,\n"
                "      duration: `${initialDurationSeconds}s`,\n"
                "      exec: \"ptp_dynamic_entry\",\n"
                "    },\n"
                "  },\n"
                "};\n\n"
                "export function setup() {\n"
                "  if (typeof base.setup === \"function\") {\n"
                "    return base.setup();\n"
                "  }\n"
                "  return undefined;\n"
                "}\n\n"
                "export function teardown(data) {\n"
                "  if (typeof base.teardown === \"function\") {\n"
                "    return base.teardown(data);\n"
                "  }\n"
                "  return undefined;\n"
                "}\n\n"
                "export function handleSummary(data) {\n"
                "  if (typeof base.handleSummary === \"function\") {\n"
                "    return base.handleSummary(data);\n"
                "  }\n"
                "  return {};\n"
                "}\n\n"
                "export function ptp_dynamic_entry(data) {\n"
                "  const handlerName = pickDynamicHandlerName(exec.scenario.iterationInTest);\n"
                "  if (handlerName && typeof base[handlerName] === \"function\") {\n"
                "    return base[handlerName](data);\n"
                "  }\n"
                "  if (typeof base.default === \"function\") {\n"
                "    return base.default(data);\n"
                "  }\n"
                "  throw new Error(\"ptp_dynamic_control_default_export_missing\");\n"
                "}\n"
            ),
            encoding="utf-8",
        )
        return wrapper_path

    def _build_standard_scenario_direct_wrapper(
        self,
        *,
        script_path: Path,
        scenario_configs: tuple[K6ScenarioConfig, ...],
    ) -> Path:
        wrapper_path = script_path.with_name(
            f"{script_path.stem}-ptp-scenario-direct-wrapper{script_path.suffix}"
        )
        relative_import = os.path.relpath(script_path, wrapper_path.parent).replace(
            os.sep, "/"
        )
        if not relative_import.startswith("."):
            relative_import = f"./{relative_import}"

        scenario_lines: list[str] = []
        handler_names: list[str] = []
        for item in scenario_configs:
            exec_name = str(item.exec_name or item.scenario_name).strip()
            handler_names.append(exec_name)
            scenario_lines.extend(
                [
                    f'    "{item.scenario_name}": {{',
                    f'      executor: "{item.executor}",',
                    *( [f"      rate: {int(item.rate)}," ] if item.rate is not None else [] ),
                    *( [f'      timeUnit: "{item.time_unit}",' ] if item.time_unit else [] ),
                    *( [f"      preAllocatedVUs: {int(item.pre_allocated_vus)}," ] if item.pre_allocated_vus is not None else [] ),
                    *( [f"      maxVUs: {int(item.max_vus)}," ] if item.max_vus is not None else [] ),
                    *( [f"      vus: {int(item.vus)}," ] if item.vus is not None else [] ),
                    *( [f'      duration: "{item.duration}",' ] if item.duration else [] ),
                    f'      exec: "{exec_name}",',
                    "    },",
                ]
            )

        unique_handler_names = tuple(dict.fromkeys(handler_names))
        handler_exports = "\n\n".join(
            (
                f"export function {handler_name}(data) {{\n"
                f'  if (typeof base.{handler_name} === "function") {{\n'
                f"    return base.{handler_name}(data);\n"
                "  }\n"
                '  if (typeof base.default === "function") {\n'
                "    return base.default(data);\n"
                "  }\n"
                f'  throw new Error("ptp_scenario_direct_missing_handler:{handler_name}");\n'
                "}"
            )
            for handler_name in unique_handler_names
        )

        wrapper_path.write_text(
            (
                f'import * as base from "{relative_import}";\n\n'
                "const baseOptions = base.options && typeof base.options === \"object\" ? base.options : {};\n\n"
                "export const options = {\n"
                "  ...baseOptions,\n"
                "  scenarios: {\n"
                + "\n".join(scenario_lines)
                + "\n  },\n};\n\n"
                "export default function (data) {\n"
                "  if (typeof base.default === \"function\") {\n"
                "    return base.default(data);\n"
                "  }\n"
                "  return undefined;\n"
                "}\n\n"
                "export function setup() {\n"
                "  if (typeof base.setup === \"function\") {\n"
                "    return base.setup();\n"
                "  }\n"
                "  return undefined;\n"
                "}\n\n"
                "export function teardown(data) {\n"
                "  if (typeof base.teardown === \"function\") {\n"
                "    return base.teardown(data);\n"
                "  }\n"
                "  return undefined;\n"
                "}\n\n"
                "export function handleSummary(data) {\n"
                "  if (typeof base.handleSummary === \"function\") {\n"
                "    return base.handleSummary(data);\n"
                "  }\n"
                "  return {};\n"
                "}\n\n"
                + handler_exports
                + "\n"
            ),
            encoding="utf-8",
        )
        return wrapper_path

    def _uses_script_defined_scenario(
        self, script_path: Path, envs: Optional[Dict[str, Any]]
    ) -> bool:
        if envs and str(envs.get("PTP_K6_SCENARIO_CONTROLLED") or "").strip() == "1":
            return True
        script_name = script_path.name
        script_stem = script_path.stem
        return any(
            fixture_name in script_name
            or Path(fixture_name).stem in script_stem
            or Path(fixture_name).stem in script_name
            for fixture_name in self.SCENARIO_CONTROLLED_FIXTURES
        )
