import sys
import tempfile
import unittest
from pathlib import Path


# 让测试可以导入当前项目 src 目录中的代码。
project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root / "src"))

from zipclaw.core.session import SessionStore


class TestSessionStore(unittest.TestCase):
    """测试会话的保存和读取，不调用大模型。"""

    def setUp(self):
        # 每个测试使用独立的临时目录，不碰真实聊天记录。
        self.temporary_directory = tempfile.TemporaryDirectory()

        # 测试结束后，自动清理临时目录。
        self.addCleanup(self.temporary_directory.cleanup)

        temporary_root = Path(self.temporary_directory.name)

        # sessions 目录此时还不存在，也能顺便验证自动创建目录。
        self.file_path = temporary_root / "sessions" / "chat.json"
        self.store = SessionStore(self.file_path)

    def test_missing_file_returns_empty_list(self):
        """首次使用，没有会话文件时返回空列表。"""

        messages = self.store.load()

        self.assertEqual(messages, [])
        self.assertFalse(self.file_path.exists())

    def test_save_then_load(self):
        """保存后重新读取，内容应该完全一致。"""

        original_messages = [
            {
                "role": "system",
                "content": "你是一个编程智能体。",
            },
            {
                "role": "user",
                "content": "这个项目叫小熊。",
            },
            {
                "role": "assistant",
                "content": "好的，这个项目叫小熊。",
            },
        ]

        self.store.save(original_messages)

        self.assertTrue(self.file_path.is_file())

        # 重新创建存储对象，模拟程序重启后再次读取。
        # 不能依赖旧对象在内存里记住了什么。
        new_store = SessionStore(self.file_path)
        loaded_messages = new_store.load()

        self.assertEqual(loaded_messages, original_messages)

    def test_invalid_json_raises_error_without_changing_file(self):
        """文件损坏时应报错，并保留原文件。"""

        self.file_path.parent.mkdir(parents=True)

        broken_content = '{"这是一段不完整的 JSON"'
        self.file_path.write_text(
            broken_content,
            encoding="utf-8",
        )

        # 预期 load() 抛出 ValueError。
        with self.assertRaises(ValueError):
            self.store.load()

        # 加载失败不应该清空或者修改原文件。
        remaining_content = self.file_path.read_text(
            encoding="utf-8",
        )
        self.assertEqual(remaining_content, broken_content)


if __name__ == "__main__":
    unittest.main(verbosity=2)