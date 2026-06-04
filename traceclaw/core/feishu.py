"""
TraceClaw 飞书集成 — Feishu/Lark Bot 消息通道
==============================================
通过飞书开放平台的长连接（WebSocket）模式接收机器人消息，
推入 TraceClaw 消息总线供 Agent 处理，并将 Agent 的回复
发送回飞书对话。

整体架构（三条数据流汇入一条队列）：
  飞书用户发消息 → 飞书服务器 → WebSocket 长连接
    → SDK Client（后台线程，protobuf 帧）
    → _on_message_receive() 回调
    → _bridge 线程安全队列（queue.Queue）
    → feishu_listener() 轮询桥接队列
    → task_queue.put() 推入 TraceClaw 消息总线
    → agent_worker 消费 → LangGraph Agent 处理
    → main.py 检测 has_pending_reply() → reply_message() 回复飞书


============ 前置条件（在飞书开放平台操作） ============

  1. 创建企业自建应用（https://open.feishu.cn/）
  2. 启用"机器人"能力
  3. 在"事件订阅"页面：
     a. 订阅方式选择「使用长连接接收事件」（不需要公网 IP）
     b. 添加事件：im.message.receive_v1（接收消息）
     c. 建议也加上 im.chat.member.bot.added_v1（机器人被加入群聊——验证通道用的金丝雀）
  4. 权限管理：
  {
  "scopes": {
    "tenant": [
      "base:app:copy",
      "base:app:create",
      "base:app:read",
      "base:app:update",
      "base:collaborator:create",
      "base:collaborator:delete",
      "base:collaborator:read",
      "base:dashboard:copy",
      "base:dashboard:read",
      "base:field:create",
      "base:field:delete",
      "base:field:read",
      "base:field:update",
      "base:form:read",
      "base:form:update",
      "base:record:create",
      "base:record:delete",
      "base:record:read",
      "base:record:retrieve",
      "base:record:update",
      "base:role:create",
      "base:role:delete",
      "base:role:read",
      "base:role:update",
      "base:table:create",
      "base:table:delete",
      "base:table:read",
      "base:table:update",
      "base:view:read",
      "base:view:write_only",
      "bitable:app",
      "bitable:app:readonly",
      "board:whiteboard:node:create",
      "board:whiteboard:node:delete",
      "board:whiteboard:node:read",
      "board:whiteboard:node:update",
      "contact:contact.base:readonly",
      "contact:user.base:readonly",
      "contact:user.employee_id:readonly",
      "contact:user.employee_number:read",
      "contact:user.id:readonly",
      "docs:doc",
      "docs:doc:readonly",
      "docs:document.comment:create",
      "docs:document.comment:read",
      "docs:document.comment:update",
      "docs:document.comment:write_only",
      "docs:document.content:read",
      "docs:document.media:download",
      "docs:document.media:upload",
      "docs:document.subscription",
      "docs:document.subscription:read",
      "docs:document:copy",
      "docs:document:export",
      "docs:document:import",
      "docs:event.document_deleted:read",
      "docs:event.document_edited:read",
      "docs:event.document_opened:read",
      "docs:event:subscribe",
      "docs:permission.member",
      "docs:permission.member:auth",
      "docs:permission.member:create",
      "docs:permission.member:delete",
      "docs:permission.member:readonly",
      "docs:permission.member:retrieve",
      "docs:permission.member:transfer",
      "docs:permission.member:update",
      "docs:permission.setting",
      "docs:permission.setting:read",
      "docs:permission.setting:readonly",
      "docs:permission.setting:write_only",
      "docx:document",
      "docx:document.block:convert",
      "docx:document:create",
      "docx:document:readonly",
      "drive:drive",
      "drive:drive.metadata:readonly",
      "drive:drive.search:readonly",
      "drive:drive:readonly",
      "drive:drive:version",
      "drive:drive:version:readonly",
      "drive:export:readonly",
      "drive:file",
      "drive:file.like:readonly",
      "drive:file.meta.sec_label.read_only",
      "drive:file:download",
      "drive:file:readonly",
      "drive:file:upload",
      "drive:file:view_record:readonly",
      "event:ip_list",
      "im:app_feed_card:write",
      "im:biz_entity_tag_relation:read",
      "im:biz_entity_tag_relation:write",
      "im:chat",
      "im:chat.access_event.bot_p2p_chat:read",
      "im:chat.announcement:read",
      "im:chat.announcement:write_only",
      "im:chat.chat_pins:read",
      "im:chat.chat_pins:write_only",
      "im:chat.collab_plugins:read",
      "im:chat.collab_plugins:write_only",
      "im:chat.managers:write_only",
      "im:chat.members:bot_access",
      "im:chat.members:read",
      "im:chat.members:write_only",
      "im:chat.menu_tree:read",
      "im:chat.menu_tree:write_only",
      "im:chat.moderation:read",
      "im:chat.tabs:read",
      "im:chat.tabs:write_only",
      "im:chat.top_notice:write_only",
      "im:chat.widgets:read",
      "im:chat.widgets:write_only",
      "im:chat:create",
      "im:chat:delete",
      "im:chat:moderation:write_only",
      "im:chat:operate_as_owner",
      "im:chat:read",
      "im:chat:readonly",
      "im:chat:update",
      "im:datasync.feed_card.time_sensitive:write",
      "im:message",
      "im:message.group_at_msg:readonly",
      "im:message.group_msg",
      "im:message.p2p_msg:readonly",
      "im:message.pins:read",
      "im:message.pins:write_only",
      "im:message.reactions:read",
      "im:message.reactions:write_only",
      "im:message.urgent",
      "im:message.urgent.status:write",
      "im:message.urgent:phone",
      "im:message.urgent:sms",
      "im:message:readonly",
      "im:message:recall",
      "im:message:send_as_bot",
      "im:message:send_multi_depts",
      "im:message:send_multi_users",
      "im:message:send_sys_msg",
      "im:message:update",
      "im:resource",
      "im:tag:read",
      "im:tag:write",
      "im:url_preview.update",
      "im:user_agent:read",
      "sheets:spreadsheet",
      "sheets:spreadsheet.meta:read",
      "sheets:spreadsheet.meta:write_only",
      "sheets:spreadsheet:create",
      "sheets:spreadsheet:read",
      "sheets:spreadsheet:readonly",
      "sheets:spreadsheet:write_only",
      "space:document.event:read",
      "space:document:delete",
      "space:document:move",
      "space:document:retrieve",
      "space:document:shortcut",
      "space:folder:create",
      "wiki:member:create",
      "wiki:member:retrieve",
      "wiki:member:update",
      "wiki:node:copy",
      "wiki:node:create",
      "wiki:node:move",
      "wiki:node:read",
      "wiki:node:retrieve",
      "wiki:node:update",
      "wiki:setting:read",
      "wiki:setting:write_only",
      "wiki:space:read",
      "wiki:space:retrieve",
      "wiki:space:write_only",
      "wiki:wiki",
      "wiki:wiki:readonly"
    ]
  }
}
  5. 创建版本 → 发布应用
  6. 将 App ID / App Secret 填入项目 .env

============ 环境变量（.env） ============

  FEISHU_ENABLED=true                 # 是否启用飞书集成
  FEISHU_APP_ID=cli_xxxxxxxx          # 飞书应用 App ID
  FEISHU_APP_SECRET=xxxxxxxx          # 飞书应用 App Secret
  FEISHU_NOTIFY_CHAT_ID=oc_xxxxxxxx   # （可选）心跳通知默认推送的群聊


============ 设计决策 ============

  1. 【双线程模型】
     - SDK 的 Client.start() 内部调用 loop.run_until_complete() 阻塞线程
     - 因此 SDK Client 必须运行在独立的后台线程（feishu-sdk）
     - feishu_listener() 协程运行在 TraceClaw 自己的 event loop 中
     - 两个线程通过 queue.Queue（线程安全）桥接

  2. 【双 Token 分离】
     - app_access_token（SDK 内部管理）：用于 WebSocket 长连接认证
     - tenant_access_token（_get_tenant_token 手动管理）：用于消息回复 REST API
     - 两者从同一个 App ID / App Secret 签发，但用途不同

  3. 【单 _pending_reply 无锁设计】
     - agent_worker 是串行消费 task_queue 的——同一时刻只处理一条消息
     - 不存在「两个飞书消息同时被处理」的并发场景
     - 因此 _pending_reply 不需要加锁，直接全局变量即可

  4. 【_bridge 轮询而非阻塞】
     - feishu_listener 用 get_nowait() + sleep(0.2) 的轮询模式
     - 好处：可以随时响应 asyncio.CancelledError 优雅退出
     - 代价：最长 200ms 的消息延迟（对聊天机器人场景完全可接受）

  5. 【send_to_chat 独立于 Agent 回复链路】
     - 心跳通知直接调用 send_to_chat() 推消息，不经过 Agent
     - 即使 Agent 正在处理长任务，通知也能立即送达飞书群
"""

import os
import sys
import json
import queue                          # 线程安全队列——用于子线程 → asyncio 的消息桥接
import threading                      # SDK Client 运行在后台线程
import asyncio                        # TraceClaw 主 event loop
from typing import Optional, Dict, Any

import httpx                          # 异步 HTTP 客户端——用于飞书 REST API 调用


# ============================================================
# _status — 终端状态输出
# ============================================================
# 飞书模块的统一日志输出函数。
#
# 为什么不用 logging？
#   - TraceClaw 没有全局配置 logging handler，logger 输出直接消失
#   - 直接写 sys.stdout + flush 保证在 Prompt Toolkit TUI 中也能即时显示
#   - ANSI 256 色转义码，不同状态用不同颜色区分
#
# 五种状态颜色：
#   info  → 紫色 (141) — 普通状态信息
#   ok    → 青色 (51)  — 成功
#   warn  → 橙色 (214) — 警告
#   error → 红色 (31)  — 错误
#   event → 绿色 (82)  — 收到消息事件

def _status(msg: str, kind: str = "info"):
    """向终端打印飞书模块状态信息（带 ANSI 颜色 + 即时刷新）"""
    prefixes = {
        "info":  "\033[38;5;141m[Feishu]\033[0m",
        "ok":    "\033[38;5;51m[Feishu]\033[0m",
        "warn":  "\033[38;5;214m[Feishu]\033[0m",
        "error": "\033[31m[Feishu]\033[0m",
        "event": "\033[38;5;82m[Feishu]\033[0m",
    }
    prefix = prefixes.get(kind, prefixes["info"])
    try:
        sys.stdout.write(f"\r{prefix} {msg}\n")
        sys.stdout.flush()            # 强制刷新，确保 Prompt Toolkit 捕获输出
    except Exception:
        pass                          # 终端输出失败不能影响主逻辑


# ============================================================
# 全局状态变量
# ============================================================
# 所有状态都是模块级全局变量，设计原因：
#   1. 整个进程只有一个飞书连接（单应用单连接）
#   2. agent_worker 串行消费消息，无并发写入竞争
#   3. 避免引入额外的状态管理类，保持模块简洁

_enabled = False                      # 飞书集成是否启用（_load_config 后确定）
_app_id = ""                          # 飞书应用 App ID（从 .env 加载）
_app_secret = ""                      # 飞书应用 App Secret（从 .env 加载）

# 待回复上下文：Agent 处理完消息后，通过这个 context 找到飞书对话并回复
# 结构: {"message_id": "om_xxxxxxxx"}  ← 飞书消息的唯一 ID
# 为什么是单变量而不是队列？
#   agent_worker 是串行的——在处理完当前消息之前，不会处理下一条
#   所以同一时刻最多只有一个待回复的飞书消息
_pending_reply: Optional[Dict[str, str]] = None

# ============================================================
# _bridge — 线程安全桥接队列
# ============================================================
# 这是整个双线程模型的"桥"。
#
# 生产者（SDK 后台线程，feishu-sdk）:
#   _on_message_receive() 回调 → _bridge.put("[From Feishu] 文本")
#
# 消费者（TraceClaw event loop）:
#   feishu_listener() 协程 → _bridge.get_nowait() 轮询 → task_queue.put()
#
# 为什么用 queue.Queue 而不是 asyncio.Queue？
#   asyncio.Queue 不是线程安全的——跨线程 put/get 会数据竞争
#   queue.Queue 是标准库的线程安全队列，天然支持跨线程通信

_bridge: queue.Queue = queue.Queue()

# tenant_access_token 缓存——用于消息回复/发送 API
# 为什么缓存？
#   Token 有效期 2 小时，不用每次调用都重新申请
#   提前 5 分钟刷新（_token_expires_at - 300），避免边界情况下的 401 错误
_tenant_token: Optional[str] = None
_tenant_token_expires_at: float = 0.0   # Unix 时间戳


# ============================================================
# _load_config — 加载飞书配置
# ============================================================

def _load_config() -> bool:
    """
    从环境变量加载飞书配置。

    调用时机：feishu_listener() 启动时调用一次。
    返回值：True 表示配置有效、可以启动；False 表示未启用或配置缺失。

    严格校验：如果 FEISHU_ENABLED=true 但缺少凭证，
    自动禁用并输出警告——避免 SDK 在缺少凭证时反复报错。
    """
    global _enabled, _app_id, _app_secret

    # 检查开关
    _enabled = os.getenv("FEISHU_ENABLED", "false").lower() == "true"
    if not _enabled:
        return False

    # 读取凭证
    _app_id = os.getenv("FEISHU_APP_ID", "")
    _app_secret = os.getenv("FEISHU_APP_SECRET", "")

    # 凭据不完整 → 自动禁用 + 警告
    if not _app_id or not _app_secret:
        _status("FEISHU_ENABLED=true but missing credentials, disabled", "warn")
        _enabled = False
        return False

    return True


# ============================================================
# _get_tenant_token — Token 管理（REST API 用）
# ============================================================
# 注意：这里的 tenant_access_token 仅用于消息回复和主动发送的 REST API。
# WebSocket 长连接的认证用的是 app_access_token，由 SDK 内部自动管理。
#
# Token 有效期：飞书返回 expire=7200 秒（2 小时）
# 刷新策略：过期前 5 分钟提前刷新（_token_expires_at - 300）
#   - 避免「刚好在 API 调用时 Token 过期」的竞态
#   - 5 分钟缓冲足够处理网络重试和时钟漂移
#
# 降级策略：网络异常时返回旧 Token（可能已过期，但聊胜于无）

async def _get_tenant_token() -> Optional[str]:
    """
    获取/刷新飞书 tenant_access_token。

    用于消息回复 API（reply_message）和主动发送 API（send_to_chat）。
    带缓存——2 小时内复用，过期前 5 分钟自动刷新。
    """
    global _tenant_token, _tenant_token_expires_at
    import time

    now = time.time()

    # 命中缓存：Token 存在且距离过期还有至少 5 分钟
    if _tenant_token and now < _tenant_token_expires_at - 300:
        return _tenant_token

    # === 未命中 → 向飞书申请新 Token ===
    # 飞书 Open API 端点：tenant_access_token 内部版
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    body = {"app_id": _app_id, "app_secret": _app_secret}

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=body, timeout=10)
            data = resp.json()
    except Exception as e:
        _status(f"Tenant token error: {e}", "error")
        # 降级：网络异常时返回旧 Token（可能还有效）
        return _tenant_token

    # 飞书 API 返回非 0 → 凭证错误或服务异常
    if data.get("code") != 0:
        _status(f"Tenant token failed: {data.get('msg')}", "error")
        return None

    # 更新缓存
    _tenant_token = data["tenant_access_token"]
    _tenant_token_expires_at = now + data.get("expire", 7200)
    return _tenant_token


# ============================================================
# _decode_content — 飞书消息内容解码器
# ============================================================
# 飞书消息的 content 字段是一个 JSON 字符串，不同消息类型有不同结构：
#
#   text  → {"text": "hello"}
#   post  → {"content": [[{"text": "段落1"}, {"title": "标题"}], [...]]}
#            ↑ 富文本消息（图文混排），需要遍历段落和元素提取文本
#   image → {"image_key": "xxx"}  → 返回 "[Image]"
#   file  → {"file_key": "xxx", "file_name": "a.pdf"} → "[File: a.pdf]"
#   audio → {"file_key": "xxx"}  → "[Audio]"
#   sticker → {} → "[Sticker]"
#
# 当前版本只把非文本消息转成占位符文本，后续可以扩展多模态处理。

def _decode_content(message_type: str, content_json: str) -> str:
    """
    将飞书消息的 content JSON 解码为纯文本。

    Args:
        message_type: 飞书消息类型（text / post / image / file / audio / sticker）
        content_json: 飞书消息 content 字段的 JSON 字符串

    Returns:
        解码后的纯文本。非文本消息返回占位符如 "[Image]"。
    """
    # 解析 JSON —— 飞书消息的 content 一定是 JSON 字符串
    try:
        content = json.loads(content_json)
    except (json.JSONDecodeError, TypeError):
        return content_json or ""

    # === 文本消息 ===
    if message_type == "text":
        return content.get("text", "")

    # === 富文本消息（图文混排） ===
    # 结构：content.content 是二重列表 [[段落1的元素], [段落2的元素], ...]
    # 每个元素可能是 {"text": "..."} 或 {"title": "..."}
    elif message_type == "post":
        parts = content.get("content", [[]])
        texts = []
        for paragraph in parts:
            for elem in paragraph:
                if isinstance(elem, dict):
                    texts.append(elem.get("text", elem.get("title", "")))
        return " ".join(texts)

    # === 非文本消息 → 占位符 ===
    elif message_type == "image":
        return "[Image]"
    elif message_type == "file":
        return f"[File: {content.get('file_name', 'unknown')}]"
    elif message_type == "audio":
        return "[Audio]"
    elif message_type == "sticker":
        return "[Sticker]"
    else:
        return f"[{message_type}]"


# ============================================================
# SDK 事件处理器（运行在 SDK 的后台线程 feishu-sdk 中）
# ============================================================
# 这些函数被 lark-oapi SDK 的 EventDispatcher 回调调用。
# 它们运行在 SDK Client 所在的独立线程中（不是 TraceClaw 的 event loop）。
#
# 因此：
#   - 可以调用 _status()（写 stdout，线程安全）
#   - 可以调用 _bridge.put()（queue.Queue，线程安全）
#   - 不能直接 await asyncio 协程（不在 event loop 中）
#   - 不能直接操作 TraceClaw 的 asyncio.Queue（不是线程安全的）

def _on_bot_added(event_data):
    """
    事件回调：机器人被加入群聊时触发。

    这个处理器的作用是「事件通道金丝雀」——
    如果用户配置了事件订阅但不确定是否生效，
    把机器人拉进一个群就能验证：看到这条日志就说明事件通道畅通。
    """
    _status("Bot was added to a chat! Event delivery is working.", "ok")


def _on_message_receive(event_data):
    """
    事件回调：收到用户发给机器人的消息时触发（im.message.receive_v1）。

    这是飞书集成的核心回调——每条飞书消息都会经过这个函数处理。

    处理流程：
      1. 解析事件数据 → 提取 message 对象
      2. 过滤机器人自己的消息（防止回复触发无限循环）
      3. 解码消息内容（JSON → 纯文本）
      4. 设置 _pending_reply 上下文（Agent 回复时用）
      5. 添加 [From Feishu] 前缀 → 推入 _bridge 桥接队列

    关于 event_data 的数据结构（飞书 v2 事件格式）：
      event_data.event.message.message_id   ← 消息唯一 ID（用于回复）
      event_data.event.message.chat_id      ← 群聊 ID
      event_data.event.message.chat_type    ← 对话类型（"bot" = 机器人自己发的）
      event_data.event.message.message_type ← 消息类型（text / post / image / ...）
      event_data.event.message.content      ← 消息内容（JSON 字符串）
    """
    global _pending_reply

    _status("Event received: im.message.receive_v1", "ok")

    # 步骤 1：提取消息对象
    # SDK 解析后的 protobuf 帧，数据路径为 event_data.event.message
    try:
        msg = event_data.event.message
    except Exception as e:
        _status(f"Failed to parse event: {e}", "error")
        return

    # 步骤 2：过滤机器人自己的消息
    # 飞书会把机器人发出的回复也作为事件推送回来
    # chat_type == "bot" 表示这条消息的发送者是机器人（即我们自己）
    # 必须过滤，否则：用户发"你好" → 机器人回"你好" → 飞书推回"你好" → 机器人再回 → ∞
    if getattr(msg, 'chat_type', None) == "bot":
        _status("Filtered: bot's own message", "info")
        return

    # 步骤 3：提取 message_id（回复时必需）
    message_id = getattr(msg, 'message_id', '')
    if not message_id:
        _status("Filtered: no message_id", "warn")
        return

    # 步骤 4：解码消息内容
    msg_type = getattr(msg, 'message_type', 'text')   # 默认 text
    content_raw = getattr(msg, 'content', '{}')       # 飞书 content 是 JSON 字符串
    text = _decode_content(msg_type, content_raw)

    # 步骤 5：过滤空内容（表情包、空白消息等）
    if not text.strip():
        _status(f"Filtered: empty content (type={msg_type})", "info")
        return

    # 步骤 6：设置待回复上下文
    # Agent 处理完这条消息后，main.py 里的 agent_worker 会检查
    # has_pending_reply() → 如果是 True → 调用 reply_message() 回复飞书
    _pending_reply = {"message_id": message_id}

    # 步骤 7：构造 Agent 输入 + 推入桥接队列
    # [From Feishu] 前缀 → Agent 的系统提示词识别出这是飞书消息
    # → Agent 知道回复会自动发回飞书，不会说"我只能在本地喊"之类的话
    agent_input = f"[From Feishu] {text}"
    _status(f"Msg: {text[:60]}{'...' if len(text) > 60 else ''}", "event")
    _bridge.put(agent_input)


# ============================================================
# _run_sdk_client — SDK 客户端（在后台线程中运行）
# ============================================================
# 为什么必须用独立线程？
#   lark-oapi SDK 的 Client.start() 内部调用逻辑：
#     1. loop.run_until_complete(self._connect())   ← 异步连接
#     2. loop.create_task(self._ping_loop())        ← 后台心跳
#     3. loop.run_until_complete(_select())         ← 阻塞 forever
#   其中 _select() = while True: await asyncio.sleep(3600)
#   → 这会占用当前线程的 event loop 永远不释放
#   → 如果放在 TraceClaw 的 event loop 中，整个系统直接卡死
#
# 线程模型示意图：
#   主线程 (TraceClaw event loop)
#     ├── agent_worker
#     ├── pacemaker_loop
#     └── feishu_listener ──轮询──→ _bridge ──put──→ 子线程 (feishu-sdk)
#                                                         └── SDK Client
#
# SDK 连接流程（_run_sdk_client 内部的 SDK 行为）：
#   1. POST https://open.feishu.cn/callback/ws/endpoint
#      Body: {"AppID": app_id, "AppSecret": app_secret}
#      ← 注意：这里是大写字段名，不是标准 REST API 的 snake_case
#   2. 响应: {"code": 0, "data": {"URL": "wss://msg-frontier.feishu.cn/ws/v2?..."}}
#   3. SDK 连接返回的 WebSocket URL（使用 protobuf 二进制帧通信）
#   4. 连接成功 → 控制台输出 "connected to wss://..."
#   5. 后台循环接收 protobuf 帧 → 反序列化 → 分发给注册的事件处理器

def _run_sdk_client():
    """
    在后台线程中启动 lark-oapi SDK 的 WebSocket 长连接客户端。

    这个函数被 feishu_listener() 通过 threading.Thread 启动。
    它会阻塞当前线程（SDK 的 event loop 不释放），直到进程退出。
    """
    # 延迟导入——SDK 只在飞书启用时才加载，不影响飞书未启用的场景
    from lark_oapi.ws import Client
    from lark_oapi.event.dispatcher_handler import EventDispatcherHandler

    # === 构建事件分发器 ===
    # EventDispatcherHandler.builder(encrypt_key, verification_token)
    #   长连接模式下不需要加密密钥和验证 token（Webhook 模式才需要），所以传空串
    #
    # register_p2_im_message_receive_v1(_on_message_receive)
    #   p2 = v2 版本事件协议（当前最新）
    #   im.message.receive_v1 = 收到消息事件
    #   回调函数 _on_message_receive 负责将消息内容推入桥接队列
    #
    # register_p2_im_chat_member_bot_added_v1(_on_bot_added)
    #   机器人被加入群聊事件——作为事件通道的"金丝雀"
    handler = (
        EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(_on_message_receive)
        .register_p2_im_chat_member_bot_added_v1(_on_bot_added)
        .build()
    )

    # === 创建 SDK 客户端 ===
    # auto_reconnect=True:
    #   SDK 内部实现了完整的断线重连逻辑——
    #   首次重连有随机抖动（避免雷群效应），之后按服务端下发的间隔重试
    #   ClientConfig（重连次数、间隔、心跳频率）由飞书服务端动态下发
    client = Client(
        app_id=_app_id,
        app_secret=_app_secret,
        event_handler=handler,
        auto_reconnect=True,
    )

    _status("SDK client starting (long-connection mode)...", "info")

    # === 启动客户端（阻塞） ===
    # client.start() 内部：
    #   1. 异步连接 WebSocket
    #   2. 启动心跳循环
    #   3. 阻塞线程直到进程结束
    # 任何异常都是致命的——说明飞书集成不可用
    try:
        client.start()
    except Exception as e:
        _status(f"SDK client fatal error: {e}", "error")
        _status("Feishu listener has stopped", "warn")


# ============================================================
# 公共 API —— 供 main.py 和 heartbeat.py 调用
# ============================================================

def is_enabled() -> bool:
    """
    检查飞书集成是否已启用。

    main.py 用它判断是否启动 feishu_listener 协程。
    heartbeat.py 用它判断是否发送飞书通知。
    """
    return _enabled


def has_pending_reply() -> bool:
    """
    检查是否有待回复的飞书消息。

    main.py 的 agent_worker 在每次 Agent 回复完成后调用此函数。
    如果返回 True，说明当前这轮对话来自飞书 → 需要把回复发回飞书。
    """
    return _pending_reply is not None


async def reply_message(content: str) -> bool:
    """
    回复当前待处理的飞书消息（直接回复用户的那条消息）。

    调用时机：agent_worker 处理完一轮对话后，检查 has_pending_reply()
            返回 True 时调用，将 Agent 的最终回复文本发送回飞书。

    实现细节：
      - 使用飞书 REST API：POST /im/v1/messages/{message_id}/reply
      - 认证：tenant_access_token（不是 SDK 的 app_access_token）
      - 消息体：{"content": "{\"text\":\"...\"}", "msg_type": "text"}

    关于 _pending_reply 的原子性：
      - 先取出 message_id，立即将 _pending_reply 置为 None
      - 如果回复失败，恢复 _pending_reply → 下次 Agent 回复时可以重试
      - 同一时刻只有一个飞书回复在进行（agent_worker 串行保证）

    Args:
        content: Agent 的最终回复文本（纯文本，不支持 Markdown）

    Returns:
        True:  回复成功发送
        False: 没有待回复消息 / Token 获取失败 / API 调用失败
    """
    global _pending_reply

    # 没有待回复消息 → 直接返回（终端输入的消息不需要飞书回复）
    if not _pending_reply:
        return False

    # 原子取出 message_id + 清除 pending 状态
    message_id = _pending_reply["message_id"]
    _pending_reply = None

    # 获取 API Token（带缓存）
    token = await _get_tenant_token()
    if not token:
        # Token 获取失败 → 恢复 pending，等下次重试
        _pending_reply = {"message_id": message_id}
        return False

    # === 调用飞书回复 API ===
    # 端点: POST /open-apis/im/v1/messages/{message_id}/reply
    # 这个 API 会直接回复用户的那条消息（用户收到一条引用回复）
    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = {
        "content": json.dumps({"text": content}),  # content 字段本身也是 JSON 字符串
        "msg_type": "text",
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=headers, json=body, timeout=10)
            data = resp.json()
    except Exception as e:
        _status(f"Reply error: {e}", "error")
        return False

    if data.get("code") != 0:
        _status(f"Reply failed: {data.get('msg')}", "error")
        return False

    _status(f"Reply sent: {content[:50]}{'...' if len(content) > 50 else ''}", "ok")
    return True


async def send_to_chat(chat_id: str, content: str) -> bool:
    """
    向指定飞书群聊主动发送消息（非回复模式）。

    与 reply_message 的区别：
      - reply_message：回复用户的消息（用户看到引用回复）
      - send_to_chat：主动发一条新消息到群里（不引用任何消息）

    调用时机：心跳任务触发时（heartbeat.py 的 pacemaker_loop）

    API 端点: POST /open-apis/im/v1/messages?receive_id_type=chat_id
    参数说明：
      - receive_id_type=chat_id：表示 receive_id 是群聊 ID（oc_xxx 开头）
      - 如果发私聊消息，receive_id_type=open_id（ou_xxx 开头）

    Args:
        chat_id: 飞书群聊 ID，格式 oc_xxxxxxxx
        content: 要发送的文本内容

    Returns:
        True 表示发送成功
    """
    token = await _get_tenant_token()
    if not token:
        return False

    # 主动发送消息的 API（非回复）
    url = "https://open.feishu.cn/open-apis/im/v1/messages"
    params = {"receive_id_type": "chat_id"}   # 发送目标类型：群聊
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = {
        "receive_id": chat_id,                # 目标群聊 ID
        "content": json.dumps({"text": content}),  # 消息内容（JSON 字符串）
        "msg_type": "text",
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url, headers=headers, json=body, params=params, timeout=10
            )
            data = resp.json()
    except Exception as e:
        _status(f"Send error: {e}", "error")
        return False

    if data.get("code") != 0:
        _status(f"Send failed: {data.get('msg')}", "error")
        return False

    return True


# ============================================================
# feishu_listener — 主监听协程（运行在 TraceClaw 的 event loop 中）
# ============================================================
# 这是飞书集成在 TraceClaw 侧的入口点。
# main.py 通过 asyncio.create_task(feishu.feishu_listener(task_queue)) 启动。
#
# 职责：
#   1. 加载配置，检查飞书是否启用
#   2. 启动 SDK 后台线程（_run_sdk_client）
#   3. 循环轮询 _bridge 桥接队列
#   4. 将桥接队列的消息转发到 TraceClaw 的 task_queue
#
# 轮询机制（为什么不用阻塞 get？）：
#   - queue.Queue.get() 是阻塞的 → 阻塞 event loop → 其他协程无法运行
#   - get_nowait() + asyncio.sleep(0.2) → 不阻塞 event loop
#   - 200ms 的轮询间隔对聊天机器人来说延迟完全可接受
#   - 可以随时响应 asyncio.CancelledError（优雅退出）

async def feishu_listener(task_queue: asyncio.Queue):
    """
    飞书消息监听协程 — 桥接 SDK 事件到 TraceClaw 消息总线。

    main.py 在启动时通过 asyncio.create_task() 调用此函数，
    与 agent_worker、pacemaker_loop 并发运行。

    生命周期：
      1. 加载配置 → 未启用则直接返回（不启动线程，不占用资源）
      2. 启动 SDK 后台线程（feishu-sdk）
      3. 进入轮询循环：检查 _bridge → 有消息就推入 task_queue
      4. 进程退出时 → asyncio.CancelledError → 优雅停止
    """
    # === 阶段 1：加载配置 ===
    # _load_config 从 .env 读取 FEISHU_ENABLED / APP_ID / APP_SECRET
    # 未启用或配置不全 → 直接返回，不启动任何后台资源
    if not _load_config():
        return

    _status(f"Enabled (App ID: {_app_id[:12]}***)", "ok")

    # === 阶段 2：启动 SDK 后台线程 ===
    # daemon=True：主进程退出时自动终止（不会阻止进程退出）
    # name="feishu-sdk"：方便调试时在线程列表中识别
    sdk_thread = threading.Thread(
        target=_run_sdk_client, daemon=True, name="feishu-sdk"
    )
    sdk_thread.start()

    # === 阶段 3：桥接轮询循环 ===
    # 这个循环是 TraceClaw 与飞书之间的"物流中转站"：
    #   _bridge（线程安全队列）→ task_queue（asyncio 队列）
    _status("Bridge active, waiting for messages...", "info")
    while True:
        try:
            try:
                # 非阻塞地检查桥接队列是否有新消息
                # 200ms 间隔 → 每秒检查 5 次 → 消息延迟最多 200ms
                text = _bridge.get_nowait()
                # 推入 TraceClaw 消息总线 → agent_worker 会消费并送入 LangGraph
                await task_queue.put(text)
            except queue.Empty:
                # 队列空 → 让出 CPU，200ms 后再检查
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            # 进程退出时 TraceClaw 会 cancel 所有后台任务
            # 优雅处理——不做任何清理，daemon 线程会随进程自动终止
            _status("Listener stopped", "info")
            break
