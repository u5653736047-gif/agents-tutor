# OfficeCLI 工具集成实施方案（方案 A：CLI 子进程封装）

> 状态：已实施（2026-08-18 完成 M0～M4 全部必做任务 + T5-3 生成文件下载回执；
> 后端 947 测试全绿、ruff/mypy 通过、`verify_officecli_integration.py` 输出 PASS、
> 真实 DeepSeek+officecli 无头端到端验收通过；剩余 T4-2 前端审批卡人工截图验收）
> 创建日期：2026-08-18　修订日期：2026-08-19
> 目标截止：2026-09-05 前提交比赛作品
> 关联源码：`D:\CODE\office-cli`、`D:\CODE\Agents`
> v2 说明：依据对 officecli 源码与 Agents 后端的逐项核对，修补了 4 个高危漏洞（选项参数逃逸 / batch 子项绕过白名单 / 缺审批运行时门 / stdin 挂死）及 6 个中危问题。修订明细见第 9 节。

---

## 1. 目标

将 officecli 以 **CLI 子进程封装为两个 LangChain 工具** 的方式集成进多智能体助教助学系统：

| 工具 | 用途 | 审批 | 角色 |
|---|---|---|---|
| `officecli_inspect` | 只读：help / view / get / query / validate | 不需要 | 四个角色均可用 |
| `officecli_edit` | 写操作：create / set / add / remove / move / swap / batch / import / merge | 必须人工审批 + 运行时门 | Supervisor、助教、评价 |

系统最终能在助教场景中读写 `.docx / .xlsx / .pptx`，并复用现有角色权限、审批门控、工作区沙箱和工具事件展示。

---

## 2. 范围与非目标

### 2.1 本期必做

- officecli 二进制解析与版本固定
- 工作区可写路径解析
- officecli 子进程执行器（stdin 关闭、无窗口、防挂死）
- 命令白名单、**选项参数白名单（默认拒绝）**、**batch 子项白名单**、文件参数与 `--prop` 路径校验
- `officecli_inspect` / `officecli_edit` 两个工具（写工具带运行时审批门与 per-file 锁）
- app.py 装配、角色权限、超时（含双层超时推导规则）
- **前端审批卡适配数组型命令参数**
- Prompt 使用策略
- 单元测试 + Ruff/mypy
- 真实 officecli 集成 smoke
- 前端审批卡人工验收
- 文档与环境变量说明

### 2.2 本期明确不做

- 前端 HTML/PNG 预览 UI
- MCP 适配器
- Python SDK / resident 常驻进程优化（`save` / `close` / `open` 动词因禁用 resident 而无意义，直接从白名单移除，见 3.1）
- `watch` / `raw` / `raw-set` / `add-part` / `dump` / `plugins` 等复杂或危险命令
- PDF 导出（需要 exporter 插件）
- 上传 Office 文件 → 模型上下文全链路（作为可选加分项，时间不够砍掉）

### 2.3 关于预览功能

officecli 的预览是 CLI 自带能力：

| 命令 | 说明 |
|---|---|
| `view <file> html` | 输出静态 HTML 快照，可 `-o` 落盘，不需要服务器 |
| `view <file> screenshot` | 内置无头浏览器渲染 PNG |
| `watch <file>` | 内置实时预览 HTTP 服务，默认端口 26315 |

本期只允许 `view` 的文本模式：`text / annotated / outline / stats / issues`。
HTML/截图不接入系统前端；`watch` 必须排除（长驻进程会阻塞工具超时）。
后续如需前端预览，只需增加“工具产出文件 → 前端展示”的独立小任务，不影响当前架构。

### 2.4 “上传文件 → 模型上下文”与“工作区文件路径演示”的关系

- 完整链路：用户上传文件 → 后端落盘 → 消息带附件 → 模型知道附件 → office 工具按 file_id 解析路径 → 读写文件。
- 当前系统 `ChatRequest.attachments` 已存在契约，但 chat/stream 路由尚未把附件注入模型上下文；同时上传白名单还不含 Office 格式。
- 这段链路涉及 API 契约、路径解析、用户隔离，测试量较大，因此列为可选任务。
- 若时间不足：**演示时直接把 `.docx/.xlsx/.pptx` 放到会话工作区目录中**，在对话里使用工作区相对路径或已授权绝对路径调用 office 工具。现有 `WorkspaceFileSystem` 已经能授权和解析这些路径，核心集成不受影响。

---

## 3. 冻结的技术决策

### 3.1 命令白名单

```text
READ_VERBS = {"help", "load_skill", "view", "get", "query", "validate"}

EDIT_VERBS = {
    "create", "set", "add", "remove", "move", "swap",
    "batch", "import", "merge"
}

VIEW_MODES = {"text", "annotated", "outline", "stats", "issues"}

# batch --commands JSON 数组中每个 item 的 "command" 字段允许值。
# 显式排除 raw / raw-set / add-part / dump（batch 分发器实际支持它们，
# 若不做子白名单，顶层白名单会被 batch 间接绕过 —— v2 高危 H2）。
BATCH_ITEM_VERBS = {"get", "query", "set", "add", "remove", "move", "swap", "view", "validate"}

# --prop 中承载外部文件路径的键，取值必须解析为工作区内文件（v2 高危 H1）。
FILE_PROPS = {"src", "href"}
```

**`save` / `close` 已从 EDIT_VERBS 移除**（v2 修订 M4）：禁用 resident 后 `save` 是官方 no-op、`close` 无对象；非 resident 路径下每条写命令在 dispose 时即落盘。若 prompt 引导模型调用 `save`，只会白耗一张审批卡。

### 3.2 文件扩展名

```text
OFFICE_EXTENSIONS = {".docx", ".xlsx", ".pptx"}
IMPORT_SOURCE_EXTENSIONS = {".csv", ".tsv"}
MERGE_DATA_EXTENSIONS = {".json"}   # merge --data 允许工作区内 .json 文件路径
```

注意：officecli 源码中 `import` 仅支持 `.xlsx` 目标（V1 限制），工具描述与测试需体现。

### 3.3 位置文件参数

| 动词 | 文件参数 |
|---|---|
| `create/view/get/query/set/add/remove/move/swap/batch/validate` | `argv[1]` |
| `merge` | `argv[1]` 模板（必须已存在）、`argv[2]` 输出（允许不存在） |
| `import` | `argv[1]` 工作簿（必须已存在）、`argv[3]` CSV/TSV 源（可选，必须已存在） |
| `help/load_skill` | 无 |

存在性规则按动词细化（v2 修订 L1）：读操作与 `set/add/remove/move/swap/batch/import` 的主文件要求已存在；`create` 允许不存在；`merge` 模板要求存在、输出允许不存在。

### 3.4 选项参数白名单（默认拒绝）〔v2 新增，对应高危 H1〕

选项白名单按动词冻结如下（已逐项对照 officecli 源码 `CommandBuilder.*.cs`）。**凡不在表内的选项一律拒绝**：

| 动词 | 允许的选项 | 显式拒绝（含理由） |
|---|---|---|
| `help` | 无 | — |
| `view` | `--start --end --max-lines --type --limit --cols --page --range --json` | `--browser --out/-o --render --grid --screenshot-* --page-count`（落盘 / 弹窗 / 浏览器） |
| `get` | `--depth --json` | **`--save`（把图片/OLE 负载写到任意路径：只读工具侧的写原语）** |
| `query` | `--find --compact --fields --json` | — |
| `validate` | `--json` | — |
| `create` | `--type --force --locale --minimal --json` | — |
| `set` | `--prop --find --replace --json` | `--force`（绕过文档保护，首期保守拒绝） |
| `add` | `--type --from --index --after --before --prop --json` | `--force`（同上） |
| `remove` | `--shift --prop --json` | — |
| `move` | `--to --index --after --before --prop --json` | — |
| `swap` | `--json` | — |
| `batch` | `--commands --stop-on-error --json` | `--input`（文件来源）、`--stdin`（隐式）、`--force --best-effort`（保护绕过 / 非原子模式）；**`--commands` 必填** |
| `import` | `--format --header --start-cell --json`；CSV/TSV 源只允许 `argv[3]` 位置形式 | `--file`（选项形式的文件来源）、`--stdin`（会阻塞读管道） |
| `merge` | `--data --force --json` | — |

`--prop` 路径值校验（对顶层与 batch item 的 `props` 均生效）：

1. 键 ∈ `FILE_PROPS`（`src` / `href`）的取值必须经 `resolve_readable_file` 解析，并把值**重写为授权绝对路径**；
2. 其余任意 `--prop` 值若“形如路径”，同样必须解析为工作区内文件，否则拒绝（兜住 `add --type ole/video` 等经由其他键携带文件的情况）。**F1 修订：启发式收紧为四类强路径特征**——盘符前缀（`^[A-Za-z]:`）、以 `/` 开头（POSIX 绝对路径）、以 `//` 或 `\` 开头（UNC 路径）、或以常见资源扩展名结尾；不再把「含单个 `/`」一律视为路径（否则误杀 `TCP/IP`、`A/B 测试`、`2026/08/18` 等教学文本）。`src`/`href` 键本就无条件强制解析，残余风险极低。

**F2 修订**：`batch --commands` 中 `view` 子项的 `mode` 字段按 `VIEW_MODES` 白名单校验（officecli 分发器支持 `mode:"html"/"svg"`，不拦截会旁路绕过「view 仅文本模式」的冻结决策）。

`merge --data` 取值规则：

- 以 `.json` 结尾（不区分大小写）→ 必须经 `resolve_readable_file` 解析为工作区内文件；
- 否则视为内联 JSON → 必须能 `json.loads` 为对象，解析失败即拒绝。

### 3.5 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `API_OFFICECLI_BINARY` | `officecli` | 二进制名或绝对路径 |
| `API_OFFICECLI_ENABLED` | `0` | `0/false/unset` 时完全不注册 office 工具；显式设 `1` 时解析二进制，失败则启动失败（fail-fast 只对显式开启生效，避免打爆无 officecli 的 CI/评委环境 —— v2 修订 M5） |
| `API_OFFICECLI_TIMEOUT_READ_SECONDS` | `60` | 只读工具的**子进程**超时 |
| `API_OFFICECLI_TIMEOUT_WRITE_SECONDS` | `120` | 写工具的**子进程**超时 |
| `API_OFFICECLI_MAX_OUTPUT_BYTES` | `131072` | 单次 stdout+stderr 上限 128KB |

**双层超时推导规则**（v2 修订 M2，沿用 `shell` 惯例）：`tool_timeouts` 注册值 = 子进程超时 + 5 秒，由同一常量推导（60→65、120→125），保证子进程先超时并返回自带诊断，而不是执行器给出泛化超时。

### 3.6 officecli 子进程环境与执行约束

每次调用（含启动自检）必须注入：

```text
OFFICECLI_SKIP_UPDATE=1        # 禁止后台更新检查（Program.cs L226）
OFFICECLI_NO_AUTO_RESIDENT=1   # 禁止自动拉起 resident（CommandBuilder.cs L585）
OFFICECLI_NO_AUTO_INSTALL=1    # 禁止自动安装（Installer.cs L250，防御纵深）
```

并遵守：

1. `subprocess.run(..., stdin=subprocess.DEVNULL)` —— **必须**。officecli 的 `batch`/`import` 在缺省时读 stdin，继承父进程 stdin 会挂死到超时（v2 高危 H4）；
2. 不经 shell；Windows 下 `creationflags=subprocess.CREATE_NO_WINDOW`；
3. `text=True, encoding="utf-8", errors="replace"`（officecli 已强制 UTF-8 输出）；
4. `cwd` 设为当前会话工作区根目录；
5. stdout/stderr 截断到 `API_OFFICECLI_MAX_OUTPUT_BYTES`，返回 `truncated` 标记。

### 3.7 审批运行时门（v2 新增，对应高危 H3）

照抄 `shell_tool` 的双保险模式：

- office_tools.py 内定义 `_APPROVED_OFFICE: ContextVar[bool]` 与 `approved_office_execution()` 上下文管理器；
- `officecli_edit` 入口处 `if not _APPROVED_OFFICE.get(): raise PermissionError`——即使图装配缺陷或未来新增执行入口，未批准上下文中的调用也无法写文件；
- `graph_builder` 的审批恢复路径（现有 `approved_shell_execution()` 所在处）必须**同时**进入 `approved_office_execution()`。

### 3.8 并发控制（v2 修订 M3）

- office_tools.py 模块级锁注册表：`_FILE_LOCKS: dict[str, threading.Lock]` + 一把全局守卫锁；
- 锁键 = 解析后的文件绝对路径；**`officecli_inspect`（含文件参数的命令）与 `officecli_edit` 共用同一注册表**，读写同文件全串行（Windows 下 `File.Replace` 与并发读句柄会撞共享冲突）；
- 无文件参数的命令（`help`/`load_skill`）不加锁；
- 该锁仅单进程有效；多 worker 部署不在本期范围（README 注明）。

### 3.9 权限矩阵

| 工具 | Supervisor | 助教 | 助学 | 评价 |
|---|---:|---:|---:|---:|
| `officecli_inspect` | ✅ | ✅ | ✅ | ✅ |
| `officecli_edit` | ✅ | ✅ | ❌ | ✅ |

注意：`API_OFFICECLI_ENABLED=0` 时工具不注册，`tool_permissions` 也**不得**包含 `officecli_*`（graph_builder 对“权限声明了未注册工具”会直接抛 ValueError）。

---

## 4. 原子任务清单

### M0：决策与准备

#### T0-1 冻结集成参数
- **内容**：把第 3 节所有常量（动词白名单、`BATCH_ITEM_VERBS`、`FILE_PROPS`、选项白名单表、扩展名、token 上限）落入 `core/tools/office_tools.py`，后续代码只引用常量。选项白名单表落库前用 `officecli help <verb>` 逐动词核对一遍实际选项面。
- **文件**：`backend/src/core/tools/office_tools.py`（新建）
- **验收**：代码评审确认常量唯一来源；`ruff check` 通过。
- **耗时**：1h

#### T0-2 二进制解析与启动自检
- **内容**：
  1. 实现 `resolve_officecli_binary()`；
  2. 读取 `API_OFFICECLI_ENABLED`（默认 `0`）：关闭时返回空工具集、不做任何二进制探测；开启时才解析；
  3. `shutil.which()` / 绝对路径解析；
  4. 经 `_run_officecli` 通道运行 `<binary> --version`（5 秒超时，**注入 3.6 节全部环境变量**，避免自检本身拉起更新子进程）；
  5. 开启状态下找不到 / 不可执行 / `--version` 运行失败 → 启动失败并给出安装提示（fail-fast）；版本号与预期不一致 → 记日志告警、不阻断；
  6. 预期版本以部署机实际安装版本为准（源码 csproj 当前为 1.0.142，风险表原 1.0.144 与仓库不符，v2 修订 L2）。
- **文件**：
  - `backend/src/core/tools/office_tools.py`
  - `.env.example`
  - `README.md`
- **验收**：
  ```powershell
  cd D:\CODE\Agents\backend
  uv run python -c "from core.tools.office_tools import resolve_officecli_binary; print(resolve_officecli_binary())"
  ```
  输出本机 officecli 绝对路径。
- **耗时**：1h

---

### M1：基础能力

#### T1-1 工作区路径解析（读 + 写）
- **内容**：在 `WorkspaceFileSystem` 新增两个公开方法：
  - `resolve_readable_file(path)`：对现有 `_resolve_existing(expected="file")` 的公开包装（inspect 工具与选项参数校验共用）；
  - `resolve_writable_file(path, *, allow_create)`：
    1. 复用 `_candidate_path()`，拒绝空串、NUL、`..`、盘符冒号；
    2. 词法路径必须在授权 root 内；
    3. 已存在文件 `resolve(strict=True)` 后仍必须在同一授权 root 内，防止符号链接/junction 逃逸；
    4. 不存在且 `allow_create=True` 时，最近已存在父目录必须可 resolve 且仍在授权 root 内；
    5. 复用 `_assert_allowed()` 拒绝敏感文件；
    6. 扩展名按 3.2 节白名单校验（`MERGE_DATA_EXTENSIONS` / `IMPORT_SOURCE_EXTENSIONS` 供对应参数使用）。
- **文件**：
  - `backend/src/core/filesystem/workspace.py`
  - `backend/tests/test_workspace_filesystem.py`
- **验收**：
  ```powershell
  uv run pytest tests/test_workspace_filesystem.py -q
  ```
- **耗时**：2.5h

#### T1-2 officecli 子进程执行器
- **内容**：实现 `_run_officecli(binary, argv, *, cwd, timeout_seconds, max_output_bytes)`。
  1. `subprocess.run`，不经过 shell；
  2. **`stdin=subprocess.DEVNULL`**（H4）；Windows 下 `CREATE_NO_WINDOW`；
  3. `text=True, encoding="utf-8", errors="replace"`；
  4. 注入 3.6 节全部三个环境变量；
  5. 捕获超时并返回稳定结果；
  6. stdout/stderr 截断，返回 `truncated` 标记；
  7. 统一返回：
     ```json
     {
       "ok": false,
       "exit_code": 1,
       "stdout": "",
       "stderr": "",
       "timed_out": false,
       "truncated": false
     }
     ```
- **文件**：
  - `backend/src/core/tools/office_tools.py`
  - `backend/tests/test_office_tools.py`（新建）
- **验收**：用 `[sys.executable, fake_script]` 假二进制验证正常/失败/超时/大输出截断/**stdin 已关闭（假脚本读 stdin 立即 EOF 而非阻塞）**。
- **耗时**：2h

#### T1-3 命令、选项与 batch 三层解析
- **内容**：实现 `_normalize_office_command(raw_command, filesystem, *, writable)`。
  1. 首 token 必须在对应动词白名单（READ_VERBS / EDIT_VERBS）；
  2. token 数 ≤ 48；单 token ≤ 2000 字符；**例外：`--commands` 与 `--data` 的取值 ≤ 32768 字符**（v2 修订 M6——否则 20 条 set 的 batch JSON 会被自己的限制卡死）；禁止 NUL；
  3. `view` 模式必须在 `VIEW_MODES`；
  4. **选项按 3.4 节白名单校验，默认拒绝**；表内“显式拒绝”项返回带理由的错误；
  5. `batch` 必须携带 `--commands`：`json.loads` 必须成功且为对象数组；每个 item 的 `command` 字段必须在 `BATCH_ITEM_VERBS`（拦截 `raw`/`raw-set`/`add-part`/`dump`，H2）；item 的 `props` 同样走 `FILE_PROPS` + 路径启发式校验（H1）；
  6. 按 3.3 节位置表把文件参数替换为授权绝对路径，存在性按动词细化规则校验；
  7. `--prop` 中 `FILE_PROPS` 键与路径形取值：解析并重写为授权绝对路径（H1）；
  8. `merge --data` 按 3.4 节规则处理（工作区 .json 文件或内联 JSON）。
- **文件**：`backend/src/core/tools/office_tools.py`、`backend/tests/test_office_tools.py`
- **验收**：白名单外动词、**白名单外选项**、**batch 内 `raw-set` item**、路径逃逸、文件位置解析均有单测。
- **耗时**：3.5h

---

### M2：工具层

#### T2-1 `officecli_inspect` 只读工具
- **内容**：
  ```python
  @tool(
      "officecli_inspect",
      args_schema=_OfficeCliInput,
      extras={"category": "office", "read_only": True},
  )
  def officecli_inspect(command: list[str]) -> dict[str, object]: ...
  ```
  - 含文件参数的命令先取 3.8 节 per-file 锁再执行子进程；
  - 描述中包含：
    - 支持 `.docx/.xlsx/.pptx`；
    - 先 help，再 get/query/view；
    - 文件使用工作区相对路径或已授权绝对路径；
    - `view` 可用模式列表；大表格用 `view text --range Sheet1!A1:C10`；
    - 大文档用 `--max-lines / --start / --end`；
    - 结构化输出加 `--json`。
- **文件**：`backend/src/core/tools/office_tools.py`
- **验收**：Python 直接 invoke 读取工作区真实 xlsx，返回 `ok=true`。
- **耗时**：2h

#### T2-2 `officecli_edit` 写工具
- **内容**：
  ```python
  @tool(
      "officecli_edit",
      args_schema=_OfficeCliInput,
      extras={
          "category": "office",
          "requires_approval": True,
          "status_from_ok": True,
      },
  )
  def officecli_edit(command: list[str]) -> dict[str, object]: ...
  ```
  1. 入口处校验 3.7 节运行时门：`if not _APPROVED_OFFICE.get(): raise PermissionError`（H3）；
  2. `requires_approval=True` 复用现有审批节点、API 和前端审批卡；
  3. `status_from_ok=True` 让非零退出自动归类为工具执行失败；
  4. 所有文件参数走 `resolve_writable_file`（create/merge 输出 `allow_create=True`，其余要求存在）；
  5. 与 inspect 共用 3.8 节 per-file 锁，防止并发写冲突；
  6. 描述中要求：多步修改优先 `batch --commands`；完成后 `validate` 校验；**不提 `save`/`close`**（非 resident 路径每条写命令即时落盘）；
  7. （可选增强，时间紧可后置）`extras` 增加 `precheck` 回调，让 `prepare_approval` 在进入审批中断前完成 `_normalize_office_command` 校验，避免把注定失败的命令送去审批。
- **文件**：
  - `backend/src/core/tools/office_tools.py`
  - `backend/src/core/tools/executor.py`（仅当实现第 7 点 precheck 时）
- **验收**：
  - 直接 invoke（在 `approved_office_execution()` 上下文内）可创建/修改文件；
  - **无批准上下文直接 invoke 抛 `PermissionError`**（H3 验收）；
  - `tool.extras["requires_approval"] is True`；
  - 非零退出返回 `ok=false`；
  - 并发读写同一文件不损坏、不撞共享冲突。
- **耗时**：2.5h

#### T2-3 前端审批卡适配数组型命令〔v2 新增，对应 M1〕
- **内容**：`officecli_edit` 的 `command` 是 `list[str]`，现有 `terminal-approval-card.tsx` 的 `textArgument` 只处理字符串，会显示“（未提供命令）”导致**用户盲批**。修改卡片渲染：`command` 为数组时按空格 join 展示（保留等宽/折行样式），并按工具名给出合适的卡片标题（office 写操作 vs shell）。
- **文件**：
  - `frontend/components/terminal-approval-card.tsx`
  - `frontend/components/assistant-ui/approval-cards.tsx`（如需透传工具名）
  - 前端组件测试
- **验收**：构造 `officecli_edit` 的 pending 审批状态，卡片完整显示命令文本；shell 卡片回归不受影响。
- **耗时**：1.5h

---

### M3：系统接线与质量

#### T3-1 app.py 装配、权限、超时、审批上下文接线
- **内容**：
  1. lifespan 中构造 office 工具（受 `API_OFFICECLI_ENABLED` 控制）；
  2. `tools` 列表加入 `*office_tools`；**禁用时 `tool_permissions` 同步省略 `officecli_*` 两项**（graph_builder 会拒绝未注册工具的权限声明）；
  3. 权限按 3.9 节矩阵注入 `tool_permissions`；
  4. `tool_timeouts` 按 3.5 节推导规则注册：`officecli_inspect: 60+5`、`officecli_edit: 120+5`（从环境变量常量推导，不二次硬编码）；
  5. `graph_builder.py` 审批恢复路径在现有 `approved_shell_execution()` 旁**并列加入 `approved_office_execution()`**（H3 接线点）。
- **文件**：
  - `backend/src/api/app.py`
  - `backend/src/core/graph_builder.py`
- **验收**：
  ```powershell
  uv run pytest tests/test_api_file_tool_wiring.py tests/test_api.py -q
  uv run pytest tests/test_graph_builder.py -q
  ```
  graph 构建成功，无 missing/unknown permission 报错；**默认 env（ENABLED=0）下全部现有测试不变绿转红**；用假二进制（`sys.executable` + 脚本）覆盖 ENABLED=1 的装配路径。
- **耗时**：2h

#### T3-2 Prompt 使用策略
- **内容**：在 `core/nodes/prompts.py` 增加短策略，不贴长 SKILL：
  - 用户要求读写 Office 文档时使用对应工具；
  - 先 `help` / `inspect`，再修改；
  - 多次修改合并为一次 `batch --commands`；
  - 完成后 `validate`（**不引导 `save`/`close`**，动词已移除）；
  - 文件必须位于当前会话工作区；`import` 仅支持 `.xlsx` 目标。
- **文件**：`backend/src/core/nodes/prompts.py`
- **验收**：现有 prompt 相关测试通过；真实模型对话能正确路由到 office 工具。
- **耗时**：1h

#### T3-3 单元测试补齐
- **内容**：`backend/tests/test_office_tools.py` 覆盖：
  1. 只读/写白名单；
  2. `mcp/watch/raw/unwatch/open/save/close/dump/plugins` 等动词被拒；
  3. **选项白名单外拒绝：`import --file`、`merge --data` 为外部绝对路径、`view -o`、`get --save`、`--browser`、`batch --input`、`--stdin`**（H1/H4）；
  4. **batch 子项：`raw-set`/`add-part`/`dump` item 被拒；`--commands` 缺失被拒；item 内 `props.src` 外部路径被拒并重写工作区路径**（H1/H2）；
  5. **`--prop src=`、`--prop href=` 及路径形取值的工作区解析与重写**（H1）；
  6. 路径逃逸拒绝（含符号链接/junction）；
  7. 存在性规则：读要求存在、create/merge 输出允许不存在、merge 模板要求存在；
  8. `--commands`/`--data` 大 token 放行、普通 token 2000 上限（M6）；
  9. 子进程环境变量注入 + stdin 关闭；
  10. 输出截断、超时处理；
  11. 写工具审批 extras 与**无门上下文拒绝**（H3）；
  12. per-file 锁：并发读写同一文件串行、不同文件并行；
  13. 真实 officecli 集成测试（无二进制时 skip）。
- **验收**：
  ```powershell
  uv run pytest tests/test_office_tools.py -q
  uv run pytest tests -q
  ```
- **耗时**：4h

#### T3-4 Ruff / mypy
- **验收**：
  ```powershell
  uv run ruff check src tests scripts
  uv run mypy src/core
  ```
- **耗时**：0.5h

---

### M4：验收与提交准备

#### T4-1 真实 officecli 集成 smoke
- **内容**：新增 `backend/scripts/verify_officecli_integration.py`：
  1. 临时 workspace；
  2. `officecli_edit` 创建 `成绩单.xlsx`；
  3. `batch --commands` 写入 20 个单元格（同时回归 M6 的大 token 放行）；
  4. `officecli_inspect` 读取并校验（含 `view text --range`）；
  5. `validate`；
  6. 输出 `PASS`。
- **验收**：
  ```powershell
  cd D:\CODE\Agents\backend
  uv run python scripts/verify_officecli_integration.py
  ```
- **耗时**：1h

#### T4-2 前端/API 人工验收
- **内容**：
  1. 启动 `scripts/start-stage3.ps1`（`.env` 显式 `API_OFFICECLI_ENABLED=1`）；
  2. 在工作区放 `测试.xlsx`；
  3. 询问读取 A1:B5，确认 `officecli_inspect` 工具卡片与回答；
  4. 询问修改 A1，确认 `officecli_edit` 审批卡**完整显示命令文本（数组渲染，无“（未提供命令）”）**；
  5. 批准后确认文件真实变化；拒绝时确认模型收到拒绝并停止；
  6. 验证角色权限（助学调写工具被拒）与事件脱敏；
  7. 演示机卫生检查：无手工拉起的 `officecli open/watch` resident、目标文件未被 Excel 进程占用（v2 修订 L4/L5）。
- **交付**：验收截图/录屏。
- **耗时**：1h

#### T4-3 文档与提交清单
- **内容**：
  1. `.env.example` 增加 `API_OFFICECLI_*`（注明默认 `0` 与开启语义）；
  2. `README.md` 增加 officecli 能力、安装、环境变量、验收说明、非目标说明；注明 per-file 锁为单进程语义、多 worker 部署不在本期范围；
  3. 确认不提交 officecli 二进制、测试生成文件、真实 API Key；
  4. 更新比赛提交材料中的功能说明。
- **耗时**：1h

---

## 5. 可选加分项（时间不足直接砍）

### T5-1 上传白名单加入 Office 格式
- **文件**：`backend/src/api/files.py`
- **内容**：
  - `ALLOWED_UPLOAD_EXTENSIONS` 加入 `.docx/.xlsx/.pptx`；
  - `_UPLOAD_CONTENT_TYPES` 增加对应 MIME。
- **验收**：前端可上传 docx/xlsx/pptx 并显示附件。
- **耗时**：1h

### T5-2 附件信息注入模型上下文
- **内容**：
  1. chat/stream 在 `graph.run` 前把附件元数据写入用户消息；
  2. office 工具能将 `file_id` 解析为受控上传目录路径；
  3. 处理 `data/uploads/<user_key>/<uuid>.<ext>` 与 workspace 授权的关系。
- **风险**：涉及用户上下文和路径授权，测试量大。时间不足时最先砍掉。
- **耗时**：2h

### T5-3 生成文件下载回执
- **内容**：把 officecli 生成的文件注册为可下载文件，复用 `Message.attachments` 契约，前端出现下载入口。
- **建议**：与 T5-2 一起做。
- **耗时**：2h

---

## 6. 建议排期（单人开发）

| 时间 | 任务 |
|---|---|
| 第 1 天上午 | T0-1 → T0-2 → T1-1 |
| 第 1 天下午 | T1-2 → T1-3（选项/batch 加固为主） |
| 第 2 天上午 | T2-1 → T2-2 |
| 第 2 天下午 | T2-3 → T3-1 |
| 第 3 天上午 | T3-2 → T3-3 |
| 第 3 天下午 | T3-3 收尾 → T3-4 → T4-1 |
| 第 4 天上午 | T4-2（含演示机卫生检查） |
| 第 4 天下午 | T4-3 + 全量回归 |
| 第 5 天 | 缓冲 + 演示排练 |

核心闭环约 **26～29 小时（约 3.5 人日）**，较 v1 增加 ~6h（安全加固 3h、前端卡片 1.5h、测试扩充 1.5h）。建议 8 月 26 日前启动，9 月 1 日前代码冻结，9 月 2～4 日整体验收和演示排练。

---

## 7. 里程碑完成标准

| 里程碑 | 完成标准 |
|---|---|
| M0 | 二进制可解析，常量冻结（含选项表、batch 子白名单），启动自检通过且默认禁用不破坏现有测试 |
| M1 | 读写路径解析、执行器（stdin 关闭）、动词/选项/batch 三层白名单全部有单测 |
| M2 | 两个工具可 Python 直接 invoke 并读写真实文件；**无批准上下文调用写工具被拒**；审批卡能完整显示数组型命令 |
| M3 | pytest 全绿 + ruff + mypy 通过，graph 权限校验通过（启用/禁用两态） |
| M4 | `verify_officecli_integration.py` 输出 PASS；前端审批卡可见完整命令文本并通过人工验收 |
| M5（可选） | 上传 Office 文件可进入模型并下载生成文件 |

---

## 8. 风险与后备方案

| 风险 | 后备方案 |
|---|---|
| 模型不按 help 优先，命令语法错误频繁 | 工具描述给固定示例；演示先收窄到 xlsx `text/get/set/batch` |
| 模型生成 `save`/`close` 等已移除动词 | 白名单拒绝 + 错误信息说明“编辑即时落盘，无需 save” |
| 模型使用白名单外选项（如 `--limit` 拼错位置） | 默认拒绝的错误信息附该动词允许的选项清单 |
| 审批影响演示节奏 | 演示前预跑；或只对工作区副本操作 |
| batch JSON 生成失败 | 先允许单条 set/add，跑通后再引导 batch |
| officecli 自动升级导致行为变化 | 注入三个 OFFICECLI_* 环境变量 + 固定部署机实际版本（当前源码 1.0.142），启动时版本不一致告警 |
| 目标文件被 Excel/WPS 进程占用，写命令共享冲突失败 | 演示前关闭 Excel；错误信息提示用户关闭占用程序后重试 |
| 演示机残留手工拉起的 resident（`open`/`watch`）导致命令走管道、busy 重试超时 | 演示机卫生检查（T4-2 第 7 项）；必要时 `officecli close` 清理 |
| 多进程部署下 per-file 锁失效 | 本期单进程运行（README 注明）；如需多 worker 再引入文件锁 |
| 附件链路时间不够 | 砍 T5-2/T5-3，演示用工作区文件路径 |
| 上传 Office 格式引发前端渲染问题 | 只做 T5-1，前端按通用附件展示/下载 |

---

## 9. v2 修订记录（问题 → 落点）

| 编号 | 问题 | 严重度 | 修订落点 |
|---|---|---|---|
| H1 | 位置参数校验遗漏选项：`import --file`、`merge --data`、`--prop src=`、`view -o`、`get --save` 可越权读写工作区外文件 | 高 | 3.4 选项白名单（默认拒绝）+ `FILE_PROPS`/路径启发式校验；T1-3 第 4/7/8 条；T3-3 用例 3/4/5 |
| H2 | batch 分发器实际支持 `raw`/`raw-set`/`add-part`/`dump`，可绕过顶层动词白名单 | 高 | 3.1 `BATCH_ITEM_VERBS`；T1-3 第 5 条；T3-3 用例 4 |
| H3 | 写工具只有 extras 标记，缺 shell 式运行时审批门，图缺陷/新入口可绕过审批 | 高 | 3.7 `approved_office_execution()`；T2-2 第 1 条与验收；T3-1 第 5 条（graph_builder 接线） |
| H4 | 子进程继承 stdin，`batch`/`import --stdin` 缺省读 stdin 会挂死到超时 | 高 | 3.6 第 1 条；T1-2 第 2 条与验收；T3-3 用例 3/9 |
| M1 | 前端审批卡 `textArgument` 只认字符串，数组命令显示“（未提供命令）”，用户盲批 | 中 | 新增 T2-3；T4-2 第 4 项；里程碑 M2/M4 |
| M2 | 双层超时同值竞态，子进程诊断被执行器泛化超时吃掉 | 中 | 3.5 推导规则（+5s，同源常量）；T3-1 第 4 条 |
| M3 | per-file 锁只在写工具内，Windows 下 `File.Replace` 撞并发读句柄 | 中 | 3.8 读写共用锁注册表；T2-1/T2-2；T3-3 用例 12 |
| M4 | `save`/`close` 在禁用 resident 后无意义，却仍走人工审批徒增摩擦 | 中 | 3.1 移除两动词；T3-2 prompt 同步修订 |
| M5 | `ENABLED` 默认 1 + fail-fast 会让无 officecli 的 CI/评委环境启动即崩 | 中 | 3.5 默认 0、显式开启才 fail-fast；T3-1 验收覆盖两态 |
| M6 | 单 token ≤2000 与 batch `--commands`/`merge --data` 自相矛盾 | 中 | T1-3 第 2 条例外（≤32768）；T4-1 回归 |
| L1 | merge 输出文件“必须存在”规则自相矛盾 | 低 | 3.3 存在性按动词细化 |
| L2 | 版本号 1.0.144 与源码 csproj 1.0.142 不符 | 低 | T0-2 第 6 条 |
| L3 | 启动自检未注入 SKIP_UPDATE，可能拉起后台更新子进程 | 低 | T0-2 第 4 条：自检复用 `_run_officecli` |
| L4 | 手工 resident 干扰（busy 重试 30s×3 可能超时） | 低 | 8 风险表 + T4-2 第 7 项 |
| L5 | Excel 占用文件锁导致写失败 | 低 | 8 风险表 |
| L6 | 路径归一化发生在批准之后，可能批准注定失败的命令 | 低 | T2-2 第 7 条 precheck（可选增强） |
| F1 | 路径启发式「含 `/` 或 `\`」误杀教学文本（TCP/IP、A/B 测试、日期），错误提示还要求模型改写内容 | 中 | 3.4 第 2 条收紧为四类强路径特征；错误提示不再要求改写普通文本；实测 PPT 写入 "Q/K/V 与自注意力" 通过 |
| F2 | batch 内 `view` 子项 `mode:"html"/"svg"` 未拦截，旁路「仅文本模式」冻结决策 | 低 | `_normalize_batch_commands` 补 `VIEW_MODES` 白名单校验 |
| F3 | `test_chat_titles_session_from_first_message_only` 同一微秒时间戳 flaky | 低 | 断言 `>` 改 `>=`（语义为「后续消息触碰 updated_at」） |
