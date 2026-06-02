# MCP Server: Image Reader Bridge

将 mimo-v2.5 的图片理解能力封装为 Claude Code 可调用的 MCP Tool。

## 工作原理

```
用户发图给 mimo-v2.5-pro
  → mimo-v2.5-pro 调用 MCP tool "describe_image"
    → MCP Server 读取本地图片文件，base64 编码
    → 调用 mimo-v2.5 API（同一代理，model 改为 mimo-v2.5）
    → 返回文字描述
  → mimo-v2.5-pro 基于描述继续对话
```

## 文件结构

```
~/.claude/
├── .claude.json                       ← MCP Server 注册配置（mcpServers 字段）
└── mcp-servers/
    └── image-reader/
        ├── server.py                  ← MCP Server 主程序
        └── README.md                  ← 本文件
```

## 技术栈

- Python 3.9（仅用标准库，零外部依赖）
- MCP 协议（JSON-RPC 2.0 over stdio）
- Anthropic Messages API（多模态图片格式）

## 安装/配置

### 1. 创建目录和文件

目录：`~/.claude/mcp-servers/image-reader/`

主程序：`server.py`（约 250 行，包含完整注释）

### 2. 注册 MCP Server

编辑 `~/.claude.json`，在顶层添加 `mcpServers` 字段（如果已有则合并）：

```json
{
  "mcpServers": {
    "image-reader": {
      "command": "python",
      "args": [
        "C:/Users/<你的用户名>/.claude/mcp-servers/image-reader/server.py"
      ],
      "env": {
        "MIMO_API_KEY": "你的 API Key",
        "MIMO_BASE_URL": "https://token-plan-cn.xiaomimimo.com/anthropic"
      }
    }
  }
}
```

> **注意**：`~/.claude.json` 是 Claude Code 的全局配置文件，可能已有其他内容。只需在其中添加 `mcpServers` 字段即可，不要覆盖整个文件。

### 3. 重启 Claude Code

关闭并重新打开 Claude Code，MCP Server 会自动加载。

## 使用方法

直接让 Claude 读取图片即可：

- "请读取桌面上的 skills.png 图片"
- "帮我看看这张截图的内容"
- "描述一下 C:/path/to/image.jpg"

Claude 会自动调用 `describe_image` tool。

### 工具参数

| 参数 | 必填 | 说明 |
|------|------|------|
| `image_path` | 是 | 图片文件的绝对路径 |
| `prompt` | 否 | 自定义提示语，默认为"请详细描述这张图片的内容" |

## 卸载方法

### 步骤 1：删除 MCP 配置

编辑 `~/.claude.json`，从 `mcpServers` 中删除 `image-reader` 条目：

```diff
  {
    "mcpServers": {
-     "image-reader": {
-       "command": "python",
-       "args": ["你的路径/server.py"],
-       "env": { ... }
-     }
    }
  }
```

### 步骤 2：删除项目目录

```bash
rm -rf ~/.claude/mcp-servers/image-reader/
```

如果 `mcp-servers/` 下没有其他内容，也可以删除整个目录：

```bash
rm -rf ~/.claude/mcp-servers/
```

### 步骤 3：重启 Claude Code

关闭并重新打开 Claude Code，MCP Server 将不再加载。

**卸载后无残留**：无后台进程、无注册表修改、无其他文件。

## 故障排查

| 问题 | 可能原因 | 排查方法 |
|------|---------|---------|
| Claude 没有调用 describe_image | MCP 配置路径错误 | 检查 `~/.claude.json` 中的绝对路径 |
| tool 调用报错 "file not found" | 图片路径问题 | 确认使用绝对路径 |
| API 返回 401 | API Key 错误 | 检查 `~/.claude.json` 中的 MIMO_API_KEY |
| API 返回 403 | 模型不可用 | 确认代理支持 mimo-v2.5 模型 |
| MCP Server 启动失败 | Python 路径问题 | 终端运行 `python --version` 确认 |
| UnicodeEncodeError | 编码问题 | server.py 已处理 Windows GBK 编码 |

## MCP 协议学习笔记

### 通信方式
- stdin 接收请求，stdout 发送响应
- 每条消息是独立的一行 JSON
- stderr 用于日志输出（不影响协议）

### 消息类型
- **Request**（有 `id` 字段）：必须回复
- **Notification**（无 `id` 字段）：静默处理，不回复

### 核心方法
| 方法 | 作用 |
|------|------|
| `initialize` | 握手，返回服务器能力声明 |
| `notifications/initialized` | 客户端确认 |
| `tools/list` | 返回可用工具列表 |
| `tools/call` | 执行工具调用 |
