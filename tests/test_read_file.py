"""读取工具测试：检查原文、分页、字符上限，以及错误路径的处理。"""

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

    async def test_read_pages_without_losing_lines(self):
        """按提示读取第二页，拼起来应该仍然是原文。"""
        original = "第一行\r\n第二行\n第三行"
        (self.workspace / "pages.txt").write_bytes(original.encode("utf-8"))

        first_result = await self.tool.run({"path": "pages.txt", "limit": 2})

        # 将文件正文和工具附加的提示分开检查。
        first_content, separator, notice = first_result.partition("\n\n[读取提示：")
        self.assertTrue(separator)
        self.assertEqual(first_content, "第一行\r\n第二行\n")
        self.assertIn("offset=3", notice)

        second_result = await self.tool.run({"path": "pages.txt", "offset": 3})
        self.assertEqual(second_result, "第三行")
        self.assertEqual(first_content + second_result, original)

    async def test_exact_line_limit_at_end_has_no_notice(self):
        """恰好读到文件末尾时，不应该误报还有内容。"""
        (self.workspace / "exact.txt").write_bytes(b"one\ntwo\n")
        result = await self.tool.run({"path": "exact.txt", "limit": 2})
        self.assertEqual(result, "one\ntwo\n")

    async def test_offset_beyond_end(self):
        """空文件和非空文件都可能出现起始行越界。"""
        for content, offset in [("", 2), ("第一行\n第二行", 3)]:
            with self.subTest(content=content, offset=offset):
                (self.workspace / "offset.txt").write_bytes(content.encode("utf-8"))
                result = await self.tool.run({"path": "offset.txt", "offset": offset})
                self.assertEqual(result, f"起始行 {offset} 超过文件范围。")

    async def test_character_limit_keeps_whole_lines_and_can_resume(self):
        """剩余字符额度不够放下一行时，整行留给下一页。"""
        original = "你好\r\n世界\n最后"
        (self.workspace / "characters.txt").write_bytes(original.encode("utf-8"))

        # 第一行有 4 个字符；第二行加入后总共 7 个，超过上限 6。
        first_result = await self.tool.run({"path": "characters.txt", "max_chars": 6})
        first_content, separator, notice = first_result.partition("\n\n[读取提示：")
        self.assertTrue(separator)
        self.assertEqual(first_content, "你好\r\n")
        self.assertLessEqual(len(first_content), 6)
        self.assertIn("offset=2", notice)

        second_result = await self.tool.run(
            {"path": "characters.txt", "offset": 2, "max_chars": 6}
        )
        self.assertEqual(first_content + second_result, original)

    async def test_exact_character_limit_at_end_has_no_notice(self):
        """中文按字符而非 UTF-8 字节计数，刚好够用时不截断。"""
        original = "你好\r\n世界"
        (self.workspace / "exact_chars.txt").write_bytes(original.encode("utf-8"))
        result = await self.tool.run(
            {"path": "exact_chars.txt", "max_chars": len(original)}
        )
        self.assertEqual(result, original)

    async def test_exact_character_limit_with_more_content(self):
        """字符额度刚好用完，但文件还有下一行时，必须提示续读。"""
        (self.workspace / "more.txt").write_bytes(b"ab\ncd")
        result = await self.tool.run({"path": "more.txt", "max_chars": 3})
        content, separator, notice = result.partition("\n\n[读取提示：")
        self.assertTrue(separator)
        self.assertEqual(content, "ab\n")
        self.assertIn("offset=2", notice)

    async def test_long_first_selected_line_is_explicitly_truncated(self):
        """起始行过长时，只展示部分内容，不能错误提示跳到下一行。"""
        original = "跳过这一行\n甲乙丙丁戊\n后续内容"
        (self.workspace / "long.txt").write_bytes(original.encode("utf-8"))
        result = await self.tool.run(
            {"path": "long.txt", "offset": 2, "max_chars": 3}
        )
        content, separator, notice = result.partition("\n\n[读取提示：")
        self.assertTrue(separator)
        self.assertEqual(content, "甲乙丙")
        self.assertIn("第 2 行", notice)
        self.assertIn("该行剩余内容未展示", notice)
        self.assertIn("从这一行重新读取", notice)
        self.assertNotIn("offset=3", notice)

        # 增大额度后，从同一行重读，应该能够得到完整内容。
        complete_result = await self.tool.run(
            {"path": "long.txt", "offset": 2, "max_chars": 100}
        )
        self.assertEqual(complete_result, "甲乙丙丁戊\n后续内容")

    async def test_default_line_limit(self):
        """不传 limit 时，默认返回 200 行。"""
        (self.workspace / "default_lines.txt").write_bytes(b"x\n" * 201)
        result = await self.tool.run({"path": "default_lines.txt"})
        content, separator, notice = result.partition("\n\n[读取提示：")
        self.assertTrue(separator)
        self.assertEqual(content, "x\n" * 200)
        self.assertIn("offset=201", notice)

    async def test_default_character_limit(self):
        """不传 max_chars 时，超长单行默认最多展示 20000 个字符。"""
        (self.workspace / "default_chars.txt").write_bytes(b"x" * 20001)
        result = await self.tool.run({"path": "default_chars.txt"})
        content, separator, notice = result.partition("\n\n[读取提示：")
        self.assertTrue(separator)
        self.assertEqual(content, "x" * 20000)
        self.assertIn("该行剩余内容未展示", notice)

    async def test_invalid_pagination_arguments(self):
        """经过 run 入口，验证 Pydantic 会拒绝超出范围的参数。"""
        invalid_arguments = [
            {"offset": 0},
            {"offset": -1},
            {"limit": 0},
            {"limit": 2001},
            {"max_chars": 0},
            {"max_chars": 100001},
        ]
        for extra_arguments in invalid_arguments:
            with self.subTest(arguments=extra_arguments):
                arguments = {"path": "example.txt"}
                arguments.update(extra_arguments)
                with self.assertRaises(ValidationError):
                    await self.tool.run(arguments)

    async def test_missing_path_is_validation_error(self):
        """从 run 入口调用，才能同时检查 Pydantic 参数验证。"""
        with self.assertRaises(ValidationError):
            await self.tool.run({})


if __name__ == "__main__":
    unittest.main(verbosity=2)
