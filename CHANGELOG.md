# 版本变更记录

**约定（从 v0.2 起执行）**：每完成一步，必须同时满足四条才算交付——

1. **能运行**：`python -m unittest discover -s tests -v` 与主要脚本全部跑通；
2. **能打开看**：有一个可直接使用的入口（命令行或浏览器页面），不是只改代码；
3. **有记录**：本文件新增一节，写清新增文件、能跑什么、验证数字、已知限制；
4. **能看到变化**：`git commit` + 打 tag，页面 `/api/changes` 与
   `git diff <上一版> <本版> --stat` 都能看到改了什么。

Git 历史从 v0.2 开始记录（v0.1 时期还没建仓库，不伪造历史，其变更以文字记录在本节）。

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
