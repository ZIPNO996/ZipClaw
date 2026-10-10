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

    offset: int = Field(
        default=1,
        ge=1,
        description="从第几行开始读取，行号从 1 开始，默认从第 1 行读取。",
    )

    limit: int = Field(
        default=200,
        ge=1,
        le=2000,
        description="本次最多读取多少行，默认 200 行，最多 2000 行。",
    )

    max_chars: int = Field(
        default=20000,
        ge=1,
        le=100000,
        description=(
            "本次最多返回多少个文件内容字符，默认 20000，最多 100000。"
            "通常返回完整行；如果第一行就超过限制，则只返回该行的部分内容。"
        ),
    )


class ReadFileTool(BaseTool):
    """
    读取当前工作区中的文件。
    """

    name = "read_file"

    description = (
        "读取工作区内文本文件的内容。"
        "文件路径相对于工作区根目录。"
        "可以通过 offset 指定起始行，通过 limit 限制读取行数。"
        "如果返回结果提示还有剩余内容，可以按照提示继续读取。"
    )

    # 告诉 BaseTool：
    # 这个工具使用 ReadFileArgs 验证参数。
    #参考base.py对此处的解释
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
        # 保存本次需要返回的文件内容。
        # 列表中的每个元素，都是从文件中读取的一行。
        # 保存准备返回的文件内容。
        selected_lines: list[str] = []

        # 已经收集的文件内容字符数。
        total_chars = 0

        # 工具附加的说明，不属于文件原文。
        notice = ""

        with target.open(
                "r",
                encoding="utf-8",
                errors="replace",
                newline="",
        ) as file:
            for line_number, line in enumerate(file, start=1):

                # 跳过起始行之前的内容。
                if line_number < args.offset:
                    continue

                # 情况一：已经达到行数限制。
                # 当前这一行还没有加入结果，下次应该从这一行开始。
                if len(selected_lines) >= args.limit:
                    notice = (
                        "已达到本次读取的行数上限，后面还有内容。"
                        f"请使用 offset={line_number} 继续读取。"
                    )
                    break

                # 计算加入当前这一行之后，总字符数会是多少。
                line_chars = len(line)
                new_total_chars = total_chars + line_chars

                # 情况二：加入当前这一行会超过字符限制。
                if new_total_chars > args.max_chars:

                    if selected_lines:
                        # 已经收集了一些完整行。
                        # 当前这一行先不加入，留给下一次读取，
                        # 这样就不会把这一行从中间截断。
                        notice = (
                            "加入下一行会超过本次读取的字符上限。"
                            f"请使用 offset={line_number} 继续读取。"
                        )

                    else:
                        # 第一条要读取的行，就已经超过字符上限。
                        # 此时只能返回这一行的前一部分。
                        partial_line = line[:args.max_chars]
                        selected_lines.append(partial_line)

                        # 这一行还没有读完整，不能让模型直接跳到下一行。
                        notice = (
                            f"第 {line_number} 行超过字符上限，"
                            f"本次仅展示该行的前 {args.max_chars} 个字符。"
                            "该行剩余内容未展示，文件后续内容也未检查。"
                            "可以增大 max_chars 后，从这一行重新读取；"
                            "如果仍超过参数允许的上限，本工具暂时无法完整展示该行。"
                        )

                    break

                # 情况三：行数和字符数都没有超限。
                # 将当前这一行完整加入结果，并更新字符总数。
                selected_lines.append(line)
                total_chars = new_total_chars

        # 保留之前的起始行越界判断。
        if not selected_lines and args.offset > 1:
            return f"起始行 {args.offset} 超过文件范围。"

        # 原来的换行符已经保存在每一行中，不需要再添加。
        content = "".join(selected_lines)

        # 只有存在额外说明时，才在文件内容后附加提示。
        if notice:
            content += (
                    "\n\n[读取提示："
                    + notice
                    + " 此提示不属于文件原文。]"
            )

        return content