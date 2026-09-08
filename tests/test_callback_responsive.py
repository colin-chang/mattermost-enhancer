"""集成测试 — 验证回调服务器独立线程重构 + 即时响应语义。

模拟 Mattermost 服务端的真实回调路径：
  1. 启动 MattermostApprovalAdapter 的 callback server（独立线程/loop）
  2. 发送真实 HTTP POST（与 MM 发出的格式一致）
  3. 断言响应时间与响应内容

不依赖真实 MM 服务器 / gateway runner — 全部 mock。
"""
import asyncio
import json
import sys
import time
import urllib.request
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

# ── stub 掉 hermes 运行时依赖（插件 import 链需要） ──
types_mod = __import__("types")
gateway_mod = types_mod.ModuleType("gateway")
platforms_mod = types_mod.ModuleType("gateway.platforms")
base_mod = types_mod.ModuleType("gateway.platforms.base")


class SendResult:
    def __init__(self, success=True, message_id=None, error=None):
        self.success = success
        self.message_id = message_id
        self.error = error


base_mod.SendResult = SendResult

# bundled adapter stub — MattermostApprovalAdapter 的父类
bundled_mm = types_mod.ModuleType("hermes_plugins.platforms_mattermost.adapter")


class MattermostAdapter:
    MAX_POST_LENGTH = 4000

    def __init__(self, config):
        self.config = config
        self._bot_user_id = "bot123"
        self._base_url = "http://localhost:8065"
        self._token = "tok"
        self._reply_mode = "thread"
        self._closing = False
        self._dedup = MagicMock()
        self._typing_paused = set()
        self.typing_calls = []

    async def send_typing(self, chat_id, metadata=None):
        self.typing_calls.append(chat_id)

    def resume_typing_for_chat(self, chat_id):
        self.typing_calls.append(f"resume:{chat_id}")

    async def _api_get(self, path):
        return {}

    async def _api_post(self, path, payload):
        return {"id": "newpost1"}

    async def _api_put(self, path, payload):
        return {"id": "patched"}

    def format_message(self, text):
        return text

    def truncate_message(self, text, limit):
        return [text]


bundled_mm.MattermostAdapter = MattermostAdapter
bundled_mm.MAX_POST_LENGTH = 4000
bundled_mm._apply_yaml_config = lambda *a, **k: None
bundled_mm._is_connected = lambda *a, **k: True
bundled_mm._standalone_send = AsyncMock(return_value={})
bundled_mm.interactive_setup = lambda *a, **k: None
bundled_mm.validate_mattermost_config = lambda *a, **k: (True, "")

hermes_plugins = types_mod.ModuleType("hermes_plugins")
hermes_plugins.__path__ = []
sys.modules["hermes_plugins"] = hermes_plugins
sys.modules["hermes_plugins.platforms_mattermost"] = types_mod.ModuleType(
    "hermes_plugins.platforms_mattermost"
)
sys.modules["hermes_plugins.platforms_mattermost.adapter"] = bundled_mm
sys.modules["gateway"] = gateway_mod
sys.modules["gateway.platforms"] = platforms_mod
sys.modules["gateway.platforms.base"] = base_mod

# tools.approval / tools.clarify_gateway — 用真实模块（纯内存逻辑）
tools_pkg = types_mod.ModuleType("tools")
tools_pkg.__path__ = []
sys.modules["tools"] = tools_pkg

approval_src = Path(
    "/Users/Colin/.hermes/hermes-agent/tools/approval.py"
)
clarify_src = Path(
    "/Users/Colin/.hermes/hermes-agent/tools/clarify_gateway.py"
)

import importlib.util

def _load_real(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

approval_mod = _load_real("tools.approval", approval_src)
clarify_mod = _load_real("tools.clarify_gateway", clarify_src)
sys.modules["tools.environments"] = types_mod.ModuleType("tools.environments")
sys.modules["tools.environments.base"] = types_mod.ModuleType("tools.environments.base")
sys.modules["tools.clarify_tool"] = types_mod.ModuleType("tools.clarify_tool")
sys.modules["tools.clarify_tool"].strip_recommended = lambda s: s

# adapter 里 `from tools.approval import resolve_gateway_approval`
# 在模块顶部 — 需要真实符号可用
import tools.approval as _ta  # noqa: E402
import tools.clarify_gateway as _tc  # noqa: E402

# 现在加载插件 adapter（作为包的子模块，让相对导入工作）
import importlib.util as _ilu

pkg_name = "mattermost_enhancer_pkg"
pkg = types_mod.ModuleType(pkg_name)
pkg.__path__ = [str(PLUGIN_DIR)]
sys.modules[pkg_name] = pkg

def _load_pkg_mod(mod_name, filename):
    spec = _ilu.spec_from_file_location(
        f"{pkg_name}.{mod_name}", PLUGIN_DIR / filename
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{pkg_name}.{mod_name}"] = mod
    spec.loader.exec_module(mod)
    return mod

cards_mod = _load_pkg_mod("cards", "cards.py")
_load_pkg_mod("models", "models.py")
adapter_mod = _load_pkg_mod("adapter", "adapter.py")
MattermostApprovalAdapter = adapter_mod.MattermostApprovalAdapter


PASS = 0
FAIL = 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


def post(path, body, headers=None):
    """向回调服务器发送真实 HTTP POST，返回 (status, body, elapsed_s)。"""
    url = f"http://127.0.0.1:18099{path}"
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read()), time.monotonic() - t0
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), time.monotonic() - t0


async def main():
    cfg = MagicMock()
    cfg.extra = {}
    ad = MattermostApprovalAdapter(cfg)
    ad._callback_bind = "127.0.0.1"
    ad._callback_port = 18099

    # 捕获当前 loop 作为「主 loop」— followup 会被派到这里
    main_loop = asyncio.get_running_loop()
    dispatched = []

    def fake_followup(coro):
        # 同步记录派发的 followup 协程（不真正执行，避免触发真实 runner）
        dispatched.append(coro)
        coro.close()

    ad._schedule_followup = fake_followup
    # _start_callback_server 里保存主 loop 引用
    with patch.object(adapter_mod.MattermostApprovalAdapter, "_schedule_followup", fake_followup):
        await ad._start_callback_server()

    # 等独立线程就绪
    for _ in range(50):
        try:
            urllib.request.urlopen("http://127.0.0.1:18099", timeout=1)
        except Exception:
            break
        await asyncio.sleep(0.1)

    print("\n── 1. HTTP 服务器在独立线程启动 ──")
    check("回调线程已启动", ad._callback_thread is not None and ad._callback_thread.is_alive())
    check("独立 loop 已创建", ad._callback_loop is not None and ad._callback_loop.is_running())
    check("独立 loop ≠ 主 loop", ad._callback_loop is not main_loop)

    print("\n── 2. 审批按钮回调 — 纯内存立即返回 ──")
    # 注册一个 pending approval（直接用真实 tools.approval 状态）
    # v2026.9.7 对齐：_ApprovalEntry 已从 tools.approval 拆到 tools.approval_gateway_wait
    from tools.approval_gateway_wait import _ApprovalEntry
    entry = _ApprovalEntry({"command": "ls -la"})
    approval_mod._gateway_queues["agent:main:mattermost:dm:ch1"] = [entry]

    body = {
        "context": {
            "action": "approve_once",
            "session_key": "agent:main:mattermost:dm:ch1",
            "command": "ls -la",
            "chat_id": "ch1",
        },
        "user_id": "u1",
        "post_id": "post_approve_1",
        "channel_id": "dm1",
        "trigger_id": "tr1",
    }
    with patch.dict("os.environ", {"MATTERMOST_ALLOWED_USERS": ""}):
        status, resp, elapsed = post("/mattermost/callback", body)
    check("HTTP 200", status == 200)
    check(
        "响应含最终结果（非 ⏳）",
        "Approved" in resp.get("update", {}).get("message", ""),
        f"got: {resp}",
    )
    check("响应 < 500ms", elapsed < 0.5, f"took {elapsed*1000:.0f}ms")
    check("审批已被 resolve", entry.result == "once")
    check("按钮已清空", resp.get("update", {}).get("props", {}).get("attachments", [{}])[0].get("actions") == [])

    print("\n── 3. 双击去重 — 同 post 同 action 第二次点击被拦截 ──")
    status, resp, elapsed = post("/mattermost/callback", body)
    check("重复点击返回已处理", "已处理" in resp.get("update", {}).get("message", ""), f"got: {resp}")
    check("重复点击 < 500ms", elapsed < 0.5)

    print("\n── 4. Clarify 选项回调 — 即时确认卡片 ──")
    c_entry = clarify_mod.register("cid001", "sess1", "选一个?", ["A", "B"])
    body = {
        "context": {
            "action": "cmd_clarify_choice",
            "clarify_id": "cid001",
            "choice_value": "A",
        },
        "user_id": "u1",
        "post_id": "post_clarify_1",
        "channel_id": "ch1",
    }
    status, resp, elapsed = post("/mattermatter/callback".replace("mattermatter", "mattermost"), body)
    check("HTTP 200", status == 200)
    att = resp.get("update", {}).get("props", {}).get("attachments", [{}])
    check("确认卡片含选项", "已选择" in json.dumps(resp, ensure_ascii=False), f"got: {resp}")
    check("clarify 已 resolve", c_entry.response == "A")
    check("响应 < 500ms", elapsed < 0.5, f"took {elapsed*1000:.0f}ms")

    print("\n── 5. 模型切换回调 — 立即 ack + followup 派发 ──")
    body = {
        "context": {
            "action": "cmd_model_switch",
            "selected_option": "zenmux/deepseek-v4-pro",
            "session_key": "agent:main:mattermost:channel:chX",
        },
        "user_id": "u1",
        "post_id": "post_model_1",
        "channel_id": "chX",
    }
    status, resp, elapsed = post("/mattermost/callback", body)
    check("HTTP 200", status == 200)
    check("立即返回 ⏳ ack", "⏳" in resp.get("update", {}).get("message", ""), f"got: {resp}")
    check("响应 < 500ms", elapsed < 0.5, f"took {elapsed*1000:.0f}ms")
    await asyncio.sleep(0.2)
    check("followup 已派发", len(dispatched) == 1)

    print("\n── 6. /new 确认回调 — 立即 ack + followup 派发 ──")
    body = {
        "context": {
            "action": "cmd_new_confirm",
            "session_key": "agent:main:mattermost:channel:chX",
        },
        "user_id": "u1",
        "post_id": "post_new_1",
        "channel_id": "chX",
    }
    status, resp, elapsed = post("/mattermost/callback", body)
    check("HTTP 200", status == 200)
    check("立即返回 ⏳ ack", "⏳" in resp.get("update", {}).get("message", ""), f"got: {resp}")
    check("响应 < 500ms", elapsed < 0.5, f"took {elapsed*1000:.0f}ms")
    await asyncio.sleep(0.2)
    check("followup 已派发", len(dispatched) == 2)

    print("\n── 7. Slash command — HTTP 立即返回空，工作派 followup ──")
    body = "command=/model&channel_id=chX&user_id=u1&root_id="
    url = "http://127.0.0.1:18099/mm-command"
    data = body.encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=15) as resp:
        slash_body = json.loads(resp.read())
        slash_elapsed = time.monotonic() - t0
    check("HTTP 200 + 空 ephemeral", slash_body == {}, f"got: {slash_body}")
    check("响应 < 500ms", slash_elapsed < 0.5, f"took {slash_elapsed*1000:.0f}ms")
    await asyncio.sleep(0.2)
    check("model command followup 已派发", len(dispatched) == 3)

    print("\n── 8. 主 loop 阻塞时回调仍即时（核心场景！）──")
    # 用真实同步阻塞占住主 loop 3 秒（模拟 agent 繁忙时同步操作霸占 loop）
    def _block_loop():
        time.sleep(3.0)
    main_loop.call_soon_threadsafe(lambda: main_loop.run_in_executor(None, _block_loop))
    # 更直接：投递一个同步阻塞任务到主 loop 自身
    blocking_started = asyncio.Event()

    async def _block_main():
        blocking_started.set()
        time.sleep(3.0)  # 同步 sleep — 真正卡住主 loop
    main_loop.create_task(_block_main())
    await asyncio.wait_for(blocking_started.wait(), timeout=2.0)
    await asyncio.sleep(0.05)  # 确保阻塞已开始

    body = {
        "context": {
            "action": "approve_session",
            "session_key": "agent:main:mattermost:dm:ch2",
            "chat_id": "ch2",
        },
        "user_id": "u1",
        "post_id": "post_busy_1",
        "channel_id": "dm2",
    }
    entry2 = _ApprovalEntry({"command": "echo"})
    approval_mod._gateway_queues["agent:main:mattermost:dm:ch2"] = [entry2]

    status, resp, elapsed = post("/mattermost/callback", body)
    check("主 loop 忙时 HTTP 200", status == 200)
    check("主 loop 忙时响应 < 500ms（旧架构会卡 3s+）", elapsed < 0.5, f"took {elapsed*1000:.0f}ms")
    check("审批仍然即时 resolve", entry2.result == "session")

    print("\n── 9. Clarify「其他」按钮 — 即时切换文本模式 ──")
    c_entry2 = clarify_mod.register("cid002", "sess2", "开放问题?", ["X", "Y"])
    body = {
        "context": {"action": "cmd_clarify_other", "clarify_id": "cid002"},
        "user_id": "u1",
        "post_id": "post_clarify_2",
        "channel_id": "ch1",
    }
    status, resp, elapsed = post("/mattermost/callback", body)
    check("HTTP 200", status == 200)
    check("awaiting_text 已标记", c_entry2.awaiting_text is True)
    check("响应 < 500ms", elapsed < 0.5, f"took {elapsed*1000:.0f}ms")

    print("\n── 10. 清理停机 ──")
    await ad._stop_callback_server()
    check("回调线程已退出", not ad._callback_thread.is_alive() if ad._callback_thread else True)

    return FAIL


if __name__ == "__main__":
    rc = asyncio.run(main())
    sys.exit(1 if rc else 0)
