from __future__ import annotations

import sys
from pathlib import Path


def _bootstrap_repo_paths() -> None:
    # 兼容两类启动口径：
    # 1. 宿主机从 backend/ptp-admin 目录直接起服务
    # 2. Docker 镜像在 /app 下运行，common 已复制到 /app/common
    current = Path(__file__).resolve()
    candidates: list[Path] = []
    for parent in current.parents:
        if (parent / "common").exists():
            candidates.append(parent)
        backend_dir = parent / "backend"
        if (backend_dir / "common").exists():
            candidates.append(backend_dir)

    seen: set[str] = set()
    for candidate in candidates:
        candidate_str = str(candidate)
        if candidate_str in seen or candidate_str in sys.path:
            continue
        sys.path.insert(0, candidate_str)
        seen.add(candidate_str)


_bootstrap_repo_paths()
