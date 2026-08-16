# /ask 知识库外部 API 对接说明

## 功能简介

MC 内使用 `/ask <问题>` 指令时，插件按以下顺序检索答案：

1. **AstrBot 内置知识库**（通过 `context` API 自动检索已注册的知识库/向量 provider）
2. **外部知识库 API**（本文档所述的 HTTP 接口）
3. **LLM 回退**（前两者都未找到时，由 LLM 基于上下文生成回复）

检索结果通过 `tellraw` 私发给提问玩家，同时存入会话历史和 memory_companion。

## 配置项

在 AstrBot WebUI 的插件配置页面中设置：

| 配置项 | 类型 | 默认值 | 说明 |
|:-------|:-----|:-------|:-----|
| `ENABLE_ASK_COMMAND` | bool | `true` | `/ask` 指令总开关 |
| `ENABLE_BUILTIN_KB` | bool | `true` | 是否查询 AstrBot 内置知识库 |
| `ENABLE_ASK_LLM_FALLBACK` | bool | `true` | 知识库都未命中时是否回退到 LLM |
| `KB_EXTERNAL_API_URL` | string | `""` | 外部知识库 API 地址，留空则跳过 |
| `KB_EXTERNAL_API_TOKEN` | string | `""` | Bearer Token 鉴权，留空则不携带 |
| `KB_EXTERNAL_TIMEOUT` | int | `10` | 请求超时时间（秒） |

## MC 端使用

在游戏内输入：

```
/ask 怎么合成钻石镐
/ask 下界要塞怎么找
/ask 服务器有什么规则
```

- 无冷却时间，随时可查询
- 回复通过 `tellraw` 私发给提问者，其他玩家看不到
- 问答内容会自动存入会话历史和 memory_companion

## 外部 API 接口规范

### 请求

```
POST <KB_EXTERNAL_API_URL>
Content-Type: application/json
Authorization: Bearer <KB_EXTERNAL_API_TOKEN>   (可选)
```

**请求体 (JSON):**

```json
{
  "query": "怎么合成钻石镐",
  "question": "怎么合成钻石镐"
}
```

> `query` 和 `question` 字段内容相同，同时发送以兼容不同 API 的字段命名习惯。

### 响应

**HTTP 200**，返回 JSON。插件兼容以下响应格式：

#### 格式 1: 直接返回答案字符串

```json
{
  "answer": "钻石镐需要3个钻石和2根木棍，在工作台中合成。"
}
```

#### 格式 2: result 字段

```json
{
  "result": "钻石镐需要3个钻石和2根木棍，在工作台中合成。"
}
```

#### 格式 3: content 字段

```json
{
  "content": "钻石镐需要3个钻石和2根木棍，在工作台中合成。"
}
```

#### 格式 4: 列表（取前5条拼接）

```json
{
  "data": [
    {"content": "钻石镐合成：3钻石+2木棍"},
    {"content": "需要在工作台中间一排放钻石"}
  ]
}
```

#### 格式 5: 纯文本

```text
钻石镐需要3个钻石和2根木棍，在工作台中合成。
```

### 未找到答案

当知识库中没有匹配内容时，API 应返回 HTTP 200 但 answer 为空：

```json
{
  "answer": ""
}
```

或返回 HTTP 404。此时插件会继续尝试下一个检索来源（外部→LLM）。

## 自建外部 API 示例

以下是一个最简 Python Flask 示例：

```python
from flask import Flask, request, jsonify

app = Flask(__name__)

# 简单的知识库（实际应用中可对接 RAG / 向量数据库 / Dify 等）
KNOWLEDGE = {
    "钻石镐": "钻石镐需要3个钻石和2根木棍，在工作台中合成。",
    "下界要塞": "下界要塞可通过下界传送门遗迹寻找，通常在 z 轴正方向。",
    "服务器规则": "禁止破坏他人建筑，禁止使用外挂，违者封禁。",
}

@app.route("/api/ask", methods=["POST"])
def ask():
    data = request.get_json() or {}
    question = (data.get("query") or data.get("question") or "").strip()

    if not question:
        return jsonify({"answer": ""}), 200

    # 简单关键词匹配（实际应用中应使用向量检索）
    for keyword, answer in KNOWLEDGE.items():
        if keyword in question:
            return jsonify({"answer": answer}), 200

    return jsonify({"answer": ""}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8777)
```

### 配置对接

在 AstrBot WebUI 中设置：

| 配置项 | 值 |
|:-------|:---|
| `KB_EXTERNAL_API_URL` | `http://127.0.0.1:8777/api/ask` |
| `KB_EXTERNAL_API_TOKEN` | （留空，或设置你的鉴权 Token） |
| `KB_EXTERNAL_TIMEOUT` | `10` |

## 检索流程图

```
MC玩家输入 /ask <问题>
        │
        ▼
  🔍 提示"正在查询知识库..."
        │
        ▼
  ① 查询 AstrBot 内置知识库
     (context API / provider_manager)
        │
     命中? ──是──▶ 📚 [内置知识库] 回复
        │
       否
        │
        ▼
  ② 查询外部知识库 API
     (POST KB_EXTERNAL_API_URL)
        │
     命中? ──是──▶ 📚 [外部知识库] 回复
        │
       否
        │
        ▼
  ③ LLM 回退 (ENABLE_ASK_LLM_FALLBACK)
        │
     成功? ──是──▶ LLM 自然回复（无前缀）
        │
       否
        │
        ▼
  "未找到相关内容，请稍后再试或换个问法。"
        │
        ▼
  tellraw 私发给提问玩家
  + 存入会话历史
  + 同步到 memory_companion
```

## 日志排查

在 AstrBot 控制台搜索 `[MCBridge]` 可看到以下日志：

```
[MCBridge][survival] /ask: player=Steve question='怎么合成钻石镐'
[MCBridge] 内置KB命中(context.query_knowledge_base): len=42
[MCBridge][survival] /ask 完成: source=内置知识库 answer_len=42
```

- `内置KB命中` = AstrBot 内置知识库找到了答案
- `外部KB命中` = 外部 API 返回了答案
- `source=LLM` = 回退到 LLM 回复
- `source=无` = 所有途径都未找到答案
