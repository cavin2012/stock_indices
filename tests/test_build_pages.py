from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pages_builder", ROOT / "scripts" / "build_pages.py")
pages = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pages)


class PublicPagesTests(unittest.TestCase):
    def test_public_artifact_has_only_safe_assets_and_no_local_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            source.joinpath("dashboard.html").write_text(ROOT.joinpath("dashboard.html").read_text(encoding="utf-8"), encoding="utf-8")
            source.joinpath("dashboard.js").write_text(ROOT.joinpath("dashboard.js").read_text(encoding="utf-8"), encoding="utf-8")
            source.joinpath("styles.css").write_text("body{}", encoding="utf-8")
            private = {"scores": {"composite": 55}, "updatedAt": "2026-11-26T16:00:00+08:00", "community": {"samplePosts": [{"title": "DO_NOT_PUBLISH", "authorKey": "PRIVATE_AUTHOR"}]}, "failures": ["C:/private/error"], "user_id": "PRIVATE_USER"}
            source.joinpath("data.example.js").write_text("window.INDEX_DATA = " + json.dumps(private) + ";", encoding="utf-8")
            source.joinpath("data.js").write_text("LOCAL_PAYLOAD_DO_NOT_COPY", encoding="utf-8")
            output = Path(directory) / "public"
            pages.build_site(output, source)
            self.assertEqual({p.name for p in output.iterdir()}, pages.PUBLIC_FILES)
            all_text = "".join(p.read_text(encoding="utf-8") for p in output.iterdir())
            for forbidden in ("DO_NOT_PUBLISH", "PRIVATE_AUTHOR", "PRIVATE_USER", "C:/private/error", "LOCAL_PAYLOAD_DO_NOT_COPY"):
                self.assertNotIn(forbidden, all_text)
            payload = output.joinpath("data.js").read_text(encoding="utf-8")
            public = json.loads(payload.split("=", 1)[1].strip().removesuffix(";"))
            self.assertTrue(public["publicData"])
            self.assertFalse(public["demoData"])
            self.assertEqual(public["scores"]["composite"], 55)
            self.assertEqual(public["community"]["samplePosts"], [])
            self.assertNotIn('"data.example.js"', output.joinpath("index.html").read_text(encoding="utf-8"))

    def test_existing_unexpected_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            output.joinpath("private.json").write_text("private", encoding="utf-8")
            with self.assertRaises(ValueError):
                pages.build_site(output)
            self.assertTrue(output.joinpath("private.json").exists())


if __name__ == "__main__":
    unittest.main()
