# src/zipclaw/tools/filesystem/grep.py
#
# 搜索流程：检查范围 -> 寻找文件 -> 读取文本 -> 逐行匹配 -> 返回位置。
# 第一版使用 Python 自己搜索，不需要安装外部 grep 命令。

from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel, Field

from ..base import BaseTool


class GrepArgs(BaseModel):
    """搜索工具的参数，由 BaseTool.run() 验证。"""

    # 不设置默认值，表示必须提供；不允许用空字符串匹配所有行。
    pattern: str = Field(
        min_length=1,
        description="要搜索的单行普通文本，区分大小写，不支持正则表达式。",
    )
    path: str = Field(
        default=".",
        min_length=1,
        description="搜索范围的工作区相对路径，可以是文件或目录；默认搜索根目录。",
    )
    max_results: int = Field(
        default=50,
        ge=1,
        le=200,
        description="最多返回的匹配行数，默认 50，范围为 1 到 200。",
    )


class GrepTool(BaseTool):
    """按普通文本搜索文件，返回工作区相对路径、行号和匹配行。"""

    name = "grep"
    description = (
        "在工作区内搜索单行普通文本，区分大小写，不支持正则表达式。"
        "指定文件时只搜索该文件，指定目录时递归搜索。"
        "返回相对路径、从 1 开始的行号和匹配行，便于随后读取或编辑文件。"
        "递归时跳过 .git、.venv、venv、__pycache__、node_modules 目录和链接。"
        "跳过超过 1 MiB、包含空字节、无法按 UTF-8 解码或无法读取的文件。"
        "达到结果上限时提示截断，匹配行超过 500 个字符时仅显示开头。"
    )
    args_model = GrepArgs

    # 集合适合判断“这个名称是否在里面”，不会重复保存同一个值。
    # 这些规则用于递归搜索；显式指定普通文件时仍会搜索该文件。
    SKIP_DIRECTORIES = {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
    }
    MAX_FILE_BYTES = 1024 * 1024  # 1 MiB，避免一次读取很大的文件。
    MAX_LINE_CHARS = 500  # 限制单条结果长度，避免压缩文件的一行占满上下文。

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()

    def _iter_files(self, target: Path) -> Iterator[Path]:
        """逐个提供候选文件；下划线表示这是工具内部使用的辅助方法。"""

        if target.is_file():
            # yield 和 return 不同：提供一个值后暂停，调用者可继续取下一个。
            yield target
            return

        # Path.walk() 从 Python 3.12 开始提供。
        # 每轮给出当前目录、子目录名称列表、文件名称列表，不读取文件内容。
        # 遍历目录出错时向外抛出，避免悄悄漏搜却声称没有结果。
        def raise_walk_error(error: OSError) -> None:
            # walk() 遇到无权限等问题时，会调用这个函数。
            # 把错误继续交给外面的 AgentLoop 处理。
            raise error

        # top_down=True：先处理当前目录，再进入子目录。
        # follow_symlinks=False：不主动进入符号链接指向的目录。
        directory_walk = target.walk(
            top_down=True,
            follow_symlinks=False,
            on_error=raise_walk_error,
        )

        for directory, dirnames, filenames in directory_walk:
            # directory：当前目录的路径。
            # dirnames：当前目录下的子目录名称列表。
            # filenames：当前目录下的文件名称列表。
            allowed_directories = []

            for name in dirnames:
                directory_path = directory / name
                lowercase_name = name.casefold()

                # 分别判断每个条件，遇到不需要搜索的目录就跳过。
                if lowercase_name in self.SKIP_DIRECTORIES:
                    continue

                if directory_path.is_symlink():
                    continue

                if directory_path.is_junction():
                    continue

                allowed_directories.append(name)

            # 排序让每次遍历的顺序一致。
            allowed_directories.sort()

            # dirnames[:] 是原地修改列表，walk() 会据此决定进入哪些子目录。
            # 只写 dirnames = ... 会换成另一个列表，无法控制 walk() 的遍历。
            dirnames[:] = allowed_directories

            filenames.sort()
            for name in filenames:
                file_path = directory / name

                # 每次交出一个文件路径，execute() 会读取并搜索这个文件。
                # 当 execute() 要下一个文件时，这里才继续往后执行。
                yield file_path

    async def execute(self, args: GrepArgs) -> str:
        # 第一步：检查搜索参数和搜索范围。
        # 按行搜索不能匹配跨行文本，直接给模型一个清楚的提示。
        if "\n" in args.pattern:
            return "搜索文本不能包含换行，请搜索单行文本。"

        if "\r" in args.pattern:
            return "搜索文本不能包含换行，请搜索单行文本。"

        requested_path = self.workspace / args.path
        target = requested_path.resolve()
        if not target.is_relative_to(self.workspace):
            raise ValueError("不能搜索工作区之外的路径。")
        if requested_path.is_symlink():
            return "不搜索链接路径，请指定工作区内的实际文件或目录。"

        if requested_path.is_junction():
            return "不搜索链接路径，请指定工作区内的实际文件或目录。"

        if not target.exists():
            return f"搜索路径不存在：{args.path}"

        # 既不是文件，也不是目录时，不能使用这个搜索工具。
        if not target.is_file():
            if not target.is_dir():
                return f"搜索路径不是普通文件或目录：{args.path}"

        # 第二步：依次读取候选文件。
        # results 存放要返回的匹配行；skipped 记录跳过的文件数量。
        results: list[str] = []
        skipped = 0

        candidate_files = self._iter_files(target)
        for candidate in candidate_files:
            try:
                # 对每个候选文件再次检查边界，且不跟随文件链接。
                if candidate.is_symlink():
                    skipped += 1
                    continue

                if candidate.is_junction():
                    skipped += 1
                    continue

                resolved = candidate.resolve()

                if not resolved.is_relative_to(self.workspace):
                    skipped += 1
                    continue

                if not resolved.is_file():
                    skipped += 1
                    continue

                # 二进制模式 rb 返回 bytes，不自动解码或转换换行。
                # 多读 1 字节用于判断文件是否超限，不能先无限读取再截断。
                read_limit = self.MAX_FILE_BYTES + 1
                with resolved.open("rb") as file:
                    raw = file.read(read_limit)

                file_size = len(raw)
                if file_size > self.MAX_FILE_BYTES:
                    skipped += 1
                    continue

                # 包含空字节时，按疑似二进制文件处理。
                if b"\x00" in raw:
                    skipped += 1
                    continue

                # 严格解码失败会抛出 UnicodeDecodeError，在下面捕获并跳过。
                text = raw.decode("utf-8")

            except OSError:
                # 文件无权限读取、读取前被删除等情况。
                skipped += 1
                continue

            except UnicodeError:
                # 文件无法按 UTF-8 解码。
                skipped += 1
                continue

            # 第三步：在这个文件中逐行搜索。
            workspace_relative_path = resolved.relative_to(self.workspace)
            relative_path = workspace_relative_path.as_posix()

            # splitlines() 拆分文本行；enumerate(..., start=1) 同时提供行号。
            lines = text.splitlines()
            for line_number, line in enumerate(lines, start=1):
                # in 是普通子串匹配；句点、星号等字符不会被当作正则语法。
                if args.pattern not in line:
                    continue
                # 发现额外一条匹配才报告截断；刚好达到上限不代表存在遗漏。
                result_count = len(results)
                if result_count == args.max_results:
                    results.append("[已截断] 还有其他匹配，请缩小搜索范围或增大 max_results。")

                    if skipped > 0:
                        skipped_message = f"[提示] 已跳过 {skipped} 个无法搜索的文件。"
                        results.append(skipped_message)

                    # return 结束整个方法，文件循环和行循环都会停止。
                    output = "\n".join(results)
                    return output

                # 搜索时用完整行，展示时只保留开头，避免结果太长。
                preview = line[:self.MAX_LINE_CHARS]
                line_length = len(line)
                if line_length > self.MAX_LINE_CHARS:
                    preview += "……[该行过长，仅显示开头，请读取原文件]"

                result_line = f"{relative_path}:{line_number}: {preview}"
                results.append(result_line)

        # 第四步：所有文件搜索结束后，整理返回文本。
        if len(results) == 0:
            results.append("在已检查的文件中未找到匹配文本。")

        if skipped > 0:
            skipped_message = f"[提示] 已跳过 {skipped} 个无法搜索的文件。"
            results.append(skipped_message)

        output = "\n".join(results)
        return output
