# Codex / Claude Code 的动态项目理解系统

## 核心结论

Codex 和 Claude Code 并不是靠一个静态的 `repo_map()` 解决大型项目理解问题。

它们更像是在运行一个动态项目理解系统：

```text
workspace root
+ 项目规则文件
+ ignore-aware 文件发现
+ 搜索 / 文件读取工具
+ 代码智能
+ 上下文压缩
+ 子任务隔离
+ diff / test feedback
= 当前任务所需的 working set
```

也就是说，成熟 coding agent 不会一次性把整个 repo 塞进上下文，而是在任务过程中持续选择、读取、压缩和更新上下文。

## 和当前 repo_map 的关系

当前项目中的 `repo_map` 是第一层能力：

```text
file discovery + coarse project summary
```

它可以告诉 Agent：

- 当前 workspace 是什么。
- 项目大概是什么类型。
- 有哪些 manifest 文件。
- README / docs 在哪里。
- 源码目录和测试目录在哪里。
- 有哪些入口脚本。
- 哪些文件应该展示给模型。

但这还不是完整项目理解。真正成熟的系统还需要：

```text
RepoMap -> SearchIndex -> ContextPlan -> EvidenceTable -> Execute/Evaluate feedback
```

## Codex 的思路

从公开信息和 Codex CLI 的工作方式看，Codex 的项目理解更接近“工具驱动的按需检索”。

### 1. workspace 是显式上下文

Codex 通常从用户当前项目目录启动，任务天然绑定到当前 workspace。

这避免了让模型从自然语言中猜测要操作哪个项目。

对应到本项目：

```text
--cwd 是权威 workspace
prompt 中的路径只是候选信号
工具只能在 workspace 内工作
```

### 2. 项目说明文件提供稳定规则

Codex 使用类似 `AGENTS.md` 的项目说明文件，为模型提供：

- 项目约定
- 测试方式
- 代码风格
- 目录说明
- 任务注意事项

这些说明文件不是完整代码上下文，而是长期稳定的项目规则。

对应到本项目：

```text
后续可以支持 AGENTS.md / CLAUDE.md / PROJECT.md
但必须限制大小、作用范围和优先级
```

### 3. ignore-aware 文件发现

Codex 的项目文件发现不是简单递归目录，而是尊重 `.gitignore` 和常见忽略规则。

它更接近：

```text
git ls-files
rg --files
ignore-aware traversal
fuzzy file search
```

对应到本项目：

```text
repo_map 应优先用 git ls-files
fallback 遍历也要在进入目录前剪枝
不要把 .venv / node_modules / __pycache__ 暴露给模型
```

### 4. 按需搜索，而不是一次性全读

Codex 更依赖 `rg`、文件搜索、精确读取、patch、diff 等工具组合。

它不是先读完整项目，而是：

```text
先找相关文件
再读关键文件
再修改最小范围
再用 diff / test 反馈继续定位
```

对应到本项目：

```text
repo_map 之后应该新增 search_files / grep_code
不要让 LLM 靠 list_files 一层层随机探索
```

### 5. sandbox / approval 约束动作

Codex 的项目理解不只关心“有哪些文件”，还关心“哪些动作可以做”。

例如：

- 哪些文件可读
- 哪些文件可写
- 哪些命令安全
- 哪些命令需要确认
- 是否允许网络访问

对应到本项目：

```text
TaskSpec 应控制 allowed_read_paths / allowed_write_paths / shell_allowed
repo_map 是理解输入，不是权限本身
```

## Claude Code 的思路

Claude Code 对大型代码库的处理更强调上下文治理和范围裁剪。

### 1. 大 repo 需要限制工作范围

大型项目中，文件读取、工具输出、说明文件和对话都会占用上下文。

Claude Code 的核心思路是让模型聚焦任务相关区域，而不是让它在整个 repo 中自由探索。

对应到本项目：

```text
TaskSpec + ContextPlan 应该决定本轮读哪些文件
repo_map 只提供候选，不应该直接触发全量读取
```

### 2. 分层项目说明

Claude Code 使用 `CLAUDE.md` / `AGENTS.md` / memory 这类机制保存项目规则。

这些文件可以出现在不同目录层级，用来给局部代码提供额外说明。

对应到本项目：

```text
后续可以支持分层项目规则：
root AGENTS.md
子目录 AGENTS.md
任务相关目录说明
```

但需要注意：

```text
项目规则是 context，不是代码全文
规则文件也要有大小预算
```

### 3. code intelligence

Claude Code 在大型项目中会借助代码智能能力，而不是只靠文本搜索。

代码智能包括：

- 找定义
- 找引用
- 找调用方
- 找类型信息
- 找测试关联

对应到本项目的长期方向：

```text
短期：rg / 文件名搜索
中期：AST / ctags
长期：LSP / language server
```

### 4. 子任务隔离

大型探索任务会污染主上下文。

Claude Code 的一个重要思路是：把大规模探索放进独立上下文，只把摘要带回主会话。

示例：

```text
主任务：修改认证逻辑
子任务：找出认证相关文件、调用链和测试入口
子任务输出：相关文件列表 + 关键事实摘要
主上下文：只接收摘要，不接收所有原始文件内容
```

对应到本项目：

```text
可以先不做多 Agent
但要设计 ContextStore，把 raw observations 和 working summary 分开
```

### 5. compact / summary

Claude Code 会通过压缩历史和摘要减少上下文膨胀。

对应到本项目：

```text
不要把所有 read_file 结果永久留在 messages
读过的文件应该变成 FileSummary / EvidenceTable
```

## 动态项目理解系统的抽象模型

可以把成熟 coding agent 的项目理解拆成以下层次。

### Layer 1：Workspace

```text
root
ignore_policy
project_rules
```

作用：确定操作边界。

### Layer 2：RepoMap

```text
project_type
manifests
docs
source_dirs
test_dirs
entry_points
important_files
```

作用：知道项目大概长什么样。

### Layer 3：SearchIndex

```text
file search
keyword search
symbol search
reference search
```

作用：根据任务找到相关文件。

### Layer 4：ContextPlan

```text
must_read
maybe_read
forbidden
max_files
max_chars_per_file
```

作用：决定本轮要读什么，读多少。

### Layer 5：ContextStore

```text
raw_tool_results
file_summaries
evidence_table
active_context
```

作用：区分原始信息、工作摘要和最终证据。

### Layer 6：Feedback Loop

```text
git diff
test output
lint output
runtime errors
user feedback
```

作用：让 Agent 根据真实反馈继续缩小问题范围。

## 对当前项目的实现建议

不要把 `repo_map` 做成万能工具。

更合理的演进路线是：

```text
1. repo_map：生成项目粗地图
2. search_files / grep_code：根据任务检索相关文件
3. TaskSpec：明确任务类型、目标文件和权限
4. ContextPlan：决定本轮读取计划
5. EvidenceTable：记录事实来源
6. diff/test feedback：驱动后续探索
```

## 下一步优先级

### P0：结构化 RepoMap

当前 `repo_map` 返回纯文本，适合给 LLM 看，但不利于程序消费。

下一步应改成：

```text
内部 RepoMap dataclass
+ format_for_llm()
```

这样 TaskSpec / ContextPlan 可以直接使用结构化结果。

### P0：搜索工具

新增：

```text
search_files(pattern)
grep_code(query)
```

短期底层可以用：

```text
rg --files
rg <query>
```

作用是让 Agent 不再靠 `list_files` 随机探索。

### P1：项目规则文件

支持读取：

```text
AGENTS.md
CLAUDE.md
PROJECT.md
```

要求：

```text
限制大小
标记来源
按目录作用域加载
```

### P1：ContextPlan

基于 TaskSpec 和 RepoMap 生成读取计划。

例如 README 任务：

```text
must_read:
- pyproject.toml
- README.md
- cli/main.py
- agent/core.py
- tools/__init__.py

maybe_read:
- tools/filesystem.py
- tools/shell.py
- model/openai_chat.py
- tests/
```

### P2：ContextStore / EvidenceTable

让文件读取不再直接永久污染 messages。

```text
read_file -> raw result -> summary -> evidence table -> active context
```

## 对当前 repo_map 的定位

当前 `repo_map` 应该被视为：

```text
动态项目理解系统的入口层
```

它解决的是：

```text
项目里有什么？
哪些文件明显重要？
哪些目录不应该让模型浪费上下文？
```

它不应该解决：

```text
这个任务具体要读哪些文件？
这些文件之间的调用关系是什么？
修改哪里最安全？
上下文超预算后如何压缩？
```

这些问题应该交给后续的 `SearchIndex`、`ContextPlan`、`EvidenceTable` 和 Evaluate 阶段。

## 一句话总结

Codex / Claude Code 的 repo map 不是一个静态目录清单，而是一套动态项目理解机制：

```text
显式 workspace
+ 项目规则文件
+ ignore-aware 文件发现
+ 搜索 / 代码智能
+ 上下文预算
+ 摘要压缩
+ 子任务隔离
+ diff / test 反馈
```

当前项目已经有了第一层 `repo_map`，下一步应该把它结构化，并补上搜索工具，让它成为 `TaskSpec` 和 `ContextPlan` 的可靠输入。
