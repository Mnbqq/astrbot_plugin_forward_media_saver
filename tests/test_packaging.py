from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - 测试环境可选依赖
    yaml = None

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_forward_media_saver.settings import Settings  # noqa: E402


@unittest.skipIf(yaml is None, "需要 PyYAML 才能读取 metadata.yaml")
class PackagingTests(unittest.TestCase):
    def test_metadata(self):
        meta = yaml.safe_load((ROOT / "metadata.yaml").read_text(encoding="utf-8"))
        self.assertEqual(meta["name"], ROOT.name)
        self.assertTrue(meta["desc"])
        self.assertIn("aiocqhttp", meta["support_platforms"])
        self.assertRegex(meta["astrbot_version"], r"^>=")

    def test_schema_is_valid_json_and_defaults_match(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        defaults = {
            key: item.get("default")
            for key, item in schema.items()
            if "default" in item
        }
        settings = Settings.from_mapping(defaults)
        self.assertEqual(settings, Settings.from_mapping({}))

    def test_schema_types(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        allowed = {"string", "text", "int", "float", "bool", "object", "list", "dict"}
        for key, item in schema.items():
            self.assertIn("description", item, key)
            self.assertIn("type", item, key)
            self.assertIn(item["type"], allowed, key)

    def test_entry_files_exist(self):
        for name in (
            "main.py",
            "metadata.yaml",
            "_conf_schema.json",
            "README.md",
            "LICENSE",
            "logo.png",
            "CHANGELOG.md",
            ".github/workflows/ci.yml",
        ):
            self.assertTrue((ROOT / name).is_file(), name)
        for name in (
            "views/dashboard/index.html",
            "views/dashboard/app.js",
            "views/dashboard/style.css",
            ".astrbot-plugin/i18n/zh-CN.json",
        ):
            self.assertTrue((ROOT / name).is_file(), name)


if __name__ == "__main__":
    unittest.main()
