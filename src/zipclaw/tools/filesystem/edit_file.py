# src/zipclaw/tools/filesystem/edit_file.py

from pydantic import BaseModel,Field

from pathlib import Path

from ..base import BaseTool

class EditFileArgs(BaseModel):
    """
    edit_file 工具需要的参数。
    """

    #path: str = Field(...)表示必填，无默认值
    path: str = Field(
        min_length=1,
        description="Relative path of the new file, for example: src/main.py.",
    )
    old_text: str = Field(
        description=(
            "Exact text to replace, including whitespace and line endings. "
            "Must occur exactly once. Include surrounding context when needed."
        )
    )
    new_text: str = Field(description="Replacement text. May be empty to delete old_text.")

class EditFileTool(BaseTool):
    """
    编辑当前 Workspace 中的文件。
    """
    name = "edit_file"

    description = """
    "Edit an existing UTF-8 text file inside the workspace "
    "by replacing one exact occurrence of old_text with new_text. "
    "Use a path relative to the workspace root. "
    "Read the file before editing and preserve exact whitespace and line endings. "
    "old_text must be non-empty and match exactly once; "
    "include surrounding context if needed to make the match unique. "
    "new_text may be empty to delete the matched text. "
    "Refuse to edit if the file is missing or the match is not unique."
    """

    args_model = EditFileArgs

    def __init__(self,workspace:Path):
        self.workspace = workspace.resolve()

    async def execute(self,args:EditFileArgs)->str:
        """
        真正编辑文件。
        """

        #合并路径
        target = (
                self.workspace / args.path
        ).resolve()

        if not target.is_relative_to(self.workspace):
            raise ValueError(
                "Cannot access files outside workspace."
            )

        # 文件不存在。
        if not target.exists():
            return f"File not found: {args.path}"

        # 用户可能传入了一个目录，
        # edit_file 只允许读取普通文件。
        if not target.is_file():
            return f"Not a file: {args.path}"

        # #读取要编辑文件的完整文件
        #
        # #创建读取工具
        # reader = ReadFileTool(self.workspace)
        #
        # #读取文件参数:Pydantic v2 之后，模型只能通过关键字参数构造
        # read_args = ReadFileArgs(path=args.path)
        # #读取文件内容
        # read = reader.execute(read_args)

        #直接读取文件
        with target.open("r", encoding="utf-8", newline="") as file:
            original = file.read()

        if not args.old_text:
            return "拒绝修改：old_text 不能为空。"

        count = original.count(args.old_text)
        if count == 0:
            return "拒绝：旧文本不存在，建议重新读取文件"
        elif count > 1:
            return "拒绝：位置不明确，要求提供更多上下文"

        #count==1

        updated = original.replace(
            args.old_text,
            args.new_text,
            1,
        )

        # 新旧内容相同就不必写入。
        if updated == original:
            return f"No changes needed: {args.path}"

        # w 表示写入模式：会清空原文件，再写入 updated。
        # newline="" 不自动转换换行符。
        # with 结束后会自动关闭文件。
        with target.open("w", encoding="utf-8", newline="") as file:
            file.write(updated)

        return f"Edited {args.path}: replaced one occurrence."


