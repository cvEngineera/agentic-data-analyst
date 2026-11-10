# 物联网监测数据分析 Agent 系统

一个面向物联网监测数据的 NL2SQL 分析 Agent：自然语言提问 → 自动生成 SQL → 多步归因 → 可复现的分层评估。
覆盖室内空气质量场景（教室 / 设备 / 读数 / 告警四表），带危险语句拦截、报错自纠、执行轨迹与图表输出。

**当前版本：v0.13.1（2026-11-10）** —— 演示库为 **UCI Occupancy Detection 公开真实数据集**；**MySQL 已在 3307 独立实例上端到端跑通**（免密码、不碰本机 3306），8000 端口可直接演示 MySQL 后端。正式评估与回归门禁仍固定在合成数据（种子 42）上，评估入口会在非基准数据源上主动拒绝出数字。技术栈对齐申报表：FastAPI + SQLAlchemy + MySQL + LangChain + Vue 3/ECharts 均已实际运行，**仅 pgvector 未端到端跑通**（WSL/Docker 引擎已验证可用，卡在镜像拉取的本机网络，见第九节）；真实模型 3 轮均值 64.6% ± 0.7%。

> **技术栈对照（申报表 9 项 vs 代码实际）**：Python、大模型接口沿用；**UCI 数据集已启用为当前演示库（v0.13）**；
> FastAPI → `scripts/api.py`，SQLAlchemy → `src/db/orm.py`（模型/引擎）+ `src/db/query.py`（执行，SQLite 与 MySQL 共用），
> MySQL → `src/db/schema_mysql.sql` + `scripts/setup_mysql.py` + `start_mysql_demo.bat`（v0.13.1 已实测跑通），
> LangChain → `src/llm/langchain_client.py`，
> pgvector → `src/schema_retrieval/vector_store.py`，
> Vue 3 + ECharts → `src/web/index.html`（依赖 vendor 在 `src/web/vendor/`）。
> **评估集固定在 SQLite 上跑**，MySQL 只承担演示与部署，原因见 v0.12 的 CHANGELOG。
> `/api/status` 的 `llm_backend` / `db_driver` / `vector_backend` 报的是**实际生效值**，不是配置值。

> 代码已逐模块补齐详细注释：每个文件开头说明它在七模块架构中的位置与设计取舍，
> 每个函数有参数/返回/副作用说明，关键分支（只读连接、危险语句不重试、外键一跳扩展、
> 报错回喂、收敛兜底、执行结果比对、消融开关）都写了「为什么这么做」。

> **两套测试集，用途不同，别混用**：
> - `cases.jsonl`（15 条，**冒烟集**）：mock 下 15/15，只证明工程链路通畅。
> - `cases_eval.jsonl`（144 条，**正式评估集**）：18 语义原型 × 8 口语化变体，标准 SQL 独立人工标注，mock 下 24.3%，
>   用来暴露规则模板的局限。**这两个数字都不能写进论文当准确率结论**。
>
> v0.4.1 起标准答案已与生成器解耦（不再引用 `MOCK_RULES`），口径变更后
> mock 基线为 23.6%，不能与旧版 33.7% 直接比较。
> **真实模型 3 轮均值 64.6% ± 0.7%（单轮最高 65.3%）见 5.1 节**；上面两个 mock 数字仍不能写进论文。

---

## 一、快速开始

```bash
cd agentic-data-analyst
python scripts/init_db.py --force          # 建库 + 灌入 7 天合成数据
python -m src.cli "3号教室昨天的平均温度是多少"
python -m unittest discover -s tests -v    # 27 个单元测试
python scripts/run_eval.py                 # 冒烟集 15 条
```

第 2 增量的四条命令：

```bash
python scripts/build_eval_cases.py                       # 生成 144 条正式评估集
python scripts/run_eval.py --cases src/evaluation/cases_eval.jsonl \
       --output data/reports/eval_summary_full.json      # 正式评估（明细不会覆盖冒烟集）
python scripts/run_ablation.py                           # 五组消融实验
python scripts/run_eval.py --cases src/evaluation/cases_eval.jsonl \
       --output data/reports/ci_summary.json --compare eval/baseline_summary.json
```

试功能：

```bash
python -m src.cli "这周哪个教室空气质量最差，为什么" --show-trace   # 多步归因
python -m src.cli "删除所有设备记录"                                # 安全拦截
python -m src.cli "各教室 CO2 均值排名" --chart                      # 出图（需 matplotlib）
```

启动网页（v0.12 起有两条通道，接口一致，任选其一）：

```bash
python scripts/api.py            # FastAPI，http://127.0.0.1:8000，/docs 是接口文档
python scripts/serve.py          # 零依赖兜底，http://127.0.0.1:8765
```

默认是 **mock 模式**（规则模板生成 SQL，离线、免费、结果确定），适合验证链路。
要看真实模型的效果，用真实模式启动——模型通道会按 `LLM_BACKEND` 走 LangChain，
页面右上角与侧栏会如实显示当前是 `mock` 还是 `real`：

```bash
# Windows：双击或命令行
start_demo.bat real
# 其他平台
LLM_MODE=real python scripts/api.py
```

切到 MySQL（v0.13.1 起**不需要 root 密码**）：

```bash
start_mysql_demo.bat          # 一键：独立实例 + 建库 + 迁移校验 + 启动 FastAPI
start_mysql_demo.bat real     # 上面这步 + 真实大模型
```

脚本用 `mysqld --initialize-insecure` 在 **3307** 起一个全新实例，数据目录
`E:/tmp/mysql3307/data`，`--no-defaults` 保证不读本机 3306 的 `my.ini`；
root 空密码，全程不碰你原来的 MySQL。它自己注入 `DB_BACKEND=mysql` 与
`MYSQL_URL`，不需要改 `.env`。想连已有的 3306 实例也可以：

```bash
set MYSQL_PASSWORD=你的root密码
python scripts/setup_mysql.py          # 建库 + 建表 + 导数 + 行数/聚合双重校验
# 脚本会在进程内切到 mysql（v0.13.1 修），不必先改 .env
```

启用 pgvector（可选，需 Docker）：

```bash
docker compose up -d                   # pgvector/pgvector:pg16，映射到 5433
python scripts/setup_pgvector.py       # 建扩展 + 建 HNSW 索引 + 写入 Schema 向量
# 之后可用 ENABLE_VECTOR_RETRIEVAL=1 让检索叠加向量打分（默认关闭以保持评估口径）
```

> 两个「可选」都不会影响主链路：装不上/连不上时自动降级，且降级来源会
> 写进执行轨迹，不会静默换实现。

可选界面（需 `pip install streamlit`）：`streamlit run src/app.py`

---

## 二、目录结构

```
agentic-data-analyst/
├── .env.example              模型与运行参数模板
├── requirements.txt          基线必需 + 可选增强依赖
├── .github/workflows/
│   └── regression.yml        回归门禁（push/PR 自动跑单测+评估+对比）
├── eval/
│   └── baseline_summary.json 固化进仓库的回归基线（mock，144 题）
├── src/
│   ├── config.py             配置 + 三个消融开关
│   ├── db/
│   │   ├── schema.sql        四张表 DDL、索引、聚合视图
│   │   └── core.py           只读连接、introspect、可复现合成数据
│   ├── schema_retrieval/
│   │   └── catalog.py        数据字典 + 关键词检索 + 外键一跳扩展（可开关）
│   ├── sql/
│   │   ├── generator.py      提示构造 + mock/真实双模式生成
│   │   ├── validator.py      三重校验 + 危险语句与表/字段白名单
│   │   └── executor.py       只读执行、超时中断、报错回喂自纠（可开关）
│   ├── agent/
│   │   └── runtime.py        自研受控运行时：工具白名单、多步归因、
│   │                         步数上限、重复检测、执行轨迹、单轮兜底
│   ├── presentation/
│   │   └── render.py         表格、图表类型建议、一句话结论
│   ├── evaluation/
│   │   ├── cases.jsonl       15 条冒烟集
│   │   ├── cases_eval.jsonl  144 条正式评估集（由脚本生成）
│   │   ├── runner.py         批量运行、五种判定口径、分层统计、回归对比
│   │   └── ablation.py       五组消融配置与 delta 报告
│   ├── cli.py                命令行入口
│   └── app.py                Streamlit 界面（可选）
├── scripts/
│   ├── init_db.py            一键建库
│   ├── build_eval_cases.py   生成正式评估集
│   ├── run_eval.py           评估入口
│   └── run_ablation.py       消融实验入口
├── tests/test_smoke.py       9 个烟雾测试
└── data/
    ├── db/bishe.db           SQLite 库（自动生成，不进 Git）
    └── reports/              评估明细、汇总、消融报告、图表
```

---

## 三、开题报告七个模块与代码的对应

| 开题报告模块 | 已落地的代码 | 当前完成度 |
|---|---|---|
| ⓪ 意图闸门（v0.10） | `agent/intent.py` | 非数据问题直接说明范围，不生成 SQL、不连库、不调模型 |
| ① 数据层建设 | `db/schema.sql`、`db/core.py`、`uci_loader.py`、`live_feed.py` | 合成数据 + UCI 双源、数据字典、可选实时采集（独立 live.db） |
| ② Schema 混合检索 | `catalog.py` + `semantic.py` | 关键词 + 别名 + 外键扩展；轻量语义检索已实现但实测为负结果、默认关闭 |
| ③ SQL 生成与三重校验 | `sql/generator.py`、`sql/validator.py` | 语法/结构/语义三层 + AST 白名单已可用 |
| ④ 安全管控 | `validator.py` + `executor.py` 只读连接 | 只读、危险关键字、表/列白名单、超时、LIMIT、日志轨迹；**拦截率 8/8 独立可测** |
| ⑤ Agent 调度与归因 | `agent/runtime.py` | 工具白名单、多步归因、步数上限、单轮兜底已可用 |
| ⑥ 结果呈现 | `presentation/render.py`、`cli.py`、`app.py`、`web/index.html` | 文本表格 + **纯 SVG 自动配图**（bar/line/pie，零依赖）；`--chart` 可导出 PNG（需 matplotlib）；Streamlit 可选 |
| ⑦ 评估体系 | `evaluation/runner.py`、`ablation.py`、`cases_eval.jsonl` | 144 条正式集、五种判定口径、分层统计、五组消融、回归门禁全部可跑 |

### 步骤 0：意图闸门（v0.10，为什么问「你好」不再返回数据表）

**现象**：v0.9 及之前，问「你好」「你是谁？」会返回一张最近 10 条监测读数的表。

**根因**：`generator.py` 有一条 `FALLBACK_SQL`（「任何规则没命中就返回最近 10 条，
保证系统永远有输出」），而 `runtime.run()` **没有判断输入是不是数据查询**，任何
问题都会走完「找表 → 生成 SQL → 校验 → 执行」。真实模式更隐蔽：`build_prompt`
写死「只输出一条 SELECT」，模型即使认为问题无关也被迫编一条 SQL。这违背了
开题报告承诺的「无法回答时追问澄清，不强行生成错误 SQL」。

**修法**：在执行链路最前面加一层确定性闸门 `agent/intent.py`，判定顺序为
① 空输入 → ② 含数据领域词 / 查询动作词 / 写操作词 → **无条件放行** →
③ 显式寒暄 / 身份 / 能力 / 感谢 / 告别 → ④ 其余判为超出范围。
非数据问题返回 `mode=gate`、空 SQL、空结果集、`steps=0`，轨迹记一条
`intent_gate`，**不连接数据库、不调用模型、零成本**。

判定用关键词规则而非让模型判断，与 `_is_analysis()` 同一取舍：意图判定不该
引入非确定性。数据线索优先级最高，是为了保证「你好，帮我查 3 号教室温度」
仍被放行，以及 `删除所有设备记录` 继续走到校验层被拦截（否则 safety 类评估
口径就变了）。

**验证方式**：`tests/test_smoke.py::TestIntentGate` 六组用例，其中
`test_all_eval_cases_pass_the_gate` 逐条检查 144 条正式评估集全部被判为
`data_query`——这是防止词表调整误伤实验数据的回归保护。实测 CLI 问「你好」
耗时 0.0 ms、SQL 为空；`LLM_MODE=real` 下同样 0.03 ms 返回，未发起任何网络请求。

**边界**：规则式闸门只覆盖常见寒暄与明显领域外提问，长尾的通用问句可能漏拦；
它不替代语义层的查全率，也不改任何评估数字（冒烟 15/15、正式集 mock 23.6%
与 baseline 完全一致）。

**外部框架基线（开题报告 5.1）尚未实施**：`langgraph` / `langchain-core` 在本机
装不上，README 5.3 给出了可直接抄进论文的措辞，不写成已跑过。

---

## 四、评估体系、判定口径与消融实验

### 两套测试集

| | 冒烟集 `cases.jsonl` | 正式集 `cases_eval.jsonl` |
|---|---|---|
| 题量 | 15 | **144**（18 语义原型 × 8 口语化变体） |
| 生成方式 | 手写 | `build_eval_cases.py` 生成，改原型即可维护 |
| 用途 | 验证链路、单测回归 | 分层准确率、消融实验、论文图表 |
| mock 下结果 | 15/15 | **35/144（24.3%）**，安全类 8/8 |

正式集的变体刻意改变语序、同义词、数字写法（3 / 三号），测的是「同一语义、不同说法」的稳定性，这是 NL2SQL 的标准评测维度。**v0.4.1 起标准答案 SQL 独立人工标注**，不再引用生成器规则；口径变更使 mock 下准确率从 33.7% 降到 24.3%，这是消除自证偏差后的诚实基线，两个数字不可直接比较。

### 五种判定口径（`case.check`）

| 口径 | 判定方式 | 适用题型 |
|---|---|---|
| `result` | 执行标准 SQL 与生成 SQL，比对结果集 | 普通查询（等价写法不同也算对） |
| `row_count` | 只比返回行数，期望值由标准 SQL 动态算出 | 趋势/筛选类，允许具体取值小幅差异 |
| `scalar_tolerance` | 单值转 float 后按容差比较 | 计数、均值类聚合 |
| `deny` | 必须被安全层拦截（走完整链路） | 冒烟集旧格式 |
| `deny_sql` | **把危险 SQL 直接喂给 validator**，独立测拦截率 | 正式集安全类，不依赖生成层命中关键词 |
| `conclusion` | 结论必须命中预设原因关键词 | 多步归因 |

`deny_sql` 是本次修正的重点：原来「删掉」「移除」这类同义词没命中 mock 规则时会退化成普通 SELECT，测出来的其实是生成层的关键词覆盖率，而不是校验层的拦截能力。现在安全类 8/8 测的是真正的拦截率。

### 消融实验（五组配置）

```
full / no_fk_expansion / no_retrieval / no_self_correction / minimal
```

三个开关在 `config.py`，由 `ablation.py` 逐组改写、跑完自动还原（避免污染后续评估）。
报告写在 `data/reports/ablation_summary.json`，含每组分层准确率与相对 full 的百分点 delta。

**当前 mock 结果（务必如实理解）**：

| 配置 | 准确率 | Δ vs full | 注入提示词的上下文字符 |
|---|---|---|---|
| full | 24.3% | — | 445 |
| no_fk_expansion | 24.3% | +0.00pp | **330** |
| no_retrieval | 24.3% | +0.00pp | **809** |
| no_self_correction | 24.3% | +0.00pp | 445 |
| minimal | 24.3% | +0.00pp | 809 |

上下文字符数的变化证明开关确实作用到了链路上；准确率 delta 全为 0，是因为 **mock 规则模板不读 `schema_context`、也不会生成错误 SQL**，等于绕过了这三个模块。这不是「模块没用」，而是「当前生成器绕过了模块」。

**接真实模型后重跑 `python scripts/run_ablation.py`，那时的 delta 才是论文第七章可用的消融结论。**

### 回归门禁

`.github/workflows/regression.yml` 在 push / PR 时自动：建库 → 12 个单测 → 生成 144 条评估集 → 分层评估 → 与 `eval/baseline_summary.json` 对比 → 任一分类下降超过 2pp 就失败 → 上传报告 artifact。

容差 2pp 是刻意的：合成数据按运行时刻的 `now` 生成，跨时区的「今天/昨天」数据量会略有差异。接真实模型后应收紧。本地手动对比：

```bash
python scripts/run_eval.py --cases src/evaluation/cases_eval.jsonl \
  --output data/reports/ci_summary.json --compare eval/baseline_summary.json
```

---

## 五、接真实大模型（测真实准确率的唯一途径）

**v0.5 起不需要安装 `openai` SDK**：模型调用改用 Python 标准库 `urllib`
实现 OpenAI 兼容协议，零第三方依赖即可接真实模型（本机 pip 大包反复超时，
不该把「能不能接模型」卡在装包上）。

```bash
# .env 只保存在本地，已被 .gitignore 忽略；确认 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 三项
# 不改文件也可以用环境变量临时指定模式，跑完即恢复默认的离线 mock：
LLM_MODE=real python scripts/run_eval.py --cases src/evaluation/cases_eval.jsonl \
       --output data/reports/eval_summary_real.json
python scripts/run_ablation.py      # 重跑消融，这时的 delta 才有意义
```

**密钥安全**：`.env` 与 `.env.*` 一律不入库（仅 `.env.example` 空模板提交）。
Key 只放在 Authorization 请求头里，**只发往 `LLM_BASE_URL` 指定的模型厂商**；
不写日志、不写报告、不写执行轨迹、不进 Git。默认 `LLM_MODE=mock` 完全离线，
只有显式指定 `real` 才会发起网络请求。

通义千问把 `LLM_BASE_URL` 换成 `https://dashscope.aliyuncs.com/compatible-mode/v1`；
硅基流动换成 `https://api.siliconflow.cn/v1`。**只改三个参数，代码不动**。

✅ v0.4.1 已完成：`cases_eval.jsonl` 的标准答案 SQL 改为独立人工标注的常量，不再引用 `MOCK_RULES`，真值来源与生成器解耦；15 条标准 SQL 全部在四张表上实测可执行。回归门禁基线按新口径重新固化。

模型接口异常、超时或限流时自动回落 mock 并打印提示，系统永远有输出。

### 5.1 真实模型实测结果（2026-11-09，v0.6 单轮 / v0.7 多轮复核）

`LLM_MODE=real` 在 144 条正式评估集上实测（DeepSeek，标准库 HTTP 接入），
与**同一评估集、同一判定口径**的 mock 基线 23.6% 对比。下表是 v0.6 的
**单轮**结果；v0.7 补做 3 轮重复实验后，论文应以 **64.6% ± 0.7%** 作为主结论
（见本节末「重复实验」），不要只引用单轮最高值：

| 类别 | 通过 / 总数 | 准确率 | 相对 mock 基线 |
|---|---|---|---|
| single_filter | 30 / 40 | 75.0% | +47.5pp |
| subquery | 16 / 16 | 100.0% | +93.8pp |
| safety | 8 / 8 | 100.0% | — |
| attribution | 5 / 8 | 62.5% | +37.5pp |
| aggregation | 16 / 32 | 50.0% | +28.1pp |
| trend | 8 / 16 | 50.0% | +31.2pp |
| join | 11 / 24 | 45.8% | +37.5pp |
| **合计（单轮）** | **94 / 144** | **65.3%** | **+41.7pp** |

平均单题 1136 ms、平均 2.03 步，估算成本 0.095 元 / 套（代码内按固定公式估算，
不是平台账单）。固化进仓库的实验数据：`eval/real_eval_summary.json` 与
`eval/real_ablation_summary.json`；逐题明细留在本地 `data/reports/`（被忽略）。

**失败分布（50 例）**：38 例「结果集列与标准答案不一致」（如问型号却多带 `id`
列，属 NL2SQL 投影偏差）、8 例趋势分桶行数不一致、3 例归因选错指标、1 例其他。
幻觉表名 **0 例**（全局表清单约束生效）、兜底触发 1 次、危险语句拦截 8/8。
短板集中在多表 JOIN（45.8%）与聚合（50.0%），论文「失败分析」一节应展开讲。

**本轮修了三处会影响数字的系统性偏差，必须写进论文**：
① 结果集比对对数值列按标准 SQL 的 `ROUND(...,2)` 精度给 0.01 容差
（否则 `22.58` 与 `22.575416…` 被误判）；② 标准答案去掉人为 `LIMIT 50`
（改由 validator 统一补 `row_limit`，让「模型返回更多正确记录」不被行数判定冤枉）；
③ 提示词加全局表清单硬约束，与 validator 白名单对齐。前两条是判定公平性修正，
第三条是工程改进。**语义判定没放宽**：字符串、行数、列顺序仍严格比较。

#### 逐层消融（真实模型，144 题 × 5 组）

| 配置 | 通过 | 准确率 | Δ vs full | 上下文字符 |
|---|---|---|---|---|
| full（完整方案） | 93/144 | 64.6% | — | 445 |
| 关外键一跳扩展 | 89/144 | 61.8% | −2.77pp | 330 |
| 关 Schema 检索 | 82/144 | 56.9% | **−7.64pp** | 809 |
| 关报错自纠 | 90/144 | 62.5% | −2.08pp | 445 |
| minimal（三层全关） | 83/144 | 57.6% | −6.94pp | 809 |

**结论**：Schema 检索贡献最大（−7.64pp），外键扩展与自纠各约 2–3pp。
minimal 反而比「只关检索」高 0.7pp，说明各层存在交互、且关掉检索后提示词
里反而塞进了全部表（上下文 445→809 字符）——这个反常现象要在论文里如实
解释，不能只挑好看的数字报。

**重复实验（v0.7，`scripts/run_repeats.py`）**：同一配置、同一 144 条评估集
连跑 3 轮 → 63.9% / 64.6% / 65.3%，**均值 64.6% ± 0.7%**（样本标准差，
极差 1.39pp）。温度已置 0 仍有波动，论文应引用均值 ± 标准差；消融里的
full 组 64.6% 正好落在均值上，v0.6 报的 65.3% 是单轮最高值，应标注为
「单轮结果」而不是主结论。原始数据在 `eval/real_repeats.json`。

### 5.2 设计目标达成情况与原因分析（论文「实验结果」章用）

开题报告 4.2 的七个目标为**设计目标**，报告第 113 行已声明「最终以实测数据
为准，并如实报告未达成项及其原因分析」。逐项对照如下：

| 指标 | 目标 | 实测（v0.6 单轮） | 结论 | 主要原因 |
|---|---|---|---|---|
| 单轮查询准确率 | ≥85% | 75.0%（`single_filter` 分层，口径近似） | 未达 | 38 例投影偏差：模型返回多余的 `id` 等列，执行结果集比对判错 |
| 多表关联准确率 | ≥65% | 45.8%（`join` 分层） | 未达 | 关联表选错、分组粒度与标准 SQL 不一致 |
| 分析任务完成率 | ≥75% | 62.5%（`attribution` 分层，口径近似） | 未达 | 多步归因时选错判据指标（用 PM2.5 而非 CO₂） |
| 归因结论正确率 | ≥70% | 62.5% | 未达 | 同上；且「最差」缺少统一定义 |
| 平均执行步数 | ≤6 | 2.03 | **达标** | 步数上限与单轮兜底起了收敛作用 |
| 平均响应时间 | <5s | 1.14s | **达标** | 单库单表、只读连接 |
| 单任务成本 | <0.01 元 | ≈0.0007 元/题（0.095 元 / 144 题） | **达标** | 代码按字符粗估，非平台账单 |

两点口径说明，论文里必须写：① `single_filter` / `attribution` 分层与开题报告的
「单轮查询准确率 / 分析任务完成率」不是严格同一定义，只能近似对照；
② 成本是估算值。未达标项不构成验收失败，反而是论文讨论部分的主要内容：
投影偏差可通过列选择约束提示与 few-shot 示例缓解，JOIN 短板指向 Schema
检索与外键扩展的进一步优化（消融已证明该层贡献 −7.64pp）。

### 5.3 LangGraph 外部基线：未实施说明（可直接抄进论文）

开题报告 5.1 计划「在独立环境中以 LangChain 1.0 / LangGraph 运行时搭建外部
框架基线，用同一测试集比较准确率、平均步数与单任务成本」。**该实验未实施**，
原因是开发机无法通过 pip 获取 `langgraph` / `langchain-core` 等依赖
（镜像源连版本列表都取不到，`openai` SDK 同样装不上，v0.5 已因此改为标准库
接入）。主工程的运行时自研不受影响，也未引入任何框架依赖。

论文可表述为：「受开发环境依赖获取限制，外部框架对比实验未能实施，改以
规则模板基线（mock，23.6%）与逐层消融实验作为对照；自研运行时与通用框架
的量化比较列为后续工作。」**不要写成已跑过 LangGraph 基线**。

### 5.4 轻量语义检索对照实验（v0.7，负结果如实保留）

开题报告 4.3 的选型是 Chroma / pgvector，但本机装不上 `chromadb`。改用
`src/schema_retrieval/semantic.py` 的零依赖实现（字符 n-gram + TF-IDF 余弦，
接口与向量库一致、可替换），接到 `hybrid_score()`；**默认关闭**以保持 v0.6
数字可比，由 `scripts/compare_semantic.py` 显式开启并量化贡献。

| 配置 | 通过 / 总数 | 准确率 | Δ |
|---|---|---|---|
| 关键词 + 数据字典（默认主方案） | 94 / 144 | 65.3% | — |
| 叠加轻量语义打分（权重 0.6） | 85 / 144 | 59.0% | **−6.25pp** |

**这是一个负结果，不删、不粉饰**：字符级相似度与中文业务术语别名的强信号
冲突——例如「3号教室昨天的平均温度」给 `site` 0.256、`sensor_data` 只有
0.059，叠加后把维表抬到事实表之前，选表被带偏。它反过来支持本课题的设计
取舍：Schema 检索更依赖领域词典，向量索引只作可选接口。实验采用固定权重、
**未做超参搜索**，不排除调参后改善，列为后续工作。数据：`eval/semantic_compare.json`。

论文写法：不要写成「已部署 Chroma、混合检索提升 X%」；应写「因环境限制
改用轻量语义匹配，实测带来 −6.25pp 退化，故主方案保留关键词检索」。

---

## 六、UCI 公开数据集（v0.13 起为当前演示库）

**当前状态**：`data/db/source.json` = `uci`，演示库用的是 UCI Occupancy Detection
（id=357）真实测量值。`tvoc` / `pm25` 两列为 NULL（数据集无此指标，不编造）。

另一套数据源 `synthetic`：`src/db/core.py::seed_synthetic()` 用固定种子 42 生成
5 个教室 × 7 天 × 每 30 分钟（1680 行），并注入已知异常：**3 号教室最近两天
CO2 偏高 + 一条 CALIBRATION_DRIFT 告警**，用于演示多步归因。144 条正式评估集、
五组消融、回归门禁全部在它上面标定。

```bash
# 切到 UCI 并保留（7 项链路验证，报告写 data/reports/uci_check.json）
python scripts/check_uci.py --keep-uci

# 切回评估基准（论文 / 门禁 / 复现实验前必做）
python scripts/init_db.py --force --source synthetic

# 只验证 UCI 链路、跑完自动恢复 synthetic（CI / 日常回归用）
python scripts/check_uci.py
```

**v0.13 的数据源防呆**：`python scripts/run_eval.py` 检测到当前库不是
`synthetic` 会直接退出（退出码 2），不会产出一串与基线不可比的准确率；
确需只验链路时加 `--allow-other-source`，汇总里会带 `data_source` 与
`warning`。页面右上角有 `UCI / 合成` 徽章，工作台会提示当前数字口径。
切换前已把原来的合成库备份为 `data/db/bishe.synthetic.db`（本地文件，不入库），
也可用上面的命令重建，二者数值一致。

`src/db/uci_loader.py` 负责下载、解析与字段映射；四张表结构与其余六个模块
一行都不用改——这就是开题报告里写的数据接入适配器设计。

### 6.1 联网实时采集（可选，v0.8）

**能自动从网上拿数据，但写进独立的 `live.db`，不碰评估库。**

```bash
python scripts/refresh_live.py              # 手动拉一次
python scripts/refresh_live.py --loop 3600  # 每小时自动，Ctrl+C 停止
```

数据源：Open-Meteo 的 Weather / Air Quality 公开接口，免费、无需 API Key、
非商业用途。一次刷新 5 个坐标 × 2 接口 = 10 次请求，别把间隔设太短。
`src/db/live_feed.py` 用标准库实现，按 `(device_id, ts)` 唯一索引幂等去重，
单点失败不影响其它站点。

| 字段 | 来源 |
|---|---|
| temperature | `current.temperature_2m` |
| humidity | `current.relative_humidity_2m` |
| pm25 | `current.pm2_5` |
| co2 / tvoc | **NULL**，公开源没有，不推算不编造 |

**双库隔离是硬要求**：`bishe.db`（冻结评估库）承载 144 条评估集、baseline、
64.6% ± 0.7%、消融与回归门禁，永远不允许实时数据写入，否则所有实验数字
不可复现、论文第七章作废。`live.db` 只用于演示「真实数据在库里、能查能画图」。

三点如实说明，答辩别含糊：

1. **CO2 / TVOC 缺失**，所以 `live.db` 不支持归因演示（现有归因靠 CO2 均值
   找最差教室）；要恢复得改用 PM2.5 判据或接入带 CO2 的数据源。
2. **Open-Meteo 是网格化再分析数据**，市区内几个坐标常落在同一网格，实测
   5 个采样点里 4 个数值完全相同。它适合演示接入链路，不能冒充「5 间教室
   的独立传感器」。
3. 论文里写「离线公开数据集 + 注入式已知异常 + 可选的实时接入演示」，
   不要写成「实时物联网监测系统」。

---

## 七、分阶段开发路线（按备考节奏倒排）

| 阶段 | 时间 | 增量目标 | 验收方式 |
|---|---|---|---|
| 基线 ✅ | 2026.11 | 一句话进、结果出；七个模块骨架；分层评估可跑 | `unittest` 9/9 + 冒烟集 15/15 |
| 第 2 增量 ✅ | 2026.11 | 104 条正式集、五种判定口径、五组消融、回归门禁 | 分层报告 + 消融报告 + CI 通过 |
| 第 3 增量 ✅ | 2026.11 | 接真实模型 + 144 条独立人工标注；接 UCI 数据集；语义检索对照（负结果） | 真实 3 轮均值 64.6% ± 0.7%、UCI 链路 7/7、语义 −6.25pp |
| 第 4 增量 ✅ | 2026.11 | 五组消融、失败分布分析、目标指标差距分析 | 消融 delta、38 处列不一致等失败归类 |
| 第 5 增量 ◐ | 2026.11 | FastAPI / LangChain / SQLAlchemy / MySQL（3307 独立实例）/ Vue 3+ECharts 均已跑通；**LangGraph 外部基线、Docker + pgvector 容器未跑通** | 技术栈对照表 + 第九节已知限制 |
| 论文 | 2027.03—05 | 图表、消融分析、失败案例、查重、答辩 | 论文 + PPT |

**每完成一层跑一次 `run_eval.py --cases ... --output ...`，把汇总另存为新的 baseline，
下次用 `--compare` 看分层 delta** —— 这些累积报告就是论文第七章的实验数据。

---

## 八、保底版边界

如果时间被考试切碎，守住下面这一组就足以毕业：

- 单数据集 + 关键词/向量混合检索 + 语法结构校验 + 单轮兜底
- 100 条以上测试集 + 分层准确率 + 一组基线对比
- 可运行的 CLI + 一份实验报告

**不要砍掉的**：分层评估、执行轨迹、安全拦截。这三项是答辩时「工作量和技术深度」的主要证据。

---

## 九、已知限制（别在答辩时含糊）

- **当前演示库是 UCI 真实数据，不是评估基准（v0.13）**：论文里所有准确率、消融 delta、失败分布均在合成数据（种子 42）上测得；UCI 库只用于演示与「数据来源」一章。`run_eval.py` 在非 synthetic 上默认拒绝（退出码 2），`/api/eval` 返回带 `warning`，切回命令见第六节；
- **mock 模式的准确率没有学术意义**，只验证链路；同口径 mock 基线 23.6%，真实模型 3 轮均值 64.6% ± 0.7%（见 5.1 节）；
- 标准答案已独立人工标注（v0.4.1），数值比对按标准 SQL 的 `ROUND(...,2)` 精度给 0.01 容差（v0.6）；语义判定未放宽；
- 真实模型下消融 delta 非零，最大贡献来自 Schema 检索（−7.64pp）；
- 目标指标中单表 75%（目标 85%）、多表 JOIN 45.8%（目标 65%）、归因 62.5%（目标 70%）未达标。开题报告已声明「以实测为准、如实报告未达成项」，论文需给原因分析，不要粉饰；
- **Chroma / pgvector（v0.12 更新）**：v0.7 时本机装不上 `chromadb`，改用 `semantic.py` 的零依赖语义打分接到 `hybrid_score()`；v0.12 已按申报表补上 pgvector 通道（`vector_store.py`，HNSW + 余弦距离），但**本机 Docker 的 WSL 后端被安全策略拦住，容器未起，实测走的是内存向量降级路径**。两者的向量都是字符 n-gram 哈希，**不是神经网络 embedding**（DeepSeek 无 embedding 接口），语义能力弱于 BGE；开启后准确率 65.3% → 59.0%（**−6.25pp**，v0.7 实测），故默认关闭，见 5.4 与 `eval/semantic_compare.json`；
- **SQLAlchemy 是默认执行路径（v0.12.1）**：`db/query.py` 用 `Engine + text()` 执行 SQLite 查询，只读靠 URI 的 `mode=ro`、超时靠原生 `set_progress_handler`、参数仍是 `:p0` 真绑定；装不上 SQLAlchemy 时自动降级 `sqlite3`，`/api/status` 的 `db_driver` 会显示实际用了哪个，不会出现「文档写一套、实际跑另一套」；
- **MySQL 已在独立实例上跑通（v0.13.1）**：本机 3306 的 root 密码未知，改用 `start_mysql_demo.bat` 在 **3307** 起全新实例（空密码、独立 datadir、`--no-defaults`）；四表行数 + 3 个聚合值双重校验全 OK，JOIN、参数绑定、只读拒绝（errno 1792）、视图、真实模型问答均实测通过。**「用了 MySQL」指的是这个 3307 实例，不是本机原有的 3306**；`DB_BACKEND` 默认仍是 sqlite，评估数字不受影响；
- **评估固定 SQLite**：144 条用例的标准 SQL 是 SQLite 方言，`evaluate()` 会强制把后端钉回 sqlite。MySQL 只用于演示/部署，两者的数据由迁移脚本保证一致；不要把 MySQL 上的结果与评估数字混着说；
- **LangGraph 外部框架基线（开题报告 5.1）未实施**；v0.12 起 SQL 生成走 LangChain（`langchain_openai.ChatOpenAI`），但**多步归因仍是自研运行时**，没有用 LangGraph 的图编排。论文措辞要区分「用了 LangChain」与「用了 LangGraph」；
- 真实模型非确定性已量化：3 轮 63.9% / 64.6% / 65.3%，均值 64.6% ± 0.7%，论文主结论用均值；
- 目前没有语义层/指标中台（这是**特意不做**的对照设计）；
- 没有行列级权限、多租户、实时连库、流式输出；
- 成本是按字符粗估的，不是真实 Token 计费；
- 归因目前是模板化的「取数 → 查告警 → 给结论」，不是自由规划；
- **LangGraph 外部基线未实施；pgvector 容器尚未落地**：`wsl.exe` 的 WorkBuddy 程序黑名单已于 2026-11-10 解除，实测 WSL 3.0.3 与 Docker Desktop 29.8.2 的 Linux 引擎都能正常启动（原有 4 个容器自动恢复），**所以不再是 WSL 的问题**。卡点是镜像拉取：本机 Docker 配的 `127.0.0.1:7897` 代理未运行；改国内镜像源后能取到 manifest，但 blob 被 CDN 鉴权与限速挡住，离线导入大层也超时。pgvector 容器因此未起，**向量检索仍走内存降级**，答辩时不要说 pgvector 已端到端验证；
- **跑通 pgvector 的最短路径**：启动本机监听 `7897` 的代理软件（Docker Desktop 原本就配好了它），然后 `docker compose up -d` → `python scripts/setup_pgvector.py` → `ENABLE_VECTOR_RETRIEVAL=1` 重启服务。镜像一旦本地化，后续不再依赖网络；

---

## 十、常用命令速查

| 目的 | 命令 |
|---|---|
| 重建数据库（切回评估基准 synthetic） | `python scripts/init_db.py --force --source synthetic` |
| 切到 UCI 真实数据（当前演示库） | `python scripts/init_db.py --force --source uci` |
| UCI 链路 7 项验证 | `python scripts/check_uci.py`（自动切回）/ `--keep-uci`（保留 UCI） |
| 单轮查询 | `python -m src.cli "问题"` |
| 多步归因 | `python -m src.cli "问题" --mode agent --show-trace` |
| JSON 输出 | `python -m src.cli "问题" --json` |
| 强制真实模型 | `python -m src.cli "问题" --llm real` |
| 冒烟评估（15 条） | `python scripts/run_eval.py`（需先切回 synthetic，否则退出码 2） |
| 生成正式评估集 | `python scripts/build_eval_cases.py` |
| 正式评估（144 条） | `python scripts/run_eval.py --cases src/evaluation/cases_eval.jsonl --output data/reports/eval_summary_full.json` |
| 回归对比 | `python scripts/run_eval.py --cases src/evaluation/cases_eval.jsonl --output data/reports/ci_summary.json --compare eval/baseline_summary.json` |
| 消融实验 | `python scripts/run_ablation.py` |
| 单测 | `python -m unittest discover -s tests -v` |
| 界面（FastAPI，推荐） | `python scripts/api.py` → `http://127.0.0.1:8000`（`/docs` 自动接口文档） |
| MySQL 后端演示（免密码） | `start_mysql_demo.bat`（独立 3307 实例 + 迁移 + 启动）/ `start_mysql_demo.bat real` |
| 界面（零依赖兜底） | `python scripts/serve.py` → `http://127.0.0.1:8765` |
| 真实模型演示 | `start_demo.bat real`（不改 `.env`，避免污染测试与评估） |
| 界面（离线快照） | 直接双击 `src/web/index.html`，点快捷问题看预录结果 |
| 刷新离线快照 | `python scripts/build_snapshots.py`（改了运行时逻辑后重跑） |

---

## 十一、版本与迭代约定

**每完成一步都会交付一个能使用的版本**，具体是四条硬要求（完整记录见 `CHANGELOG.md`）：

1. 能运行：单测与主要脚本全绿；
2. 能打开看：有可直接使用的入口，不只是改代码；
3. 有记录：`CHANGELOG.md` 新增一节，写清新增文件、能跑什么、验证数字、已知限制；
4. 能看到变化：`git commit` + 打 tag，页面上「代码变化」区与
   `git diff <上一版> <本版> --stat` 都能看到改了什么。

### 现在怎么打开看

```bash
python scripts/serve.py          # 或 Windows 双击 start_demo.bat
# 浏览器自动打开 http://127.0.0.1:8765
```

v0.9 起界面按 Chat2DB 桌面客户端的版式组织：顶部居中标题栏（版本 / 模型模式 /
数据源）、最左图标栏、**会话列表侧栏**（新建对话、搜索、近期对话、5 个快捷示例，
历史存浏览器 localStorage）、主区欢迎页与**内嵌工具栏的输入框**。提问后变成对话
消息流，输入框自动停靠底部；每条回答可折叠查看生成的 SQL、结果表格与执行轨迹。

「工作台」抽屉（右上角按钮 / 左侧第二个图标）承载原先右栏的全部信息：数据源状态、
评估 KPI、现跑评估与消融、消融静态表、版本演进时间线、git 代码变化、命令速查。
快捷键：`Ctrl+K` 聚焦输入框、`Enter` 发送、`Shift+Enter` 换行、`Esc` 关抽屉。

**离线直接双击 `src/web/index.html` 也能用**：5 个快捷示例内嵌了预录的真实运行结果，
会明确标注「离线快照」；工作台的 git 与现跑按钮在离线时给出提示，不会假装成功。

### 版本一览

| 版本 | 日期 | 能用的东西 |
|---|---|---|
| v0.1 | 2026-11-07 | 七模块骨架、CLI、15 条冒烟集、9 个单测 |
| v0.2 | 2026-11-07 | 104 条正式集、五种判定口径、五组消融、回归门禁 |
| v0.3 | 2026-11-08 | 零依赖浏览器演示、版本可视化、Git 迭代历史 |
| v0.3.1 | 2026-11-08 | 离线也能点：5 个快捷问题的真实结果快照 |
| v0.3.2 | 2026-11-08 | 代码变化区自动跟随最新两个 tag |
| v0.3.3 | 2026-11-09 | 本地目录与云端仓库统一为 agentic-data-analyst |
| v0.4 | 2026-11-09 | UCI 公开数据集接入，两种数据源可切换 |
| v0.4.1 | 2026-11-09 | 标准 SQL 独立标注、评估集扩到 144 条 |
| v0.5 | 2026-11-09 | 真实模型零依赖接入、密钥本地防护（默认仍离线） |
| v0.6 | 2026-11-09 | 真实模型实测（单轮 65.3%）、逐层消融产出非零 delta |
| v0.7 | 2026-11-09 | 3 轮重复实验 64.6% ± 0.7%、指标差距分析、轻量语义检索对照 |
| v0.8 | 2026-11-09 | 可选的联网实时采集，双库隔离不影响实验 |
| v0.9 | 2026-11-09 | 深色客户端风格界面：会话列表、对话式结果、工作台抽屉 |
| v0.10 | 2026-11-09 | 意图闸门：非数据问题不生成 SQL、不查库 |
| **v0.11** | **2026-11-09** | **结果页真正出图：纯 SVG 柱状/折线/饼图，零依赖** |

### 数据源切换（v0.4 实现，v0.13 起当前演示库为 uci）

```bash
python scripts/check_uci.py --keep-uci            # 切到 UCI + 7 项验证 + 保留（当前状态）
python scripts/init_db.py --force --source synthetic  # 切回评估基准，论文/门禁前必做
python scripts/check_uci.py                       # 只验 UCI 链路，跑完自动恢复 synthetic
```

| | synthetic（评估基准） | uci（当前演示库） |
|---|---|---|
| 数值来源 | 固定种子 42 合成 | UCI Occupancy Detection，20560 条抽样到 1680 行 |
| 用途 | 144 条正式评估集、五组消融、回归门禁、离线复现 | 演示、数据接入适配器验证、论文数据来源 |
| tvoc / pm25 | 有值 | **NULL**（数据集无此指标，不编造） |

UCI 模式的三处预处理：表头缺列按位置解析；2015 年时间轴平移到最近 7 天；
单房间数据轮转分配到 5 个虚拟教室，并注入式保留 3 号教室的已知异常，
供归因链路判定。页面右上角显示 `UCI / 合成` 徽章，悬停可看数据集全称与说明。

**正式准确率只在 synthetic 上统计**。v0.13 起 `run_eval.py` 在非基准源上
直接拒绝（退出码 2），`--allow-other-source` 只用于验链路、汇总带 `warning`；
两种口径不混用，否则「跳过不适用的题」会让数字好看但失去意义。

查看变化：

```bash
git log --oneline --decorate
git tag -l
git diff v0.2 v0.3 --stat       # 本版新增了哪些文件、改了多少行
git show v0.3 --stat
```

Git 历史从 v0.2 开始（v0.1 时还没建仓库，不伪造历史，其变更以文字记在 CHANGELOG）。
之后每完成一个小步骤都单独提交、单独打 tag，不攒成大版本一起交。
