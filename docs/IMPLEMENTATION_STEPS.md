# Experiment 1 Implementation Steps

## 核心结论

这次实验暴露出的核心问题不是 `max_steps` 太小，也不是 prompt 写得不够详细，而是当前 Agent 的控制权过度交给了 LLM。

当前执行模式更接近：

```text
用户请求 -> LLM 自主决定 tool_calls -> 程序执行工具 -> 工具结果回传给 LLM -> LLM 自主决定是否继续
```

这会导致几个系统性问题：

- LLM 自己决定 workspace，但工具实际只基于当前 `Path.cwd()` 工作。
- LLM 自己决定读哪些文件，但程序没有项目地图、读取预算和覆盖标准。
- LLM 自己决定什么时候写文件，但程序没有阶段约束和变更审查。
- LLM 自己决定验证命令，但程序没有根据任务类型控制验证策略。
- LLM 自己决定什么时候结束，但程序没有独立的完成判定和验收标准。

因此，下一阶段目标不是继续增强 prompt，而是把项目从“LLM 自由调用工具”升级成“程序主导流程，LLM 在受控阶段内推理和生成候选动作”。

## 目标架构方向

后续 Agent 应该围绕下面的控制循环实现：

```text
Task -> Understand -> Plan -> Execute -> Evaluate -> Finalize
```

其中：

- 程序负责 workspace、任务规格、阶段流转、工具权限、上下文预算、diff、验证和停止条件。
- LLM 负责理解语义、根据上下文提出计划、生成 patch、解释验证结果和总结输出。
- 工具不再只是低阶能力集合，而是受任务状态和阶段约束的动作集合。

## 实施步骤

### Step 1：实现 WorkspaceResolver

目标：明确所有工具操作的项目根目录，避免依赖 `Path.cwd()` 和 LLM 从自然语言中猜路径。

实施内容：

- 在 CLI 层把 `--cwd` 解析后的绝对路径作为权威 workspace。
- 如果用户 prompt 中出现绝对路径，只把它作为候选 workspace。
- 当候选路径和 `--cwd` 不一致时，先记录或提示，不让工具静默操作错误目录。
- 修改文件工具和 shell 工具，使它们依赖显式传入的 workspace context，而不是各自调用 `Path.cwd()`。

验收标准：

- 用户请求里包含 `/some/project` 时，Agent 不会默认在当前仓库误操作。
- 所有工具返回结果中都能明确说明当前 workspace。
- 测试覆盖“默认 cwd”、“显式 --cwd”、“prompt 中路径与 --cwd 不一致”三种情况。

### Step 2：实现 TaskSpec

目标：把自然语言任务转成结构化任务规格，作为后续计划、执行和停止条件的依据。

建议结构：

```text
task_type: create_readme | modify_code | fix_test | explain_project | unknown
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

实施内容：

- 先用简单规则识别常见任务类型，例如创建 README、修改文件、运行测试、解释项目。
- 允许 LLM 辅助补全 TaskSpec，但程序必须做基本校验。
- 后续工具执行必须参考 `allowed_write_paths`。

验收标准：

- README 任务能明确目标文件和允许写入范围。
- 不是所有任务都默认开放写工具和 shell 工具。
- 最终完成判断能基于 `acceptance_criteria` 做检查。

### Step 3：实现 RepoMap + IgnorePolicy

目标：让 Agent 先获得经过过滤的项目地图，而不是让 LLM 对裸目录做随机探索。

实施内容：

- 新增 `repo_map` 工具。
- 优先使用 `git ls-files` 获取受版本控制的文件。
- 非 git 项目使用文件遍历，但应用 ignore 规则。
- 默认忽略：`.git/`、`.venv/`、`venv/`、`__pycache__/`、`*.egg-info/`、`build/`、`dist/`、`.pytest_cache/`、`.mypy_cache/`。
- 标记关键文件：`pyproject.toml`、`package.json`、`go.mod`、`README.md`、入口文件、测试目录。

验收标准：

- 模型不会再看到 `.venv/`、`.git/`、`__pycache__/` 这类无关目录。
- README 任务优先读取 manifest、入口文件、已有 README 和测试目录。
- 大项目下不会无控制地全量读取所有文件。

### Step 4：实现 Patch/Diff 编辑闭环

目标：降低写文件风险，让每次变更都可审查、可验证、可回滚。

实施内容：

- 保留 `write_file` 用于创建新文件。
- 新增 `apply_patch` 或 `replace_text`，用于修改已有文件。
- 修改已有文件时禁止直接全量覆盖，除非用户明确要求。
- 每次写入后自动运行 `git diff -- <file>`。
- 将 diff 作为 Evaluate 阶段输入，让模型或程序检查是否符合任务目标。

验收标准：

- 修改已有 README 或源码时，Agent 使用 patch 类工具。
- 写入后一定能看到对应 diff。
- 最终回答包含 changed files 和验证/审查结果。

### Step 5：实现阶段状态机

目标：让 `Task-Understand-Plan-Execute-Evaluate` 成为代码中的状态，而不是只存在于系统提示词中。

建议阶段：

```text
Understand：解析任务、确认 workspace、生成 repo map
Plan：制定步骤和读取清单，不允许写文件
Execute：执行读写和命令，但受权限策略限制
Evaluate：查看 diff、运行验证、检查验收标准
Finalize：总结结果，不再允许修改类工具
```

实施内容：

- 在 `CodingAgent.run` 外层引入 phase loop。
- 每个 phase 只暴露允许的工具。
- phase 切换由程序判断，LLM 可以建议但不能单方面决定。

验收标准：

- Plan 阶段不会调用 `write_file`。
- Finalize 阶段不会再执行工具调用。
- 达到步数上限时能报告当前 phase 和未完成事项。

### Step 6：实现 ValidationPolicy

目标：让验证命令由程序根据任务和变更类型决定，而不是让 LLM 自由猜。

实施内容：

- README / 文档变更：优先做 diff review，可选 markdown 检查，不默认跑 `compileall`。
- Python 源码变更：优先 `python -m compileall` 和相关测试。
- 配置或依赖变更：根据项目类型选择 build/test，但可能需要用户确认。
- 高风险或耗时命令进入 review 队列。

验收标准：

- 修改 README 不再默认触发 Python 编译。
- 修改 Python 源码时至少有轻量级语法验证。
- 最终回答明确说明运行了什么验证，或为什么跳过验证。

### Step 7：实现结构化日志

目标：替代完整 dump `messages`，让实验可复盘、可比较、少泄密。

建议 JSONL 事件：

```json
{"type":"step_started","step":1,"phase":"Understand"}
{"type":"tool_call","step":1,"tool":"repo_map","args":{"path":"."}}
{"type":"tool_result_summary","step":1,"tool":"repo_map","summary":"12 files, 4 key files"}
{"type":"file_changed","path":"README.md","mode":"create"}
{"type":"validation_result","command":"git diff -- README.md","exit_code":0}
{"type":"task_finished","status":"success"}
```

实施内容：

- 新增 event logger。
- 默认只记录工具名、参数摘要、结果摘要、变更文件、验证结果。
- 原始工具结果可以可选落盘，但需要截断和脱敏。

验收标准：

- 日志能复盘每一步行动。
- 日志不会默认包含完整文件内容和敏感信息。
- 多次实验结果可以横向比较。

## 推荐落地顺序

建议按下面顺序迭代，不要一次性做完整 Agent 框架：

```text
1. WorkspaceResolver
2. RepoMap + IgnorePolicy
3. TaskSpec
4. Patch/Diff 编辑闭环
5. 阶段状态机
6. ValidationPolicy
7. 结构化日志
```

前三步解决“操作哪个项目、理解哪些文件、任务边界是什么”。

中间两步解决“如何安全修改、如何让流程可控”。

最后两步解决“如何验证、如何复盘和持续改进”。

## 对 README 生成任务的目标流程

```text
1. Resolve workspace
2. Build TaskSpec: create_readme
3. Build repo map with ignore policy
4. Detect project type from manifest files
5. Read manifest, entry files, existing docs and tests
6. Build evidence summary
7. Draft README from evidence
8. Create or patch README
9. Review git diff
10. Run task-appropriate validation
11. Finalize with changed files, validation result and remaining risks
```

## 判断标准

当完成上述改造后，Agent 应该具备以下行为特征：

- 不会因为用户 prompt 中路径表达不清而操作错目录。
- 不会把 `.venv`、`.git`、缓存目录塞给模型理解。
- 不会在计划阶段提前写文件。
- 不会修改已有文件却不展示 diff。
- 不会把 `max_steps` 当成任务完成标准。
- 不会让 LLM 无约束执行 shell 命令。
- 能说明最终产物基于哪些文件和证据。

