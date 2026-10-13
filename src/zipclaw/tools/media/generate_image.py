"""ModelScope 生图工具：提交任务、异步轮询、下载并保存一张 PNG。"""

import asyncio
import io
import ipaddress
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field, field_validator, model_validator

from ...error.custom_errors import ImageGenerationError, ImageInputError
from ..base import BaseTool
from .image_utils import local_image_data_url


class GenerateImageArgs(BaseModel):
    """模型只决定图片描述、输出路径和可选参考图，不决定密钥或接口。"""

    prompt: str = Field(
        min_length=1,
        max_length=2000,
        description="图片生成或修改要求，最多 2000 个字符。不要包含密钥或私密信息。",
    )
    path: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="新 PNG 文件的工作区相对路径；省略时自动生成唯一文件名，不覆盖已有文件。",
    )
    image_url: list[str] = Field(
        default_factory=list,
        max_length=2,
        description="可选的公开 HTTPS 参考图链接；与 image_paths 合计最多两张，不要传入 Base64。",
    )
    image_paths: list[str] = Field(
        default_factory=list,
        max_length=2,
        description="可选的本机参考图片路径，相对于工作区；微信收到的图片会提供路径，与 image_url 合计最多两张。",
    )

    @field_validator("image_paths")
    @classmethod
    def check_reference_paths(cls, values: list[str]) -> list[str]:
        paths = []
        for value in values:
            path = value.strip()
            if not path or len(path) > 500:
                raise ValueError("参考图路径不能为空，也不能超过 500 个字符。")
            paths.append(path)
        return paths

    @model_validator(mode="after")
    def check_reference_count(self):
        if len(self.image_url) + len(self.image_paths) > 2:
            raise ValueError("本机参考图与参考图链接合计最多两张。")
        return self

    @field_validator("prompt", "path")
    @classmethod
    def check_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("图片描述或路径不能只有空白。")
        return value

    @field_validator("image_url")
    @classmethod
    def check_reference_urls(cls, values: list[str]) -> list[str]:
        urls = []
        for value in values:
            url = value.strip()
            if len(url) > 4096:
                raise ValueError("参考图链接不能超过 4096 个字符。")
            try:
                parts = urlsplit(url)
                host = parts.hostname
                if parts.scheme != "https" or not host or parts.port not in {None, 443}:
                    raise ValueError
                if parts.username is not None or parts.password is not None:
                    raise ValueError
                if host == "localhost" or host.endswith(".local"):
                    raise ValueError
            except ValueError:
                raise ValueError("参考图必须使用不含认证信息的公开 HTTPS 链接。") from None
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                address = None
            if address is not None and not address.is_global:
                raise ValueError("参考图不能使用本机或内网 IP 地址。")
            # 我们不下载参考图；这些链接会交给 ModelScope 服务读取。
            urls.append(url)
        return urls


class GenerateImageTool(BaseTool):
    """调用 ModelScope 的异步图片接口，不在工作区生成辅助代码。"""

    name = "generate_image"
    description = (
        "通过 ModelScope 生成一张图片，或根据本机参考图、公开参考图链接修改图片，保存为 PNG。"
        "只生成图片，不创建 Python 脚本、README 或依赖文件。"
        "省略 path 时使用工作区内的唯一文件名；已有文件一律不覆盖。"
        "返回实际保存的路径；通过微信发图时，成功后还需调用 send_image。"
        "本机参考图使用 image_paths，链接使用 image_url；程序负责编码，不要用工具生成 Base64。"
        "会发送图片描述和参考图片，使用 ModelScope 的额度；失败时不要自动重新提交。"
        "本地取消或超时不代表远端任务已经取消，不能声称额度已退回。"
    )
    args_model = GenerateImageArgs
    API_URL = "https://api-inference.modelscope.cn/v1"
    TOTAL_TIMEOUT_SECONDS = 180
    REQUEST_TIMEOUT_SECONDS = 30
    POLL_INTERVAL_SECONDS = 5
    MAX_JSON_BYTES = 1024 * 1024
    MAX_IMAGE_BYTES = 10 * 1024 * 1024
    MAX_IMAGE_PIXELS = 16_000_000
    # 下载地址来自服务端响应，但仍需限制域名，不能据此访问本机或任意服务。
    IMAGE_HOST_SUFFIXES = ("modelscope.cn", "aliyuncs.com")

    def __init__(
        self,
        workspace: Path,
        api_key: str,
        model: str,
        full_access: bool = False,
    ):
        if not api_key.strip() or not model.strip():
            raise ValueError("图片生成需要配置 ModelScope 密钥和模型名称。")
        self.workspace = workspace.resolve()
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.full_access = full_access

    def error_result(self, message: str, task_id: str | None) -> str:
        result = {
            "status": "error",
            "message": message,
            "task_id": task_id,
            "notice": "不要自动重新提交生图任务。已提交的远端任务可能仍在执行或已消耗额度。",
        }
        return json.dumps(result, ensure_ascii=False)

    def prepare_target(self, path: str | None) -> Path:
        # 先检查输出路径，再发送生图请求，避免为无效路径消耗额度。
        if path is None:
            path = f"generated_{uuid4().hex}.png"
        original = self.workspace / path
        if original.is_symlink():
            raise ImageGenerationError("不能把图片写入已有的符号链接。")
        target = original.resolve()
        if not self.full_access and not target.is_relative_to(self.workspace):
            raise ImageGenerationError("图片输出路径不能超出工作区。")
        if target.suffix.lower() != ".png":
            raise ImageGenerationError("图片输出路径必须以 .png 结尾。")
        if target.exists():
            raise ImageGenerationError("图片输出路径已经存在，请使用新文件名。")
        return target

    def check_download_url(self, url: object) -> str:
        if not isinstance(url, str) or len(url) > 4096:
            raise ImageGenerationError("服务没有返回有效的图片下载链接。")
        url = url.strip()
        if any(ord(character) < 32 for character in url):
            raise ImageGenerationError("图片下载链接包含无效控制字符。")
        try:
            parts = urlsplit(url)
            host = parts.hostname
            if parts.scheme != "https" or not host or parts.port not in {None, 443}:
                raise ValueError
            if parts.username is not None or parts.password is not None:
                raise ValueError
            allowed = False
            for suffix in self.IMAGE_HOST_SUFFIXES:
                if host == suffix or host.endswith("." + suffix):
                    allowed = True
                    break
            if not allowed:
                raise ValueError
        except ValueError:
            raise ImageGenerationError("图片下载只允许 ModelScope 或阿里云域名的 HTTPS 链接。") from None
        return url

    def check_http_status(self, status_code: int) -> None:
        if 200 <= status_code < 300:
            return
        if status_code in {401, 403}:
            message = "ModelScope 认证或权限不足，请检查 MODELSCOPE_API_KEY 和模型使用权限。"
        elif status_code == 429:
            message = "ModelScope 请求频率或额度达到上限，请检查其控制台。"
        elif status_code in {400, 422}:
            message = "ModelScope 拒绝了请求，请检查模型、图片描述和参考图参数。"
        elif status_code >= 500:
            message = "ModelScope 服务暂时不可用，请稍后手动再试。"
        else:
            message = f"生图服务返回异常状态码：{status_code}。"
        # 不展示服务端原始正文，避免把认证信息或内部错误发送给模型。
        raise ImageGenerationError(message)

    async def read_bytes(self, response: httpx.Response, limit: int) -> bytes:
        data = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=65536):
            if len(data) + len(chunk) > limit:
                raise ImageGenerationError("服务响应超过工具允许的大小，已停止接收。")
            data.extend(chunk)
        return bytes(data)

    async def request_json(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        headers: dict,
        body: dict | None = None,
    ) -> dict:
        async with client.stream(method, url, headers=headers, json=body) as response:
            self.check_http_status(response.status_code)
            data_bytes = await self.read_bytes(response, self.MAX_JSON_BYTES)
        try:
            data = json.loads(data_bytes)
        except ValueError:
            raise ImageGenerationError("ModelScope 没有返回可解析的 JSON。") from None
        if not isinstance(data, dict):
            raise ImageGenerationError("ModelScope 响应不是 JSON 对象。")
        return data

    async def wait_for_image(self, client: httpx.AsyncClient, task_id: str) -> str:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-ModelScope-Task-Type": "image_generation",
        }
        # 对编号做 URL 编码，不能把服务返回的编号直接当成路径片段。
        task_url = f"{self.API_URL}/tasks/{quote(task_id, safe='')}"
        while True:
            data = await self.request_json(client, "GET", task_url, headers)
            status = data.get("task_status")
            if status == "SUCCEED":
                images = data.get("output_images")
                if not isinstance(images, list) or not images:
                    raise ImageGenerationError("任务成功，但服务没有返回图片链接。")
                return self.check_download_url(images[0])
            if status in {"FAILED", "CANCELED", "CANCELLED"}:
                raise ImageGenerationError("ModelScope 生图任务失败或已被服务端取消。")
            if not isinstance(status, str) or not status:
                raise ImageGenerationError("服务没有返回有效的任务状态。")
            # 对其它状态继续等待，但外层有总超时，不会无限轮询。
            # 异步 sleep 不阻塞微信接收消息，也可以被 /cancel 中断。
            await asyncio.sleep(self.POLL_INTERVAL_SECONDS)

    async def download_image(self, client: httpx.AsyncClient, url: str) -> bytes:
        # 不传 Authorization，也不跟随重定向，不能把 ModelScope Token 发给图片 CDN。
        async with client.stream("GET", url) as response:
            self.check_http_status(response.status_code)
            return await self.read_bytes(response, self.MAX_IMAGE_BYTES)

    def encode_png(self, image_bytes: bytes) -> tuple[bytes, int, int]:
        """检查实际图片内容，再转换为微信能够发送的 PNG。"""

        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                width, height = image.size
                if width * height > self.MAX_IMAGE_PIXELS:
                    raise ImageGenerationError("图片像素数量过大，已拒绝转换。")
                image.load()
                # 使用 RGB/RGBA，让 JPEG、WebP 等响应也统一保存成 PNG。
                mode = "RGBA" if "A" in image.getbands() else "RGB"
                with image.convert(mode) as converted:
                    output = io.BytesIO()
                    converted.save(output, format="PNG")
                    png_bytes = output.getvalue()
        except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError):
            raise ImageGenerationError("下载结果不是可读取的图片。") from None
        if len(png_bytes) > self.MAX_IMAGE_BYTES:
            raise ImageGenerationError("转换后的 PNG 超过 10 MiB，请调整图片要求。")
        return png_bytes, width, height

    def save_image(self, target: Path, png_bytes: bytes) -> None:
        """先写完临时文件，再以不覆盖的方式发布最终图片。"""

        # 再次解析路径，防止等待期间父目录的链接被改变。
        current = target.resolve()
        if current != target:
            raise ImageGenerationError("等待生图期间输出路径发生变化，已停止保存。")
        if not self.full_access and not current.is_relative_to(self.workspace):
            raise ImageGenerationError("图片输出路径已经超出工作区。")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=target.parent,
                prefix=".zipclaw-image-",
                suffix=".tmp",
                delete=False,
            ) as file:
                temporary_path = Path(file.name)
                file.write(png_bytes)
                file.flush()
            # 硬链接只增加一个文件名，不复制数据；目标存在时直接失败。
            # 因而不会覆盖等待期间其他程序新建的文件，也不会暴露写到一半的图片。
            # 本机桌面的 NTFS 支持该操作，不需要创建符号链接的管理员权限。
            os.link(temporary_path, target)
        finally:
            if temporary_path is not None:
                # 只清理本工具创建的临时文件，不删除最终图片或用户原有文件。
                temporary_path.unlink(missing_ok=True)

    async def execute(self, args: GenerateImageArgs) -> str:
        task_id = None
        try:
            target = self.prepare_target(args.path)
            body = {
                "model": self.model,
                "prompt": args.prompt,
            }
            # 在提交付费任务之前检查全部参考图，不能先提交再发现输入无效。
            # Base64 只存在于请求体中，工具参数、结果和聊天记录均不包含它。
            references = list(args.image_url)
            for path in args.image_paths:
                data_url = await asyncio.to_thread(
                    local_image_data_url,
                    self.workspace,
                    path,
                    self.full_access,
                )
                references.append(data_url)
            if references:
                body["image_url"] = references
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "X-ModelScope-Async-Mode": "true",
            }

            # 总超时覆盖提交、轮询、下载和转换；HTTPX 另有限制每次网络等待。
            async with asyncio.timeout(self.TOTAL_TIMEOUT_SECONDS):
                async with httpx.AsyncClient(
                    timeout=self.REQUEST_TIMEOUT_SECONDS,
                    follow_redirects=False,
                ) as client:
                    data = await self.request_json(
                        client=client,
                        method="POST",
                        url=f"{self.API_URL}/images/generations",
                        headers=headers,
                        body=body,
                    )
                    task_id = data.get("task_id")
                    if not isinstance(task_id, str) or not task_id or len(task_id) > 200:
                        task_id = None
                        raise ImageGenerationError("服务没有返回有效的生图任务编号，请勿自动重新提交。")
                    if self.api_key in task_id:
                        task_id = None
                        raise ImageGenerationError("服务返回了异常任务编号，已停止处理。")
                    print(f"[生图] 已提交 ModelScope 任务：{task_id}", flush=True)
                    image_url = await self.wait_for_image(client, task_id)
                    image_bytes = await self.download_image(client, image_url)
                    # 线程里只进行图片转换，不写文件；取消后不会偷偷创建产物。
                    png_bytes, width, height = await asyncio.to_thread(self.encode_png, image_bytes)

            # 最后才保存，并且这段不包含 await，避免取消后后台线程仍在写文件。
            self.save_image(target, png_bytes)
        except (ImageGenerationError, ImageInputError) as exc:
            return self.error_result(str(exc), task_id)
        except (httpx.TimeoutException, TimeoutError):
            return self.error_result("生图等待超时。远端任务可能仍在执行，请勿自动重新提交。", task_id)
        except httpx.RequestError:
            return self.error_result("无法连接生图服务或下载图片，请检查网络。", task_id)
        except FileExistsError:
            return self.error_result("图片输出路径已被其它程序创建，未覆盖该文件。", task_id)
        except OSError:
            return self.error_result("图片保存失败，请检查目录权限和文件系统的硬链接支持。", task_id)
        # 不捕获 CancelledError：让聊天入口记录中断。此操作不取消远端任务。

        if target.is_relative_to(self.workspace):
            display_path = target.relative_to(self.workspace).as_posix()
        else:
            display_path = str(target)
        result = {
            "status": "success",
            "path": display_path,
            "task_id": task_id,
            "model": self.model,
            "width": width,
            "height": height,
            "notice": "图片已保存。微信入口请调用 send_image 发送该路径；本工具没有生成辅助代码文件。",
        }
        return json.dumps(result, ensure_ascii=False)
