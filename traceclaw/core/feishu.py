"""
TraceClaw Feishu Integration — Feishu/Lark Bot Message Channel
===============================================================
Uses the official lark-oapi SDK's WebSocket long-connection client
to receive bot messages and pushes them into TraceClaw's message bus.

Architecture:
  Feishu WS event -> SDK Client (bg thread) -> bridge Queue -> feishu_listener() -> task_queue

Prerequisites (Feishu Open Platform):
  1. Create a self-built enterprise app at https://open.feishu.cn/
  2. Enable "Bot" capability
  3. Under "Event Subscription":
     - Subscribe to im.message.receive_v1
     - Select "Use Long Connection" mode (no public IP needed)
  4. Get App ID / App Secret -> fill in .env

Environment variables (.env):
  FEISHU_ENABLED=true
  FEISHU_APP_ID=cli_xxxxxxxx
  FEISHU_APP_SECRET=xxxxxxxx
"""

import os
import sys
import json
import queue
import threading
import asyncio
from typing import Optional, Dict, Any

import httpx

# ---- Terminal output ----

def _status(msg: str, kind: str = "info"):
    """Print status message to terminal with ANSI color."""
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
        sys.stdout.flush()
    except Exception:
        pass


# ---- State ----

_enabled = False
_app_id = ""
_app_secret = ""

# Pending reply context (message_id to reply to after agent finishes)
_pending_reply: Optional[Dict[str, str]] = None

# Thread-safe bridge: SDK event handler (bg thread) -> feishu_listener (asyncio)
_bridge: queue.Queue = queue.Queue()

# Cached tenant_access_token for message reply API
_tenant_token: Optional[str] = None
_tenant_token_expires_at: float = 0.0


def _load_config() -> bool:
    global _enabled, _app_id, _app_secret
    _enabled = os.getenv("FEISHU_ENABLED", "false").lower() == "true"
    if not _enabled:
        return False
    _app_id = os.getenv("FEISHU_APP_ID", "")
    _app_secret = os.getenv("FEISHU_APP_SECRET", "")
    if not _app_id or not _app_secret:
        _status("FEISHU_ENABLED=true but missing credentials, disabled", "warn")
        _enabled = False
        return False
    return True


# ---- Token management (for reply API) ----

async def _get_tenant_token() -> Optional[str]:
    global _tenant_token, _tenant_token_expires_at
    import time
    now = time.time()
    if _tenant_token and now < _tenant_token_expires_at - 300:
        return _tenant_token

    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    body = {"app_id": _app_id, "app_secret": _app_secret}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=body, timeout=10)
            data = resp.json()
    except Exception as e:
        _status(f"Tenant token error: {e}", "error")
        return _tenant_token

    if data.get("code") != 0:
        _status(f"Tenant token failed: {data.get('msg')}", "error")
        return None

    _tenant_token = data["tenant_access_token"]
    _tenant_token_expires_at = now + data.get("expire", 7200)
    return _tenant_token


# ---- Message content decoder ----

def _decode_content(message_type: str, content_json: str) -> str:
    """Decode Feishu message content JSON into plain text."""
    try:
        content = json.loads(content_json)
    except (json.JSONDecodeError, TypeError):
        return content_json or ""

    if message_type == "text":
        return content.get("text", "")
    elif message_type == "post":
        parts = content.get("content", [[]])
        texts = []
        for paragraph in parts:
            for elem in paragraph:
                if isinstance(elem, dict):
                    texts.append(elem.get("text", elem.get("title", "")))
        return " ".join(texts)
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


# ---- SDK event handler (runs in SDK's background thread) ----

# ---- Event handlers (run in SDK's background thread) ----

def _on_bot_added(event_data):
    """Called when the bot is added to a chat — confirms event delivery works."""
    _status("Bot was added to a chat! Event delivery is working.", "ok")


def _on_message_receive(event_data):
    """Called by the SDK when im.message.receive_v1 fires."""
    global _pending_reply

    _status("Event received: im.message.receive_v1", "ok")

    try:
        msg = event_data.event.message
    except Exception as e:
        _status(f"Failed to parse event: {e}", "error")
        return

    # Filter bot's own messages (prevent infinite loop)
    if getattr(msg, 'chat_type', None) == "bot":
        _status("Filtered: bot's own message", "info")
        return

    message_id = getattr(msg, 'message_id', '')
    if not message_id:
        _status("Filtered: no message_id", "warn")
        return

    msg_type = getattr(msg, 'message_type', 'text')
    content_raw = getattr(msg, 'content', '{}')
    text = _decode_content(msg_type, content_raw)

    if not text.strip():
        _status(f"Filtered: empty content (type={msg_type})", "info")
        return

    _pending_reply = {"message_id": message_id}

    agent_input = f"[From Feishu] {text}"
    _status(f"Msg: {text[:60]}{'...' if len(text) > 60 else ''}", "event")
    _bridge.put(agent_input)


# ---- SDK client thread ----

def _run_sdk_client():
    """
    Run the lark-oapi WebSocket client in a dedicated background thread.

    The SDK's Client.start() blocks by running its own event loop,
    so it must live in its own thread. Event callbacks bridge to
    TraceClaw's asyncio world via a thread-safe queue.
    """
    from lark_oapi.ws import Client
    from lark_oapi.event.dispatcher_handler import EventDispatcherHandler

    # Build event handler with message receive + bot-added canary
    handler = (
        EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(_on_message_receive)
        .register_p2_im_chat_member_bot_added_v1(_on_bot_added)
        .build()
    )

    client = Client(
        app_id=_app_id,
        app_secret=_app_secret,
        event_handler=handler,
        auto_reconnect=True,
    )

    _status("SDK client starting (long-connection mode)...", "info")

    try:
        client.start()
    except Exception as e:
        _status(f"SDK client fatal error: {e}", "error")
        _status("Feishu listener has stopped", "warn")


# ---- Public API ----

def is_enabled() -> bool:
    return _enabled


def has_pending_reply() -> bool:
    return _pending_reply is not None


async def reply_message(content: str) -> bool:
    """Reply to the pending Feishu message using the REST API."""
    global _pending_reply

    if not _pending_reply:
        return False

    message_id = _pending_reply["message_id"]
    _pending_reply = None

    token = await _get_tenant_token()
    if not token:
        _pending_reply = {"message_id": message_id}
        return False

    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = {
        "content": json.dumps({"text": content}),
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


# ---- Main listener coroutine (runs in TraceClaw's event loop) ----

async def feishu_listener(task_queue: asyncio.Queue):
    """
    Feishu message listener — bridges SDK events into TraceClaw's message bus.

    Starts the lark-oapi SDK client in a background thread, then polls
    the bridge queue and forwards messages to task_queue.
    """
    if not _load_config():
        return

    _status(f"Enabled (App ID: {_app_id[:12]}***)", "ok")

    # Start SDK client in background thread
    sdk_thread = threading.Thread(target=_run_sdk_client, daemon=True, name="feishu-sdk")
    sdk_thread.start()

    # Poll bridge queue and forward to TraceClaw's task_queue
    _status("Bridge active, waiting for messages...", "info")
    while True:
        try:
            try:
                text = _bridge.get_nowait()
                await task_queue.put(text)
            except queue.Empty:
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            _status("Listener stopped", "info")
            break
