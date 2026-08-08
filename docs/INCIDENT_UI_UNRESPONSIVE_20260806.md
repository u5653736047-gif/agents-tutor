# 前端页面按钮全部失灵问题——排查交接文档

> 记录时间：2026-08-06 17:4x
> 记录人：opencode 会话（deepseek-v4-flash）
> 目的：将本次会话的排查过程、证据、已排除项与待办方向完整交接给后续处理人。

---

## 一、背景

为验收「多智能体助教助学系统」（`D:\CODE\Agents`，FastAPI 后端 + Next.js 前端），按 README 流程启动双端服务。验收过程中用户浏览器出现前端交互问题，本轮会话聚焦排查。

### 当前服务状态（排查结束时）

| 服务 | 端口 | 进程 | 状态 |
| --- | --- | --- | --- |
| FastAPI 后端（uvicorn） | 127.0.0.1:8000 | python 18268/34504（15:08 启动） | 正常，healthz OK |
| Next.js dev server | 127.0.0.1:3000 | node（17:3x 干净重启后） | 正常，HTTP 200 |

- 后端日志：`%TEMP%\opencode\api.log`（access log）、`%TEMP%\opencode\api.err.log`
- 前端日志：`%TEMP%\opencode\web.log`、`%TEMP%\opencode\web.err.log`
- 两个服务均为 `Start-Process -WindowStyle Hidden` 启动的后台进程

### 启动过程的插曲（重要时间线）

1. `scripts/start-stage3.ps1` 启动失败（隐藏窗口无日志，原因未查明，改用手工启动）
2. **15:01** 手工启动 uvicorn #1 —— `.env` 解析 bug：变量名与 `=` 之间有空格（`DEEPSEEK_API_KEY = sk-...`），我用正则 `^DEEPSEEK_API_KEY=` 没匹配上，**API Key 未传入** → 15:04–15:08 期间后端能启动但模型调用全部 `model_call_failed`
3. **15:04** 启动 Next dev
4. **15:08** 修复 .env 解析后杀掉旧 uvicorn 重启后端（18268）→ 此后模型调用正常（实测 SSE 流式问答完整跑通）
5. **15:3x** 完成 SSE 流式、审批续跑、知识引用等后端验收
6. **17:3x** 干净重启前端（清 `.next/dev` 缓存）

**注意：用户浏览器很可能正是在 15:04–15:38 这段多次重启/故障窗口打开的页面。**

---

## 二、用户报告的症状（原始描述）

1. 初次报告：「显示网络连接出现了问题，连接不到网络」；已确认网络环境无问题
2. 补充：http://127.0.0.1:3000 和 http://localhost:3000 两个地址都试过，都显示网络连接失败；代理已退出
3. **关键补充（改变排查方向）：点击任何按钮都没有反应（包括「网络失败」提示下方的重试按钮），只有切换黑白主题的按钮有效；点击按钮后 Network 面板不会出现任何请求**

---

## 三、已完成的排查与证据（全部实测）

### 3.1 已确认正常的部分（可复现验证）

| 检查项 | 结果 |
| --- | --- |
| 后端 `/healthz` | 200，`mode=hybrid` + FastEmbedProvider（512 维），语义检索在线 |
| `POST /chat` 同步问答 | 正常（含 handoff 审批断点 → confirm 续跑 → 助学 Agent 检索作答带教材引用） |
| `POST /chat/stream` SSE 流式 | curl 实测完整事件流：thinking→tool_call→tool_result→message_end→done |
| CORS | 已配置 `http://localhost:3000` + `http://127.0.0.1:3000`，预检 OPTIONS 实测 200 |
| 前端 SSR 健康检查 | 页面 HTML 实测含 `apiConnected:true`（徽标应为绿） |
| 客户端 bundle 中 API 地址 | 回退正确：`http://127.0.0.1:8000` |
| DeepSeek 外网连通 | 可达（带 key 直连 200，带错 key 401，非网络问题） |
| 系统代理 / WinHTTP | 均关闭；绕过列表含 `127.*`；无 TUN 网卡、无代理进程 |
| Chrome/Edge 注册表策略 | 无代理强制策略（仅无障碍设置） |
| Windows 防火墙 | python/node 入站规则为 Allow |
| Playwright 加载页面 | 页面 1088ms 加载，无 pageerror、无 console 错误（仅 1 个 404 资源，非 chunk） |
| 机器身份 | 实体机（Lenovo SOLDIER），局域网 192.168.8.122，装有 VMware（VMnet1/VMnet8 虚拟网卡） |

### 3.2 关键异常证据（api.log 全量 37 条请求时序）

- 用户浏览器的请求**确实到达过后端**：`OPTIONS /sessions`、`GET /sessions`、`GET /stats/overview`、`GET /openapi.json` 全部 200 —— 说明浏览器→后端链路曾通
- 但 `POST /chat/stream` **从未到达过后端**（仅 1 次 OPTIONS 预检 200，之后无任何 POST）—— 浏览器发出的流式聊天请求在网络层丢失
- `/healthz` 被轮询约 20 次（对应页面反复刷新 + 前端断线自愈轮询）
- 15 秒观察窗口内零请求增长 —— 无活跃浏览器连接

### 3.3 前端代码层排查（已排除）

- `app/page.tsx` SSR 健康检查：`apiConnected:true`（实测）
- `lib/api-client.ts` / `lib/api-base-url.ts`：地址解析正确，无问题
- `lib/stream-client.ts`：SSE 解析逻辑无 bug；网络错误抛 `ApiClientError(code=null)` → 界面显示「网络请求失败：请检查网络连接后重试」+ 重试按钮（与用户描述吻合，说明**该弹窗是客户端渲染的，即用户浏览器当时已完成 React 水合、store 有状态**）
- `stores/chat-store.ts`：无 persist/localStorage；`isStreaming` 有 finally 兜底复位，**不存在卡死状态**
- `conversation-panel.tsx` 错误块：内联渲染，**无全屏遮罩/对话框**，不阻塞其它按钮
- 水合警告（web.err.log）：仅主题按钮 aria-label 的 SSR/客户端 mismatch（next-themes 已知良性现象），**不是按钮失灵原因**

---

## 四、症状指向的结论

> **服务端、前端 SSR、网络链路全部健康（本机 curl/Playwright 均复现正常）。「所有按钮无响应且不发请求、仅主题切换有效」是浏览器侧 JS 交互层问题。**

按可能性排序的待查方向：

1. **用户浏览器标签页为陈旧状态**（最可能）：页面在 15:04–15:38 服务重启/故障窗口打开，普通刷新未能重建 JS 状态（已做前端干净重启 + 要求 Ctrl+Shift+R 强刷，用户反馈仍不行 → 此项未完全排除，需确认强刷方式/是否清缓存）
2. **浏览器扩展/安全软件干扰**（React 官方文档明确指出扩展修改 DOM 可导致水合失败→事件失效）：需用无痕模式（禁用扩展）验证
3. **浏览器侧代理残留**（系统级已排除，但浏览器自身代理/扩展级代理未验证）
4. **用户浏览器与运行服务的机器不一致**（如远程访问/VM 内浏览器）：需确认用户浏览器所在机器

## 五、下一步需要的信息/动作（交给后续处理人）

1. **（最关键）请用户打开 F12 → Console 面板，点击任意按钮，把红色报错完整文本发来** —— 直接定位 JS 崩溃点
2. 请用户用**无痕模式**（Ctrl+Shift+N）访问 http://127.0.0.1:3000 验证（排除扩展）
3. 请用户**换一个浏览器**（Chrome↔Edge）验证（排除浏览器设置）
4. 确认用户浏览器与服务的机器是否为同一台
5. 若仍失败：F12 → Network 面板，点发送按钮，查看 `chat/stream` 请求的状态（failed/canceled/pending）与错误码（ERR_CONNECTION_REFUSED / ERR_PROXY_CONNECTION_FAILED / ERR_BLOCKED_BY_CLIENT / ERR_FAILED）
6. 若怀疑后端：本机 `curl http://127.0.0.1:8000/healthz` 与 `curl http://127.0.0.1:3000` 应均返回 200

## 六、服务重启方法（供后续使用）

```powershell
# 后端（先在 PowerShell 设置好环境变量，注意 .env 里变量名与 = 之间有空格）
$env:PYTHONPATH = 'D:\CODE\Agents\backend\src'
Get-Content D:\CODE\Agents\.env | ForEach-Object {
  $line = $_.Trim()
  if ($line -and -not $line.StartsWith('#') -and $line.Contains('=')) {
    $sep = $line.IndexOf('=')
    $name = $line.Substring(0,$sep).Trim()
    if ($name -match '^DEEPSEEK_(MODEL|BASE_URL|API_KEY)$') {
      Set-Item "Env:$name" ($line.Substring($sep+1).Trim().Trim('"').Trim("'"))
    }
  }
}
Start-Process 'D:\CODE\Agents\backend\.venv\Scripts\python.exe' -ArgumentList '-m','uvicorn','api.app:create_app','--factory','--host','127.0.0.1','--port','8000' -WorkingDirectory 'D:\CODE\Agents\backend' -WindowStyle Hidden

# 前端
$env:NEXT_PUBLIC_API_BASE_URL = 'http://127.0.0.1:8000'
Start-Process 'node.exe' -ArgumentList 'node_modules\next\dist\bin\next','dev','--hostname','127.0.0.1','--port','3000' -WorkingDirectory 'D:\CODE\Agents\frontend' -WindowStyle Hidden

# 停止
Get-Process -Name python,node -ErrorAction SilentlyContinue | Stop-Process -Force
```

## 七、附：后端验收已通过的证据（与本次问题无关，但可证明系统本身可用）

- 会话创建 → Supervisor 意图识别（detect_intent）→ 尝试 search_knowledge 被权限守卫拦截（tool_unauthorized，设计如此，检索仅授权助学/助教）→ handoff 转助学 Agent 进入审批断点
- `POST /sessions/{id}/handoff` confirm → agent_switch 到 learning_assistant → 检索作答（引用《深度学习》第 9 章、邱锡鹏《神经网络与深度学习》第 5 章、LeCun 论文）→ 交回 Supervisor → done
- 全部事件流正常：thinking / tool_call / tool_result / agent_switch / message_end / done

---

## 八、根因确认（2026-08-07 补充，本交接文档的最终结论）

### 8.1 结论一句话

> **「前端页面按钮全部失灵」是 Next.js 16.2.x dev 模式的框架级回归 bug（vercel/next.js #96294），不是本项目代码、浏览器扩展、网络或后端问题。切换到生产模式（`next build && next start`）后一切正常（实测）。**

### 8.2 决定性证据链（全部实测）

1. **干净 Playwright Chromium 149（无扩展）加载 dev 页面可稳定复现**：`__REACT_DEVTOOLS_GLOBAL_HOOK__.renderers.size === 0`（React 客户端 JS 已加载、但 `hydrateRoot` 从未被调用），90 秒无变化，无任何 console 报错。用户真实浏览器（Chrome/Edge、含/不含扩展、无痕模式）现象一致。
2. **代码级定位**：Next 客户端入口 `app-index.js` 的 `hydrate()` 卡死在 `await initialServerResponse`——用 `addInitScript` 插桩实测：内联 flight 数据（`self.__next_f` 5 段共 8.3KB）完整入流、`ReadableStream` 正常 `close()`，但 RSC payload 解析永不完成（root chunk 依赖的 client module 永远 pending）。
3. **对照实验**：同一浏览器、同一后端，`next build && next start`（生产模式）后：按钮存在 React 内部属性（`__reactFiber$`/`__reactProps$`）、页面加载 376ms 即发出 `GET /sessions`（200）、点击「新建会话」发出 `POST /sessions`（201）、会话列表与对话区正常渲染。
4. **已知 issue 完全吻合**：vercel/next.js #96294「Dev server never hydrates in 16.2.x: initial RSC payload root chunk stays blocked forever (regression from 16.1.7)」——16.2.9 / 16.2.12 / 16.3.0-preview.9 全坏；16.1.7 与 15.x 正常；`next dev --webpack` 同样无效；**仅 dev 模式坏，生产模式正常**；OS/浏览器无关（macOS/Linux/Chromium/WebKit/Firefox 均复现）。该 issue 无官方修复，已被 bot 关闭。
5. 此前所有「网络失败」弹窗 = dev 页面早期（水合尚正常的窗口）请求失败留下的 `requestError` 状态；「点击无 Network 输出」= 未水合页面上 React 事件根本不存在；「仅知识库链接有效」= 原生 `<a>` 导航不依赖 JS；「仅主题按钮有效」= next-themes 内联脚本（hydration 前执行）使外观随系统变化造成的误判。

### 8.3 已排除项（两轮四路子代理 + 逐文件复核）

- 前端组件层（事件绑定、store 状态机、覆盖层/z-index/pointer-events/disabled 悬挂、水合 mismatch）——排除
- 后端（CORS 配置、/chat/stream 路由、中间件、uvicorn 日志链路）——排除
- 浏览器扩展（沉浸式翻译等）——排除为根因（干净 Chromium 同样复现）
- 网络/代理/缓存/Service Worker——排除（chunk 全 200、flight 数据完整）

### 8.6 【2026-08-07 第二次排查】真正的代码 bug：fetch 调用 this 绑定错误（已修复）

> 上文的「prod 模式正常」结论在深入验证后被修正：**prod 模式下存在本项目的前端代码 bug，导致所有 API 请求静默失败**。已修复并验证。

**现象（干净浏览器 100% 复现）**：页面水合正常（按钮有 React 属性、Link prefetch 发出），但 `GET /sessions`、`POST /sessions` 等**所有应用请求都未发出**，界面显示「网络请求失败」错误块；手动在 Console 执行同 URL fetch 却正常。

**根因**：`lib/api-client.ts` 的 `request()` 以**成员访问方式**调用 fetch：

```ts
response = await config.fetchImpl(`${config.baseUrl}${path}`, { ... });
```

浏览器原生 `fetch` 是 WebIDL 方法，要求 `this` 为 `window`/`undefined`；成员调用把 `this` 绑定为 `config` 对象，**同步抛 `TypeError: Failed to execute 'fetch' on 'Window': Illegal invocation`** → 被 `request()` 的 catch 归一为 `ApiClientError(code=null)` → UI 显示「网络请求失败」，且请求从未发出（Network 面板无任何记录）。

**实测证据**（Playwright 干净 Chromium，prod 页面内）：
- `const f = fetch; f(url)`（this=undefined）→ `ok 200`
- `fetch.call({}, url)`（this=对象，即应用的实际调用方式）→ `ERR TypeError: Illegal invocation`
- 用 `addInitScript` 把 `window.fetch` 替换为普通 JS 函数后，应用全部请求恢复正常（这正是「带沉浸式翻译扩展的浏览器反而正常」的原因——扩展 hook 了 `window.fetch` 为普通包装函数，碰巧掩盖了此 bug）

**修复**（`lib/api-client.ts:195` 附近）：解构后再调用，`this` 为 `undefined`：

```ts
const fetchImpl = config.fetchImpl;
response = await fetchImpl(`${config.baseUrl}${path}`, { ... });
```

`lib/stream-client.ts:114` 本来就是参数解构后调用（安全），无需修改。

**验证**：api-client/chat-store/stream-client 相关单测 73/73 通过；lint 0 error（1 条基线 warning）；typecheck 通过；重新 `next build` 后干净浏览器实测：无错误块、`GET /sessions` 200、点击新建会话 `POST /sessions` 正常、会话列表与对话区正常渲染。

**两层根因总结**（本交接文档的最终结论）：
1. **dev 模式**（用户最初遇到的场景）：Next 16.2.x 框架回归 #96294，客户端永不水合——非本项目问题，用 prod 模式绕过（见 8.1-8.4）。
2. **prod 模式**（切换 prod 后暴露）：本项目 `api-client.ts` fetch 调用 this 绑定错误，所有 API 请求 `Illegal invocation` 静默失败——已修复。
3. 用户浏览器最初「部分功能可用」是因为沉浸式翻译扩展 hook 了 `window.fetch`（普通函数包装）意外修复了 this 问题，造成现象忽好忽坏。

### 8.4 处置与当前服务状态（2026-08-07 恢复）

| 服务 | 模式 | 状态 |
| --- | --- | --- |
| FastAPI 后端 | uvicorn 127.0.0.1:8000 | 正常（Start-Process 常驻，.env 解析已按「变量名与 = 之间可能有空格」处理） |
| Next.js 前端 | **prod 模式** `next start` 127.0.0.1:3000 | 正常（`next build` 产物，Start-Process 常驻） |

### 8.5 后续可选动作（交给后续处理人决策）

- **A（推荐，零风险）**：验收与日常使用继续走 **prod 模式**（`npm run build && npm run start`），dev 模式在 Next 官方修复前不可用于本机验收。
- **B（恢复 dev 模式）**：降级 `next@16.1.7`（issue 报告者确认 16.1.7 dev 正常）。注意：影响 package.json/package-lock.json，需重跑前端完整门禁（220 test + lint + typecheck + build）。
- **C**：关注 vercel/next.js #96294 的修复进展，升级到修复版本后恢复 dev 模式。
- 诊断脚本已清理；本机的「Next 16 dev 不水合」与 M3 里程碑记录的环境阻塞（D6-T8 E2E）为同一根因，E2E 自动化门禁需在 prod 模式或修复版 Next 下运行。
