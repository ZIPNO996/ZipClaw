"""命令工具测试：启动本次测试所用的 Python，不依赖系统的 python 命令。"""

import sys
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root / "src"))

from zipclaw.tools.shell.run_command import RunCommandTool


class TestRunCommandTool(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        # 故意带空格和中文，确认工作目录不需要手动添加引号。
        self.workspace = Path(self.temporary_directory.name).resolve() / "测试 workspace"
        self.workspace.mkdir()
        self.tool = RunCommandTool(workspace=self.workspace)

    def python_command(self, code):
        """组装命令列表；-c 后面的一整个字符串是要执行的 Python 代码。"""
        # sys.executable 是运行测试的解释器绝对路径。
        # -X utf8 保证子进程输出编码与工具的 UTF-8 解码一致。
        return [sys.executable, "-X", "utf8", "-c", code]

    async def test_success_with_chinese_output(self):
        command = self.python_command("print('检查通过')")
        result = await self.tool.run({"command": command})
        self.assertIn("退出码：0", result)
        self.assertIn("正常输出：\n检查通过", result)
        self.assertIn("错误输出：\n（无）", result)

    async def test_nonzero_exit_preserves_both_outputs(self):
        code = "import sys; print('运行中'); print('实际报错', file=sys.stderr); sys.exit(3)"
        result = await self.tool.run({"command": self.python_command(code)})
        self.assertIn("退出码：3", result)
        self.assertIn("正常输出：\n运行中", result)
        self.assertIn("错误输出：\n实际报错", result)

    async def test_command_runs_in_workspace(self):
        """子进程读取相对路径，成功说明 cwd 确实是工作区。"""
        (self.workspace / "marker.txt").write_text("工作区标记", encoding="utf-8")
        code = "from pathlib import Path; print(Path('marker.txt').read_text(encoding='utf-8'))"
        result = await self.tool.run({"command": self.python_command(code)})
        self.assertIn("退出码：0", result)
        self.assertIn("工作区标记", result)

    async def test_empty_outputs(self):
        result = await self.tool.run({"command": self.python_command("pass")})
        self.assertEqual(result, "退出码：0\n正常输出：\n（无）\n错误输出：\n（无）")

    async def test_stdin_is_closed(self):
        """读取标准输入应立刻得到 EOF，而不是一直等待用户打字。"""
        code = "import sys; print(repr(sys.stdin.read()))"
        result = await self.tool.run({"command": self.python_command(code), "timeout": 5})
        self.assertIn("退出码：0", result)
        self.assertIn("正常输出：\n''", result)

    async def test_arguments_are_not_interpreted_by_shell(self):
        """空格和 & 原样作为参数传递，不拆成 Shell 命令。"""
        command = self.python_command("import sys; print(sys.argv[1])")
        command.append("hello world & echo 不应执行")
        result = await self.tool.run({"command": command})
        self.assertIn("退出码：0", result)
        self.assertIn("正常输出：\nhello world & echo 不应执行\n", result)

    async def test_stdout_keeps_head_and_stderr_keeps_tail(self):
        code = "import sys; sys.stdout.write('ABCDE12345'); sys.stderr.write('12345VWXYZ')"
        result = await self.tool.run({
            "command": self.python_command(code), "max_output_chars": 5,
        })
        expected = (
            "退出码：0\n正常输出：\nABCDE\n"
            "……[正常输出已截断，省略了后面的 5 个字符]\n"
            "错误输出：\n[错误输出已截断，省略了前面的 5 个字符]\nVWXYZ"
        )
        self.assertEqual(result, expected)

    async def test_output_at_limit_is_not_truncated(self):
        code = "import sys; sys.stdout.write('ABCDE'); sys.stderr.write('VWXYZ')"
        result = await self.tool.run({
            "command": self.python_command(code), "max_output_chars": 5,
        })
        self.assertNotIn("截断", result)
        self.assertIn("ABCDE", result)
        self.assertIn("VWXYZ", result)

    async def test_invalid_output_bytes_do_not_crash_decoder(self):
        code = "import sys; sys.stdout.buffer.write(b'hello\\xff')"
        result = await self.tool.run({"command": self.python_command(code)})
        self.assertIn("退出码：0", result)
        self.assertIn("hello\ufffd", result)

    async def test_real_process_timeout(self):
        """实际启动一个睡眠进程，确认超时转换成友好提示。"""
        # subprocess.run 超时后会终止并等待这个直接子进程。
        # 本测试不创建孙进程，不需要依赖操作系统的进程树清理。
        command = self.python_command("import time; time.sleep(30)")
        result = await self.tool.run({"command": command, "timeout": 1})
        self.assertIn("执行超时", result)
        self.assertIn("1 秒", result)

    async def test_missing_program(self):
        # 明确指向临时目录中的不存在路径，不依赖 PATH 的配置。
        program = self.workspace / "does-not-exist.exe"
        result = await self.tool.run({"command": [str(program)]})
        self.assertIn("启动失败", result)

    async def test_blank_program_name(self):
        result = await self.tool.run({"command": ["   "]})
        self.assertIn("程序名称不能为空", result)

    async def test_invalid_arguments(self):
        invalid_arguments = [
            {}, {"command": []}, {"command": "python --version"},
            {"command": [123]},
            {"command": [sys.executable], "timeout": 0},
            {"command": [sys.executable], "timeout": 121},
            {"command": [sys.executable], "max_output_chars": 0},
            {"command": [sys.executable], "max_output_chars": 20001},
        ]
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValidationError):
                    await self.tool.run(arguments)


if __name__ == "__main__":
    unittest.main(verbosity=2)
