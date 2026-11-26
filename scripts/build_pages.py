"""Package only privacy-safe static assets for GitHub Pages (no network)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from build_public_demo import build_demo, find_forbidden_fields

PUBLIC_FILES = {"index.html", "styles.css", "dashboard.js", "data.js", ".nojekyll"}


def build_site(output: Path, source: Path = ROOT) -> dict:
    output = output.resolve()
    if output == source.resolve() or output in source.resolve().parents:
        raise ValueError("Output must be a separate directory")
    if output.exists() and any(path.name not in PUBLIC_FILES or not path.is_file() for path in output.iterdir()):
        raise ValueError("Output contains files outside the public allowlist")
    raw = (source / "data.example.js").read_text(encoding="utf-8")
    match = re.fullmatch(r"\s*window\.INDEX_DATA\s*=\s*(.*?)\s*;?\s*", raw, re.DOTALL)
    if not match:
        raise ValueError("Invalid public snapshot")
    snapshot = json.loads(match.group(1).removesuffix(";").rstrip())
    if not isinstance(snapshot, dict):
        raise ValueError("Public snapshot must be an object")
    public = build_demo(snapshot)
    public.update({
        "demoData": False, "publicData": True,
        "publicNote": "公开脱敏聚合快照：不包含论坛原文和用户标识。数据时间见下方最近更新；本页不是实时数据。",
    })
    public.pop("demoNote", None)
    forbidden = find_forbidden_fields(public)
    if forbidden:
        raise ValueError("Forbidden public fields: " + ", ".join(forbidden[:8]))
    page = (source / "dashboard.html").read_text(encoding="utf-8")
    page = page.replace('if (!window.INDEX_DATA?.demoData) return;', 'if (!window.INDEX_DATA?.publicData) return;')
    page = re.sub(r'notice.textContent = window.INDEX_DATA.demoNote[^;]*;', 'notice.textContent = window.INDEX_DATA.publicNote;', page)
    start = page.index('      loadScript(\n        "data.js",')
    end = page.index('    })();', start)
    page = page[:start] + '      loadScript("data.js", loadDashboard);\n' + page[end:]
    page = page.replace('无法读取 ${source}，请重新双击“一键更新指数.cmd”。', '无法读取 ${source}，请刷新页面或稍后重试。')
    page = page.replace('本地生成 · 数据不上传', '公开脱敏聚合快照 · 非实时数据')
    page = page.replace('<strong>使用方式</strong>', '<strong>数据更新说明</strong>')
    page = re.sub(r'<p>在 A 股交易日双击文件夹里的.*?</p>', '<p>采集由维护者执行，脱敏后发布。本页展示最近一次成功发布的快照，请结合更新时间与证据覆盖情况阅读。</p>', page)
    script = (source / "dashboard.js").read_text(encoding="utf-8")
    script = script.replace('社区样本暂不可用', '公开页不展示论坛原文').replace('程序会在下次更新时重试', '本页仅发布脱敏聚合指标')
    output.mkdir(parents=True, exist_ok=True)
    contents = {
        "index.html": page, "dashboard.js": script,
        "styles.css": (source / "styles.css").read_text(encoding="utf-8"),
        "data.js": "window.INDEX_DATA = " + json.dumps(public, ensure_ascii=False, separators=(",", ":")) + ";\n",
        ".nojekyll": "",
    }
    for name, text in contents.items():
        (output / name).write_text(text, encoding="utf-8")
    return {"output": str(output), "files": sorted(contents), "marketDate": public.get("marketDate"), "updatedAt": public.get("updatedAt")}


def main() -> int:
    parser = argparse.ArgumentParser(description="Package a privacy-safe GitHub Pages site")
    parser.add_argument("--output", type=Path, default=ROOT / "_site")
    args = parser.parse_args()
    print(json.dumps(build_site(args.output), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
