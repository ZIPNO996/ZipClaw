# src/zipclaw/llm/base.py

from abc import ABC, abstractmethod

from .llm_types import LLMResponse,LLMStreamEvent,TextDeltaEvent,ReasoningDeltaEvent,ResponseCompletedEvent
from collections.abc import AsyncIterator

#ABC:抽象基类
#@abstractmethod 负责贴标签，ABC 负责让检查生效。
class BaseLLM(ABC):
    """
    所有模型服务适配器的统一接口。

    以后可以有：

    OpenAILLM(BaseLLM)
    DeepSeekLLM(BaseLLM)
    AnthropicLLM(BaseLLM)

    AgentLoop 只依赖 BaseLLM，
    不关心底层到底是哪一个模型。
    """


    #抽象方法
    #所有具体的 模型 都必须有 chat 这个方法（@abstractmethod 规定）
    #... 就是占位符，意思是：
    # 这里本来该有代码，但我先不写，留个位置。
    @abstractmethod
    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        """
        向模型发送消息。

        参数：
        messages:
            当前对话历史。

        tools:
            当前允许模型使用的工具参数结构（JSON Schema）。

        返回：
            统一的 LLMResponse。
        """

        ...

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[LLMStreamEvent]:
        """
        逐个返回模型响应事件。

        默认实现先等待 chat() 返回完整结果，
        再将结果转换成事件。

        具体模型适配器可以覆盖这个方法，
        实现真正的边接收、边返回。
        """

        # 复用当前已经实现的普通请求。
        response = await self.chat(
            messages=messages,
            tools=tools,
        )

        # 如果有推理文本，先返回推理事件。
        if response.reasoning_content:
            yield ReasoningDeltaEvent(
                text=response.reasoning_content,
            )

        # 如果有正文，再返回正文事件。
        if response.content:
            yield TextDeltaEvent(
                text=response.content,
            )

        # 最后返回完整响应。
        # 即使没有正文、只有工具调用，也需要这个事件。
        yield ResponseCompletedEvent(
            response=response,
        )
