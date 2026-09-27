#Adapter 层

from typing import Any

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    """
    表示 LLM 发起的一次工具调用。

    例如模型可能返回：
    read_file(path="main.py")

    我们会统一转换成 ToolCall，
    避免 AgentLoop 直接依赖 OpenAI / DeepSeek 的返回格式。
    """

    # 一次工具调用的唯一 ID。
    # 后面把工具执行结果返回给 LLM 时需要用到。
    id: str

    # 工具名称，例如：
    # read_file
    # grep
    # bash
    name: str

    # 调用工具时传入的参数。
    #
    # 例如：
    # {
    #     "path": "main.py"
    # }
    arguments: dict[str, Any]


class LLMResponse(BaseModel):
    """
    ZipClaw 内部统一的 LLM 返回格式。

    无论底层使用 OpenAI、DeepSeek 还是 Claude，
    最终都转换成这个结构。
    """

    # 如果模型直接回答用户，这里会有文本。
    #
    # 如果模型决定调用工具，
    # content 很可能是 None。
    content: str | None = None

    # 保存模型返回的推理字段，供后续请求原样回传。
    reasoning_content: str | None = None

    # 模型要求执行的工具。
    #
    # 使用 default_factory，
    # 避免把 [] 作为共享默认值。
    tool_calls: list[ToolCall] = Field(default_factory=list)


    #@property 把方法变成只读属性，调用时不用加括号，内部可以动态计算，对外保持简洁、安全的接口。
    @property
    def has_tool_calls(self) -> bool:
        """
        判断模型是否请求调用工具。
        """
        return bool(self.tool_calls)