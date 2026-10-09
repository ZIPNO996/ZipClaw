"""编辑工具测试：不只检查提示，还验证文件字节是否真的正确。"""

import sys
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root / "src"))

from zipclaw.tools.filesystem.edit_file import EditFileTool


class TestEditFileTool(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_root = Path(self.temporary_directory.name).resolve()
        self.workspace = self.temporary_root / "workspace"
        self.workspace.mkdir()
        self.tool = EditFileTool(workspace=self.workspace)
        self.target = self.workspace / "demo.py"

    async def test_replace_unique_text(self):
        """只替换函数中的减号，保留断言和 CRLF 换行。"""
        original = "def add(a, b):\r\n    return a - b\r\n\r\nassert add(2, 3) == 5\r\n"
        expected = "def add(a, b):\r\n    return a + b\r\n\r\nassert add(2, 3) == 5\r\n"
        self.target.write_bytes(original.encode("utf-8"))

        result = await self.tool.run({
            "path": "demo.py",
            "old_text": "return a - b",
            "new_text": "return a + b",
        })

        self.assertIn("已修改", result)
        self.assertEqual(self.target.read_bytes(), expected.encode("utf-8"))

    async def test_multiline_chinese_replacement(self):
        self.target.write_bytes("开头\n旧第一行\n旧第二行\n结尾".encode("utf-8"))
        await self.tool.run({
            "path": "demo.py",
            "old_text": "旧第一行\n旧第二行",
            "new_text": "新内容",
        })
        self.assertEqual(self.target.read_text(encoding="utf-8"), "开头\n新内容\n结尾")

    async def test_empty_new_text_deletes_match(self):
        self.target.write_bytes(b"before REMOVE after")
        result = await self.tool.run({
            "path": "demo.py", "old_text": "REMOVE ", "new_text": "",
        })
        self.assertIn("已修改", result)
        self.assertEqual(self.target.read_bytes(), b"before after")

    async def test_missing_match_preserves_file(self):
        original = b"return a - b\r\n"
        self.target.write_bytes(original)
        result = await self.tool.run({
            "path": "demo.py", "old_text": "return a * b", "new_text": "new",
        })
        self.assertIn("未找到旧文本", result)
        self.assertEqual(self.target.read_bytes(), original)

    async def test_multiple_matches_preserve_file(self):
        original = b"value = 1\nvalue = 1\n"
        self.target.write_bytes(original)
        result = await self.tool.run({
            "path": "demo.py", "old_text": "value = 1", "new_text": "value = 2",
        })
        self.assertIn("旧文本出现 2 次", result)
        self.assertEqual(self.target.read_bytes(), original)

    async def test_identical_text_needs_no_change(self):
        self.target.write_bytes(b"unchanged\r\n")
        result = await self.tool.run({
            "path": "demo.py", "old_text": "unchanged", "new_text": "unchanged",
        })
        self.assertIn("无需修改", result)
        self.assertEqual(self.target.read_bytes(), b"unchanged\r\n")

    async def test_missing_file_is_not_created(self):
        result = await self.tool.run({
            "path": "demo.py", "old_text": "old", "new_text": "new",
        })
        self.assertIn("文件不存在", result)
        self.assertFalse(self.target.exists())

    async def test_directory_is_rejected(self):
        result = await self.tool.run({
            "path": ".", "old_text": "old", "new_text": "new",
        })
        self.assertIn("不是普通文件", result)

    async def test_invalid_utf8_is_not_overwritten(self):
        """解码失败时不能把替代字符写回，损坏原始文件。"""
        original = b"old\xff"
        self.target.write_bytes(original)
        with self.assertRaises(UnicodeDecodeError):
            await self.tool.run({
                "path": "demo.py", "old_text": "old", "new_text": "new",
            })
        self.assertEqual(self.target.read_bytes(), original)

    async def test_invalid_arguments_preserve_file(self):
        self.target.write_bytes(b"old")
        invalid_arguments = [
            {"path": "demo.py", "old_text": "", "new_text": "new"},
            {"path": "", "old_text": "old", "new_text": "new"},
            {"path": "demo.py", "old_text": "old"},
        ]
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValidationError):
                    await self.tool.run(arguments)
                self.assertEqual(self.target.read_bytes(), b"old")

    async def test_refuse_outside_paths(self):
        outside = self.temporary_root / "outside.txt"
        outside.write_bytes(b"old")
        for path in ["../outside.txt", str(outside)]:
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "工作区"):
                    await self.tool.run({
                        "path": path, "old_text": "old", "new_text": "new",
                    })
                self.assertEqual(outside.read_bytes(), b"old")


if __name__ == "__main__":
    unittest.main(verbosity=2)
