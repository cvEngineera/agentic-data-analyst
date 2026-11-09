# 版本变更记录

**约定（从 v0.2 起执行）**：每完成一步，必须同时满足四条才算交付——

1. **能运行**：`python -m unittest discover -s tests -v` 与主要脚本全部跑通；
2. **能打开看**：有一个可直接使用的入口（命令行或浏览器页面），不是只改代码；
3. **有记录**：本文件新增一节，写清新增文件、能跑什么、验证数字、已知限制；
4. **能看到变化**：`git commit` + 打 tag，页面 `/api/changes` 与
   `git diff <上一版> <本版> --stat` 都能看到改了什么。

Git 历史从 v0.2 开始记录（v0.1 时期还没建仓库，不伪造历史，其变更以文字记录在本节）。

---

## v0.12 · 2026-11-09 —— 技术栈对齐申报表

**问题**：申报表列了 9 项技术，实际落地的只有 3 项（Python、LLM 接口、UCI 数据集）。
FastAPI 换成了 `http.server`、MySQL 换成了 SQLite、SQLAlchemy 换成了裸 `sqlite3`、
LangChain 换成了手写 urllib、pgvector 换成了自研 TF-IDF、Vue3+ECharts 换成了
原生 JS + 手绘 SVG。答辩时「你说用了 MySQL，代码里为什么是 SQLite」这个问题
没法圆，必须真补。

**做法**：不是把旧的推倒重写，而是**加一层抽象、保留可复现的旧路径**——

| 申报表技术 | 落地位置 | 说明 |
|---|---|---|
| FastAPI | `scripts/api.py` | 7 个接口 + `/docs` 自动文档；`serve.py` 保留为兜底 |
| SQLAlchemy | `src/db/orm.py` | 四个声明式模型、引擎工厂、Inspector 反射 |
| MySQL | `src/db/schema_mysql.sql` + `scripts/setup_mysql.py` | 建库/建表/导数/行数与聚合双重校验 |
| LangChain | `src/llm/langchain_client.py` | `prompt \| ChatOpenAI \| StrOutputParser`，失败回落 urllib |
| pgvector | `src/schema_retrieval/vector_store.py` | HNSW 索引 + `<=>` 检索，连不上退回内存向量 |
| Vue 3 + ECharts | `src/web/index.html` + `src/web/vendor/` | 单文件 Vue 应用，图表组件化，依赖本地引入 |
| Python / LLM / UCI | 沿用 | 不变 |

**关键设计：评估口径不动。** 144 条用例的标准 SQL 全是 SQLite 方言，
一旦因为 `.env` 里 `DB_BACKEND=mysql` 而切库，standard SQL 会整片语法报错，
准确率从 64.6% 掉到个位数——那不是模型退化，是量错了对象。因此
`evaluation/runner.py` 在 `evaluate()` 入口**强制把后端钉回 sqlite**，跑完不恢复
（评估进程独立，不需要恢复）。演示/部署走 MySQL，评估永远走冻结库。

**降级是显式的。** LangChain 缺失 → 回落 urllib；pgvector 连不上 → 内存向量，
且 `search_or_fallback()` 会把来源（`pgvector` / `memory`）返回出来写进轨迹。
之所以强调「显式」，是因为静默降级在答辩时答不出「你怎么证明用了 pgvector」。

**验证**：
- 单测 27/27 仍全绿（executor 改成委托 `db/query.py`，返回契约不变）；
- FastAPI `/api/status`、`/api/schema`、`/api/ask` 实测返回正常，`/docs` 可打开；
- 无头 Chrome 截图确认：Vue 应用正常挂载、表格与 ECharts 柱状图同屏渲染；
- `python -m src.cli` / 冒烟集 / 正式集数字未变（评估口径未动）。

**边界**：
- MySQL 迁移脚本已就绪，但**需要本机 root 密码**才能执行，未在本次提交内跑通；
- pgvector 依赖 Docker 里的 `pgvector/pgvector:pg16`，本机 Docker Desktop 的
  WSL 后端被安全策略拦住，容器未起，代码走的是内存降级路径；
- pgvector 里的向量是字符 n-gram 哈希，不是神经网络 embedding（DeepSeek 无
  embedding 接口），语义泛化能力弱于 BGE，论文「局限性」要如实写。

---

## v0.11 · 2026-11-09 —— 结果自动配图（纯 SVG，零依赖）

**问题**：功能清单写的是「结果以表格和图表展示」，但实际只能出表。`choose_chart()`
判出 bar / line / pie 后，网页端没有任何渲染；`save_chart()` 依赖 matplotlib，
而本机装不上，`--chart` 静默返回空串。等于承诺了配图、实际跑不出图。

**做法**：`AgentResult.to_dict()` 增加 `chart` 字段（调用 `choose_chart`），
网页端 `svgChart()` 按类型手绘 SVG。三处判断共用同一函数，保证命令行、网页、
离线快照画的是同一张图——否则同一问题三处图不一致，答辩解释不清。

- 后端只加一个字段，`asdict` 其余字段与评估脚本用的 `jsonl` 结构完全不动；
- 前端零依赖：不引 ECharts / Chart.js、不装 matplotlib，静态双击页面也能出图；
- 点数超过 24 自动等间隔抽样；柱子超过 8 根自动转横向条形，避免标签重叠；
- `table`（单行结果、拒答）一律不画图——给拒答画图会让人误以为查到了数据。

**验证**：单测 **27/27**（新增 `TestChartSelection` 五组：排名→bar、趋势→line、
单行→table、gate→table、字段不破坏既有键）；node 模拟渲染三种图分别产出
5 根柱、24 个折线点、3 个扇形，边界（table / 单行 / 无数值列）均返回空串；
冒烟 15/15、正式集 mock 23.6% 与 baseline 一致，回归门禁通过；5 个离线快照
重录并带上 `chart` 字段。

**边界**：SVG 是无交互的静态图（没有 tooltip、缩放、图例筛选）；`--chart` 导出
PNG 仍需 matplotlib，本机未装故未验证，网页路径不依赖它。

---

## v0.10 · 2026-11-09 —— 意图闸门：非数据问题不再返回数据表

**问题**：用户实测发现问「你好」「你是谁？」会返回
`SELECT d.ts, d.temperature, d.humidity, d.co2 ... LIMIT 10` 这张无关表。
根因是 `generator.FALLBACK_SQL`（未命中规则时兜底返回最近 10 条）加上
`runtime.run()` 缺少「该不该用 SQL 回答」的判定；real 模式下 `build_prompt`
写死「只输出一条 SELECT」，模型同样被迫编造 SQL。开题报告承诺的
「无法回答时追问澄清，不强行生成错误 SQL」此前只写在文档里，代码未兑现。

**新增**：`src/agent/intent.py`（确定性规则闸门），`runtime.run()` 在连接数据库
之前调用 `classify()`。判定顺序：空输入 → 含数据领域词 / 查询动作词 / 写操作词
**无条件放行** → 显式寒暄 / 身份 / 能力 / 感谢 / 告别 → 其余判为超出范围。
非数据问题返回 `mode=gate`、空 SQL、空结果集、`steps=0`，轨迹记 `intent_gate`，
不连库、不调模型、零成本；页面对这类回答显示「未查库 · 意图闸门」标记。

**两处刻意的设计**：① 用关键词而非模型判意图，避免同一问题有时走闸门有时走
查询导致评估数字抖动；② 数据线索优先级最高，保证「你好，帮我查温度」放行，
且 `删除所有设备记录` 仍走到校验层被拦截，safety 类 8 条评估口径不变。

**改动**：`src/cli.py` 对 gate 模式不再打印「建议图表 table」；
`tests/test_smoke.py` 新增 `TestIntentGate` 六组用例（含逐条检查 144 条评估集
全部放行）；README 第三章新增「步骤 0：意图闸门」并顺手修正 ①② 两行过期描述
（UCI 与语义检索早已落地）；页面徽章、单测数 16→22、时间线、命令速查同步。

**验证**：单测 **22/22**；CLI 问「你好 / 你是谁？/ 讲个笑话」均为 `mode=gate`、
SQL 空、结果空、耗时 0.0 ms；`LLM_MODE=real` 下问「你好」0.03 ms 返回且
**未发起任何网络请求**；冒烟 15/15、正式集 mock 23.6% 与 baseline 完全一致；
5 个离线快照仍是数据链路结果；Web 接口正常。

**已知边界**：规则式闸门只覆盖常见寒暄与明显领域外提问，长尾通用问句可能漏拦；
不替代语义层，也**不改变任何评估数字**。词表若调整，必须跑
`test_all_eval_cases_pass_the_gate`。

---

## v0.9 · 2026-11-09 —— 演示界面改版（深色客户端风格）

**改动**：`src/web/index.html` 重写，`scripts/build_snapshots.py` 简化。

按 Chat2DB 桌面客户端的版式重做演示页：顶部居中标题栏（版本 / 模型模式 / 数据源
三个 chip + 工作台入口）、52px 图标 rail、222px 会话侧栏、主区大留白欢迎页与内嵌
工具栏输入框。配色为深灰底 + 紫色强调色，与截图对齐。

**功能没有减少，只做了重新组织**：

| 原来 | v0.9 |
|---|---|
| 顶部状态栏 | 标题栏 chip + 工作台「数据源与运行状态」 |
| 快捷按钮 + 输入框 + 结果区 | 欢迎页输入框、侧栏快捷示例、**对话消息流**（可折叠 SQL / 表格 / 轨迹） |
| 右栏版本演进、代码变化、评估、消融 | 右上角「工作台」抽屉，全部保留并可离线查看静态数字 |
| 无 | 新增：会话历史（localStorage）、新建对话、搜索、删除、`Ctrl+K` / `Enter` / `Esc` 快捷键 |

**离线快照仍是 5 个**：`build_snapshots.py` 改为只替换 `const SNAPSHOTS = ...` 一行，
不再改写页面代码（离线回退逻辑已内建在页面脚本中），注入锚点 `const $ = id =>` 保留。

**服务端零改动**：`/api/status`、`/api/ask`、`/api/eval`、`/api/ablation`、
`/api/changes` 契约不变，只把 `VERSION` 从 v0.8 提到 v0.9。页面仍是纯标准库 +
单文件 HTML，静态双击可用，不引入任何前端框架或 CDN。

**已知限制**：会话历史只存在当前浏览器 localStorage，换浏览器/清缓存即丢失，
不写服务端、不入库；抽屉里的评估/消融按钮在离线时会如实提示「服务未启动」；
界面未做移动端深度适配（窄屏下侧栏自动隐藏）。

---

## v0.8 · 2026-11-09 —— 可选的联网实时采集（双库隔离）

**新增**：`src/db/live_feed.py`、`scripts/refresh_live.py`。从 Open-Meteo 的
Weather / Air Quality 公开接口（免费、无需 API Key、非商业用途）拉取真实观测，
写入**独立的** `data/db/live.db`。支持
`python scripts/refresh_live.py --loop 3600` 定时自动刷新，默认只跑一次、
不常驻；按 `(device_id, ts)` 唯一索引幂等去重，单点失败不影响整批。

**为什么必须双库隔离**：`bishe.db` 是冻结评估库，144 条评估集、baseline
23.6%、真实模型 64.6% ± 0.7%、消融 −7.64pp 与回归门禁全建立在它上面。
实时数据一旦混入，数字不可复现、注入式已知异常失效、「昨天」类题目随时间
漂移，论文第七章作废。因此 `live.db` 与评估链路完全隔离，页面与评估脚本
默认行为一行未改。

**实测**：一次刷新写入 5 行真实数据（桂林 11.7℃ / 湿度 38% / PM2.5 6.3，
2026-11-09 21:00 UTC+8）；采集前后 `bishe.db` 均为 1680 行、
2026-11-02 16:00 ~ 2026-11-09 15:30，纹丝不动。

**诚实边界（README 6.1 有完整版）**：公开源没有 CO2 / TVOC，两列保持 NULL，
所以 `live.db` **不支持归因演示**；Open-Meteo 是网格化再分析数据，5 个示意
坐标里 4 个数值完全相同，不能冒充「5 间教室的独立传感器」。论文需注明
数据来源与许可、控制调用频率，不要写成「实时物联网监测系统」。

**验证**：单测 16/16（新增 2 个纯函数用例，不联网）、compileall、冒烟 15/15、
正式集 mock 23.6% 与 baseline 一致、回归门禁通过、密钥扫描干净。

---

## v0.7 · 2026-11-09 —— 补做 B 类：重复实验、指标差距、两处承诺

**B-6 多次运行取均值**：新增 `scripts/run_repeats.py`，同一配置 / 同一 144 条
连跑 3 轮 → 63.9% / 64.6% / 65.3%，均值 **64.6% ± 0.7%**（样本标准差，
极差 1.39pp）。**据此修正 v0.6 的主结论**：此前报的 65.3% 是单轮最高值，
论文应引用均值 ± 标准差；消融 full 组 64.6% 正好落在均值上。
数据 `eval/real_repeats.json`，README / 页面主数字已同步。

**B-8 指标差距分析**：README 5.2 给出开题报告 4.2 七个目标与实测的逐项对照
及原因。未达 3 项（单表 75% vs 85%、JOIN 45.8% vs 65%、归因 62.5% vs 70%），
达标 4 项（步数 2.03、响应 1.14s、成本约 0.0007 元/题等），并写明分层口径
与设计目标只是近似对应。开题报告第 113 行已声明「以实测为准、如实报告
未达成项」，不构成验收失败。

**B-7 ① Chroma / 语义检索（真补做）**：本机装不上 `chromadb`（镜像连版本列表
都取不到），改为 `src/schema_retrieval/semantic.py` 的零依赖实现（字符 n-gram
+ TF-IDF 余弦，接口与向量库一致、可替换），接入 `hybrid_score()`，新增开关
`ENABLE_SEMANTIC_RETRIEVAL`（默认关闭，保持 v0.6 数字可比）。
`scripts/compare_semantic.py` 在同一 144 条上跑两组：关键词基线 65.3%、
叠加语义 59.0%，**delta −6.25pp**。**负结果如实保留**：字符级相似度与中文
业务术语别名的强信号冲突，把 `site` 抬到 `sensor_data` 之前导致选表偏移；
这反过来支持「以关键词 + 数据字典为主、向量索引仅作可选接口」的设计。
实验未做超参搜索，列为后续工作。数据 `eval/semantic_compare.json`。

**B-7 ② LangGraph 外部基线（如实说明未实施）**：`langgraph` / `langchain-core`
同样装不上，实验**未实施**。README 5.3 给出可直接抄进论文的措辞：说明依赖
限制、改以 mock 基线与逐层消融为对照、把框架量化比较列为后续工作。
不写成已跑过。

**验证**：单测 14/14（新增语义打分与开关两组）、compileall、冒烟 15/15、
正式集 mock 23.6% 与 baseline 完全一致（证明默认配置未变、v0.6 结论未被推翻）、
回归门禁通过、页面 v0.7、密钥扫描干净。

---

## v0.6 · 2026-11-09 —— 真实模型实测与逐层消融（论文核心数据）

**先修了三处会让数字失真的系统性偏差**（否则实测没有意义）：
1. `evaluation/runner.py` 新增 `_rows_equal()`：结果集比对对数值列按标准 SQL
   的 `ROUND(...,2)` 精度给 0.01 容差。此前 `22.58` 与 `22.575416…` 被判失败，
   10 条小样本里 7 条明明正确的 SQL 被误杀；字符串、行数、列顺序仍严格比较。
2. `build_eval_cases.py` 两条标准 SQL 去掉人为 `LIMIT 50`（`co2_over`、
   `hot_and_humid`），改由 validator 统一补 `row_limit`，避免「模型返回更多
   正确记录」反而被行数判定判错。
3. `generator.build_prompt()` 加全局表清单硬约束，与 validator 白名单对齐。
   此前模型会臆造 `FROM temperature` 这类不存在的表；修完幻觉表名 0 例。

前两条是判定公平性修正，第三条是工程改进，**语义判定没有放宽**。
小样本从修前 1/10 变为修后 10/10。

**真实模型全量评估**（`LLM_MODE=real`，144 题，DeepSeek）：
**94/144 = 65.3%**，同口径 mock 基线 23.6%，**+41.7pp**。
分层：single_filter 75.0%、subquery 100%、safety 100%、attribution 62.5%、
aggregation 50.0%、trend 50.0%、join 45.8%。平均 1136 ms / 题、2.03 步。

**逐层消融**（144 题 × 5 组，真实模型，耗时约 13 分钟）：
full 64.6%；关外键扩展 61.8%（−2.77pp）；关 Schema 检索 56.9%（**−7.64pp**）；
关报错自纠 62.5%（−2.08pp）；minimal 57.6%（−6.94pp）。
Schema 检索贡献最大；minimal 反常地高于「只关检索」0.7pp，原因是关检索后
提示词反而塞进全部表（445→809 字符），论文需如实解释交互效应。

**非确定性实测**：消融 full 组 64.6%（93/144）与单独全量评估 65.3%（94/144）
差 1 题。温度置 0 仍会波动，论文应「多次运行取均值并报告方差」。

**失败分布（50 例）**：38 例结果集列与标准答案不一致（NL2SQL 投影偏差）、
8 例趋势分桶行数差异、3 例归因选错指标、1 例其他；幻觉表名 0 例、兜底触发
1 次、危险语句拦截 8/8。短板集中在多表 JOIN 与聚合。

**归档**：`eval/real_eval_summary.json`、`eval/real_ablation_summary.json`
固化进仓库；逐题明细留在本地 `data/reports/`（被忽略）。回归门禁 baseline
按新口径重新固化为 23.6%。README 5.1 节与页面已同步全部真实数字。

**安全**：全轮只在显式 `LLM_MODE=real` 下向 `api.deepseek.com` 发请求，
Key 不进任何提交、日志、报告或轨迹；提交前用 `git diff --cached` 扫描确认。

---

## v0.5 · 2026-11-09 —— 真实模型零依赖接入与密钥防护

**改动**：模型调用从 `openai` SDK 改为标准库 `urllib` 实现 OpenAI 兼容协议
（`src/sql/generator.py::_real_generate`）；`config.real_mode_available` 不再
要求 SDK 已安装；`.gitignore` 增加 `.env.*` 与 `!.env.example` 白名单；
CLI 的回落提示与 README 第五节同步更新。

**为什么**：本机 pip 大包反复超时，`openai` 一直装不上，「接真实模型」不该
被卡在装包上。这个接口本质只是一次 ``POST /chat/completions``，标准库够用，
也少一处供应链风险。换供应商仍只改 base_url / model / api_key 三项。

**密钥安全**：`.env` 只存本地，被忽略且未跟踪；Key 只放在 Authorization
请求头，只发往 `.env` 里配置的模型厂商域名；不写日志、不写报告、不写执行
轨迹，异常只记录类型名。默认 `LLM_MODE=mock` 完全离线，只有显式指定 real
才发起网络请求。用 `git grep` 与 `git diff --cached` 两次扫描确认历史与
暂存区均无密钥。

**验证**：配置读取确认「密钥已配置 / 真实模式可用 / 当前仍为 mock」；
compileall OK、单测 12/12、冒烟 15/15、正式集 144 条 24.3%、回归门禁通过。
**本轮未发起任何真实模型调用**，保持离线。

**下一步**：确认允许联网调用后，以 `LLM_MODE=real` 重跑 144 条评估与消融，
那时的准确率与 delta 才是论文第七章可用的数字。

---

## v0.4.1 · 2026-11-09 —— 评估口径独立与评估集扩容

**改动**：`scripts/build_eval_cases.py` 移除
`from src.sql.generator import MOCK_RULES`，标准答案改为本文件内
**18 个人工标注的 SQL 常量**（JOIN 片段也独立书写）；语义原型 13 → 18，
正式评估集 104 → **144 条**（达到开题报告承诺的 ≥140 条）；新增双条件筛选、
告警类型双字段 JOIN、按日趋势、Top-N、IN 子查询五类覆盖；
`eval/baseline_summary.json` 按新口径重新固化，README 与页面数字同步。

**解决什么**：v0.2 起标准 SQL 取自 mock 规则表，等于拿被测对象的规则去判
自己的答案，存在**自证偏差**，论文里的准确率站不住。现在真值来源与生成器
完全解耦：生成器怎么改、换成真实大模型、换数据源，标准答案都不变；
正确结果仍由 runner 对同一个库执行标准 SQL 动态取得，不写死数值。

**验证**：15 条独立标准 SQL 全部实测可执行（0 报错）；144 条分层分布
aggregation 32 / single_filter 40 / join 24 / subquery 16 / trend 16 /
attribution 8 / safety 8；正式评估 **35/144 = 24.3%**，安全类 8/8；
五组消融同为 24.3%、delta +0.00pp（mock 预期，上下文字符 445/330/809
证明开关生效）；回归门禁容差 2pp 通过；单测 12/12、compileall、Web 接口全通。

**⚠️ 口径变更**：24.3% 不能与 v0.2–v0.4 的 33.7% 直接比较——分母变大、
标准答案独立，两者不是同一把尺子。真实准确率仍须 `LLM_MODE=real` 测量。

---

## v0.4 · 2026-11-09 —— UCI 公开数据集接入

**新增**：`src/db/uci_loader.py`、`scripts/check_uci.py`；
`init_db.py --source synthetic|uci`；`data/db/source.json` 数据源元信息；
页面状态栏显示当前数据源（悬停可见数据集全称与预处理说明）。

**解决什么**：v0.1–v0.3 只有固定种子的合成数据，链路能跑但论文「数据来源」
一章经不起追问。本版接入 UCI Occupancy Detection（id=357，20560 条真实
测量记录），**保持四张表结构、维表与告警剧情不变**，验证开题报告里
「数据接入适配器」不是空话：换数据源后 Schema 检索、SQL 生成、Agent 归因、
安全管控一行未改。

**诚实性约束（论文需照抄）**：表头缺一列，按位置解析；原始 2015 年时间
平移到最近 7 天；单房间数据轮转分配到 5 个虚拟教室；3 号教室注入式
已知异常用于给归因链路提供客观答案；UCI 无 tvoc/pm25，两列保持 NULL，
不做推算或编造。

**验证**：`python scripts/check_uci.py` **7/7 通过**——数据规模（1680 行 /
原始 20560 条）、真实值范围（温度 19.1~24.29℃，CO2 413~2248ppm）、
NULL 列 1680/1680、单轮查询、三表 JOIN、危险语句拦截、多步归因命中校准；
报告落在 `data/reports/uci_check.json`。脚本用 finally 保证跑完自动恢复
synthetic 数据源，不让后续评估跑错口径。单测 12/12、compileall、Web 接口全通。

**边界**：正式评估集与回归门禁仍基于 synthetic；UCI 模式只验证链路、
不统计准确率，避免用「跳过不适用的题」把数字做好看。

---

## v0.3.3 · 2026-11-09 —— 项目标识统一

**改动**：本地目录 `bishe-agent/` 重命名为 `agentic-data-analyst/`，与云端仓库名、
remote 地址保持一致；同步更新 README 的运行命令、`start_demo.bat` 提示、
`tests/test_smoke.py` 运行说明、`src/config.py` 两处注释中的旧目录名。

**为什么**：`bishe` 是拼音缩写，作为仓库与目录名不利于简历阅读；新名同时覆盖
agentic / data / analyst 三个关键词，且不把项目绑定到物联网硬件方向。

**验证**：`git rev-parse` 指向新路径，`remote get-url` 仍为 443 备用通道的新地址，
4 次提交与 4 个 tag 完好；`compileall`、单测 9/9、冒烟评估 15/15、
CLI 归因、Web 六接口全部复跑通过，工作区干净。

**说明**：这是标识与文档调整，不含功能变化；`.git/` 随目录整体搬迁，历史与标签未改写。
下一功能版本仍为 v0.4（UCI 真实数据接入 + 独立标注标准 SQL）。

---

## v0.3.2 · 2026-11-08 —— 代码变化自动跟随最新版本

**改动**：`scripts/serve.py::_changes()` 不再把 diff 写死成 `v0.2..v0.3`，
改为按版本排序取最近两个 tag 自动对比；页面「代码变化」区显示动态的
`prev → cur` 标签。**验证**：`v0.3.1 → v0.3.2` diff 自动出现，
`git status` 干净，单测 9/9，六接口全通过。

意义：以后每打一个新 tag，刷新页面就能看到最新一步改了哪些文件，不用改页面代码。

---

## v0.3.1 · 2026-11-08 —— 离线也能点

**新增**：`scripts/build_snapshots.py`（幂等构建脚本）。

**解决什么**：v0.3 的页面必须启动服务才能提问，静态打开只能看不能点。
本版把 5 个快捷问题的**真实运行结果**（结论、SQL、结果集、执行轨迹）
内嵌进 `index.html`，离线打开时自动回退到快照并明确标注「预录结果」；
启动服务后实时结果优先，快照不参与。这样「每步都能打开看」在离线状态下也成立。

**验证**：快照 5 条（单轮 / 多步归因 / 三表 JOIN / 安全拦截 / 分组聚合），
复跑脚本结构不重复插入；HTTP 六个接口仍全通过；单测 9/9；compileall OK。

**约定补充**：以后改了运行时逻辑，重跑 `python scripts/build_snapshots.py`
刷新快照，保证页面与代码一致，再提交新版本。

---

## v0.3 · 2026-11-08 —— 可交互版本与版本可视化

**新增文件**

| 文件 | 作用 |
|---|---|
| `scripts/serve.py` | 零第三方依赖的本地 HTTP 演示服务（标准库 `http.server`） |
| `src/web/index.html` | 自包含单页：提问、结果、SQL、执行轨迹、版本演进、评估数字、代码变化 |
| `src/web/__init__.py` | 静态资源包说明 |
| `start_demo.bat` | Windows 双击启动（纯 ASCII，避免编码问题） |
| `CHANGELOG.md` | 本文件 |
| `.git/` + tag `v0.3` | 版本历史，可 `git diff v0.2 v0.3 --stat` |

**现在能做什么**

- 双击 `start_demo.bat` 或运行 `python scripts/serve.py`，浏览器打开
  `http://127.0.0.1:8765`，直接提问、切换 single/agent 模式、看生成的 SQL
  与完整执行轨迹；
- 页面上有 5 个 `/api/*` 接口：状态、提问、冒烟评估、五组消融、git 变化；
- 页面**离线打开也能看**版本时间线、当前评估数字与消融快照（fetch 失败自动降级）；
- `git tag` / `git log` / `git diff` 可以随时看代码一步步怎么变的。

**为什么不用 Streamlit**：它是可选增强，本机 pip 装大包一直超时。
为了保证「每步都有能打开的版本」，改用标准库 HTTP 层，零依赖、任何环境都能起。
`src/app.py` 的 Streamlit 版本仍然保留，装上依赖后也能用。

**验证**：单测 9/9、CLI 归因正常、HTTP 六个接口逐个 curl 通过、
`git diff v0.2 v0.3 --stat` 能看到本版新增文件。

**已知限制**：只监听 127.0.0.1；无鉴权、无并发优化；页面是演示级，
不替代论文里的正式实验环境；数据与评估仍为 mock 模式。

---

## v0.2 · 2026-11-07 —— 分层评估、消融实验与回归门禁

**新增**

- `scripts/build_eval_cases.py` 生成 `src/evaluation/cases_eval.jsonl`：
  13 语义原型 × 8 口语化变体 = **104 条**正式评估集；
- `runner.py` 支持五种判定口径：`result` / `row_count` / `scalar_tolerance` /
  `deny` / `deny_sql` / `conclusion`；安全类改为直接考校验层；
- `config.py` 三个消融开关 + `src/evaluation/ablation.py` 五组配置 +
  `scripts/run_ablation.py`；跑完自动还原配置；
- `.github/workflows/regression.yml` 回归门禁 + `eval/baseline_summary.json` 固化基线；
- 所有源码逐模块补齐详细注释。

**验证数字**：冒烟集 15/15；正式集 35/104（33.7%）；安全拦截 8/8；
消融 5 组 delta 全 +0.00pp（mock 模板绕过模块），上下文字符 445/330/809 证明开关生效；
回归门禁本地模拟通过；单测 9/9。

**已知限制**：mock 下数字不能写进论文；正式集标准答案 SQL 与 `MOCK_RULES` 同源，
真实测量前必须人工重标；`openai` SDK 本机安装超时，real 模式待测。

---

## v0.1 · 2026-11-07 —— 基线（Git 建立之前，文字记录）

**新增**：七个模块骨架 + CLI + 可选 Streamlit 界面。

| 模块 | 落地 |
|---|---|
| ① 数据层 | 四表 DDL、索引、视图；固定种子合成数据 1680 行；数据字典 |
| ② Schema 检索 | 中文别名打分 + 外键一跳扩展；`hybrid_score()` 预留向量接口 |
| ③ SQL 生成与校验 | mock/real 双模式；sqlglot AST + 表列白名单；报错回喂自纠 |
| ④ 安全管控 | 只读连接、危险关键字、超时、LIMIT、审计轨迹 |
| ⑤ Agent 运行时 | 自研：工具白名单、多步归因、步数上限、单轮兜底 |
| ⑥ 结果呈现 | 文本表格、图表类型建议、显示生成的 SQL |
| ⑦ 评估体系 | 15 条冒烟集、分层统计、`--compare` 回归对比 |

**验证**：`unittest` 9/9；冒烟集 15/15；归因能命中 CALIBRATION_DRIFT；
危险语句被拦截。`python -m src.cli "..."` 一句话进、结果出。

---

## 下一版计划（v0.4，第 3 增量）

- 接真实模型（`LLM_MODE=real`），需要用户提供 API Key 并装好 `openai`；
- **人工重标** `build_eval_cases.py` 的 `SQL` 字典，消除自证偏差；
- 接 UCI 公开数据集（只改 `db/core.py::seed()`）；
- Chroma / pgvector 语义检索接入 `hybrid_score()`，跑真实消融；
- 每完成其中一项就单独打一个版本，不攒到大版本一起交。
