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
            for call in response.tool_calls:

                # 真正执行工具。
                #
                # 例如：
                #
                # read_file(
                #     path="calculator.py"
                # )
                result = await self.tools.execute(
                    name=call.name,
                    arguments=call.arguments,
                )

                # 记录 assistant 发起了什么工具调用。
                #
                # 注意：
                # 这里第一版只是表达我们的内部逻辑。
                #
                # 后续可以进一步设计自己的 Message Model，
                # 再由 Provider Adapter 转换为 OpenAI 消息格式。
                #ensure_ascii=False	保留原字符，中文就是中文，emoji 就是 emoji
                messages.append(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                                },
                            }
                        ],
                    }
                )

                # 把 Tool 的执行结果告诉模型。
                #
                # 比如 read_file 得到：
                #
                # def add(a, b):
                #     return a + b
                #
                # 模型下一轮就能看到这些内容。
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": str(result),
                    }
                )

            # 执行完 Tool 后不会 return。
            #
            # 会回到 for 循环顶部，
            # 再次调用：
            #
            # self.llm.chat(...)
            #
            # 于是模型可以根据工具结果继续思考。

        # 如果循环次数超过限制，
        # 很可能 Agent 出现死循环。
        raise RuntimeError(
            "Maximum agent steps reached."
        )