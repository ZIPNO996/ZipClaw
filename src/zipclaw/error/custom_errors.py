
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
        # 默认 False，不建议调用方重试；标记本身不会自动发送请求。
        # 当前只有摘要请求使用这个标记决定是否重试。
        self.retryable = retryable


class ModelResponseError(RuntimeError):
    """模型返回内容无法使用，例如工具参数不是合法 JSON。"""


class ImageGenerationError(Exception):
    """生图工具的业务错误，不是聊天模型请求失败。"""

    # 图片工具在遇到无效路径、任务失败或无效图片时抛出这个异常。
    # execute() 会捕获它并返回工具失败结果，让模型知道本次没有成功。
    # 传入的错误说明不包含原始 HTTP 正文或认证信息。


class ImageInputError(ValueError):
    """图片输入无效，例如越界路径、过大文件或损坏的图片。"""

    # 微信接收、图片分析和参考图编辑共用这个异常。
    # 调用方负责把它转换成简短中文提示，不发送原始图片或 Base64。
