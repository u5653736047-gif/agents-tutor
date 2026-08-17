# assistant-ui 接入性能验收记录（T17 量化表）

测量环境：`npm run build` 生产构建 + `next start`（127.0.0.1:3000）、真实后端
（127.0.0.1:8000）、Chrome headless。Lighthouse 报告原始 JSON 见同目录
`lighthouse-desktop.json`。

| 指标 | 目标（计划） | 实测 | 结论 |
| --- | --- | --- | --- |
| 首屏 bundle 增量 | ≤120 KB gz | ≈0（主 chunk 196.1→190.8 KB gz，反向变小；assistant-ui 全量在动态 async chunk） | ✅ |
| 转换器 1000 事件 | <16ms | 1.45ms（单测断言） | ✅ |
| Markdown 分块扫描 | 亚毫秒/次 | <2ms（5000 token 级 50 次均值，单测断言） | ✅ |
| 桥层通知频率 | 60 events/s 输入下 ≤30/s | ≤31/s（领先+尾沿合并，单测断言） | ✅ |
| 长会话 DOM 行数 | 120 条消息有界 | e2e 断言渲染行数 <60（视口+overscan 有界），滚动到底无丢消息 | ✅ |
| 流式 long task（~5000 token 参考规模） | 无 >200ms | 生产构建实测 16K 字符回答：1 个 long task（80ms），0 个 >200ms | ✅ |
| 流式 long task（极端规模） | — | 49K 字符数学长文：484 个 long task 中 24 个 >200ms（KaTeX 大块收尾渲染；dev 模式同场景 10 倍劣化）。登记为后续优化（KaTeX 延迟渲染） | ⚠ 已登记 |
| Lighthouse 桌面性能分 | ≥90 | **99**（preset=desktop；FCP 242ms / LCP 779ms / TBT 79ms） | ✅ |
| Lighthouse CLS | ≈0 | **0** | ✅ |

备注：
- Lighthouse 移动 preset（4x CPU 节流 + slow 4G）得分 79，非计划口径（计划明确为
  「桌面性能分」），仅作参考记录。
- 上翻暂停（auto-scroll pause）：流式中手动上翻后 scrollTop 保持 0 不被拉回，
  2.5s 复测仍为 0（浏览器实测）。
- 附件端到端：原生 Composer 与旧 ChatInput 行为逐项等价（发送时上传 → 回执 →
  流式请求携带）；历史消息不回显附件是后端 D7-T3 已登记的预留限制
  （`api/sessions.py::_public_message` 显式 `attachments=None`，两条路径一致）。
