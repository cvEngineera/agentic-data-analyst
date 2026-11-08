"""结果呈现层（对应开题报告「4.1（6）结果呈现」）。

职责：把查询结果转成人能看的东西——表格、图表类型建议、一句话结论。

设计取舍：
- 基线用**纯文本表格**，不依赖 pandas / matplotlib，命令行里直接可读。
- ``choose_chart`` 只返回「建议用哪种图」，不负责画图。这样命令行、Streamlit、
  论文图表三条路径可以共用同一套判断逻辑，也方便单独测试。
- ``save_chart`` 是可选增强：装了 matplotlib 才出图，没装就返回空字符串，
  主链路完全不受影响（延续全项目的优雅降级思路）。

**显示生成的 SQL 是安全设计而非炫技**：用户能核对系统到底查了什么，
避免「SQL 语法对但口径错」这种最危险的静默错误（开题报告 4.1 与 6）。
"""
from __future__ import annotations

from ..config import REPORT_DIR, ensure_dirs


def format_table(columns: list[str], rows: list[tuple], max_rows: int = 12) -> str:
    """把结果集格式化成等宽文本表格。

    Args:
        columns: 列名列表。
        rows: 行元组列表。
        max_rows: 最多显示几行。默认 12，避免一次刷屏；
                  真实行数由 validator 的 LIMIT 控制，这里只管展示。

    Returns:
        可直接 print 的多行字符串；无列时返回「（无结果）」。
    """
    if not columns:
        return "（无结果）"
    shown = rows[:max_rows]
    # 每列宽度 = max(表头长度, 各单元格长度)，再截到 28 防止超宽破坏排版
    widths = [max(len(str(c)), *(len(str(r[i])) for r in shown)) if shown else len(str(c))
              for i, c in enumerate(columns)]
    widths = [min(w, 28) for w in widths]
    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def line(cells) -> str:
        """把一行单元格按 widths 左对齐拼成表格行。"""
        return "|" + "|".join(" " + str(v)[:w].ljust(w) + " "
                              for v, w in zip(cells, widths)) + "|"

    out = [sep, line(columns), sep]
    out += [line(r) for r in shown]
    out.append(sep)
    if len(rows) > max_rows:
        out.append(f"（仅显示前 {max_rows} 行，共 {len(rows)} 行）")
    return "\n".join(out)


def choose_chart(question: str, columns: list[str], rows: list[tuple]) -> str:
    """按问题语义 + 结果形态自动选图，返回 line / bar / pie / table。

    判断优先级：时序 > 排名 > 占比 > 表格。
    都要求 len(rows) > 1，因为单行结果画图没有信息量。
    这套规则是「自动配图」的基线，后续可以换成模型判断，
    但接口不变，正好又是一处可消融的开关。
    """
    q = question.lower()
    if any(k in q for k in ("趋势", "按小时", "按天", "变化", "走势")) and len(rows) > 1:
        return "line"
    if any(k in q for k in ("排名", "最多", "最高", "最差", "top")) and len(rows) > 1:
        return "bar"
    if any(k in q for k in ("占比", "比例", "构成")) and len(rows) > 1:
        return "pie"
    if len(rows) == 1 and len(columns) == 1:
        return "table"
    return "table"


def conclusion_text(columns: list[str], rows: list[tuple]) -> str:
    """生成一句话结论（通用版）。

    注意：Agent 模式的结论由 runtime 给出（带归因依据），这里只用于
    单轮/脚本场景的简单摘要，两者不要混用。
    """
    if not rows:
        return "查询未返回数据。"
    if len(rows) == 1 and len(columns) == 1:
        return f"结果为 {rows[0][0]}。"
    return f"共返回 {len(rows)} 行、{len(columns)} 个字段，详见表格。"


def save_chart(columns: list[str], rows: list[tuple], kind: str,
               name: str = "chart") -> str:
    """可选：用 matplotlib 把结果画成图并保存到 data/reports/。

    Args:
        kind: choose_chart 的返回值；为 table 时不出图。
        name: 输出文件名（不含扩展名）。

    Returns:
        图片绝对路径；未安装 matplotlib 或不需要出图时返回空字符串。

    约定：第一列当标签、第二列当数值；取不到数值时填 0，
    保证脏数据不会让演示崩掉。最多画前 20 行，避免标签挤成一团。
    """
    try:
        import matplotlib
        matplotlib.use("Agg")  # 无界面后端，服务器上也能跑
        import matplotlib.pyplot as plt
    except ImportError:
        return ""
    if kind == "table" or not rows:
        return ""
    ensure_dirs()
    labels = [str(r[0])[:12] for r in rows[:20]]
    values = []
    for r in rows[:20]:
        try:
            values.append(float(r[1]))
        except (IndexError, ValueError, TypeError):
            values.append(0.0)
    plt.figure(figsize=(9, 4))
    if kind == "line":
        plt.plot(labels, values, marker="o")
    elif kind == "pie":
        plt.pie(values or [1], labels=labels or ["-"], autopct="%1.1f%%")
    else:
        plt.bar(labels, values)
    plt.xticks(rotation=45, ha="right")
    plt.tight_layout()
    path = REPORT_DIR / f"{name}.png"
    plt.savefig(path, dpi=120)
    plt.close()
    return str(path)
