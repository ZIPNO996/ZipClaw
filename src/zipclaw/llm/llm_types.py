# src/zipclaw/llm/llm_types.py
# 模型适配层的数据结构

from typing import Any,Literal,TypeAlias
from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    """
    表示模型发起的一次工具调用。

    例如模型可能返回：
    read_file(path="main.py")

    我们会统一转换成 ToolCall，
    避免 AgentLoop 直接依赖 OpenAI / DeepSeek 的返回格式。
    """

    # 一次工具调用的唯一 ID。
    # 后面把工具执行结果返回给 模型 时需要用到。
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
    ZipClaw 内部统一的模型返回格式。

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


#流式输出

class TextDeltaEvent(BaseModel):
    """模型新返回的一小段正文，用于即时显示。"""

    # 固定的事件类型，接收方可以通过它判断如何处理。
    #Literal 就是“字面量类型”，用来把类型限制为几个具体的值
    type: Literal["text_delta"] = "text_delta"

    # 这里只保存本次新收到的片段，不是全部正文。
    text: str


class ReasoningDeltaEvent(BaseModel):
    """模型服务新返回的一小段推理文本。"""

    type: Literal["reasoning_delta"] = "reasoning_delta"

    # 是否显示这个片段，由核心循环中的显示开关决定。
    text: str


class ResponseCompletedEvent(BaseModel):
    """本次模型响应已经完整接收，并且通过解析和校验。"""

    type: Literal["response_completed"] = "response_completed"

    # 复用已有的完整响应结构。
    # 包含完整正文、推理字段，以及解析后的全部工具调用。
    response: LLMResponse


# 给这三种事件起一个共同的类型名称。
# “|”表示：一个事件可以是其中任意一种类型。
# LLMStreamEvent = (
#     TextDeltaEvent
#     | ReasoningDeltaEvent
#     | ResponseCompletedEvent
# )
# 明确告诉类型检查器：这是一个类型的别名，不是普通变量。
LLMStreamEvent: TypeAlias = (
    TextDeltaEvent
    | ReasoningDeltaEvent
    | ResponseCompletedEvent
)
