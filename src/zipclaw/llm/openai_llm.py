# src/zipclaw/llm/openai_llm.py

import json

from openai import AsyncOpenAI

from .base import BaseLLM
from .types import LLMResponse, ToolCall


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
        )

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> LLMResponse:

        # 请求 模型。
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
        )

        # 第一版只处理响应中的第一个候选回答（choice）。
        message = response.choices[0].message

        tool_calls: list[ToolCall] = []

        # 如果模型决定调用工具，
        # SDK 会返回 message.tool_calls。
        if message.tool_calls:
            for call in message.tool_calls:

                # OpenAI 的 function.arguments 通常是 JSON 字符串，
                # 所以需要 json.loads。
                arguments = json.loads(
                    call.function.arguments
                )

                # 转换成 ZipClaw 自己的 ToolCall。
                tool_calls.append(
                    ToolCall(
                        id=call.id,
                        name=call.function.name,
                        arguments=arguments,
                    )
                )

        # 模型服务的响应转换到这里完成。
        #
        # 外面的 AgentLoop 只会看到 LLMResponse，
        # 不会知道这是 OpenAI SDK 返回的。
        return LLMResponse(
            content=message.content,
            # DeepSeek 思考模式下的工具调用要求回传原始推理内容。
            reasoning_content=getattr(message, "reasoning_content", None),
            tool_calls=tool_calls,
        )
