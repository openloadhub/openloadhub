#!/usr/bin/env python3
"""Dry-run or apply the public OpenLoadHub GitHub label set."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LABEL_FILES = (
    ROOT / ".github" / "labels.yml",
    ROOT / "docs" / "public" / "labels.yml",
)
DEFAULT_SUMMARY_JSON = ROOT / ".tmp" / "logs" / "openloadhub-label-sync-summary.json"
DEFAULT_SUMMARY_MD = ROOT / ".tmp" / "logs" / "openloadhub-label-sync-summary.md"
HEX_COLOR_RE = re.compile(r"^[0-9A-Fa-f]{6}$")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="openloadhub/openloadhub")
    parser.add_argument("--labels-file", type=Path)
    parser.add_argument("--summary-json", type=Path, default=DEFAULT_SUMMARY_JSON)
    parser.add_argument("--summary-md", type=Path, default=DEFAULT_SUMMARY_MD)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply labels with gh label create --force. Default is dry-run.",
    )
    return parser.parse_args(argv)


def resolve_labels_file(raw_path: Path | None) -> Path:
    if raw_path:
        return raw_path.resolve()
    for path in DEFAULT_LABEL_FILES:
        if path.exists():
            return path
    raise SystemExit("No label file found. Expected .github/labels.yml or docs/public/labels.yml.")


def _strip_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def load_labels(path: Path) -> list[dict[str, str]]:
    labels: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.startswith("- "):
            if current:
                labels.append(current)
            current = {}
            line = line[2:].strip()
            if line:
                key, _, value = line.partition(":")
                if not value:
                    raise ValueError(f"{path}:{line_no}: invalid label item")
                current[key.strip()] = _strip_value(value)
            continue
        if current is None:
            raise ValueError(f"{path}:{line_no}: expected a label item")
        key, _, value = line.strip().partition(":")
        if not value:
            raise ValueError(f"{path}:{line_no}: invalid label field")
        current[key.strip()] = _strip_value(value)
    if current:
        labels.append(current)
    return labels


def validate_labels(labels: list[dict[str, str]]) -> list[str]:
    blockers: list[str] = []
    names: set[str] = set()
    for index, label in enumerate(labels, start=1):
        name = label.get("name", "").strip()
        color = label.get("color", "").strip()
        description = label.get("description", "").strip()
        if not name:
            blockers.append(f"label_{index}_missing_name")
        elif name in names:
            blockers.append(f"duplicate_label:{name}")
        else:
            names.add(name)
        if not HEX_COLOR_RE.match(color):
            blockers.append(f"label_{name or index}_invalid_color")
        if not description:
            blockers.append(f"label_{name or index}_missing_description")
    return blockers


def build_command(repo: str, label: dict[str, str]) -> list[str]:
    return [
        "gh",
        "label",
        "create",
        label["name"],
        "--repo",
        repo,
        "--color",
        label["color"],
        "--description",
        label["description"],
        "--force",
    ]


def render_markdown(payload: dict[str, object]) -> str:
    lines = [
        "# OpenLoadHub Label Sync Summary",
        "",
        f"- status: `{payload['status']}`",
        f"- mode: `{payload['mode']}`",
        f"- repo: `{payload['repo']}`",
        f"- labels_file: `{payload['labels_file']}`",
        f"- label_count: `{payload['label_count']}`",
        f"- blockers: `{', '.join(payload['blockers']) or 'none'}`",
        "",
        "## Commands",
        "",
    ]
    commands = payload["commands"]
    assert isinstance(commands, list)
    for command in commands:
        assert isinstance(command, list)
        lines.append("- `" + " ".join(command) + "`")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    labels_file = resolve_labels_file(args.labels_file)
    labels = load_labels(labels_file)
    blockers = validate_labels(labels)
    commands = [build_command(args.repo, label) for label in labels]
    results: list[dict[str, object]] = []
    if args.apply and not blockers:
        for command in commands:
            proc = subprocess.run(command, text=True, capture_output=True, check=False)
            results.append(
                {
                    "command": command,
                    "exit_code": proc.returncode,
                    "stdout": proc.stdout.strip(),
                    "stderr": proc.stderr.strip(),
                }
            )
            if proc.returncode != 0:
                blockers.append("gh_label_apply_failed")
                break
    payload = {
        "status": "passed" if not blockers else "blocked",
        "mode": "apply" if args.apply else "dry-run",
        "generated_at_local": time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime()),
        "repo": args.repo,
        "labels_file": str(labels_file),
        "label_count": len(labels),
        "labels": labels,
        "commands": commands,
        "apply_results": results,
        "blockers": blockers,
    }
    args.summary_json.parent.mkdir(parents=True, exist_ok=True)
    args.summary_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.summary_md.parent.mkdir(parents=True, exist_ok=True)
    args.summary_md.write_text(render_markdown(payload), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not blockers else 1


if __name__ == "__main__":
    raise SystemExit(main())
