# 物联网监测数据分析 Agent 系统

毕业设计课题《基于大语言模型的物联网监测数据分析 Agent 系统设计与实现》的代码工程。

**当前版本：v0.2 第 2 增量（2026-11-07）** —— v0.1 跑通七个模块骨架；v0.2 补齐正式评估集（104 条）、多样化判定口径、逐层消融开关与回归门禁。

> 代码已逐模块补齐详细注释：每个文件开头说明它在七模块架构中的位置与设计取舍，
> 每个函数有参数/返回/副作用说明，关键分支（只读连接、危险语句不重试、外键一跳扩展、
> 报错回喂、收敛兜底、执行结果比对、消融开关）都写了「为什么这么做」。

> **两套测试集，用途不同，别混用**：
> - `cases.jsonl`（15 条，**冒烟集**）：mock 下 15/15，只证明工程链路通畅。
> - `cases_eval.jsonl`（104 条，**正式评估集**）：口语化表述变体，mock 下 33.7%，
>   用来暴露规则模板的局限。**这两个数字都不能写进论文当准确率结论**。
>
> 真实准确率必须在 `LLM_MODE=real` 下、用人工标注的标准 SQL 重新测量。

---

## 一、快速开始

```bash
cd bishe-agent
python scripts/init_db.py --force          # 建库 + 灌入 7 天合成数据
python -m src.cli "3号教室昨天的平均温度是多少"
python -m unittest discover -s tests -v    # 9 个烟雾测试
python scripts/run_eval.py                 # 冒烟集 15 条
```

第 2 增量的四条命令：

```bash
python scripts/build_eval_cases.py                       # 生成 104 条正式评估集
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

可选界面（需 `pip install streamlit`）：`streamlit run src/app.py`

---

## 二、目录结构

```
bishe-agent/
├── .env.example              模型与运行参数模板
├── requirements.txt          基线必需 + 可选增强依赖
├── .github/workflows/
│   └── regression.yml        回归门禁（push/PR 自动跑单测+评估+对比）
├── eval/
│   └── baseline_summary.json 固化进仓库的回归基线（mock，104 题）
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
│   │   ├── cases_eval.jsonl  104 条正式评估集（由脚本生成）
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
| ① 数据层建设 | `db/schema.sql`、`db/core.py`、`schema_retrieval/catalog.py` | 合成数据 + 数据字典；接 UCI 数据集待做 |
| ② Schema 混合检索 | `catalog.py` 关键词 + 别名 + 外键扩展 | 语义向量接口已预留，未接入；消融开关已可用 |
| ③ SQL 生成与三重校验 | `sql/generator.py`、`sql/validator.py` | 语法/结构/语义三层 + AST 白名单已可用 |
| ④ 安全管控 | `validator.py` + `executor.py` 只读连接 | 只读、危险关键字、表/列白名单、超时、LIMIT、日志轨迹；**拦截率 8/8 独立可测** |
| ⑤ Agent 调度与归因 | `agent/runtime.py` | 工具白名单、多步归因、步数上限、单轮兜底已可用 |
| ⑥ 结果呈现 | `presentation/render.py`、`cli.py`、`app.py` | 文本表格 + 图表建议；Streamlit 可选 |
| ⑦ 评估体系 | `evaluation/runner.py`、`ablation.py`、`cases_eval.jsonl` | 104 条正式集、五种判定口径、分层统计、五组消融、回归门禁全部可跑 |

**外部框架基线（开题报告 5.1）尚未实现**：需在独立环境用 LangChain 1.0 `create_agent` / LangGraph 搭一个 SQL Agent，跑同一套 `cases_eval.jsonl`，对比准确率、平均步数、单任务成本。

---

## 四、评估体系、判定口径与消融实验

### 两套测试集

| | 冒烟集 `cases.jsonl` | 正式集 `cases_eval.jsonl` |
|---|---|---|
| 题量 | 15 | **104**（13 语义原型 × 8 口语化变体） |
| 生成方式 | 手写 | `build_eval_cases.py` 生成，改原型即可维护 |
| 用途 | 验证链路、单测回归 | 分层准确率、消融实验、论文图表 |
| mock 下结果 | 15/15 | **35/104（33.7%）**，安全类 8/8 |

正式集的变体刻意改变语序、同义词、数字写法（3 / 三号），测的是「同一语义、不同说法」的稳定性，这是 NL2SQL 的标准评测维度。mock 下掉到 33.7% 是**诚实的基线**，说明规则模板扛不住表述变化。

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
| full | 33.7% | — | 445 |
| no_fk_expansion | 33.7% | +0.00pp | **330** |
| no_retrieval | 33.7% | +0.00pp | **809** |
| no_self_correction | 33.7% | +0.00pp | 445 |
| minimal | 33.7% | +0.00pp | 809 |

上下文字符数的变化证明开关确实作用到了链路上；准确率 delta 全为 0，是因为 **mock 规则模板不读 `schema_context`、也不会生成错误 SQL**，等于绕过了这三个模块。这不是「模块没用」，而是「当前生成器绕过了模块」。

**接真实模型后重跑 `python scripts/run_ablation.py`，那时的 delta 才是论文第七章可用的消融结论。**

### 回归门禁

`.github/workflows/regression.yml` 在 push / PR 时自动：建库 → 9 个单测 → 生成 104 条评估集 → 分层评估 → 与 `eval/baseline_summary.json` 对比 → 任一分类下降超过 2pp 就失败 → 上传报告 artifact。

容差 2pp 是刻意的：合成数据按运行时刻的 `now` 生成，跨时区的「今天/昨天」数据量会略有差异。接真实模型后应收紧。本地手动对比：

```bash
python scripts/run_eval.py --cases src/evaluation/cases_eval.jsonl \
  --output data/reports/ci_summary.json --compare eval/baseline_summary.json
```

---

## 五、接真实大模型（测真实准确率的唯一途径）

```bash
cp .env.example .env
# 编辑 .env：LLM_MODE=real / LLM_API_KEY=sk-... /
#           LLM_BASE_URL=https://api.deepseek.com / LLM_MODEL=deepseek-chat
pip install -r requirements.txt     # 至少要装 openai
python scripts/run_eval.py --cases src/evaluation/cases_eval.jsonl \
       --output data/reports/eval_summary_real.json
python scripts/run_ablation.py      # 重跑消融，这时的 delta 才有意义
```

通义千问把 `LLM_BASE_URL` 换成 `https://dashscope.aliyuncs.com/compatible-mode/v1`；
硅基流动换成 `https://api.siliconflow.cn/v1`。**只改三个参数，代码不动**。

⚠️ 还有一步不能省：当前 `cases_eval.jsonl` 的标准答案 SQL 直接取自 `MOCK_RULES`（见 `build_eval_cases.py` 顶部说明），存在**标准答案同源偏差**。正式测量前应把 `SQL` 字典换成人工标注的标准 SQL，并重新设计判定口径。这一步做完，测出来的数字才能写进论文。

模型接口异常或没装 SDK 时会自动回落 mock 并打印提示，系统永远有输出。

---

## 六、换成 UCI 公开数据集

当前 `db/core.py::seed()` 用固定随机种子生成 5 个教室 × 7 天 × 每 30 分钟的数据（1680 行），
并内置一段剧情：**3 号教室最近两天 CO2 偏高 + 一条 CALIBRATION_DRIFT 告警**，
用于演示「发现异常 → 追加查询 → 定位原因」。

换成真实公开数据集时：
1. 把 CSV 放到 `data/raw/`；
2. 改写 `seed()` 的读数与字段映射（保持 `site / device / sensor_data / alarm` 四张表结构不变）；
3. 重新 `python scripts/init_db.py --force`；
4. 按新数据重标 `build_eval_cases.py` 里的 `SQL` 字典与问题变体。

**其余模块一行都不用改** —— 这就是开题报告里写的数据接入适配器设计。

---

## 七、分阶段开发路线（按备考节奏倒排）

| 阶段 | 时间 | 增量目标 | 验收方式 |
|---|---|---|---|
| 基线 ✅ | 2026.11 | 一句话进、结果出；七个模块骨架；分层评估可跑 | `unittest` 9/9 + 冒烟集 15/15 |
| 第 2 增量 ✅ | 2026.11 | 104 条正式集、五种判定口径、五组消融、回归门禁 | 分层报告 + 消融报告 + CI 通过 |
| 第 3 增量 ⏳ | 2026.12 | **接真实模型 + 人工重标标准 SQL**；接 UCI 数据集；Chroma 语义检索 | 真实准确率数字 + 混合检索对比表 |
| 第 4 增量 | 2027.01 | 重排、语义检索消融、真实模式下的自纠错实验 | 每个模块贡献多少个百分点 |
| 第 5 增量 | 2027.02 | LangGraph 外部基线；FastAPI 包装；Docker 化；Streamlit 界面 | 对比数据表 + 在线 demo |
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

- **mock 模式的准确率没有学术意义**，只验证链路；正式集 33.7% 是规则模板的诚实基线；
- 正式集的标准答案 SQL 与 mock 规则同源，存在自证偏差，真实测量前必须人工重标；
- mock 下消融 delta 全为 0，因为规则模板绕过了检索与自纠模块；
- 目前没有语义层/指标中台（这是**特意不做**的对照设计）；
- 没有行列级权限、多租户、实时连库、流式输出；
- 成本是按字符粗估的，不是真实 Token 计费；
- 归因目前是模板化的「取数 → 查告警 → 给结论」，不是自由规划；
- LangGraph 外部基线、FastAPI、Docker 尚未落地。

---

## 十、常用命令速查

| 目的 | 命令 |
|---|---|
| 重建数据库 | `python scripts/init_db.py --force` |
| 单轮查询 | `python -m src.cli "问题"` |
| 多步归因 | `python -m src.cli "问题" --mode agent --show-trace` |
| JSON 输出 | `python -m src.cli "问题" --json` |
| 强制真实模型 | `python -m src.cli "问题" --llm real` |
| 冒烟评估（15 条） | `python scripts/run_eval.py` |
| 生成正式评估集 | `python scripts/build_eval_cases.py` |
| 正式评估（104 条） | `python scripts/run_eval.py --cases src/evaluation/cases_eval.jsonl --output data/reports/eval_summary_full.json` |
| 回归对比 | `python scripts/run_eval.py --cases src/evaluation/cases_eval.jsonl --output data/reports/ci_summary.json --compare eval/baseline_summary.json` |
| 消融实验 | `python scripts/run_ablation.py` |
| 单测 | `python -m unittest discover -s tests -v` |
| 界面（零依赖，推荐） | `python scripts/serve.py` → 打开 `http://127.0.0.1:8765` |
| 界面（可选） | `streamlit run src/app.py` |

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

页面可以：直接提问（5 个快捷问题）、切换 single/agent 模式、查看生成的 SQL
与执行轨迹、现跑冒烟评估与五组消融、查看 git 提交历史与 diff 统计。
**离线直接双击 `src/web/index.html` 也能看**版本时间线与评估数字快照。

### 版本一览

| 版本 | 日期 | 能用的东西 |
|---|---|---|
| v0.1 | 2026-11-07 | 七模块骨架、CLI、15 条冒烟集、9 个单测 |
| v0.2 | 2026-11-07 | 104 条正式集、五种判定口径、五组消融、回归门禁 |
| **v0.3** | **2026-11-08** | **零依赖浏览器演示、版本可视化、Git 迭代历史** |

查看变化：

```bash
git log --oneline --decorate
git tag -l
git diff v0.2 v0.3 --stat       # 本版新增了哪些文件、改了多少行
git show v0.3 --stat
```

Git 历史从 v0.2 开始（v0.1 时还没建仓库，不伪造历史，其变更以文字记在 CHANGELOG）。
之后每完成一个小步骤都单独提交、单独打 tag，不攒成大版本一起交。
