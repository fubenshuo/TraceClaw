"""
TraceClaw 监控终端 — JSONL 日志实时可视化面板
==============================================
一个独立的 Rich TUI 程序，通过 tail -f 方式实时读取并美化渲染
agent 运行过程中产生的 JSONL 审计日志。

架构角色：
  这是一个纯只读的观察者。它不参与 Agent 的任何逻辑——
  只是在另一个终端窗口中展示 "Agent 正在做什么"。

使用方式：
  # 终端 1：启动 Agent
  traceclaw run

  # 终端 2：启动监控面板（观察 Agent 的一举一动）
  traceclaw monitor

数据流：
  agent.py (audit_logger.log_event)
       ↓
  logger.py (_write_loop 后台线程)
       ↓
  logs/local_geek_master.jsonl (一行一条 JSON)
       ↓
  monitor.py (tail_f → render_event → Rich Panel 渲染)

四种事件的渲染方式：
  - llm_input:      单行文本，显示 "发送了 N 条上下文记忆"
  - tool_call:      Panel 面板，显示工具名和传入参数
  - tool_result:    Panel 面板，显示工具名和返回结果（截断到 300 字符）
  - system_action:  单行文本，显示系统动作内容

面试要点：
  - tail -f 模拟：seek(0, 2) 跳到文件末尾，readline() 阻塞等待新行
  - 为什么不用 watchdog？→ 零外部依赖，readline 轮询对日志场景完全够用
  - Rich Panel 的 width=60 设计 → 终端宽度适配，防止长 JSON 参数撑破面板
"""

import time
import json
import os
from rich.console import Console
from rich.theme import Theme
from rich.panel import Panel
from rich.text import Text
from rich.align import Align
from rich import box
from datetime import datetime

# ── Rich 主题定义 ──
# 为 5 种日志事件类型定义专属颜色：
#   info         → 暗青色（辅助信息）
#   warning      → 紫色 #8d52ff（系统动作，呼应 TraceClaw 主题色）
#   error        → 红色加粗（错误）
#   llm_input    → 暗白色（上下文输入，不那么显眼）
#   tool_call    → 黄色加粗（工具调用，醒目）
#   tool_result  → 绿色加粗（工具成功返回，正向信号）
#   ai_message   → 亮品红加粗（最终回复，最重要）
#   timestamp    → 暗白色（时间戳，辅助信息）
cyber_theme = Theme({
    "info": "dim cyan",
    "warning": "color(141)",
    "error": "bold red",
    "llm_input": "dim white",
    "tool_call": "bold yellow",
    "tool_result": "bold green",
    "ai_message": "bold bright_magenta",
    "timestamp": "dim white"
})

# Rich Console 实例，绑定 cyber_theme
console = Console(theme=cyber_theme)

# ── 日志文件路径 ──
# 当前硬编码为 local_geek_master.jsonl（对应 main.py 中的 thread_id）
# 如果未来支持多会话，这里需要改为动态路径
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_FILE = os.path.join(PROJECT_ROOT, "logs", "local_geek_master.jsonl")

def print_header():
    """
    渲染监控面板的标题区 — 展示 ASCII Art 怪兽图标和 "Live Stream" 标题。

    使用 Rich 的 Panel + Align.center + Text 组合：
      - Panel 用紫色 (#8d52ff) 圆角边框
      - 标题栏显示 "TraceClaw"
      - 内容区显示 ASCII 怪兽 + "Live Stream" + 副标题
    """

    # ASCII Art — TraceClaw 怪兽形象
    monster = (
        "  ▄█▄▄█▄  \n"
        " ▀██████▀ \n"
        " ██▄██▄██ \n"
        "  ▀    ▀  "
    )

    # Text 组件：支持多段不同样式的文本拼接
    content = Text(justify="center")
    content.append("\n  Live Stream  \n\n", style="bold white italic")
    content.append(monster + "\n\n", style="color(141)")
    content.append("   What is TraceClaw doing?    \n", style="dim white italic")

    # Panel 将内容包裹在带边框的盒子中
    # Align.center 让 Panel 在终端中水平居中
    panel = Panel(
        Align.center(content),
        title="[bold color(141)] TraceClaw [/bold color(141)]",
        title_align="left",
        border_style="color(141)",
        box=box.ROUNDED,
        width=42,               # 固定宽度，保证怪兽图案不被拉伸
        padding=0
    )

    console.print(Align.center(panel))
    console.print()

def tail_f(filepath):
    """
    模拟 tail -f 的文件末尾监听生成器。

    实现原理：
      1. 如果文件不存在，等待直到它被创建（Agent 首次运行时才创建日志文件）
      2. seek(0, 2) → 跳到文件末尾（不读取已有内容）
      3. 死循环 readline() → 有新行就 yield，没新行就 sleep(0.1)

    为什么不用 inotify / watchdog？
      - JSONL 一行一条记录，readline 轮询足够简单可靠
      - 零依赖，跨平台（Windows / Linux / Mac 都能跑）
      - 0.1s 的轮询间隔对人眼来说已经接近实时

    Args:
        filepath: JSONL 日志文件的绝对路径

    Yields:
        每一行 JSON 字符串（去掉末尾换行符）
    """
    # ── 等待日志文件生成 ──
    # Agent 可能还没启动——轮询直到文件出现
    if not os.path.exists(filepath):
        console.print(f"[warning]⏳ 等待日志文件生成...[/warning]")
        while not os.path.exists(filepath):
            time.sleep(0.5)

    # 打开文件并跳到末尾
    with open(filepath, 'r', encoding='utf-8') as f:
        f.seek(0, 2)        # 参数 2 = os.SEEK_END，即跳到文件末尾
        print_header()      # 文件就绪后才显示标题

        # 死循环：每次读取一行新内容
        while True:
            line = f.readline()
            if not line:
                # 没有新行 → 休眠 0.1 秒后重试（防止空转占满 CPU）
                time.sleep(0.1)
                continue
            yield line

def render_event(line: str):
    """
    解析单行 JSONL 日志并渲染为 Rich 组件。

    事件类型 → 渲染策略：
      llm_input     → 单行 "[时间] 🧠 神经元唤醒：发送了 N 条上下文记忆..."
      tool_call     → Panel("✦ 意图决断") 展示工具名和参数
      tool_result   → Panel("✦ 环境回传") 展示工具名和返回结果
      system_action → 单行 "[时间] ✦ 底层状态机：..."

    Args:
        line: 一行 JSON 字符串（来自 tail_f 生成器）
    """
    try:
        data = json.loads(line.strip())
        event = data.get("event")

        # ── 时间戳解析 ──
        # JSONL 中存储的是 UTC 时间（ISO 8601 格式："2026-06-03T12:30:00Z"）
        # 转换为本地时区后取 HH:MM:SS 显示
        ts_str = data.get("ts", "")
        try:
            # ISO 8601 中 Z = UTC，替换为 +00:00 以兼容 fromisoformat
            if ts_str.endswith('Z'): ts_str = ts_str[:-1] + '+00:00'
            dt_local = datetime.fromisoformat(ts_str).astimezone()
            ts = dt_local.strftime("%H:%M:%S")
        except:
            # 解析失败时用字符串切片兜底
            ts = ts_str.split("T")[-1][:8]

        # 时间戳前缀（所有事件类型共用）
        prefix = f"[timestamp][ {ts} ][/timestamp] "

        # ── 事件 1：llm_input（LLM 输入） ──
        # 记录发送给 LLM 的上下文消息数量
        if event == "llm_input":
            count = data.get("message_count", 0)
            console.print(f"{prefix}[llm_input]🧠 神经元唤醒：发送了 {count} 条上下文记忆...[/llm_input]")

        # ── 事件 2：tool_call（工具调用） ──
        # LLM 决定调用某个工具——展示工具名和参数
        elif event == "tool_call":
            tool_name = data.get("tool", "unknown")
            # ensure_ascii=False 保证中文参数正常显示
            args_str = json.dumps(data.get("args", {}), ensure_ascii=False, indent=2)
            content = f"[bold white] ● 使用工具: [/bold white][bold color(141)]{tool_name}[/bold color(141)]\n传入参数:\n{args_str}"
            console.print(Panel(content, title=f"✦ 意图决断 [ {ts} ]", title_align="left", border_style="color(141)", width=60))

        # ── 事件 3：tool_result（工具返回） ──
        # 工具执行完毕——展示工具名和返回结果
        elif event == "tool_result":
            tool_name = data.get("tool", "unknown")
            result = data.get("result_summary", "")
            # 截断长结果（> 300 字符），防止输出刷屏
            display_result = result[:300] + "\n...[截断]..." if len(result) > 300 else result
            content = f"[bold white] ● 执行结果: [/bold white][bold cyan]{tool_name}[/bold cyan]\n{display_result}"
            console.print(Panel(content, title=f"✦ 环境回传 [ {ts} ]", title_align="left", border_style="cyan", width=60))

        # ── 事件 4：system_action（系统动作） ──
        # 上下文裁剪、心跳触发等系统级事件
        elif event == "system_action":
            action = data.get("content", "")
            console.print(f"{prefix}[warning]✦ 底层状态机：{action}[/warning]")

    except:
        # 单行解析失败不中断整体流程（JSON 格式异常、编码问题等）
        pass

def main():
    """
    监控面板入口 — 清屏 → tail -f 监听 → 逐行渲染。

    退出方式：Ctrl+C（KeyboardInterrupt）优雅退出。
    """
    try:
        console.clear()
        # tail_f 是生成器，for 循环每次迭代处理一行新日志
        for line in tail_f(LOG_FILE):
            render_event(line)
    except KeyboardInterrupt:
        console.print("\n[warning]✦ 监控网络已断开。[/warning]")

if __name__ == "__main__":
    main()
