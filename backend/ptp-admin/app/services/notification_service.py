"""
通知服务

发送实时通知给客户端
"""

import logging
import base64
import hashlib
import hmac
import json
import os
import re
import time
from pathlib import Path
from string import Formatter
from typing import Optional, Dict, Any
from datetime import datetime, timezone
from enum import Enum
from urllib.parse import parse_qsl, urlparse, urlunparse

import httpx
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.alert_event import RunAlertEvent
from app.models.notification import WebhookConfig, WebhookSendRecord
from app.models.plan import Plan
from app.models.run import Run
from app.models.task import Task
from app.core.websocket_manager import manager
from common.config.settings import REPO_ROOT
from app.schemas.notification import (
    WebhookChannel,
    WebhookConfigCreateRequest,
    WebhookConfigResponse,
    WebhookConfigUpdateRequest,
    WebhookEventType,
    WebhookSendRecordResponse,
    WebhookSendRequest,
    WebhookSendResponse,
    WebhookSignatureType,
    WebhookTemplatePreviewRequest,
    WebhookTemplatePreviewResponse,
)

logger = logging.getLogger(__name__)


class NotificationType(Enum):
    """通知类型"""

    TASK_STATUS = "task_status"
    TASK_PROGRESS = "task_progress"
    TASK_COMPLETED = "task_completed"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_RESULT = "approval_result"
    REPORT_READY = "report_ready"
    SYSTEM_ALERT = "system_alert"


class NotificationService:
    """通知服务"""

    WEBHOOK_REQUIRED_VARIABLES = [
        "run_id",
        "plan_name",
        "target_tps",
        "p95",
        "p99",
        "error_rate",
        "grafana_url",
        "report_url",
        "ai_summary",
    ]

    WEBHOOK_EVENT_LABELS = {
        WebhookEventType.PLAN_RUN_COMPLETED: "批次执行完成",
        WebhookEventType.PLAN_RUN_FAILED: "批次执行失败",
        WebhookEventType.THRESHOLD_BREACHED: "压测指标超阈值",
        WebhookEventType.REGRESSION_BLOCKED: "Regression 阻断",
    }

    WEBHOOK_DEFAULT_TEMPLATE = (
        "**{plan_name} {execution_entity_label} #{plan_run_id} {status_result_label}**\n"
        "- {execution_kind_label}：#{plan_id} · {plan_exec_type_label} · {round_label}\n"
        "{execution_summary_line}\n"
        "- 范围：{task_run_summary}\n"
        "{metrics_summary_line}\n"
        "- 结果：{status_detail_summary}\n"
        "- 明细：{plan_run_url}"
    )
    WEBHOOK_THRESHOLD_TEMPLATE = (
        "**{alertname} {severity_label}**\n"
        "- 状态：{alert_status_label} · 来源 {source} · 订阅 {subscription_label}\n"
        "- 对象：{target_label}\n"
        "- 摘要：{alert_summary}\n"
        "- 原因：{alert_reason}\n"
        "- 动作：{action_status_label}\n"
        "- 链接：{alert_url}"
    )

    @classmethod
    def preview_webhook_template(
        cls, request: WebhookTemplatePreviewRequest
    ) -> WebhookTemplatePreviewResponse:
        """渲染 webhook 通知模板并生成目标平台 payload，不发送外网请求。"""
        variables = {
            key: cls._stringify_webhook_value(value)
            for key, value in dict(request.variables or {}).items()
        }
        event_type = WebhookEventType(request.event_type)
        channel = WebhookChannel(request.channel)
        event_label = cls.WEBHOOK_EVENT_LABELS[event_type]
        variables.setdefault("event_label", event_label)
        variables.setdefault("notification_env", cls._notification_env_label())
        if event_type == WebhookEventType.THRESHOLD_BREACHED:
            variables.setdefault("alert_reason", "-")
        else:
            variables.setdefault("execution_entity_label", "批次")
            variables.setdefault("execution_kind_label", "计划")
            cls._ensure_plan_run_webhook_summary_lines(variables)

        template = request.template or cls._default_template_for_event_type(event_type)
        referenced_variables = cls._extract_template_variables(template)
        missing_variables = sorted(
            {
                key
                for key in referenced_variables
                if key not in variables or variables.get(key) in {None, ""}
            }
        )
        covered_variables = sorted(
            key
            for key in referenced_variables
            if key in variables and variables.get(key) not in {None, ""}
        )
        rendered_text = cls._render_webhook_template(template, variables)
        title = cls._title_with_env(request.title or event_label, variables)
        warnings: list[str] = []
        required_missing = [
            key
            for key in cls._required_variables_for_event_type(event_type)
            if key not in variables or variables.get(key) in {None, ""}
        ]
        if required_missing:
            warnings.append(
                "模板变量缺失："
                + "、".join(required_missing)
                + "；发送前建议补齐以满足 OpenLoadHub 通知规则。"
            )
        if missing_variables:
            warnings.append(
                "自定义模板引用了未提供变量："
                + "、".join(missing_variables)
                + "；预览中已用 '-' 兜底。"
            )

        return WebhookTemplatePreviewResponse(
            channel=channel,
            event_type=event_type,
            title=title,
            rendered_text=rendered_text,
            payload=cls._build_webhook_payload(
                channel=channel,
                title=title,
                rendered_text=rendered_text,
            ),
            covered_variables=covered_variables,
            missing_variables=missing_variables,
            warnings=warnings,
            dry_run=True,
        )

    @classmethod
    def send_webhook(
        cls,
        db: Session,
        request: WebhookSendRequest,
        *,
        user_id: Optional[int] = None,
    ) -> WebhookSendResponse:
        """发送 webhook，并持久化发送记录。"""
        return cls._dispatch_webhook(
            db,
            channel=WebhookChannel(request.channel),
            event_type=WebhookEventType(request.event_type),
            variables=dict(request.variables or {}),
            template=request.template,
            title=request.title,
            webhook_url=str(request.webhook_url),
            signature_type=WebhookSignatureType(request.signature_type).value,
            signing_secret=request.signing_secret,
            timeout_seconds=request.timeout_seconds,
            max_retry_count=request.max_retry_count,
            retry_interval_seconds=request.retry_interval_seconds,
            user_id=user_id,
            config_id=None,
            trigger_source="manual",
        )

    @classmethod
    def create_webhook_config(
        cls,
        db: Session,
        request: WebhookConfigCreateRequest,
        *,
        user_id: Optional[int] = None,
    ) -> WebhookConfigResponse:
        config = WebhookConfig(
            name=request.name,
            channel=WebhookChannel(request.channel).value,
            event_types=[
                WebhookEventType(event_type).value for event_type in request.event_types
            ],
            webhook_url=str(request.webhook_url),
            signature_type=WebhookSignatureType(request.signature_type).value,
            signing_secret=cls._normalize_signing_secret(
                WebhookSignatureType(request.signature_type).value,
                request.signing_secret,
            ),
            enabled=bool(request.enabled),
            template=request.template,
            title=request.title,
            timeout_seconds=request.timeout_seconds,
            max_retry_count=request.max_retry_count,
            retry_interval_seconds=request.retry_interval_seconds,
            created_by=user_id,
        )
        db.add(config)
        db.commit()
        db.refresh(config)
        return cls._config_to_response(config)

    @classmethod
    def update_webhook_config(
        cls,
        db: Session,
        config_id: int,
        request: WebhookConfigUpdateRequest,
    ) -> WebhookConfigResponse | None:
        config = (
            db.query(WebhookConfig).filter(WebhookConfig.config_id == config_id).first()
        )
        if not config:
            return None

        values = request.model_dump(exclude_unset=True)
        if "channel" in values and values["channel"] is not None:
            config.channel = WebhookChannel(values["channel"]).value
        if "event_types" in values and values["event_types"] is not None:
            config.event_types = [
                WebhookEventType(event_type).value
                for event_type in values["event_types"]
            ]
        if "signature_type" in values and values["signature_type"] is not None:
            config.signature_type = WebhookSignatureType(values["signature_type"]).value
            if config.signature_type == WebhookSignatureType.NONE.value:
                config.signing_secret = None
        if "signing_secret" in values:
            config.signing_secret = cls._normalize_signing_secret(
                str(config.signature_type or WebhookSignatureType.NONE.value),
                values["signing_secret"],
            )
        for key in (
            "name",
            "webhook_url",
            "enabled",
            "template",
            "title",
            "timeout_seconds",
            "max_retry_count",
            "retry_interval_seconds",
        ):
            if key in values:
                setattr(config, key, values[key])

        db.add(config)
        db.commit()
        db.refresh(config)
        return cls._config_to_response(config)

    @classmethod
    def list_webhook_configs(
        cls,
        db: Session,
        *,
        enabled: Optional[bool] = None,
    ) -> list[WebhookConfigResponse]:
        query = db.query(WebhookConfig)
        if enabled is not None:
            query = query.filter(WebhookConfig.enabled == enabled)
        configs = query.order_by(WebhookConfig.config_id.desc()).all()
        return [cls._config_to_response(config) for config in configs]

    @classmethod
    def trigger_plan_run_terminal_webhooks(
        cls,
        db: Session,
        plan_run: Any,
        *,
        user_id: Optional[int] = None,
    ) -> list[WebhookSendResponse]:
        if not settings.PTP_ENABLE_NOTIFICATIONS:
            return []
        event_type = cls._plan_run_event_type(plan_run)
        if event_type is None:
            return []

        variables = cls._build_plan_run_webhook_variables(db, plan_run)
        responses: list[WebhookSendResponse] = []
        default_title_override = (
            str(variables.get("event_label_override") or "").strip() or None
        )
        configs = (
            db.query(WebhookConfig)
            .filter(WebhookConfig.enabled.is_(True))
            .order_by(WebhookConfig.config_id.asc())
            .all()
        )
        for config in configs:
            if event_type.value not in set(config.event_types or []):
                continue
            try:
                responses.append(
                    cls._dispatch_webhook(
                        db,
                        channel=WebhookChannel(config.channel),
                        event_type=event_type,
                        variables=variables,
                        template=config.template,
                        title=config.title or default_title_override,
                        webhook_url=str(config.webhook_url),
                        signature_type=str(
                            config.signature_type or WebhookSignatureType.NONE.value
                        ),
                        signing_secret=config.signing_secret,
                        timeout_seconds=float(config.timeout_seconds or 5.0),
                        max_retry_count=int(config.max_retry_count or 0),
                        retry_interval_seconds=float(
                            config.retry_interval_seconds or 0.0
                        ),
                        user_id=user_id,
                        config_id=int(config.config_id),
                        trigger_source="plan_run_terminal",
                    )
                )
            except Exception:
                # 通知失败不能反向破坏 PlanRun 终态收敛。
                logger.exception(
                    "PlanRun terminal webhook config %s dispatch failed",
                    getattr(config, "config_id", None),
                )
                db.rollback()
        return responses

    @classmethod
    def trigger_threshold_breached_webhooks(
        cls,
        db: Session,
        event: Any,
        *,
        user_id: Optional[int] = None,
    ) -> list[WebhookSendResponse]:
        if not settings.PTP_ENABLE_NOTIFICATIONS:
            return []
        variables = cls._build_alert_event_webhook_variables(db, event)
        responses: list[WebhookSendResponse] = []
        configs = (
            db.query(WebhookConfig)
            .filter(WebhookConfig.enabled.is_(True))
            .order_by(WebhookConfig.config_id.asc())
            .all()
        )
        for config in configs:
            if WebhookEventType.THRESHOLD_BREACHED.value not in set(
                config.event_types or []
            ):
                continue
            try:
                responses.append(
                    cls._dispatch_webhook(
                        db,
                        channel=WebhookChannel(config.channel),
                        event_type=WebhookEventType.THRESHOLD_BREACHED,
                        variables=variables,
                        template=config.template,
                        title=config.title,
                        webhook_url=str(config.webhook_url),
                        signature_type=str(
                            config.signature_type or WebhookSignatureType.NONE.value
                        ),
                        signing_secret=config.signing_secret,
                        timeout_seconds=float(config.timeout_seconds or 5.0),
                        max_retry_count=int(config.max_retry_count or 0),
                        retry_interval_seconds=float(
                            config.retry_interval_seconds or 0.0
                        ),
                        user_id=user_id,
                        config_id=int(config.config_id),
                        trigger_source="alert_event",
                    )
                )
            except Exception:
                logger.exception(
                    "Alert event %s webhook config %s dispatch failed",
                    getattr(event, "event_id", None),
                    getattr(config, "config_id", None),
                )
                db.rollback()
        return responses

    @classmethod
    def _dispatch_webhook(
        cls,
        db: Session,
        *,
        channel: WebhookChannel,
        event_type: WebhookEventType,
        variables: dict[str, Any],
        template: Optional[str],
        title: Optional[str],
        webhook_url: str,
        signature_type: str,
        signing_secret: Optional[str],
        timeout_seconds: float,
        max_retry_count: int,
        retry_interval_seconds: float,
        user_id: Optional[int],
        config_id: Optional[int],
        trigger_source: str,
    ) -> WebhookSendResponse:
        preview = cls.preview_webhook_template(
            WebhookTemplatePreviewRequest(
                channel=channel,
                event_type=event_type,
                variables=variables,
                template=template,
                title=title,
            )
        )
        parsed_url = urlparse(str(webhook_url))
        record = WebhookSendRecord(
            channel=(
                preview.channel.value
                if hasattr(preview.channel, "value")
                else preview.channel
            ),
            event_type=(
                preview.event_type.value
                if hasattr(preview.event_type, "value")
                else preview.event_type
            ),
            status="pending",
            title=preview.title,
            rendered_text=preview.rendered_text,
            payload=preview.payload,
            variables=dict(variables or {}),
            webhook_url_masked=cls._mask_webhook_url(str(webhook_url)),
            webhook_host=parsed_url.netloc or None,
            attempt_count=0,
            config_id=config_id,
            trigger_source=trigger_source,
            created_by=user_id,
        )
        db.add(record)
        db.commit()
        db.refresh(record)

        attempts = max(1, min(int(max_retry_count or 0), 5) + 1)
        for attempt_index in range(attempts):
            record.attempt_count = int(record.attempt_count or 0) + 1
            record.sent_at = datetime.now(timezone.utc)
            try:
                outbound_payload = cls._prepare_webhook_payload(
                    channel=channel,
                    payload=preview.payload,
                    signature_type=signature_type,
                    signing_secret=signing_secret,
                )
                response = cls._post_webhook_payload(
                    url=str(webhook_url),
                    payload=outbound_payload,
                    timeout_seconds=timeout_seconds,
                )
                record.http_status_code = response.status_code
                record.response_body = cls._redact_sensitive_webhook_text(
                    cls._truncate_response_body(response.text),
                    webhook_url=webhook_url,
                    signing_secret=signing_secret,
                    outbound_payload=outbound_payload,
                )
                provider_error = cls._webhook_provider_error(channel, response)
                if 200 <= response.status_code < 300 and provider_error is None:
                    record.status = "success"
                    record.error_message = None
                    break
                record.status = "failed"
                record.error_message = cls._redact_sensitive_webhook_text(
                    provider_error or f"webhook_http_status_{response.status_code}",
                    webhook_url=webhook_url,
                    signing_secret=signing_secret,
                    outbound_payload=outbound_payload,
                )
            except Exception as exc:
                record.status = "failed"
                record.error_message = cls._redact_sensitive_webhook_text(
                    str(exc),
                    webhook_url=webhook_url,
                    signing_secret=signing_secret,
                    outbound_payload=None,
                )
            if attempt_index < attempts - 1 and retry_interval_seconds > 0:
                time.sleep(retry_interval_seconds)

        db.add(record)
        db.commit()
        db.refresh(record)
        return WebhookSendResponse(
            record=WebhookSendRecordResponse.model_validate(record),
            preview=preview,
        )

    @classmethod
    def sync_webhook_configs_from_file(
        cls,
        db: Session,
        *,
        config_file: str | Path,
    ) -> list[WebhookConfigResponse]:
        resolved_config_file = cls._resolve_webhook_config_path(config_file)
        payload = json.loads(resolved_config_file.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("Webhook bootstrap config must be a JSON array.")

        synced_configs: list[WebhookConfigResponse] = []
        for index, item in enumerate(payload):
            if not isinstance(item, dict):
                raise ValueError(
                    f"Webhook bootstrap entry #{index + 1} must be a JSON object."
                )
            create_payload = cls._build_webhook_config_request_from_mapping(
                item,
                config_file=resolved_config_file,
            )
            if create_payload is None:
                continue
            existing = (
                db.query(WebhookConfig)
                .filter(WebhookConfig.name == create_payload.name)
                .first()
            )
            if existing is None:
                synced_configs.append(cls.create_webhook_config(db, create_payload))
                continue

            update_payload = WebhookConfigUpdateRequest.model_validate(
                create_payload.model_dump()
            )
            updated = cls.update_webhook_config(
                db,
                int(existing.config_id),
                update_payload,
            )
            if updated is None:
                raise RuntimeError(
                    f"Failed to update webhook config {existing.config_id}."
                )
            synced_configs.append(updated)
        return synced_configs

    @classmethod
    def list_webhook_records(
        cls,
        db: Session,
        *,
        limit: int = 20,
        status: Optional[str] = None,
    ) -> list[WebhookSendRecordResponse]:
        query = db.query(WebhookSendRecord)
        if status:
            query = query.filter(WebhookSendRecord.status == status)
        records = (
            query.order_by(WebhookSendRecord.record_id.desc())
            .limit(max(1, min(limit, 100)))
            .all()
        )
        return [WebhookSendRecordResponse.model_validate(record) for record in records]

    @classmethod
    def _config_to_response(cls, config: WebhookConfig) -> WebhookConfigResponse:
        parsed_url = urlparse(str(config.webhook_url))
        return WebhookConfigResponse(
            config_id=int(config.config_id),
            name=str(config.name),
            channel=WebhookChannel(config.channel),
            event_types=[
                WebhookEventType(event_type)
                for event_type in (config.event_types or [])
            ],
            webhook_url_masked=cls._mask_webhook_url(str(config.webhook_url)),
            webhook_host=parsed_url.netloc or None,
            signature_type=WebhookSignatureType(
                config.signature_type or WebhookSignatureType.NONE.value
            ),
            signing_secret_set=bool(config.signing_secret),
            enabled=bool(config.enabled),
            template=config.template,
            title=config.title,
            timeout_seconds=float(config.timeout_seconds or 5.0),
            max_retry_count=int(config.max_retry_count or 0),
            retry_interval_seconds=float(config.retry_interval_seconds or 0.0),
            created_by=config.created_by,
            created_at=config.created_at,
            updated_at=config.updated_at,
        )

    @classmethod
    def _default_template_for_event_type(cls, event_type: WebhookEventType) -> str:
        if event_type == WebhookEventType.THRESHOLD_BREACHED:
            return cls.WEBHOOK_THRESHOLD_TEMPLATE
        return cls.WEBHOOK_DEFAULT_TEMPLATE

    @classmethod
    def _build_webhook_config_request_from_mapping(
        cls,
        payload: dict[str, Any],
        *,
        config_file: Path,
    ) -> WebhookConfigCreateRequest | None:
        normalized = dict(payload)
        is_enabled = bool(normalized.get("enabled", True))
        template_file = normalized.pop("template_file", None)
        if template_file:
            normalized["template"] = cls._resolve_webhook_config_path(
                template_file,
                base_dir=config_file.parent,
            ).read_text(encoding="utf-8")

        webhook_url = cls._resolve_secret_value(
            direct_value=normalized.pop("webhook_url", None),
            env_name=normalized.pop("webhook_url_env", None),
            field_name="webhook_url",
            required=is_enabled,
        )
        if not webhook_url:
            return None
        normalized["webhook_url"] = webhook_url
        signing_secret = cls._resolve_secret_value(
            direct_value=normalized.pop("signing_secret", None),
            env_name=normalized.pop("signing_secret_env", None),
            field_name="signing_secret",
            required=False,
        )
        if signing_secret is not None:
            normalized["signing_secret"] = signing_secret
        return WebhookConfigCreateRequest.model_validate(normalized)

    @staticmethod
    def _resolve_secret_value(
        *,
        direct_value: Any,
        env_name: Any,
        field_name: str,
        required: bool = True,
    ) -> str | None:
        direct_text = str(direct_value or "").strip()
        if direct_text:
            return direct_text

        env_name_text = str(env_name or "").strip()
        if env_name_text:
            resolved = str(os.getenv(env_name_text) or "").strip()
            if resolved:
                return resolved
            if not required:
                return None
            raise ValueError(
                f"Webhook bootstrap env `{env_name_text}` for `{field_name}` is empty."
            )

        if required:
            raise ValueError(
                f"Webhook bootstrap entry must set `{field_name}` or `{field_name}_env`."
            )
        return None

    @staticmethod
    def _resolve_webhook_config_path(
        raw_path: str | Path,
        *,
        base_dir: Path | None = None,
    ) -> Path:
        candidate = Path(raw_path)
        if candidate.is_absolute():
            return candidate
        if base_dir is not None:
            return (base_dir / candidate).resolve()
        return (REPO_ROOT / candidate).resolve()

    @classmethod
    def _required_variables_for_event_type(
        cls, event_type: WebhookEventType
    ) -> list[str]:
        if event_type == WebhookEventType.THRESHOLD_BREACHED:
            return [
                "alertname",
                "severity_label",
                "alert_status_label",
                "alert_summary",
                "alert_url",
            ]
        return cls.WEBHOOK_REQUIRED_VARIABLES

    @staticmethod
    def _plan_run_event_type(plan_run: Any) -> WebhookEventType | None:
        status = getattr(plan_run, "status", None)
        status_value = getattr(status, "value", status)
        if status_value == "succeeded":
            return WebhookEventType.PLAN_RUN_COMPLETED
        if status_value in {"failed", "stopped"}:
            return WebhookEventType.PLAN_RUN_FAILED
        return None

    @classmethod
    def _build_alert_event_webhook_variables(
        cls, db: Session, event: Any
    ) -> dict[str, Any]:
        labels = getattr(event, "labels", None)
        annotations = getattr(event, "annotations", None)
        labels = labels if isinstance(labels, dict) else {}
        annotations = annotations if isinstance(annotations, dict) else {}
        run_id = getattr(event, "run_id", None)
        task_id = getattr(event, "task_id", None)
        run = cls._load_run_for_webhook(db, run_id)
        task = cls._load_task_for_webhook(db, task_id)
        if task is None and run is not None:
            task = cls._load_task_for_webhook(db, getattr(run, "task_id", None))
        public_base = str(getattr(settings, "PTP_PUBLIC_BASE_URL", "") or "").strip()
        run_url = (
            f"{public_base.rstrip('/')}/runs/{run_id}"
            if public_base and run_id
            else f"/runs/{run_id}" if run_id else "-"
        )
        alert_url = (
            getattr(event, "dashboard_url", None)
            or annotations.get("dashboard_url")
            or annotations.get("grafana_url")
            or run_url
            or "-"
        )
        alertname = getattr(event, "alertname", None) or labels.get("alertname") or "-"
        severity = getattr(event, "severity", None) or labels.get("severity") or "-"
        status = getattr(event, "status", None) or "-"
        source = getattr(event, "source", None) or labels.get("source") or "-"
        subscription = getattr(event, "subscription", None) or "-"
        summary = (
            annotations.get("summary")
            or annotations.get("description")
            or labels.get("summary")
            or alertname
        )
        target_parts = []
        if run_id:
            target_parts.append(f"Run #{run_id}")
        if task is not None:
            target_parts.append(f"任务 {getattr(task, 'name', None) or task_id}")
        elif task_id:
            target_parts.append(f"任务 #{task_id}")
        service_label = labels.get("service") or labels.get("component")
        if service_label:
            target_parts.append(f"服务 {service_label}")
        instance_label = labels.get("target_instance") or labels.get("instance")
        if instance_label:
            target_parts.append(f"实例 {instance_label}")
        target_label = " · ".join(target_parts) or "-"
        action_status = getattr(event, "action_status", None) or "recorded"
        alert_reason = cls._format_alert_reason(
            alertname=alertname,
            summary=summary,
            annotations=annotations,
            labels=labels,
            alert_url=alert_url,
        )
        return {
            "event_id": getattr(event, "event_id", None) or "-",
            "run_id": run_id or "-",
            "task_id": task_id or "-",
            "plan_run_id": getattr(event, "plan_run_id", None) or "-",
            "mixed_run_id": getattr(event, "mixed_run_id", None) or "-",
            "alertname": alertname,
            "severity": severity,
            "severity_label": str(severity).upper(),
            "priority": getattr(event, "priority", None) or "-",
            "status": status,
            "alert_status_label": str(status).upper(),
            "source": source,
            "subscription": subscription,
            "subscription_label": subscription,
            "alert_summary": summary,
            "alert_reason": alert_reason,
            "action_status": action_status,
            "action_status_label": cls._format_alert_action_status_label(action_status),
            "target_label": target_label,
            "alert_url": alert_url,
            "grafana_url": alert_url,
            "run_url": run_url,
            "labels": labels,
            "annotations": annotations,
        }

    @classmethod
    def _build_plan_run_webhook_variables(
        cls, db: Session, plan_run: Any
    ) -> dict[str, Any]:
        plan_run_id = getattr(plan_run, "plan_run_id", None)
        plan_id = getattr(plan_run, "plan_id", None)
        status = getattr(plan_run, "status", None)
        status_value = getattr(status, "value", status)
        status_value = str(status_value or "").strip() or "-"
        plan = cls._load_plan_for_webhook(db, plan_id)
        is_mixed_run = str(getattr(plan, "domain_type", "") or "") == "mixed_run"
        launched_run_ids = cls._resolve_plan_run_launched_run_ids(plan_run)
        runs = cls._load_runs_for_webhook(db, launched_run_ids)
        task_summary = cls._build_plan_task_summary_for_webhook(db, plan, runs)
        metrics = cls._build_run_metrics_for_webhook(runs)
        peak_qps_context = cls._build_run_group_peak_qps_context_for_webhook(
            db, runs, current_total_tps=metrics.get("total_tps")
        )
        peak_qps = peak_qps_context["peak_qps"]
        metrics["peak_qps"] = round(peak_qps, 4) if peak_qps is not None else "-"
        metrics["peak_qps_label"] = cls._format_peak_qps_label(peak_qps)
        compact_metric_summary = cls._should_use_compact_metric_summary(
            is_mixed_run=is_mixed_run,
            run_count=metrics["run_count"],
            task_count=task_summary["task_count"],
            endpoint_count=peak_qps_context["endpoint_count"],
        )
        stop_alert_event = cls._load_plan_run_stop_alert_event(
            db,
            plan_run_id=plan_run_id,
            launched_run_ids=launched_run_ids,
            is_mixed_run=is_mixed_run,
        )
        stop_reason_summary = cls._format_plan_run_stop_reason_summary(
            plan_run=plan_run,
            alert_event=stop_alert_event,
            is_mixed_run=is_mixed_run,
        )
        display_status_value = cls._derive_plan_run_display_status(
            status_value=status_value,
            status_detail=str(getattr(plan_run, "status_detail", None) or "").strip(),
            runs=runs,
        )
        run_result_summary = cls._format_run_result_summary(
            status_value=display_status_value,
            launched_run_ids=launched_run_ids,
            runs=runs,
        )
        duration_seconds = getattr(plan_run, "duration_seconds", None)
        plan_name = getattr(plan_run, "plan_name", None) or getattr(plan, "name", None)
        plan_public_base = str(
            getattr(settings, "PTP_PUBLIC_BASE_URL", "") or ""
        ).strip()
        detail_path = "mixed-runs" if is_mixed_run else "plan-runs"
        plan_run_url = (
            f"{plan_public_base.rstrip('/')}/{detail_path}/{plan_run_id}"
            if plan_public_base and plan_run_id
            else f"/{detail_path}/{plan_run_id}" if plan_run_id else "-"
        )
        status_summary = (
            f"{'混压' if is_mixed_run else '批次'}执行完成"
            if display_status_value == "succeeded"
            else (
                f"{'混压' if is_mixed_run else '批次'}执行已停止"
                if display_status_value == "stopped"
                else (
                    f"{'混压' if is_mixed_run else '批次'}执行失败"
                    if display_status_value == "failed"
                    else f"{'混压' if is_mixed_run else '批次'}状态更新"
                )
            )
        )
        status_detail = str(getattr(plan_run, "status_detail", None) or "").strip()
        event_label_override = (
            f"{'混压' if is_mixed_run else '批次'}执行已停止"
            if display_status_value == "stopped"
            else None
        )
        task_run_summary = cls._format_task_run_summary(
            run_count=metrics["run_count"] or len(launched_run_ids),
            task_count=task_summary["task_count"],
            env_label=task_summary["env_list_label"],
            engine_label=task_summary["engine_types_label"],
        )
        summary_lines = cls._build_plan_run_webhook_summary_lines(
            compact_metric_summary=compact_metric_summary,
            run_result_summary=run_result_summary,
            duration_label=cls._format_duration_label(duration_seconds),
            total_tps_label=metrics["total_tps_label"],
            peak_qps_label=metrics["peak_qps_label"],
            p95_label=metrics["p95_label"],
            error_rate_label=metrics["error_rate_label"],
        )
        return {
            "run_id": plan_run_id,
            "plan_run_id": plan_run_id,
            "plan_id": plan_id,
            "plan_name": plan_name or f"plan#{plan_id or '-'}",
            "plan_run_status": display_status_value,
            "plan_run_status_raw": status_value,
            "is_mixed_run": is_mixed_run,
            "compact_metric_summary": compact_metric_summary,
            "execution_entity_label": "混压" if is_mixed_run else "批次",
            "execution_kind_label": "混压计划" if is_mixed_run else "计划",
            "event_label_override": event_label_override,
            "status_summary": status_summary,
            "status_result_label": cls._format_status_result_label(
                display_status_value
            ),
            "status_detail": status_detail or "-",
            "stop_reason_summary": stop_reason_summary,
            "status_detail_summary": cls._format_status_detail_summary(
                status_value=display_status_value,
                status_detail=status_detail,
                run_result_summary=run_result_summary,
                stop_reason_summary=stop_reason_summary,
            ),
            "duration_seconds": duration_seconds or "-",
            "duration_label": cls._format_duration_label(duration_seconds),
            "execution_summary_line": summary_lines["execution_summary_line"],
            "metrics_summary_line": summary_lines["metrics_summary_line"],
            "launched_run_ids": launched_run_ids,
            "run_count": metrics["run_count"],
            "success_run_count": metrics["success_run_count"],
            "failed_run_count": metrics["failed_run_count"],
            "stopped_run_count": metrics["stopped_run_count"],
            "run_result_summary": run_result_summary,
            "plan_exec_type": cls._enum_value(getattr(plan, "exec_type", None)) or "-",
            "plan_exec_type_label": cls._format_plan_exec_type_label(
                cls._enum_value(getattr(plan, "exec_type", None))
            ),
            "round": getattr(plan_run, "round", None) or "-",
            "round_total": getattr(plan, "total_round", None) or "-",
            "round_label": cls._format_round_label(
                getattr(plan_run, "round", None),
                getattr(plan, "total_round", None),
            ),
            "task_count": task_summary["task_count"],
            "task_run_summary": task_run_summary,
            "env_list": task_summary["env_list_label"],
            "engine_types": task_summary["engine_types_label"],
            "business_lines": task_summary["business_lines_label"],
            "target_tps": metrics["total_tps_label"],
            "total_tps": metrics["total_tps"],
            "total_tps_label": metrics["total_tps_label"],
            "peak_qps": metrics["peak_qps"],
            "peak_qps_label": metrics["peak_qps_label"],
            "total_requests": metrics["total_requests"],
            "total_requests_label": metrics["total_requests_label"],
            "p95": metrics["p95"],
            "p95_label": metrics["p95_label"],
            "p99": metrics["p99"],
            "p99_label": metrics["p99_label"],
            "error_rate": metrics["error_rate"],
            "error_rate_label": metrics["error_rate_label"],
            "grafana_url": "-",
            "report_url": plan_run_url,
            "plan_run_url": plan_run_url,
            "ai_summary": status_detail or "-",
        }

    @classmethod
    def _ensure_plan_run_webhook_summary_lines(cls, variables: dict[str, str]) -> None:
        compact_metric_summary = cls._webhook_truthy(
            variables.get("compact_metric_summary")
        ) or cls._webhook_truthy(variables.get("is_mixed_run"))
        lines = cls._build_plan_run_webhook_summary_lines(
            compact_metric_summary=compact_metric_summary,
            run_result_summary=variables.get("run_result_summary") or "-",
            duration_label=variables.get("duration_label") or "暂无",
            total_tps_label=variables.get("total_tps_label") or "暂无",
            peak_qps_label=variables.get("peak_qps_label")
            or variables.get("total_tps_label")
            or "暂无",
            p95_label=variables.get("p95_label") or "暂无",
            error_rate_label=variables.get("error_rate_label") or "暂无",
        )
        variables.setdefault("execution_summary_line", lines["execution_summary_line"])
        variables.setdefault("metrics_summary_line", lines["metrics_summary_line"])

    @staticmethod
    def _webhook_truthy(value: Any) -> bool:
        return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}

    @staticmethod
    def _build_plan_run_webhook_summary_lines(
        *,
        compact_metric_summary: bool,
        run_result_summary: str,
        duration_label: str,
        total_tps_label: str,
        peak_qps_label: str,
        p95_label: str,
        error_rate_label: str,
    ) -> dict[str, str]:
        if compact_metric_summary:
            return {
                "execution_summary_line": f"- 执行：{run_result_summary}",
                "metrics_summary_line": f"- 指标：峰值 QPS {peak_qps_label}",
            }
        return {
            "execution_summary_line": (
                f"- 执行：{run_result_summary} · 耗时 {duration_label}"
            ),
            "metrics_summary_line": (
                f"- 指标：TPS {total_tps_label} · P95 {p95_label} · "
                f"错误率 {error_rate_label}"
            ),
        }

    @classmethod
    def _build_run_group_peak_qps_context_for_webhook(
        cls,
        db: Session,
        runs: list[Run],
        *,
        current_total_tps: Any = None,
    ) -> dict[str, Any]:
        batch_points_by_ts: dict[str, float] = {}
        endpoint_names: set[str] = set()
        try:
            from app.services.run_service import RunService

            run_service = RunService(db)
            for run in runs:
                run_id = getattr(run, "run_id", None)
                if run_id is None:
                    continue
                trends = run_service.get_endpoint_trends(
                    int(run_id), metric="throughput", step_seconds=5
                )
                endpoint_names.update(
                    cls._extract_non_overall_trend_endpoint_names(trends)
                )
                run_points = cls._sum_endpoint_throughput_points_by_ts(trends)
                for ts, value in run_points.items():
                    batch_points_by_ts[ts] = round(
                        batch_points_by_ts.get(ts, 0.0) + value, 4
                    )
        except Exception as exc:
            logger.warning("plan run webhook peak qps trend lookup failed: %s", exc)

        candidates: list[float] = []
        if batch_points_by_ts:
            candidates.append(max(batch_points_by_ts.values()))
        try:
            total_tps = float(current_total_tps)
        except (TypeError, ValueError):
            total_tps = None
        if total_tps is not None and total_tps > 0:
            candidates.append(total_tps)
        return {
            "peak_qps": round(max(candidates), 4) if candidates else None,
            "endpoint_count": len(endpoint_names),
        }

    @staticmethod
    def _extract_non_overall_trend_endpoint_names(trends: Any) -> set[str]:
        endpoint_names: set[str] = set()
        for item in getattr(trends, "items", None) or []:
            metric = getattr(item, "metric", None)
            metric_value = getattr(metric, "value", metric)
            if str(metric_value) != "throughput":
                continue
            endpoint_name = str(getattr(item, "endpoint_name", "") or "").strip()
            if endpoint_name and endpoint_name != "overall":
                endpoint_names.add(endpoint_name)
        return endpoint_names

    @staticmethod
    def _should_use_compact_metric_summary(
        *,
        is_mixed_run: bool,
        run_count: Any,
        task_count: Any,
        endpoint_count: Any,
    ) -> bool:
        try:
            normalized_run_count = int(run_count)
        except (TypeError, ValueError):
            normalized_run_count = 0
        try:
            normalized_task_count = int(task_count)
        except (TypeError, ValueError):
            normalized_task_count = 0
        try:
            normalized_endpoint_count = int(endpoint_count)
        except (TypeError, ValueError):
            normalized_endpoint_count = 0
        return (
            bool(is_mixed_run)
            or normalized_run_count > 1
            or normalized_task_count > 1
            or normalized_endpoint_count > 1
        )

    @staticmethod
    def _sum_endpoint_throughput_points_by_ts(trends: Any) -> dict[str, float]:
        points_by_ts: dict[str, float] = {}
        for item in getattr(trends, "items", None) or []:
            metric = getattr(item, "metric", None)
            metric_value = getattr(metric, "value", metric)
            if str(metric_value) != "throughput":
                continue
            for point in getattr(item, "points", None) or []:
                value = getattr(point, "value", None)
                if not isinstance(value, (int, float)):
                    continue
                ts = getattr(point, "ts", None)
                if isinstance(ts, datetime):
                    ts_key = ts.isoformat()
                else:
                    ts_key = str(ts or "").strip()
                if not ts_key:
                    continue
                points_by_ts[ts_key] = round(points_by_ts.get(ts_key, 0.0) + value, 4)
        return points_by_ts

    @classmethod
    def _derive_plan_run_display_status(
        cls, *, status_value: str, status_detail: str, runs: list[Run]
    ) -> str:
        normalized_status = str(status_value or "").strip().lower()
        if normalized_status == "stopped":
            return "stopped"
        if normalized_status != "failed":
            return normalized_status or "-"

        status_detail_tokens = {
            part.strip() for part in str(status_detail or "").split(";") if part.strip()
        }
        if (
            "user_stopped" in status_detail_tokens
            or "terminal_reconciled:stopped_run_terminal" in status_detail_tokens
        ):
            return "stopped"

        if not runs:
            return normalized_status

        run_statuses = [
            cls._enum_value(getattr(run, "run_status", None)) for run in runs
        ]
        if run_statuses and all(status == "stopped" for status in run_statuses):
            if any(
                str(getattr(run, "stop_reason", "") or "").startswith(
                    "stopped_from_plan_run:"
                )
                for run in runs
            ):
                return "stopped"
        return normalized_status

    @staticmethod
    def _format_alert_action_status_label(action_status: Any) -> str:
        normalized = str(action_status or "").strip()
        return {
            "stop_mixed_run_triggered": "已触发混压自动停止",
            "stop_run_triggered": "已触发 Run 自动停止",
            "recorded": "已记录",
            "no_policy_matched": "未匹配策略",
            "auto_stop_disabled_matched": "已匹配但自动止停关闭",
            "observe_only_matched": "仅观测，不执行停止",
            "deduped_recorded": "重复告警已去重记录",
            "stop_mixed_run_skipped_terminal": "混压已终态，跳过停止",
            "stop_run_skipped_terminal": "Run 已终态，跳过停止",
            "stop_mixed_run_skipped_cooldown": "混压停止冷却中，跳过",
            "stop_run_skipped_cooldown": "Run 停止冷却中，跳过",
        }.get(normalized, normalized or "-")

    @classmethod
    def _format_alert_reason(
        cls,
        *,
        alertname: Any,
        summary: Any,
        annotations: dict[str, Any],
        labels: dict[str, Any],
        alert_url: Any,
    ) -> str:
        alertname_text = str(alertname or "").strip()
        threshold = cls._extract_threshold_from_text(
            " ".join(
                str(value or "")
                for value in (
                    alert_url,
                    annotations.get("description"),
                    annotations.get("summary"),
                )
            )
        )
        normalized_name = alertname_text.lower()
        target = (
            labels.get("target_instance")
            or labels.get("instance")
            or labels.get("service")
            or labels.get("component")
            or "目标服务"
        )
        if "cpu" in normalized_name:
            suffix = f" > {threshold}%" if threshold else " 超过阈值"
            return f"触发当前配置的 CPU{suffix} 告警（对象 {target}）"
        if "mem" in normalized_name or "memory" in normalized_name:
            suffix = f" > {threshold}%" if threshold else " 超过阈值"
            return f"触发当前配置的 MEM{suffix} 告警（对象 {target}）"
        description = annotations.get("description")
        if description:
            return str(description)
        if summary:
            return str(summary)
        return alertname_text or "-"

    @staticmethod
    def _extract_threshold_from_text(text: str) -> str | None:
        match = re.search(
            r"(?:%3E|>|大于|超过)(?:\s|\+)*([0-9]+(?:\.[0-9]+)?)(?:\s|\+)*%?",
            text,
        )
        if not match:
            return None
        value = match.group(1)
        return value[:-2] if value.endswith(".0") else value

    @classmethod
    def _load_plan_run_stop_alert_event(
        cls,
        db: Session,
        *,
        plan_run_id: Any,
        launched_run_ids: list[int],
        is_mixed_run: bool,
    ) -> RunAlertEvent | None:
        try:
            normalized_plan_run_id = int(plan_run_id)
        except (TypeError, ValueError):
            normalized_plan_run_id = 0
        filters = []
        if normalized_plan_run_id > 0:
            filters.append(RunAlertEvent.plan_run_id == normalized_plan_run_id)
            if is_mixed_run:
                filters.append(RunAlertEvent.mixed_run_id == normalized_plan_run_id)
        if launched_run_ids:
            filters.append(RunAlertEvent.run_id.in_(launched_run_ids))
        if not filters:
            return None

        stop_actions = (
            ["stop_mixed_run_triggered"] if is_mixed_run else ["stop_run_triggered"]
        )
        return (
            db.query(RunAlertEvent)
            .filter(or_(*filters))
            .filter(RunAlertEvent.action_status.in_(stop_actions))
            .order_by(RunAlertEvent.created_at.desc(), RunAlertEvent.event_id.desc())
            .first()
        )

    @classmethod
    def _format_plan_run_stop_reason_summary(
        cls,
        *,
        plan_run: Any,
        alert_event: RunAlertEvent | None,
        is_mixed_run: bool,
    ) -> str:
        if alert_event is not None:
            labels = alert_event.labels if isinstance(alert_event.labels, dict) else {}
            annotations = (
                alert_event.annotations
                if isinstance(alert_event.annotations, dict)
                else {}
            )
            reason = cls._format_alert_reason(
                alertname=alert_event.alertname,
                summary=annotations.get("summary") or alert_event.alertname,
                annotations=annotations,
                labels=labels,
                alert_url=alert_event.dashboard_url,
            )
            action = cls._format_alert_action_status_label(alert_event.action_status)
            alertname = alert_event.alertname or "-"
            severity = str(alert_event.severity or alert_event.priority or "-").upper()
            return f"{reason}；{alertname} {severity}；动作：{action}"

        status_detail = str(getattr(plan_run, "status_detail", "") or "").strip()
        if "user_stopped" in status_detail:
            return "用户手动停止"
        if "alert_policy:" in status_detail:
            return status_detail
        if "terminal_reconciled:alert_stopped_run_terminal" in status_detail:
            return "告警策略自动停止；未找到触发停止的告警事件"
        if is_mixed_run and "stopped_run_terminal" in status_detail:
            return "混压已停止；未找到触发停止的告警事件"
        return "-"

    @staticmethod
    def _enum_value(value: Any) -> str | None:
        if value is None:
            return None
        return str(getattr(value, "value", value) or "").strip() or None

    @staticmethod
    def _load_plan_for_webhook(db: Session, plan_id: Any) -> Plan | None:
        try:
            normalized_plan_id = int(plan_id)
        except (TypeError, ValueError):
            return None
        if normalized_plan_id <= 0:
            return None
        return db.query(Plan).filter(Plan.plan_id == normalized_plan_id).first()

    @staticmethod
    def _load_run_for_webhook(db: Session, run_id: Any) -> Run | None:
        try:
            normalized_run_id = int(run_id)
        except (TypeError, ValueError):
            return None
        if normalized_run_id <= 0:
            return None
        return db.query(Run).filter(Run.run_id == normalized_run_id).first()

    @staticmethod
    def _load_task_for_webhook(db: Session, task_id: Any) -> Task | None:
        try:
            normalized_task_id = int(task_id)
        except (TypeError, ValueError):
            return None
        if normalized_task_id <= 0:
            return None
        return db.query(Task).filter(Task.id == normalized_task_id).first()

    @classmethod
    def _load_runs_for_webhook(cls, db: Session, run_ids: list[int]) -> list[Run]:
        if not run_ids:
            return []
        rows = db.query(Run).filter(Run.run_id.in_(run_ids)).all()
        run_map = {int(row.run_id): row for row in rows if row.run_id is not None}
        return [run_map[run_id] for run_id in run_ids if run_id in run_map]

    @classmethod
    def _resolve_plan_run_launched_run_ids(cls, plan_run: Any) -> list[int]:
        raw_ids = getattr(plan_run, "launched_run_ids", None)
        if isinstance(raw_ids, str):
            try:
                parsed = json.loads(raw_ids)
            except json.JSONDecodeError:
                parsed = None
            raw_ids = parsed if isinstance(parsed, list) else raw_ids
        ids = cls._normalize_int_list(raw_ids)
        if ids:
            return ids
        status_detail = str(getattr(plan_run, "status_detail", "") or "")
        matched = re.search(r"launched_runs=([0-9,]+)", status_detail)
        if not matched:
            return []
        return cls._normalize_int_list(matched.group(1).split(","))

    @staticmethod
    def _normalize_int_list(raw_values: Any) -> list[int]:
        if not isinstance(raw_values, (list, tuple, set)):
            return []
        normalized: list[int] = []
        for raw_value in raw_values:
            try:
                value = int(raw_value)
            except (TypeError, ValueError):
                continue
            if value > 0 and value not in normalized:
                normalized.append(value)
        return normalized

    @classmethod
    def _build_plan_task_summary_for_webhook(
        cls, db: Session, plan: Plan | None, runs: list[Run]
    ) -> dict[str, Any]:
        env_values = {
            str(getattr(run, "env", "") or "").strip()
            for run in runs
            if str(getattr(run, "env", "") or "").strip()
        }
        engine_values = {
            cls._enum_value(getattr(run, "engine_type", None)) or ""
            for run in runs
            if cls._enum_value(getattr(run, "engine_type", None))
        }
        task_ids = {
            int(run.task_id)
            for run in runs
            if getattr(run, "task_id", None) is not None
        }
        if plan and isinstance(plan.stages, list):
            for stage in plan.stages:
                if not isinstance(stage, dict):
                    continue
                for item in stage.get("items") or []:
                    if not isinstance(item, dict):
                        continue
                    try:
                        task_id = int(item.get("task_id"))
                    except (TypeError, ValueError):
                        continue
                    if task_id > 0:
                        task_ids.add(task_id)
        task_rows = (
            db.query(Task).filter(Task.id.in_(sorted(task_ids))).all()
            if task_ids
            else []
        )
        for task in task_rows:
            env = str(getattr(task, "env", "") or "").strip()
            if env:
                env_values.add(env)
            engine = cls._enum_value(getattr(task, "engine_type", None))
            if engine:
                engine_values.add(engine)
        business_values = {
            str((getattr(task, "properties", None) or {}).get("business_line") or "").strip()
            for task in task_rows
            if isinstance(getattr(task, "properties", None), dict)
            and str((task.properties or {}).get("business_line") or "").strip()
        }
        if plan and isinstance(getattr(plan, "business_lines", None), list):
            business_values.update(
                str(value).strip() for value in plan.business_lines if str(value).strip()
            )
        return {
            "task_count": len(task_ids),
            "env_list_label": " / ".join(sorted(env_values)) or "-",
            "engine_types_label": " / ".join(sorted(engine_values)) or "-",
            "business_lines_label": " / ".join(sorted(business_values)) or "-",
        }

    @classmethod
    def _build_run_metrics_for_webhook(cls, runs: list[Run]) -> dict[str, Any]:
        run_count = len(runs)
        statuses = [cls._enum_value(getattr(run, "run_status", None)) for run in runs]
        success_count = sum(1 for status in statuses if status == "succeeded")
        failed_count = sum(1 for status in statuses if status == "failed")
        stopped_count = sum(1 for status in statuses if status == "stopped")
        total_requests = sum(
            int(run.total_requests)
            for run in runs
            if isinstance(getattr(run, "total_requests", None), int)
        )
        total_tps_values = [
            float(run.rps)
            for run in runs
            if isinstance(getattr(run, "rps", None), (int, float))
        ]
        p95_values = [
            float(run.p95_rt_ms)
            for run in runs
            if isinstance(getattr(run, "p95_rt_ms", None), (int, float))
        ]
        p99_values = [
            float(run.p99_rt_ms)
            for run in runs
            if isinstance(getattr(run, "p99_rt_ms", None), (int, float))
        ]
        weighted_error = cls._weighted_error_rate(runs)
        total_tps = sum(total_tps_values) if total_tps_values else None
        p95 = max(p95_values) if p95_values else None
        p99 = max(p99_values) if p99_values else None
        return {
            "run_count": run_count,
            "success_run_count": success_count,
            "failed_run_count": failed_count,
            "stopped_run_count": stopped_count,
            "total_requests": total_requests if total_requests > 0 else "-",
            "total_requests_label": (
                f"{total_requests:,}" if total_requests > 0 else "暂无"
            ),
            "total_tps": round(total_tps, 4) if total_tps is not None else "-",
            "total_tps_label": cls._format_number_label(total_tps, suffix=" req/s"),
            "p95": round(p95, 4) if p95 is not None else "-",
            "p95_label": cls._format_number_label(p95, suffix=" ms"),
            "p99": round(p99, 4) if p99 is not None else "-",
            "p99_label": cls._format_number_label(p99, suffix=" ms"),
            "error_rate": (
                round(weighted_error, 6) if weighted_error is not None else "-"
            ),
            "error_rate_label": cls._format_percent_label(weighted_error),
        }

    @staticmethod
    def _weighted_error_rate(runs: list[Run]) -> float | None:
        weighted_total = 0
        weighted_failed = 0.0
        fallback_rates: list[float] = []
        for run in runs:
            error_rate = getattr(run, "error_rate", None)
            if error_rate is None and getattr(run, "success_rate", None) is not None:
                error_rate = max(0.0, min(1.0, 1 - float(run.success_rate)))
            if not isinstance(error_rate, (int, float)):
                continue
            total_requests = getattr(run, "total_requests", None)
            if isinstance(total_requests, int) and total_requests > 0:
                weighted_total += total_requests
                weighted_failed += total_requests * float(error_rate)
            else:
                fallback_rates.append(float(error_rate))
        if weighted_total > 0:
            return weighted_failed / weighted_total
        if fallback_rates:
            return sum(fallback_rates) / len(fallback_rates)
        return None

    @staticmethod
    def _format_number_label(value: float | None, *, suffix: str = "") -> str:
        if value is None:
            return "暂无"
        if abs(value) >= 100:
            formatted = f"{value:.0f}"
        elif abs(value) >= 10:
            formatted = f"{value:.1f}"
        else:
            formatted = f"{value:.2f}"
        return f"{formatted}{suffix}"

    @staticmethod
    def _format_peak_qps_label(value: float | None) -> str:
        if value is None:
            return "暂无"
        return f"{value:.1f} req/s"

    @staticmethod
    def _format_percent_label(value: float | None) -> str:
        if value is None:
            return "暂无"
        return f"{value * 100:.2f}%"

    @staticmethod
    def _format_duration_label(duration_seconds: Any) -> str:
        try:
            seconds = int(duration_seconds)
        except (TypeError, ValueError):
            return "暂无"
        if seconds < 0:
            return "暂无"
        minutes, remain = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        parts: list[str] = []
        if hours:
            parts.append(f"{hours}小时")
        if minutes:
            parts.append(f"{minutes}分")
        if remain or not parts:
            parts.append(f"{remain}秒")
        return "".join(parts)

    @staticmethod
    def _format_plan_exec_type_label(exec_type: str | None) -> str:
        return {
            "manual": "手动任务",
            "fixed": "定时任务",
            "cron": "周期任务",
        }.get(exec_type or "", "执行任务")

    @staticmethod
    def _format_status_result_label(status_value: str) -> str:
        return {
            "succeeded": "已完成",
            "failed": "失败",
            "stopped": "已停止",
        }.get(status_value, "状态更新")

    @staticmethod
    def _format_status_detail_summary(
        *,
        status_value: str,
        status_detail: str,
        run_result_summary: str,
        stop_reason_summary: str = "-",
    ) -> str:
        normalized_detail = status_detail.strip()
        if status_value == "succeeded":
            return f"{run_result_summary}，可查看明细确认报告与指标"
        if (
            status_value == "stopped"
            and stop_reason_summary
            and stop_reason_summary != "-"
        ):
            return f"{run_result_summary}；停止原因：{stop_reason_summary}"
        if (
            stop_reason_summary
            and stop_reason_summary != "-"
            and "停止" in run_result_summary
        ):
            return f"{run_result_summary}；停止原因：{stop_reason_summary}"
        if normalized_detail:
            return f"{run_result_summary}；线索：{normalized_detail}"
        return f"{run_result_summary}；暂无额外失败线索"

    @staticmethod
    def _format_task_run_summary(
        *, run_count: Any, task_count: Any, env_label: str, engine_label: str
    ) -> str:
        parts: list[str] = []
        try:
            normalized_run_count = int(run_count)
        except (TypeError, ValueError):
            normalized_run_count = 0
        try:
            normalized_task_count = int(task_count)
        except (TypeError, ValueError):
            normalized_task_count = 0
        if normalized_run_count > 0:
            parts.append(f"{normalized_run_count} 个 Run")
        if normalized_task_count > 0:
            parts.append(f"{normalized_task_count} 个任务")
        if env_label and env_label != "-":
            parts.append(env_label)
        if engine_label and engine_label != "-":
            parts.append(engine_label)
        return " / ".join(parts) or "暂无任务/Run 明细"

    @staticmethod
    def _format_round_label(round_value: Any, round_total: Any) -> str:
        try:
            current_round = int(round_value)
        except (TypeError, ValueError):
            current_round = None
        try:
            total_round = int(round_total)
        except (TypeError, ValueError):
            total_round = None
        if current_round and total_round:
            return f"轮次 {current_round} / 共 {total_round}"
        if total_round and total_round > 1:
            return f"共 {total_round} 个轮次"
        return "单轮"

    @classmethod
    def _format_run_result_summary(
        cls, *, status_value: str, launched_run_ids: list[int], runs: list[Run]
    ) -> str:
        run_total = len(runs) or len(launched_run_ids)
        if run_total <= 0:
            return "暂无 Run 明细"
        statuses = [cls._enum_value(getattr(run, "run_status", None)) for run in runs]
        success_count = sum(1 for status in statuses if status == "succeeded")
        failed_count = sum(1 for status in statuses if status == "failed")
        stopped_count = sum(1 for status in statuses if status == "stopped")
        if not runs and status_value == "succeeded":
            success_count = run_total
        parts = [f"{success_count}/{run_total} 成功"]
        if failed_count:
            parts.append(f"{failed_count} 失败")
        if stopped_count:
            parts.append(f"{stopped_count} 停止")
        return "，".join(parts)

    @staticmethod
    def _notification_env_label() -> str:
        configured = (settings.NOTIFICATION_ENV_LABEL or "").strip()
        if configured:
            return configured
        runtime_project = (settings.RUNTIME_COMPOSE_PROJECT or "").strip()
        if runtime_project and runtime_project != "unknown":
            return runtime_project
        return "local"

    @classmethod
    def _title_with_env(cls, title: str, variables: dict[str, str]) -> str:
        title = cls._public_alpha_webhook_title(title)
        env_label = variables.get("notification_env") or cls._notification_env_label()
        if not env_label:
            return title
        prefix = f"[{env_label}]"
        if title.startswith(prefix):
            return title
        return f"{prefix} {title}"

    @staticmethod
    def _public_alpha_webhook_title(title: str) -> str:
        if not settings.PTP_PUBLIC_ALPHA_MODE:
            return title
        return re.sub(
            r"(^|\]\s*)PTP(?=\s|批次|阈值)",
            r"\1OpenLoadHub",
            title,
            count=1,
        )

    @staticmethod
    def _post_webhook_payload(
        *, url: str, payload: dict[str, Any], timeout_seconds: float
    ) -> httpx.Response:
        with httpx.Client(timeout=timeout_seconds) as client:
            return client.post(url, json=payload)

    @classmethod
    def _prepare_webhook_payload(
        cls,
        *,
        channel: WebhookChannel,
        payload: dict[str, Any],
        signature_type: str,
        signing_secret: Optional[str],
    ) -> dict[str, Any]:
        channel_value = channel.value if hasattr(channel, "value") else str(channel)
        signature_value = str(signature_type or WebhookSignatureType.NONE.value)
        if (
            channel_value != WebhookChannel.FEISHU.value
            or signature_value != WebhookSignatureType.FEISHU_V1.value
            or not signing_secret
        ):
            return payload
        signed_payload = dict(payload)
        timestamp = str(int(time.time()))
        signed_payload["timestamp"] = timestamp
        signed_payload["sign"] = cls._build_feishu_v1_sign(
            timestamp=timestamp,
            signing_secret=signing_secret,
        )
        return signed_payload

    @staticmethod
    def _build_feishu_v1_sign(*, timestamp: str, signing_secret: str) -> str:
        string_to_sign = f"{timestamp}\n{signing_secret}"
        digest = hmac.new(
            string_to_sign.encode("utf-8"),
            b"",
            digestmod=hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    @staticmethod
    def _normalize_signing_secret(
        signature_type: str, signing_secret: Optional[str]
    ) -> Optional[str]:
        if str(signature_type or WebhookSignatureType.NONE.value) == (
            WebhookSignatureType.NONE.value
        ):
            return None
        normalized = (signing_secret or "").strip()
        return normalized or None

    @staticmethod
    def _webhook_provider_error(
        channel: WebhookChannel | str, response: httpx.Response
    ) -> str | None:
        if not (200 <= response.status_code < 300):
            return None

        try:
            payload = json.loads(response.text or "{}")
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None

        channel_value = channel.value if hasattr(channel, "value") else str(channel)
        if channel_value == WebhookChannel.FEISHU.value and payload.get("code") not in (
            None,
            0,
        ):
            code = payload.get("code")
            message = payload.get("msg") or "-"
            if code == 19024:
                return (
                    "feishu_code_19024: Key Words Not Found; "
                    "Feishu bot keyword security rejected the message. "
                    "Include the configured keyword in the webhook title/template "
                    "or disable keyword verification on the bot."
                )
            return f"feishu_code_{code}: {message}"
        if channel_value in {
            WebhookChannel.WECOM.value,
            WebhookChannel.DINGTALK.value,
        } and payload.get("errcode") not in (None, 0):
            return (
                f"{channel_value}_errcode_{payload.get('errcode')}: "
                f"{payload.get('errmsg') or '-'}"
            )
        return None

    @staticmethod
    def _mask_webhook_url(raw_url: str) -> str:
        parsed = urlparse(raw_url)
        if not parsed.scheme or not parsed.netloc:
            return "***"
        path = parsed.path
        if parsed.netloc in {"open.feishu.cn", "open.larksuite.com"} and path:
            path_parts = path.rstrip("/").split("/")
            if path_parts and path_parts[-1]:
                path_parts[-1] = "***"
                path = "/".join(path_parts)
        return urlunparse((parsed.scheme, parsed.netloc, path, "", "***", ""))

    @classmethod
    def _redact_sensitive_webhook_text(
        cls,
        text: str,
        *,
        webhook_url: str,
        signing_secret: Optional[str],
        outbound_payload: Optional[dict[str, Any]],
    ) -> str:
        redacted = str(text or "")
        sensitive_values = {webhook_url}
        masked_url = cls._mask_webhook_url(webhook_url)
        parsed_url = urlparse(webhook_url)
        if parsed_url.query:
            sensitive_values.add(parsed_url.query)
            for _, value in parse_qsl(parsed_url.query, keep_blank_values=False):
                if value:
                    sensitive_values.add(value)
        path_token = parsed_url.path.rstrip("/").rsplit("/", maxsplit=1)[-1]
        if parsed_url.netloc in {"open.feishu.cn", "open.larksuite.com"} and path_token:
            sensitive_values.add(path_token)
        if signing_secret:
            sensitive_values.add(signing_secret)
        if outbound_payload:
            sign = outbound_payload.get("sign")
            timestamp = outbound_payload.get("timestamp")
            if isinstance(sign, str) and sign:
                sensitive_values.add(sign)
            if isinstance(timestamp, str) and timestamp:
                redacted = re.sub(
                    rf'("timestamp"\s*:\s*"){re.escape(timestamp)}(")',
                    r"\1***\2",
                    redacted,
                )
        for value in sorted(sensitive_values, key=len, reverse=True):
            if value:
                redacted = redacted.replace(value, "***")
        if masked_url != webhook_url:
            redacted = redacted.replace(webhook_url, masked_url)
        return redacted

    @staticmethod
    def _truncate_response_body(body: str, limit: int = 1000) -> str:
        if len(body) <= limit:
            return body
        return f"{body[:limit]}..."

    @staticmethod
    def _stringify_webhook_value(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, float):
            return f"{value:.4g}"
        if isinstance(value, (list, tuple, set)):
            return "、".join(
                NotificationService._stringify_webhook_value(item) for item in value
            )
        if isinstance(value, dict):
            return ", ".join(
                f"{key}={NotificationService._stringify_webhook_value(item)}"
                for key, item in value.items()
            )
        return str(value)

    @staticmethod
    def _extract_template_variables(template: str) -> set[str]:
        variables: set[str] = set()
        for _, field_name, _, _ in Formatter().parse(template):
            if not field_name:
                continue
            variables.add(re.split(r"[.[]", field_name, maxsplit=1)[0])
        return variables

    @classmethod
    def _render_webhook_template(cls, template: str, variables: dict[str, str]) -> str:
        safe_variables = _WebhookTemplateVariables(variables)
        return template.format_map(safe_variables)

    @staticmethod
    def _build_webhook_payload(
        *, channel: WebhookChannel, title: str, rendered_text: str
    ) -> dict[str, Any]:
        if channel == WebhookChannel.WECOM:
            return {
                "msgtype": "markdown",
                "markdown": {"content": rendered_text},
            }
        if channel == WebhookChannel.DINGTALK:
            return {
                "msgtype": "markdown",
                "markdown": {"title": title, "text": rendered_text},
            }
        return {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": title},
                    "template": "blue",
                },
                "elements": [
                    {"tag": "markdown", "content": rendered_text},
                ],
            },
        }

    @staticmethod
    async def notify_task_status(
        task_id: int, status: str, user_id: Optional[int] = None
    ):
        """通知任务状态变更"""
        message = {
            "type": NotificationType.TASK_STATUS.value,
            "task_id": task_id,
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        if user_id:
            await manager.send_personal_message(message, user_id)
        else:
            # 广播到任务相关房间
            await manager.broadcast(message, room=f"task_{task_id}")

    @staticmethod
    async def notify_task_progress(
        task_id: int, progress: int, message_text: str, user_id: Optional[int] = None
    ):
        """通知任务执行进度"""
        notification = {
            "type": NotificationType.TASK_PROGRESS.value,
            "task_id": task_id,
            "progress": progress,
            "message": message_text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        if user_id:
            await manager.send_personal_message(notification, user_id)
        else:
            await manager.broadcast(notification, room=f"task_{task_id}")

    @staticmethod
    async def notify_task_completed(
        task_id: int, result: Dict[str, Any], user_id: Optional[int] = None
    ):
        """通知任务完成"""
        notification = {
            "type": NotificationType.TASK_COMPLETED.value,
            "task_id": task_id,
            "result": result,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        if user_id:
            await manager.send_personal_message(notification, user_id)
        else:
            await manager.broadcast(notification, room=f"task_{task_id}")

    @staticmethod
    async def notify_approval_required(
        approval_id: int, task_id: int, submitter_id: int, approver_id: int
    ):
        """通知需要审批"""
        notification = {
            "type": NotificationType.APPROVAL_REQUIRED.value,
            "approval_id": approval_id,
            "task_id": task_id,
            "submitter_id": submitter_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # 通知审批人
        await manager.send_personal_message(notification, approver_id)

        # 同时通知提交人
        await manager.send_personal_message(notification, submitter_id)

    @staticmethod
    async def notify_approval_result(
        approval_id: int, task_id: int, status: str, approver_id: int, submitter_id: int
    ):
        """通知审批结果"""
        notification = {
            "type": NotificationType.APPROVAL_RESULT.value,
            "approval_id": approval_id,
            "task_id": task_id,
            "status": status,
            "approver_id": approver_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # 通知提交人
        await manager.send_personal_message(notification, submitter_id)

        # 通知审批人自己
        await manager.send_personal_message(notification, approver_id)

    @staticmethod
    async def notify_report_ready(report_id: int, task_id: int, user_id: int):
        """通知报告就绪"""
        notification = {
            "type": NotificationType.REPORT_READY.value,
            "report_id": report_id,
            "task_id": task_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        await manager.send_personal_message(notification, user_id)

    @staticmethod
    async def notify_system_alert(
        level: str, message: str, user_id: Optional[int] = None
    ):
        """通知系统告警"""
        notification = {
            "type": NotificationType.SYSTEM_ALERT.value,
            "level": level,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        if user_id:
            await manager.send_personal_message(notification, user_id)
        else:
            await manager.broadcast(notification)

    @staticmethod
    async def broadcast_message(message: Dict[str, Any], room: Optional[str] = None):
        """广播自定义消息"""
        await manager.broadcast(message, room=room)

    @staticmethod
    async def send_personal_message(message: Dict[str, Any], user_id: int):
        """发送个人消息"""
        await manager.send_personal_message(message, user_id)


class _WebhookTemplateVariables(dict):
    def __missing__(self, key: str) -> str:
        return "-"
