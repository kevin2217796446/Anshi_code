"""基于 LangChain 框架的大模型调用入口，配置从 .env 读取。"""

import base64
import logging
import os

import httpx
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

#加载配置
load_dotenv()

_logger = logging.getLogger(__name__)


def _get_llm(
    provider: str = "deepseek",
    model: str | None = None,
    temperature: float = 0.5,
    top_p: float = 0.95,
    presence_penalty: float = -1.8,
    max_tokens: int = 15000,
    extra_body: dict | None = None,
    reasoning_effort="high"
) -> ChatOpenAI:
    """构建 ChatOpenAI 实例，配置从 .env 环境变量读取。"""
    if provider == "local":
        base_url = os.getenv("LLM_LOCAL_BASE_URL", "")
        api_key = os.getenv("LLM_LOCAL_API_KEY", "") or "local"
        if not base_url:
            raise ValueError("LLM_LOCAL_BASE_URL 未在 .env 中配置")
    elif provider == "deepseek":
        base_url = os.getenv("LLM_DEEPSEEK_BASE_URL", "")
        api_key = os.getenv("LLM_DEEPSEEK_API_KEY", "")
    elif provider == "qwen":
        base_url = os.getenv("LLM_QWEN_BASE_URL", "")
        api_key = os.getenv("LLM_QWEN_API_KEY", "")
    elif provider == "teco":
        base_url = os.getenv("LLM_TECO_BASE_URL", "")
        api_key = os.getenv("LLM_TECO_API_KEY", "")
    else:
        raise ValueError(f"不支持的 LLM provider: {provider}")

    if not base_url:
        raise ValueError(f".env 中缺少 {provider} base_url 配置")
    if not api_key:
        raise ValueError(f".env 中 {provider} api_key 为空")

    # 默认模型
    if not model:
        if provider == "local":
            model = os.getenv("LLM_LOCAL_MODEL", "")
        elif provider == "qwen":
            model = os.getenv("LLM_QWEN_MODEL", "")
        elif provider == "teco":
            model = os.getenv("LLM_TECO_MODEL", "")
        else:
            model = os.getenv("LLM_DEEPSEEK_MODEL", "")
    if not model:
        raise ValueError(f"未指定模型，且 .env 中缺少 {provider} model 配置")

    # HTTP 客户端（local 关闭 SSL 验证、跳过代理）
    http_client_kwargs = {}
    default_headers = {}

    if provider == "local":
        http_client_kwargs["verify"] = False
        http_client_kwargs["trust_env"] = False
        local_user = os.getenv("LLM_LOCAL_USER", "")
        local_password = os.getenv("LLM_LOCAL_PASSWORD", "")
        if local_user and local_password:
            auth_value = base64.b64encode(
                f"{local_user}:{local_password}".encode("utf-8")
            ).decode("ascii")
            default_headers["Authorization"] = f"Basic {auth_value}"

    kwargs = {
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "temperature": temperature,
        "top_p": top_p,
        "presence_penalty": presence_penalty,
        "max_tokens": max_tokens,
        "http_async_client": httpx.AsyncClient(**http_client_kwargs),
    }
    # reasoning_effort 仅 OpenAI/DeepSeek 支持，Qwen 等不传；None 表示禁用（不传给 API）
    if provider in ("deepseek",) and reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort

    if default_headers:
        kwargs["default_headers"] = default_headers
    if extra_body:
        kwargs["extra_body"] = extra_body

    return ChatOpenAI(**kwargs)


async def call_llm_api(
    prompt: list[dict],
    model: str | None = None,
    provider: str = "deepseek",
    max_tokens: int = 10000,
    temperature: float = 1.0,
    top_p: float = 0.95,
    presence_penalty: float = -1.8,
    reasoning_effort="high",
    extra_body: dict | None = None,
):
    """
    统一的大模型调用入口（LangChain 版），配置从 .env 读取。

    参数：
    - prompt: OpenAI ChatCompletion 格式的 messages 列表
    - 其余同 call_llm.call_llm_api
    """
    try:
        llm = _get_llm(
            provider=provider,
            model=model,
            temperature=temperature,
            top_p=top_p,
            presence_penalty=presence_penalty,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            extra_body=extra_body,
        )

        lc_messages = []
        for m in prompt:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "system":
                lc_messages.append(SystemMessage(content=content))
            else:
                lc_messages.append(HumanMessage(content=content))

        response = await llm.ainvoke(lc_messages)
        return response

    except Exception as e:
        _logger.error("调用 LLM API 时出错 (provider=%s): %s", provider, e)
        raise
