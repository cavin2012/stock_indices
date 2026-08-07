"""Build the privacy-safe aggregate demo committed to the public repository.

The runtime snapshot can contain short forum excerpts.  This script deliberately
removes every post array and identity-like field before writing data.example.js.
It keeps the full aggregate market history, while retaining sector-level history
only for the latest four points so the example stays small enough for normal Git.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "data" / "latest.json"
DEFAULT_OUTPUT = ROOT / "data.example.js"
IDENTITY_KEYS = {
    "author",
    "authorkey",
    "nickname",
    "postid",
    "post_id",
    "uid",
    "user_id",
    "userid",
    "username",
}
POST_ARRAY_KEYS = {
    "latestposts",
    "posts",
    "rawposts",
    "representativeposts",
    "sampleposts",
}


def sanitise(value: Any) -> Any:
    """Return a deep, JSON-compatible copy without user content or identifiers."""
    if isinstance(value, list):
        return [sanitise(item) for item in value]
    if not isinstance(value, dict):
        return value

    result: dict[str, Any] = {}
    for key, item in value.items():
        normalised = str(key).replace("-", "_").lower()
        if normalised in IDENTITY_KEYS:
            continue
        if normalised in POST_ARRAY_KEYS and isinstance(item, list):
            result[key] = []
            continue
        result[key] = sanitise(item)
    return result


def build_demo(snapshot: dict[str, Any]) -> dict[str, Any]:
    demo = sanitise(copy.deepcopy(snapshot))
    history = demo.get("history")
    if isinstance(history, list):
        sector_history_start = max(0, len(history) - 4)
        for index, point in enumerate(history):
            if index < sector_history_start and isinstance(point, dict):
                point.pop("sectors", None)

    demo["failures"] = []
    demo["demoData"] = True
    demo["demoNote"] = (
        "当前展示开源脱敏演示数据：保留聚合分数，不含论坛原帖、帖子编号或用户标识。"
        "双击“一键更新指数.cmd”后会切换为本机最新结果。"
    )
    return demo


def find_forbidden_fields(value: Any, path: str = "$") -> list[str]:
    findings: list[str] = []
    if isinstance(value, list):
        for index, item in enumerate(value):
            findings.extend(find_forbidden_fields(item, f"{path}[{index}]"))
    elif isinstance(value, dict):
        for key, item in value.items():
            normalised = str(key).replace("-", "_").lower()
            if normalised in IDENTITY_KEYS:
                findings.append(f"{path}.{key}")
            if normalised in POST_ARRAY_KEYS and isinstance(item, list) and item:
                findings.append(f"{path}.{key}")
            findings.extend(find_forbidden_fields(item, f"{path}.{key}"))
    return findings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成不含社区原文和身份字段的开源演示数据")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    snapshot = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(snapshot, dict):
        raise ValueError("输入快照必须是 JSON 对象")

    demo = build_demo(snapshot)
    findings = find_forbidden_fields(demo)
    if findings:
        raise ValueError(f"演示数据仍含受限字段：{', '.join(findings[:8])}")

    payload = json.dumps(demo, ensure_ascii=False, separators=(",", ":"))
    args.output.write_text(f"window.INDEX_DATA = {payload};\n", encoding="utf-8")
    print(f"已生成 {args.output}（{args.output.stat().st_size:,} bytes）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
