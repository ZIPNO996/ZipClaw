"""搜索工具测试：使用小型临时项目验证真实搜索结果。"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root / "src"))

from zipclaw.tools.filesystem.grep import GrepTool


class TestGrepTool(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_root = Path(self.temporary_directory.name).resolve()
        self.workspace = self.temporary_root / "workspace"
        self.workspace.mkdir()
        self.tool = GrepTool(workspace=self.workspace)

    async def test_recursive_search_with_relative_paths_and_line_numbers(self):
        (self.workspace / "a.txt").write_text("普通行\n目标内容\n", encoding="utf-8")
        source = self.workspace / "src"
        source.mkdir()
        (source / "b.txt").write_text("另一个目标\n", encoding="utf-8")

        result = await self.tool.run({"pattern": "目标"})

        self.assertEqual(result.splitlines(), ["a.txt:2: 目标内容", "src/b.txt:1: 另一个目标"])

    async def test_explicit_file_limits_search_scope(self):
        (self.workspace / "a.txt").write_text("needle", encoding="utf-8")
        (self.workspace / "b.txt").write_text("needle", encoding="utf-8")
        result = await self.tool.run({"pattern": "needle", "path": "b.txt"})
        self.assertEqual(result, "b.txt:1: needle")

    async def test_literal_case_sensitive_matching(self):
        """句点和星号是普通字符；大小写不同就不匹配。"""
        (self.workspace / "demo.txt").write_text("a.b*\naxbXYZ\nA.B*", encoding="utf-8")
        result = await self.tool.run({"pattern": "a.b*"})
        self.assertEqual(result, "demo.txt:1: a.b*")

    async def test_no_match(self):
        (self.workspace / "demo.txt").write_text("other", encoding="utf-8")
        result = await self.tool.run({"pattern": "needle"})
        self.assertIn("未找到匹配文本", result)

    async def test_missing_search_path(self):
        result = await self.tool.run({"pattern": "needle", "path": "missing"})
        self.assertIn("搜索路径不存在", result)

    async def test_skip_dependency_and_cache_directories(self):
        """用明确的名单作预期，避免跟着源码中的名单一起出错。"""
        ignored_names = [".git", ".venv", "venv", "__pycache__", "node_modules", "NODE_MODULES"]
        for name in ignored_names:
            directory = self.workspace / name
            # Windows 文件名不区分大小写，最后一个名称可能指向已有目录。
            directory.mkdir(exist_ok=True)
            (directory / "ignored.txt").write_text("needle", encoding="utf-8")
        (self.workspace / "visible.txt").write_text("needle", encoding="utf-8")
        result = await self.tool.run({"pattern": "needle"})
        self.assertEqual(result, "visible.txt:1: needle")

    async def test_skip_binary_invalid_utf8_and_oversized_files(self):
        (self.workspace / "binary.bin").write_bytes(b"needle\x00")
        (self.workspace / "invalid.txt").write_bytes(b"needle\xff")
        # 真正写入一个略大于 1 MiB 的文件，验证大小限制。
        oversized = b"needle" + b"x" * (1024 * 1024)
        (self.workspace / "large.txt").write_bytes(oversized)
        result = await self.tool.run({"pattern": "needle"})
        self.assertIn("未找到匹配文本", result)
        self.assertIn("已跳过 3 个", result)

    async def test_file_at_size_limit_is_searchable(self):
        content = b"needle\n" + b"x" * (1024 * 1024 - 7)
        (self.workspace / "exact.txt").write_bytes(content)
        result = await self.tool.run({"pattern": "needle"})
        self.assertEqual(result, "exact.txt:1: needle")

    async def test_result_limit_reports_only_actual_omissions(self):
        (self.workspace / "demo.txt").write_text("needle one\nneedle two", encoding="utf-8")
        limited = await self.tool.run({"pattern": "needle", "max_results": 1})
        complete = await self.tool.run({"pattern": "needle", "max_results": 2})
        self.assertIn("demo.txt:1: needle one", limited)
        self.assertNotIn("demo.txt:2:", limited)
        self.assertIn("已截断", limited)
        self.assertNotIn("已截断", complete)
        self.assertEqual(len(complete.splitlines()), 2)

    async def test_long_line_matches_before_preview_is_shortened(self):
        """关键词在第 500 字符以后也要匹配，只是展示结果要缩短。"""
        content = "前" * 500 + "needle"
        (self.workspace / "long.txt").write_text(content, encoding="utf-8")
        result = await self.tool.run({"pattern": "needle"})
        expected = "long.txt:1: " + "前" * 500 + "……[该行过长，仅显示开头，请读取原文件]"
        self.assertEqual(result, expected)

    async def test_pattern_cannot_contain_line_breaks(self):
        for pattern in ["first\nsecond", "first\rsecond"]:
            with self.subTest(pattern=pattern):
                result = await self.tool.run({"pattern": pattern})
                self.assertIn("搜索文本不能包含换行", result)

    async def test_invalid_arguments(self):
        invalid_arguments = [
            {}, {"pattern": ""}, {"pattern": "x", "path": ""},
            {"pattern": "x", "max_results": 0},
            {"pattern": "x", "max_results": 201},
        ]
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValidationError):
                    await self.tool.run(arguments)

    async def test_refuse_outside_paths(self):
        for path in ["..", str(self.temporary_root)]:
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "工作区"):
                    await self.tool.run({"pattern": "needle", "path": path})

    async def test_unreadable_file_is_reported_as_skipped(self):
        """模拟权限不足，不去修改电脑上真实文件的访问权限。"""
        (self.workspace / "demo.txt").write_bytes(b"needle")
        # patch 仅在 with 代码块中临时替换方法，退出后自动恢复。
        with patch.object(Path, "open", side_effect=PermissionError("测试用的读取失败")):
            result = await self.tool.run({"pattern": "needle"})
        self.assertIn("已跳过 1 个", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
