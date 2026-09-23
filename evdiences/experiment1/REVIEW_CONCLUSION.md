# Experiment 1 Review Conclusion

## 背景

这次实验的任务是让当前 `coding-agent` 为一个项目生成 `README.md`。实验过程中记录了 `PROBLEM.md` 和多份 `llm-run-*` 日志，用于观察 Agent 在真实任务中的行为。

这份文档总结前面对实验现象的分析、对抗评审结论，以及从第一性原理出发得到的后续迭代方向。

## 总体结论

当前项目的问题不是单纯的 prompt 不够好，也不是 LLM 不够聪明，而是：

```text
Agent 控制平面缺失。
模型直接驱动低阶工具。
程序没有任务状态、环境模型、动作策略、评估标准和安全边界。
```

一个可用于真实编码工作的 Agent，本质上应该是一个控制系统：

```text
任务目标 -> 环境建模 -> 行动选择 -> 执行动作 -> 观测反馈 -> 质量评估 -> 停止/继续
```

当前 `run` 循环更接近：

```text
用户请求 -> LLM -> tool_calls -> 执行工具 -> 继续给 LLM
```

这意味着 LLM 同时承担了 planner、executor、evaluator 和 stop condition 的职责，导致行为不稳定。

## 已观察到的主要现象

- 当已有 `README.md` 看起来比较完整时，模型可能较早结束，不一定跑满 `max_steps`。
- 当项目说明不完整时，模型会继续探索和写入，容易触达或超过 `max_steps`。
- 用户 prompt 中出现的目标项目路径没有自动成为工具工作目录，工具仍然基于当前进程的 `Path.cwd()` 工作。
- LLM 选择读取哪些文件、先读哪些目录是不稳定的，并且没有覆盖标准。
- `max_steps` 限制的是模型轮数，不是任务完成度；一轮可能包含多个 tool calls，也可能没有 tool calls。
- 大项目不能靠逐个 `read_file` 全量塞进上下文，需要上下文治理和项目索引。
- 模型会主动选择验证命令，例如 Python 项目中执行 `python -m compileall ...`。
- 当前 `write_file` 是整文件覆盖，适合创建新文件，但不适合修改已有源码或重要文档。

## 对抗评审后的修正

前面的分析大方向合理，但有些结论需要更严格地表达。

- `llm-run-1` 到 `llm-run-6` 更像同一次执行中的多次 `messages` 快照，而不是 6 次完全独立实验；相同 tool call id 是强证据，但仍应以日志采集方式为最终依据。
- “解析用户 prompt 中的目录”不是最佳根治方案；更可靠的方式是由 CLI 或程序层显式确定 workspace，prompt 中的路径只能作为候选信号。
- LLM 读取顺序不稳定本身不是 bug；真正的问题是没有读取预算、覆盖标准和证据完整性检查。
- 模型执行 `compileall` 不只是因为系统提示要求验证，也因为暴露了 `run_command` 这个 affordance；程序需要根据任务类型约束验证策略。
- `repo_map` 能解决文件范围和入口识别问题，但不能单独解决大项目理解；还需要摘要、检索、符号索引和上下文压缩。
- `write_file` 不应被完全废弃；创建新文件时全量写入合理，修改已有文件时应优先 `apply_patch` 或精确替换。

## 漏掉但关键的问题

### 1. 缺少任务规格化

用户请求没有先被转换成结构化任务，例如：

```text
任务类型：生成 README
目标项目：/Users/ligen120/code/coding-agent-test-project
目标文件：README.md
允许修改范围：README.md
验收标准：内容覆盖安装、使用、项目结构、开发验证、安全说明
```

没有 `TaskSpec`，后续工具选择和停止条件都只能依赖模型自觉。

### 2. 缺少显式停止条件

当前逻辑中，“模型没有 tool_calls” 就代表结束。但这不等价于任务完成。

更合理的停止条件应该是：

```text
目标文件已创建或修改
变更 diff 已审查
必要验证已执行，或明确说明无需验证
最终产物满足 TaskSpec 中的验收标准
```

### 3. 缺少证据约束

README 中的项目事实应该来自已读文件、项目配置和工具结果。否则模型可能基于项目名、常识或旧 README 生成不可靠内容。

应引入 `EvidenceTable` 或 `ProjectSummary`，记录：

```text
事实 -> 来源文件 -> 置信度 -> 是否用于最终文档
```

### 4. 缺少变更事务性

写入前没有 snapshot，写入后没有 diff，失败后没有 rollback。

真实编码任务中，文件修改应该具有事务感：

```text
计划修改 -> 应用 patch -> 查看 diff -> 验证 -> 保留或回滚
```

### 5. 缺少上下文治理

当前所有工具结果都会长期留在 `messages` 中。大文件、重复日志和无关目录会污染上下文。

后续应该区分：

```text
原始观测：工具返回的完整内容
压缩观测：给模型继续推理的摘要内容
审计日志：落盘保存的结构化事件
```

### 6. 缺少敏感文件策略

即使 `.gitignore` 忽略了 `.env`，工具层也应该默认禁止或要求确认读取：

```text
.env
*.pem
*.key
id_rsa
credentials*
secrets*
```

### 7. 缺少命令安全策略

当前 shell 工具允许模型执行任意命令。仅靠 prompt 要求避免破坏性命令不够。

应至少区分：

```text
safe：ls、cat、python -m compileall、pytest 指定路径
review：pip install、npm install、git checkout、网络访问
deny：rm -rf、git reset --hard、curl | sh、写系统目录
```

### 8. 缺少实验可重复性

如果某一轮已经写入 `README.md`，后续实验环境就被改变了。评估 Agent 能力前，需要自动准备干净 fixture 或回滚变更。

### 9. 缺少结构化日志

当前打印完整 `messages` 会导致日志巨大、难以比较，并可能泄露敏感内容。

更好的日志是 JSONL event：

```text
step_started
phase_changed
tool_call
tool_result_summary
file_changed
diff_reviewed
validation_result
task_finished
```

## 更准确的根因分层

### 控制器缺失

程序没有显式管理 `Task -> Understand -> Plan -> Execute -> Evaluate`，而是让模型自由决定下一步。

### 工具过于低阶

当前工具主要是 `list_files`、`read_file`、`write_file`、`run_command`。这些是原子能力，但缺少编码任务所需的高阶工具：

- `resolve_workspace`
- `repo_map`
- `detect_project`
- `apply_patch`
- `git_diff`
- `validate_project`
- `summarize_file`

### 上下文缺少预算和筛选

没有忽略规则、文件优先级、读取预算、摘要压缩和证据链，大项目必然不可控。

### 安全边界偏弱

文件路径限制只是第一层。真实 Agent 还需要命令策略、敏感文件策略、写入策略、日志脱敏和用户确认机制。

### 评估闭环不完整

写完文件后没有强制 diff review，也没有根据任务类型判断应该跑什么验证。

## 推荐的迭代优先级

### P0：WorkspaceResolver

由程序显式确定 workspace。

- CLI 的 `--cwd` 是权威来源。
- prompt 中出现的绝对路径只能作为候选。
- 如果候选路径和当前 workspace 不一致，应提示或由程序层切换。
- 所有工具都应依赖统一的 workspace context，而不是直接读 `Path.cwd()`。

### P0：TaskSpec

把用户请求规格化为结构化任务。

示例：

```text
task_type: create_readme
workspace: /path/to/project
target_files: [README.md]
allowed_write_paths: [README.md]
acceptance_criteria:
  - explains project purpose
  - includes installation
  - includes usage
  - includes project structure
  - includes validation notes
```

### P0：RepoMap + IgnorePolicy

生成过滤后的项目结构。

- 优先使用 `git ls-files`。
- 默认排除 `.git/`、`.venv/`、`__pycache__/`、`*.egg-info/`、构建产物。
- 标记 manifest、入口文件、测试目录、文档文件。

### P0：Patch/Diff 编辑闭环

区分创建和修改：

- 新文件：允许 `create_file` 或 `write_file`。
- 已有文件：优先 `apply_patch`。
- 每次写后自动 `git diff -- <file>`。
- diff 必须回传给模型或 evaluator 审查。

### P1：阶段状态机

显式实现：

```text
Understand -> Plan -> Execute -> Evaluate -> Finalize
```

每个阶段限制可用工具：

- Understand：只允许读和 repo map。
- Plan：输出计划，不允许写。
- Execute：允许 patch/create/run safe command。
- Evaluate：允许 diff 和验证命令。
- Finalize：不允许再调用修改类工具。

### P1：ValidationPolicy

根据任务和变更类型选择验证。

- 修改 README：通常做 markdown/diff review，不一定跑 `compileall`。
- 修改 Python 源码：优先 `python -m compileall`、相关测试。
- 修改依赖或配置：优先安装/构建检查，但可能需要用户确认。

### P1：结构化事件日志

不要 dump 全量 `messages`。

记录事件：

```json
{"type":"tool_call","step":2,"tool":"read_file","args":{"path":"pyproject.toml"}}
{"type":"file_changed","path":"README.md","mode":"create"}
{"type":"validation_result","command":"python -m compileall agent cli model tools","exit_code":0}
```

### P2：上下文压缩和证据链

引入 `ProjectSummary` 和 `EvidenceTable`，让最终文档基于已确认事实生成。

### P2：可重复实验框架

每次实验前准备 fixture，运行后保存：

```text
输入任务
初始文件树
工具事件日志
最终 diff
验证结果
人工评价
```

## README 生成任务的理想控制流

```text
1. Resolve workspace
2. Build TaskSpec
3. Build repo map with ignore policy
4. Detect project type
5. Read manifest and key source files
6. Build evidence summary
7. Draft README
8. Create or patch README
9. Review git diff
10. Run task-appropriate validation
11. Final summary
```

## 下一步建议

下一步不要优先调整 `max_steps`，也不要只优化 prompt。

最值得先做的是：

```text
WorkspaceResolver + TaskSpec + RepoMap
```

这三件事会把 Agent 从“模型自由探索项目”推进到“程序先定义任务和工作区，模型在受控上下文中行动”。

随后再做：

```text
Patch/Diff 编辑闭环 + ValidationPolicy + 结构化日志
```

这会让它更接近真实可用的编码 Agent，而不是简单的 tool-calling demo。

