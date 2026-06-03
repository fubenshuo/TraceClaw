"""
TraceClaw 内置工具集
====================
12 个随框架启动即加载的基础工具，涵盖系统查询、记忆管理、沙盒操作、任务调度四大类。

工具分类：
  系统查询类：
    get_current_time()       — 获取当前时间
    get_system_model_info()  — 查询当前使用的 LLM 型号和提供商
    calculator()             — 数学表达式计算（使用受限 eval）

  记忆管理类：
    save_user_profile()      — 更新用户长期画像（覆盖式写入 user_profile.md）

  沙盒操作类（从 sandbox_tools.py 导入）：
    list_office_files()      — 浏览 office 目录
    read_office_file()       — 读取 office 内文件
    write_office_file()      — 写入/追加 office 内文件
    execute_office_shell()   — 在 office 内执行 Shell 命令

  任务调度类（CRUD on tasks.json）：
    schedule_task()          — 创建定时任务
    list_scheduled_tasks()   — 查看所有定时任务
    delete_scheduled_task()  — 删除定时任务（含防批量误删协议）
    modify_scheduled_task()  — 修改定时任务（含安全确认协议）

并发安全：
  - tasks_lock (threading.Lock): 与 heartbeat.py 共享，
    保证对 tasks.json 的读写操作在心跳扫描和用户操作之间串行化

面试核心考点：
  - calculator 为什么用 eval 不安全？→ AST 字面量解析才是安全做法，代码中已标注 TODO
  - 任务调度的安全协议（AM/PM 确认、批量删除确认）→ prompt engineering 的工程化实践
  - BUILTIN_TOOLS 列表收集所有工具 → 单一出口，方便管理和测试
"""

from datetime import datetime
from .base import traceclaw_tool, TraceClawBaseTool
import os
import json
import uuid
import threading
from ..config import MEMORY_DIR, TASKS_FILE
from .sandbox_tools import (
    list_office_files,
    read_office_file,
    write_office_file,
    execute_office_shell
)

# ============================================================
# 全局并发锁
# ============================================================
# 与 heartbeat.py 的 pacemaker_loop 共享同一把锁。
# 当用户在对话中增删改任务时，心跳可能恰好在同一时刻扫描 tasks.json。
# 锁保证以下操作串行化：
#   - builtins: schedule_task / delete_scheduled_task / modify_scheduled_task
#   - heartbeat: pacemaker_loop 的"读取→修改→写回"事务
# 注意：这是 threading.Lock 而非 asyncio.Lock，
# 因为 subprocess 和文件 I/O 可能在底层阻塞 event loop，
# 使用线程锁可以兼容同步/异步混合场景。
# ============================================================
tasks_lock = threading.Lock()

# user_profile.md 的绝对路径
PROFILE_PATH = os.path.join(MEMORY_DIR, "user_profile.md")


# ============================================================
# 工具 1：get_system_model_info — 模型身份查询
# ============================================================
@traceclaw_tool
def get_system_model_info() -> str:
    """
    获取当前 TraceClaw 正在运行的底层大模型（LLM）型号和提供商信息。
    当用户询问"你是基于什么模型"、"你的底层大模型是什么"、"你是GPT还是GLM"、"现在用的什么模型"等身份问题时，调用此工具。

    从环境变量 DEFAULT_PROVIDER 和 DEFAULT_MODEL 读取。
    这两个值在 .env 文件中配置，在 entry/main.py 中通过 load_dotenv() 加载。
    """
    provider = os.getenv("DEFAULT_PROVIDER", "unknown")
    model = os.getenv("DEFAULT_MODEL", "unknown")

    if provider == "unknown" or model == "unknown":
        return "无法获取当前的系统模型配置，可能是环境变量未正确加载。"

    return f"当前使用的模型提供商(Provider)是: {provider}，具体型号(Model)是: {model}。"


# ============================================================
# 工具 2：save_user_profile — 长期记忆更新
# ============================================================
@traceclaw_tool
def save_user_profile(new_content: str) -> str:
    """
    更新用户的全局显性记忆档案。

    设计意图：
      系统 Prompt 中包含了"【记忆进化】"指令，引导 LLM 在发现用户偏好时
      主动调用此工具。这实现了"Agent 自动学习用户偏好"的闭环——
      不需要用户手动编辑 user_profile.md。

    操作模式：完全覆盖（而非追加）
      这是因为用户偏好可能发生变化（如"不再喜欢 Python"→"现在喜欢 Rust"），
      追加模式会导致矛盾信息累积。

    协议要求（写在工具 description 中，LLM 会读到）：
      1. 先调用 read_user_profile（实则直接读文件）获取当前档案
      2. 将新信息融入档案，删去冲突/过时的旧信息
      3. 将修改后的一整篇完整 Markdown 传入 new_content

    Args:
        new_content: 完整的、更新后的用户画像 Markdown 文本
    """
    os.makedirs(MEMORY_DIR, exist_ok=True)
    with open(PROFILE_PATH, "w", encoding="utf-8") as f:
        f.write(new_content)

    return "记忆档案已成功覆写更新。新的人设画像已生效。"


# ============================================================
# 工具 3：get_current_time — 当前时间查询
# ============================================================
@traceclaw_tool
def get_current_time() -> str:
    """
    获取当前的系统时间和日期。
    当用户询问"现在几点"、"今天星期几"、"今天几号"等与当前时间相关的问题时，调用此工具。

    返回的是宿主机的本地系统时间，不是 UTC。
    对于定时任务创建（schedule_task），此工具的输出是时间推算的基准。
    """
    now = datetime.now()
    return f"当前本地系统时间是: {now.strftime('%Y-%m-%d %H:%M:%S')}"


# ============================================================
# 工具 4：calculator — 数学计算器
# ============================================================
@traceclaw_tool
def calculator(expression: str) -> str:
    """
    一个简单的数学计算器。
    用于计算基础的数学表达式，例如: '3 * 5' 或 '100 / 4'。
    注意：参数 expression 必须是一个合法的 Python 数学表达式字符串。

    安全警告（开发者自注）：
      eval(expression, {"__builtins__": {}}, {}) 中的 {"__builtins__": {}}
      禁用了 Python 内置函数（如 __import__、open），一定程度上限制了注入攻击。
      但这个方案对恶意输入仍有风险（如通过字符串属性访问绕过限制）。
      生产环境中应替换为 AST 字面量解析器（ast.literal_eval）或 numexpr 库。
    """
    try:
        # 警告: eval 在真实的生产环境中存在注入风险！
        # 这里仅为了搭建核心层做快速 Demo。未来在生产级扩展中，
        # 应该替换为基于 AST 的安全解析器，或者更专业的数学库（如 numexpr）。
        result = eval(expression, {"__builtins__": {}}, {})
        return f"表达式 '{expression}' 的计算结果是: {result}"
    except Exception as e:
        return f"计算出错，请检查表达式格式。错误信息: {str(e)}"


# ============================================================
# 工具 5：schedule_task — 创建定时任务
# ============================================================
@traceclaw_tool
def schedule_task(target_time: str, description: str, repeat: str = None, repeat_count: int = None) -> str:
    """
    为一个未来的任务设定闹钟或提醒。

    核心功能：
      - 单次任务：到点触发一次，然后从队列中移除
      - 有限循环：repeat + repeat_count（如"每天提醒，共 3 次"）
      - 无限循环：repeat 但 repeat_count=None（如"每天提醒，直到取消"）

    参数 target_time 必须是严格的格式："YYYY-MM-DD HH:MM:SS"
    （请先调用 get_current_time 获取当前时间，并在其基础上推算）。

    【高级循环功能】：
    - repeat (可选): 设置重复频率。可选值为 "hourly", "daily", "weekly"。如果不重复请留空。
    - repeat_count (可选): 结合 repeat 使用，表示一共需要触发几次。

    【案例教学】：
    1. 用户说："以后每天8点提醒我喝牛奶" -> repeat="daily", repeat_count=None (无限循环)
    2. 用户说："接下来的3天，每天提醒我吃药" -> repeat="daily", repeat_count=3 (有限循环)
    3. 用户说："明早8点叫我起床" -> repeat=None, repeat_count=None (单次任务)

    【时间歧义严格确认协议 (AM/PM Ambiguity CRITICAL)】：
    当用户说出的时间存在 12 小时制的模糊性时（例如：只说了"7点"，没明确说早上还是晚上）：
    1. 你必须向用户提问确认是上午还是下午。
    2. 【死命令】：在用户明确回复"上午"或"下午"（或改为24小时制）之前，本工具处于【绝对锁定状态】！
    3. 就算用户发省略号（如"。。"）、发脾气、或者说无关内容，你也【绝对禁止】为了讨好用户而自行猜测时间！
    4. 严禁出现"抱歉多问了"、"默认早上"这种妥协行为。
    5. 如果用户不明确回答，你必须坚定地回复："抱歉，没有明确上下午，我无权为您设置闹钟。请明确告知时间段。"并立即中止工具调用。

    Args:
        target_time:  触发时间，格式 "YYYY-MM-DD HH:MM:SS"
        description:  任务描述（触发时显示给用户看的内容）
        repeat:       循环频率（"hourly"/"daily"/"weekly"/"monthly"），可选
        repeat_count: 循环次数（None=无限循环），可选
    """
    try:
        target_dt = datetime.strptime(target_time, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return "设定失败：时间格式错误，必须严格遵循 'YYYY-MM-DD HH:MM:SS' 格式。"

    now = datetime.now()
    if target_dt <= now:
        return (
            "设定失败：target_time 必须晚于当前时间。"
            f" 当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}，"
            f" 你传入的是：{target_time}"
        )

    # ── 加锁：保证与 heartbeat 的互斥 ──
    with tasks_lock:
        tasks = []
        if os.path.exists(TASKS_FILE):
            try:
                with open(TASKS_FILE, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        tasks = json.loads(content)
            except Exception as e:
                return f"设定失败：读取任务队列异常 {str(e)}"

        # 生成新任务（id 取 UUID 前 8 位，足够唯一且简洁）
        new_task = {
            "id": str(uuid.uuid4())[:8],
            "target_time": target_time,
            "description": description,
            "repeat": repeat,
            "repeat_count": repeat_count
        }
        tasks.append(new_task)

        try:
            with open(TASKS_FILE, "w", encoding="utf-8") as f:
                json.dump(tasks, f, ensure_ascii=False, indent=2)
        except Exception as e:
            return f"设定失败：写入任务队列异常 {str(e)}"

    msg = f" 任务已成功加入队列。首发时间：{target_time} | 任务：{description}"
    if repeat:
        msg += f" | 循环模式：{repeat} (共 {repeat_count if repeat_count else '无限'} 次)"
    return msg


# ============================================================
# 工具 6：list_scheduled_tasks — 查看定时任务
# ============================================================
@traceclaw_tool
def list_scheduled_tasks() -> str:
    """
    查看当前所有待处理的定时任务列表。
    当用户询问"我都有哪些任务"、"查一下闹钟"、"刚才定了什么"时调用此工具。

    返回的任务列表按 target_time 升序排列（最近触发的排在最前面）。
    """
    with tasks_lock:
        if not os.path.exists(TASKS_FILE):
            return "当前没有任何定时任务。"

        try:
            with open(TASKS_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return "任务列表为空。"
                tasks = json.loads(content)

            if not tasks:
                return "当前没有任何定时任务。"

            # 按触发时间升序排列
            tasks.sort(key=lambda x: x['target_time'])

            res = " 当前待执行任务列表：\n"
            for t in tasks:
                res += f"- [ID: {t['id']}] 时间: {t['target_time']} | 任务: {t['description']}\n"
            return res
        except Exception as e:
            return f"查询失败：{str(e)}"


# ============================================================
# 工具 7：delete_scheduled_task — 删除定时任务
# ============================================================
@traceclaw_tool
def delete_scheduled_task(task_id: str) -> str:
    """
    根据任务 ID 取消或删除一个定时任务。

    【强制性风险控制协议 (CRITICAL)】：
    删除操作具有不可逆性。
    1. 只要匹配到符合描述的任务数量 > 1。
    2. 无论用户语气多么确定，只要他没提供具体的任务 ID。

    【你必须执行的动作】：
    【禁止】在单次回复中针对同一个模糊描述发起多个删除工具调用。
    你必须先列出所有匹配的任务（1. 2. 3.），并询问用户：
    "发现了多个符合条件的提醒（列出列表），为了安全起见，请问是要全部删除，还是只删除其中几个？"
    必须要用户明确给出编号或者说确定全部删除，才能调用此工具！！
    严禁自作主张执行批量删除。

    Args:
        task_id: 要删除的任务的唯一 ID（从 list_scheduled_tasks 的返回结果中获取）
    """

    with tasks_lock:
        if not os.path.exists(TASKS_FILE):
            return "删除失败：任务列表文件不存在。"

        try:
            with open(TASKS_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
                tasks = json.loads(content) if content else []

            # 过滤掉匹配 ID 的任务（列表推导式，不修改原列表）
            new_tasks = [t for t in tasks if t['id'] != task_id]

            if len(new_tasks) == len(tasks):
                return f"删除失败：未找到 ID 为 {task_id} 的任务。"

            with open(TASKS_FILE, "w", encoding="utf-8") as f:
                json.dump(new_tasks, f, ensure_ascii=False, indent=2)

            return f" 任务 [ID: {task_id}] 已成功取消。"
        except Exception as e:
            return f"操作异常：{str(e)}"


# ============================================================
# 工具 8：modify_scheduled_task — 修改定时任务
# ============================================================
@traceclaw_tool
def modify_scheduled_task(task_id: str, new_time: str = None, new_description: str = None) -> str:
    """
    修改现有定时任务的时间或内容。

    【强制性风险控制协议 (CRITICAL)】：
    1. 只要用户通过"模糊描述"（如：那个5天的任务、洗澡的任务）来要求修改，而没有直接提供 ID。
    2. 无论用户的话语看起来是单数还是复数（如："把5天的任务全改了"）。
    3. 只要系统中匹配到的任务数量 > 1。

    【你必须执行的动作】：
    禁止直接调用本工具！你必须向用户展示匹配到的所有任务列表，并强制询问：
    "我发现有 [N] 个任务符合描述（列出列表），请问你是要【全部修改】，还是修改其中【某几个】？（请告诉我编号或确认全部）"

    必须在用户回复"全部"或者指定了具体编号后，你才能继续操作！修改任务并非小事,这是为了安全！！

    Args:
        task_id:         要修改的任务 ID
        new_time:        新的触发时间（可选，不传则保持原时间）
        new_description: 新的任务描述（可选，不传则保持原描述）
    """

    with tasks_lock:
        if not os.path.exists(TASKS_FILE):
            return "修改失败：任务列表为空。"

        try:
            with open(TASKS_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
                tasks = json.loads(content) if content else []

            found = False
            for t in tasks:
                if t['id'] == task_id:
                    # ── 修改时间 ──
                    if new_time:
                        parsed_new_time = datetime.strptime(new_time, "%Y-%m-%d %H:%M:%S")
                        now = datetime.now()
                        if parsed_new_time <= now:
                            return (
                                "修改失败：new_time 必须晚于当前时间。"
                                f" 当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}，"
                                f" 你传入的是：{new_time}"
                            )
                        t['target_time'] = new_time
                    # ── 修改描述 ──
                    if new_description:
                        t['description'] = new_description
                    found = True
                    break

            if not found:
                return f"修改失败：未找到 ID 为 {task_id} 的任务。"

            with open(TASKS_FILE, "w", encoding="utf-8") as f:
                json.dump(tasks, f, ensure_ascii=False, indent=2)

            return f" 任务 [ID: {task_id}] 已成功更新。"
        except ValueError:
            return "修改失败：时间格式错误。"
        except Exception as e:
            return f"操作异常：{str(e)}"


# ============================================================
# BUILTIN_TOOLS — 内置工具注册表（单一出口）
# ============================================================
# 所有内置工具在这里集中注册。agent.py 的 create_agent_app()
# 会遍历此列表 + load_dynamic_skills() 的结果，合并后绑定到 LLM。
# 新增工具只需在此列表追加一行即可。
# ============================================================
BUILTIN_TOOLS = [
    get_current_time,
    calculator,
    save_user_profile,
    list_office_files,
    read_office_file,
    write_office_file,
    execute_office_shell,
    get_system_model_info,
    schedule_task,
    list_scheduled_tasks,
    delete_scheduled_task,
    modify_scheduled_task
]