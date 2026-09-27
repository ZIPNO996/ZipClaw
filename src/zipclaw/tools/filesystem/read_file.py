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
            "Relative path of the file to read, "
            "for example: src/main.py"
        )
    )


class ReadFileTool(BaseTool):
    """
    读取当前 Workspace 中的文件。
    """

    name = "read_file"

    description = (
        "Read the contents of a text file "
        "inside the current workspace."
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
                "Cannot access files outside workspace."
            )

        # 文件不存在。
        if not target.exists():
            return f"File not found: {args.path}"

        # 用户可能传入了一个目录，
        # read_file 只允许读取普通文件。
        if not target.is_file():
            return f"Not a file: {args.path}"

        # 读取文本内容,指定用什么编码解码
        #
        # errors="replace"：
        # 遇到不能正常解码的字符时不要直接崩溃。
        return target.read_text(
            encoding="utf-8",
            errors="replace",
        )