# src/zipclaw/llm/base.py

from abc import ABC, abstractmethod

from .types import LLMResponse

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
