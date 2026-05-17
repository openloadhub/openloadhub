import logging
import math
import os
import re
import shutil
import subprocess
import uuid
import xml.etree.ElementTree as ET
import zipfile
from tempfile import NamedTemporaryFile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class JMeterRunner:
    INFLUX_BACKEND_CLASSNAME = (
        "org.apache.jmeter.visualizers.backend.influxdb.InfluxdbBackendListenerClient"
    )
    COMPAT_INFLUX_BACKEND_CLASSNAME = (
        "io.github.mderevyankoaqa.influxdb2.visualizer.InfluxDatabaseBackendListenerClient"
    )
    COMPAT_INFLUX_BACKEND_CLASSNAME_V1 = (
        "org.md.jmeter.influxdb2.visualizer.InfluxDatabaseBackendListenerClient"
    )
    INFLUX_BACKEND_CLASSNAME_CANDIDATES = (
        INFLUX_BACKEND_CLASSNAME,
        COMPAT_INFLUX_BACKEND_CLASSNAME,
        COMPAT_INFLUX_BACKEND_CLASSNAME_V1,
    )
    _PTP_PLACEHOLDER_RE = re.compile(r"###\{(?P<name>[^{}#]+)\}###")
    _SANITIZE_PATTERNS: dict[str, tuple[str, ...]] = {
        "jmeter-grpc-request-v2.jar": (
            "org/slf4j/impl/StaticLoggerBinder.class",
            "org/slf4j/impl/StaticMarkerBinder.class",
        ),
    }
    _INFLUX_ARGUMENTS: tuple[tuple[str, str], ...] = (
        (
            "influxdbMetricsSender",
            "${__P(influxdbMetricsSender,org.apache.jmeter.visualizers.backend.influxdb.HttpMetricsSender)}",
        ),
        (
            "influxdbUrl",
            "${__P(influxdbUrl,http://influxdb:8086/api/v2/write?org=ptp&bucket=ptp)}",
        ),
        ("influxdbToken", "${__P(influxdbToken,)}"),
        ("application", "${__P(application,R001)}"),
        ("measurement", "${__P(measurement,jmeter)}"),
        ("summaryOnly", "${__P(summaryOnly,false)}"),
        ("samplersRegex", "${__P(samplersRegex,.*)}"),
        ("percentiles", "${__P(percentiles,90;95;99)}"),
        ("testTitle", "${__P(testTitle,Test name)}"),
        ("eventTags", "${__P(eventTags,)}"),
        ("TAG_runId", "${__P(TAG_runId,R001)}"),
        ("TAG_taskId", "${__P(TAG_taskId,0)}"),
        ("TAG_nodeName", "${__P(TAG_nodeName,ptp-agent)}"),
    )

    def __init__(self, jmeter_home: Path):
        self.jmeter_home = jmeter_home
        self.jmeter_bin = self.jmeter_home / "bin" / "jmeter"
        if not self.jmeter_bin.exists():
            raise FileNotFoundError(f"JMeter not found at {self.jmeter_bin}")

    def run_test(
        self,
        script_path: Path,
        thread_count: int,
        duration: int,
        ramp_up: int = 0,
        properties: Optional[Dict[str, Any]] = None,
        protocol: Optional[str] = None,
    ) -> Tuple[subprocess.Popen, Path]:
        protocol_key = self._resolve_protocol_key(protocol=protocol, script_path=script_path)
        runtime_home = self._prepare_runtime_home_if_needed(
            run_dir=script_path.parent,
            protocol=protocol_key,
            properties=properties,
        )
        prepared_script_path = self._prepare_runtime_placeholders_if_needed(
            script_path,
            properties=properties,
        )
        prepared_script_path = self._prepare_grpc_script_if_needed(
            prepared_script_path,
            properties=properties,
        )
        prepared_script_path = self._prepare_grpc_deadline_if_needed(
            prepared_script_path,
            properties=properties,
            protocol=protocol_key,
        )
        influx_backend_classname = self._resolve_influx_backend_classname(runtime_home)
        prepared_script_path = self._prepare_influx_backend_listener_if_needed(
            prepared_script_path,
            properties=properties,
            backend_classname=influx_backend_classname,
        )
        prepared_script_path = self._prepare_iteration_mode_script_if_needed(
            prepared_script_path,
            properties=properties,
            protocol=protocol_key,
        )
        result_path = Path("/tmp") / f"jmeter_results_{script_path.stem}_{uuid.uuid4().hex[:6]}.jtl"
        cmd = [
            str(runtime_home / "bin" / "jmeter"),
            "-n",
            "-t",
            str(prepared_script_path),
            "-j",
            str(script_path.parent / "jmeter.log"),
            "-Jthreads",
            str(thread_count),
            "-Jduration",
            str(duration),
            "-Jrampup",
            str(ramp_up),
            "-l",
            str(result_path),
            "-Jjmeter.save.saveservice.autoflush=true",
        ]
        jmeter_log_level = str(os.getenv("JMETER_STDOUT_LOG_LEVEL", "") or "").strip()
        if jmeter_log_level:
            normalized_log_level = jmeter_log_level.upper()
            cmd.extend(
                [
                    f"-Lorg.apache.jmeter={normalized_log_level}",
                    f"-Lorg.apache.jorphan={normalized_log_level}",
                ]
            )

        if properties:
            for key, value in properties.items():
                cmd.append(f"-J{key}={self._stringify_property_value(value)}")

        logger.info("Executing JMeter: %s", " ".join(cmd))

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(runtime_home),
            start_new_session=True,
        )

        logger.info("JMeter process started with PID %s", process.pid)
        return process, result_path

    @staticmethod
    def _coerce_positive_float(value: Any) -> Optional[float]:
        if value is None or isinstance(value, bool):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if parsed > 0:
            return parsed
        return None

    @staticmethod
    def _coerce_optional_bool(value: Any) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in {0, 1}:
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        return None

    @classmethod
    def _is_duration_mode_requested(cls, properties: Dict[str, Any]) -> bool:
        mode = str(
            properties.get("run_mode") or properties.get("run_by") or ""
        ).strip().lower()
        if mode in {"duration", "time", "timed"}:
            return True
        if mode in {"iteration", "iterations", "request_count", "loops", "count"}:
            return False
        return cls._coerce_optional_bool(properties.get("scheduler_enabled")) is True

    @classmethod
    def _is_iteration_mode_requested(cls, properties: Dict[str, Any]) -> bool:
        mode = str(
            properties.get("run_mode") or properties.get("run_by") or ""
        ).strip().lower()
        if mode in {"iteration", "iterations", "request_count", "loops", "count"}:
            return True
        return cls._coerce_optional_bool(properties.get("scheduler_enabled")) is False

    @classmethod
    def _resolve_iteration_count(
        cls,
        properties: Optional[Dict[str, Any]] = None,
        *,
        protocol: Optional[str] = None,
    ) -> Optional[int]:
        if not properties:
            return None
        for key in ("request_count", "iterations", "loops"):
            raw_value = properties.get(key)
            if raw_value is None or isinstance(raw_value, bool):
                continue
            try:
                parsed = int(float(raw_value))
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                return parsed
        if str(protocol or "").strip().lower() != "mixed":
            return None
        if cls._is_duration_mode_requested(properties):
            return None
        if not cls._is_iteration_mode_requested(properties):
            return None
        per_agent_tps = cls._coerce_positive_float(properties.get("target_tps_per_agent"))
        duration_seconds = cls._coerce_positive_float(
            properties.get("duration") or properties.get("PTP_DURATION_SECONDS")
        )
        thread_count = cls._coerce_positive_float(
            properties.get("threads")
            or properties.get("thread_count")
            or properties.get("vus")
            or properties.get("PTP_THREAD_COUNT")
        )
        if not per_agent_tps or not duration_seconds or not thread_count:
            return None
        return max(1, int(math.ceil((per_agent_tps * duration_seconds) / thread_count)))

    @classmethod
    def _resolve_protocol_key(
        cls,
        *,
        protocol: Optional[str],
        script_path: Path,
    ) -> str:
        normalized = str(protocol or "").strip().lower()
        if normalized:
            return normalized
        inferred = cls._infer_protocol_key_from_script(script_path)
        return inferred or ""

    @staticmethod
    def _infer_protocol_key_from_script(script_path: Path) -> Optional[str]:
        try:
            content = script_path.read_text(encoding="utf-8")
        except OSError:
            return None

        has_http_sampler = "HTTPSamplerProxy" in content
        has_grpc_sampler = (
            "vn.zalopay.benchmark.GRPCSampler" in content
            or "GRPCSampler.fullMethod" in content
        )
        if has_http_sampler and has_grpc_sampler:
            return "mixed"
        if has_grpc_sampler:
            return "grpc"
        if has_http_sampler:
            return "http"
        return None

    def _prepare_runtime_home_if_needed(
        self,
        *,
        run_dir: Path,
        protocol: Optional[str],
        properties: Optional[Dict[str, Any]] = None,
    ) -> Path:
        protocol_key = str(protocol or "").strip().lower()
        if not protocol_key:
            return self.jmeter_home
        should_isolate = protocol_key == "mixed" or (
            os.getenv(
                f"JMETER_ISOLATE_EXT_FOR_{protocol_key.upper()}",
                "0",
            )
            == "1"
        )
        if not should_isolate:
            return self.jmeter_home

        isolated_home = run_dir / "jmeter-home"
        if isolated_home.exists():
            return isolated_home

        shutil.copytree(self.jmeter_home, isolated_home, symlinks=True)
        self._refresh_runtime_ext_jars_from_source(isolated_home)

        default_excluded = ""
        if protocol_key == "http":
            default_excluded = "jmeter-grpc-request-v2.jar"

        excluded = {
            item.strip()
            for item in (
                os.getenv(
                    f"JMETER_{protocol_key.upper()}_EXCLUDED_EXT_JARS",
                    default_excluded,
                )
            ).split(",")
            if item.strip()
        }
        ext_dir = isolated_home / "lib" / "ext"
        for jar_name in excluded:
            target = ext_dir / jar_name
            if target.exists() or target.is_symlink():
                target.unlink()

        try:
            self._sanitize_ext_jars(ext_dir)
            self._validate_ext_jars(ext_dir)
        except RuntimeError:
            # Some Linux overlayfs/runtime copies can intermittently corrupt large jars
            # in the isolated run directory; refresh once from the source home and retry.
            self._refresh_runtime_ext_jars_from_source(isolated_home)
            self._sanitize_ext_jars(ext_dir)
            self._validate_ext_jars(ext_dir)
        return isolated_home

    @classmethod
    def _sanitize_ext_jars(cls, ext_dir: Path) -> None:
        for jar_name, patterns in cls._SANITIZE_PATTERNS.items():
            jar_path = ext_dir / jar_name
            if not jar_path.exists():
                continue
            cls._strip_zip_entries(jar_path, patterns)

    @staticmethod
    def _strip_zip_entries(jar_path: Path, patterns: tuple[str, ...]) -> bool:
        try:
            with zipfile.ZipFile(jar_path, "r") as source:
                infos = source.infolist()
                should_strip = any(
                    any(
                        info.filename == pattern or info.filename.startswith(pattern)
                        for pattern in patterns
                    )
                    for info in infos
                )
                if not should_strip:
                    return False

                temp_path = jar_path.with_suffix(f"{jar_path.suffix}.tmp")
                with zipfile.ZipFile(temp_path, "w") as target:
                    for info in infos:
                        if any(
                            info.filename == pattern or info.filename.startswith(pattern)
                            for pattern in patterns
                        ):
                            continue
                        if info.is_dir():
                            target.writestr(info, b"")
                        else:
                            target.writestr(info, source.read(info.filename))
            temp_path.replace(jar_path)
            return True
        except zipfile.BadZipFile:
            return False

    def _refresh_runtime_ext_jars_from_source(self, isolated_home: Path) -> None:
        source_ext_dir = self.jmeter_home / "lib" / "ext"
        isolated_ext_dir = isolated_home / "lib" / "ext"
        isolated_ext_dir.mkdir(parents=True, exist_ok=True)
        for jar_name in self._SANITIZE_PATTERNS:
            source_path = source_ext_dir / jar_name
            if not source_path.exists():
                continue
            target_path = isolated_ext_dir / jar_name
            self._copy_file_atomic(source_path, target_path)

    @staticmethod
    def _copy_file_atomic(source_path: Path, target_path: Path) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            dir=str(target_path.parent),
            prefix=f"{target_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp_file:
            temp_path = Path(tmp_file.name)
        try:
            shutil.copy2(source_path, temp_path)
            temp_path.replace(target_path)
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)

    @classmethod
    def _validate_ext_jars(cls, ext_dir: Path) -> None:
        for jar_name in cls._SANITIZE_PATTERNS:
            jar_path = ext_dir / jar_name
            if not jar_path.exists():
                continue
            try:
                with zipfile.ZipFile(jar_path, "r") as source:
                    source.infolist()
                    if source.testzip() is not None:
                        raise zipfile.BadZipFile(f"zip_crc_failed:{jar_path.name}")
            except zipfile.BadZipFile as exc:
                raise RuntimeError(
                    f"invalid_jmeter_ext_jar:{jar_path.name}"
                ) from exc

    def _resolve_influx_backend_classname(self, runtime_home: Path) -> str:
        return self.INFLUX_BACKEND_CLASSNAME

    @staticmethod
    def _stringify_property_value(value: Any) -> str:
        if isinstance(value, bool):
            return str(value).lower()
        return str(value)

    @staticmethod
    def _prepare_grpc_script_if_needed(
        script_path: Path,
        *,
        properties: Optional[Dict[str, Any]] = None,
    ) -> Path:
        if not properties:
            return script_path

        proto_dir = str(properties.get("PTP_PROTO_DIR") or "").strip()
        if not proto_dir:
            return script_path

        content = script_path.read_text(encoding="utf-8")
        updated_content, updated = JMeterRunner._rewrite_grpc_folder_props(
            content, proto_dir
        )
        if not updated:
            return script_path

        prepared_path = script_path.with_name(f"{script_path.stem}.grpc-prepared{script_path.suffix}")
        prepared_path.write_text(updated_content, encoding="utf-8")
        return prepared_path

    @classmethod
    def _prepare_grpc_deadline_if_needed(
        cls,
        script_path: Path,
        *,
        properties: Optional[Dict[str, Any]] = None,
        protocol: Optional[str] = None,
    ) -> Path:
        protocol_key = str(protocol or "").strip().lower()
        if protocol_key not in {"grpc", "mixed"}:
            protocol_key = cls._infer_protocol_key_from_script(script_path) or ""
        if protocol_key not in {"grpc", "mixed"}:
            return script_path

        default_deadline_ms = cls._resolve_grpc_deadline_default_ms(properties)
        try:
            root = ET.fromstring(script_path.read_text(encoding="utf-8"))
        except ET.ParseError:
            return script_path

        updated = False
        replacement = f"${{__P(grpc_deadline_ms,{default_deadline_ms})}}"
        for prop in root.findall(".//stringProp[@name='GRPCSampler.deadline']"):
            current = (prop.text or "").strip()
            if current == replacement:
                continue
            if cls._should_rewrite_grpc_deadline(current, default_deadline_ms):
                prop.text = replacement
                updated = True

        if not updated:
            return script_path

        prepared_path = script_path.with_name(
            f"{script_path.stem}.grpc-deadline-prepared{script_path.suffix}"
        )
        tree = ET.ElementTree(root)
        if hasattr(ET, "indent"):
            ET.indent(tree, space="  ")
        prepared_path.write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")
        return prepared_path

    @classmethod
    def _resolve_grpc_deadline_default_ms(
        cls, properties: Optional[Dict[str, Any]] = None
    ) -> int:
        candidates: list[Any] = []
        if properties:
            candidates.extend(
                (
                    properties.get("grpc_deadline_default_ms"),
                    properties.get("GRPC_DEADLINE_DEFAULT_MS"),
                )
            )
        candidates.append(os.getenv("JMETER_GRPC_DEADLINE_DEFAULT_MS"))
        for candidate in candidates:
            parsed = cls._coerce_positive_float(candidate)
            if parsed:
                return max(1000, int(parsed))
        return 30000

    @staticmethod
    def _should_rewrite_grpc_deadline(current: str, default_deadline_ms: int) -> bool:
        if not current:
            return True
        literal_match = re.fullmatch(r"\d+(?:\.\d+)?", current)
        if literal_match:
            return float(current) < default_deadline_ms
        prop_match = re.fullmatch(
            r"\$\{__P\(\s*grpc_deadline_ms\s*,\s*(\d+(?:\.\d+)?)\s*\)\}",
            current,
        )
        if prop_match:
            return float(prop_match.group(1)) < default_deadline_ms
        return False

    @classmethod
    def _prepare_runtime_placeholders_if_needed(
        cls,
        script_path: Path,
        *,
        properties: Optional[Dict[str, Any]] = None,
    ) -> Path:
        content = script_path.read_text(encoding="utf-8")
        rendered, changed = cls.render_runtime_placeholder_preview(
            content,
            properties=properties,
        )
        if not changed:
            return script_path
        prepared_path = script_path.with_name(
            f"{script_path.stem}.runtime-prepared{script_path.suffix}"
        )
        prepared_path.write_text(rendered, encoding="utf-8")
        return prepared_path

    @classmethod
    def render_runtime_placeholder_preview(
        cls,
        content: str,
        *,
        properties: Optional[Dict[str, Any]] = None,
    ) -> tuple[str, bool]:
        if "###{" not in content:
            return content, False

        runtime_properties: Dict[str, Any] = {}
        if properties:
            runtime_properties.update(properties)
            raw_variables = properties.get("variables")
            if isinstance(raw_variables, dict):
                for key, value in raw_variables.items():
                    runtime_properties.setdefault(str(key), value)

        changed = False
        unresolved: set[str] = set()

        def _replace(match: re.Match[str]) -> str:
            nonlocal changed
            key = match.group("name").strip()
            if key in runtime_properties and runtime_properties[key] is not None:
                changed = True
                return cls._stringify_property_value(runtime_properties[key])
            unresolved.add(key)
            return match.group(0)

        rendered = cls._PTP_PLACEHOLDER_RE.sub(_replace, content)
        if unresolved:
            raise ValueError(
                "unresolved_jmeter_placeholders: " + ", ".join(sorted(unresolved))
            )
        return rendered, changed

    @classmethod
    def _prepare_iteration_mode_script_if_needed(
        cls,
        script_path: Path,
        *,
        properties: Optional[Dict[str, Any]] = None,
        protocol: Optional[str] = None,
    ) -> Path:
        iteration_count = cls._resolve_iteration_count(properties, protocol=protocol)
        if iteration_count is None:
            return script_path

        root = ET.fromstring(script_path.read_text(encoding="utf-8"))
        updated = False

        for thread_group in root.findall(".//ThreadGroup"):
            scheduler_prop = thread_group.find("./boolProp[@name='ThreadGroup.scheduler']")
            if scheduler_prop is not None and (scheduler_prop.text or "").strip().lower() != "false":
                scheduler_prop.text = "false"
                updated = True

            controller = thread_group.find("./elementProp[@name='ThreadGroup.main_controller']")
            if controller is None:
                continue

            continue_forever_prop = controller.find("./boolProp[@name='LoopController.continue_forever']")
            if continue_forever_prop is not None and (continue_forever_prop.text or "").strip().lower() != "false":
                continue_forever_prop.text = "false"
                updated = True

            loops_prop = controller.find("./stringProp[@name='LoopController.loops']")
            if loops_prop is not None and (loops_prop.text or "").strip() != str(iteration_count):
                loops_prop.text = str(iteration_count)
                updated = True

        if not updated:
            return script_path

        prepared_path = script_path.with_name(
            f"{script_path.stem}.iteration-prepared{script_path.suffix}"
        )
        tree = ET.ElementTree(root)
        if hasattr(ET, "indent"):
            ET.indent(tree, space="  ")
        prepared_path.write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")
        return prepared_path

    @classmethod
    def _prepare_influx_backend_listener_if_needed(
        cls,
        script_path: Path,
        *,
        properties: Optional[Dict[str, Any]] = None,
        backend_classname: Optional[str] = None,
    ) -> Path:
        rendered, changed = cls.render_influx_backend_listener_preview(
            script_path.read_text(encoding="utf-8"),
            properties=properties,
            backend_classname=backend_classname,
        )
        if not changed:
            return script_path
        prepared_path = script_path.with_name(
            f"{script_path.stem}.influx-prepared{script_path.suffix}"
        )
        prepared_path.write_text(rendered, encoding="utf-8")
        return prepared_path

    @classmethod
    def render_influx_backend_listener_preview(
        cls,
        content: str,
        *,
        properties: Optional[Dict[str, Any]] = None,
        backend_classname: Optional[str] = None,
    ) -> tuple[str, bool]:
        if not properties:
            return content, False
        enabled = str(properties.get("jmeter_influx_enabled", "1")).strip().lower()
        if enabled in {"0", "false", "off", "no"}:
            return content, False
        if not str(properties.get("influxdbToken") or "").strip():
            return content, False

        resolved_backend_classname = str(
            backend_classname or cls.INFLUX_BACKEND_CLASSNAME
        ).strip() or cls.INFLUX_BACKEND_CLASSNAME

        root = ET.fromstring(content)
        root_hash_tree = root.find("hashTree")
        if root_hash_tree is None:
            return content, False

        children = list(root_hash_tree)
        testplan_hash_tree = next(
            (child for child in children if child.tag == "hashTree"),
            None,
        )
        if testplan_hash_tree is None:
            return content, False

        direct_listeners = list(testplan_hash_tree.findall("BackendListener"))
        if direct_listeners:
            if any(not cls._is_supported_influx_backend_listener(listener) for listener in direct_listeners):
                return content, False
            changed = False
            for listener in direct_listeners:
                changed = cls._canonicalize_influx_backend_listener(
                    listener,
                    backend_classname=resolved_backend_classname,
                ) or changed
            if changed:
                tree = ET.ElementTree(root)
                if hasattr(ET, "indent"):
                    ET.indent(tree, space="  ")
                return ET.tostring(root, encoding="unicode"), True
            return content, False

        active_listeners = [
            listener
            for listener in root.findall(".//BackendListener")
            if cls._is_enabled_backend_listener(listener)
        ]
        for listener in active_listeners:
            if not cls._is_supported_influx_backend_listener(listener):
                continue
            changed = cls._canonicalize_influx_backend_listener(
                listener,
                backend_classname=resolved_backend_classname,
            )
            if changed:
                tree = ET.ElementTree(root)
                if hasattr(ET, "indent"):
                    ET.indent(tree, space="  ")
                return ET.tostring(root, encoding="unicode"), True
            return content, False

        if active_listeners:
            return content, False

        backend_listener = cls._build_influx_backend_listener(
            backend_classname=resolved_backend_classname
        )
        testplan_hash_tree.append(backend_listener)
        testplan_hash_tree.append(ET.Element("hashTree"))

        tree = ET.ElementTree(root)
        if hasattr(ET, "indent"):
            ET.indent(tree, space="  ")
        rendered = ET.tostring(root, encoding="unicode")
        return rendered, True

    @staticmethod
    def _is_enabled_backend_listener(listener: ET.Element) -> bool:
        enabled = str(listener.attrib.get("enabled", "")).strip().lower()
        return not enabled or enabled == "true"

    @classmethod
    def _is_supported_influx_backend_listener(cls, listener: ET.Element) -> bool:
        classname = listener.find("./stringProp[@name='classname']")
        current = (classname.text or "").strip() if classname is not None else ""
        return current in cls.INFLUX_BACKEND_CLASSNAME_CANDIDATES

    @classmethod
    def _build_influx_backend_listener(
        cls,
        *,
        backend_classname: str,
    ) -> ET.Element:
        backend_listener = ET.Element(
            "BackendListener",
            {
                "guiclass": "BackendListenerGui",
                "testclass": "BackendListener",
                "testname": "Backend Listener",
                "enabled": "true",
            },
        )
        arguments = ET.SubElement(
            backend_listener,
            "elementProp",
            {
                "name": "arguments",
                "elementType": "Arguments",
                "guiclass": "ArgumentsPanel",
                "testclass": "Arguments",
                "enabled": "true",
            },
        )
        collection = ET.SubElement(
            arguments,
            "collectionProp",
            {"name": "Arguments.arguments"},
        )
        for name, value in cls._INFLUX_ARGUMENTS:
            argument = ET.SubElement(
                collection,
                "elementProp",
                {"name": name, "elementType": "Argument"},
            )
            ET.SubElement(argument, "stringProp", {"name": "Argument.name"}).text = name
            ET.SubElement(argument, "stringProp", {"name": "Argument.value"}).text = value
            ET.SubElement(argument, "stringProp", {"name": "Argument.metadata"}).text = "="
        ET.SubElement(backend_listener, "stringProp", {"name": "classname"}).text = (
            backend_classname
        )
        return backend_listener

    @classmethod
    def _canonicalize_influx_backend_listener(
        cls,
        listener: ET.Element,
        *,
        backend_classname: str,
    ) -> bool:
        expected = cls._build_influx_backend_listener(backend_classname=backend_classname)
        changed = False
        for key in ("guiclass", "testclass", "testname"):
            expected_value = expected.attrib.get(key)
            if listener.attrib.get(key) != expected_value:
                listener.set(key, expected_value or "")
                changed = True
        classname = listener.find("./stringProp[@name='classname']")
        if classname is None:
            classname = ET.SubElement(listener, "stringProp", {"name": "classname"})
            changed = True
        if (classname.text or "").strip() != backend_classname:
            classname.text = backend_classname
            changed = True
        queue_size = listener.find("./stringProp[@name='queueSize']")
        if queue_size is not None:
            listener.remove(queue_size)
            changed = True
        existing_arguments = listener.find("./elementProp[@name='arguments']")
        if existing_arguments is not None:
            listener.remove(existing_arguments)
            changed = True
        expected_arguments = expected.find("./elementProp[@name='arguments']")
        if expected_arguments is not None:
            listener.insert(0, expected_arguments)
        return changed

    @staticmethod
    def _rewrite_grpc_folder_props(content: str, proto_dir: str) -> tuple[str, bool]:
        updated = False
        escaped_dir = proto_dir.replace("\\", "\\\\")
        patterns = (
            r'(<stringProp name="GRPCSampler\.protoFolder">)(.*?)(</stringProp>)',
            r'(<stringProp name="GRPCSampler\.libFolder">)(.*?)(</stringProp>)',
        )
        updated_content = content
        for pattern in patterns:
            next_content, count = re.subn(
                pattern,
                rf"\1{escaped_dir}\3",
                updated_content,
                flags=re.DOTALL,
            )
            if count:
                updated = True
                updated_content = next_content
        return updated_content, updated
