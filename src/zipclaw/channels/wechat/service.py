"""把微信文字消息交给 Agent；独立接收循环让取消和操作确认能及时处理。"""

import asyncio
import json
from collections import deque
from pathlib import Path
from uuid import uuid4

from ...core.loop import AgentLoop
from ...core.session import SessionStore
from ...error.custom_errors import (
    AgentStepLimitError,
    ModelRequestError,
    ModelResponseError,
)
from .client import WechatAPIError, WechatClient
from .storage import WechatCredentials, read_json, write_json


HELP_TEXT = (
    "发送文字即可让 ZipClaw 处理当前工作区。\n"
    "/help：查看帮助\n"
    "/status：查看任务状态\n"
    "/cancel：停止当前任务\n"
    "/clear：清空微信会话（先停止正在执行的任务）\n"
    "受限模式下，写文件、运行命令、联网搜索和生图前会请求确认，"
    "请回复消息中的 /approve 或 /deny 指令。"
)


class WechatService:
    def __init__(
        self,
        client: WechatClient,
        credentials: WechatCredentials,
        agent: AgentLoop,
        session_store: SessionStore,
        state_path: Path,
        full_access: bool = False,
    ):
        self.client = client
        self.credentials = credentials
        self.agent = agent
        self.session_store = session_store
        self.state_path = state_path
        self.full_access = full_access
        self.state = read_json(state_path)
        self.cursor = self.state.get("cursor", "")
        self.seen_ids = self.state.get("seen_ids", [])
        if (
            not isinstance(self.cursor, str)
            or not isinstance(self.seen_ids, list)
            or any(not isinstance(item, str) for item in self.seen_ids)
        ):
            raise ValueError("微信收消息进度格式不正确，请检查本地记录。")

        self.context_token = ""
        self.current_task: asyncio.Task | None = None
        self.approval_id = ""
        self.approval_future: asyncio.Future | None = None
        self.send_lock = asyncio.Lock()
        # 忙碌时只排队，不向手机重复发送状态提示。
        self.pending_tasks: deque[str] = deque()
        self.stopping = False
        self.typing_task: asyncio.Task | None = None
        self.typing_ticket = ""
        self.typing_lock = asyncio.Lock()
        self.waiting_for_approval = False
        # 一个新进程不会自动重跑上次任务，避免重复执行已发生的修改。
        previous_status = self.state.get("task_status", "空闲")
        if not isinstance(previous_status, str):
            raise ValueError("微信任务状态格式不正确，请检查本地记录。")
        self.task_status = (
            "上次任务随进程退出而停止，请核查文件后再发新任务"
            if previous_status == "执行中"
            else previous_status
        )
        self.agent.approval_callback = self.confirm_tool

    def busy(self) -> bool:
        return self.current_task is not None and not self.current_task.done()

    def save_state(self) -> None:
        write_json(
            self.state_path,
            {
                "cursor": self.cursor,
                "seen_ids": self.seen_ids[-1000:],
                "task_status": self.task_status,
            },
        )

    async def send(self, text: str) -> None:
        # 避免长回答和确认消息的多个分段互相穿插。
        async with self.send_lock:
            await self.client.send_text(
                self.credentials.owner_id,
                text,
                self.context_token,
            )

    async def send_image(self, data: bytes) -> None:
        async with self.send_lock:
            await self.client.send_image(
                self.credentials.owner_id,
                data,
                self.context_token,
            )

    async def set_typing(self, active: bool) -> None:
        """输入状态失败只记在电脑终端，不打断实际任务或刷微信消息。"""

        async with self.typing_lock:
            if not self.typing_ticket:
                return
            if active and self.waiting_for_approval:
                return
            try:
                await self.client.send_typing(
                    self.credentials.owner_id,
                    self.typing_ticket,
                    active,
                )
            except WechatAPIError as exc:
                print(f"微信输入状态更新失败：{exc}", flush=True)

    async def typing_loop(self) -> None:
        try:
            self.typing_ticket = await self.client.get_typing_ticket(
                self.credentials.owner_id,
                self.context_token,
            )
        except WechatAPIError as exc:
            print(f"无法取得微信输入状态凭证：{exc}", flush=True)
            return
        if not self.typing_ticket:
            print("微信未返回输入状态凭证，继续执行任务。", flush=True)
            return

        # 长任务定期更新，避免手机输入提示只出现一瞬间。
        while True:
            await self.set_typing(not self.waiting_for_approval)
            await asyncio.sleep(8)

    async def stop_typing(self) -> None:
        task = self.typing_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.typing_task = None
        await self.set_typing(False)

    def start_task(self, text: str) -> None:
        self.task_status = "执行中"
        self.save_state()
        self.current_task = asyncio.create_task(self.run_task(text))

    async def confirm_tool(self, name: str, arguments: dict) -> bool:
        # 图片只发送给当前扫码用户；完全访问模式则所有工具免确认。
        if self.full_access or name == "send_image":
            return True

        parameters = json.dumps(arguments, ensure_ascii=False, indent=2)
        if len(parameters.encode("utf-8")) > 18000:
            await self.send("本次操作参数过长，无法完整展示确认，已拒绝。请在终端执行。")
            return False

        approval_id = uuid4().hex[:8]
        future = asyncio.get_running_loop().create_future()
        self.approval_id = approval_id
        self.approval_future = future
        self.waiting_for_approval = True
        try:
            await self.set_typing(False)
            prompt = (
                f"等待确认：{name}\n完整参数：\n{parameters}\n\n"
            )
            if name == "web_search":
                prompt += "搜索关键词会发送给 Tavily，并消耗其搜索额度。\n"
            if name == "generate_image":
                prompt += (
                    "图片描述和参考图链接会发送给 ModelScope，并使用其额度。\n"
                    "取消本地等待不代表取消远端任务；失败时不会自动重新提交。\n"
                )
            if name == "edit_file":
                preview = self.agent.build_edit_preview(arguments)
                if preview is not None:
                    prompt += "\n差异预览（片段内行号）：\n" + preview
            prompt += (
                f"\n\n同意：/approve {approval_id}\n"
                f"拒绝：/deny {approval_id}\n"
                "确认消息全部发送后开始计时，两分钟内未同意就拒绝。"
            )

            await self.send(prompt)
            try:
                # shield 防止 wait_for 超时直接取消用于接收确认的 Future。
                return await asyncio.wait_for(asyncio.shield(future), timeout=120)
            except TimeoutError:
                if not future.done():
                    future.cancel()
                await self.send("操作确认已超时，本次工具调用未执行。")
                return False
        finally:
            if not future.done():
                future.cancel()
            self.approval_id = ""
            self.approval_future = None
            self.waiting_for_approval = False

    async def handle_message(self, message: dict) -> None:
        if not isinstance(message, dict):
            return
        if message.get("from_user_id") != self.credentials.owner_id:
            return
        if message.get("group_id") or message.get("message_type") != 1:
            return
        if message.get("message_state") not in {None, 2}:
            return

        message_id = message.get("message_id")
        if (
            isinstance(message_id, bool)
            or not isinstance(message_id, (str, int))
            or not str(message_id)
        ):
            print("跳过没有有效 ID 的微信消息，避免重复执行。", flush=True)
            return
        message_id = str(message_id)
        if message_id in self.seen_ids:
            return

        context_token = message.get("context_token")
        if not isinstance(context_token, str) or not context_token:
            print("跳过缺少 context_token 的微信消息。", flush=True)
            return

        # 在调用模型或执行任何操作前保存 ID。
        # 程序崩溃后不会自动重跑这条消息；未完成任务需要用户重新提出。
        self.context_token = context_token
        self.seen_ids.append(message_id)
        self.seen_ids = self.seen_ids[-1000:]
        self.save_state()

        items = message.get("item_list")
        if not isinstance(items, list):
            await self.send("当前只支持文字消息。")
            return
        pieces = []
        for item in items:
            if not isinstance(item, dict) or item.get("type") != 1:
                continue
            text_item = item.get("text_item")
            if isinstance(text_item, dict) and isinstance(text_item.get("text"), str):
                pieces.append(text_item["text"])
        text = "\n".join(pieces).strip()
        if not text:
            await self.send("当前只支持文字，请直接发送文字任务。")
            return
        if len(text.encode("utf-8")) > 30000:
            await self.send("文字任务过长，请拆成较短的消息。")
            return

        words = text.split()
        command = words[0].lower()
        if command in {"/approve", "/deny"}:
            future = self.approval_future
            if (
                len(words) == 2
                and words[1] == self.approval_id
                and future is not None
                and not future.done()
            ):
                future.set_result(command == "/approve")
                await self.send("已同意本次操作。" if command == "/approve" else "已拒绝本次操作。")
            else:
                await self.send("确认编号无效或已过期，请使用当前确认消息中的完整指令。")
            return
        if text == "/help":
            await self.send(HELP_TEXT)
            return
        if text == "/status":
            status = "等待操作确认" if self.approval_future is not None else self.task_status
            mode = "完全访问" if self.full_access else "需要操作确认"
            await self.send(
                f"当前状态：{status}\n访问模式：{mode}\n"
                f"待处理消息：{len(self.pending_tasks)} 条"
            )
            return
        if text == "/cancel":
            # 取消时也清掉排队消息，避免刚停止就自动开启后续任务。
            self.pending_tasks.clear()
            if self.busy():
                self.current_task.cancel()
                # 不在接收循环里等待任务结束，任务本身完成清理后会回复。
            else:
                await self.send("当前没有正在执行的任务。")
            return
        if text == "/clear":
            if self.busy():
                await self.send("当前任务仍在执行，请先发送 /cancel，停止后再 /clear。")
            else:
                cleared_messages = [self.agent.messages[0]]
                self.session_store.save(cleared_messages)
                self.agent.messages = cleared_messages
                await self.send("微信会话已清空。终端会话不受影响。")
            return
        if text.startswith("/"):
            await self.send("未知指令，请发送 /help 查看可用指令。")
            return
        if self.busy():
            self.pending_tasks.append(text)
            return

        self.start_task(text)

    async def run_task(self, text: str) -> None:
        reply = ""
        self.typing_ticket = ""
        self.typing_task = asyncio.create_task(self.typing_loop())
        try:
            result = await self.agent.run(text)
            reply = result if result.strip() else "任务已结束，模型没有返回文字回答。"
            self.task_status = "已完成"
        except asyncio.CancelledError:
            self.agent.record_interruption()
            self.task_status = "已取消"
            reply = "当前任务已取消。已经发生的文件修改不会自动撤销。"
        except (AgentStepLimitError, ModelRequestError, ModelResponseError) as exc:
            self.task_status = "任务停止"
            reply = f"任务已停止：{exc}\n可以继续发送新的要求。"
        except Exception as exc:
            # 本次任务可能在工具确认阶段或执行阶段失败，补齐未完成工具记录。
            self.agent.record_interruption()
            self.task_status = "执行失败"
            reply = "任务执行失败，已停止。请检查运行终端，并核查已有文件修改。"
            print(f"微信任务执行失败，异常类型：{type(exc).__name__}。", flush=True)

        try:
            self.session_store.save(self.agent.messages)
            self.save_state()
        except (OSError, TypeError, ValueError):
            reply += "\n会话保存失败，历史仍在内存中；请检查本地目录权限。"
            print("微信会话或状态保存失败。", flush=True)

        try:
            try:
                await self.send(reply)
            except WechatAPIError as exc:
                # 只重试收消息；不重复请求模型，也不重复执行任务或发送回答。
                print(f"微信回复发送失败：{exc} 回答仍在本地会话中。", flush=True)
        finally:
            await self.stop_typing()

        if self.pending_tasks and not self.stopping:
            self.start_task(self.pending_tasks.popleft())

    async def run(self) -> None:
        print("微信入口已启动。向微信 ClawBot 发送文字即可使用，Ctrl+C 停止服务。")
        self.save_state()
        try:
            while True:
                try:
                    result = await self.client.get_updates(self.cursor)
                except WechatAPIError as exc:
                    if not exc.retryable:
                        raise
                    print(f"{exc} 五秒后重新收取消息。", flush=True)
                    await asyncio.sleep(5)
                    continue

                messages = result.get("msgs", [])
                if not isinstance(messages, list):
                    raise WechatAPIError("微信返回的消息列表格式不正确。")
                for message in messages:
                    try:
                        await self.handle_message(message)
                    except WechatAPIError as exc:
                        print(f"微信指令回复失败：{exc}", flush=True)

                next_cursor = result.get("get_updates_buf")
                if isinstance(next_cursor, str) and next_cursor:
                    self.cursor = next_cursor
                    self.save_state()
                # 服务端长轮询通常已经等待；加短暂间隔避免空响应时高频请求。
                await asyncio.sleep(0.5)
        finally:
            self.stopping = True
            self.pending_tasks.clear()
            if self.busy():
                self.current_task.cancel()
                await self.current_task
