"""Publish selected public excerpts and platform identifiers, never runtime caches."""
from __future__ import annotations
import argparse
import json
import re
from pathlib import Path
from build_public_demo import build_demo

ROOT = Path(__file__).resolve().parent
SAMPLE_FIELDS = ("title", "label", "score", "comments", "clicks", "source", "platform", "platformName", "publishTime", "postId", "authorKey")


def build_public_snapshot(snapshot: dict, cache: dict | None = None) -> dict:
    public = build_demo(snapshot)
    lookup = {}
    for source in (cache or {}).values():
        for post in source.get("posts", []):
            key = (post.get("platform"), post.get("title"), post.get("publishTime"))
            lookup.setdefault(key, []).append(post)

    def samples(rows):
        result, seen = [], set()
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            content_key = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", str(row.get("title") or "")).lower()
            if not content_key or content_key in seen:
                continue
            seen.add(content_key)
            item = {k: row[k] for k in SAMPLE_FIELDS if k in row}
            matches = lookup.get((row.get("platform"), row.get("title"), row.get("publishTime")), [])
            if len(matches) == 1:
                post = matches[0]
                item["postId"] = str(post.get("id") or "")
                item["authorKey"] = str(post.get("authorKey") or "")
            result.append(item)
        return result

    public.setdefault("community", {})["samplePosts"] = samples(snapshot.get("community", {}).get("samplePosts", []))
    source_sectors = {s.get("key"): s for s in snapshot.get("sectorIndices", [])}
    for sector in public.get("sectorIndices", []):
        sector["samplePosts"] = samples(source_sectors.get(sector.get("key"), {}).get("samplePosts", []))
    public.update({"publicPostsEnabled": True, "demoData": False})
    public.pop("demoNote", None)
    return public


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "data.public.js")
    args = parser.parse_args()
    snapshot = json.loads((ROOT / "data/latest.json").read_text(encoding="utf-8"))
    cache = json.loads((ROOT / "data/forum_cache.json").read_text(encoding="utf-8"))
    payload = build_public_snapshot(snapshot, cache)
    args.output.write_text("window.INDEX_DATA = " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ";\n", encoding="utf-8")
    print("Public snapshot generated")


if __name__ == "__main__":
    main()
