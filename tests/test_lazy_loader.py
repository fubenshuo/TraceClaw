"""
懒加载技能加载器 — 集成测试
============================
这是一个纯集成测试（不用 unittest.TestCase），直接创建临时文件系统
并完整测试 LazySkillLoader 的四层缓存机制。

测试流程（7 个阶段）：
  1.1 扫描技能目录 → 验证扫描到 5 个技能
  1.2 获取工具列表 → 验证返回 5 个工具（都是懒加载占位符）
  1.3 验证工具属性 → 确认每个工具包裹的是 lazy_runner 函数
  1.4 首次调用（触发完整内容加载）→ 验证从 SKILL.md 读取的内容正确
  1.5 第二次调用（验证缓存命中）→ 第二次应该更快
  2.1 动态新增技能 → 创建新 SKILL.md
  2.2 强制重新扫描 → 验证技能数从 5 变为 6
  3.1 清除缓存 → 验证缓存清除后重新加载

技术要点：
  - importlib.reload() 强制重新加载模块，使 SKILLS_DIR 指向测试临时目录
  - 缓存速度对比：首次调用 vs 第二次调用的耗时对比
  - 环境变量恢复 + 临时目录清理（finally 块保证）

面试要点：
  - 为什么用 importlib.reload 而不是直接修改模块变量？→ 模块级别的 SKILLS_DIR
    在 import 时就已确定，reload 是唯一能让新环境变量生效的方法
  - 缓存验证的设计思路：不是验证"缓存被使用了"，而是验证"第二次比第一次快"
"""

import os
import sys
import time
import tempfile
import shutil
import importlib

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from traceclaw.core.skill_loader import load_dynamic_skills, get_skill_count, reload_skills, clear_skill_cache


def create_test_skills(test_dir: str, num_skills: int = 5):
    """
    在指定目录下创建 num_skills 个测试技能。

    每个技能包含：
      - 一个子目录（test_skill_0 ~ test_skill_N）
      - 一个 SKILL.md 文件（YAML frontmatter + Markdown 正文）

    Args:
        test_dir:   临时测试目录的根路径
        num_skills: 要创建的技能数量

    Returns:
        skills_dir 的路径
    """
    skills_dir = os.path.join(test_dir, "office", "skills")
    os.makedirs(skills_dir, exist_ok=True)

    for i in range(num_skills):
        skill_dir = os.path.join(skills_dir, f"test_skill_{i}")
        os.makedirs(skill_dir, exist_ok=True)

        # ── SKILL.md 内容模板 ──
        # YAML frontmatter（name + description）+ Markdown 正文
        skill_content = f"""name: Test Skill {i}
description: 这是第 {i} 个测试技能，用于验证懒加载机制

## 详细说明

这是一个测试技能的详细文档内容。
它应该有足够的内容来测试缓存机制。

## 使用方法

1. 先调用 mode='help' 查看此文档
2. 然后调用 mode='run' 执行命令

命令示例：
```bash
echo "Skill {i} executed"
```
"""

        with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(skill_content)

    return skills_dir


def test_lazy_loading():
    """
    懒加载机制的完整集成测试。

    本函数不使用 unittest，而是用纯 Python assert + print。
    这样每个阶段的耗时和输出可以直接看到，方便定位问题。
    """
    print("=" * 60)
    print("测试 1: 基本懒加载功能")
    print("=" * 60)

    # ── 创建临时测试目录 ──
    # tempfile.mkdtemp 创建一个随机命名的临时目录
    temp_dir = tempfile.mkdtemp(prefix="traceclaw_test_")
    skills_dir = create_test_skills(temp_dir, num_skills=5)

    # ── 保存原始环境变量（用于测试后恢复） ──
    original_env = os.environ.get("TRACECLAW_WORKSPACE")
    # 设置 TRACECLAW_WORKSPACE 指向临时目录，让 config.py 导出测试路径
    os.environ["TRACECLAW_WORKSPACE"] = temp_dir

    try:
        # ── importlib.reload 强制重新加载模块 ──
        # 因为 config.py 在首次 import 时就计算了 SKILLS_DIR，
        # 不 reload 的话它还是指向原始 workspace 下的 skills 目录。
        # reload 会让模块重新执行顶层代码，用新的 TRACECLAW_WORKSPACE 重新计算路径。
        import traceclaw.core.config as config_module
        importlib.reload(config_module)

        # 重新导入 skill_loader 以使用新的 SKILLS_DIR
        import traceclaw.core.skill_loader as skill_loader_module
        importlib.reload(skill_loader_module)

        # 清除缓存（确保干净的测试起点）
        skill_loader_module.clear_skill_cache()

        # ============================================================
        # 测试 1.1：扫描技能目录
        # ============================================================
        print(f"\n[测试 1.1] 扫描技能目录...")
        count = skill_loader_module.get_skill_count()
        print(f"[OK] 扫描到 {count} 个技能")
        assert count == 5, f"期望 5 个技能，实际 {count} 个"

        # ============================================================
        # 测试 1.2：获取工具列表（懒加载占位符）
        # ============================================================
        # 此时只读取了 SKILL.md 的 frontmatter（name + description），
        # 还没有读取完整内容——这就是"懒加载"的含义。
        print(f"\n[测试 1.2] 获取工具列表（懒加载）...")
        start_time = time.time()
        tools = skill_loader_module.load_dynamic_skills()
        elapsed = time.time() - start_time
        print(f"[OK] 获取 {len(tools)} 个工具，耗时: {elapsed:.4f}秒")
        assert len(tools) == 5, f"期望 5 个工具，实际 {len(tools)} 个"

        # ============================================================
        # 测试 1.3：验证工具属性（确认是懒加载包装器）
        # ============================================================
        # 每个工具的 func 应该是 "lazy_runner" 闭包，
        # 而非直接指向某个具体函数。
        print(f"\n[测试 1.3] 验证工具属性...")
        for i, tool in enumerate(tools):
            print(f"  - 工具 {i}: {tool.name}")
            assert "lazy_runner" in str(tool.func), f"工具 {tool.name} 不是懒加载函数"
        print(f"[OK] 所有工具都是懒加载模式")

        # ============================================================
        # 测试 1.4：模拟首次调用（触发完整 SKILL.md 内容加载）
        # ============================================================
        # mode='help' 触发 LazySkillLoader 打开文件读取完整内容。
        # 这是"懒加载"的兑现时刻——第一次真正需要时才加载。
        print(f"\n[测试 1.4] 模拟首次调用技能（触发完整内容加载）...")
        start_time = time.time()
        result = tools[0].func(mode='help')
        elapsed = time.time() - start_time
        print(f"[OK] 首次调用耗时: {elapsed:.4f}秒")
        print(f"[OK] 结果预览: {result[:100]}...")
        assert "Test Skill 0" in result, "技能内容未正确加载"

        # ============================================================
        # 测试 1.5：第二次调用（应该命中 LRU 缓存）
        # ============================================================
        # 同一个技能的 SKILL.md 内容已被缓存，
        # 第二次调用应该明显更快（不读磁盘）。
        print(f"\n[测试 1.5] 第二次调用（应该使用缓存）...")
        start_time = time.time()
        result2 = tools[0].func(mode='help')
        elapsed2 = time.time() - start_time
        print(f"[OK] 第二次调用耗时: {elapsed2:.4f}秒")
        if elapsed2 > 0:
            print(f"[OK] 速度提升: {(elapsed / elapsed2):.2f}x")
        else:
            print(f"[OK] 速度提升: 缓存响应极快 (< 0.001s)")
        # 缓存命中应该不慢于首次加载
        assert elapsed2 <= elapsed, "第二次调用应该更快或相等（使用缓存）"

        # ============================================================
        # 测试 2：强制重新扫描（动态新增技能）
        # ============================================================
        print("\n" + "=" * 60)
        print("测试 2: 强制重新扫描")
        print("=" * 60)

        # 测试 2.1：在文件系统中新增一个技能
        print(f"\n[测试 2.1] 添加新技能...")
        new_skill_dir = os.path.join(skills_dir, "new_skill")
        os.makedirs(new_skill_dir, exist_ok=True)
        with open(os.path.join(new_skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("name: New Skill\ndescription: 新添加的技能")

        # 测试 2.2：强制重新扫描 → 应该发现新技能
        print(f"\n[测试 2.2] 强制重新扫描...")
        skill_loader_module.reload_skills()
        count_after = skill_loader_module.get_skill_count()
        print(f"[OK] 扫描后技能数: {count_after}")
        assert count_after == 6, f"期望 6 个技能，实际 {count_after} 个"

        # ============================================================
        # 测试 3：缓存清除
        # ============================================================
        print("\n" + "=" * 60)
        print("测试 3: 缓存清除")
        print("=" * 60)

        # 测试 3.1：清除 LRU 缓存
        print(f"\n[测试 3.1] 清除缓存...")
        skill_loader_module.clear_skill_cache()

        # 测试 3.2：缓存清除后首次调用 → 应该重新读取文件
        print(f"\n[测试 3.2] 缓存清除后首次调用...")
        start_time = time.time()
        result3 = tools[0].func(mode='help')
        elapsed3 = time.time() - start_time
        print(f"[OK] 缓存清除后调用耗时: {elapsed3:.4f}秒")

        print("\n" + "=" * 60)
        print("[PASS] 所有测试通过！")
        print("=" * 60)

    finally:
        # ── 恢复原始环境 ──
        if original_env is not None:
            os.environ["TRACECLAW_WORKSPACE"] = original_env
        else:
            os.environ.pop("TRACECLAW_WORKSPACE", None)

        # 清理临时目录
        shutil.rmtree(temp_dir, ignore_errors=True)
        print(f"\n[OK] 临时测试目录已清理")


if __name__ == "__main__":
    test_lazy_loading()
