# src/zipclaw/tools/filesystem/edit_file.py

from pathlib import Path

from pydantic import BaseModel, Field

from ..base import BaseTool

import shutil
from tempfile import NamedTemporaryFile


class EditFileArgs(BaseModel):
    """编辑工具的参数；先由 Pydantic 验证，再执行文件修改。"""

    # 没有默认值，表示必须提供；min_length=1 拒绝空字符串。
    path: str = Field(
        min_length=1,
        description="已有文件的工作区相对路径，例如：src/main.py。",
    )
    old_text: str = Field(
        min_length=1,
        description=(
            "需要替换的准确原文，包括空格、缩进和换行；"
            "必须恰好出现一次，必要时加入附近代码以明确位置。"
        ),
    )
    # 允许空字符串，代表删除旧文本，因此不设置 min_length=1。
    new_text: str = Field(
        description="替换后的文本；可以为空字符串，表示删除匹配内容。",
    )


class EditFileTool(BaseTool):
    """精确替换已有文件的一段文本，不负责创建新文件。"""

    name = "edit_file"
    # 括号内相邻字符串会自动拼接；不会像三引号内的引号一样成为描述内容。
    description = (
        "修改工作区内已有的 UTF-8 文本文件。"
        "将唯一匹配的 old_text 精确替换为 new_text。"
        "使用工作区相对路径，编辑前先读取文件。"
        "旧文本不能为空，必须包含准确的空格、缩进和换行，并恰好出现一次。"
        "若位置不明确，请加入附近代码以明确位置。"
        "新文本可以为空，表示删除旧文本。"
        "文件不存在或匹配不唯一时拒绝修改。"
    )
    args_model = EditFileArgs

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()

    async def execute(self, args: EditFileArgs) -> str:
        """先检查路径和匹配，再生成新内容，最后写入磁盘。"""

        target = (self.workspace / args.path).resolve()
        if not target.is_relative_to(self.workspace):
            raise ValueError("不能访问工作区之外的文件。")

        if not target.exists():
            return f"文件不存在：{args.path}"
        if not target.is_file():
            return f"不是普通文件：{args.path}"

        # 防御直接调用 execute()、绕过正常参数验证的情况。
        if not args.old_text:
            return "拒绝修改：旧文本不能为空。"

        # newline="" 保留原始换行；和 read_file 的读取方式保持一致。
        # 不使用 errors="replace"：无法解码时拒绝编辑，避免替换字符被写回。
        with target.open("r", encoding="utf-8", newline="") as file:
            original = file.read()

        # count() 统计非重叠匹配；这里沿用第一版的精确替换规则。
        count = original.count(args.old_text)
        if count == 0:
            return f"拒绝修改 {args.path}：未找到旧文本，请重新读取文件。"
        if count > 1:
            return f"拒绝修改 {args.path}：旧文本出现 {count} 次，请提供更多上下文。"

        # 生成新字符串，此时还没有修改磁盘文件。
        # 第三个参数 1 表示最多替换一处，其他文字保持不变。
        updated = original.replace(args.old_text, args.new_text, 1)
        if updated == original:
            return f"无需修改：{args.path}"

        # 保存本次创建的临时文件路径。
        # 一开始还没创建，所以是 None。
        temporary_path: Path | None = None

        try:
            # 在原文件所在目录创建一个名称不重复的临时文件。
            # 同目录可以避免跨磁盘替换的问题。
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                dir=target.parent,
                prefix=".zipclaw-edit-",
                suffix=".tmp",

                # 离开 with 时关闭文件，但先不删除。
                # 后面还需要用它替换原文件。
                delete=False,
            ) as file:
                # file.name 是实际创建出来的临时文件路径。
                # 先记住它，写入失败时也能找到并清理。
                temporary_path = Path(file.name)

                # 新内容先写进临时文件。
                # 此时原文件还没有被修改。
                file.write(updated)

            # 执行到这里，临时文件已经写完并关闭。
            # 保留原文件的普通权限位，例如执行权限。
            # 这不等于复制所有文件属性或 Windows ACL。
            # target是原文件路径
            shutil.copymode(target, temporary_path)

            # 保存前，再读取一次磁盘上的当前内容。
            with target.open("r", encoding="utf-8", newline="") as file:
                current_content = file.read()

            # 与本次编辑开始时读到的内容进行比较。
            if current_content != original:
                raise ValueError(
                    "文件在编辑过程中发生了变化，本次修改已取消。"
                    "请重新读取文件后再修改。"
                )

            # 用写好的临时文件替换原文件。
            # 替换成功后，新内容就在 target 路径下，
            # 临时文件原来的路径不再存在。
            temporary_path.replace(target)

        finally:
            # 无论成功还是发生异常，都尝试清理临时文件。
            if temporary_path is not None:
                try:
                    # 替换成功后，临时路径已经不存在。
                    # missing_ok=True 表示这种情况不用报错。
                    temporary_path.unlink(missing_ok=True)

                except OSError as exc:
                    # 清理失败时给出提示。
                    # 不让清理异常覆盖前面真正的写入或替换异常。
                    print(
                        f"[清理提示] 临时文件未能删除："
                        f"{temporary_path}，原因：{exc}"
                    )

        return f"已修改 {args.path}，替换了 1 处文本。"


