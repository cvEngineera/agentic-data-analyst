"""大模型调用层：LangChain（v0.12，对齐申报表技术栈）。

三条通道，同一份提示词
----------------------
v0.5 起 SQL 生成一直是「标准库 urllib 直连 OpenAI 兼容接口」，理由是当时
pip 装不上 openai SDK，而把「能不能接真实模型」卡在装包上不合理。现在
依赖装得上了，按申报表改成 LangChain，但**不删掉旧通道**：

    LLM_BACKEND=langchain  → 本文件，langchain_openai.ChatOpenAI（默认）
    LLM_BACKEND=urllib     → sql/generator.py 里的 _real_generate（备用）

两条通道打的是同一个 ``POST /chat/completions``，出问题时一键切回，
实验口径不受影响。这既是工程上的降级设计，也方便对照「框架封装」与
「裸协议」在延迟、可观测性上的差异（论文 4.3 节技术选型会用到）。

为什么用 LCEL 而不是 AgentExecutor
----------------------------------
这里的任务只是「给一段提示词，取一段文本」，不需要工具调用与多轮规划；
用 ``prompt | llm | parser`` 三件套最直白。项目自己的多步归因逻辑写在
``agent/runtime.py``，步数上限、重复检测、轨迹记录都是显式的——
把控制流交给框架反而不好在论文里讲清楚每一步为什么这么走。

失败语义
--------
本模块**不吞异常**。调用失败由 ``sql/generator.py`` 捕获并回落 mock 规则，
那里才知道「这一次失败要不要影响评估口径」；在这一层兜底会把
「模型没答上来」伪装成「模型答了个兜底」。
"""
from __future__ import annotations

import os

from ..config import settings

# 进程级缓存：ChatOpenAI 内部持有一个 httpx 客户端（连接池），
# 每次生成都新建实例会反复建池、反复握手，长跑评估时延迟会明显变高。
_LLM = None


def _build_llm():
    """按 settings 构造 ChatOpenAI 实例。

    Returns:
        ChatOpenAI。首次调用时构造，之后复用。

    参数说明：
        - ``base_url``：指向 OpenAI 兼容网关；DeepSeek / 通义 / 硅基流动
          只换这一个值，代码不动——这就是「统一模型调用层」的落地方式。
        - ``temperature``：默认 0，让同一问题尽量产出同一条 SQL，
          否则重复实验的方差会把消融差异淹没掉。
        - ``timeout`` / ``max_retries``：单次 30 秒、重试 1 次。重试交给
          LangChain 而不是项目里的自纠链路——后者针对的是 SQL 语义错误，
          前者针对的是网络抖动，两件事不能混。
        - ``http_client``：本机对部分证书链的吊销检查会失败（GitHub 的
          二进制通道就是这样被掐的），因此允许用 ``LLM_VERIFY_SSL=0``
          关掉校验。默认仍开启校验，只有确实连不上时才关。
    """
    global _LLM
    if _LLM is not None:
        return _LLM

    from langchain_openai import ChatOpenAI

    kwargs = dict(
        model=settings.model,
        base_url=settings.base_url,
        api_key=settings.api_key,
        temperature=settings.temperature,
        timeout=30,
        max_retries=1,
    )
    if os.getenv("LLM_VERIFY_SSL", "1") != "1":
        import httpx

        kwargs["http_client"] = httpx.Client(verify=False, timeout=30)
    _LLM = ChatOpenAI(**kwargs)
    return _LLM


def complete(prompt: str) -> str:
    """把一段提示词发给模型，返回纯文本结果。

    Args:
        prompt: 完整提示词（已含 Schema 片段与可能的错误回喂）。

    Returns:
        模型输出的原始文本，未做 SQL 清洗。清洗（去掉 ```sql 包裹、
        截掉结尾分号）统一在 generator 里做，保证两条通道行为一致。

    Raises:
        Exception: 任何网络/鉴权/配额异常原样上抛，由调用方决定是否兜底。
    """
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.prompts import ChatPromptTemplate

    chain = (
        ChatPromptTemplate.from_messages([("user", "{prompt}")])
        | _build_llm()
        | StrOutputParser()
    )
    return chain.invoke({"prompt": prompt})


def is_available() -> bool:
    """判断 LangChain 通道是否真的可用（依赖已装 + 配了 Key）。

    只做本地检查，不发起网络请求。
    """
    if not settings.api_key:
        return False
    try:
        import langchain_openai  # noqa: F401
    except Exception:
        return False
    return True


def reset() -> None:
    """清空缓存的 LLM 实例。

    测试里切换 model / base_url 后调用，否则旧实例会一直用旧配置。
    """
    global _LLM
    _LLM = None
