"""链接边界测试：路径看似在工作区内，实际目标也必须接受检查。

Windows 上创建符号链接可能需要开发者模式或额外权限。
如果系统明确不允许创建，测试会标记为 skipped，不要求提升权限。
"""

import errno
import sys
import tempfile
import unittest
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root / "src"))

from zipclaw.tools.filesystem.edit_file import EditFileTool
from zipclaw.tools.filesystem.grep import GrepTool
from zipclaw.tools.filesystem.list_dir import ListDirTool
from zipclaw.tools.filesystem.read_file import ReadFileTool
from zipclaw.tools.filesystem.write_file import WriteFileTool


class TestFilesystemLinks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_root = Path(self.temporary_directory.name).resolve()
        self.workspace = self.temporary_root / "workspace"
        self.workspace.mkdir()
        self.outside = self.temporary_root / "outside.txt"
        self.outside.write_bytes(b"outside marker")

    def create_link(self, link, target, is_directory=False):
        """仅在系统不支持或不授权创建链接时跳过，其他错误仍然报错。"""
        try:
            link.symlink_to(target, target_is_directory=is_directory)
        except OSError as exc:
            permission_errors = [errno.EPERM, errno.EACCES, errno.ENOSYS, errno.ENOTSUP]
            if exc.errno in permission_errors or getattr(exc, "winerror", None) == 1314:
                self.skipTest("当前系统不支持或未授权创建符号链接。")
            raise

    async def test_file_tools_refuse_link_to_outside_file(self):
        self.create_link(self.workspace / "link.txt", self.outside)
        cases = [
            (ReadFileTool(self.workspace), {"path": "link.txt"}),
            (WriteFileTool(self.workspace), {"path": "link.txt", "content": "new"}),
            (EditFileTool(self.workspace), {"path": "link.txt", "old_text": "outside", "new_text": "new"}),
            (GrepTool(self.workspace), {"path": "link.txt", "pattern": "marker"}),
        ]
        for tool, arguments in cases:
            with self.subTest(tool=tool.name):
                with self.assertRaisesRegex(ValueError, "工作区"):
                    await tool.run(arguments)
                self.assertEqual(self.outside.read_bytes(), b"outside marker")

    async def test_file_tools_refuse_escape_through_parent_link(self):
        """不仅最终文件可能是链接，路径中间的父目录也可能是链接。"""
        self.create_link(self.workspace / "escape", self.temporary_root, is_directory=True)
        cases = [
            (ReadFileTool(self.workspace), {"path": "escape/outside.txt"}),
            (WriteFileTool(self.workspace), {"path": "escape/new.txt", "content": "new"}),
            (EditFileTool(self.workspace), {"path": "escape/outside.txt", "old_text": "outside", "new_text": "new"}),
            (ListDirTool(self.workspace), {"path": "escape"}),
            (GrepTool(self.workspace), {"path": "escape", "pattern": "marker"}),
        ]
        for tool, arguments in cases:
            with self.subTest(tool=tool.name):
                with self.assertRaisesRegex(ValueError, "工作区"):
                    await tool.run(arguments)
        self.assertFalse((self.temporary_root / "new.txt").exists())
        self.assertEqual(self.outside.read_bytes(), b"outside marker")

    async def test_list_marks_link_without_reading_target(self):
        self.create_link(self.workspace / "link.txt", self.outside)
        result = await ListDirTool(self.workspace).run({})
        self.assertEqual(result, "[链接] link.txt")

    async def test_recursive_grep_skips_file_and_directory_links(self):
        self.create_link(self.workspace / "link.txt", self.outside)
        outside_dir = self.temporary_root / "outside_dir"
        outside_dir.mkdir()
        (outside_dir / "hidden.txt").write_bytes(b"outside marker")
        self.create_link(self.workspace / "linked_dir", outside_dir, is_directory=True)
        result = await GrepTool(self.workspace).run({"pattern": "marker"})
        self.assertIn("未找到匹配文本", result)
        self.assertNotIn("outside marker", result)

    async def test_write_refuses_dangling_link_inside_workspace(self):
        """链接本身存在但目标不存在时，也不能借机创建目标文件。"""
        target = self.workspace / "missing.txt"
        self.create_link(self.workspace / "link.txt", target)
        result = await WriteFileTool(self.workspace).run({"path": "link.txt", "content": "new"})
        self.assertIn("符号链接", result)
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
