"""读取工具测试：检查返回原文、换行保留，以及错误路径的处理。"""

import sys
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

# 使用当前项目的源码，不依赖系统中是否安装过 zipclaw。
project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root / "src"))

from zipclaw.tools.filesystem.read_file import ReadFileTool


class TestReadFileTool(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        """每个测试都拿到全新的临时工作区，互不影响。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_root = Path(self.temporary_directory.name).resolve()
        self.workspace = self.temporary_root / "workspace"
        self.workspace.mkdir()
        self.tool = ReadFileTool(workspace=self.workspace)

    async def test_read_chinese_and_preserve_line_endings(self):
        """读取应返回原文，不能把 Windows 换行自动改成另一种换行。"""
        content = "第一行\r\n第二行\n最后一行"
        target = self.workspace / "中文文件.txt"
        target.write_bytes(content.encode("utf-8"))

        result = await self.tool.run({"path": "中文文件.txt"})

        self.assertEqual(result, content)

    async def test_read_empty_file(self):
        """空文件应该返回空字符串，而不是报文件不存在。"""
        (self.workspace / "empty.txt").write_bytes(b"")
        result = await self.tool.run({"path": "empty.txt"})
        self.assertEqual(result, "")

    async def test_missing_file(self):
        result = await self.tool.run({"path": "missing.txt"})
        self.assertIn("文件不存在", result)

    async def test_directory_is_not_file(self):
        result = await self.tool.run({"path": "."})
        self.assertIn("不是普通文件", result)

    async def test_replace_invalid_utf8_when_reading(self):
        """当前读取策略允许替代乱码；编辑工具则会拒绝这类文件。"""
        (self.workspace / "invalid.txt").write_bytes(b"hello\xff")
        result = await self.tool.run({"path": "invalid.txt"})
        self.assertEqual(result, "hello\ufffd")

    async def test_refuse_outside_paths(self):
        """相对路径和绝对路径都不能绕过工作区边界。"""
        outside = self.temporary_root / "outside.txt"
        outside.write_text("工作区外的内容", encoding="utf-8")
        for path in ["../outside.txt", str(outside)]:
            # subTest 会显示是哪组参数失败，便于定位问题。
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "工作区"):
                    await self.tool.run({"path": path})

    async def test_missing_path_is_validation_error(self):
        """从 run 入口调用，才能同时检查 Pydantic 参数验证。"""
        with self.assertRaises(ValidationError):
            await self.tool.run({})


if __name__ == "__main__":
    unittest.main(verbosity=2)
