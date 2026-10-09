# src/zipclaw/llm/openai_llm.py

import json
import openai
from openai import AsyncOpenAI

from .base import BaseLLM
from .types import LLMResponse, ToolCall
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

            # 本阶段不自动重试。
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

        # 请求 模型。
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools,
            )

        except openai.APITimeoutError as exc:
            # 超时异常也是连接异常的一种。
            # 因此必须先捕获它，才能给出单独的超时提示。
            raise ModelRequestError(
                "模型请求超时。请检查网络，或稍后手动重试。"
            ) from exc

        except openai.APIConnectionError as exc:
            raise ModelRequestError(
                "无法连接模型服务。请检查网络、代理和服务地址。"
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

            # 不直接把服务端返回的完整错误内容打印给用户。
            # 自定义异常携带中文提示，原始异常保留为排查线索。
            raise ModelRequestError(message) from exc

        # 第一版只处理响应中的第一个候选回答（choice）。
        # 没有候选回复时，不能直接访问 choices[0]。
        if not response.choices:
            raise ModelResponseError(
                "模型没有返回候选回复，本次任务已停止。"
            )

        message = response.choices[0].message

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
