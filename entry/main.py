"""
TraceClaw 主运行时 — Prompt Toolkit TUI 交互终端
================================================
这是用户与 TraceClaw Agent 交互的主界面——一个基于 Prompt Toolkit
构建的全屏终端应用，包含赛博风格的 ASCII 启动画面、旋转动画、
底部状态栏、流式输出渲染。

架构全景（3 个并发协程）：
  ┌─────────────────────────────────────────────────────┐
  │                   asyncio event loop                │
  │                                                     │
  │  user_input_loop()     agent_worker()    pacemaker  │
  │  (Prompt Toolkit)      (LangGraph)       (heartbeat)│
  │       │                     ↑                 │     │
  │       │    task_queue       │                 │     │
  │       └───── put() ────────→┘                 │     │
  │                              └─── put() ──────┘     │
  └─────────────────────────────────────────────────────┘

输入流（两条路径汇入一条队列）：
  用户键盘 → user_input_loop → task_queue.put()
  心跳闹钟 → pacemaker_loop  → task_queue.put()
                                ↓
                          agent_worker.get() → LangGraph → 流式输出

核心技术栈：
  - Prompt Toolkit：全屏 TUI + 异步输入提示 + 底部工具栏
  - LangGraph AsyncSqliteSaver：对话状态 SQLite 持久化
  - asyncio：三协程并发协作

面试要点：
  - 为什么用 asyncio.Queue 而不是直接调用？→ 解耦输入源和处理逻辑，心跳和用户输入天然统一
  - SpinnerState 为什么是类而不是普通变量？→ 多协程共享状态需要可变容器
  - patch_stdout() 的作用？→ Prompt Toolkit 接管 stdout，防止 print() 破坏 TUI 布局
  - AsyncSqliteSaver.from_conn_string() 的 timeout=30000 → 30s 的 busy_timeout，防止并发写冲突
  - redraw_timer 为什么是 0.08s？→ 12.5 FPS 刚好够 spinner 动画流畅，再高就浪费 CPU
"""

import os
import sys
import time
import asyncio
import random
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.styles import Style
from prompt_toolkit.application import get_app

from traceclaw.core.agent import create_agent_app
from traceclaw.core.config import DB_PATH
from traceclaw.core.bus import task_queue
from traceclaw.core.heartbeat import pacemaker_loop
from traceclaw.core import feishu

# ── SQLite 连接 URI ──
# 使用 SQLite URI 格式加 busy_timeout，防止并发写入导致 "database is locked"
# timeout=30000 = 30 秒——SQLite 在锁冲突时会等待最多 30 秒，超时才报错
# WAL 模式下读写不互斥，但写-写仍然互斥，busy_timeout 给写操作留出等待窗口
_DB_URI = f"file:{DB_PATH}?timeout=30000"

def clear_screen():
    """跨平台清屏：Windows 用 cls，Linux/Mac 用 clear"""
    os.system('cls' if os.name == 'nt' else 'clear')

def type_line(text: str, delay: float = 0.008):
    """
    打字机效果 — 逐字符打印文本，模拟黑客电影中的终端效果。

    用于启动画面中的提示文字，给用户一种 "系统正在初始化" 的感觉。
    纯视觉体验——不涉及任何实际初始化逻辑。

    Args:
        text:  要逐字符显示的文本
        delay: 每个字符之间的延迟（秒），默认 0.008s ≈ 125 字符/秒
    """
    for ch in text:
        print(ch, end='', flush=True)
        time.sleep(delay)
    print()

def print_banner():
    """
    渲染 TraceClaw 赛博朋克风格启动画面。

    包含 4 个视觉元素：
      1. ASCII Art Logo（CYBER CLAW 大字，青色粗体）
      2. 欢迎语（白字，含紫色 TraceClaw 高亮）
      3. 随机极客名言（如 "It works on my machine."）
      4. 操作提示（/exit 退出）

    使用 ANSI 转义序列手动控制颜色（不使用 Rich），
    因为 Prompt Toolkit 的 print_formatted_text(ANSI(...)) 可以正确解析 ANSI。
    """
    clear_screen()

    # ── ANSI 颜色定义 ──
    # 38;5;N 是 256 色调色板语法：
    #   51 = 青色 (Cyan)
    #   141 = 紫色 (Purple) — TraceClaw 主题色
    #   250 = 银色 (Silver)
    #   255 = 白色
    CYAN = '\033[38;5;51m'
    PURPLE = '\033[38;5;141m'
    SILVER = '\033[38;5;250m'
    DIM = '\033[2m'
    BOLD = '\033[1m'
    RESET = '\033[0m'
    WHITE = '\033[37m'

    # ── ASCII Art Logo ──
    # 两行大字：TRACE + CLAW
    logo = f"""{CYAN}{BOLD}
████████╗██████╗  █████╗  ██████╗███████╗
╚══██╔══╝██╔══██╗██╔══██╗██╔════╝██╔════╝
   ██║   ██████╔╝███████║██║     █████╗
   ██║   ██╔══██╗██╔══██║██║     ██╔══╝
   ██║   ██║  ██║██║  ██║╚██████╗███████╗
   ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝╚══════╝

 ██████╗██╗      █████╗ ██╗    ██╗
██╔════╝██║     ██╔══██╗██║    ██║
██║     ██║     ███████║██║ █╗ ██║
██║     ██║     ██╔══██║██║███╗██║
╚██████╗███████╗██║  ██║╚███╔███╔╝
 ╚═════╝╚══════╝╚═╝  ╚═╝ ╚══╝╚══╝
{RESET}"""

    # ── 欢迎语 ──
    sub_title = f"{WHITE}{BOLD} 👾 Welcome to the {PURPLE}{BOLD}TraceClaw{RESET}{WHITE}{BOLD} !  {RESET}"

    # ── 极客名言库 ──
    # 每次启动随机选一条显示，给用户一个小小的彩蛋
    quotes = [
        "It works on my machine.",
        "It compiles! Ship it.",
        "Git commit, push, pray.",
        "There's no place like 127.0.0.1.",
        "sudo make me a sandwich.",
        "Works fine in dev.",
        "May the source be with you.",
        "Ctrl+C, Ctrl+V, Deploy.",
        "Hello, World."
    ]
    quote = random.choice(quotes)
    meta = f" {SILVER}✦{RESET} {CYAN}{quote}{RESET}"

    # ── 操作提示 ──
    tip = (
        f"{PURPLE} ✦ {RESET}"
        f"{SILVER}{PURPLE}{BOLD}TraceClaw{RESET} 已完成启动。输入命令开始，输入 {PURPLE}/exit{RESET}{SILVER} 退出。{RESET}\n"
    )

    # 逐块打印（手动控制顺序和延迟）
    print(logo)
    print(sub_title)
    print()
    time.sleep(0.12)      # 短暂延迟，营造启动仪式感
    print(meta)
    print()
    type_line(tip, delay=0.004)   # 打字机效果


def cprint(text="", end="\n"):
    """
    Prompt Toolkit 兼容的打印函数。

    在 Prompt Toolkit 环境中，不能直接用 print()——
    必须用 print_formatted_text(ANSI(...)) 才能正确渲染 ANSI 颜色代码。
    这个函数是对 ANSI 格式打印的快捷封装。

    Args:
        text: 要打印的文本（支持 ANSI 转义序列）
        end:  行尾字符（默认换行）
    """
    print_formatted_text(ANSI(str(text)), end=end)


# ============================================================
# async_main — 主异步入口
# ============================================================
async def async_main():
    """
    TraceClaw 的主异步运行时 — 初始化 → 启动三协程 → 运行。

    按照以下顺序执行：
      1. 打印启动画面（Banner）
      2. 加载 .env 配置
      3. 创建 SQLite Checkpointer（对话持久化）
      4. 创建 Agent 应用（LangGraph StateGraph）
      5. 初始化 Spinner 状态（旋转动画控制器）
      6. 启动三个并发协程：
         - agent_worker      : 从队列消费消息 → 调用 LangGraph → 流式输出
         - heartbeat_worker  : 每 10s 扫描定时任务
         - user_input_loop   : 读取用户键盘输入 → 推入队列
      7. 优雅退出：队列清空 → 协程取消
    """
    print_banner()

    # ── 加载 .env ──
    # load_dotenv 读取项目根目录的 .env 文件，将配置注入 os.environ
    from dotenv import load_dotenv
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    load_dotenv(env_path)

    # 从环境变量获取当前使用的 Provider 和 Model
    current_provider = os.getenv("DEFAULT_PROVIDER", "aliyun")
    current_model = os.getenv("DEFAULT_MODEL", "glm-5")

    # ── AsyncSqliteSaver 上下文管理器 ──
    # async with 保证在退出时自动关闭数据库连接
    # from_conn_string() 接收 SQLite URI，自动启用 WAL 模式
    async with AsyncSqliteSaver.from_conn_string(_DB_URI) as memory:
        # 创建 Agent 应用，传入 checkpointer=memory 启用对话持久化
        app = create_agent_app(provider_name=current_provider, model_name=current_model, checkpointer=memory)

        # ── LangGraph 运行时配置 ──
        # thread_id 是会话的唯一标识——同一 thread_id 的对话共享 checkpoint 链
        # "local_geek_master" 是硬编码的单会话 ID（当前版本只支持一个会话）
        config = {"configurable": {"thread_id": "local_geek_master"}}

        # ============================================================
        # SpinnerState — 旋转动画状态容器
        # ============================================================
        # 这是一个可变状态对象，被 agent_worker 和 user_input_loop 两个协程共享：
        #   agent_worker:    设置 is_spinning / is_tool_calling / tool_msg
        #   user_input_loop: 读取这些状态 → get_bottom_toolbar() 渲染底部工具栏
        #
        # 为什么不用 asyncio.Queue 传递状态？
        #   → 因为底部工具栏每 0.08s 刷新一次，需要的是"最新状态"而非"事件流"
        #   → 可变对象是最简单的共享状态方案（GIL 保证了基本线程安全）
        # ============================================================
        class SpinnerState:
            # LLM 思考时的随机俏皮话（每次思考随机打乱顺序，增加趣味性）
            action_words = [
                "Thinking...",
                "Working...",
                "Beep boop...",
                "Eating bugs...",
                "Charging battery...",
                "Brewing coffee...",
                "Blinking lights...",
                "Polishing pixels...",
                "Scanning matrix...",
                "Warming up circuits...",
                "Syncing data...",
                "Pinging server..."
            ]
            current_words = []     # 本轮思考的随机词序（agent_worker 在每次请求前设置）
            is_spinning = False    # 是否正在等待 LLM 响应
            start_time = 0         # 本轮思考的开始时间（用于计算 elapsed）
            frames = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']  # Braille 旋转帧
            is_tool_calling = False  # 当前是否正在执行工具（vs 纯思考）
            tool_msg = ""            # 工具调用时的提示文本（如 "唤醒内置工具 : calculator..."）

        spinner = SpinnerState()

        # ============================================================
        # get_bottom_toolbar — Prompt Toolkit 底部状态栏回调
        # ============================================================
        # 这个函数被 Prompt Toolkit 高频调用（配合 redraw_timer 每 0.08s 一次），
        # 用于渲染输入区域下方的状态栏。
        #
        # 三种显示模式：
        #   1. 空闲：不显示任何内容（is_spinning = False）
        #   2. 纯思考：显示 "👾 Thinking..."  + 旋转帧 + 计时
        #   3. 工具调用：显示 "唤醒内置工具 : xxx..." + 旋转帧 + 计时
        #
        # 旋转帧的计算：
        #   elapsed * 12 → 每秒切换 12 帧（与 frames 列表的 10 帧大致匹配）
        #   % len(frames) → 循环使用 Braille 字符
        # ============================================================
        def get_bottom_toolbar():
            # 空闲状态 → 返回空字符串（工具栏不显示）
            if not spinner.is_spinning:
                return ANSI("")

            # 已流逝时间（秒）
            elapsed = time.time() - spinner.start_time

            # 根据当前状态选择显示文本
            if spinner.is_tool_calling:
                display_msg = spinner.tool_msg
            else:
                # 每秒钟换一个词（int(elapsed) 取整，按秒轮换）
                idx_word = int(elapsed) % len(spinner.current_words)
                display_msg = f"👾 {spinner.current_words[idx_word]}"

            # Braille 旋转帧（每秒 12 帧的速率）
            idx_frame = int(elapsed * 12) % len(spinner.frames)
            frame = spinner.frames[idx_frame]

            # 最终格式：旋转帧 + 状态文本 + 计时
            # 颜色：青色旋转帧 + 银色文本 + 紫色计时
            return ANSI(f"  \033[38;5;51m{frame}\033[0m \033[38;5;250m{display_msg}\033[0m \033[38;5;141m[{elapsed:.1f}s]\033[0m")

        # ── Prompt Toolkit 输入提示符 ──
        # 青色 "❯" 作为输入提示符（类似 zsh 的 agnoster 主题）
        prompt_message = ANSI("  \033[38;5;51m❯\033[0m ")

        # ── Placeholder 文本 ──
        # 输入框为空时显示的灰色斜体提示文字
        placeholder_text = ANSI("\033[3m\033[38;5;242minput...\033[0m")

        # ============================================================
        # 协程 1：agent_worker — 消息消费者
        # ============================================================
        # 这是整个系统的核心循环：死循环等待队列中的消息，
        # 有消息就调用 LangGraph 处理，然后流式输出结果。
        #
        # 处理流程：
        #   1. task_queue.get() 阻塞等待（有消息才往下走）
        #   2. 检查 /exit 指令 → break 退出
        #   3. 初始化 spinner 状态（打乱词序 + 记录开始时间）
        #   4. 将用户输入包装为 HumanMessage
        #   5. app.astream() 流式调用 LangGraph
        #   6. 遍历事件流：区分 agent 节点的 tool_call / text 输出
        #   7. 恢复 spinner 为空闲状态
        #   8. task_queue.task_done() 标记此消息处理完毕
        # ============================================================
        async def agent_worker():
            while True:
                # ── 阻塞等待消息 ──
                # asyncio.Queue.get() 是协程友好的阻塞——不占 CPU
                user_input = await task_queue.get()

                # ── /exit 指令检查 ──
                # /exit 和 /quit 都能退出（和 user_input_loop 保持一致）
                if user_input.lower() in ["/exit", "/quit"]:
                    task_queue.task_done()
                    break

                # ── 初始化 spinner ──
                # 每次新请求打乱俏皮话顺序，让用户每次看到不同的词序
                spinner.current_words = spinner.action_words.copy()
                random.shuffle(spinner.current_words)

                spinner.start_time = time.time()
                spinner.is_spinning = True
                spinner.is_tool_calling = False

                # ── 构造 LangGraph 输入 ──
                # messages 列表包含一条 HumanMessage（用户说的话）
                inputs = {"messages": [HumanMessage(content=user_input)]}

                # 追踪 Agent 的最终文本回复（用于飞书等外部渠道回复）
                final_response = None

                try:
                    # ── 流式调用 LangGraph ──
                    # app.astream() 返回一个异步生成器，每当 StateGraph 的一个节点
                    # 执行完毕就 yield 一个事件字典。
                    # stream_mode="updates" 表示只返回状态更新（而非完整的 checkpoint）
                    async for event in app.astream(inputs, config=config, stream_mode="updates"):
                        for node_name, node_data in event.items():
                            # === agent 节点的输出 ===
                            if node_name == "agent":
                                # 获取 agent 节点返回的最后一条消息
                                last_msg = node_data["messages"][-1]

                                # --- 情况 A：LLM 决定调用工具 ---
                                # tool_calls 是 AIMessage 的属性，包含 LLM 请求调用的工具列表
                                if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
                                    for tc in last_msg.tool_calls:
                                        # 切换 spinner 到工具调用模式
                                        spinner.is_tool_calling = True
                                        spinner.tool_msg = f"唤醒内置工具 : {tc['name']}..."
                                        # 打印工具调用信息（青色 Tool Call 标签 + 工具名）
                                        cprint(f"  ●\033[38;5;51m Tool Call: \033[0m{tc['name']}")
                                        cprint('')

                                # --- 情况 B：LLM 返回文本回复 ---
                                elif last_msg.content:
                                    # 停止旋转动画——回复已就绪
                                    spinner.is_spinning = False

                                    # 保存最终回复文本（供飞书等外部渠道使用）
                                    final_response = last_msg.content.strip()

                                    # 分段格式化输出：
                                    # 第一行以紫色 ❯ 开头，后续行缩进对齐
                                    lines = last_msg.content.strip().split('\n')
                                    if lines:
                                        formatted_out = f"  \033[38;5;141m❯\033[0m \033[38;5;250m{lines[0]}"
                                        for line in lines[1:]:
                                            formatted_out += f"\n    {line}"
                                        formatted_out += "\033[0m"
                                        cprint(formatted_out)

                            # === tools 节点的输出（非 agent 节点） ===
                            # 工具执行完毕后，spinner 回到纯思考模式
                            elif node_name != "agent":
                                spinner.is_tool_calling = False

                except Exception as e:
                    # 错误处理：停止 spinner，显示错误信息
                    spinner.is_spinning = False
                    cprint(f"  \033[31m[ ⚠️ 引擎异常 : {e} ]\033[0m")

                # ── 飞书回复（如果有待回复的飞书消息） ──
                if final_response and feishu.has_pending_reply():
                    try:
                        await feishu.reply_message(final_response)
                    except Exception as e:
                        cprint(f"  \033[31m[ 飞书回复失败 : {e} ]\033[0m")

                # ── 本轮处理完毕 ──
                spinner.is_spinning = False
                cprint() # 空出舒适的行距
                task_queue.task_done()

        # ============================================================
        # 协程 2：user_input_loop — 键盘输入生产者
        # ============================================================
        # 使用 Prompt Toolkit 的 PromptSession 提供全屏 TUI 体验。
        # 用户输入被推入 task_queue，由 agent_worker 消费。
        #
        # Prompt Toolkit 的核心概念：
        #   PromptSession   — 可复用的输入会话（维护历史和样式）
        #   bottom_toolbar   — 输入区域下方的动态状态栏
        #   get_app().invalidate() — 强制刷新 UI（用于 spinner 动画）
        #   patch_stdout()   — 接管 stdout，使 print 输出不破坏 TUI 布局
        # ============================================================
        async def user_input_loop():
            # ── Prompt Toolkit 样式 ──
            # 底部工具栏使用默认背景色（不反转颜色）
            custom_style = Style.from_dict({
                'bottom-toolbar': 'bg:default fg:default noreverse',
            })

            # ── 创建 PromptSession ──
            # bottom_toolbar=get_bottom_toolbar → 底部工具栏的内容由回调函数动态生成
            # erase_when_done=True → 用户提交后清除输入行
            # reserve_space_for_menu=0 → 底部不预留菜单空间（减少闪烁）
            session = PromptSession(
                bottom_toolbar=get_bottom_toolbar,
                style=custom_style,
                erase_when_done=True,
                reserve_space_for_menu=0
            )

            # ============================================================
            # redraw_timer — 屏幕刷新定时器
            # ============================================================
            # Prompt Toolkit 默认只在用户按键时刷新屏幕。
            # 但 spinner 动画需要在等待 LLM 响应时持续刷新——
            # 这个协程每 0.08s 调用 get_app().invalidate() 强制重绘。
            #
            # 0.08s ≈ 12.5 FPS——刚好够 Braille spinner 流畅旋转，
            # 再高就会无谓占用 CPU（刷新频率 > 显示设备刷新率没意义）。
            # ============================================================
            async def redraw_timer():
                while True:
                    if spinner.is_spinning:
                        try:
                            # invalidate() 触发 Prompt Toolkit 重新渲染整个 UI
                            get_app().invalidate()
                        except Exception:
                            pass
                    await asyncio.sleep(0.08)

            # 启动刷新定时器（作为后台任务）
            redraw_task = asyncio.create_task(redraw_timer())

            # ── 主输入循环 ──
            while True:
                try:
                    # prompt_async() 异步等待用户输入（不阻塞 event loop）
                    # 用户按 Enter 提交后返回输入字符串
                    user_input = await session.prompt_async(prompt_message, placeholder=placeholder_text)

                    # 去除首尾空白
                    user_input = user_input.strip()

                    # 空输入 → 跳过（不推入队列，避免浪费 LLM 调用）
                    if not user_input:
                        continue

                    # ── 在屏幕上回显用户输入 ──
                    # 深灰背景 + 白字的聊天气泡效果
                    padded_bubble = f"  ❯ {user_input}    "
                    cprint(f"\033[48;2;38;38;38m\033[38;5;255m{padded_bubble}\033[0m\n")

                    # ── 推入消息队列 ──
                    # 这是 user_input_loop 的核心输出——将用户输入交给 agent_worker 处理
                    await task_queue.put(user_input)

                    # ── /exit 退出检查 ──
                    if user_input.lower() in ["/exit", "/quit"]:
                        cprint("  \033[38;5;141m✦ 记忆已固化，TraceClaw 进入休眠。\033[0m")
                        break

                except (KeyboardInterrupt, EOFError):
                    # Ctrl+C 或 Ctrl+D → 优雅退出
                    cprint("\n  \033[38;5;141m✦ 强制中断，TraceClaw 进入休眠。\033[0m")
                    await task_queue.put("/exit")
                    break

            # 取消屏幕刷新定时器
            redraw_task.cancel()

        # ── 三协程并发启动 ──
        # patch_stdout() 是一个上下文管理器，将所有 print 输出重定向到
        # Prompt Toolkit 的渲染管线——这样 print 不会破坏 TUI 布局。
        with patch_stdout():
            # agent_worker、heartbeat_worker、feishu_listener 作为后台任务并发运行
            worker = asyncio.create_task(agent_worker())
            heartbeat_worker = asyncio.create_task(pacemaker_loop(task_queue, check_interval=10))
            feishu_worker = asyncio.create_task(feishu.feishu_listener(task_queue))

            # user_input_loop 在前台运行（await 阻塞直到用户退出）
            await user_input_loop()

            # ── 优雅退出 ──
            # 等待队列中的所有消息处理完毕
            await task_queue.join()

            # 取消后台协程
            worker.cancel()
            heartbeat_worker.cancel()
            feishu_worker.cancel()

def main():
    """
    TraceClaw 的全局入口点。

    被 cli.py 的 run_agent() 调用，也被 setup.py 的 console_scripts 注册：
      console_scripts = ["traceclaw-run = entry.main:main"]

    asyncio.run() 是整个异步世界的入口——它创建 event loop 并运行 async_main()。
    """
    asyncio.run(async_main())

if __name__ == "__main__":
    main()
