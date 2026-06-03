"""
TraceClaw CLI 入口 — Typer 命令行界面
=====================================
三个子命令，对应 TraceClaw 的三种运行模式：

  traceclaw config   → 交互式配置向导（设置 .env 中的模型、API Key、Base URL）
  traceclaw run      → 启动主 Agent 终端（Prompt Toolkit TUI 交互界面）
  traceclaw monitor  → 启动日志监控面板（Rich 驱动的 JSONL 实时查看器）

设计模式：
  - Typer 作为 CLI 框架，自动生成 --help 文档
  - questionary 实现交互式选择/输入（替代传统 input()，支持方向键导航）
  - config_wizard 的核心逻辑：交互式采集 → 探活验证 → 写入 .env
  - run / monitor 命令本质上只是"跳板"——它们 import 对应的模块后调用其 main()

面试要点：
  - 为什么用 Typer 而不是 argparse？→ Typer 基于类型注解自动生成 CLI，代码更简洁
  - 为什么 run 命令用 import 而不是直接 inline？→ 延迟导入，避免启动 CLI 时就加载 LangGraph 等重型依赖
  - 配置验证的"探测包"设计 → 不只检查格式，而是真发一条 LLM 请求验证连通性
"""

import os
import typer
import questionary
import logging
from rich.console import Console
from rich.panel import Panel
from rich.status import Status
from dotenv import set_key, load_dotenv, unset_key
import sys

from traceclaw.core.provider import get_provider
from langchain_core.messages import HumanMessage

# ── 路径解析 ──
# ENTRY_DIR  = TraceClaw-main/entry/
# PROJECT_ROOT = TraceClaw-main/
ENTRY_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(ENTRY_DIR)

# 将工作目录切换到项目根目录，确保 .env 等相对路径正确解析
os.chdir(PROJECT_ROOT)

# 确保项目根目录在 sys.path 中（pip install -e 模式下可能不需要，但直接运行需要）
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ── Typer 应用实例 ──
# Typer 是 FastAPI 作者开发的 CLI 框架，基于类型注解自动生成命令行界面
app = typer.Typer(help="TraceClaw - 极客专属的赛博智能终端")

# ── Rich Console ──
# Rich 提供终端美化：颜色、面板、进度条等
# Console 是 Rich 的核心输出对象，替代 print()
console = Console()

# ── questionary 赛博风格主题 ──
# questionary 是 Python 的交互式命令行问答库（类似 inquirer.js）
# 这里的 Style 定义了每个 UI 元素的 ANSI 颜色：
#   qmark       → 问号标记（紫色 #8d52ff）
#   question    → 问题文本（青色 #00ffff）
#   answer      → 用户回答（紫色）
#   pointer     → 选中指示器（青色）
#   highlighted → 高亮选项（青色加粗）
#   selected    → 已选选项（青色）
#   instruction → 提示文字（灰色）
cyber_style = questionary.Style([
    ('qmark', 'fg:#8d52ff bold'),
    ('question', 'fg:#00ffff bold'),
    ('answer', 'fg:#8d52ff bold'),
    ('pointer', 'fg:#00ffff bold'),
    ('highlighted', 'fg:#00ffff bold'),
    ('selected', 'fg:#00ffff'),
    ('instruction', 'fg:#808080 dim'),
])

# .env 文件的绝对路径
ENV_PATH = os.path.join(PROJECT_ROOT, ".env")

# ============================================================
# 命令 1：traceclaw config — 交互式配置向导
# ============================================================
@app.command("config")
def config_wizard():
    """
    TraceClaw 配置向导 — 交互式设置 LLM 提供商、模型和 API Key。

    流程（5 步）：
      1. 选择 Provider（openai / anthropic / aliyun / ollama / ...）
      2. 输入模型名称（如 gpt-4o-mini、glm-5）
      3. 输入 API Key（密码模式，输入时不回显）
      4. 输入 Base URL（可选，代理场景需要）
      5. 探活验证：发送 "回复我'收到'" 测试连通性
      6. 写入 .env 文件（使用 python-dotenv 的 set_key，保留已有配置）

    安全设计：
      - API Key 输入使用 questionary.password（输入时不显示明文）
      - .env 写入前先 unset_key 清除旧的 Base URL 配置，避免残留
      - 配置失败时不会写入任何内容到 .env
    """
    # ── 清屏 + 欢迎面板 ──
    console.clear()
    console.print(Panel(
        "👾 Welcome to [bold #8d52ff]TraceClaw[/bold #8d52ff]...\n\n☁️[dim] 请完成模型配置，我们将把密钥安全固化在本地。[/dim]",
        title="[bold white]✦  TraceClaw Config[/bold white]",
        border_style="#8d52ff"
    ))

    # ── 步骤 1：选择 Provider ──
    # questionary.select 提供方向键导航的选项列表
    # 返回值是用户选中的选项文本
    provider_raw = questionary.select(
        "选择你的模型提供商 (Provider):",
        choices=["openai", "anthropic", "aliyun (openai compatible)","tencent (openai compatible)", "z.ai (openai compatible)", "other (openai compatible)", "ollama"],
        style=cyber_style,
        instruction="(按上下键选择，回车确认)"
    ).ask()

    # 用户按 Ctrl+C 或 Esc 取消 → 返回 None
    if not provider_raw:
        console.print("[dim #8d52ff]✦   录入中断，TraceClaw 配置已取消。[/dim #8d52ff]")
        return

    # 从选项文本中提取纯 provider 名称（如 "aliyun (openai compatible)" → "aliyun"）
    provider = provider_raw.split(" ")[0].strip()

    # 判断是否为 OpenAI 兼容的提供商（aliyun、z.ai、tencent、other 都走 OpenAI 兼容 API）
    is_openai_compatible = "openai" in provider_raw.lower()

    # ── 步骤 2：输入模型名称 ──
    model_name = questionary.text(
        "输入指定的模型型号 (如 gpt-4o-mini, qwen-max, glm-4 等):",
        style=cyber_style
    ).ask()

    if model_name is None:
        console.print("[dim #8d52ff]✦   录入中断，TraceClaw 配置已取消。[/dim #8d52ff]")
        return

    # ── 步骤 3：输入 API Key ──
    # Ollama 是本地模型，不需要 API Key
    api_key = ""

    # env_key 决定写入 .env 中的哪个键名：
    #   OpenAI 兼容 → OPENAI_API_KEY
    #   Anthropic   → ANTHROPIC_API_KEY
    env_key = ""
    if provider != "ollama":
        if is_openai_compatible:
            env_key = "OPENAI_API_KEY"
        elif provider == "anthropic":
            env_key = "ANTHROPIC_API_KEY"

        # questionary.password 输入时不回显字符（类似 Unix passwd）
        api_key = questionary.password(
            f"输入你的 {env_key} (对应 {provider_raw}):",
            style=cyber_style
        ).ask()

        if api_key is None:
            console.print("[dim #8d52ff]✦   录入中断，TraceClaw 配置已取消。[/dim #8d52ff]")
            return

    # ── 步骤 4：输入 Base URL（可选） ──
    # OpenAI / Anthropic 官方 → 直连不需要填，代理场景需要填
    # aliyun / z.ai / tencent → provider.py 已有内置默认 Base URL，不填则用默认
    # Ollama → 默认 localhost:11434
    base_url = ""
    if provider in ["openai", "anthropic"]:
        base_url = questionary.text(
            f"输入 {provider} 代理 Base URL (直连请直接回车跳过):",
            style=cyber_style
        ).ask()
    elif provider == "ollama":
        base_url = questionary.text(
            "输入 Ollama Base URL (默认 http://localhost:11434，直接回车跳过):",
            style=cyber_style
        ).ask()
    else:
        # 兼容提供商（aliyun, z.ai, tencent, other）
        base_url = questionary.text(
            "输入兼容 Base URL (不填直接回车将使用官方默认地址):",
            style=cyber_style
        ).ask()

    if base_url is None:
        console.print("[dim #8d52ff]✦   录入中断，TraceClaw 配置已取消。[/dim #8d52ff]")
        return

    # ── 步骤 5：探活验证 ──
    # 不只是检查格式，而是真正发一条 LLM 请求，
    # 用 "回复我'收到'" 测试连通性——能通才算配置成功
    console.print("\n[dim]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/dim]")

    # Rich Status 组件显示旋转动画（spinner），类似 npm install 的等待效果
    with Status(f"[bold #8d52ff]正在连接 {provider.upper()} 引擎并发送探测包...[/bold #8d52ff]", spinner="dots", spinner_style="#00ffff"):
        try:
            # 临时设置环境变量，供 get_provider() 读取
            if env_key and api_key:
                os.environ[env_key] = api_key
            if base_url:
                if is_openai_compatible:
                    os.environ["OPENAI_API_BASE"] = base_url
                else:
                    os.environ[f"{provider.upper()}_BASE_URL"] = base_url

            # 获取 LLM 实例并发探测请求
            llm = get_provider(provider_name=provider, model_name=model_name)
            response = llm.invoke([HumanMessage(content="回复我'收到'。")])

            console.print(" [bold #00ffff][ 配置成功!][/bold #00ffff]")

        except Exception as e:
            # 探测失败：Key 不对、网络不通、模型名错误等
            console.print(f" [bold #8d52ff][ 配置失败!][/bold #8d52ff]  无法连接到模型，请检查 Key、Base URL、模型型号 或 网络！\n[dim]错误信息: {str(e)}[/dim]")
            return

    # ── 步骤 6：写入 .env 文件 ──
    # 如果 .env 不存在，创建一个空文件
    if not os.path.exists(ENV_PATH):
        open(ENV_PATH, 'w').close()

    # 抑制 dotenv 的 INFO 日志（如 "Python-dotenv could not find configuration file..."）
    logging.getLogger("dotenv.main").setLevel(logging.ERROR)

    # 先清除旧的 Base URL 配置，避免同一 provider 多次配置时残留旧值
    # unset_key 会删除 .env 中对应的键（如果存在）
    unset_key(ENV_PATH, "OPENAI_API_BASE")
    unset_key(ENV_PATH, "ANTHROPIC_BASE_URL")
    unset_key(ENV_PATH, "OLLAMA_BASE_URL")

    # 写入 API Key
    if env_key and api_key:
        set_key(ENV_PATH, env_key, api_key)

    # 写入 Base URL（仅当用户填写时）
    if base_url:
        if is_openai_compatible:
            set_key(ENV_PATH, "OPENAI_API_BASE", base_url)
        else:
            set_key(ENV_PATH, f"{provider.upper()}_BASE_URL", base_url)

    # 写入默认 Provider 和 Model（每次 config 都会更新这两个字段）
    set_key(ENV_PATH, "DEFAULT_PROVIDER", provider)
    set_key(ENV_PATH, "DEFAULT_MODEL", model_name)

    # 配置完成，显示最终面板
    console.print(Panel(
        f"配置已保存至 [#8d52ff]{ENV_PATH}[/#8d52ff]\n"
        f"当前默认提供商: [#8d52ff]{provider}[/#8d52ff] | 模型: [#8d52ff]{model_name}[/#8d52ff]\n\n"
        f"👉 输入 [bold #00ffff]traceclaw run[/bold #00ffff] 即可启动系统！",
        border_style="#00ffff"
    ))

# ============================================================
# 启动失败提示面板（run 命令复用）
# ============================================================
def _show_boot_error():
    """
    当检测到配置不完整时，显示引导用户执行 traceclaw config 的提示面板。

    触发条件（在 run_agent 中检查）：
      - DEFAULT_PROVIDER 或 DEFAULT_MODEL 缺失
      - 非 Ollama 但对应的 API Key 未配置
    """
    console.print(Panel(
        "[bold #00ffff]TraceClaw未完成配置![/bold #00ffff]\n\n"
        "[#8d52ff]检测到 API Key、模型或Baseurl。请重新执行以下命令完成配置：[/#8d52ff]\n"
        "[bold #00ffff]traceclaw config[/bold #00ffff]",
        title="[bold #8d52ff]⚠️ Boot Sequence Failed[/bold #8d52ff]",
        border_style="#8d52ff"
    ))


# ============================================================
# 命令 2：traceclaw run — 启动主 Agent 终端
# ============================================================
@app.command("run")
def run_agent():
    """
    启动 TraceClaw 的主交互终端。

    启动前检查：
      1. .env 中必须有 DEFAULT_PROVIDER 和 DEFAULT_MODEL
      2. 非 Ollama 的 provider 必须有对应的 API Key

    通过检查后，延迟导入 entry.main 并调用其 main() 函数。
    延迟导入的好处：执行 traceclaw --help 或 traceclaw config 时
    不需要加载 LangGraph、Prompt Toolkit 等重型依赖。
    """
    # 加载 .env 文件中的环境变量
    load_dotenv(ENV_PATH)

    provider = os.getenv("DEFAULT_PROVIDER")
    model = os.getenv("DEFAULT_MODEL")

    # 检查 1：Provider 和 Model 必须存在
    if not provider or not model:
        _show_boot_error()
        raise typer.Exit()

    # 检查 2：非 Ollama 的 provider 必须有 API Key
    if provider != "ollama":
        # OpenAI 兼容提供商（openai, aliyun, z.ai, tencent, other）
        if provider in ["openai", "aliyun", "z.ai", "tencent", "other"]:
            if not os.getenv("OPENAI_API_KEY"):
                _show_boot_error()
                raise typer.Exit()

        # Anthropic 需要 ANTHROPIC_API_KEY
        elif provider == "anthropic":
            if not os.getenv("ANTHROPIC_API_KEY"):
                _show_boot_error()
                raise typer.Exit()

    # 延迟导入：只有确认要启动时才加载 main.py（含 LangGraph、Prompt Toolkit）
    import entry.main as traceclaw_main
    traceclaw_main.main()

# ============================================================
# 命令 3：traceclaw monitor — 启动日志监控面板
# ============================================================
@app.command("monitor")
def run_monitor():
    """
    启动 JSONL 日志的实时监控面板。

    监控面板通过 tail -f 式的方式读取 logs/local_geek_master.jsonl，
    用 Rich 的 Panel 组件美化渲染 4 种事件类型（llm_input / tool_call / tool_result / system_action）。

    这是一个纯只读的观察者模式——不修改任何文件，不影响 Agent 的运行。
    通常在另一个终端窗口中运行：一边跑 traceclaw run，一边跑 traceclaw monitor。
    """

    try:
        import entry.monitor as traceclaw_monitor
        traceclaw_monitor.main()
    except ImportError as e:
        console.print(f"[bold red]启动失败：找不到监视器模块！[/bold red]\n[dim]请确保 monitor.py 和 cli.py 在同一目录下。\n报错信息: {e}[/dim]")

# ============================================================
# main() — 包内入口（setup.py 中 console_scripts 指向这里）
# ============================================================
def main():
    """
    Typer 应用的启动入口。

    这个函数被 setup.py 的 console_scripts 注册为 'traceclaw' 命令：
      console_scripts = ["traceclaw = entry.cli:main"]

    当用户在终端输入 traceclaw 时，实际执行的就是这个函数。
    """
    app()

if __name__ == "__main__":
    main()
