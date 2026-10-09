# 大型项目上下文治理总结

## 核心结论

大型项目的上下文问题，最终不是靠“把整个代码库塞进模型上下文”，也不是只靠更大的上下文窗口解决，而是靠一套上下文选择和治理机制解决：

```text
项目索引 -> 任务定位 -> 按需读取 -> 摘要压缩 -> 证据记录 -> diff / test feedback -> 必要时继续检索
```

成熟 coding agent 的关键能力不是“读得多”，而是“读得准、保留得少、反馈得快”。

## 第一性原理

模型上下文窗口本质上是短期工作记忆，不是代码库数据库。

对于大型项目，Agent 需要像程序员一样工作：

1. 先看项目结构，而不是直接读所有文件。
2. 根据任务判断相关区域。
3. 只读取当前任务需要的文件和片段。
4. 把读过的内容压缩成可复用摘要。
5. 修改最小必要范围。
6. 用 diff 和测试结果继续缩小问题。

所以真正要解决的是 `context selection`，而不是单纯扩大 `context size`。

## Codex / Claude Code 的共同思路

Codex 和 Claude Code 都不是一次性把整个 repo 全量喂给模型，而是通过工具循环、项目说明、权限模式、计划模式和上下文压缩来管理任务。

抽象后可以理解为：

```text
用户任务
  + 当前 workspace
  + 项目说明文件
  + 可用工具
  + 权限/模式
  + 文件检索结果
  + diff/test 反馈
  = 当前轮模型真正需要的上下文
```

它们的共同策略包括：

- 使用项目级说明文件承载稳定规则，例如 `AGENTS.md`、`CLAUDE.md`。
- 让模型通过工具按需读取文件，而不是预加载整个项目。
- 对大型代码库使用目录级规则、忽略规则、稀疏工作区或 code intelligence 降低上下文噪音。
- 在任务过程中用计划、diff、测试反馈不断调整 working set。
- 必要时通过 compact / summary / subagent 把大量探索压缩成简短结论。

## 上下文治理的分层方案

### 1. RepoMap：项目地图

第一步应该生成项目地图，而不是读取所有源码。

RepoMap 应包含：

```text
项目类型
manifest 文件
目录结构摘要
入口文件
测试目录
文档文件
关键模块
忽略目录
可能相关文件
```

示例：

```text
project_type: python
manifests:
  - pyproject.toml
packages:
  - agent/
  - cli/
  - model/
  - tools/
tests:
  - tests/
ignored:
  - .git/
  - .venv/
  - __pycache__/
  - *.egg-info/
```

### 2. TaskSpec：任务规格

不同任务需要完全不同的上下文。

示例：

```text
生成 README：
  读取 pyproject.toml、README.md、入口文件、核心目录结构、测试目录摘要

修复测试：
  读取失败日志、相关测试文件、被测模块、错误栈相关代码

重构模块：
  读取目标模块、调用方、接口定义、相关测试

解释项目：
  读取 manifest、目录结构、入口文件、README、核心模块摘要
```

因此上下文选择应由：

```text
user prompt -> TaskSpec -> ContextPlan -> read files
```

而不是：

```text
user prompt -> LLM 随机 list/read
```

### 3. ContextPlan：读取计划

ContextPlan 决定本轮最多读什么、先读什么、不能读什么。

最小结构可以是：

```python
@dataclass
class ContextPlan:
    repo_summary: str
    must_read: list[str]
    maybe_read: list[str]
    forbidden: list[str]
    max_files: int
    max_chars_per_file: int
```

README 任务示例：

```python
ContextPlan(
    repo_summary="Python CLI project with agent, cli, model, tools packages",
    must_read=[
        "pyproject.toml",
        "README.md",
        "cli/main.py",
        "agent/core.py",
        "tools/__init__.py",
    ],
    maybe_read=[
        "tools/filesystem.py",
        "tools/shell.py",
        "model/openai_chat.py",
        "tests/",
    ],
    forbidden=[
        ".env",
        ".venv/",
        ".git/",
        "__pycache__/",
    ],
    max_files=10,
    max_chars_per_file=12000,
)
```

### 4. 混合检索，而不是只靠向量库

大型代码库不能只靠 embedding / vector search。

代码任务通常需要精确关系：

```text
函数定义在哪里？
谁调用了它？
这个测试覆盖哪个模块？
错误栈对应哪一行？
接口在哪定义？
```

更合理的是混合检索：

```text
文件名匹配
路径匹配
ripgrep 关键词搜索
AST / symbol index
LSP / code intelligence
调用关系
测试失败栈
git diff
向量检索
LLM 判断
```

短期个人项目可以先做：

```text
RepoMap + 文件名/路径匹配 + rg 搜索 + TaskSpec 规则
```

后续再考虑 AST、LSP、embedding。

### 5. 原始信息与工作摘要分离

不要把所有工具结果永久塞进 `messages`。

应拆成三层：

```text
raw observations:
  工具原始输出，落盘保存，用于审计和复盘

working context:
  当前轮给模型看的短摘要

evidence table:
  任务相关事实、来源文件和置信度
```

例如读完 `pyproject.toml` 后，不必永久保留全文，可以压缩成：

```text
项目名：coding-agent
Python：>=3.11
依赖：openai
CLI 入口：coding-agent = cli.main:main
来源：pyproject.toml
```

### 6. 上下文随阶段变化

同一个任务在不同阶段需要不同上下文。

```text
Understand:
  repo map、manifest、入口文件摘要

Plan:
  TaskSpec、相关文件列表、约束条件

Execute:
  目标文件全文、相邻代码片段、计划

Evaluate:
  git diff、测试输出、错误日志

Finalize:
  changed files、验证结果、风险摘要
```

因此主上下文不应该无限增长，而应该随阶段重组。

### 7. 子任务隔离

大型项目里，可以把探索任务隔离出去。

例如主 Agent 需要修改认证逻辑，可以先派生一个探索任务：

```text
找出认证相关文件、调用链、测试入口，只返回摘要和文件列表。
```

探索过程可以读取很多文件，但主上下文只接收：

```text
相关文件：
- auth/service.py
- auth/middleware.py
- tests/test_auth.py

关键事实：
- login flow 从 middleware 进入
- token 校验在 service.validate_token
- 测试入口是 tests/test_auth.py
```

这样可以避免主上下文被大量无关内容污染。

### 8. Prompt caching 不是根本解法

Prompt caching 可以降低重复上下文的成本，但不能决定“应该读哪些代码”。

它解决的是：

```text
重复处理成本
```

不是：

```text
上下文选择质量
```

所以即使未来使用更大的上下文窗口或缓存机制，仍然需要 RepoMap、TaskSpec、ContextPlan 和摘要压缩。

## 对本项目的推荐设计

建议后续逐步引入这些对象：

```text
Workspace
  root
  ignore_policy
  repo_map

TaskSpec
  task_type
  target_files
  allowed_write_paths
  acceptance_criteria

ContextPlan
  must_read
  maybe_read
  forbidden
  max_files
  max_chars_per_file

ContextStore
  raw_tool_results
  file_summaries
  evidence_table
  active_context

AgentLoop
  understand
  plan
  execute
  evaluate
  finalize
```

## 最小可落地版本

短期可以先实现以下能力：

1. `repo_map` 工具：生成过滤后的项目地图。
2. `TaskSpec`：识别任务类型和目标文件。
3. `ContextPlan`：根据任务类型给出 must-read 文件列表。
4. `read_file` 增加读取预算：限制文件数和每个文件字符数。
5. `EvidenceTable`：记录最终回答所依据的事实来源。

## README 任务示例流程

```text
1. Resolve workspace
2. Build repo map
3. Build TaskSpec: create_readme
4. Build ContextPlan
5. Read must_read files
6. Summarize project facts into EvidenceTable
7. Draft README from evidence
8. Create or patch README
9. Review git diff
10. Finalize with changed files and evidence summary
```

## 一句话总结

大型项目上下文问题的最终解法是：

```text
不读全部，只读相关；
不保留全部，只保留摘要；
不让模型随机探索，而由程序构建 RepoMap、TaskSpec、ContextPlan；
不让主上下文无限膨胀，而用摘要、证据表、diff/test feedback 和子任务隔离管理上下文。
```
