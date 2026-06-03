"""
TraceClaw 渐进式技能加载器 (Lazy Skill Loader)
==============================================
实现"两阶段工具调用协议"的底层支撑——技能在启动时只扫描元数据（轻量），
首次被 LLM 调用时才加载完整内容（按需），并用 LRU 缓存常用技能。

核心类：LazySkillLoader
  启动时：_scan_skills()
    → 遍历 SKILLS_DIR 下的文件夹
    → 只读每个 SKILL.md 的前 50 行
    → 提取 YAML frontmatter 中的 name 和 description
    → 创建 StructuredTool 占位符（mode='help'/'run'）

  首次调用（mode='help'）：
    → _load_skill_content() 被触发
    → 读取完整的 SKILL.md
    → LRU 缓存（maxsize=50，基于文件 mtime 自动失效）
    → 返回完整说明书给 LLM

  第二次调用（mode='run'）：
    → LLM 根据说明书组装好 command
    → {baseDir} 占位符被替换为实际的技能目录路径
    → 调用 execute_office_shell.invoke() 在沙盒中执行

缓存策略（四层）：
  1. 元数据缓存：_scan_interval=60s，避免每次请求都扫磁盘
  2. 内容 LRU 缓存：@lru_cache(maxsize=50)，最常用的 50 个技能常驻内存
  3. mtime 检测：文件修改后缓存自动失效（热更新支持）
  4. force_rescan：手动强制刷新所有缓存

面试核心考点：
  - 为什么用懒加载？→ 17 个技能，每个 SKILL.md 可能几千行，全加载浪费内存
  - 为什么 LRU 而不是 LFU？→ 使用频率难以准确统计（短对话中 LFU 退化为 LRU），LRU 实现更简单
  - 两阶段协议解决了什么问题？→ LLM 在信息不足时会"猜"工具参数，mode='help' 先给它全部信息
"""

import os
import re
import time
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from langchain_core.tools import StructuredTool
from functools import lru_cache

from .config import SKILLS_DIR
from .tools.sandbox_tools import execute_office_shell  # mode='run' 时在此执行命令

# ============================================================
# DynamicSkillInput — 两阶段协议的参数模型
# ============================================================
# 这是两阶段协议的核心数据结构。
# 所有通过 LazySkillLoader 创建的工具都共享此 args_schema。
# LLM 第一次调用时必须传入 mode='help'（获取说明书），
# 第二次调用时传入 mode='run' + command（实际执行）。
# ============================================================
class DynamicSkillInput(BaseModel):
    mode: str = Field(
        description="必须是 'help' 或 'run'。第一次使用时强烈建议先传入 'help' 阅读说明书。"
    )
    command: Optional[str] = Field(
        default="",
        description="仅在 mode='run' 时需要。你要执行的完整命令，保留 {baseDir} 占位符。"
    )


class LazySkillLoader:
    """
    渐进式技能加载器 + 缓存机制

    特性：
    1. 启动时只扫描元数据（name, description），不加载完整内容
    2. 首次调用技能时才加载完整内容并缓存
    3. 支持热更新（修改技能文件后自动重新加载）
    4. LRU缓存策略，自动清理不常用的技能
    """

    def __init__(self, cache_size: int = 50):
        """
        初始化技能加载器。

        Args:
            cache_size: LRU 缓存的最大条目数，默认 50。
                        对于 17 个技能来说，50 绰绰有余——意味着所有已加载的技能都会在缓存中命中。
        """
        self._skill_registry: Optional[List[Dict[str, Any]]] = None  # 元数据缓存
        self._cache_size = cache_size
        self._last_scan_time = 0        # 上次扫描的时间戳（Unix 秒）
        self._scan_interval = 60        # 元数据缓存有效期（秒）

    # ============================================================
    # LRU 内容缓存
    # ============================================================
    # @lru_cache 是 Python 标准库提供的 LRU 缓存装饰器。
    # maxsize=50 表示最多缓存 50 个不同技能的完整内容。
    # 当缓存满时，最久未被使用的条目会被自动驱逐。
    #
    # 参数设计：
    #   md_path: 技能文件的绝对路径（作为缓存键）
    #   mtime:   文件的最后修改时间（用于缓存失效——文件更新后自动重新读取）
    #
    # 如果想手动清除缓存，可以调用 _load_skill_content.cache_clear()
    # ============================================================
    @lru_cache(maxsize=50)
    def _load_skill_content(self, md_path: str, mtime: float) -> str:
        """
        加载技能完整内容（带缓存）

        Args:
            md_path: 技能文件路径
            mtime: 文件修改时间（用于缓存失效检测）

        Returns:
            技能的完整 Markdown 内容

        缓存失效原理：
            当文件被修改后，os.path.getmtime() 返回的 mtime 会变化。
            新的 mtime 作为参数传入，LRU 缓存找不到匹配的 (md_path, mtime) 键，
            因此会重新执行函数体，读取最新的文件内容。
        """
        with open(md_path, "r", encoding="utf-8") as f:
            return f.read()

    def _scan_skills(self, force_rescan: bool = False) -> List[Dict[str, Any]]:
        """
        扫描技能目录，只提取元数据（轻量级操作）

        设计思路：
          启动时不加载任何技能内容，只收集"有哪些技能、叫什么名字、做什么用"。
          这些元数据足够创建工具占位符——LLM 看到工具名和描述就能决定要不要调用。
          真正的内容在 LLM 第一次调用 mode='help' 时才加载。

        Args:
            force_rescan: 是否强制重新扫描（忽略缓存）

        Returns:
            技能元数据列表，每个元素包含：
            {
                "folder":      技能文件夹名（如 "skill-creator"）
                "md_path":     技能文件的绝对路径
                "mtime":       文件最后修改时间（unix 时间戳）
                "raw_name":    原始名称（从 SKILL.md frontmatter 提取）
                "name":        安全名称（去除了特殊字符，用作工具名）
                "description": 技能描述
            }
        """
        current_time = time.time()

        # ── 元数据缓存检查 ──
        # 缓存检查：如果最近扫描过且不强制刷新，直接返回缓存
        if (not force_rescan and
            self._skill_registry is not None and
            current_time - self._last_scan_time < self._scan_interval):
            return self._skill_registry

        skills = []

        if not os.path.exists(SKILLS_DIR):
            self._skill_registry = []
            self._last_scan_time = current_time
            return []

        # ── 遍历技能目录 ──
        for item in os.listdir(SKILLS_DIR):
            folder_path = os.path.join(SKILLS_DIR, item)
            # 只处理文件夹（跳过可能存在的临时文件）
            if not os.path.isdir(folder_path):
                continue

            # 优先找 SKILL.md（Claude Code 生态标准），
            # 找不到则回退到 README.md（通用兼容）
            md_path = os.path.join(folder_path, "SKILL.md")
            if not os.path.exists(md_path):
                md_path = os.path.join(folder_path, "README.md")

            if not os.path.exists(md_path):
                continue  # 既没有 SKILL.md 也没有 README.md → 跳过

            try:
                # ── 只提取元数据（前 50 行）──
                metadata = self._extract_metadata(md_path)

                if metadata:
                    skills.append({
                        "folder": item,
                        "md_path": md_path,
                        "mtime": os.path.getmtime(md_path),  # 用于后续缓存失效检测
                        **metadata
                    })
            except Exception as e:
                print(f" [警告] 扫描技能 {item} 失败: {e}")

        # ── 更新缓存 ──
        self._skill_registry = skills
        self._last_scan_time = current_time

        if skills:
            print(f" [OK] 扫描到 {len(skills)} 个技能（懒加载模式）")

        return skills

    def _extract_metadata(self, md_path: str) -> Optional[Dict[str, str]]:
        """
        从技能文件中提取元数据（只读取必要的部分）

        只读取前 50 行——通常 YAML frontmatter 在文件最开头，
        包含了 name: 和 description: 字段。不需要解析完整文件。

        兼容两种格式：
          - YAML frontmatter（用 --- 包裹的元数据块）
          - 裸的 name:/description: 行（简化格式）

        Args:
            md_path: 技能文件路径

        Returns:
            包含 raw_name, name, description 的字典，或 None（提取失败）
        """
        try:
            with open(md_path, "r", encoding="utf-8") as f:
                # 只读取前 50 行（通常元数据在文件开头）
                lines = []
                for i, line in enumerate(f):
                    if i >= 50:
                        break
                    lines.append(line)

                content = "\n".join(lines)

            # ── 正则提取 name ──
            # 匹配 "name: xxx" 行（允许冒号前后有空格）
            name_match = re.search(r"^name:\s*(.+)$", content, re.MULTILINE)
            # ── 正则提取 description ──
            desc_match = re.search(r"^description:\s*(.+)$", content, re.MULTILINE)

            # 如果提取不到 name，用文件夹名作为后备
            raw_name = name_match.group(1).strip() if name_match else os.path.basename(os.path.dirname(md_path))
            # 工具名净化：去掉非法字符，只保留字母数字和 _-
            tool_name = re.sub(r'[^a-zA-Z0-9_-]', '_', raw_name)

            # 提取描述，如果提取不到则生成一个默认描述
            raw_desc = desc_match.group(1).strip() if desc_match else f"提供 {raw_name} 相关功能"
            # 去除可能的引号包裹（如 description: "This is a tool"）
            if (raw_desc.startswith('"') and raw_desc.endswith('"')) or (raw_desc.startswith("'") and raw_desc.endswith("'")):
                raw_desc = raw_desc[1:-1]

            return {
                "raw_name": raw_name,
                "name": tool_name,
                "description": raw_desc
            }
        except Exception as e:
            print(f" [警告] 提取元数据失败 {md_path}: {e}")
            return None

    def _create_lazy_tool(self, skill_info: Dict[str, Any]) -> StructuredTool:
        """
        创建懒加载工具对象

        这是两阶段协议的核心实现。每个技能被包装成一个 StructuredTool，
        其内部 lazy_runner 函数根据 mode 参数决定行为：
          - mode='help': 加载并返回 SKILL.md 的完整内容（说明书）
          - mode='run':  在沙盒中执行 command 中的 Shell 命令

        Args:
            skill_info: 技能元数据（来自 _scan_skills）

        Returns:
            LangChain StructuredTool 对象，可直接绑定到 LLM
        """
        def lazy_runner(mode: str, command: str = "") -> str:
            """
            懒加载执行器：首次调用时才加载完整内容

            mode='help' → 加载 SKILL.md 完整内容，返回给 LLM 阅读
            mode='run'  → 将 command 中的 {baseDir} 替换为实际路径，在沙盒中执行
            """
            if mode == "help":
                # ── 懒加载触发点：这里才真正读取文件 ──
                # _load_skill_content 有 @lru_cache 保护，同一文件不会重复读取
                skill_content = self._load_skill_content(
                    skill_info["md_path"],
                    skill_info["mtime"]
                )

                # 截断到 3000 字符（防止超长说明书撑爆 LLM 上下文）
                return (
                    f"========== 【{skill_info['raw_name']} 完整说明书】 ==========\n"
                    f"{skill_content[:3000]}\n"
                    f"====================================\n"
                    f"提示：请根据以上说明，如果觉得能解决问题，就将 mode 设为 'run'，"
                    f"并将拼装好的执行命令填入 command 重新调用。"
                )
            elif mode == "run":
                if not command:
                    return "错误：在 'run' 模式下，必须提供 command 参数！"

                # ── {baseDir} 占位符替换 ──
                # 将 {baseDir} 替换为 "skills/<folder_name>"，
                # 这样技能的作者可以用 {baseDir}/scripts/xxx.sh 来引用技能目录下的脚本
                actual_cmd = command.replace("{baseDir}", f"skills/{skill_info['folder']}")
                # 委托给沙盒 Shell 工具执行（享受相同的安全防护）
                return execute_office_shell.invoke({"command": actual_cmd})
            else:
                return "错误：mode 参数只能是 'help' 或 'run'。"

        # ── 精简版工具描述 ──
        # 在工具描述中嵌入"请先调用 mode='help'"的提示，
        # 这是 prompt engineering —— 通过工具描述来引导 LLM 的行为。
        mini_description = (
            f"{skill_info['description']}\n\n"
            f"注意：这是一个外部扩展技能。首次使用请务必先传入 `mode='help'` 来阅读完整说明书，"
            f"之后再使用 `mode='run'` 配合 `command` 执行底层脚本。"
        )

        # 创建 LangChain StructuredTool
        # args_schema=DynamicSkillInput 确保 LLM 知道参数结构（mode + command）
        return StructuredTool.from_function(
            func=lazy_runner,
            name=skill_info["name"],
            description=mini_description,
            args_schema=DynamicSkillInput
        )

    # ============================================================
    # 公开 API
    # ============================================================

    def get_all_tools(self, force_rescan: bool = False) -> List[StructuredTool]:
        """
        获取所有工具（懒加载占位符）

        这是主要的公开接口。agent.py 在构建 StateGraph 时调用此方法，
        获取所有已安装技能的工具对象列表。

        Args:
            force_rescan: 是否强制重新扫描技能目录

        Returns:
            工具对象列表（懒加载占位符——内容尚未加载）
        """
        skill_infos = self._scan_skills(force_rescan=force_rescan)

        tools = []
        for skill_info in skill_infos:
            tools.append(self._create_lazy_tool(skill_info))

        return tools

    def get_tool_count(self) -> int:
        """获取技能数量（不触发加载）"""
        return len(self._scan_skills())

    def clear_cache(self):
        """清除所有缓存（内容缓存 + 元数据缓存）"""
        self._load_skill_content.cache_clear()
        self._skill_registry = None
        print(f" [OK] 技能缓存已清除")


# ============================================================
# 全局懒加载器实例（模块级单例）
# ============================================================
_lazy_loader = LazySkillLoader(cache_size=50)


# ============================================================
# 公开 API 函数（供 agent.py 等模块调用）
# ============================================================

def load_dynamic_skills(force_rescan: bool = False) -> List[StructuredTool]:
    """
    加载动态技能（懒加载 + 缓存版本）

    agent.py 的 create_agent_app() 在启动时调用一次此函数。
    后续的 LLM 工具调用会触发懒加载的实际内容读取。

    Args:
        force_rescan: 是否强制重新扫描技能目录（默认 False）

    Returns:
        工具对象列表（懒加载占位符）

    Note:
        - 启动时只扫描元数据，不加载完整内容
        - 首次调用技能时才加载完整内容
        - 支持热更新（修改技能文件后自动重新加载）
        - 使用 LRU 缓存策略
    """
    return _lazy_loader.get_all_tools(force_rescan=force_rescan)


def reload_skills() -> List[StructuredTool]:
    """
    强制重新扫描技能目录并清除缓存。

    用于热更新场景：用户在 skills/ 下新增或修改了技能文件，
    调用此函数可以立即生效，无需重启 TraceClaw。

    Returns:
        更新后的工具列表
    """
    return _lazy_loader.get_all_tools(force_rescan=True)


def get_skill_count() -> int:
    """
    获取当前技能数量（不触发加载）

    Returns:
        技能总数
    """
    return _lazy_loader.get_tool_count()


def clear_skill_cache():
    """清除技能内容缓存"""
    _lazy_loader.clear_cache()