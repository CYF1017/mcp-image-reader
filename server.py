#!/usr/bin/env python3
"""
MCP Server: Image Reader Bridge
================================
将 mimo-v2.5 的图片理解能力封装为 Claude Code 可调用的 MCP Tool。

工作原理：
  1. Claude Code 通过 stdin 发送 JSON-RPC 2.0 请求
  2. 本 Server 处理请求，对于 describe_image 工具调用：
     - 读取本地图片文件
     - base64 编码
     - 调用 mimo-v2.5 API 获取图片描述
  3. 通过 stdout 返回 JSON-RPC 2.0 响应

技术要点：
  - 纯 Python 3.9 标准库，零外部依赖
  - MCP 协议基于 JSON-RPC 2.0 over stdio
  - API 调用使用 Anthropic Messages API 格式
"""

import json
import sys
import os
import io
import base64
import mimetypes
import urllib.request
import urllib.error

# Windows 下 stdout/stderr 默认是 GBK 编码，无法输出 Unicode 字符
# 强制使用 UTF-8，确保中文和特殊字符（如 ⚠️）能正常输出
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# ============================================================
# 第一部分：配置
# ============================================================
# API 凭证从环境变量读取（由 Claude Code settings.json 的 env 字段注入）
# 这样做的好处：更换 API Key 时只需改配置，不用改代码

API_KEY = os.environ.get("MIMO_API_KEY", "")
BASE_URL = os.environ.get("MIMO_BASE_URL", "https://api.anthropic.com")
MODEL = "mimo-v2.5"  # 使用支持图片的非 pro 版本

# ============================================================
# 第二部分：MCP 协议层（JSON-RPC 2.0 over stdio）
# ============================================================
# MCP 协议的核心非常简单：
#   - 从 stdin 逐行读取 JSON 对象（每个对象占一行）
#   - 根据 method 字段路由到对应处理函数
#   - 通过 stdout 输出 JSON 响应
#
# 关键区分：
#   - Request：有 id 字段，必须回复
#   - Notification：无 id 字段，不需要回复（静默处理）

def log(msg):
    """调试日志，输出到 stderr（不影响 stdout 的协议通信）"""
    sys.stderr.write(f"[image-reader] {msg}\n")
    sys.stderr.flush()


def read_message():
    """
    从 stdin 读取一条 JSON-RPC 消息。
    MCP 协议规定每条消息是独立的一行 JSON，以换行符结尾。
    """
    line = sys.stdin.readline()
    if not line:
        return None  # stdin 关闭，进程应退出
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError as e:
        log(f"JSON 解析错误: {e}")
        return None


def send_response(response):
    """
    通过 stdout 发送一条 JSON-RPC 响应。
    注意：必须用 sys.stdout.write + flush，不能用 print（避免额外换行）。
    """
    raw = json.dumps(response, ensure_ascii=False)
    sys.stdout.write(raw + "\n")
    sys.stdout.flush()


def make_result(id, result):
    """构造成功的 JSON-RPC 响应"""
    return {"jsonrpc": "2.0", "id": id, "result": result}


def make_error(id, code, message):
    """构造错误的 JSON-RPC 响应"""
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}


# ============================================================
# 第三部分：MCP 请求处理函数
# ============================================================

def handle_initialize(id, params):
    """
    处理 initialize 请求。
    这是 Claude Code 启动时发送的第一个请求，用于握手。
    我们需要返回服务器的能力声明。

    关键字段：
      - protocolVersion：必须与客户端协商（这里直接回传客户端的版本）
      - capabilities：声明本服务器支持的能力（tools）
      - serverInfo：服务器的名称和版本
    """
    client_version = params.get("protocolVersion", "2024-11-05")
    return make_result(id, {
        "protocolVersion": client_version,
        "capabilities": {
            "tools": {}  # 声明本服务器提供工具能力
        },
        "serverInfo": {
            "name": "image-reader",
            "version": "1.0.0"
        }
    })


def handle_tools_list(id, params):
    """
    处理 tools/list 请求。
    返回本服务器提供的所有工具定义。
    Claude Code 根据这个列表决定何时调用哪个工具。

    工具定义包含：
      - name：工具的唯一标识符
      - description：告诉 Claude 这个工具做什么、何时使用
      - inputSchema：参数的 JSON Schema，Claude 会按此格式传参
    """
    return make_result(id, {
        "tools": [
            {
                "name": "describe_image",
                "description": (
                    "读取图片文件并返回图片内容的详细描述。当用户请求涉及以下任何情况时必须使用：读取图片、查看图片、打开图片、分析图片、"
                    "描述图片、识别图片内容、理解图片、看图片、图片里有什么、这是什么图片、告诉/说明图片内容。只要请求与图片相关（如"
                    ".png/.jpg/.jpeg/.gif/.webp/.bmp 文件，或提到'图片'、'图'、'截图'、'照片'、'这张图'），就必须调用此工具。不要尝试自行描"
                    "述图片，必须先通过此工具获取图片信息。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "image_path": {
                            "type": "string",
                            "description": "图片文件的绝对路径"
                        },
                        "prompt": {
                            "type": "string",
                            "description": "可选的自定义提示语，默认为'请详细描述这张图片的内容'",
                            "default": "请详细描述这张图片的内容，包括文字、布局、颜色等所有可见信息。"
                        }
                    },
                    "required": ["image_path"]
                }
            }
        ]
    })


def handle_tools_call(id, params):
    """
    处理 tools/call 请求。
    这是实际执行工具的地方。

    params 结构：
      - name：要调用的工具名
      - arguments：工具的参数（对应 inputSchema）
    """
    tool_name = params.get("name")
    arguments = params.get("arguments", {})

    if tool_name != "describe_image":
        return make_error(id, -32602, f"未知工具: {tool_name}")

    image_path = arguments.get("image_path", "")
    prompt = arguments.get("prompt", "请详细描述这张图片的内容，包括文字、布局、颜色等所有可见信息。")

    # 参数校验
    if not image_path:
        return make_error(id, -32602, "缺少必要参数: image_path")

    if not os.path.isfile(image_path):
        return make_error(id, -32602, f"文件不存在: {image_path}")

    # 调用图片处理流程
    description = process_image(image_path, prompt)

    if description is None:
        return make_error(id, -32000, "图片处理失败，请检查日志")

    # MCP tool 响应格式：content 数组，每个元素有 type 和对应内容
    return make_result(id, {
        "content": [
            {
                "type": "text",
                "text": description
            }
        ]
    })


# ============================================================
# 第四部分：图片处理 + API 调用
# ============================================================

def process_image(image_path, prompt):
    """
    完整的图片处理流程：
    1. 读取图片文件
    2. 检测 MIME 类型
    3. base64 编码
    4. 调用 mimo-v2.5 API
    5. 返回文字描述
    """
    # 1. 读取图片
    log(f"读取图片: {image_path}")
    try:
        with open(image_path, "rb") as f:
            image_data = f.read()
    except IOError as e:
        log(f"读取文件失败: {e}")
        return None

    # 2. 检测 MIME 类型
    # mimetypes 根据文件扩展名猜测类型
    mime_type, _ = mimetypes.guess_type(image_path)
    if mime_type is None:
        # 无法识别时的兜底：读取文件头魔数
        mime_type = detect_mime_from_header(image_data)
    if mime_type is None:
        mime_type = "image/png"  # 最终兜底
    log(f"MIME 类型: {mime_type}")

    # 3. base64 编码
    b64_data = base64.b64encode(image_data).decode("utf-8")
    log(f"base64 编码完成，长度: {len(b64_data)}")

    # 4. 调用 API
    description = call_mimo_api(b64_data, mime_type, prompt)
    return description


def detect_mime_from_header(data):
    """
    通过文件头魔数检测图片类型（兜底方案）。
    常见图片格式的魔数：
      PNG:  \\x89PNG
      JPEG: \\xff\\xd8\\xff
      GIF:  GIF8
      WebP: RIFF....WEBP
    """
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return "image/png"
    if data[:3] == b'\xff\xd8\xff':
        return "image/jpeg"
    if data[:4] == b'GIF8':
        return "image/gif"
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return "image/webp"
    return None


def call_mimo_api(b64_data, mime_type, prompt):
    """
    调用 mimo-v2.5 的 Anthropic Messages API。

    重点理解：
      - 使用 Anthropic 的多模态消息格式（不是纯文本）
      - message.content 是一个数组，可以包含 text 和 image 两种类型的块
      - image 块使用 base64 编码的图片数据
    """
    url = f"{BASE_URL}/v1/messages"

    headers = {
        "x-api-key": API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }

    body = {
        "model": MODEL,
        "max_tokens": 4096,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime_type,
                            "data": b64_data
                        }
                    },
                    {
                        "type": "text",
                        "text": prompt
                    }
                ]
            }
        ]
    }

    log(f"调用 API: {url}, model={MODEL}")
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        # Anthropic Messages API 响应格式：
        # { "content": [{ "type": "text", "text": "描述..." }], ... }
        text_blocks = [b for b in result.get("content", []) if b.get("type") == "text"]
        if text_blocks:
            description = text_blocks[0]["text"]
            log(f"API 返回描述，长度: {len(description)}")
            return description
        else:
            log("API 响应中没有 text 块")
            return None

    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        log(f"API HTTP 错误 {e.code}: {error_body}")
        return None
    except Exception as e:
        log(f"API 调用异常: {e}")
        return None


# ============================================================
# 第五部分：主循环（事件循环）
# ============================================================

def main():
    """
    MCP Server 的主循环。

    流程：
    1. 从 stdin 读取一条消息
    2. 判断是 Request（有 id）还是 Notification（无 id）
    3. 根据 method 字段路由到对应处理函数
    4. Request 需要回复，Notification 静默处理
    5. 重复，直到 stdin 关闭
    """
    log("Image Reader MCP Server 启动")

    while True:
        message = read_message()
        if message is None:
            # stdin 关闭或读取失败，退出
            break

        method = message.get("method", "")
        id = message.get("id")  # None 表示 Notification
        params = message.get("params", {})

        log(f"收到消息: method={method}, id={id}")

        # ---------- Request 路由（有 id，需要回复）----------
        if id is not None:
            if method == "initialize":
                send_response(handle_initialize(id, params))

            elif method == "tools/list":
                send_response(handle_tools_list(id, params))

            elif method == "tools/call":
                send_response(handle_tools_call(id, params))

            else:
                # 未知方法，返回 method not found 错误
                send_response(make_error(id, -32601, f"未知方法: {method}"))

        # ---------- Notification 处理（无 id，不回复）----------
        else:
            if method == "notifications/initialized":
                log("客户端已确认初始化")
            elif method == "notifications/cancelled":
                log("请求被取消")
            else:
                log(f"忽略未知 notification: {method}")

    log("Image Reader MCP Server 关闭")


# Python 入口
if __name__ == "__main__":
    main()
