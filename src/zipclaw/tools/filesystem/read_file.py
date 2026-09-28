# src/zipclaw/tools/filesystem/read_file.py

from pathlib import Path

from pydantic import BaseModel, Field

from ..base import BaseTool


class ReadFileArgs(BaseModel):
    """
    read_file 工具需要的参数。
    """

    path: str = Field(
        description=(
            "文件的工作区相对路径，"
            "例如：src/main.py"
        )
    )


class ReadFileTool(BaseTool):
    """
    读取当前工作区中的文件。
    """

    name = "read_file"

    description = (
        "读取工作区内文本文件的内容，"
        "文件路径相对于工作区根目录。"
    )

    # 告诉 BaseTool：
    # 这个工具使用 ReadFileArgs 验证参数。
    args_model = ReadFileArgs

    def __init__(self, workspace: Path):
        """
        workspace 表示 Agent 当前允许操作的项目目录。

        比如：

        /Users/zhang/code/demo

        Agent 原则上只能访问这个目录里面的文件。
        """

        # resolve() 会转换为绝对路径，
        # 同时清理 ../ 等路径。
        self.workspace = workspace.resolve()

    async def execute(
        self,
        args: ReadFileArgs,
    ) -> str:
        """
        真正读取文件。
        """

        # 假设 workspace 是：
        #
        # /project/demo
        #
        # args.path 是：
        #
        # src/main.py
        #
        # 最终得到：
        #
        # /project/demo/src/main.py
        #/ 在 pathlib.Path 里是重载过的运算符，专门用来拼接路径
        #resolve() 就是把路径变成“最终完整、干净、真实”的路径。比如去掉原有路径的.
        # 比如你写：
        #
        # python
        # self.workspace / args.path
        # 只是把两个路径拼起来，可能长这样：
        # / home / user / project /../ data /./ file.txt
        # 这看起来乱糟糟的。调用.resolve()
        # 之后，它会帮你整理成：
        # / home / user / data / file.txt
        target = (
            self.workspace / args.path
        ).resolve()

        # 安全检查。
        #
        # 防止模型输入类似：
        #
        # ../../../../etc/passwd
        #
        # 从而访问 workspace 外部的文件。
        #最终要访问的这个文件，到底在不在我的工作目录（workspace）里面
        if not target.is_relative_to(self.workspace):
            raise ValueError(
                "不能访问工作区之外的文件。"
            )

        # 文件不存在。
        if not target.exists():
            return f"文件不存在： {args.path}"

        # 用户可能传入了一个目录，
        # read_file 只允许读取普通文件。
        if not target.is_file():
            return f"不是普通文件： {args.path}"

        # 读取文本内容,指定用什么编码解码
        #
        # errors="replace"：
        # 遇到不能正常解码的字符时不要直接崩溃。
        # newline="" 保留原始换行，让模型读到的多行文本能被 edit_file 精确匹配。
        # 只读工具保留替换解码错误的行为；编辑工具仍严格解码，避免损坏原文。
        with target.open("r", encoding="utf-8", errors="replace", newline="") as file:
            return file.read()
