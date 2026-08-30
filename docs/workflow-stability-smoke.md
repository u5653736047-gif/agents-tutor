# 双工作流多学科稳定性冒烟报告 — workflow-stability-smoke

> 状态：**已完成并闭环** 2026-08-30
> 目的：两个固定工作流（`lesson_plan` / `ppt_slides`）的多学科功能性验证 + 稳定性观察。
> 环境：真实模型 deepseek-v4-flash（api.deepseek.com），`API_WORKFLOW_MODE=auto`、`API_OFFICECLI_ENABLED=1`，本机 Windows，officecli 1.0.145。
> 用例原则：每条用例锁定一个**学科内容类型**或一个**参数边界/主题路径**；产物逐条经 officecli 机械校验（docx 段落数 / pptx 页数精确相等 + validate），工作流状态逐条核对（completed、零审批、步骤 attempts、review verdict）。

## 一、测试矩阵

### 教案 workflow（lesson_plan：collect → draft → generate → review）

| # | 学科 | 课题 | 年级 | 覆盖点 |
|---|---|---|---|---|
| L1 | 数学 | 《勾股定理》 | 八年级 | 理科公式推导类；默认参数 |
| L2 | 语文 | 《背影》 | 八年级 | 人文文本细读类 |
| L3 | 化学 | 《质量守恒定律》 | 九年级 | 实验探究类 |
| L4 | 英语 | 《一般过去时》 | 七年级 | 外语技能类 |

### 课件 workflow（ppt_slides：collect → outline → generate → review）

| # | 学科 | 课题 | 参数 | 覆盖点 |
|---|---|---|---|---|
| P1 | 物理 | 《牛顿第二定律》 | 高一 · 16 页 · 学术风 | 页数硬上界 16 + academic 关键词命中 |
| P2 | 历史 | 《丝绸之路》 | 七年级 · 12 页 · 教育风 | 文科叙事类 + edu 显式命中 |
| P3 | 地理 | 《水循环》 | 高一 · 10 页 · 无风格 | 页数硬下界 10 + 缺省主题路径 |
| P4 | 数学 | 《二次函数的图像与性质》 | 九年级 · 不说页数 · 学术风 | page_count 缺省规整(→12) + academic |

## 二、逐条结果

所有运行均为真实模型（deepseek-v4-flash）端到端：HTTP 200、零人工审批、产物经 officecli 机械校验。

### 教案矩阵

| # | 结果 | review | 产物校验（docx） | 耗时 |
|---|---|---|---|---|
| L1 勾股定理（数学） | ✅ completed（修复前跑，review 未触顶） | pass | 56 段落 / 2,422 字符 / validate 0 错 | 101s |
| L2 背影（语文） | ❌→✅ 首跑冻结于 review（见 §三缺陷），修复后重跑 completed | pass（v2） | 67 段落 / 2,960 字符 / validate 0 错 | 111s（v2） |
| L3 质量守恒定律（化学） | ❌→✅ 同上 | pass（v2） | 65 段落 / 2,570 字符 / validate 0 错 | 145s（v2） |
| L4 一般过去时（英语） | ❌→✅ 同上 | pass（v2） | 83 段落 / 3,849 字符 / validate 0 错 | 121s（v2） |

注：L1 collect 阶段知识库未命中《勾股定理》教材原文，工作流按设计优雅降级（模型以自身学科知识继续成稿），非缺陷。

### 课件矩阵

| # | 结果 | review | 页数（请求/实际） | 主题路径 | 耗时 |
|---|---|---|---|---|---|
| P1 牛顿第二定律（物理·16页·学术风） | ✅ completed | pass | 16 / **16**（精确命中硬上界） | academic | 134s |
| P2 丝绸之路（历史·12页·教育风） | ✅ completed | pass | 12 / 12 | edu（显式关键词） | 165s |
| P3 水循环（地理·10页·无风格） | ✅ completed | pass | 10 / **10**（精确命中硬下界） | edu（缺省默认路径） | 196s |
| P4 二次函数（数学·不说页数·学术风） | ✅ completed | pass | 缺省 12 / 13（容差内） | academic | 162s |

全部 pptx：页数与计划精确相等（导出自验闸）、validate 0 错误、封面截图核对主题与 style_hint 对应（P2 教育青 / P4 学术藏蓝）。

## 三、稳定性观察

### 发现并修复的真实缺陷（本报告核心产出）

**缺陷**：固定工作流 Worker 触发 `react_iteration_limit` / `model_call_failed` 时整轮图被提前判死，步骤永远停在 RUNNING，`on_failure` 策略（retry/continue）从未执行——工作流永久冻结，用户侧表现为「一直运行中」。教案矩阵首跑 4 条命中 3 条（L2/L3/L4 全部冻结在 review 步，events 以 `react_iteration_limit` 收尾）；此即 lesson-workflow 遗留问题 3 的真实根因（当时猜测为 model_call_failed 流终止，实为 Worker 错误路由缺口）。

**根因**（`graph_builder.py` Worker 轮簿记）：`fail(result.error)` 早退闸在 `_workflow_worker_updates` 之前执行；TaskPlan Worker 对这两类错误有显式豁免（可恢复、由调度器再分派），固定工作流 Worker 不在豁免范围。而 `_workflow_worker_updates` 本身已具备完善的错误处理（落 FAILED → 调度节点执行 on_failure），只是永远执行不到。

**修复**（两点）：
1. 豁免范围扩展到「工作流当前步骤 Worker」（新谓词 `_workflow_current_step_worker`，合法入口条件与 `_workflow_worker_updates` 一致：RUNNING/PAUSED_APPROVAL）：错误交工作流簿记落 FAILED，调度节点按策略处置——review=continue → SKIPPED 照常收口；collect/draft/generate=retry → 有界重试。回归锁：`test_workflow_orchestration.py` 新增 3 用例（review 冻结→SKIPPED 收口 / retry 步重分派 / 豁免范围不放大）。
2. 两工作流 review 步迭代预算放宽 5/4 → 8（硬帽 12 内）：review 是读重步骤，officecli_inspect 多项检视 + 结论一轮，4~5 次预算被真实冒烟证实必触顶——修复后重跑的 L2v2/L3v2/L4v2 review 全部**真跑完并给出 pass verdict**（而非靠 SKIPPED 兜底），预算放宽是让 review 名副其实的主因，路由修复是安全网。

### 稳定性指标（修复后 7 条运行：L1 + L2v2/L3v2/L4v2 + P1~P4）

- **步骤 attempts 全部 = 1**：零重试、零预算告警、零熔断；
- **审批次数全部 = 0**：产物区自动授权全程无人工介入；
- **耗时分布**：教案 101~145s、课件 134~196s，稳定带宽内（无超时/挂起）；
- **产物机械校验 100% 通过**：docx 段落 56~83、字符 2.4K~3.8K；pptx 页数与计划精确相等、validate 0 错误；
- **参数边界行为正确**：页数硬上界 16 / 硬下界 10 精确命中；page_count 缺省规整为 12（实际 13，±2 容差内）；
- **主题选择路径全部正确**：显式教育风/学术风关键词命中、缺省走默认主题，回执 template 字段与产物视觉一致。

## 四、结论

1. 两个工作流在 8 条多学科用例上功能性全部达标：状态机、参数管道、结构门禁、落盘闸、模板主题化、降级语义均按设计工作；
2. 稳定性冒烟揪出并修复了一个会让工作流永久冻结的真实缺陷（Worker 可恢复错误被提前判死），修复带 3 个回归测试，全量 1319 用例全绿；
3. kimi 双引擎方案（`docs/ppt-kimi-integration-research.md` 方案 C）经用户决策**不再考虑**，模板主题化路线即 PPT 视觉能力的最终形态。

## 四、结论

（运行后回填）
