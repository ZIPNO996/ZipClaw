
class AgentStepLimitError(RuntimeError):
    """当前任务已达到最大执行步数。"""


class ModelRequestError(RuntimeError):
    """模型请求失败，例如连接失败、超时或认证失败。"""


class ModelResponseError(RuntimeError):
    """模型返回内容无法使用，例如工具参数不是合法 JSON。"""