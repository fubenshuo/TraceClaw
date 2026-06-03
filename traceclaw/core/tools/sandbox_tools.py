r"""
TraceClaw 沙盒工具集
====================
Agent 与本地文件系统和 Shell 交互的唯一通道——所有操作被严格限制在 OFFICE_DIR 内。

四个工具：
  list_office_files()     — 浏览 office 目录中的文件和文件夹
  read_office_file()      — 读取 office 内文件的内容（超过 10000 字符自动截断）
  write_office_file()     — 写入/追加文件（支持 w 和 a 两种模式）
  execute_office_shell()  — 在 office 目录下执行 Shell 命令（5 层正则安全防御）

核心安全函数：_get_safe_path()
  - 将所有用户传入的相对路径解析为绝对路径
  - 强制检查解析后的路径是否以 OFFICE_DIR 开头
  - 如果不是 → PermissionError（拦截路径穿越攻击）

五层正则防御（execute_office_shell）：
  杀招 1: \.\.                  → 拦截所有 ../ 相对路径穿越
  杀招 2: (^|\s|[<>|&;])/       → 拦截 Unix 绝对路径（含重定向注入如 cat</etc/passwd）
  杀招 3: (^|\s|[<>|&;])~       → 拦截 Unix 用户主目录引用
  杀招 4: (^|\s|[<>|&;])\       → 拦截 Windows 根目录引用
  杀招 5: (?i)(^|\s|[<>|&;])[a-z]: → 拦截 Windows 盘符（D:, type C:\...）

面试核心考点：
  - _get_safe_path 为什么用 os.path.abspath 而不是 os.path.realpath？
    → 用 realpath 会解析符号链接，反而可能暴露攻击面
    → 用 abspath + startswith 白名单是更保守但更安全的策略
  - 正则防御的局限性？
    → 不防 base64 编码绕过、curl | bash 网络下载、Python/Node 单行执行器
    → 已在系统 Prompt 中补充禁止解释器单行命令的规则（纵深防御）
"""

import os
import subprocess
from .base import traceclaw_tool
from ..config import OFFICE_DIR
import re
import platform

# ── 操作系统检测 ──
# 在 execute_office_shell 的返回信息中告知 Agent 当前系统类型，
# 帮助 LLM 在 Windows 上用 dir/del，在 Linux/Mac 上用 ls/rm
SYS_OS = platform.system()

def _get_safe_path(relative_path: str) -> str:
    r"""
    沙盒路径防火墙 — 将用户传入的相对路径转换为绝对路径，
    并强制验证转换后的路径没有逃逸出 OFFICE_DIR。

    防御原理（白名单模式）：
      1. OFFICE_DIR 的绝对路径是"安全区域"的边界
      2. 用户传入的相对路径与 OFFICE_DIR 拼接后，abs 化得到目标绝对路径
      3. 检查目标路径是否以 OFFICE_DIR 开头
      4. 如果不是 → 说明用户试图通过 ../ 等技巧跳出去 → 拦截

    攻击示例（都会被拦截）：
      "../../etc/passwd"   → target_path = "E:\...\etc\passwd"
                           → 不以 OFFICE_DIR 开头 → PermissionError
      "skills/../../../"   → 同理被拦截

    Args:
        relative_path: 用户（LLM）传入的相对路径字符串

    Returns:
        安全的绝对路径（一定在 OFFICE_DIR 内部）

    Raises:
        PermissionError: 检测到路径越界
    """
    # 将 OFFICE_DIR 转化为标准绝对路径
    base_dir = os.path.abspath(OFFICE_DIR)
    # 将目标路径转化为绝对路径
    target_path = os.path.abspath(os.path.join(base_dir, relative_path))

    # 核心防御：目标路径必须以 OFFICE_DIR 开头！
    # 如果 relative_path 是 "../../etc/passwd"，
    # abspath 会将其解析为真正的绝对路径（如 /etc/passwd），
    # 这时 startswith(base_dir) 必然返回 False，触发拦截。
    if not target_path.startswith(base_dir):
        raise PermissionError(f"越权拦截：你试图访问沙盒外的路径 '{relative_path}'！你只能在 office 工位内活动。")

    return target_path

# ============================================================
# 工具 1：list_office_files — 浏览工位目录
# ============================================================
@traceclaw_tool
def list_office_files(sub_dir: str = "") -> str:
    """
    查看你的 office 工位里有哪些文件和文件夹。
    如果 sub_dir 为空，则查看工位根目录。

    返回内容带图标标注：
      📁 表示文件夹
      📄 表示文件

    Args:
        sub_dir: 要查看的子目录（相对于 office），留空则查看根目录
    """
    try:
        target_dir = _get_safe_path(sub_dir)
        if not os.path.exists(target_dir):
            return f"目录不存在：{sub_dir}"

        items = os.listdir(target_dir)
        if not items:
            return f"[{sub_dir if sub_dir else 'office 根目录'}] 是空的。"

        # 格式化输出，标注是文件还是文件夹
        result = []
        for item in items:
            item_path = os.path.join(target_dir, item)
            item_type = "📁" if os.path.isdir(item_path) else "📄"
            result.append(f"{item_type} {item}")

        return "\n".join(result)
    except Exception as e:
        return str(e)

# ============================================================
# 工具 2：read_office_file — 读取工位内文件
# ============================================================
@traceclaw_tool
def read_office_file(filepath: str) -> str:
    """
    读取 office 工位里指定文件的内容。
    filepath 参数应该是相对于 office 的路径，例如 "test.py" 或 "skills/my_skill.py"。

    安全截断：如果文件内容超过 10000 字符，只返回前 10000 字符。
    这是为了防止 LLM 读取超大日志文件时耗尽 token 预算。

    Args:
        filepath: 相对于 office 目录的文件路径
    """
    try:
        target_path = _get_safe_path(filepath)
        if not os.path.exists(target_path):
            return f"文件不存在：{filepath}"

        with open(target_path, "r", encoding="utf-8") as f:
            content = f.read()
            # 防爆截断：防止读取几个 G 的日志把 Token 撑爆
            if len(content) > 10000:
                return content[:10000] + "\n\n...[内容过长，已被安全截断]..."
            return content
    except Exception as e:
        return str(e)

# ============================================================
# 工具 3：write_office_file — 写入工位内文件
# ============================================================
@traceclaw_tool
def write_office_file(filepath: str, content: str, mode: str = "w") -> str:
    """
    在 office 工位里操作文件内容。

    参数说明:
    - filepath: 相对路径，例如 "spider.py" 或 "docs/readme.md"。
    - content: 要写入的具体文本或代码内容。
    - mode: 写入模式。
        - "w" (默认): 【覆盖/新建】模式。如果文件已存在，将彻底清空原内容并写入新内容！
        - "a": 【追加】模式。保留原内容，将新内容追加到文件最末尾（常用于写日志或在文件末尾新增函数）。

    ⚠️ 智能体操作规范：
    1. 如果你要修改一个长文件中间的某几行，目前最安全的做法是：读取原文件，在你的内存中完成替换，然后用 "w" 模式把【完整的最新代码】重写进去。
    2. 如果你需要重命名文件或删除文件，请直接使用 execute_office_shell 工具执行 `mv` 或 `rm` 命令。
    3. 禁止编写 与 跳出office工位 相关的任何语言脚本！

    Args:
        filepath: 相对于 office 的文件路径
        content:  要写入的完整内容
        mode:     "w" (覆盖) 或 "a" (追加)
    """
    try:
        target_path = _get_safe_path(filepath)

        # 严格校验传入的 mode（防止 LLM 传入 "x" / "r+" 等非预期模式）
        if mode not in ["w", "a"]:
             return "❌ 错误：mode 参数必须是 'w' (覆盖) 或 'a' (追加)。"

        # 如果模型想在子目录里写文件，确保子目录存在
        os.makedirs(os.path.dirname(target_path), exist_ok=True)

        with open(target_path, mode, encoding="utf-8") as f:
            # 如果是追加模式，且内容不是以换行符开头，自动补一个换行，防止代码粘连
            # 例如上一行是 "def foo():"，新内容是 "def bar():"，
            # 不加换行的话会变成 "def foo():def bar():" → 语法错误
            if mode == "a" and not content.startswith("\n"):
                f.write("\n" + content)
            else:
                f.write(content)

        action = "覆盖/新建" if mode == "w" else "追加"
        return f" ● 成功以 {action} 模式写入文件：{filepath} (共 {len(content)} 字符)"
    except Exception as e:
        return str(e)


# ============================================================
# 工具 4：execute_office_shell — 在工位内执行 Shell 命令
# ============================================================
@traceclaw_tool
def execute_office_shell(command: str) -> str:
    """
    在 office 工位中执行 Shell 命令。

    ⚠️ 【极其重要的环境限制】：
    1. 💻 跨平台注意：当前宿主机可能是 Windows、Linux 或 Mac。请根据你得到的环境反馈，使用对应的原生 Shell 命令（例如 Win 用 dir/del，Linux 用 ls/rm）。如果命令报错，请自行调整重试！
    2. 这是一个非交互式终端！所有命令必须携带免确认参数（如 -y, --quiet）。
    3. 禁止使用 cd 命令跳出当前目录，你的活动范围仅限 office。
    4. [无状态警告] 每次执行都是独立的终端进程！需要进入子目录请使用"命令链"或相对路径。
    5. 禁止一切形式跳出office工位!!! 例如运行跳出或查看office路径的任何脚本以及其他高危操作。

    安全机制：
      - 执行前，命令字符串需通过 5 层正则表达式的安全扫描
      - 执行时，cwd 强制设为 OFFICE_DIR（即使命令中有 cd，也不会生效）
      - 60 秒超时熔断，防止阻塞型命令（如 ping、sleep 9999）卡死
      - stdout/stderr 各截断到 2000 字符，防止输出撑爆 token

    Args:
        command: 要执行的 Shell 命令字符串
    """
    try:
        # ── 五层正则安全扫描 ──
        # 在命令传给 subprocess 之前，先用正则检查是否包含危险路径模式。
        # 这五条正则覆盖了常见的路径穿越和系统文件访问向量。
        dangerous_patterns = [
            r"\.\.",                        # 杀招1：拦截所有相对路径越权 (如 ../)
            r"(?:^|\s|[<>|&;])/",           # 杀招2：Unix 拦截绝对路径 (连 cat </etc/passwd 这种黑客写法也防了)
            r"(?:^|\s|[<>|&;])~",           # 杀招3：Unix 拦截用户主目录 (防 ~/.ssh/)
            r"(?:^|\s|[<>|&;])\\",          # 杀招4：Win 拦截根目录 (防 dir \)
            r"(?i)(?:^|\s|[<>|&;])[a-z]:",  # 杀招5：Win 拦截直接跳盘符及绝对路径 (防 D:, type C:\...)
        ]
        for pattern in dangerous_patterns:
            if re.search(pattern, command):
                return f"❌ 权限拒绝：检测到危险的目录跳转指令。你被禁止离开 office 工位！"

        # ── 执行 Shell 命令 ──
        # shell=True:      允许管道、重定向等 Shell 语法
        # cwd=OFFICE_DIR:  工作目录强制设为 office，无论命令中写了什么 cd
        # timeout=60:      60 秒超时，防止死循环或阻塞命令
        # capture_output:  同时捕获 stdout 和 stderr
        result = subprocess.run(
            command,
            shell=True,
            cwd=OFFICE_DIR,          # 强制锁定工作目录
            capture_output=True,      # 捕获标准输出和标准错误
            encoding='utf-8',
            errors='replace',         # 无法解码的字符用 ? 替代，不抛异常
            timeout=60               # 60 秒超时熔断
        )

        # ── 构造返回信息 ──
        output = f" ● 当前系统: {SYS_OS}\n"
        output += f" ● 执行命令: `{command}`\n"
        output += f" ● 退出码 (Exit Code): {result.returncode}\n"

        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        # 检测交互式命令失败（如 pip install 需要确认）
        if result.returncode != 0 and ("prompt" in stderr.lower() or "y/n" in stdout.lower()):
            output += "\n💡 系统提示：命令可能由于交互式等待而失败。请重试并添加 -y 参数！"

        # 截断防止 token 爆炸
        if stdout:
            output += f"\n[STDOUT]\n{stdout[-2000:] if len(stdout) > 2000 else stdout}"
        if stderr:
            output += f"\n[STDERR]\n{stderr[-2000:] if len(stderr) > 2000 else stderr}"

        # 静默成功的命令（如 rm 成功时没有输出）
        if not stdout and not stderr:
            if result.returncode == 0:
                output += "\n(静默执行完毕：无终端输出)"
            else:
                output += "\n(异常退出：Exit Code 非 0，无错误日志输出)"

        return output

    except subprocess.TimeoutExpired:
        # 60 秒超时 — 可能是 LLM 误发了阻塞命令（如不带 -c 的 ping）
        return "❌ 严重错误：命令执行超时（60s）被熔断！请检查是否有阻塞式交互。"
    except Exception as e:
        return f"❌ 执行异常：{str(e)}"