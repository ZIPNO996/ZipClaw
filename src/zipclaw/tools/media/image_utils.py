"""图片输入的公共检查：限制路径、文件大小和像素，并生成接口需要的数据。"""

import base64
import io
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from ...error.custom_errors import ImageInputError


MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
SUPPORTED_FORMATS = {"PNG", "JPEG", "WEBP"}


def read_local_image(
    workspace: Path,
    path: str,
    full_access: bool = False,
) -> bytes:
    """读取本机参考图；工作区检查与其它文件工具保持一致。"""

    root = workspace.resolve()
    target = (root / path).resolve()
    if not full_access and not target.is_relative_to(root):
        raise ImageInputError("图片路径不能超出工作区。")
    if not target.is_file():
        raise ImageInputError("图片文件不存在，或指定路径不是文件。")

    # 不直接 read_bytes() 读取任意大小的文件。
    # 多读一个字节，才能区分“恰好达到上限”和“已经超过上限”。
    with target.open("rb") as file:
        image_bytes = file.read(MAX_IMAGE_BYTES + 1)
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ImageInputError("输入图片不能超过 10 MiB。")
    return image_bytes


def prepare_png(image_bytes: bytes, max_side: int | None = None) -> bytes:
    """检查实际格式，纠正照片方向，移除元数据，返回 PNG 字节。"""

    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ImageInputError("输入图片不能超过 10 MiB。")
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            if image.format not in SUPPORTED_FORMATS:
                raise ImageInputError("只支持 PNG、JPEG 和 WebP 图片。")
            width, height = image.size
            if width * height > MAX_IMAGE_PIXELS:
                raise ImageInputError("输入图片不能超过 1600 万像素。")
            if getattr(image, "is_animated", False):
                raise ImageInputError("暂不支持动画图片，请发送静态图片。")
            image.load()

            # 手机照片的方向可能记录在 EXIF 中，像素本身并没有旋转。
            # exif_transpose() 将这个方向落实到像素，避免模型收到横倒的照片。
            with ImageOps.exif_transpose(image) as oriented:
                if max_side is not None:
                    oriented.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
                has_alpha = "A" in oriented.getbands() or "transparency" in oriented.info
                mode = "RGBA" if has_alpha else "RGB"
                with oriented.convert(mode) as converted:
                    # 不保留 EXIF、文字块等元数据，避免附带位置或其它隐私字段。
                    converted.info.clear()
                    output = io.BytesIO()
                    converted.save(output, format="PNG")
                    png_bytes = output.getvalue()
    except ImageInputError:
        # 自己主动拒绝的输入，保留具体原因，例如文件格式或像素上限。
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise ImageInputError("文件内容不是可读取的静态图片，或图片已经损坏。") from None

    if len(png_bytes) > MAX_IMAGE_BYTES:
        raise ImageInputError("转换后的图片超过 10 MiB，请先缩小图片。")
    return png_bytes


def local_image_data_url(
    workspace: Path,
    path: str,
    full_access: bool = False,
    max_side: int | None = None,
) -> str:
    """在工具内部生成 Base64；调用参数、会话和工具结果只保留文件路径。"""

    image_bytes = read_local_image(workspace, path, full_access)
    png_bytes = prepare_png(image_bytes, max_side=max_side)
    encoded = base64.b64encode(png_bytes).decode("ascii")
    return "data:image/png;base64," + encoded
