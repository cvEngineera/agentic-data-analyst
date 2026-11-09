"""全局配置模块（对应开题报告 4.3「技术选型与开发环境」）。

职责：集中管理路径、模型供应商参数与运行约束，让其余模块不各自硬编码配置。

设计取舍：
1. **零第三方依赖**。自己解析 .env，不强制装 python-dotenv；环境变量优先级高于
   .env 文件，方便 CI / 命令行临时覆盖（例如 ``LLM_MODE=real python -m src.cli ...``）。
2. **单一配置对象**。所有模块 import 同一个 ``settings`` 实例；CLI 用
   ``settings.llm_mode = ...`` 覆盖时，其余模块立即生效（因此 Settings 不能 frozen）。
3. **模型供应商解耦**。只通过 base_url / api_key / model 三个参数切换
   DeepSeek、通义、硅基流动，调用代码一行不改，对应可行性分析里的
   「统一模型调用层，支持多供应商切换」。

注意：本文件在 import 时就会执行 _load_dotenv() 并构造 settings，
所以若要在进程内改配置，应修改 settings 实例而不是重新加载模块。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# ---- 路径常量 ------------------------------------------------------------
# 全部用「相对本文件位置」推导，保证把整个 agentic-data-analyst 目录复制到任意盘符都能跑。
ROOT = Path(__file__).resolve().parents[1]          # 项目根目录 agentic-data-analyst/
DATA_DIR = ROOT / "data"                            # 所有运行时数据的根目录
DB_DIR = DATA_DIR / "db"                            # SQLite 数据库文件所在目录
DB_PATH = DB_DIR / "bishe.db"                       # 唯一的业务库文件
REPORT_DIR = DATA_DIR / "reports"                   # 评估明细、汇总、图表输出目录
SCHEMA_PATH = Path(__file__).resolve().parent / "db" / "schema.sql"   # 四张表 DDL
CASES_PATH = Path(__file__).resolve().parent / "evaluation" / "cases.jsonl"  # 分层测试集


def _load_dotenv() -> None:
    """读取项目根目录的 .env，补进 os.environ。

    采用 setdefault 而不是直接赋值：已存在的环境变量优先，这样命令行或 CI
    注入的配置不会被本地文件覆盖。解析规则刻意保持简单（只支持 KEY=VALUE），
    是为了不引入第三方依赖；引号会被去掉，注释行与空行跳过。

    副作用：修改进程级 os.environ。无返回值。
    """
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        # 跳过空行、注释行、没有等号的行
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)  # 只切一次，兼容值里包含等号的情况
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# 必须在定义 Settings 之前执行：默认值来自 os.getenv
_load_dotenv()


@dataclass
class Settings:
    """运行参数的集中定义。

    每个字段都可以在 .env 里覆盖；默认值按「离线也能跑通」的原则选取。
    """

    # mock = 规则模板生成 SQL（无需联网/Key）；real = 调用大模型。
    # 基线默认 mock，保证第一周目标不依赖申请 API Key。
    llm_mode: str = os.getenv("LLM_MODE", "mock")
    model: str = os.getenv("LLM_MODEL", "deepseek-chat")
    base_url: str = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
    api_key: str = os.getenv("LLM_API_KEY", "")
    # 温度置 0：让同输入输出尽量一致，演示与评估才可复现。
    temperature: float = float(os.getenv("LLM_TEMPERATURE", "0"))
    # Agent 最大步数，对应开题报告「带收敛约束的多步归因机制」。
    max_steps: int = int(os.getenv("MAX_STEPS", "6"))
    # 单条 SQL 的执行超时（秒），防止全表扫描拖死演示。
    query_timeout: int = int(os.getenv("QUERY_TIMEOUT", "5"))
    # 结果行数上限，validator 会对没有 LIMIT 的语句自动补上。
    row_limit: int = int(os.getenv("ROW_LIMIT", "1000"))

    # ---- 可选语义检索（v0.7）-------------------------------------------
    # 默认关闭：v0.6 的 65.3%、五组消融与重复实验都是在「关键词 + 数据字典」
    # 口径下测的，保持默认不变才能让已有数字可比。开启方式：
    #   ENABLE_SEMANTIC_RETRIEVAL=1 或 scripts/compare_semantic.py 显式切换。
    # 打开后 hybrid_score() 会叠加 semantic.py 的零依赖语义相似度；
    # 该实现是字符 n-gram + TF-IDF，不是 Chroma 的神经网络 embedding，
    # 但接口一致，将来装得上向量库可直接替换 provider。
    enable_semantic_retrieval: bool = os.getenv("ENABLE_SEMANTIC_RETRIEVAL", "0") == "1"

    # ---- 消融实验开关 ---------------------------------------------------
    # 三个开关默认全开（即完整方案）。ablation.py 会在每组实验前临时改写
    # settings 实例，跑完恢复，保证「逐层独立开关」这一论文要求可落地。
    # 也可用环境变量覆盖，便于单组复现，例如 ABLATION_FK_EXPANSION=0。
    ablation_fk_expansion: bool = os.getenv("ABLATION_FK_EXPANSION", "1") == "1"
    ablation_schema_retrieval: bool = os.getenv("ABLATION_SCHEMA_RETRIEVAL", "1") == "1"
    ablation_self_correction: bool = os.getenv("ABLATION_SELF_CORRECTION", "1") == "1"

    @property
    def real_mode_available(self) -> bool:
        """判断真实模型是否可用：只需配置了 API Key。

        v0.5 起模型调用改用标准库 ``urllib`` 走 OpenAI 兼容协议，不再依赖
        ``openai`` SDK。本机 pip 大包反复超时，把「能不能接真实模型」卡在
        装包上不合理；少一个依赖也少一处供应链风险。

        注意：这里只检查 Key 是否存在，**不发起任何网络请求**。是否真的
        调用模型由 ``llm_mode`` 决定，默认是 mock（完全离线）。
        """
        return bool(self.api_key)


# 全局单例：其余模块统一从这里读配置
settings = Settings()


def ensure_dirs() -> None:
    """确保数据目录存在，避免首次运行时因目录缺失报 FileNotFoundError。

    幂等，可重复调用；在建库、写报告、出图之前都会调用一次。
    """
    for d in (DATA_DIR, DB_DIR, REPORT_DIR, DATA_DIR / "raw"):
        d.mkdir(parents=True, exist_ok=True)
