# src/zipclaw/core/loop.py

import json
from ..llm.base import BaseLLM
from ..tools.registry import ToolRegistry


class AgentLoop:
    """
    ZipClaw 最核心的 Agent 循环。

    基本逻辑：

    LLM
     ↓
    要不要调用 Tool？
     ↓
    如果调用 → 执行 Tool
     ↓
    把 Tool Result 给 LLM
     ↓
    再让 LLM 判断下一步
    """

    def __init__(
        self,
        llm: BaseLLM,
        tools: ToolRegistry,
        max_steps: int = 10,
    ):
        # 使用哪个 LLM。
        self.llm = llm

        # Agent 有哪些工具。
        self.tools = tools

        # 最大循环次数。
        #
        # 防止模型进入死循环，
        # 一直调用工具停不下来。
        self.max_steps = max_steps

    async def run(
        self,
        task: str,
    ) -> str:
        """
        执行一个用户任务。
        """

        # messages 就是整个 Agent 当前的上下文。
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a coding agent. "
                    "Use tools when needed to inspect "
                    "the user's project."
                ),
            },
            {
                "role": "user",
                "content": task,
            },
        ]

        # Agent Loop。
        #
        # 每循环一次，可以认为 Agent 做了一步。
        for _ in range(self.max_steps):

            # 把当前消息和 Tool Schema 全部交给 LLM。
            response = await self.llm.chat(
                messages=messages,
                tools=self.tools.schemas(),
            )

            # ==========================
            # 情况 1：LLM 不调用工具
            # ==========================
            #
            # 说明模型认为：
            #
            # "现在信息够了，我可以直接回答用户。"
            if not response.has_tool_calls:
                return response.content or ""

            # ==========================
            # 情况 2：LLM 请求调用 Tool
            # ==========================
            # 一次模型响应对应一条 assistant 消息，
            # 其中可能包含多个工具调用。
            assistant_message = {
                "role": "assistant",
                "content": response.content,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(
                                call.arguments,
                                ensure_ascii=False,
                            ),
                        },
                    }
                    for call in response.tool_calls
                ],
            }

            # 原样保留模型返回的字段，不自己编写或拼接内容。
            if response.reasoning_content is not None:
                assistant_message["reasoning_content"] = response.reasoning_content

            messages.append(assistant_message)

            # 再逐个执行工具，将结果与调用 ID 对应起来。
            for call in response.tool_calls:
                result = await self.tools.execute(
                    name=call.name,
                    arguments=call.arguments,
                )

                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": str(result),
                })

        # 如果循环次数超过限制，
        # 很可能 Agent 出现死循环。
        raise RuntimeError(
            "Maximum agent steps reached."
        )