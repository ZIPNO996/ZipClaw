"""模型适配器测试：用假 SDK 响应检查解析和错误转换。

不创建真正的网络客户端，不读取 .env，也不调用付费 API。
SimpleNamespace 用来准备带属性的假对象，例如 response.choices。
这些测试验证适配器，不验证 SDK 自己如何解析网络字节。
"""

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import openai

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root / "src"))

from zipclaw.error.custom_errors import ModelRequestError, ModelResponseError
from zipclaw.llm import openai_llm
from zipclaw.llm.types import LLMResponse


class TestOpenAILLM(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # AsyncMock 可以被 await，适合替代异步的 create() 方法。
        self.create = AsyncMock()
        completions = SimpleNamespace(create=self.create)
        chat = SimpleNamespace(completions=completions)
        self.fake_client = SimpleNamespace(chat=chat)

        # 替换适配器实际使用的构造函数，确保不会创建网络客户端。
        self.client_constructor = self.enterContext(
            patch.object(openai_llm, "AsyncOpenAI", return_value=self.fake_client)
        )
        self.model = openai_llm.OpenAILLM(
            api_key="test-only-key",
            model="test-model",
            base_url="https://model.invalid/v1",
        )
        self.messages = [{"role": "user", "content": "测试问题"}]

    def make_call(self, arguments="{}", call_id="call-1", name="read_file", kind="function"):
        """准备一个尚未经过适配器解析的工具调用。"""
        function = SimpleNamespace(name=name, arguments=arguments)
        return SimpleNamespace(id=call_id, type=kind, function=function)

    def make_response(self, content=None, calls=None):
        """默认不提供推理字段，模拟不返回该字段的模型。"""
        message = SimpleNamespace(content=content, tool_calls=calls)
        choice = SimpleNamespace(message=message)
        return SimpleNamespace(choices=[choice])

    async def assert_response_rejected(self, response, expected_text):
        """多个场景复用：必须抛出项目异常，且只请求一次。"""
        self.create.reset_mock()
        self.create.return_value = response
        with self.assertRaises(ModelResponseError) as caught:
            await self.model.chat(self.messages)
        self.assertIn(expected_text, str(caught.exception))
        self.assertEqual(self.create.await_count, 1)

    def test_client_disables_retries_and_sets_timeout(self):
        """不仅程序不写重试循环，还要明确关闭 SDK 的自动重试。"""
        self.client_constructor.assert_called_once_with(
            api_key="test-only-key",
            base_url="https://model.invalid/v1",
            max_retries=0,
            timeout=120.0,
        )

    async def test_plain_answer_and_request_arguments(self):
        self.create.return_value = self.make_response(content="中文回答")
        tools = [{
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "读取文件",
                "parameters": {"type": "object"},
            },
        }]

        result = await self.model.chat(self.messages, tools=tools)

        self.assertIsInstance(result, LLMResponse)
        self.assertEqual(result.content, "中文回答")
        self.assertEqual(result.tool_calls, [])
        self.assertIsNone(result.reasoning_content)
        self.create.assert_awaited_once_with(
            model="test-model", messages=self.messages, tools=tools,
        )

    async def test_multiple_calls_preserve_order_and_decode_json(self):
        arguments = {
            "path": "中文.txt",
            "content": '第一行\n他说："你好"',
            "options": {"enabled": True, "value": None},
        }
        calls = [
            self.make_call(json.dumps(arguments, ensure_ascii=False), "write-1", "write_file"),
            self.make_call('{"path": "中文.txt"}', "read-2", "read_file"),
        ]
        self.create.return_value = self.make_response(calls=calls)

        result = await self.model.chat(self.messages)

        self.assertIsNone(result.content)
        self.assertTrue(result.has_tool_calls)
        self.assertEqual(len(result.tool_calls), 2)
        self.assertEqual(result.tool_calls[0].id, "write-1")
        self.assertEqual(result.tool_calls[0].name, "write_file")
        self.assertEqual(result.tool_calls[0].arguments, arguments)
        self.assertEqual(result.tool_calls[1].id, "read-2")
        self.assertEqual(result.tool_calls[1].arguments, {"path": "中文.txt"})

    async def test_reasoning_field_preserves_original_value(self):
        for reasoning in [None, "", "测试用推理字段\n第二行"]:
            with self.subTest(reasoning=reasoning):
                response = self.make_response(content="回答")
                response.choices[0].message.reasoning_content = reasoning
                self.create.return_value = response
                result = await self.model.chat(self.messages)
                self.assertEqual(result.reasoning_content, reasoning)

    async def test_missing_choices_or_message_is_rejected(self):
        cases = [
            (SimpleNamespace(choices=[]), "候选回复"),
            (SimpleNamespace(choices=[SimpleNamespace(message=None)]), "消息为空"),
        ]
        for response, expected in cases:
            with self.subTest(expected=expected):
                await self.assert_response_rejected(response, expected)

    async def test_unsupported_tool_type_is_rejected(self):
        call = self.make_call(kind="custom")
        await self.assert_response_rejected(self.make_response(calls=[call]), "不支持")

    async def test_invalid_call_ids_are_rejected(self):
        for call_id in [None, "", "   ", 123]:
            with self.subTest(call_id=call_id):
                call = self.make_call(call_id=call_id)
                await self.assert_response_rejected(self.make_response(calls=[call]), "有效 ID")

    async def test_duplicate_call_ids_are_rejected(self):
        calls = [self.make_call(call_id="same"), self.make_call(call_id="same")]
        await self.assert_response_rejected(self.make_response(calls=calls), "重复")

    async def test_invalid_tool_names_are_rejected(self):
        for name in [None, "", "   ", 123]:
            with self.subTest(name=name):
                call = self.make_call(name=name)
                await self.assert_response_rejected(self.make_response(calls=[call]), "有效名称")

    async def test_raw_arguments_must_be_string(self):
        for arguments in [None, {}, [], 42]:
            with self.subTest(arguments=arguments):
                call = self.make_call(arguments=arguments)
                await self.assert_response_rejected(self.make_response(calls=[call]), "JSON 字符串")

    async def test_malformed_json_does_not_echo_raw_arguments(self):
        # 这是测试标记，不是真实密钥。检查错误提示不重复原始参数。
        marker = "测试专用敏感标记"
        raw_arguments = '{"secret": "' + marker
        call = self.make_call(arguments=raw_arguments)
        self.create.return_value = self.make_response(calls=[call])

        with self.assertRaises(ModelResponseError) as caught:
            await self.model.chat(self.messages)

        self.assertIn("不是合法 JSON", str(caught.exception))
        self.assertNotIn(marker, str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, json.JSONDecodeError)

    async def test_valid_json_must_decode_to_object(self):
        for arguments in ["[]", "null", "42", "true", '"hello"']:
            with self.subTest(arguments=arguments):
                call = self.make_call(arguments=arguments)
                await self.assert_response_rejected(self.make_response(calls=[call]), "JSON 对象")

    async def test_invalid_second_call_rejects_whole_response(self):
        calls = [
            self.make_call('{"path": "demo.txt"}', "good"),
            self.make_call("{broken", "bad"),
        ]
        # chat() 应抛出异常，不能返回只包含第一个调用的部分响应。
        await self.assert_response_rejected(self.make_response(calls=calls), "不是合法 JSON")

    async def test_invalid_reply_fields_become_response_error(self):
        for field_name in ["content", "reasoning_content"]:
            with self.subTest(field=field_name):
                response = self.make_response(content="回答")
                setattr(response.choices[0].message, field_name, {"invalid": True})
                await self.assert_response_rejected(response, "回复字段")

    async def test_timeout_is_distinguished_from_connection_failure(self):
        # SDK 异常只需要一个 request 属性对象；这里不会发送请求。
        request = SimpleNamespace(url="https://model.invalid/v1")
        error = openai.APITimeoutError(request=request)
        self.create.side_effect = error

        with self.assertRaises(ModelRequestError) as caught:
            await self.model.chat(self.messages)

        self.assertIn("超时", str(caught.exception))
        self.assertIs(caught.exception.__cause__, error)
        self.assertEqual(self.create.await_count, 1)

    async def test_connection_failure_becomes_request_error(self):
        request = SimpleNamespace(url="https://model.invalid/v1")
        error = openai.APIConnectionError(request=request)
        self.create.side_effect = error

        with self.assertRaises(ModelRequestError) as caught:
            await self.model.chat(self.messages)

        self.assertIn("无法连接", str(caught.exception))
        self.assertIs(caught.exception.__cause__, error)
        self.assertEqual(self.create.await_count, 1)

    async def test_http_errors_have_readable_messages(self):
        cases = [
            (400, openai.BadRequestError, "无法接受"),
            (401, openai.AuthenticationError, "认证失败"),
            (403, openai.PermissionDeniedError, "拒绝访问"),
            (404, openai.NotFoundError, "接口或模型不存在"),
            (422, openai.UnprocessableEntityError, "无法接受"),
            (429, openai.RateLimitError, "请求受到限制"),
            (500, openai.InternalServerError, "服务端发生错误"),
            (503, openai.InternalServerError, "服务端发生错误"),
            (409, openai.ConflictError, "409"),
        ]
        for status, error_class, expected in cases:
            with self.subTest(status=status):
                # 构造真实 SDK 异常，但响应对象是本地准备的假对象。
                response = SimpleNamespace(
                    status_code=status,
                    request=SimpleNamespace(url="https://model.invalid/v1"),
                    headers={},
                )
                error = error_class(
                    "原始服务错误标记",
                    response=response,
                    body={"message": "原始服务错误标记"},
                )
                self.create.reset_mock()
                self.create.side_effect = error

                with self.assertRaises(ModelRequestError) as caught:
                    await self.model.chat(self.messages)

                self.assertIn(expected, str(caught.exception))
                self.assertNotIn("原始服务错误标记", str(caught.exception))
                self.assertIs(caught.exception.__cause__, error)
                self.assertEqual(self.create.await_count, 1)

    async def test_unexpected_programming_error_is_not_disguised(self):
        """未预期的代码错误仍向外抛出，便于发现真正的程序问题。"""
        error = RuntimeError("测试用程序错误")
        self.create.side_effect = error
        with self.assertRaises(RuntimeError) as caught:
            await self.model.chat(self.messages)
        self.assertIs(caught.exception, error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
