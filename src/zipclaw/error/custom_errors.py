
class AgentStepLimitError(RuntimeError):
    """当前任务已达到最大执行步数。"""


class ModelRequestError(RuntimeError):
    """模型请求失败，同时记录这个错误是否适合重试。"""

    def __init__(
        self,
        message: str,
        retryable: bool = False,
    ):
        # 交给父类保存错误说明。
        # 这样 print(exc)、str(exc) 仍然能得到提示文字。
        super().__init__(message)

        # True 表示调用方可以进行有限重试。
        # 默认 False，避免所有错误都被自动重试。
        self.retryable = retryable


class ModelResponseError(RuntimeError):
    """模型返回内容无法使用，例如工具参数不是合法 JSON。"""