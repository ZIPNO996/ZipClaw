"""目录工具测试：条目类型、相对路径、排序、截断和边界。"""

import sys
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root / "src"))

from zipclaw.tools.filesystem.list_dir import ListDirTool


class TestListDirTool(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_root = Path(self.temporary_directory.name).resolve()
        self.workspace = self.temporary_root / "workspace"
        self.workspace.mkdir()
        self.tool = ListDirTool(workspace=self.workspace)

    async def test_default_path_and_sorted_entries(self):
        """不传 path 时列根目录；排序忽略大小写，而非按创建顺序。"""
        (self.workspace / "z.txt").touch()
        (self.workspace / "Beta").mkdir()
        (self.workspace / "alpha.txt").touch()
        result = await self.tool.run({})
        self.assertEqual(result.splitlines(), [
            "[文件] alpha.txt", "[目录] Beta", "[文件] z.txt",
        ])

    async def test_subdirectory_paths_and_no_recursion(self):
        source = self.workspace / "src"
        source.mkdir()
        (source / "main.py").touch()
        child = source / "sub"
        child.mkdir()
        (child / "hidden.py").touch()

        result = await self.tool.run({"path": "src"})

        self.assertEqual(result.splitlines(), ["[文件] src/main.py", "[目录] src/sub"])
        self.assertNotIn("hidden.py", result)
        self.assertNotIn(str(self.workspace), result)

    async def test_empty_directory(self):
        result = await self.tool.run({})
        self.assertIn("目录为空", result)

    async def test_missing_directory(self):
        result = await self.tool.run({"path": "missing"})
        self.assertIn("目录不存在", result)

    async def test_file_is_not_directory(self):
        (self.workspace / "file.txt").touch()
        result = await self.tool.run({"path": "file.txt"})
        self.assertIn("不是目录", result)

    async def test_limit_truncates_only_when_entries_remain(self):
        (self.workspace / "a.txt").touch()
        (self.workspace / "b.txt").touch()
        truncated = await self.tool.run({"max_entries": 1})
        complete = await self.tool.run({"max_entries": 2})
        self.assertIn("[文件] a.txt", truncated)
        self.assertNotIn("[文件] b.txt", truncated)
        self.assertIn("共 2 项，仅显示前 1 项", truncated)
        self.assertNotIn("已截断", complete)

    async def test_invalid_entry_limits(self):
        for limit in [0, 501]:
            with self.subTest(limit=limit):
                with self.assertRaises(ValidationError):
                    await self.tool.run({"max_entries": limit})

    async def test_refuse_outside_paths(self):
        for path in ["..", str(self.temporary_root)]:
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "工作区"):
                    await self.tool.run({"path": path})


if __name__ == "__main__":
    unittest.main(verbosity=2)
