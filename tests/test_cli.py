"""CLI 集成测试：模拟输入和网络返回，运行真实 main()。

模型适配器、AgentLoop、工具注册表和会话存储都使用真实代码。
只将 SDK 客户端、input()、配置加载和目录位置临时替换。
所有文件都在独立临时目录中，测试结束后自动清理。
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import openai

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root / "src"))

from zipclaw import cli
from zipclaw.core.session import SessionStore
from zipclaw.llm import openai_llm


class TestCLI(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="zipclaw-cli-test-")
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.test_home = self.root / "home"
        self.session_directory = self.test_home / ".zipclaw" / "sessions"

    def make_call(self, call_id, name, arguments):
        """传入字典，转换成 SDK 返回的 JSON 参数字符串。"""
        function = SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments, ensure_ascii=False),
        )
        return SimpleNamespace(id=call_id, type="function", function=function)

    def make_response(self, content=None, calls=None):
        message = SimpleNamespace(content=content, tool_calls=calls)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    def connection_error(self):
        request = SimpleNamespace(url="https://model.invalid/v1")
        return openai.APIConnectionError(request=request)

    async def run_cli(self, inputs, responses, workspace=None):
        """运行一次 CLI，返回终端输出、请求快照和输入调用记录。"""
        if workspace is None:
            workspace = self.workspace

        requests = []

        async def fake_create(**kwargs):
            response_index = len(requests)
            # messages 后面还会继续变化，必须保存深拷贝。
            requests.append(deepcopy(kwargs))
            if response_index >= len(responses):
                raise AssertionError("发生了测试未预期的额外模型请求。")
            response = responses[response_index]
            if isinstance(response, Exception):
                raise response
            return response

        create = AsyncMock(side_effect=fake_create)
        completions = SimpleNamespace(create=create)
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        terminal_output = io.StringIO()

        # ExitStack 统一管理多个临时替换，离开 with 时自动恢复。
        with ExitStack() as stack:
            # 不加载真实 .env，环境变量也只提供测试用的占位值。
            stack.enter_context(patch.object(cli, "load_dotenv"))
            stack.enter_context(patch.dict(os.environ, {
                "OPENAI_API_KEY": "test-only-key",
                "OPENAI_MODEL": "test-model",
                "OPENAI_BASE_URL": "https://model.invalid/v1",
            }))
            stack.enter_context(patch.object(openai_llm, "AsyncOpenAI", return_value=fake_client))

            # 分别替换工作目录和用户目录，避免接触真实项目及会话。
            stack.enter_context(patch.object(Path, "cwd", return_value=workspace))
            stack.enter_context(patch.object(Path, "home", return_value=self.test_home))

            # 每调用一次 input()，返回 inputs 中的下一项。
            # 如果程序错误地继续读取，列表耗尽会让测试报错。
            input_mock = stack.enter_context(patch("builtins.input", side_effect=inputs))
            stack.enter_context(redirect_stdout(terminal_output))

            await cli.main()

        return {
            "output": terminal_output.getvalue(),
            "requests": requests,
            "input_mock": input_mock,
        }

    def only_session_file(self):
        """单工作区测试中应该恰好生成一个 JSON 文件。"""
        files = list(self.session_directory.glob("*.json"))
        self.assertEqual(len(files), 1)
        return files[0]

    async def seed_history(self):
        """通过真实 CLI 先完成一次对话，用于后续重启和错误测试。"""
        await self.run_cli(
            ["项目叫小熊", "exit"],
            [self.make_response(content="记住了")],
        )
        return self.only_session_file()

    async def test_normal_answer_is_saved_and_all_tools_are_registered(self):
        run = await self.run_cli(["你好", "exit"], [self.make_response(content="你好！")])
        history = SessionStore(self.only_session_file()).load()

        self.assertIn("ZipClaw > 你好！", run["output"])
        self.assertEqual(history[0]["role"], "system")
        self.assertEqual(history[1:], [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！"},
        ])
        self.assertEqual(len(run["requests"]), 1)
        names = []
        for schema in run["requests"][0]["tools"]:
            names.append(schema["function"]["name"])
        self.assertEqual(set(names), {
            "list_dir", "read_file", "write_file", "edit_file", "grep", "run_command",
        })
        self.assertEqual(len(names), 6)

    async def test_restart_restores_history_and_uses_current_system_prompt(self):
        session_file = await self.seed_history()
        store = SessionStore(session_file)
        history = store.load()
        history[0]["content"] = "测试用的旧系统提示词"
        store.save(history)

        run = await self.run_cli(["项目叫什么？", "exit"], [self.make_response(content="小熊")])

        sent = run["requests"][0]["messages"]
        self.assertNotEqual(sent[0]["content"], "测试用的旧系统提示词")
        self.assertEqual(sent[0]["role"], "system")
        self.assertEqual(sent[1:-1], history[1:])
        self.assertEqual(sent[-1], {"role": "user", "content": "项目叫什么？"})
        self.assertIn("已恢复 2 条历史消息", run["output"])
        self.assertEqual(store.load()[:-1], sent)

    async def test_workspaces_with_same_name_have_separate_sessions(self):
        first_file = await self.seed_history()
        original = first_file.read_bytes()
        other = self.root / "other" / "workspace"
        other.mkdir(parents=True)

        run = await self.run_cli(
            ["另一个项目", "exit"],
            [self.make_response(content="另一份记录")],
            workspace=other,
        )

        self.assertEqual(run["requests"][0]["messages"][1:], [
            {"role": "user", "content": "另一个项目"},
        ])
        self.assertEqual(len(list(self.session_directory.glob("*.json"))), 2)
        self.assertEqual(first_file.read_bytes(), original)

    async def test_clear_removes_old_history_before_next_request(self):
        session_file = await self.seed_history()
        run = await self.run_cli(
            ["/clear", "新的问题", "exit"],
            [self.make_response(content="新的回答")],
        )

        self.assertIn("对话已清空", run["output"])
        self.assertEqual(len(run["requests"]), 1)
        self.assertEqual(run["requests"][0]["messages"][1:], [
            {"role": "user", "content": "新的问题"},
        ])
        history = SessionStore(session_file).load()
        self.assertEqual(len(history), 3)
        self.assertEqual(history[-1]["content"], "新的回答")

    async def test_clear_without_new_task_is_persisted(self):
        session_file = await self.seed_history()
        await self.run_cli(["/clear", "exit"], [])
        history = SessionStore(session_file).load()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["role"], "system")
        restarted = await self.run_cli(["exit"], [])
        self.assertIn("已恢复 0 条历史消息", restarted["output"])

    async def test_clear_save_failure_preserves_memory_and_disk(self):
        session_file = await self.seed_history()
        original_bytes = session_file.read_bytes()
        old_history = SessionStore(session_file).load()

        # 模拟磁盘拒绝写入，不更改电脑的实际文件权限。
        with patch.object(SessionStore, "save", side_effect=PermissionError("测试写入失败")):
            run = await self.run_cli(
                ["/clear", "继续聊", "exit"],
                [self.make_response(content="仍可继续")],
            )

        self.assertIn("清空失败", run["output"])
        self.assertEqual(run["requests"][0]["messages"][:-1], old_history)
        self.assertEqual(session_file.read_bytes(), original_bytes)

    async def test_corrupt_session_stops_before_input_and_preserves_file(self):
        session_file = await self.seed_history()
        session_file.write_bytes(b"{broken")
        run = await self.run_cli([], [])

        self.assertIn("会话恢复失败", run["output"])
        run["input_mock"].assert_not_called()
        self.assertEqual(run["requests"], [])
        self.assertEqual(session_file.read_bytes(), b"{broken")

    async def test_session_without_initial_system_message_is_rejected(self):
        session_file = await self.seed_history()
        SessionStore(session_file).save([{"role": "user", "content": "缺少系统消息"}])
        original = session_file.read_bytes()
        run = await self.run_cli([], [])

        self.assertIn("第一条消息不是系统消息", run["output"])
        run["input_mock"].assert_not_called()
        self.assertEqual(run["requests"], [])
        self.assertEqual(session_file.read_bytes(), original)

    async def test_unreadable_session_stops_startup(self):
        with patch.object(SessionStore, "load", side_effect=PermissionError("测试读取失败")):
            run = await self.run_cli([], [])
        self.assertIn("会话恢复失败", run["output"])
        run["input_mock"].assert_not_called()
        self.assertEqual(run["requests"], [])
        self.assertFalse(self.session_directory.exists())

    async def test_save_failure_does_not_end_chat_or_lose_memory(self):
        # 第一次保存失败，第二次恢复正常，检查完整历史能否再次保存。
        real_save = SessionStore.save
        save_count = 0

        def save_with_first_failure(store, messages):
            nonlocal save_count
            save_count += 1
            if save_count == 1:
                raise PermissionError("测试写入失败")
            return real_save(store, messages)

        with patch.object(SessionStore, "save", autospec=True, side_effect=save_with_first_failure):
            run = await self.run_cli(
                ["第一问", "第二问", "exit"],
                [self.make_response(content="第一答"), self.make_response(content="第二答")],
            )

        self.assertIn("[保存失败]", run["output"])
        self.assertEqual(save_count, 2)
        history = SessionStore(self.only_session_file()).load()
        self.assertEqual(history[1:], [
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "第一答"},
            {"role": "user", "content": "第二问"},
            {"role": "assistant", "content": "第二答"},
        ])
        self.assertEqual(run["requests"][1]["messages"], history[:-1])

    async def test_request_failure_is_saved_before_exit(self):
        run = await self.run_cli(["测试请求失败", "exit"], [self.connection_error()])
        history = SessionStore(self.only_session_file()).load()

        self.assertIn("[模型请求失败]", run["output"])
        self.assertNotIn("Traceback", run["output"])
        self.assertEqual(len(run["requests"]), 1)
        # 失败请求没有伪造助手回答，只保存用户已经提出的问题。
        self.assertEqual(history[1:], [{"role": "user", "content": "测试请求失败"}])

    async def test_request_failure_keeps_completed_tool_results_and_accepts_next_input(self):
        (self.workspace / "demo.txt").write_text("文件原文", encoding="utf-8")
        call = self.make_call("read-1", "read_file", {"path": "demo.txt"})
        run = await self.run_cli(
            ["读取文件", "继续聊", "exit"],
            [
                self.make_response(calls=[call]),
                self.connection_error(),
                self.make_response(content="下一条输入正常"),
            ],
        )

        self.assertIn("[模型请求失败]", run["output"])
        self.assertIn("ZipClaw > 下一条输入正常", run["output"])
        self.assertEqual(len(run["requests"]), 3)
        history = SessionStore(self.only_session_file()).load()
        self.assertEqual(len(history), 6)
        self.assertEqual(history[2]["tool_calls"][0]["id"], "read-1")
        self.assertEqual(history[3], {
            "role": "tool", "tool_call_id": "read-1", "content": "文件原文",
        })
        self.assertEqual(run["requests"][2]["messages"], history[:-1])

    async def test_invalid_second_call_executes_neither_and_keeps_previous_round(self):
        (self.workspace / "demo.txt").write_text("前轮原文", encoding="utf-8")
        read_call = self.make_call("read-old", "read_file", {"path": "demo.txt"})
        good_call = self.make_call(
            "write-good", "write_file", {"path": "must-not-exist.txt", "content": "禁止写入"},
        )
        bad_call = self.make_call("write-bad", "write_file", {})
        bad_call.function.arguments = "{broken"

        run = await self.run_cli(
            ["开始任务", "继续聊", "exit"],
            [
                self.make_response(calls=[read_call]),
                self.make_response(calls=[good_call, bad_call]),
                self.make_response(content="可以继续"),
            ],
        )

        self.assertIn("[模型响应无效]", run["output"])
        self.assertIn("ZipClaw > 可以继续", run["output"])
        self.assertFalse((self.workspace / "must-not-exist.txt").exists())
        self.assertEqual(len(run["requests"]), 3)
        history = SessionStore(self.only_session_file()).load()
        self.assertEqual(len(history), 6)
        self.assertEqual(history[2]["tool_calls"][0]["id"], "read-old")
        self.assertEqual(history[3]["tool_call_id"], "read-old")
        self.assertEqual(history[3]["content"], "前轮原文")
        saved_text = json.dumps(history)
        self.assertNotIn("write-good", saved_text)
        self.assertNotIn("write-bad", saved_text)
        self.assertEqual(run["requests"][2]["messages"], history[:-1])

    async def test_clear_after_request_or_response_error_allows_new_chat(self):
        # 两种错误都应回到输入界面，让 /clear 能正常执行。
        failures = [self.connection_error(), SimpleNamespace(choices=[])]
        for failure in failures:
            with self.subTest(failure_type=type(failure).__name__):
                run = await self.run_cli(
                    ["失败任务", "/clear", "新问题", "exit"],
                    [failure, self.make_response(content="新回答")],
                )
                self.assertEqual(len(run["requests"]), 2)
                self.assertEqual(run["requests"][1]["messages"][1:], [
                    {"role": "user", "content": "新问题"},
                ])
                self.assertIn("对话已清空", run["output"])
                history = SessionStore(self.only_session_file()).load()
                self.assertEqual(len(history), 3)
                self.assertEqual(history[-1]["content"], "新回答")

    async def test_step_limit_saves_results_and_accepts_next_input(self):
        (self.workspace / "demo.txt").write_text("原文", encoding="utf-8")
        responses = []
        for index in range(10):
            call = self.make_call(f"read-{index}", "read_file", {"path": "demo.txt"})
            responses.append(self.make_response(calls=[call]))
        responses.append(self.make_response(content="后续回答"))

        run = await self.run_cli(["多轮任务", "解释结果", "exit"], responses)

        self.assertIn("[任务停止]", run["output"])
        self.assertIn("10 轮", run["output"])
        self.assertEqual(len(run["requests"]), 11)
        history = SessionStore(self.only_session_file()).load()
        self.assertEqual(len(history), 24)
        self.assertEqual(history[-3]["tool_call_id"], "read-9")
        self.assertEqual(history[-3]["content"], "原文")
        self.assertEqual(history[-1]["content"], "后续回答")
        self.assertEqual(run["requests"][-1]["messages"], history[:-1])

    async def test_blank_input_and_exit_do_not_call_model(self):
        for exit_command in ["exit", "QUIT"]:
            with self.subTest(command=exit_command):
                run = await self.run_cli(["   ", exit_command], [])
                self.assertEqual(run["requests"], [])
                self.assertIn("已退出", run["output"])
                self.assertFalse(self.session_directory.exists())

    async def test_eof_and_keyboard_interrupt_at_input_exit_cleanly(self):
        """只验证等待输入时退出，不宣称已支持执行工具时的中断恢复。"""
        for error in [EOFError(), KeyboardInterrupt()]:
            with self.subTest(error=type(error).__name__):
                run = await self.run_cli([error], [])
                self.assertEqual(run["requests"], [])
                self.assertIn("已退出", run["output"])
                self.assertNotIn("Traceback", run["output"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
