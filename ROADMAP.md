# coding-agent Implementation Roadmap

## 总体判断

这个项目不应该以完整复刻 Codex 或 Claude Code 为目标，而应该以实现一个个人可用、可控、可复盘的本地编码 Agent 为目标。

现实目标是达到个人日常编码场景中约 60%–75% 的成熟编码 Agent 体感：

- 能理解当前项目结构。
- 能制定短计划。
- 能安全修改文件。
- 能查看 diff。
- 能运行合适的验证。
- 能总结变更和剩余风险。

不建议短期追求完整 UI、多 Agent、云端任务、复杂权限系统或 IDE 深度集成。

## 第一性原理

一个可用的 coding agent 本质上是一个控制系统：

```text
任务目标 -> 环境建模 -> 行动选择 -> 执行动作 -> 观测反馈 -> 质量评估 -> 停止/继续
```

当前项目最大的问题不是模型调用，而是控制面不足：

```text
模型直接驱动低阶工具，程序缺少任务状态、项目地图、编辑审查、验证策略和停止条件。
```

后续架构应围绕以下流程演进：

```text
Task -> Understand -> Plan -> Execute -> Evaluate -> Finalize
```

## 时间预估

### 2–3 天

可以从 demo 变成谨慎可用的个人工具。

目标：

- workspace 明确。
- 项目文件过滤。
- 基础 patch 编辑。
- 写后 diff。

### 5–10 天

可以达到个人日常小项目中较可用的水平。

目标：

- README / 文档生成稳定可用。
- 单文件代码修改较可靠。
- 简单测试修复可用。
- 输出变更摘要和验证结果。

### 2–4 周

可以形成较稳定的个人编码 Agent。

目标：

- 多文件小重构可用。
- 项目理解更稳定。
- 有阶段状态机。
- 有结构化日志和回归实验。

### 难以由个人完全追平的部分

- 模型训练质量。
- 产品级长上下文系统。
- 云端隔离执行环境。
- 多 IDE / 多端体验。
- 团队权限、审计和协作能力。
- 大规模真实任务评估体系。

## 核心实施路径

### Phase 1：RepoMap + IgnorePolicy

目标：让 Agent 先看到干净、可控的项目地图。

要做：

- 新增 `repo_map` 工具。
- 优先使用 `git ls-files` 获取项目文件。
- 非 git 项目使用遍历，但应用 ignore 规则。
- 默认忽略：
  - `.git/`
  - `.venv/`
  - `venv/`
  - `__pycache__/`
  - `*.egg-info/`
  - `node_modules/`
  - `build/`
  - `dist/`
  - `.pytest_cache/`
  - `.mypy_cache/`
- 标记关键文件：
  - `pyproject.toml`
  - `package.json`
  - `go.mod`
  - `README.md`
  - 入口文件
  - 测试目录

验收标准：

- LLM 不再直接看到 `.git/`、`.venv/`、缓存目录。
- README 任务会优先读取 manifest、入口文件、已有文档和测试目录。
- 大项目不会无控制地全量读取文件。

### Phase 2：TaskSpec

目标：把自然语言任务转成结构化任务规格。

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

要做：

- 用规则识别常见任务类型。
- 允许 LLM 辅助补全 TaskSpec。
- 程序层校验 `workspace`、`target_files`、`allowed_write_paths`。
- 后续工具执行参考 TaskSpec，而不是完全依赖模型自觉。

验收标准：

- README 任务能明确目标文件和写入范围。
- 解释类任务不会开放写文件工具。
- 最终完成判断能对照 `acceptance_criteria`。

### Phase 3：Patch / Replace Editing

目标：从“会写文件”升级为“会安全修改代码”。

要做：

- 保留 `write_file`，仅用于创建新文件或用户明确要求覆盖。
- 新增 `apply_patch` 或 `replace_text`。
- 修改已有文件时默认使用 patch / replace。
- patch 失败时返回明确错误，要求模型重新读取文件后再试。

验收标准：

- 修改已有文件不再默认整文件覆盖。
- patch 结果可被 diff 审查。
- 单文件修改任务可稳定完成。

### Phase 4：Git Diff Review

目标：让每次修改都可见、可审查。

要做：

- 新增 `git_diff` 工具。
- 写入文件后自动执行 `git diff -- <file>`。
- diff 作为 Evaluate 阶段输入。
- 最终回答列出 changed files。

验收标准：

- 每次文件修改后都有 diff。
- Agent 能基于 diff 判断是否改错。
- 用户最终能清楚看到改了哪些文件。

### Phase 5：ValidationPolicy

目标：让验证命令由程序根据任务和变更类型选择，而不是让 LLM 随意猜。

策略：

- 文档变更：diff review，可选 markdown 检查，不默认跑 `compileall`。
- Python 源码变更：优先 `python -m compileall` 和相关测试。
- 配置或依赖变更：根据项目类型选择 build/test，必要时要求确认。
- 高风险命令进入 review 队列。

验收标准：

- 修改 README 不再默认触发 Python 编译。
- 修改 Python 源码时至少有轻量级语法验证。
- 最终回答说明运行了什么验证，或为什么跳过验证。

### Phase 6：Structured Event Log

目标：让任务过程可复盘、可比较、少泄密。

不要继续 dump 完整 `messages`。

建议 JSONL 事件：

```json
{"type":"step_started","step":1,"phase":"Understand"}
{"type":"tool_call","step":1,"tool":"repo_map","args":{"path":"."}}
{"type":"tool_result_summary","step":1,"tool":"repo_map","summary":"12 files, 4 key files"}
{"type":"file_changed","path":"README.md","mode":"create"}
{"type":"validation_result","command":"git diff -- README.md","exit_code":0}
{"type":"task_finished","status":"success"}
```

验收标准：

- 能复盘每一步行动。
- 日志默认不包含完整文件内容和敏感信息。
- 多次实验可以横向比较。

### Phase 7：Phase State Machine

目标：让 `Task -> Understand -> Plan -> Execute -> Evaluate -> Finalize` 成为代码中的状态。

阶段约束：

- `Understand`：允许 `repo_map`、`read_file`。
- `Plan`：只产出计划，不允许写文件。
- `Execute`：允许 patch、create file、低风险命令。
- `Evaluate`：允许 diff 和验证命令。
- `Finalize`：不再允许修改类工具。

验收标准：

- Plan 阶段不会写文件。
- Finalize 阶段不会再调用工具。
- 达到上限时能报告当前阶段和未完成事项。

## 推荐假期计划

### Day 1

完成：

- `RepoMap + IgnorePolicy`
- 基础测试
- README 任务实验

### Day 2

完成：

- `TaskSpec`
- README / explain / modify code 三类任务识别
- allowed write paths

### Day 3

完成：

- `apply_patch` 或 `replace_text`
- 禁止默认整文件覆盖已有文件
- patch 失败反馈

### Day 4

完成：

- `git_diff`
- 写后自动 diff
- 最终 changed files 输出

### Day 5

完成：

- `ValidationPolicy`
- 文档、Python 源码、配置文件三类验证策略

### Day 6

完成：

- 结构化事件日志
- 实验记录格式
- 日志脱敏和截断

### Day 7

完成：

- 用 5 个真实任务试跑
- 记录失败原因
- 修 prompt、工具边界和停止条件

### Day 8–10

完成：

- 阶段状态机雏形
- 补测试
- 清理文档
- 形成第二轮实验报告

## README 生成任务的目标流程

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

## 可达效果预估

完成上述核心能力后，预期可以达到：

```text
小项目 README / 文档生成：80% 可用
单文件代码修改：70% 可用
多文件小重构：50%–60% 可用
测试失败定位和修复：50% 可用
大型项目理解：30%–40% 可用
复杂架构改造：仍然不稳
```

## 不建议短期投入的方向

- 复杂 UI。
- 多 Agent。
- 云端任务执行。
- 完整 MCP 生态。
- 复杂长期记忆系统。
- IDE 深度集成。
- 过早迁移完整 Responses API 架构。

这些方向有价值，但当前瓶颈是 Agent 控制器，不是入口形态。

## 近期最重要的四件事

如果假期只能完成一组能力，优先完成：

```text
RepoMap + TaskSpec + Patch/Diff + ValidationPolicy
```

完成这四项后，项目会从“会调用工具的 LLM”明显升级为“能帮自己干活的本地编码 Agent”。

