"""
MattermostApprovalAdapter — 继承内置 MattermostAdapter，扩展 DM 审批 + /model 卡片 + /new 确认。

架构说明：
  Mattermost 拦截所有 / 开头消息，必须注册 Slash Command 才能接收。
  Slash Command payload 不含 root_id，需要通过 Mattermost API 反查 thread 上下文。

核心能力：
  1. DM 审批卡片 → 多用户频道按消息真实发送者精确定位发起者
  2. /model 模型切换卡片 → session_key 与 Gateway build_session_key 对齐
  3. /new 会话重置确认卡片
  4. Clarify 交互卡片（按钮选择 + 「其他」文本输入）
  5. 独立线程回调服务器 → HTTP 响应与 gateway 主 loop 负载解耦
  6. Typing 指示器进 Thread、WebSocket 心跳 15s、footer 编辑合并

上游对齐（v2026.9.7）：
  上游 bundled adapter 已原生实现 thread 路由（_post_message → root_id 解析 +
  metadata.thread_id 降级 + broken-thread-root fallback）、mentions 抑制、
  媒体文件 Thread 投递、消息分块 — 插件不再覆写这些方法，仅保留缓存版
  _resolve_root_id 供上游多态调用。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, Optional, Tuple

from gateway.platforms.base import SendResult
from tools.approval import resolve_gateway_approval

# Mattermost 基础适配器已从 gateway/platforms/ 移至 bundled plugin
# (hermes-agent/plugins/platforms/mattermost/)。
#
# 插件系统会先加载 bundled mattermost-platform 为
# hermes_plugins.platforms_mattermost，然后才加载 mattermost-enhancer。
# 正常场景下 try 分支即命中；fallback 仅在非标准环境（如直接 pytest）下触发。
try:
    from hermes_plugins.platforms_mattermost.adapter import MattermostAdapter
except ImportError:
    import importlib.util
    import sys
    from pathlib import Path as _Path

    _bundled_mm = _Path(__file__).parent.parent.parent / "hermes-agent" / "plugins" / "platforms" / "mattermost"
    _init = _bundled_mm / "__init__.py"
    if _init.exists():
        import types as _types
        if "hermes_plugins" not in sys.modules:
            _ns = _types.ModuleType("hermes_plugins")
            _ns.__path__ = []
            sys.modules["hermes_plugins"] = _ns
        _spec = importlib.util.spec_from_file_location(
            "hermes_plugins.platforms_mattermost",
            _init,
            submodule_search_locations=[str(_bundled_mm)],
        )
        if _spec and _spec.loader:
            _mod = importlib.util.module_from_spec(_spec)
            sys.modules["hermes_plugins.platforms_mattermost"] = _mod
            _spec.loader.exec_module(_mod)
            from hermes_plugins.platforms_mattermost.adapter import MattermostAdapter  # noqa: F811
        else:
            raise
    else:
        raise

from .cards import (
    render_model_selector_card,
    render_new_session_confirm_card,
    render_switch_success_card,
    render_reset_success_card,
    render_clarify_card,
    render_clarify_choice_confirmed_card,
    render_clarify_other_prompt_card,
)

logger = logging.getLogger(__name__)


class MattermostApprovalAdapter(MattermostAdapter):
    """Mattermost 适配器 — DM 审批 + /model 卡片 + /new 确认。"""

    # 消息长度限制：上游 bundled adapter 已定义 MAX_MESSAGE_LENGTH 派生链，
    # 基类 max_message_length_for_chat() 读取该属性（不存在时回退 4096）。
    # 插件历史上曾用 MAX_MESSAGE_LENGTH = MAX_POST_LENGTH 修正 4096 > 4000
    # 的截断差，现已由父类统一提供，不再重复定义。

    def __init__(self, config):
        super().__init__(config)
        self._model_picker_callbacks: Dict[str, Callable] = {}
        # Channel type 缓存: channel_id → chat_type（避免每次 /model 都调 API）
        self._channel_type_cache: Dict[str, str] = {}

        # ── Callback server 配置 ──
        self._callback_server = None
        self._callback_port: int = int(
            os.getenv("MATTERMOST_CALLBACK_PORT", "18065")
        )
        self._callback_bind: str = os.getenv(
            "MATTERMOST_CALLBACK_BIND", "127.0.0.1"
        )
        self._callback_url: str = os.getenv(
            "MATTERMOST_CALLBACK_URL", ""
        )
        self._callback_secret: str = os.getenv(
            "MATTERMOST_CALLBACK_SECRET", ""
        )
        # DM channel 缓存: user_id → dm_channel_id
        self._dm_cache: Dict[str, str] = {}
        # root_id 缓存: post_id → (root_id, timestamp)
        # 避免每次发消息都调 API GET（WebSocket 不稳定时频繁超时）
        self._root_id_cache: Dict[str, Tuple[str, float]] = {}
        self._root_id_cache_ttl: float = 300.0  # 5 分钟
        # Footer 追踪: chat_id → (post_id, content)
        # runtime footer 不独立发帖，而是编辑上一条消息追加到末尾
        self._tracked_posts: Dict[str, Tuple[str, str]] = {}
        # 回调去重: "{post_id}:{action}" → True（防双击重复处理）
        # 独立回调线程可并发处理多请求，去重读写需线程锁
        self._callback_dedup: Dict[str, bool] = {}
        import threading as _threading
        self._dedup_lock = _threading.Lock()
        # 独立回调线程的 loop/thread（由 _start_callback_server 填充）
        self._callback_loop = None
        self._callback_thread = None
        # gateway 主事件循环引用（由 _start_callback_server 填充，followup 使用）
        self._gateway_main_loop = None
        # 入站消息 sender 追踪：用于 DM 审批精确定位发起者。
        # key = channel_id 或 (channel_id, thread_id)，value = 最近发言的 user_id。
        # 修复多用户频道下审批误发给管理员（members 反查只能取第一个非 bot 成员）。
        self._last_sender_by_channel: Dict[str, str] = {}
        self._last_sender_by_thread: Dict[Tuple[str, str], str] = {}

    # ══════════════════════════════════════════════════════════════════════
    # 公共辅助方法
    # ══════════════════════════════════════════════════════════════════════

    def _build_callback_url(self) -> str:
        """构建回调 URL：环境变量优先，否则用 localhost 默认值."""
        return self._callback_url or (
            f"http://{self._callback_bind}:{self._callback_port}"
            f"/mattermost/callback"
        )

    def _get_allowed_users(self) -> set:
        """获取 MATTERMOST_ALLOWED_USERS 配置."""
        allowed_str = os.getenv("MATTERMOST_ALLOWED_USERS", "").strip()
        if not allowed_str:
            return set()
        return {u.strip() for u in allowed_str.split(",") if u.strip()}

    @staticmethod
    def _is_footer_line(content: str) -> bool:
        """检测 runtime footer 行 — 单行、含 · 分隔符、纯文本."""
        if "\n" in content or len(content) > 120:
            return False
        if " · " not in content:
            return False
        return True

    async def _get_or_create_dm(self, user_id: str) -> str:
        """获取或创建与指定用户的 DM channel（幂等，带缓存）."""
        if user_id in self._dm_cache:
            return self._dm_cache[user_id]

        payload = [self._bot_user_id, user_id]
        data = await self._api_post("channels/direct", payload)

        dm_id = data.get("id", "")
        if dm_id:
            self._dm_cache[user_id] = dm_id

        return dm_id

    async def _get_user_id_from_channel(self, channel_id: str) -> Optional[str]:
        """从 channel members 中提取非 bot 的 user_id。

        替代 patch 8：当 run.py 未传 user_id 时，通过 channel members API
        反查 DM channel 中的对方用户 ID。额外一次 GET 请求，仅在审批触发时调用。

        注意：多用户频道（public/group）下 members 列表有多个非 bot 成员，
        取「第一个」不可靠（通常是管理员）。senders 追踪命中时应优先用
        `_resolve_approval_requester`，本方法仅作最后兜底。
        """
        try:
            data = await self._api_get(f"channels/{channel_id}/members")
            if isinstance(data, list):
                for member in data:
                    uid = member.get("user_id", "")
                    if uid and uid != self._bot_user_id:
                        return uid
        except Exception:
            logger.warning(
                "Mattermost: _get_user_id_from_channel failed for %s",
                channel_id, exc_info=True,
            )
        return None

    def build_source(self, *args, **kwargs):
        """覆写基类 build_source — 记录入站消息 sender，供 DM 审批定位发起者。

        审批请求由 gateway 在消息处理过程中同步触发，此时该消息的 sender
        就是真正发起人。多用户频道下 members 反查无法区分发起者，这里在
        每条入站消息落地 source 前记录发送者，send_exec_approval 据此精确定位。
        """
        try:
            chat_id = kwargs.get("chat_id")
            user_id = kwargs.get("user_id")
            thread_id = kwargs.get("thread_id")
            if chat_id and user_id:
                self._last_sender_by_channel[str(chat_id)] = str(user_id)
                if thread_id:
                    self._last_sender_by_thread[(str(chat_id), str(thread_id))] = str(user_id)
        except Exception:
            logger.debug(
                "Mattermost: build_source sender tracking failed",
                exc_info=True,
            )
        return super().build_source(*args, **kwargs)

    def _resolve_approval_requester(
        self, chat_id: str, metadata: Optional[Dict[str, Any]]
    ) -> Optional[str]:
        """从入站 sender 追踪中定位审批发起者。

        优先级：thread 精确匹配 → channel 最近发言 → None（由调用方兜底）。
        修复多用户频道下 members 反查误取第一个非 bot 成员（通常是管理员）。
        """
        cid = str(chat_id)
        thread_id = (metadata or {}).get("thread_id")
        if thread_id:
            uid = self._last_sender_by_thread.get((cid, str(thread_id)))
            if uid:
                return uid
        return self._last_sender_by_channel.get(cid)

    # ══════════════════════════════════════════════════════════════════════
    # 回调服务器（多路由）— 独立线程 + 独立事件循环
    # ══════════════════════════════════════════════════════════════════════
    #
    # 为什么回调服务器必须跑在独立线程/独立 loop 上：
    #   旧实现用 asyncio.start_server() 把回调协程挂在 gateway 主事件循环
    #   上。agent 执行复杂操作时，主 loop 被流式 delta、消息编辑、心跳等
    #   回调排满，按钮回调协程被饿死——用户点击后卡片长时间无反馈
    #   （实测：MM 日志显示点击后 8.4s 才收到 update 响应），用户误以为
    #   没点中而重复点击（MM 日志中存在 2ms 双击实录）。
    #
    #   独立线程 + 独立 loop 后：
    #     1. HTTP 响应延迟与主 loop 负载彻底解耦（恒为毫秒级）；
    #     2. 纯内存操作（审批/Clarify resolve，tools.approval /
    #        tools.clarify_gateway 均为线程安全模块级状态）在回调线程
    #        同步完成，HTTP 响应直接携带最终卡片状态；
    #     3. 涉及网络/主 loop 状态的耗时操作（模型切换、会话重置、
    #        Bot API 发帖）通过 followup 协程派回主 loop 后台执行，
    #        HTTP 先返回「⏳ 已收到」并清空按钮，杜绝重复点击。
    # ══════════════════════════════════════════════════════════════════════

    def _get_gateway_loop(self):
        """获取 gateway 主事件循环（用于把耗时操作派回主 loop 执行）。

        只返回真正的主 loop — 绝不能返回当前回调线程的 loop：
        aiohttp.ClientSession 绑定在主 loop 上，跨 loop 使用是未定义行为。
        """
        loop = self._gateway_main_loop
        if loop is not None and loop.is_running():
            return loop
        try:
            from gateway.run import _gateway_runner_ref
            runner = _gateway_runner_ref()
            if runner is not None:
                loop = getattr(runner, "_gateway_loop", None)
                if loop is not None and loop.is_running():
                    return loop
        except Exception:
            pass
        return None

    def _schedule_followup(self, coro) -> None:
        """把耗时 followup 协程派回 gateway 主 loop 后台执行。

        主 loop 不可用时降级为当前（回调）loop 的后台任务，
        保证回调响应永远不被阻塞。
        """
        loop = self._get_gateway_loop()
        if loop is not None:
            asyncio.run_coroutine_threadsafe(coro, loop)
        else:
            asyncio.get_running_loop().create_task(coro)

    async def _start_callback_server(self) -> None:
        """启动 HTTP callback server（独立线程 + 独立事件循环）。

        路由：
          POST /mattermost/callback → 按钮回调（审批 + 模型切换 + 会话重置 + Clarify）
          POST /mm-command          → Slash 指令（/model + /new）
        """
        import asyncio as _asyncio
        import threading as _threading

        adapter_self = self
        main_loop = _asyncio.get_running_loop()  # gateway 主 loop（供 followup 使用）
        adapter_self._gateway_main_loop = main_loop

        async def _handler(reader: _asyncio.StreamReader, writer: _asyncio.StreamWriter):
            try:
                request_data = await _asyncio.wait_for(reader.read(65536), timeout=10.0)
                if not request_data:
                    writer.close()
                    return

                request_text = request_data.decode("utf-8", errors="replace")
                headers, _, body = request_text.partition("\r\n\r\n")
                request_line = headers.split("\r\n")[0]
                parts = request_line.split(" ", 2)
                if len(parts) < 2:
                    writer.close()
                    return
                method, path = parts[0], parts[1]

                if method != "POST":
                    writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
                    await writer.drain()
                    writer.close()
                    return

                if path == "/mattermost/callback":
                    result = await adapter_self._route_callback(headers, body)
                elif path == "/mm-command":
                    result = await adapter_self._route_slash_command(body)
                else:
                    writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                    await writer.drain()
                    writer.close()
                    return

                response_body = json.dumps(result).encode("utf-8")
                response = (
                    f"HTTP/1.1 200 OK\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(response_body)}\r\n\r\n"
                ).encode("utf-8") + response_body
                writer.write(response)
                await writer.drain()
                writer.close()
            except Exception:
                logger.exception("Unhandled error in callback server handler")
                try:
                    err_body = json.dumps({"ephemeral_text": "⚠️ Internal error"}).encode("utf-8")
                    err_resp = (
                        f"HTTP/1.1 200 OK\r\n"
                        f"Content-Type: application/json\r\n"
                        f"Content-Length: {len(err_body)}\r\n\r\n"
                    ).encode("utf-8") + err_body
                    writer.write(err_resp)
                    await writer.drain()
                except Exception:
                    pass
                writer.close()

        async def _serve() -> None:
            server = await _asyncio.start_server(
                _handler, host=adapter_self._callback_bind, port=adapter_self._callback_port,
            )
            adapter_self._callback_server = server
            logger.info(
                "MattermostApproval callback server on %s:%s "
                "(routes: /mattermost/callback, /mm-command) — dedicated thread/loop",
                adapter_self._callback_bind, adapter_self._callback_port,
            )
            async with server:
                await server.serve_forever()

        def _run_callback_loop() -> None:
            loop = _asyncio.new_event_loop()
            _asyncio.set_event_loop(loop)
            adapter_self._callback_loop = loop
            try:
                loop.run_until_complete(_serve())
            except (_asyncio.CancelledError, KeyboardInterrupt):
                pass  # 正常停机路径 — server 被主动关闭
            except Exception:
                if not getattr(adapter_self, "_closing", False):
                    logger.exception("Callback server loop crashed")
            finally:
                loop.close()

        thread = _threading.Thread(
            target=_run_callback_loop,
            name="mm-callback-server",
            daemon=True,
        )
        thread.start()
        adapter_self._callback_thread = thread

    # ══════════════════════════════════════════════════════════════════════
    # 路由: Interactive Message 回调
    # ══════════════════════════════════════════════════════════════════════

    async def _route_callback(self, headers: str, body: str) -> Dict[str, Any]:
        """处理 POST /mattermost/callback。"""
        signature = ""
        for line in headers.split("\r\n"):
            if line.lower().startswith("x-mattermost-signature:"):
                signature = line.split(":", 1)[1].strip()
                break

        if self._callback_secret:
            if not signature or not self._verify_signature(body.encode("utf-8"), signature):
                return {"ephemeral_text": "Unauthorized"}

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return {"ephemeral_text": "Invalid JSON"}

        return await self._handle_callback(payload)

    # ══════════════════════════════════════════════════════════════════════
    # 路由: Slash 指令
    # ══════════════════════════════════════════════════════════════════════

    async def _route_slash_command(self, body: str) -> Dict[str, Any]:
        """处理 POST /mm-command（/model + /new）。

        关键设计：
          Slash Command 的 HTTP response 以用户身份显示 ephemeral（MM 设计限制）。
          为避免用户头像发送 Bot 消息的困惑，HTTP response 返回空 ephemeral，
          所有可见内容通过 Bot API 发帖。

          响应解耦：卡片构建 + Bot API 发帖涉及多次网络往返（load_config、
          get_chat_info、get_current_model...），全部作为 followup 派回
          gateway 主 loop 后台执行，HTTP 立即返回空 ephemeral —
          避免斜杠指令在 agent 繁忙时长时间无响应。
        """
        from urllib.parse import unquote_plus
        params: Dict[str, str] = {}
        for pair in body.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                params[k] = unquote_plus(v)

        command = params.get("command", "").lstrip("/")
        channel_id = params.get("channel_id", "")
        user_id = params.get("user_id", "")
        # MM Slash Command payload 包含 root_id 字段！
        # - 在 Thread 中发送时，root_id = thread 的 root post ID
        # - 在 Channel 顶层发送时，root_id = 空字符串
        root_id = params.get("root_id", "") or None

        logger.info("Slash command: /%s user=%s channel=%s root_id=%s",
                    command, user_id[:8], channel_id[:8], root_id or "(channel-level)")

        # 校验权限（纯内存，同步执行）
        allowed_users = self._get_allowed_users()
        if allowed_users and user_id not in allowed_users:
            return {"response_type": "ephemeral", "text": "⛔ Unauthorized"}

        if command == "model":
            self._schedule_followup(
                self._handle_model_command(channel_id, user_id, root_id)
            )
            return {}
        elif command == "new":
            self._schedule_followup(
                self._handle_new_command(channel_id, user_id, root_id)
            )
            return {}

        return {"response_type": "ephemeral", "text": f"Unknown command: /{command}"}

    # ══════════════════════════════════════════════════════════════════════
    # Slash 指令处理
    # ══════════════════════════════════════════════════════════════════════

    # NOTE: _find_user_thread_root_id 已移除 — MM Slash Command payload
    # 原生包含 root_id 字段，无需 API 反查。

    async def _post_card_in_thread(
        self, channel_id: str, root_id: Optional[str], card: Dict[str, Any],
    ) -> Optional[str]:
        """通过 Bot API 在 thread 中发送 Interactive Message 卡片。返回 post_id。

        关键：message 留空，所有可见内容只在 props.attachments 中。
        如果 message 和 props.attachments 都有内容，MM 会重复显示。
        """
        attachments = card.get("attachments", [])

        payload: Dict[str, Any] = {
            "channel_id": channel_id,
            "message": "",  # 留空，避免与 props 重复显示
            "props": {"attachments": attachments},
        }

        if root_id:
            payload["root_id"] = root_id

        try:
            data = await self._api_post("posts", payload)
            if data and "id" in data:
                return data["id"]
            logger.error("Failed to post card: %s", data)
            return None
        except Exception as e:
            logger.error("Error posting card: %s", e)
            return None

    async def _handle_model_command(
        self, channel_id: str, user_id: str, root_id: Optional[str],
    ) -> Dict[str, Any]:
        """处理 /model Slash Command。

        root_id 来自 MM Slash Command payload：
          - Thread 中发送 → root_id = thread root post ID
          - Channel 顶层发送 → root_id = None
        """
        # 1. 获取可用模型（按 provider 分组）
        from .models import get_models_by_provider
        provider_groups = get_models_by_provider()

        # 2. 当前模型
        current_model = await self._get_current_model_for_session(channel_id, root_id)

        # 4. 渲染卡片（分组模式）
        callback_url = self._build_callback_url()
        card = render_model_selector_card(
            callback_url=callback_url,
            channel_id=channel_id,
            user_id=user_id,
            current_model=current_model,
            provider_groups=provider_groups,
        )

        # 5. 注入 session_key + provider 到按钮 context
        # user_id 必传：group/channel 层级 session 默认 per-user 隔离，
        # session key 带 user 后缀，缺它就写错 session。
        session_key = await self._build_session_key(channel_id, root_id, user_id)
        self._inject_model_context(card, session_key)

        # 6. Bot API 发帖到 thread（Bot 头像，非用户头像）
        post_id = await self._post_card_in_thread(channel_id, root_id, card)

        if post_id:
            logger.info("Model picker posted: session=%s post=%s groups=%d",
                        session_key, post_id, len(provider_groups))
            # 返回空 ephemeral — 所有可见内容在 Bot 帖子中
            return {}

        return {"response_type": "ephemeral", "text": "❌ 发送模型选择器失败，请稍后重试"}

    async def _handle_new_command(
        self, channel_id: str, user_id: str, root_id: Optional[str],
    ) -> Dict[str, Any]:
        """处理 /new Slash Command。

        root_id 来自 MM Slash Command payload：
          - Thread 中发送 → root_id = thread root post ID
          - Channel 顶层发送 → root_id = None
        """
        callback_url = self._build_callback_url()
        card = render_new_session_confirm_card(
            callback_url=callback_url,
            channel_id=channel_id,
            user_id=user_id,
        )

        session_key = await self._build_session_key(channel_id, root_id, user_id)
        self._inject_session_key(card, session_key)

        post_id = await self._post_card_in_thread(channel_id, root_id, card)

        if post_id:
            logger.info("New session confirm posted: session=%s post=%s", session_key, post_id)
            # 保存 post_id 供后续回调更新
            self._new_confirm_posts = getattr(self, "_new_confirm_posts", {})
            self._new_confirm_posts[session_key] = post_id
            # 返回空 ephemeral
            return {}

        return {"response_type": "ephemeral", "text": "❌ 发送确认卡片失败，请稍后重试"}

    # ══════════════════════════════════════════════════════════════════════
    # Session 上下文辅助
    # ══════════════════════════════════════════════════════════════════════

    async def _build_session_key(
        self, channel_id: str, root_id: Optional[str], user_id: Optional[str] = None,
    ) -> str:
        """构建 session_key — 直接调用上游 build_session_key() 单一真源。

        历史教训：手拼 f"agent:main:mattermost:{chat_type}:{channel_id}[:{root_id}]"
        无法覆盖上游规则（group/channel 层级默认 per-user 隔离 → key 带 user 后缀；
        thread 层级默认共享 → key 不带 user）。state.db 实测：
          agent:main:mattermost:channel:{chat_id}:{thread_id}
          agent:main:mattermost:group:{chat_id}:{user_id}
        改用上游函数后与 Gateway 完全一致，per-user 差异由上游逻辑处理。
        """
        chat_type = self._channel_type_cache.get(channel_id)
        if chat_type is None:
            try:
                info = await self.get_chat_info(channel_id)
                chat_type = info.get("type", "channel")
            except Exception:
                chat_type = "channel"
            self._channel_type_cache[channel_id] = chat_type

        try:
            from gateway.session import SessionSource, build_session_key
            from gateway.config import Platform
            from hermes_cli.config import load_config
            cfg = load_config()
            source = SessionSource(
                platform=Platform.MATTERMOST, chat_id=str(channel_id),
                chat_type=chat_type, user_id=user_id or None,
                thread_id=root_id or None,
            )
            return build_session_key(
                source,
                group_sessions_per_user=bool(cfg.get("group_sessions_per_user", True)),
                thread_sessions_per_user=bool(cfg.get("thread_sessions_per_user", False)),
            )
        except Exception:
            logger.warning("Mattermost: build_session_key fallback (upstream import failed)", exc_info=True)
            key = f"agent:main:mattermost:{chat_type}:{channel_id}"
            if root_id:
                key += f":{root_id}"
            return key

    async def _get_current_model_for_session(
        self, channel_id: str, root_id: Optional[str],
    ) -> str:
        """获取当前 session 使用的模型名。"""
        session_key = await self._build_session_key(channel_id, root_id)

        # 先查 session override
        try:
            from gateway.run import _gateway_runner_ref
            runner = _gateway_runner_ref()
            if runner:
                override = runner._session_model_overrides.get(session_key, {})
                if override:
                    return override.get("model", "")
        except Exception:
            pass

        # 回退到 config 默认
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
            model_cfg = cfg.get("model", {})
            if isinstance(model_cfg, dict):
                return model_cfg.get("default", "")
        except Exception:
            pass

        return ""

    def _inject_model_context(
        self, card: Dict[str, Any], session_key: str,
    ) -> None:
        """在模型选择卡片 context 中注入 session_key + provider_name。

        支持 select 和 button 两种 action 类型：
        - select: context 是共享的，selected_option 由 MM 在回调时添加
        - button: 每个 button 的 context 独立，包含 model_id 和 provider_name
        """
        from .models import _resolve_provider_for_model

        for att in card.get("attachments", []):
            for action in att.get("actions", []):
                ctx = action.get("integration", {}).get("context", {})

                # select 类型：context 是共享的，不需要 model_id/provider_name
                # 这些在回调时通过 selected_option 获取
                if action.get("type") == "select":
                    ctx["session_key"] = session_key
                    continue

                # button 类型：每个按钮的 context 包含 model_id
                model_id = ctx.get("model_id", "")
                ctx["session_key"] = session_key
                if model_id:
                    ctx["provider_name"] = _resolve_provider_for_model(model_id)

    def _inject_session_key(self, card: Dict[str, Any], session_key: str) -> None:
        """在卡片按钮 context 中注入 session_key。"""
        for att in card.get("attachments", []):
            for action in att.get("actions", []):
                ctx = action.get("integration", {}).get("context", {})
                ctx["session_key"] = session_key

    # ══════════════════════════════════════════════════════════════════════
    # 回调处理（Interactive Message 按钮）
    # ══════════════════════════════════════════════════════════════════════

    def _is_duplicate_click(self, payload: Dict[str, Any]) -> bool:
        """post_id+action 去重 — 防双击（首次点击 False，重复点击 True）。

        MM 日志曾录得 2ms 内双击实录：去重必须做在 HTTP 响应之前，
        否则两个请求都会执行副作用并各自返回一次 update。
        """
        post_id = str(payload.get("post_id", ""))
        action = str(payload.get("context", {}).get("action", ""))
        if not post_id or not action:
            return False
        key = f"{post_id}:{action}"
        with self._dedup_lock:
            if key in self._callback_dedup:
                logger.info("Duplicate click suppressed: %s", key)
                return True
            self._callback_dedup[key] = True
            # 简单容量控制：超过 512 条清空（post 维度幂等，清空无害）
            if len(self._callback_dedup) > 512:
                self._callback_dedup.clear()
                self._callback_dedup[key] = True
        return False

    async def _handle_callback(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """处理按钮回调 — 审批 + 模型切换 + 会话重置 + Clarify。

        响应解耦原则：
          1. 纯内存操作 → 同步完成后立即返回最终卡片状态；
          2. 耗时操作（API 调用/主 loop 状态）→ 立即返回「⏳ 已收到」
             清空按钮，耗时部分作为 followup 派回 gateway 主 loop 后台执行。
        """
        context = payload.get("context", {})
        action = context.get("action", "")

        # ── 双击去重（最先执行）──
        if self._is_duplicate_click(payload):
            return {"update": {"message": "✅ 已处理本次点击", "props": {}}}

        # ── Clarify 选择 ──
        if action == "cmd_clarify_choice":
            return self._handle_clarify_choice_callback(payload)

        # ── Clarify「其他」─→
        if action == "cmd_clarify_other":
            return self._handle_clarify_other_callback(payload)

        # ── 模型切换 ──
        if action == "cmd_model_switch":
            return await self._handle_model_switch_callback(payload)

        # ── 会话重置确认 ──
        if action == "cmd_new_confirm":
            return await self._handle_new_confirm_callback(payload)

        # ── 会话重置取消 ──
        if action == "cmd_new_cancel":
            return {"update": {"message": "❌ 已取消重置", "props": {}}}

        # ── DM 审批 ──
        session_key = context.get("session_key", "")
        if not action or not session_key:
            return {"ephemeral_text": "Invalid callback data"}

        user_id = payload.get("user_id", "")
        allowed_users = self._get_allowed_users()
        if allowed_users and user_id not in allowed_users:
            return {"ephemeral_text": "Unauthorized"}

        choice_map = {
            "approve_once": "once",
            "approve_session": "session",
            "approve_always": "always",
            "deny": "deny",
        }
        choice = choice_map.get(action)
        if not choice:
            return {"ephemeral_text": f"Unknown action: {action}"}

        # ── 并发点击防护：每个审批按 session_key 串行化 ──
        # 回调服务器跑在独立线程，用户快速双击时两个请求并发进入。
        # 第一层防护：_is_duplicate_click（post_id+action 去重，入口已做）。
        # 第二层：resolve_gateway_approval 本身线程安全且幂等
        # （count==0 表示已处理），此处只做结果标注。
        count = resolve_gateway_approval(session_key, choice)
        if count == 0:
            # 审批已被处理（重复点击）— 仍然返回 update 清空卡片按钮
            # 防止用户继续点击看到 "No pending approval found" 错误
            return {
                "update": {
                    "message": "⚠️ 此审批已处理",
                    "props": {
                        "attachments": [{
                            "actions": [],  # 清空按钮
                        }],
                    },
                },
            }

        # 恢复原始 Topic 的 typing 指示器
        # slash_commands.py 中 /approve 和 /deny 命令会自动恢复 typing，
        # 但 DM 按钮回调路径不会 — 需要从按钮 context 取出原始 chat_id 手动恢复。
        source_chat_id = context.get("chat_id", "")
        if source_chat_id:
            self.resume_typing_for_chat(source_chat_id)

        label_map = {
            "once": "✅ Approved — Allow Once",
            "session": "✅ Approved — Allow Session",
            "always": "✅ Approved — Always Allow",
            "deny": "❌ Denied",
        }
        cmd = context.get("command", "")
        reason = context.get("reason", "")
        cmd_display = f"\n```\n{cmd}\n```" if cmd else ""
        reason_display = f"\n**Reason:** {reason}" if reason else ""
        _update_msg = f"{label_map.get(choice, choice)}{reason_display}{cmd_display}"

        logger.info("Approval callback: %s → %s (session %s), %d resolved",
                     action, choice, session_key[:40], count)

        # update 响应替换卡片内容，同时清空 actions 防止重复点击
        # MM 的 update 只替换 message+props，按钮仍在 — 必须在 props 中返回空 actions
        return {
            "update": {
                "message": _update_msg,
                "props": {
                    "attachments": [{
                        "actions": [],  # 清空按钮，防止 Deny 后重复点击
                    }],
                },
            },
        }

    # ── Clarify 回调处理 ──
    # 两个 clarify 回调均为纯内存操作（resolve/mark + 渲染），
    # 同步执行、立即返回最终卡片状态 — 无需 followup。

    def _handle_clarify_choice_callback(
        self, payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """处理 Clarify 选项按钮回调。

        用户点击某个选项 → resolve_gateway_clarify → 更新卡片为确认状态。
        """
        from tools.clarify_gateway import resolve_gateway_clarify

        context = payload.get("context", {})
        clarify_id = context.get("clarify_id", "")
        choice_value = context.get("choice_value", "")

        if not clarify_id:
            logger.warning("Clarify choice callback: missing clarify_id")
            return {"ephemeral_text": "⚠️ Invalid clarify callback"}

        # 从 send_clarify 缓存中取回原始问题与全部选项，用于保留历史
        meta = getattr(self, "_clarify_posts", {}).get(clarify_id, {})
        question = meta.get("question", "")
        choices = meta.get("choices")

        resolved = resolve_gateway_clarify(clarify_id, choice_value)
        if not resolved:
            logger.warning(
                "Clarify choice callback: resolve failed (already resolved?) clarify_id=%s",
                clarify_id,
            )
            return {"update": {"message": "⚠️ 此问题已过期", "props": {}}}

        logger.info(
            "Clarify choice callback: resolved clarify_id=%s choice=%r",
            clarify_id, choice_value,
        )

        # 更新原始卡片为确认状态（保留原问题 + 全部选项 + 选择结果）
        card = render_clarify_choice_confirmed_card(question, choices, choice_value)
        return {
            "update": {
                "message": "",
                "props": card,
            },
        }

    def _handle_clarify_other_callback(
        self, payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """处理 Clarify「其他」按钮回调。

        标记 clarify 进入文本捕获模式 → 用户下一条消息被 Gateway 拦截为回答。
        """
        from tools.clarify_gateway import mark_awaiting_text

        context = payload.get("context", {})
        clarify_id = context.get("clarify_id", "")

        if not clarify_id:
            logger.warning("Clarify other callback: missing clarify_id")
            return {"ephemeral_text": "⚠️ Invalid clarify callback"}

        # 从 send_clarify 缓存取回原问题与选项，保留历史
        meta = getattr(self, "_clarify_posts", {}).get(clarify_id, {})
        question = meta.get("question", "")
        choices = meta.get("choices")

        ok = mark_awaiting_text(clarify_id)
        if not ok:
            logger.warning(
                "Clarify other callback: mark_awaiting_text failed clarify_id=%s",
                clarify_id,
            )

        logger.info("Clarify other callback: awaiting text clarify_id=%s", clarify_id)

        # 更新原始卡片为「请输入」提示（保留原问题 + 原选项）
        card = render_clarify_other_prompt_card(question, choices)
        return {
            "update": {
                "message": "",
                "props": card,
            },
        }

    # ── 模型切换回调（立即响应 + followup）──

    async def _model_switch_followup(
        self, session_key: str, model_id: str, provider_name: str,
        channel_id: str, root_post_id: str,
    ) -> None:
        """模型切换的耗时收尾 — 在 gateway 主 loop 后台执行。

        完成后用 Bot API 更新卡片为最终结果（成功/失败）。
        不阻塞回调 HTTP 响应 — 用户点击后立刻收到过 ack。
        """
        old_model = self._get_current_model_from_key(session_key)
        success, message = await self._switch_session_model(
            session_key, model_id, provider_name,
        )

        if success:
            old_display = old_model.split("/", 1)[-1] if "/" in old_model else old_model
            new_display = model_id.split("/", 1)[-1] if "/" in model_id else model_id
            update = {
                "message": (
                    f"✅ 模型已切换: {old_display or '(default)'} → {new_display}\n"
                    f"💡 重新选择请输入 `/model`"
                ),
                "props": {},
            }
        else:
            update = {"message": f"❌ 切换失败: {message}", "props": {}}

        try:
            await self._api("PUT", f"posts/{root_post_id}/patch",
                            {"message": update.get("message", ""), "props": update.get("props", {})})
        except Exception:
            logger.error(
                "Model switch followup: failed to patch post %s",
                root_post_id, exc_info=True,
            )
        logger.info(
            "Model switch followup done: session=%s model=%s success=%s",
            session_key, model_id, success,
        )

    async def _handle_model_switch_callback(
        self, payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """处理模型选择回调（支持下拉列表 select + 按钮 button）。

        Select 下拉列表：context 中包含 selected_option 字段（值为 option.value）
        Button 按钮：context 中包含 model_id 字段

        响应解耦：HTTP 立即返回「⏳ 已收到」并清空按钮，实际切换 +
        卡片最终更新作为 followup 派回 gateway 主 loop 后台执行。
        """
        context = payload.get("context", {})

        # 兼容 select 和 button 两种模式
        model_id = context.get("selected_option", "") or context.get("model_id", "")
        session_key = context.get("session_key", "")
        provider_name = context.get("provider_name", "")
        user_id = payload.get("user_id", "")
        post_id = payload.get("post_id", "")
        channel_id = payload.get("channel_id", "")

        logger.info(
            "Model switch callback: model=%s session=%s provider=%s",
            model_id, session_key, provider_name,
        )

        allowed_users = self._get_allowed_users()
        if allowed_users and user_id not in allowed_users:
            return {"ephemeral_text": "Unauthorized"}

        if not model_id or not session_key:
            return {"ephemeral_text": "Missing model_id or session context"}

        # 如果 provider_name 为空（select 模式可能没有注入），从 model_id 解析
        if not provider_name:
            from .models import _resolve_provider_for_model
            provider_name = _resolve_provider_for_model(model_id)

        # 耗时部分派回主 loop 后台执行，HTTP 先 ack
        if post_id:
            self._schedule_followup(
                self._model_switch_followup(
                    session_key, model_id, provider_name, channel_id, post_id,
                )
            )
            return {
                "update": {
                    "message": f"⏳ 正在切换到 {model_id}，请稍候...",
                    "props": {},
                },
            }

        # post_id 缺失（异常场景）— 降级为同步等待，保证功能不丢
        old_model = self._get_current_model_from_key(session_key)
        success, message = await self._switch_session_model(
            session_key, model_id, provider_name,
        )
        if success:
            old_display = old_model.split("/", 1)[-1] if "/" in old_model else old_model
            new_display = model_id.split("/", 1)[-1] if "/" in model_id else model_id
            return {
                "update": {
                    "message": f"✅ 模型已切换: {old_display or '(default)'} → {new_display}",
                    "props": {},
                },
            }
        return {"ephemeral_text": f"切换失败: {message}"}

    # ── 会话重置回调（立即响应 + followup）──

    async def _new_confirm_followup(
        self, session_key: str, root_post_id: str,
    ) -> None:
        """会话重置的耗时收尾 — 在 gateway 主 loop 后台执行。"""
        success, message = await self._reset_session(session_key)
        update = (
            {"message": "✅ 会话已重置，新会话已创建，对话上下文已清空。", "props": {}}
            if success
            else {"message": f"❌ 重置失败: {message}", "props": {}}
        )
        try:
            await self._api("PUT", f"posts/{root_post_id}/patch",
                            {"message": update.get("message", ""), "props": update.get("props", {})})
        except Exception:
            logger.error(
                "New confirm followup: failed to patch post %s",
                root_post_id, exc_info=True,
            )
        logger.info(
            "New confirm followup done: session=%s success=%s", session_key, success,
        )

    async def _handle_new_confirm_callback(
        self, payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """处理会话重置确认按钮回调。

        响应解耦：HTTP 立即返回「⏳ 已收到」并清空按钮，实际重置 +
        卡片最终更新作为 followup 派回 gateway 主 loop 后台执行。
        """
        context = payload.get("context", {})
        session_key = context.get("session_key", "")
        user_id = payload.get("user_id", "")
        post_id = payload.get("post_id", "")

        allowed_users = self._get_allowed_users()
        if allowed_users and user_id not in allowed_users:
            return {"ephemeral_text": "Unauthorized"}

        if not session_key:
            return {"ephemeral_text": "Missing session context"}

        # 耗时部分派回主 loop 后台执行，HTTP 先 ack
        if post_id:
            self._schedule_followup(
                self._new_confirm_followup(session_key, post_id)
            )
            return {
                "update": {
                    "message": "⏳ 正在重置会话，请稍候...",
                    "props": {},
                },
            }

        # post_id 缺失（异常场景）— 降级为同步等待，保证功能不丢
        success, message = await self._reset_session(session_key)
        if success:
            # 只在 message 中放内容，props 清空避免重复
            return {
                "update": {
                    "message": "✅ 会话已重置，新会话已创建，对话上下文已清空。",
                    "props": {},
                },
            }
        return {"ephemeral_text": f"重置失败: {message}"}

    # ── DM 审批发送 ──

    async def send_exec_approval(
        self,
        chat_id: str,
        command: str,
        session_key: str,
        description: str = "dangerous command",
        metadata: Optional[Dict[str, Any]] = None,
        user_id: Optional[str] = None,
        allow_permanent: bool = True,
        allow_session: bool = True,
        smart_denied: bool = False,
    ) -> SendResult:
        """发送按钮式审批提示到用户 DM.

        Bot API 创建的帖子 integration 字段虽被 API 响应剥离，
        但数据库中完整保留，MM 服务端处理按钮点击时从 DB 读取，
        因此 Bot API + DM 方式可正常触发回调。

        Args:
            allow_permanent: True → 显示 "Always Allow" 永久授权按钮
            allow_session: True → 显示 "Allow Session" 按钮
            smart_denied: True → 追加 owner override 提示（仅本次生效）
        """
        if smart_denied:
            description += "（Owner override，仅本次生效）"
        if not user_id:
            # 精准定位发起者：先用 build_source 记录的最近发言信息。
            # 多用户频道（public/group）下 members 反查会误发管理员，
            # 只有追踪未命中时才回退到旧逻辑。
            user_id = self._resolve_approval_requester(chat_id, metadata)
        if not user_id:
            # 兜底：反查 DM channel members（仅 DM 场景可靠；追踪命中的正常
            # 路径不会走到这里）。
            user_id = await self._get_user_id_from_channel(chat_id)
        if not user_id:
            return SendResult(
                success=False,
                error="Cannot send DM approval without user_id",
            )

        try:
            # 1. 获取/创建 DM channel
            dm_channel_id = await self._get_or_create_dm(user_id)
            if not dm_channel_id:
                return SendResult(
                    success=False,
                    error="Failed to create DM channel",
                )

            # 2. 构建 callback URL
            callback_url = self._build_callback_url()

            cmd_preview = (
                command[:3800] + "..." if len(command) > 3800 else command
            )

            # 3. 构建 Interactive Message
            # ── 基础按钮（始终显示）──
            base_actions = [
                {
                    "id": "approveonce",
                    "name": "Allow Once",
                    "type": "button",
                    "style": "primary",
                    "integration": {
                        "url": callback_url,
                        "context": {
                            "action": "approve_once",
                            "session_key": session_key,
                            "command": command,
                            "reason": description,
                            "chat_id": chat_id,
                        },
                    },
                },
            ]
            # ── Session 授权按钮（allow_session=True 时显示）──
            if allow_session:
                base_actions.append({
                    "id": "approvesession",
                    "name": "Allow Session",
                    "type": "button",
                    "integration": {
                        "url": callback_url,
                        "context": {
                            "action": "approve_session",
                            "session_key": session_key,
                            "command": command,
                            "reason": description,
                            "chat_id": chat_id,
                        },
                    },
                })
            # ── 永久授权按钮（仅 allow_permanent=True 时显示）──
            if allow_permanent and not smart_denied:
                base_actions.append({
                    "id": "approvealways",
                    "name": "Always Allow",
                    "type": "button",
                    "integration": {
                        "url": callback_url,
                        "context": {
                            "action": "approve_always",
                            "session_key": session_key,
                            "command": command,
                            "reason": description,
                            "chat_id": chat_id,
                        },
                    },
                })
            base_actions.append({
                "id": "deny",
                "name": "Deny",
                "type": "button",
                "style": "danger",
                "integration": {
                    "url": callback_url,
                    "context": {
                        "action": "deny",
                        "session_key": session_key,
                        "command": command,
                        "reason": description,
                        "chat_id": chat_id,
                    },
                },
            })

            attachment = {
                "fallback": f"⚠️ 危险命令需要审批: {command[:100]}",
                "color": "#ff9900",
                "text": (
                    f"```\n{cmd_preview}\n```\n"
                    f"**Reason:** {description}\n\n"
                    f"请点击下方按钮审批或拒绝此操作。"
                ),
                "actions": base_actions,
            }

            # 4. 通过 Bot API 发送到 DM（props.attachments）
            payload = {
                "channel_id": dm_channel_id,
                "message": "⚠️ 危险命令需要审批",
                "props": {"attachments": [attachment]},
            }

            data = await self._api_post("posts", payload)
            if not data or "id" not in data:
                return SendResult(
                    success=False, error="Failed to send DM approval post"
                )

            # 5. 在原频道/Thread 发送简短提示（带上 metadata 确保路由到正确 Thread）
            await self.send(
                chat_id,
                "⏳ 已向您发送私信，请在 DM 中审批危险命令。",
                metadata=metadata,
            )

            return SendResult(success=True, message_id=data.get("id"))

        except Exception as e:
            logger.error(
                "[Mattermost] send_exec_approval failed: %s",
                e,
                exc_info=True,
            )
            return SendResult(success=False, error=str(e))

    # ══════════════════════════════════════════════════════════════════════
    # 核心操作：模型切换 + 会话重置
    # ══════════════════════════════════════════════════════════════════════

    def _get_current_model_from_key(self, session_key: str) -> str:
        """从 session override 或 config 获取当前模型名。"""
        try:
            from gateway.run import _gateway_runner_ref
            runner = _gateway_runner_ref()
            if runner:
                override = runner._session_model_overrides.get(session_key, {})
                if override:
                    return override.get("model", "")
        except Exception:
            pass

        try:
            from hermes_cli.config import load_config
            cfg = load_config()
            return cfg.get("model", {}).get("default", "")
        except Exception:
            return ""

    async def _switch_session_model(
        self, session_key: str, model_id: str, provider_name: str,
    ) -> Tuple[bool, str]:
        """执行模型切换 — 直接从 custom_providers 配置构建 session override。

        绕过 switch_model() 的复杂路由逻辑，直接读取 provider 配置。
        这确保 api_key 正确解析、响应速度快、provider 正确匹配。
        """
        try:
            from gateway.run import _gateway_runner_ref
            runner = _gateway_runner_ref()
            if not runner:
                return False, "GatewayRunner not available"

            # 从 custom_providers 配置直接解析 provider 连接信息
            from .models import resolve_provider_config
            prov_cfg = resolve_provider_config(provider_name)

            # 先记录旧模型（必须在写入 override 之前）
            old_model = self._get_current_model_from_key(session_key) or "(default)"

            if prov_cfg:
                # 直接构建 override — 无需调用 switch_model
                runner._session_model_overrides[session_key] = {
                    "model": model_id,
                    "provider": prov_cfg["provider"],
                    "base_url": prov_cfg["base_url"],
                    "api_key": prov_cfg["api_key"],
                    "api_mode": prov_cfg["api_mode"],
                }
            else:
                # provider 不在 custom_providers 中 — 回退到 switch_model
                logger.warning(
                    "Provider '%s' not in custom_providers, falling back to switch_model for %s",
                    provider_name, model_id,
                )
                from hermes_cli.config import load_config
                cfg = load_config()
                model_cfg = cfg.get("model", {})
                user_provs = cfg.get("providers")
                try:
                    from hermes_cli.config import get_compatible_custom_providers
                    custom_provs = get_compatible_custom_providers(cfg)
                except Exception:
                    custom_provs = cfg.get("custom_providers")

                override = runner._session_model_overrides.get(session_key, {})
                current_provider = override.get("provider", model_cfg.get("provider", "openrouter"))
                current_model = override.get("model", model_cfg.get("default", ""))
                current_base_url = override.get("base_url", model_cfg.get("base_url", ""))
                current_api_key = override.get("api_key", "")

                from hermes_cli.model_switch import switch_model
                result = switch_model(
                    raw_input=model_id,
                    current_provider=current_provider,
                    current_model=current_model,
                    current_base_url=current_base_url,
                    current_api_key=current_api_key,
                    user_providers=user_provs,
                    custom_providers=custom_provs,
                    explicit_provider=provider_name or None,
                )

                if not result.success:
                    return False, result.error_message or "switch_model failed"

                runner._session_model_overrides[session_key] = {
                    "model": result.new_model,
                    "provider": result.target_provider,
                    "base_url": result.base_url,
                    "api_key": result.api_key,
                    "api_mode": result.api_mode,
                }

            # 清除缓存的 agent
            runner._evict_cached_agent(session_key)

            # 注入 model note — 让 LLM 知道自己被切换了
            # 这样 LLM 回答"当前模型"时会正确报告新模型
            if not hasattr(runner, "_pending_model_notes"):
                runner._pending_model_notes = {}
            _verify = runner._session_model_overrides.get(session_key, {})
            _new_provider = _verify.get("provider", provider_name)
            runner._pending_model_notes[session_key] = (
                f"[Note: model was just switched from {old_model} to {model_id} "
                f"via {_new_provider}. "
                f"Adjust your self-identification accordingly.]"
            )

            # 验证 override 是否真的写入了
            verify = runner._session_model_overrides.get(session_key)
            if verify:
                logger.info(
                    "Model switched: session=%s → %s provider=%s api_key_len=%d override_verified=YES",
                    session_key, model_id,
                    verify.get("provider", "?"),
                    len(verify.get("api_key", "")),
                )
            else:
                logger.error(
                    "Model switch FAILED to persist: session=%s model=%s override_keys=%s",
                    session_key, model_id,
                    list(runner._session_model_overrides.keys())[:5],
                )
            return True, model_id

        except Exception as e:
            logger.error("Model switch failed: %s", e, exc_info=True)
            return False, str(e)

    async def _reset_session(self, session_key: str) -> Tuple[bool, str]:
        """执行会话重置，通过 GatewayRunner。"""
        try:
            from gateway.run import _gateway_runner_ref
            runner = _gateway_runner_ref()
            if not runner:
                return False, "GatewayRunner not available"

            # 清除 session override
            runner._session_model_overrides.pop(session_key, None)

            # 清除缓存 agent
            runner._evict_cached_agent(session_key)

            # 重置 session store
            if hasattr(runner, "session_store"):
                runner.session_store.reset_session(session_key)

            # 清除 reasoning override
            if hasattr(runner, "_set_session_reasoning_override"):
                runner._set_session_reasoning_override(session_key, None)

            # 清除 pending model notes
            if hasattr(runner, "_pending_model_notes"):
                runner._pending_model_notes.pop(session_key, None)

            # 清除 session boundary security state
            if hasattr(runner, "_clear_session_boundary_security_state"):
                runner._clear_session_boundary_security_state(session_key)

            logger.info("Session reset: session=%s", session_key)
            return True, "Session reset"

        except Exception as e:
            logger.error("Session reset failed: %s", e, exc_info=True)
            return False, str(e)

    # ══════════════════════════════════════════════════════════════════════
    # （send_model_picker 已移除 — v2026.9.7 对齐）
    # 上游 slash_commands_model._model_listing_reply 通过
    # `getattr(type(adapter), "send_model_picker", None)` 探测交互 picker 能力。
    # 旧插件覆写永远返回失败 SendResult，会让上游误判「picker 可用」后收到失败
    # 并中断文本降级链路。删除后 /model 走上游统一实现；插件自己的 /model
    # Slash Command 卡片入口不受影响。
    # ══════════════════════════════════════════════════════════════════════

    # ══════════════════════════════════════════════════════════════════════
    # 回调服务器辅助方法
    # ══════════════════════════════════════════════════════════════════════

    async def _stop_callback_server(self) -> None:
        """停止 callback server（独立线程 + 独立 loop）."""
        server = self._callback_server
        loop = self._callback_loop
        if server is not None and loop is not None and loop.is_running():
            # 从外部线程安全地请求关闭独立 loop 中的 server
            async def _shutdown(srv):
                srv.close()
                await srv.wait_closed()
            fut = asyncio.run_coroutine_threadsafe(_shutdown(server), loop)
            try:
                await asyncio.wrap_future(fut)
            except Exception:
                logger.debug("Callback server shutdown raced with loop close", exc_info=True)
        self._callback_server = None
        self._callback_loop = None
        if self._callback_thread is not None:
            self._callback_thread.join(timeout=5.0)
            self._callback_thread = None
        logger.info("Mattermost callback server stopped")

    def _verify_signature(self, body: bytes, signature: str) -> bool:
        """HMAC-SHA256 校验 Mattermost 回调签名."""
        import hmac as _hmac
        import hashlib as _hashlib

        if not self._callback_secret:
            return True

        expected = _hmac.new(
            self._callback_secret.encode("utf-8"),
            body,
            _hashlib.sha256,
        ).hexdigest()

        return _hmac.compare_digest(expected, signature)

    # ══════════════════════════════════════════════════════════════════════
    # 父类方法覆写（修复内置适配器的 Bug）
    # ══════════════════════════════════════════════════════════════════════

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None):
        """覆写父类：将 typing 指示器发送到正确的 Thread 内。

        内置 MattermostAdapter.send_typing() 只传 channel_id，
        在 reply_mode=thread 时 typing 指示器错误地显示在频道而非 Thread 内。
        Mattermost API 支持 parent_id 参数指定 Thread。
        """
        body: Dict[str, Any] = {"channel_id": chat_id}
        if metadata and metadata.get("thread_id"):
            body["parent_id"] = metadata["thread_id"]
        await self._api_post(f"users/{self._bot_user_id}/typing", body)

    # ══════════════════════════════════════════════════════════════════════
    # 生命周期覆写（启动/停止回调服务器）
    # ══════════════════════════════════════════════════════════════════════
    # WebSocket 覆写 — 心跳优化 30s→15s（替代 P6 shell patch）
    # ══════════════════════════════════════════════════════════════════════

    async def _ws_connect_and_listen(self) -> None:
        """Override: use heartbeat=15s instead of upstream's 30s.

        Mattermost server's idle timeout is ~50s; with heartbeat=30s the
        first ping may arrive *after* the server closes the connection
        (code 258).  15s keeps the connection alive without patching
        gateway source code.
        """
        import re as _re
        import json as _json

        ws_url = _re.sub(r"^http", "ws", self._base_url) + "/api/v4/websocket"
        logger.info("Mattermost: connecting to %s (heartbeat=15s)", ws_url)

        import aiohttp
        self._ws = await self._session.ws_connect(
            ws_url, heartbeat=15.0,
        )

        # Authenticate via the WebSocket.
        auth_msg = {
            "seq": 1,
            "action": "authentication_challenge",
            "data": {"token": self._token},
        }
        await self._ws.send_json(auth_msg)
        logger.info("Mattermost: WebSocket connected and authenticated (heartbeat=15s)")

        async for raw_msg in self._ws:
            if self._closing:
                return

            if raw_msg.type in {
                raw_msg.type.TEXT,
                raw_msg.type.BINARY,
            }:
                try:
                    event = _json.loads(raw_msg.data)
                except (_json.JSONDecodeError, TypeError):
                    continue
                await self._handle_ws_event(event)
            elif raw_msg.type in {
                raw_msg.type.ERROR,
                raw_msg.type.CLOSE,
                raw_msg.type.CLOSING,
                raw_msg.type.CLOSED,
            }:
                logger.info("Mattermost: WebSocket closed (%s)", raw_msg.type)
                break

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Connect to Mattermost — 覆写父类，追加回调服务器启动."""
        # 先调用内置 connect（认证 + WebSocket）
        result = await super().connect(is_reconnect=is_reconnect)
        if not result:
            return False

        # 启动审批 + Slash 指令回调服务器（独立线程，fire-and-forget；
        # 端口冲突等启动失败由 _run_callback_loop 内部记录日志）
        await self._start_callback_server()
        return True

    async def disconnect(self) -> None:
        """Disconnect from Mattermost — 覆写父类，追加回调服务器停止."""
        # 先停止回调服务器
        await self._stop_callback_server()

        # 再调用内置 disconnect
        await super().disconnect()

    # ══════════════════════════════════════════════════════════════════════
    # Thread root_id 解析 — 缓存版覆写（v2026.9.7 对齐）
    # ══════════════════════════════════════════════════════════════════════
    # 上游 _post_message 在 thread 模式下对每个候选（reply_to 或
    # metadata.thread_id）调用 self._resolve_root_id(...)。覆写为缓存版 +
    # 签名对齐上游（post_id: str -> str）：None/异常时回退返回 post_id 本身，
    # 与上游「不解析、按根帖处理」的语义一致，避免 Optional 返回值泄漏进
    # 上游调用链导致 thread 路由静默失效。

    async def _resolve_root_id(self, post_id: str) -> str:
        """Resolve a post_id to the thread root_id for Mattermost (cached).

        Mattermost requires root_id to be the *root* post of a thread.
        If the post is a reply (has its own root_id), we must use that
        root_id instead. Using a reply's own ID as root_id causes
        "Invalid RootId parameter" errors.

        Results are cached for 5 minutes to avoid repeated API calls
        (especially important when WebSocket is unstable and API calls
        frequently time out). Failures fall back to returning post_id
        itself — matching the upstream contract (str in, str out).
        """
        if not post_id:
            return post_id

        # ── 缓存命中 ──
        import time as _time
        cached = self._root_id_cache.get(post_id)
        if cached is not None:
            cached_root, cached_ts = cached
            if _time.monotonic() - cached_ts < self._root_id_cache_ttl:
                logger.debug(
                    "Mattermost: _resolve_root_id — cache hit for post=%s → %s",
                    post_id, cached_root,
                )
                return cached_root
            # 过期，清除
            del self._root_id_cache[post_id]

        # ── API 调用 ──
        try:
            data = await self._api_get(f"posts/{post_id}")
        except Exception:
            logger.warning(
                "Mattermost: _resolve_root_id — API call failed for post=%s, "
                "falling back to post_id itself",
                post_id, exc_info=True,
            )
            return post_id

        if not data:
            logger.warning(
                "Mattermost: _resolve_root_id — API returned no data for post=%s, "
                "falling back to post_id itself",
                post_id,
            )
            return post_id

        root_id = data.get("root_id")
        # root_id can be "" (empty string = this post IS the root).
        # Only use data["root_id"] when it's a non-empty string pointing
        # to a different post.
        if isinstance(root_id, str) and root_id:
            logger.info(
                "Mattermost: _resolve_root_id — input=%s root_id=%s (reply in thread → use root)",
                post_id, root_id,
            )
            result = root_id
        else:
            # root_id is "" or missing → this post IS a root-level post.
            # IMPORTANT: this means the triggering message was sent in the
            # CHANNEL (not inside an existing Thread). Hermes will create a
            # NEW thread rooted at this post. If the user expected this to
            # appear in an existing Thread, check the Mattermost UI — the
            # message was likely typed in the channel main input, not the
            # Thread reply box.
            logger.info(
                "Mattermost: _resolve_root_id — input=%s is_root=True (root_id=%r — "
                "CHANNEL-LEVEL post, NOT in an existing Thread)",
                post_id, root_id,
            )
            result = post_id

        # 写入缓存
        self._root_id_cache[post_id] = (result, _time.monotonic())
        # 清理过期条目（简单 LRU：每次解析后清理）
        now = _time.monotonic()
        expired = [
            k for k, (_, ts) in self._root_id_cache.items()
            if now - ts >= self._root_id_cache_ttl
        ]
        for k in expired:
            del self._root_id_cache[k]

        return result

    # ══════════════════════════════════════════════════════════════════════
    # edit_message() — 轻量覆写（v2026.9.7 对齐）
    # ══════════════════════════════════════════════════════════════════════
    # 上游已统一 _api("PUT") 并带 30s timeout，旧的「_api_put 无限挂起」
    # bug 已不存在。保留覆写仅为了：(1) 接受 gateway 各调用方传入的
    # metadata kwarg（上游 edit_message 签名没有该参数）；(2) 空内容防护
    # （MM API 对空 message 返回 400 并刷错误日志）。内容非空时直接委托上游。

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Edit an existing post — metadata kwarg 兼容 + 空内容防护."""
        _ = metadata, finalize  # 接受但不使用（metadata 由上游 thread 路由处理）
        if not content or not content.strip():
            logger.debug(
                "Mattermost: edit_message skipped — empty content for post=%s",
                message_id,
            )
            return SendResult(success=False, error="empty message")

        return await super().edit_message(chat_id, message_id, content, finalize=finalize)

    async def _get_thread_root_id(self, reply_to: Optional[str]) -> Optional[str]:
        """Resolve reply_to → thread root_id when in thread mode."""
        if reply_to and self._reply_mode == "thread":
            return await self._resolve_root_id(reply_to)
        return None

    # ══════════════════════════════════════════════════════════════════════
    # send() — footer 拦截 + 委托上游（v2026.9.7 对齐）
    # ══════════════════════════════════════════════════════════════════════
    # 旧版手搓了完整发送链路（root_id 解析、metadata 降级、分块、发帖），
    # 与上游 _post_message() 完全重复且缺少 mentions 抑制与 broken-thread-root
    # fallback。v2026.9.7 起仅保留插件特色 —— footer 行编辑合并到上一条消息，
    # 其余全部委托上游 send()（thread 路由经缓存的 _resolve_root_id 仍然生效）。

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """footer 拦截 + 委托父类 send()。"""
        if not content:
            return SendResult(success=True)

        # ── Footer 拦截：编辑上一条消息而非独立发帖 ──
        if self._is_footer_line(content):
            tracked = self._tracked_posts.get(chat_id)
            if tracked:
                post_id, _prev_content = tracked
                # 实时拉取当前帖子内容（流式模式下 send() 收到的 content 不完整）
                current = await self._api_get(f"posts/{post_id}")
                current_text = current.get("message", "") if isinstance(current, dict) else ""
                if current_text:
                    footer_text = content.replace(" · ", " ")
                    footer_md = f"`── {footer_text} ──`"
                    edited = f"{current_text}\n\n{footer_md}"
                    # 使用覆写的 edit_message（带 timeout 和错误处理）
                    edit_result = await self.edit_message(
                        chat_id=chat_id,
                        message_id=post_id,
                        content=edited,
                    )
                    if edit_result.success:
                        self._tracked_posts[chat_id] = (post_id, edited)
                        return SendResult(success=True, message_id=post_id)
                    logger.warning(
                        "Mattermost: footer edit failed for post=%s, fallback to normal send",
                        post_id,
                    )
                else:
                    logger.warning(
                        "Mattermost: footer edit skipped — failed to fetch post=%s content",
                        post_id,
                    )
            # 无追踪帖子或编辑/拉取失败 → 正常发送（降级）

        result = await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)

        # 追踪非 footer 帖子（用于后续 footer 编辑合并到上一条消息）
        if result.success and result.message_id and not self._is_footer_line(content):
            self._tracked_posts[chat_id] = (result.message_id, content)

        return result

    # ══════════════════════════════════════════════════════════════════════
    # send_clarify() 覆写 — 渲染交互卡片替代纯文本（替代 base.send_clarify）
    # ══════════════════════════════════════════════════════════════════════

    async def send_clarify(
        self,
        chat_id: str,
        question: str,
        choices: Optional[list],
        clarify_id: str,
        session_key: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """覆写 base.send_clarify()：用 MM interactive card 渲染选项按钮。

        - 有 choices → 每个选项渲染为一个按钮 + 「其他」按钮
        - 无 choices → 纯文本提问，Gateway text-intercept 自动捕获回复
        """
        callback_url = self._build_callback_url()
        logger.info(
            "Mattermost: send_clarify — callback_url=%r _callback_url=%r card_choices=%d",
            callback_url, self._callback_url,
            len(choices) if choices else 0,
        )

        # 从 metadata 中提取 channel_id（兼容不同调用方）
        channel_id_for_card = chat_id

        card = render_clarify_card(
            question=question,
            choices=list(choices) if choices else None,
            clarify_id=clarify_id,
            session_key=session_key,
            callback_url=callback_url,
            channel_id=channel_id_for_card,
            user_id="",  # user_id 不需要在 clarify 卡片中
        )

        # 通过 Bot API 发送交互卡片到 thread
        root_id = None
        if metadata and metadata.get("thread_id"):
            root_id = await self._get_thread_root_id(metadata["thread_id"])

        post_id = await self._post_card_in_thread(chat_id, root_id, card)

        if post_id:
            # 保存 clarify 元信息（post_id + 问题 + 选项），
            # 供回调时渲染「保留原问题/选项」的确认卡片。
            if not hasattr(self, "_clarify_posts"):
                self._clarify_posts: Dict[str, Dict[str, Any]] = {}
            self._clarify_posts[clarify_id] = {
                "post_id": post_id,
                "question": question,
                "choices": list(choices) if choices else None,
            }

            logger.info(
                "Mattermost: send_clarify — question=%r clarify_id=%s post_id=%s",
                question[:60], clarify_id, post_id,
            )
            return SendResult(success=True, message_id=post_id)

        # 降级：Bot API 失败时回退到纯文本
        logger.warning("Mattermost: send_clarify card post failed, falling back to text")
        return await super().send_clarify(
            chat_id=chat_id,
            question=question,
            choices=choices,
            clarify_id=clarify_id,
            session_key=session_key,
            metadata=metadata,
        )

    # ══════════════════════════════════════════════════════════════════════
    # （媒体/文件类覆写已全部移除 — v2026.9.7 上游对齐）
    # 上游 _post_message() 已原生实现：reply_to / metadata.thread_id /
    # metadata.root_id → root_id 解析（经插件缓存版 _resolve_root_id）、
    # mentions 抑制（_with_mentions_disabled）、broken-thread-root 降级。
    # 受影响的已删覆写：send_multiple_images / send_image / send_image_file /
    # send_document / send_video / send_voice / _derive_reply_to /
    # _send_local_file（静默跳过已在上游实现）/ _send_url_as_file。
    # gateway 对这些方法的调用（含 metadata kwarg）均走上游签名，行为一致。
    # ══════════════════════════════════════════════════════════════════════
