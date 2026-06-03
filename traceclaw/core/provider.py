"""
TraceClaw LLM 提供商适配层
==========================
使用"简单工厂模式"将不同的 LLM 提供商统一封装为 LangChain BaseChatModel 接口。

支持的提供商：
  - OpenAI 兼容协议：openai / aliyun(百炼) / dashscope / z.ai(智谱) / tencent(混元) / DeepSeek
  - Anthropic 官方协议：anthropic
  - 本地模型：ollama

核心设计：
  1. Base URL 三级 fallback 链：
     显式传入 base_url → 环境变量 OPENAI_API_BASE → 厂商默认地址 (COMPATIBLE_BASE_URLS)

  2. 国产厂商兼容：
     阿里百炼、智谱、腾讯混元都提供了 OpenAI 兼容的 API 端点，
     只需一行 provider_name='aliyun' 即可切换——不需要改任何调用代码。

  3. 返回类型统一：
     所有分支都返回 BaseChatModel，上层 agent.py 不关心底层是谁。

面试要点：
  - 为什么封装这一层？→ 解耦：切换模型只需改 .env 中的一行，不碰业务逻辑
  - 为什么用 ChatOpenAI 承载国产模型？→ 它们都实现了 OpenAI 兼容的 /v1/chat/completions 端点
  - 工厂模式和策略模式的区别？→ 这里是工厂（根据参数创建对象），策略模式是运行时切换算法
"""

import os
from typing import Any
from langchain_core.language_models.chat_models import BaseChatModel
from dotenv import load_dotenv
'''
多模型适配(Factory)
'''
load_dotenv()

# ============================================================
# 国产厂商 OpenAI 兼容端点（兜底地址）
# ============================================================
# 当用户没有在 .env 中配置 OPENAI_API_BASE 时，
# 根据 provider_name 从这张表中查找厂商的默认端点。
# 注意：DeepSeek 不在此表中——因为 DeepSeek 的 API 标准兼容 OpenAI，
# 直接用 provider_name='openai' + OPENAI_API_BASE='https://api.deepseek.com/v1'
# ============================================================
# 各大厂商官方的 OpenAI 兼容接口地址 (当用户未配置 BASE_URL 时作为兜底)
COMPATIBLE_BASE_URLS = {
    "aliyun": "https://dashscope.aliyuncs.com/compatible-mode/v1",       # 阿里云百炼平台
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",    # 同上（别名）
    "z.ai": "https://open.bigmodel.cn/api/paas/v4",                      # 智谱 AI (GLM 系列)
    "tencent": "https://api.hunyuan.cloud.tencent.com/v1"                # 腾讯混元大模型
}

def get_provider(
    provider_name: str = "openai",
    model_name: str = "gpt-4o-mini",
    temperature: float = 0.0,
    base_url: str | None = None,  # 允许外部传入
    api_key: str | None = None,   # 允许外部传入
    **kwargs: Any
) -> BaseChatModel:
    """
    模型适配器工厂 — 根据提供商名称返回对应的 LangChain ChatModel 实例。

    Args:
        provider_name: 提供商标识（openai / anthropic / aliyun / z.ai / tencent / ollama）
        model_name:    模型名称（如 gpt-4o-mini / deepseek-v4-pro / claude-sonnet-4-6）
        temperature:   生成温度（0.0 = 确定性输出，适合工具调用）
        base_url:      API 端点 URL（可选，优先级高于环境变量）
        api_key:       API 密钥（可选，优先级高于环境变量）
        **kwargs:      透传给底层 ChatModel 的额外参数（如 max_tokens）

    Returns:
        BaseChatModel 实例，可直接调用 .invoke() / .bind_tools()

    Raises:
        ValueError: 未找到 API Key 或传入了不支持的 provider_name
    """
    provider_name = provider_name.lower()

    # ================================================================
    # 分支 1：OpenAI 兼容协议（覆盖 6 家厂商）
    # ================================================================
    # 包括：原生 OpenAI、DeepSeek、阿里百炼、智谱、腾讯混元
    # 以及任何实现了 OpenAI 兼容端点的第三方服务（provider='other'）
    # ================================================================
    if provider_name in ["openai", "aliyun", "dashscope", "z.ai", "tencent", "other"]:
        from langchain_openai import ChatOpenAI

        # ── API Key：显式传参 > 环境变量 OPENAI_API_KEY ──
        current_api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not current_api_key:
            raise ValueError(f"未找到 API Key！请确保 .env 中配置了 OPENAI_API_KEY")

        # ── Base URL 三级 fallback 链 ──
        # 1. 显式传入的 base_url（最高优先级）
        # 2. 环境变量 OPENAI_API_BASE
        # 3. COMPATIBLE_BASE_URLS 中的厂商默认地址（兜底）
        final_base_url = base_url or os.environ.get("OPENAI_API_BASE")
        if not final_base_url:
            final_base_url = COMPATIBLE_BASE_URLS.get(provider_name)

        return ChatOpenAI(
            model=model_name,
            temperature=temperature,
            api_key=current_api_key,
            base_url=final_base_url,
            **kwargs
        )

    # ================================================================
    # 分支 2：Anthropic 官方协议（Claude 系列）
    # ================================================================
    elif provider_name == "anthropic":
        from langchain_anthropic import ChatAnthropic

        current_api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not current_api_key:
            raise ValueError("未找到 ANTHROPIC_API_KEY 环境变量！")

        final_base_url = base_url or os.environ.get("ANTHROPIC_BASE_URL")

        return ChatAnthropic(
            model_name=model_name,
            temperature=temperature,
            api_key=current_api_key,
            base_url=final_base_url,
            **kwargs
        )

    # ================================================================
    # 分支 3：Ollama 本地模型
    # ================================================================
    # 默认连接 localhost:11434，适合离线或隐私敏感场景
    # ================================================================
    elif provider_name == "ollama":
        from langchain_community.chat_models import ChatOllama

        final_base_url = base_url or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

        return ChatOllama(
            model=model_name,
            temperature=temperature,
            base_url=final_base_url,
            **kwargs
        )

    else:
        raise ValueError(f"不支持的模型提供商: {provider_name}")

# 测试模型调用
# LLM = get_provider(provider_name='aliyun', model_name='glm-5')
# res = LLM.invoke('你是谁')
# print(type(res))
# print(res)
