r"""验证 write_file 的创建、内容保留、参数校验和文件保护行为。

所有文件都创建在测试自己的临时目录里，不会修改实际工作区文件。
在项目根目录执行：
    .\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError


# 项目采用 src 目录结构，测试解释器不一定已经安装了 zipclaw 包。
# __file__ 是本测试文件的路径；parents[1] 是它上两层的项目根目录。
# 例如：ZipClaw/tests/test_write_file.py -> ZipClaw。
project_root = Path(__file__).resolve().parents[1]
source_directory = project_root / "src"

# 把源码目录放到导入搜索路径的开头，保证测试的是这份项目源码。
# 这只影响当前测试进程，不修改系统环境变量或其他文件。
sys.path.insert(0, str(source_directory))

from zipclaw.tools.filesystem.write_file import WriteFileTool


class TestWriteFileTool(unittest.IsolatedAsyncioTestCase):
    """每个测试使用独立目录，并支持 await 调用异步工具。"""

    # 普通 TestCase 适合同步方法。
    # IsolatedAsyncioTestCase 会为每个异步测试准备事件循环，
    # 所以 test_ 开头的方法可以使用 async def 和 await。

    def setUp(self):
        """每个测试开始前自动执行，准备临时工作区和工具实例。"""

        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="zipclaw-write-file-test-",
        )

        # addCleanup() 注册清理动作：即使测试失败，也会尝试删除临时目录。
        # 这里传入 cleanup 方法本身，没有括号，留给 unittest 之后调用。
        self.addCleanup(self.temporary_directory.cleanup)

        # 临时根目录内再建 workspace，外面留空间测试“工作区之外”。
        self.temporary_root = Path(self.temporary_directory.name).resolve()
        self.workspace = self.temporary_root / "workspace"
        self.workspace.mkdir()

        self.tool = WriteFileTool(workspace=self.workspace)

    async def test_create_new_file(self):
        """新建文件应成功，并保存准确的内容和换行。"""

        # 准备：子目录和文件都还不存在。
        relative_path = "src/demo.py"
        target = self.workspace / relative_path
        content = "print('你好')\n"

        self.assertFalse(target.exists(), "测试开始时文件不应存在。")

        arguments = {
            "path": relative_path,
            "content": content,
        }

        # 执行：使用 run()，同时覆盖参数验证和 execute() 的调用链。
        result = await self.tool.run(arguments)

        # 验证：不只看成功提示，还检查磁盘上的实际文件。
        self.assertTrue(target.is_file(), "工具应当创建一个普通文件。")

        # read_bytes() 不转换换行，能检查写入内容是否准确。
        actual_bytes = target.read_bytes()
        expected_bytes = content.encode("utf-8")
        self.assertEqual(actual_bytes, expected_bytes, "文件内容应与输入一致。")
        self.assertIn("已创建", result, "返回值应明确说明创建成功。")

    async def test_refuse_to_overwrite_existing_file(self):
        """同名文件已存在时拒绝覆盖，原有内容应保持不变。"""

        target = self.workspace / "existing.py"
        original_content = "print('保留原内容')\r\n"
        original_bytes = original_content.encode("utf-8")

        # 准备一个已有文件，不调用被测工具，避免混淆两个测试场景。
        # xb 是独占创建的二进制模式，按原字节保存测试数据。
        with target.open("xb") as file:
            file.write(original_bytes)

        arguments = {
            "path": "existing.py",
            "content": "print('不应该写入的新内容')\n",
        }

        result = await self.tool.run(arguments)

        self.assertIn("未覆盖", result, "应返回拒绝覆盖的提示。")
        self.assertTrue(target.is_file(), "原文件应仍然存在。")
        self.assertEqual(
            target.read_bytes(),
            original_bytes,
            "拒绝覆盖后，原文件的内容和换行都应保持不变。",
        )

    async def test_create_empty_file(self):
        """空内容是合法输入，应当创建一个大小为零的文件。"""
        result = await self.tool.run({"path": "empty.txt", "content": ""})
        self.assertIn("已创建", result)
        self.assertEqual((self.workspace / "empty.txt").read_bytes(), b"")

    async def test_preserve_mixed_line_endings(self):
        """中文文件名、CRLF 和 LF 都应原样保存。"""
        content = "第一行\r\n第二行\n"
        await self.tool.run({"path": "中文文件.txt", "content": content})
        self.assertEqual(
            (self.workspace / "中文文件.txt").read_bytes(),
            content.encode("utf-8"),
        )

    async def test_directory_cannot_be_overwritten(self):
        target = self.workspace / "existing"
        target.mkdir()
        result = await self.tool.run({"path": "existing", "content": "new"})
        self.assertIn("路径是目录", result)
        self.assertTrue(target.is_dir())

    async def test_file_cannot_be_used_as_parent_directory(self):
        parent = self.workspace / "parent"
        parent.write_bytes(b"keep")
        # Windows 和 Linux 可能抛不同的 OSError 子类，检查共同的父类即可。
        with self.assertRaises(OSError):
            await self.tool.run({"path": "parent/child.txt", "content": "new"})
        self.assertEqual(parent.read_bytes(), b"keep")

    async def test_whitespace_path_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "空白"):
            await self.tool.run({"path": "   ", "content": "new"})
        self.assertEqual(list(self.workspace.iterdir()), [])

    async def test_invalid_arguments_create_nothing(self):
        invalid_arguments = [
            {"path": "", "content": "new"},
            {"content": "new"},
            {"path": "demo.txt"},
            {"path": "demo.txt", "content": None},
        ]
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValidationError):
                    await self.tool.run(arguments)
                self.assertEqual(list(self.workspace.iterdir()), [])

    async def test_absolute_outside_path_is_rejected(self):
        outside = self.temporary_root / "outside.txt"
        with self.assertRaisesRegex(ValueError, "工作区"):
            await self.tool.run({"path": str(outside), "content": "new"})
        self.assertFalse(outside.exists())

    async def test_refuse_path_outside_workspace(self):
        """../ 指向工作区之外时，拒绝写入且不创建外部目录。"""

        # outside_dir 位于临时根目录内，但在 workspace 之外。
        # 即使出现实现错误，测试也只会触及自己的临时区域。
        outside_directory = self.temporary_root / "outside_dir"
        outside_file = outside_directory / "blocked.py"

        arguments = {
            "path": "../outside_dir/blocked.py",
            "content": "print('不能写到工作区外')\n",
        }

        # 这里测试工具本身，因此预期直接抛出 ValueError。
        # 实际聊天时，AgentLoop 会把这个异常转换成工具错误消息。
        # with 里面若没有抛出指定异常，unittest 就会报告测试失败。
        with self.assertRaisesRegex(ValueError, "工作区"):
            await self.tool.run(arguments)

        self.assertFalse(outside_file.exists(), "不应创建工作区外的文件。")
        self.assertFalse(
            outside_directory.exists(),
            "拒绝越界应发生在创建父目录之前。",
        )


if __name__ == "__main__":
    # 直接执行这个文件时也能运行测试；verbosity=2 显示每个测试的名称。
    unittest.main(verbosity=2)
