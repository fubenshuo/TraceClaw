"""
TraceClaw 全局配置中心
=====================
本模块是整个框架的"地基"——所有目录路径、文件路径都在这里定义。
模块在被 import 的瞬间就会：
  1. 修复 Windows 中文终端的编码问题（GBK → UTF-8）
  2. 加载 .env 环境变量
  3. 解析出项目根目录和 workspace 目录
  4. 自动创建所有必需的目录（惰性初始化，不会重复创建）
  5. 打印一行确认信息

设计原则：
  - 所有路径相关的常量集中管理，其他模块不硬编码路径
  - 环境变量 TRACECLAW_WORKSPACE 允许用户自定义工作区位置（生产部署入口）
  - 目录在 import 时自动就绪，调用方无需关心"目录是否存在"
"""

import os
import sys
from dotenv import load_dotenv

# ============================================================
# Windows 终端编码修复
# ============================================================
# Windows 中文系统的 CMD/PowerShell 默认使用 GBK (cp936) 编码，
# 而 TraceClaw 大量使用 emoji（🔧🎯💡 等）和 Unicode 字符。
# GBK 无法编码 emoji，会抛出 UnicodeEncodeError 导致程序崩溃。
# 这里的解决方案：检测到编码不是 UTF-8 时，强制重配 stdout 为 UTF-8。
# errors='replace' 确保万一还有无法编码的字符，用 ? 替代而不是炸掉。
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ============================================================
# 加载 .env 环境变量
# ============================================================
# python-dotenv 从项目根目录的 .env 文件中读取配置（API Key、模型名等），
# 注入到 os.environ，后续所有模块都可以通过 os.getenv() 获取。
# 注意：这里是在模块顶层调用，意味着只要 import config，.env 就已经生效。
load_dotenv()

# ============================================================
# 路径解析链：从当前文件逐层向上定位项目根目录
# ============================================================
# __file__                = traceclaw/core/config.py
# os.path.dirname(...)    = traceclaw/core/          → CORE_DIR
# 再往上一层              = traceclaw/               → PACKAGE_DIR（Python 包根）
# 再往上一层              = TraceClaw-main/          → PROJECT_ROOT（项目根）
# ============================================================
CORE_DIR = os.path.dirname(os.path.abspath(__file__))       # traceclaw/core/
PACKAGE_DIR = os.path.dirname(CORE_DIR)                      # traceclaw/（Python 包根目录）
PROJECT_ROOT = os.path.dirname(PACKAGE_DIR)                  # 项目根目录（setup.py 所在位置）

# ============================================================
# WORKSPACE_DIR — 运行时数据的大本营
# ============================================================
# 所有 Agent 运行时产生的数据（数据库、记忆、技能、任务）都存放在此目录下。
# 优先级：环境变量 TRACECLAW_WORKSPACE > 默认的 <项目根>/workspace
# 用户可以通过设置 TRACECLAW_WORKSPACE 把工作区放到任意位置（如外置硬盘、网盘同步目录）。
# ============================================================
WORKSPACE_DIR = os.getenv("TRACECLAW_WORKSPACE", os.path.join(PROJECT_ROOT, "workspace"))

# ============================================================
# 工作区子目录路径定义
# ============================================================

# DB_PATH — LangGraph 的 SQLite 状态持久化文件
# 存储完整的对话历史（messages）和上下文摘要（summary）。
# 使用 WAL 模式 + busy_timeout 支持并发读写（详见 entry/main.py 中的 URI 拼接）。
# 面试类比：这是 Agent 的"海马体"——短期记忆，断电不丢。
DB_PATH = os.path.join(WORKSPACE_DIR, "state.sqlite3")

# MEMORY_DIR — 用户长期画像存储目录
# 目前只存放一个 user_profile.md，由 save_user_profile 工具写入。
# 画像内容会被直接注入到系统 Prompt 中，影响 Agent 的行为风格。
# 面试类比：这是 Agent 的"长期记忆"——关于用户的持久化知识。
MEMORY_DIR = os.path.join(WORKSPACE_DIR, "memory")

# PERSONAS_DIR — 人设/角色预设目录
# 可以放置不同的系统 Prompt 模板，让 Agent 切换不同的人设风格。
# 当前版本暂未使用，属于预留扩展点。
PERSONAS_DIR = os.path.join(WORKSPACE_DIR, "personas")

# SCRIPTS_DIR — 自动化脚本目录
# 可以放置用户自定义的 Python/Bash 脚本，供 Agent 调度执行。
# 当前版本暂未使用，属于预留扩展点。
SCRIPTS_DIR = os.path.join(WORKSPACE_DIR, "scripts")

# OFFICE_DIR — 沙盒工位（安全边界）
# 这是 Agent 唯一被允许进行文件读写和 Shell 执行的目录。
# 所有 sandbox_tools 的操作都在此目录内完成。
# _get_safe_path() 函数会将任何试图跳出此目录的路径请求拦截。
# 面试类比：Agent 的"工位"——出了这个门就不归你管了。
OFFICE_DIR = os.path.join(WORKSPACE_DIR, "office")

# SKILLS_DIR — 可插拔技能卡槽
# 位于 office 目录之下（skills 本质上是 office 内的脚本集）。
# 每个子文件夹代表一个技能，其中必须包含 SKILL.md 或 README.md。
# LazySkillLoader 扫描此目录来发现可用技能。
# 面试类比：Agent 的"技能树"——可以随时安装新的技能包。
SKILLS_DIR = os.path.join(OFFICE_DIR, "skills")

# TASKS_FILE — 定时任务持久化队列
# 以 JSON 数组形式存储用户通过 schedule_task 工具创建的定时任务。
# heartbeat.py 的 pacemaker_loop 每 10 秒轮询此文件，到期自动触发。
# 读写由 threading.Lock (tasks_lock) 保护并发安全。
TASKS_FILE = os.path.join(WORKSPACE_DIR, "tasks.json")

# ============================================================
# 惰性目录初始化
# ============================================================
# 模块被 import 时自动创建所有必需的目录。
# os.makedirs 配合 exist_ok=True 保证幂等性——目录已存在时不会报错，
# 也不会覆盖已有内容。这意味着"第一次运行"和"第 N 次运行"行为一致。
# ============================================================
for d in [WORKSPACE_DIR, MEMORY_DIR, PERSONAS_DIR, SCRIPTS_DIR, OFFICE_DIR, SKILLS_DIR]:
    os.makedirs(d, exist_ok=True)

# ============================================================
# 启动确认信息
# ============================================================
# 这行 print 在每次 import config 时都会执行，作为工作区就绪的确认信号。
# 在 TraceClaw 启动日志中，这是最早出现的几行输出之一。
# ============================================================
print(f"🔧 [Config] Workspace 路径已就绪: {WORKSPACE_DIR}")