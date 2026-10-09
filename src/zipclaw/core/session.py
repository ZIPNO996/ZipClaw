import json
from pathlib import Path


class SessionStore:
    """负责将对话历史保存到文件，以及从文件恢复历史。"""

    def __init__(self, file_path: Path):
        # file_path 是会话文件的位置，不是工作区的位置。
        # 具体保存在哪里，之后由 cli.py 创建实例时决定。
        self.file_path = file_path

    def save(self, messages: list[dict]) -> None:
        """将完整的对话历史保存为 JSON 文件。"""

        # 获取会话文件所在的目录。
        directory = self.file_path.parent

        # 如果目录不存在，就创建它。
        # parents=True：允许连同上层目录一起创建。
        # exist_ok=True：目录已经存在时不报错。
        directory.mkdir(parents=True, exist_ok=True)

        # 将 Python 列表转换成 JSON 文本。
        # ensure_ascii=False：中文直接显示，不转成转义字符。
        # indent=2：增加缩进，方便我们打开文件阅读。
        content = json.dumps(
            messages,
            ensure_ascii=False,
            indent=2,
        )

        # 先写入临时文件，避免写到一半时损坏原来的会话文件。
        # 例如 session.json 对应 session.json.tmp。
        # self.file_path.name：获取文件名，包含扩展名，不包含目录部分。
        # self.file_path.name + ".tmp"：在原文件名后面追加 .tmp。
        # self.file_path.with_name(...)：返回一个新路径，目录不变，只替换文件名部分。
        # 它不会创建文件，也不会修改原来的 self.file_path，只是计算出一个新的 Path 对象。
        # 原来的 self.file_path 不受影响。
        temporary_path = self.file_path.with_name(
            self.file_path.name + ".tmp"
        )

        with temporary_path.open(
            "w",
            encoding="utf-8",
            newline="\n",
        ) as file:
            file.write(content)

        # 临时文件完整写入并关闭后，再替换正式文件。
        # 写临时文件失败时，不会执行到这里，原会话文件仍保留。
        temporary_path.replace(self.file_path)

    def load(self) -> list[dict]:
        """读取已保存的对话；首次使用、文件不存在时返回空列表。"""

        # 第一次运行还没有会话文件，这是正常情况。
        if not self.file_path.exists():
            return []

        # 读取文件内容，得到一个字符串。
        with self.file_path.open("r", encoding="utf-8") as file:
            content = file.read()

        # 将 JSON 字符串还原为 Python 对象。
        try:
            messages = json.loads(content)
        except json.JSONDecodeError as exc:
            # 文件可能被手动改坏，或者内容不是完整 JSON。
            # 不直接返回 []，避免把损坏的历史当成新会话覆盖掉。
            raise ValueError(
                "会话文件不是合法的 JSON，暂时无法恢复。"
            ) from exc

        # 合法 JSON 不一定是我们需要的对话列表。
        # 例如 {"name": "小熊"} 是合法 JSON，但不是会话历史。
        if not isinstance(messages, list):
            raise ValueError("会话内容必须是一个消息列表。")

        # 先做基础结构检查：列表中的每一项都应该是消息字典。
        allowed_roles = {"system", "user", "assistant", "tool"}

        for index, message in enumerate(messages):
            message_number = index + 1

            if not isinstance(message, dict):
                raise ValueError(
                    f"第 {message_number} 条消息不是字典。"
                )

            role = message.get("role")

            # 先检查类型，再检查角色是否合法。
            #如果 role 不是字符串类型，就进入 if 分支。
            if not isinstance(role, str):
                raise ValueError(
                    f"第 {message_number} 条消息缺少有效的角色。"
                )

            if role not in allowed_roles:
                raise ValueError(
                    f"第 {message_number} 条消息的角色不受支持：{role}"
                )

        return messages