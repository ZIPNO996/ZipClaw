# src/zipclaw/tools/filesystem/list_dir.py

from pathlib import Path

from pydantic import BaseModel, Field

from ..base import BaseTool


class ListDirArgs(BaseModel):
    """list_dir 的参数，由 BaseTool.run() 自动验证。"""

    # "." 表示工作区根目录；模型不传 path 也可以使用这个工具。
    path: str = Field(
        default=".",
        description="Directory path relative to the workspace root, for example: src. Defaults to .",
    )

    # ge / le 表示最小值和最大值，限制返回内容占用的上下文。
    max_entries: int = Field(
        default=100,
        ge=1,
        le=500,
        description="Maximum number of entries to return, between 1 and 500.",
    )


class ListDirTool(BaseTool):
    """列出工作区内目录的直接子项，不递归，也不读取文件内容。"""

    name = "list_dir"
    description = (
        "List immediate files and subdirectories inside a workspace directory. "
        "Does not recurse or read file contents. "
        "Input and output paths are relative to the workspace root."
    )
    args_model = ListDirArgs

    def __init__(self, workspace: Path):
        # 与 read_file 使用同一个工作区，两个工具才能配合使用。
        self.workspace = workspace.resolve()

    async def execute(self, args: ListDirArgs) -> str:
        # 例如工作区是 /project，path 是 src，则目标是 /project/src。
        # resolve() 清理 ../ 并解析链接，再按实际路径检查边界。
        target = (self.workspace / args.path).resolve()
        if not target.is_relative_to(self.workspace):
            raise ValueError("Cannot access directories outside workspace.")

        if not target.exists():
            return f"Directory not found: {args.path}"
        if not target.is_dir():
            return f"Not a directory: {args.path}"

        # iterdir() 只列出当前一层，不会展开子目录。
        # 排序让输出稳定；这里只限制返回数量，排序仍会收集全部子项。
        entries = sorted(
            target.iterdir(),
            key=lambda entry: (entry.name.casefold(), entry.name),
        )
        if not entries:
            return f"Empty directory: {args.path}"

        lines = []
        for entry in entries[:args.max_entries]:
            # 单独标记符号链接和 Windows 目录联接，不读取链接目标内容。
            if entry.is_symlink() or entry.is_junction():
                kind = "link"
            elif entry.is_dir():
                kind = "dir"
            elif entry.is_file():
                kind = "file"
            else:
                kind = "other"

            # 返回 src/main.py，而非仅 main.py，模型可直接拿去调用 read_file。
            # as_posix() 将显示的路径分隔符统一为 /，不返回工作区绝对路径。
            relative_path = entry.relative_to(self.workspace).as_posix()
            lines.append(f"[{kind}] {relative_path}")

        if len(entries) > args.max_entries:
            lines.append(
                f"[truncated] Showing {args.max_entries} of {len(entries)} entries. "
                "Increase max_entries (up to 500) to show more."
            )

        # 每个子项占一行；权限等 OSError 由 AgentLoop 已有的异常处理接住。
        return "\n".join(lines)
