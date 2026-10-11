# src/zipclaw/llm/openai_llm.py

import json
import openai
from openai import AsyncOpenAI
from collections.abc import AsyncIterator
from types import SimpleNamespace

from .base import BaseLLM
from .llm_types import LLMResponse,ToolCall,LLMStreamEvent,TextDeltaEvent,ReasoningDeltaEvent,ResponseCompletedEvent
from ..error.custom_errors import ModelRequestError,ModelResponseError
from pydantic import ValidationError

class OpenAILLM(BaseLLM):
    """
    兼容 OpenAI 消息协议的模型适配器。

    OpenAI、部分第三方模型接口都可以走类似协议。
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str | None = None,
    ):
        self.model = model

        # 创建异步客户端。
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,

            # 关闭 SDK 自带的自动重试，避免与摘要请求的有限重试叠加。
            # 普通任务的流式请求当前没有自动重试逻辑。
            max_retries=0,

            # 设置网络请求超时，单位为秒。
            # 先用 120 秒，后面可以改成配置项。
            timeout=120.0,
        )

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> LLMResponse:

        # 普通非流式请求；当前上下文摘要使用这个入口。
        # 这里没有显式传入 thinking 参数，思考模式由服务端默认设置决定。
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools,
            )

        except openai.APITimeoutError as exc:
            raise ModelRequestError(
                "模型请求超时。",
                retryable=True,
            ) from exc

        except openai.APIConnectionError as exc:
            raise ModelRequestError(
                "无法连接模型服务。请检查网络、代理和服务地址。",
                retryable=True,
            ) from exc

        except openai.APIStatusError as exc:
            # 服务返回了 HTTP 错误状态码。
            status_code = exc.status_code

            if status_code == 401:
                message = (
                    "模型服务认证失败。"
                    "请检查 API Key 是否正确、有效，以及是否对应当前服务地址。"
                )

            elif status_code == 403:
                message = (
                    "模型服务拒绝访问。"
                    "请检查账号权限和模型访问权限。"
                )

            elif status_code == 404:
                message = (
                    "请求的接口或模型不存在。"
                    "请检查服务地址和模型名称。"
                )

            elif status_code == 400 or status_code == 422:
                message = (
                    "模型服务无法接受当前请求。"
                    "请检查模型支持的参数和消息格式。"
                )

            elif status_code == 429:
                message = (
                    "请求受到限制。"
                    "可能是请求过于频繁，也可能是账户额度不足，"
                    "请检查服务商控制台。"
                )

            elif status_code >= 500:
                message = (
                    "模型服务端发生错误。"
                    "请稍后手动重试。"
                )

            else:
                message = (
                    f"模型请求失败，HTTP 状态码：{status_code}。"
                    "请检查服务配置。"
                )

            # 标记可能适合有限重试的状态；这里只分类，不直接重试。
            # 429 也可能是额度不足，因此标记可重试不保证重试会成功。
            retryable_status_codes = {
                408,  # 请求超时。
                429,  # 请求受限。
                500,  # 服务内部错误。
                502,  # 网关错误。
                503,  # 服务暂时不可用。
                504,  # 网关超时。
            }

            retryable = status_code in retryable_status_codes

            raise ModelRequestError(
                message,
                retryable=retryable,
            ) from exc

        # 第一版只处理响应中的第一个候选回答（choice）。
        # 没有候选回复时，不能直接访问 choices[0]。
        if not response.choices:
            raise ModelResponseError(
                "模型没有返回候选回复，本次任务已停止。"
            )

        message = response.choices[0].message

        # 将完整消息交给统一的解析方法。
        return self.parse_message(message)



    def parse_message(self, message) -> LLMResponse:
        """将完整的服务端消息解析为 ZipClaw 的统一响应。"""

        if message is None:
            raise ModelResponseError(
                "模型返回的消息为空，本次任务已停止。"
            )

        tool_calls: list[ToolCall] = []

        # 用于检查同一次回复中，工具调用 ID 是否重复。
        seen_call_ids: set[str] = set()

        if message.tool_calls:
            for call in message.tool_calls:
                # 当前只支持 function 类型的工具调用。
                if call.type != "function":
                    raise ModelResponseError(
                        "模型返回了当前不支持的工具调用类型。"
                    )

                # 工具结果需要通过 ID 对应到工具调用。
                if not isinstance(call.id, str) or not call.id.strip():
                    raise ModelResponseError(
                        "模型返回的工具调用缺少有效 ID。"
                    )

                if call.id in seen_call_ids:
                    raise ModelResponseError(
                        "同一次模型回复中出现了重复的工具调用 ID。"
                    )

                seen_call_ids.add(call.id)

                # 先取出原始参数，不立即执行任何工具。
                raw_arguments = call.function.arguments

                if not isinstance(raw_arguments, str):
                    raise ModelResponseError(
                        "模型返回的工具参数不是 JSON 字符串。"
                    )

                try:
                    arguments = json.loads(raw_arguments)
                except json.JSONDecodeError as exc:
                    # 不把完整原始参数放进错误提示，
                    # 因为其中可能包含代码或敏感内容。
                    raise ModelResponseError(
                        "模型返回的工具参数不是合法 JSON，"
                        "本轮工具尚未执行。"
                    ) from exc

                # 合法 JSON 也可能是列表、数字或 null。
                # 我们的工具参数必须是字典。
                if not isinstance(arguments, dict):
                    raise ModelResponseError(
                        "模型返回的工具参数必须是 JSON 对象，"
                        "不能是列表、数字或 null。"
                    )

                tool_name = call.function.name

                if not isinstance(tool_name, str) or not tool_name.strip():
                    raise ModelResponseError(
                        "模型返回的工具调用缺少有效名称。"
                    )

                try:
                    tool_call = ToolCall(
                        id=call.id,
                        name=tool_name,
                        arguments=arguments,
                    )
                except ValidationError as exc:
                    raise ModelResponseError(
                        "模型返回的工具调用结构不符合要求。"
                    ) from exc

                tool_calls.append(tool_call)

        # 全部工具调用都解析成功后，才返回给 AgentLoop。
        try:
            result = LLMResponse(
                content=message.content,
                #从 message 对象中安全地读取 reasoning_content 属性；如果这个属性不存在，就返回 None，而不是报错。
                reasoning_content=getattr(
                    message,
                    "reasoning_content",
                    None,
                ),
                tool_calls=tool_calls,
            )
        except ValidationError as exc:
            raise ModelResponseError(
                "模型返回的回复字段不符合要求。"
            ) from exc

        return result

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[LLMStreamEvent]:
        """接收流式响应，返回文本片段，最后返回完整响应。"""

        # 保存完整正文和推理文本。
        # None 表示服务端尚未提供这个字段。
        content_text: str | None = None
        reasoning_text: str | None = None

        # 一次响应可能调用多个工具。
        # 用服务端给出的 index 区分它们，分别拼接。
        tool_buffers: dict[int, dict] = {}

        # 用来确认模型正常结束，而不是连接中途断开。
        finish_reason: str | None = None

        try:
            # stream=True 表示要求服务端分批返回结果。
            # thinking 是 DeepSeek 风格的扩展参数，需要当前服务支持或转发。
            # 这里只显式开启思考模式，没有设置 reasoning_effort 思考强度。
            response_stream = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools,
                stream=True,
                extra_body={
                    "thinking": {
                        "type": "enabled",
                    },
                },
            )

            # 流正常耗尽或在此代码块中抛出异常时关闭连接。
            # 调用方提前停止迭代时，也应及时关闭这个异步生成器。
            async with response_stream:
                async for chunk in response_stream:

                    # 部分数据块只包含统计信息，没有回答片段。
                    if not chunk.choices:
                        continue

                    # 和普通请求一样，第一版只处理第一个候选回答。
                    choice = chunk.choices[0]

                    if choice.index != 0:
                        continue

                    # 收到结束原因后，不应该再收到该候选的内容。
                    if finish_reason is not None:
                        raise ModelResponseError(
                            "模型在结束标记后继续返回了回答片段。"
                        )

                    delta = choice.delta

                    # ---------- 接收推理片段 ----------

                    reasoning_piece = getattr(
                        delta,
                        "reasoning_content",
                        None,
                    )

                    if reasoning_piece is not None:
                        if reasoning_text is None:
                            reasoning_text = ""

                        reasoning_text += reasoning_piece

                        if reasoning_piece:
                            yield ReasoningDeltaEvent(
                                text=reasoning_piece,
                            )

                    # ---------- 接收正文片段 ----------

                    content_piece = delta.content

                    if content_piece is not None:
                        if content_text is None:
                            content_text = ""

                        content_text += content_piece

                        if content_piece:
                            yield TextDeltaEvent(
                                text=content_piece,
                            )

                    # ---------- 收集工具调用片段 ----------

                    if delta.tool_calls:
                        for tool_piece in delta.tool_calls:
                            tool_index = tool_piece.index

                            if (
                                not isinstance(tool_index, int)
                                or tool_index < 0
                            ):
                                raise ModelResponseError(
                                    "工具调用片段缺少有效的序号。"
                                )

                            # 第一次遇到这个工具序号，创建存放位置。
                            if tool_index not in tool_buffers:
                                tool_buffers[tool_index] = {
                                    "id": "",
                                    "type": None,
                                    "name": "",
                                    "arguments": "",
                                }

                            # buffer 引用字典中的同一个对象。
                            # 修改它，就会更新 tool_buffers 中的数据。
                            buffer = tool_buffers[tool_index]

                            if tool_piece.id is not None:
                                buffer["id"] += tool_piece.id

                            if tool_piece.type is not None:
                                if tool_piece.type != "function":
                                    raise ModelResponseError(
                                        "模型返回了不支持的工具调用类型。"
                                    )

                                buffer["type"] = tool_piece.type

                            function_piece = tool_piece.function

                            if function_piece is not None:
                                if function_piece.name is not None:
                                    buffer["name"] += function_piece.name

                                if function_piece.arguments is not None:
                                    buffer["arguments"] += (
                                        function_piece.arguments
                                    )

                    # 先处理当前块的内容，再记录结束原因。
                    # 因为最后一个块也可能携带内容。
                    if choice.finish_reason is not None:
                        finish_reason = choice.finish_reason

        except openai.APITimeoutError as exc:
            raise ModelRequestError(
                "流式响应超时，本次回答可能尚未接收完整。",
                retryable=True,
            ) from exc

        except openai.APIConnectionError as exc:
            raise ModelRequestError(
                "流式连接失败或中断，本次回答可能尚未接收完整。",
                retryable=True,
            ) from exc

        except openai.APIStatusError as exc:
            status_code = exc.status_code

            retryable_status_codes = {
                408,
                429,
                500,
                502,
                503,
                504,
            }

            raise ModelRequestError(
                f"流式请求失败，HTTP 状态码：{status_code}。",
                retryable=status_code in retryable_status_codes,
            ) from exc

        except openai.APIError as exc:
            # 流中也可能收到服务端发来的错误事件。
            raise ModelRequestError(
                "模型服务在流式响应过程中返回了错误。"
            ) from exc

        # ---------- 检查响应是否完整 ----------

        if finish_reason is None:
            raise ModelResponseError(
                "流式响应没有正常结束，本轮工具不会执行。"
            )

        if finish_reason not in {"stop", "tool_calls"}:
            # 例如 length 表示回答因长度限制而被截断。
            raise ModelResponseError(
                f"流式响应未完整完成，结束原因：{finish_reason}。"
                "本轮工具不会执行。"
            )

        if tool_buffers and finish_reason != "tool_calls":
            raise ModelResponseError(
                "模型返回了工具调用，但结束原因不匹配。"
            )

        if finish_reason == "tool_calls" and not tool_buffers:
            raise ModelResponseError(
                "模型声明要调用工具，却没有返回工具调用内容。"
            )

        # ---------- 将拼接结果转换为完整消息 ----------

        complete_tool_calls = []

        # 按工具序号排列，保持调用顺序。
        for tool_index in sorted(tool_buffers):
            buffer = tool_buffers[tool_index]

            function = SimpleNamespace(
                name=buffer["name"],
                arguments=buffer["arguments"],
            )

            tool_call = SimpleNamespace(
                id=buffer["id"],
                type=buffer["type"],
                function=function,
            )

            complete_tool_calls.append(tool_call)

        complete_message = SimpleNamespace(
            content=content_text,
            reasoning_content=reasoning_text,
            tool_calls=complete_tool_calls,
        )

        # 复用现有解析：
        # 检查工具 ID、解析 JSON 参数、检查参数是否为字典等。
        response = self.parse_message(complete_message)

        # 所有工具调用校验成功后，才返回完成事件。
        yield ResponseCompletedEvent(
            response=response,
        )
