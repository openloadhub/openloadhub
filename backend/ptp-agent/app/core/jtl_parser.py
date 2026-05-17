import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


@dataclass
class SampleStat:
    ts: datetime
    elapsed_ms: int
    label: str
    response_code: str
    response_message: str
    success: bool
    bytes: int
    sent_bytes: int
    thread_name: str


def parse_jtl(path: Path) -> Iterable[SampleStat]:
    """
    解析 JMeter CSV JTL 的 canonical fields。
    """
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts_raw = row.get("timeStamp")
                if not ts_raw:
                    continue
                ts = datetime.fromtimestamp(int(ts_raw) / 1000.0, tz=timezone.utc)
                yield SampleStat(
                    ts=ts,
                    elapsed_ms=int(row.get("elapsed") or 0),
                    label=row.get("label") or "",
                    response_code=row.get("responseCode") or "",
                    response_message=row.get("responseMessage") or "",
                    success=str(row.get("success") or "").lower() == "true",
                    bytes=int(row.get("bytes") or 0),
                    sent_bytes=int(row.get("sentBytes") or 0),
                    thread_name=row.get("threadName") or "",
                )
            except Exception:
                continue


def summarize_samples(
    samples: Iterable[SampleStat],
    *,
    duration_ms_override: Optional[float] = None,
) -> Optional[dict]:
    samples = list(samples)
    if not samples:
        return None
    total = len(samples)
    successes = sum(1 for s in samples if s.success)
    fails = total - successes
    error_rate = fails / total if total else 0.0
    elapsed = sorted(s.elapsed_ms for s in samples)
    def pct(p: float) -> float:
        if not elapsed:
            return 0.0
        k = int(len(elapsed) * p / 100)
        k = min(max(k, 0), len(elapsed) - 1)
        return float(elapsed[k])
    duration_ms = duration_ms_override
    if duration_ms is None:
        duration_ms = (samples[-1].ts - samples[0].ts).total_seconds() * 1000
    throughput = total / (duration_ms / 1000.0) if duration_ms > 0 else 0.0
    return {
        "total_requests": total,
        "successful_requests": successes,
        "failed_requests": fails,
        "error_rate": round(error_rate * 100, 2),
        "avg_response_time": sum(elapsed) / total if total else 0.0,
        "p50_response_time": pct(50),
        "p95_response_time": pct(95),
        "p99_response_time": pct(99),
        "throughput": round(throughput, 2),
    }
