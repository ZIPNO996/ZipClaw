"""微信专用工具：把本机图片发送给当前扫码用户。"""

import asyncio
import io
from collections.abc import Awaitable, Callable
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field

from ...tools.base import BaseTool
from .client import WechatAPIError


class SendImageArgs(BaseModel):
    path: str = Field(
        min_length=1,
        description="本机 PNG 或 JPEG 图片路径，默认使用工作区相对路径。",
    )


class SendImageTool(BaseTool):
    name = "send_image"
    description = (
        "把已经存在的本机 PNG 或 JPEG 图片作为原生微信图片发送给当前用户。"
        "需要把生成的图片发到用户手机时调用此工具，不能只返回本机路径或 Markdown。"
        "不生成图片；发送前先确认文件存在。"
    )
    args_model = SendImageArgs
    MAX_IMAGE_BYTES = 10 * 1024 * 1024

    def __init__(
        self,
        workspace: Path,
        sender: Callable[[bytes], Awaitable[None]],
        full_access: bool = False,
    ):
        self.workspace = workspace.resolve()
        self.sender = sender
        self.full_access = full_access

    def read_image(self, path: str) -> bytes:
        target = (self.workspace / path).resolve()
        if not self.full_access and not target.is_relative_to(self.workspace):
            raise ValueError("不能发送工作区之外的图片。")
        if not target.is_file():
            raise ValueError("图片文件不存在，或不是普通文件。")

        # 读取上限加一字节，发现超限后拒绝，不先无限读取整个文件。
        with target.open("rb") as file:
            data = file.read(self.MAX_IMAGE_BYTES + 1)
        if len(data) > self.MAX_IMAGE_BYTES:
            raise ValueError("图片超过 10 MiB，请先缩小图片。")

        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {"PNG", "JPEG"}:
                    raise ValueError("当前只发送 PNG 或 JPEG，请先转换图片格式。")
                image.verify()
        except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError):
            raise ValueError("文件不是可读取的 PNG 或 JPEG 图片。") from None
        return data

    async def execute(self, args: SendImageArgs) -> str:
        # 图片读取和验证放到线程里，微信仍可及时接收取消指令。
        data = await asyncio.to_thread(self.read_image, args.path)
        try:
            await self.sender(data)
        except WechatAPIError as exc:
            # 不自动重复上传或发送；把实际失败反馈给模型。
            return f"图片发送失败：{exc} 请说明失败，不要声称图片已送达。"
        return "图片消息已提交微信接口，请用户在手机查看。"
