# coding-agent

`coding-agent` 是一个轻量级的 Python 本地编码助手。它基于 OpenAI Chat Completions 的工具调用能力，可以在指定工作目录中帮助开发者阅读项目、修改文件、执行命令并总结结果。

项目目标不是复刻某个特定模型的内部机制，而是实现一个实用、可控的本地开发工作流。

## 功能特性

- 提供命令行工具 `coding-agent`；
- 支持指定工作目录执行任务；
- 支持通过命令行参数或环境变量配置模型；
- 支持兼容 OpenAI API 的自定义 Base URL；
- 内置常用工具：
  - `list_files`：列出工作区文件；
  - `read_file`：读取文件内容；
  - `write_file`：写入文件内容；
  - `run_command`：在工作区中执行 Shell 命令；
- 对文件路径访问做了工作区边界限制，降低误操作风险。

## 项目结构

```text
.
├── agent/              # Agent 主流程、系统提示词和执行循环
├── cli/                # 命令行入口
├── model/              # OpenAI Chat Completions 客户端封装
├── tools/              # 文件系统、Shell 和工具注册逻辑
├── tests/              # 测试或实验脚本
├── pyproject.toml      # Python 项目配置
└── README.md           # 项目说明文档
```

## 环境要求

- Python 3.11 或更高版本；
- OpenAI API Key 或兼容 OpenAI API 的服务；
- Python 依赖：`openai`。

## 安装

建议使用虚拟环境：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

配置 API Key：

```bash
export OPENAI_API_KEY=your_api_key_here
```

如果使用兼容 OpenAI 协议的第三方服务，可以配置 Base URL：

```bash
export OPENAI_BASE_URL=https://api.example.com/v1
```

## 使用方法

在当前目录运行：

```bash
coding-agent "Inspect this repo and summarize the project"
```

指定工作目录：

```bash
coding-agent --cwd /path/to/project "Add a usage section to README"
```

指定模型：

```bash
coding-agent --model gpt-4.1-mini "Run tests and summarize failures"
```

也可以通过环境变量指定默认模型：

```bash
export CODING_AGENT_MODEL=gpt-4.1-mini
coding-agent "Review the project structure"
```

从标准输入读取任务：

```bash
echo "List files and explain this project" | coding-agent
```

## 命令行参数

```text
coding-agent [OPTIONS] [PROMPT...]

Options:
  --cwd PATH        指定 Agent 操作的工作目录，默认为当前目录
  --model MODEL     指定使用的模型，默认读取 CODING_AGENT_MODEL 或 gpt-4.1-mini
  --base-url URL    指定兼容 OpenAI API 的服务地址，默认读取 OPENAI_BASE_URL
  --max-steps N     Agent 最大循环步数，默认 12
```

## 工作流程

`coding-agent` 的典型执行流程如下：

1. CLI 解析用户输入、工作目录和模型配置；
2. 创建 `CodingAgent` 并注册可用工具；
3. Agent 将系统提示词、用户任务和工具定义发送给模型；
4. 模型按需发起工具调用；
5. Agent 执行工具并将结果返回给模型；
6. 循环执行，直到模型给出最终回答或达到最大步数。

## 安全说明

当前项目实现了基础安全边界：

- 文件读写路径限制在当前工作区内；
- 文件读取和命令输出有长度限制；
- Shell 命令执行有超时时间限制；
- 系统提示词要求 Agent 避免破坏性命令，除非用户明确要求。

需要注意的是，`run_command` 仍然可以执行 Shell 命令。请仅在可信工作区中运行，并谨慎处理高风险任务。

## 开发与验证

安装为可编辑模式后，可以直接运行：

```bash
coding-agent --cwd . "List files in this project"
```

如后续补充正式测试，可以使用：

```bash
pytest
```

## 后续计划

- 增加基于 diff/patch 的文件编辑能力；
- 增加高风险命令确认机制；
- 增加工具调用过程的可视化输出；
- 增加持久化会话和任务恢复能力；
- 完善测试用例，覆盖工具安全和 Agent 消息格式。
