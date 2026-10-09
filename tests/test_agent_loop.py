"""核心循环测试：假模型给出预设回复，真实 AgentLoop 负责调度工具。

这里不连接模型服务，也不读取 API Key。
假模型只用于控制“模型下一步返回什么”，不会替我们实现被测试的循环。
消息拼接、参数校验、工具分发和步数限制仍然使用项目的真实代码。
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path

from pydantic import BaseModel, Field

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root / "src"))

from zipclaw.core.loop import AgentLoop
from zipclaw.error.custom_errors import AgentStepLimitError
from zipclaw.llm.base import BaseLLM
from zipclaw.llm.types import LLMResponse, ToolCall
from zipclaw.tools.base import BaseTool
from zipclaw.tools.filesystem.edit_file import EditFileTool
from zipclaw.tools.filesystem.read_file import ReadFileTool
from zipclaw.tools.filesystem.write_file import WriteFileTool
from zipclaw.tools.registry import ToolRegistry


class FakeLLM(BaseLLM):
    """按顺序返回提前准备好的回复，同时记录每次收到的请求。"""

    def __init__(self, responses: list[LLMResponse | Exception]):
        self.responses = responses
        self.requests: list[dict] = []

    async def chat(self, messages, tools=None) -> LLMResponse:
        # 先记住这是第几次请求：第一次的下标是 0。
        response_index = len(self.requests)

        # 必须深拷贝。AgentLoop 后面还会修改同一份 messages 列表。
        # 如果只保存引用，最后看到的会是“最终历史”，而非“当时的请求”。
        request_snapshot = {
            "messages": deepcopy(messages),
            "tools": deepcopy(tools),
        }
        self.requests.append(request_snapshot)

        if response_index >= len(self.responses):
            raise AssertionError("模型调用次数超过测试预期，请检查循环是否正确停止。")

        response = self.responses[response_index]

        # 用异常模拟模型请求失败，不需要真的断网或请求付费接口。
        if isinstance(response, Exception):
            raise response

        return response


class RecordTextArgs(BaseModel):
    """测试工具要求提供非空文本，便于验证真实的参数校验过程。"""

    text: str = Field(min_length=1, description="测试用文本。")


class RecordTextTool(BaseTool):
    """记录是否实际执行过；也能模拟一个返回值或预期的工具错误。"""

    name = "record_text"
    description = "仅供测试使用，记录传入的文本。"
    args_model = RecordTextArgs

    def __init__(self, result="执行成功", error: Exception | None = None):
        self.result = result
        self.error = error
        self.received_texts: list[str] = []

    async def execute(self, args: RecordTextArgs):
        self.received_texts.append(args.text)
        if self.error is not None:
            raise self.error
        return self.result


class TestAgentLoop(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        """为每个测试准备独立工具箱和工作区，并收集终端输出。"""
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="zipclaw-loop-test-")
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_root = Path(self.temporary_directory.name).resolve()
        self.workspace = self.temporary_root / "workspace"
        self.workspace.mkdir()

        self.registry = ToolRegistry()
        self.registry.register(ReadFileTool(self.workspace))
        self.registry.register(WriteFileTool(self.workspace))
        self.registry.register(EditFileTool(self.workspace))

        # 将 print 输出暂时存入内存，避免每个测试都打印一大段执行日志。
        # enterContext 会在测试结束时恢复 stdout，即使测试失败也会恢复。
        self.output = io.StringIO()
        self.enterContext(redirect_stdout(self.output))

    async def test_direct_answer_saves_history_and_stops(self):
        """没有工具调用时，一次模型请求即可结束，并保存双方消息。"""
        model = FakeLLM([LLMResponse(content="你好，我是 ZipClaw。")])
        agent = AgentLoop(llm=model, tools=self.registry)

        result = await agent.run("你好")

        self.assertEqual(result, "你好，我是 ZipClaw。")
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(len(agent.messages), 3)
        self.assertEqual(agent.messages[0]["role"], "system")
        self.assertEqual(agent.messages[1], {"role": "user", "content": "你好"})
        self.assertEqual(agent.messages[2], {"role": "assistant", "content": result})
        # 请求时还没有收到回答，因此快照中应当只有 system 和 user。
        self.assertEqual(model.requests[0]["messages"], agent.messages[:2])

    async def test_empty_answer_is_saved_as_string(self):
        model = FakeLLM([LLMResponse(content=None)])
        agent = AgentLoop(llm=model, tools=self.registry)
        result = await agent.run("测试空回复")
        self.assertEqual(result, "")
        self.assertEqual(agent.messages[-1], {"role": "assistant", "content": ""})
        self.assertEqual(len(model.requests), 1)

    async def test_read_result_reaches_next_model_request(self):
        """读文件不应立即结束任务；模型还要收到真实文件原文。"""
        content = "第一行\r\n第二行"
        (self.workspace / "demo.txt").write_bytes(content.encode("utf-8"))
        call = ToolCall(id="read-1", name="read_file", arguments={"path": "demo.txt"})
        model = FakeLLM([
            LLMResponse(content="先读取文件。", tool_calls=[call]),
            LLMResponse(content="文件已读取。"),
        ])
        agent = AgentLoop(llm=model, tools=self.registry)

        result = await agent.run("读取 demo.txt")

        self.assertEqual(result, "文件已读取。")
        self.assertEqual(len(model.requests), 2)
        second_messages = model.requests[1]["messages"]
        roles = []
        for message in second_messages:
            roles.append(message["role"])
        self.assertEqual(roles, ["system", "user", "assistant", "tool"])
        self.assertEqual(second_messages[2]["content"], "先读取文件。")
        self.assertEqual(second_messages[3], {
            "role": "tool", "tool_call_id": "read-1", "content": content,
        })
        # 每轮请求都应提供当前可用工具的参数说明。
        for request in model.requests:
            self.assertEqual(request["tools"], self.registry.schemas())

    async def test_multiple_calls_run_in_order_and_keep_ids(self):
        """同一回复先写再读；读到新文件，才证明两项确实按顺序执行。"""
        calls = [
            ToolCall(id="write-1", name="write_file", arguments={"path": "new.txt", "content": "新内容"}),
            ToolCall(id="read-2", name="read_file", arguments={"path": "new.txt"}),
        ]
        model = FakeLLM([
            LLMResponse(tool_calls=calls), LLMResponse(content="创建并读取完成。"),
        ])
        agent = AgentLoop(llm=model, tools=self.registry)
        await agent.run("创建后读取文件")

        messages = model.requests[1]["messages"]
        self.assertEqual(len(messages), 5)
        self.assertEqual(len(messages[2]["tool_calls"]), 2)
        self.assertEqual(messages[3]["tool_call_id"], "write-1")
        self.assertIn("已创建", messages[3]["content"])
        self.assertEqual(messages[4], {"role": "tool", "tool_call_id": "read-2", "content": "新内容"})
        self.assertEqual((self.workspace / "new.txt").read_text(encoding="utf-8"), "新内容")
        self.assertEqual(len(model.requests), 2)

    async def test_multiple_rounds_read_edit_and_answer(self):
        """每轮可以提出新工具调用，不能执行完第一轮工具就提前返回。"""
        target = self.workspace / "demo.txt"
        target.write_bytes(b"old\r\n")
        model = FakeLLM([
            LLMResponse(tool_calls=[ToolCall(id="r1", name="read_file", arguments={"path": "demo.txt"})]),
            LLMResponse(tool_calls=[ToolCall(id="e1", name="edit_file", arguments={
                "path": "demo.txt", "old_text": "old", "new_text": "new",
            })]),
            LLMResponse(content="修改完成。"),
        ])
        agent = AgentLoop(llm=model, tools=self.registry)
        result = await agent.run("读取并修改文件")

        self.assertEqual(result, "修改完成。")
        self.assertEqual(target.read_bytes(), b"new\r\n")
        self.assertEqual(len(model.requests), 3)
        self.assertEqual(model.requests[1]["messages"][-1]["content"], "old\r\n")
        self.assertEqual(model.requests[2]["messages"][-1]["tool_call_id"], "e1")
        self.assertIn("已修改", model.requests[2]["messages"][-1]["content"])

    async def test_tool_arguments_are_valid_json_strings(self):
        """防止再次出现把 Python 字典直接塞进 function.arguments 的问题。"""
        arguments = {"path": "中文.txt", "content": '他说："你好"\n第二行\\结束'}
        model = FakeLLM([
            LLMResponse(tool_calls=[ToolCall(id="w1", name="write_file", arguments=arguments)]),
            LLMResponse(content="完成"),
        ])
        agent = AgentLoop(llm=model, tools=self.registry)
        await agent.run("创建中文文件")

        saved_call = model.requests[1]["messages"][2]["tool_calls"][0]
        self.assertEqual(saved_call["id"], "w1")
        self.assertEqual(saved_call["type"], "function")
        self.assertEqual(saved_call["function"]["name"], "write_file")
        encoded = saved_call["function"]["arguments"]
        self.assertIsInstance(encoded, str)
        self.assertEqual(json.loads(encoded), arguments)

    async def test_validation_error_can_be_corrected_next_round(self):
        tool = RecordTextTool()
        self.registry.register(tool)
        model = FakeLLM([
            LLMResponse(tool_calls=[ToolCall(id="bad", name="record_text", arguments={})]),
            LLMResponse(tool_calls=[ToolCall(id="fixed", name="record_text", arguments={"text": "修正后"})]),
            LLMResponse(content="参数修正成功。"),
        ])
        agent = AgentLoop(llm=model, tools=self.registry)
        result = await agent.run("测试参数修正")

        error_message = model.requests[1]["messages"][-1]
        self.assertEqual(error_message["tool_call_id"], "bad")
        self.assertIn("工具参数校验失败", error_message["content"])
        self.assertIn("缺少必填参数", error_message["content"])
        # 无效参数必须在 execute 之前拦住，所以只能记录到第二次调用。
        self.assertEqual(tool.received_texts, ["修正后"])
        self.assertEqual(model.requests[2]["messages"][-1]["tool_call_id"], "fixed")
        self.assertEqual(model.requests[2]["messages"][-1]["content"], "执行成功")
        self.assertEqual(result, "参数修正成功。")

    async def test_validation_error_does_not_repeat_raw_input(self):
        """仅验证错误结果不重复原始输入，不宣称历史或终端已经自动脱敏。"""
        tool = RecordTextTool()
        self.registry.register(tool)
        fake_sensitive_value = "仅用于测试的敏感标记-123"
        model = FakeLLM([
            LLMResponse(tool_calls=[ToolCall(id="bad", name="record_text", arguments={
                "text": {"secret": fake_sensitive_value},
            })]),
            LLMResponse(content="需要修正参数。"),
        ])
        agent = AgentLoop(llm=model, tools=self.registry)
        await agent.run("测试错误信息")
        error_text = model.requests[1]["messages"][-1]["content"]
        self.assertIn("参数必须是字符串", error_text)
        self.assertNotIn(fake_sensitive_value, error_text)
        self.assertEqual(tool.received_texts, [])

    async def test_unknown_tool_does_not_block_later_call_in_batch(self):
        tool = RecordTextTool()
        self.registry.register(tool)
        model = FakeLLM([
            LLMResponse(tool_calls=[
                ToolCall(id="unknown", name="not_registered", arguments={}),
                ToolCall(id="valid", name="record_text", arguments={"text": "继续执行"}),
            ]),
            LLMResponse(content="已处理。"),
        ])
        agent = AgentLoop(llm=model, tools=self.registry)
        await agent.run("测试部分失败")
        messages = model.requests[1]["messages"]
        self.assertEqual(messages[-2]["tool_call_id"], "unknown")
        self.assertIn("工具不存在", messages[-2]["content"])
        self.assertEqual(messages[-1]["tool_call_id"], "valid")
        self.assertEqual(messages[-1]["content"], "执行成功")
        self.assertEqual(tool.received_texts, ["继续执行"])

    async def test_path_rejection_becomes_tool_result(self):
        outside = self.temporary_root / "outside.txt"
        outside.write_bytes(b"outside content")
        model = FakeLLM([
            LLMResponse(tool_calls=[ToolCall(id="outside", name="read_file", arguments={"path": "../outside.txt"})]),
            LLMResponse(content="不能越界读取。"),
        ])
        agent = AgentLoop(llm=model, tools=self.registry)
        result = await agent.run("读取外部文件")
        error_message = model.requests[1]["messages"][-1]
        self.assertEqual(error_message["tool_call_id"], "outside")
        self.assertIn("工具执行被拒绝", error_message["content"])
        self.assertNotIn("outside content", error_message["content"])
        self.assertEqual(result, "不能越界读取。")

    async def test_os_error_becomes_friendly_tool_result(self):
        tool = RecordTextTool(error=PermissionError("内部路径不应出现在错误结果中"))
        self.registry.register(tool)
        model = FakeLLM([
            LLMResponse(tool_calls=[ToolCall(id="denied", name="record_text", arguments={"text": "测试"})]),
            LLMResponse(content="操作失败。"),
        ])
        agent = AgentLoop(llm=model, tools=self.registry)
        await agent.run("测试权限错误")
        error_message = model.requests[1]["messages"][-1]
        self.assertEqual(error_message["tool_call_id"], "denied")
        self.assertIn("文件操作失败", error_message["content"])
        self.assertNotIn("内部路径", error_message["content"])

    async def test_non_string_tool_result_is_converted_to_string(self):
        self.registry.register(RecordTextTool(result=42))
        model = FakeLLM([
            LLMResponse(tool_calls=[ToolCall(id="number", name="record_text", arguments={"text": "测试"})]),
            LLMResponse(content="完成"),
        ])
        agent = AgentLoop(llm=model, tools=self.registry)
        await agent.run("测试返回类型")
        self.assertEqual(model.requests[1]["messages"][-1]["content"], "42")

    async def test_reasoning_is_preserved_on_tool_and_final_messages(self):
        self.registry.register(RecordTextTool())
        model = FakeLLM([
            LLMResponse(reasoning_content="测试用推理字段一\n保持原样", tool_calls=[
                ToolCall(id="r1", name="record_text", arguments={"text": "测试"}),
            ]),
            LLMResponse(content="完成", reasoning_content="测试用推理字段二"),
            LLMResponse(content="下一次回答"),
        ])
        agent = AgentLoop(llm=model, tools=self.registry)
        await agent.run("第一次任务")
        await agent.run("第二次任务")
        self.assertEqual(model.requests[1]["messages"][2]["reasoning_content"], "测试用推理字段一\n保持原样")
        self.assertEqual(model.requests[2]["messages"][4]["reasoning_content"], "测试用推理字段二")

    async def test_none_reasoning_is_omitted_but_empty_string_is_preserved(self):
        """None 表示没返回该字段；空字符串则是服务确实返回了一个空值。"""
        for reasoning in [None, ""]:
            with self.subTest(reasoning=reasoning):
                model = FakeLLM([
                    LLMResponse(reasoning_content=reasoning, tool_calls=[
                        ToolCall(id="missing", name="not_registered", arguments={}),
                    ]),
                    LLMResponse(content="完成", reasoning_content=reasoning),
                ])
                agent = AgentLoop(llm=model, tools=self.registry)
                await agent.run("测试可选字段")
                for message in [agent.messages[2], agent.messages[-1]]:
                    if reasoning is None:
                        self.assertNotIn("reasoning_content", message)
                    else:
                        self.assertEqual(message["reasoning_content"], "")

    async def test_conversation_history_survives_multiple_tasks(self):
        model = FakeLLM([LLMResponse(content="第一答"), LLMResponse(content="第二答")])
        agent = AgentLoop(llm=model, tools=self.registry)
        await agent.run("第一问")
        await agent.run("第二问")
        self.assertEqual(model.requests[1]["messages"][1:], [
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "第一答"},
            {"role": "user", "content": "第二问"},
        ])
        self.assertEqual(len(agent.messages), 5)

    async def test_different_agents_do_not_share_history(self):
        first = AgentLoop(llm=FakeLLM([LLMResponse(content="第一答")]), tools=self.registry)
        second_model = FakeLLM([LLMResponse(content="第二答")])
        second = AgentLoop(llm=second_model, tools=self.registry)
        await first.run("第一人的任务")
        await second.run("第二人的任务")
        self.assertIsNot(first.messages, second.messages)
        self.assertEqual(second_model.requests[0]["messages"][1:], [
            {"role": "user", "content": "第二人的任务"},
        ])

    async def test_step_limit_keeps_all_tool_results_and_allows_next_task(self):
        """一轮指一次模型请求，不是只能执行一个工具；停止后历史仍可用。"""
        tool = RecordTextTool()
        self.registry.register(tool)
        model = FakeLLM([
            LLMResponse(tool_calls=[
                ToolCall(id="one", name="record_text", arguments={"text": "第一项"}),
                ToolCall(id="two", name="record_text", arguments={"text": "第二项"}),
            ]),
            LLMResponse(content="两项工具均已执行。"),
        ])
        agent = AgentLoop(llm=model, tools=self.registry, max_steps=1)

        with self.assertRaisesRegex(AgentStepLimitError, "1 轮"):
            await agent.run("执行两项操作")

        self.assertEqual(len(model.requests), 1)
        self.assertEqual(tool.received_texts, ["第一项", "第二项"])
        self.assertEqual(agent.messages[-2]["tool_call_id"], "one")
        self.assertEqual(agent.messages[-1]["tool_call_id"], "two")
        saved_history = deepcopy(agent.messages)

        # 模拟 CLI 捕获异常后，用户又输入一条消息；不重新创建 Agent。
        result = await agent.run("说明刚才的结果")
        self.assertEqual(result, "两项工具均已执行。")
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(model.requests[1]["messages"][:-1], saved_history)

    async def test_final_answer_on_last_allowed_step_is_success(self):
        self.registry.register(RecordTextTool())
        model = FakeLLM([
            LLMResponse(tool_calls=[ToolCall(id="t1", name="record_text", arguments={"text": "测试"})]),
            LLMResponse(content="刚好两轮完成。"),
        ])
        agent = AgentLoop(llm=model, tools=self.registry, max_steps=2)
        result = await agent.run("测试最后一轮")
        self.assertEqual(result, "刚好两轮完成。")
        self.assertEqual(len(model.requests), 2)

    async def test_display_truncation_does_not_shorten_model_history(self):
        """终端预览缩短，不等于发送给模型的工具结果也被截断。"""
        long_argument = "参" * 400 + "参数末尾标记"
        long_result = "果" * 600 + "结果末尾标记"
        tool = RecordTextTool(result=long_result)
        self.registry.register(tool)
        model = FakeLLM([
            LLMResponse(tool_calls=[ToolCall(id="long", name="record_text", arguments={"text": long_argument})]),
            LLMResponse(content="完成"),
        ])
        agent = AgentLoop(llm=model, tools=self.registry)
        await agent.run("测试长输出")

        messages = model.requests[1]["messages"]
        saved_arguments = messages[2]["tool_calls"][0]["function"]["arguments"]
        self.assertEqual(json.loads(saved_arguments), {"text": long_argument})
        self.assertEqual(messages[-1]["content"], long_result)
        self.assertEqual(tool.received_texts, [long_argument])
        terminal_output = self.output.getvalue()
        self.assertIn("参数显示已截断", terminal_output)
        self.assertIn("结果显示已截断", terminal_output)
        self.assertNotIn("参数末尾标记", terminal_output)
        self.assertNotIn("结果末尾标记", terminal_output)

    async def test_model_failure_propagates_without_fabricated_answer(self):
        """记录当前边界：请求异常仍向外抛出，本测试不代表已实现自动恢复。"""
        failure = RuntimeError("测试用模型请求失败")
        model = FakeLLM([failure])
        agent = AgentLoop(llm=model, tools=self.registry)
        with self.assertRaises(RuntimeError) as caught:
            await agent.run("模拟请求失败")
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(len(agent.messages), 2)
        self.assertEqual(agent.messages[-1], {"role": "user", "content": "模拟请求失败"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
