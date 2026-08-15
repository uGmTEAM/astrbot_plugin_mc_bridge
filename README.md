# astrbot_plugin_mc_bridge (MC服务器桥接 v2.0.0)

> 作者：**uGmTEAM**  
> 兼容 AstrBot 版本：**>= 4.5.7**  
> 标签：`minecraft` · `MC` · `桥接` · `聊天记录` · `tellraw` · `memory_companion` · `impression` · `多服务器`

---

## 一、插件简介

把 **一台或多台 Minecraft (Paper / Spigot / Bukkit 1.13+) 服务器** 接入你的 AstrBot 机器人，实现：

| 模块 | 说明 |
|:----:|:-----|
| 📨 **虚拟群聊会话** | 每台 MC 服务器的玩家聊天，会被记录成"按服隔离的虚拟群聊消息流"。玩家 @机器人 / 触发关键词 时，LLM 会结合最近上下文回复，并以 tellraw 彩色聊天格式回传到 MC。 |
| 🌐 **多服务器接入** | 单 AstrBot 实例可配置 **N 台不同 IP/端口** 的 MC 服务器，每台服务器独立拥有监听端口、显示名、触发词、黑白名单、回传通道(bridge / rcon)。 |
| 🪪 **正版UUID跨服合并** | `online_mode=true` 的正版验证服，会根据玩家的 **正版 UUID** 自动把他在所有正版服里的：聊天上下文、记忆(memory_companion)、印象(impression) **合并到同一身份**，切服不掉记忆。 |
| 🧠 **记忆同步到 memory_companion** | MC 聊天 / LLM 回复事件会异步批处理推送到已安装的 `astrbot_plugin_memory_companion` 伴侣插件（优先 `bridge.submit_emotion_event`，失败自动回退 `memory_api.record`）。 |
| 🎭 **印象同步到 impression** | 根据交互次数或 LLM 触发，调用 AstrBot 自带 LLM 生成对该 MC 玩家的「第一人称印象 JSON」，直接写入 `astrbot_plugin_impression` 插件的 `_save_summary` 内部接口。 |
| 🛠️ **管理员双入口指令** | AstrBot 管理员可通过 **① /mc 系列文字命令** 或 **② 自然语言（LLM 注册的工具调用）** 执行：状态查询、玩家管理(踢/封禁/白名单)、游戏操作(模式/传送/给物/任意命令)、插件运维(重同步/版本)。 |
| 🔗 **正版服处罚联动** | 在任何一台正版服里 `kick / ban / whitelist add` 玩家时，其他 `online_mode=true` 的正版服会自动同步执行同样的命令。 |
| 🔐 **权限分级 + 二次确认** | SUPER_ADMIN 全权限；PermissionType.ADMIN 群管默认仅"查询 + 运维"；玩家管理/游戏操作类可开启二次确认(管理员再发 确认/是/y/好)。 |
| 📡 **双通道回传** | 每服可独立选择回传通道：<br>• `bridge` → MC 端插件 HTTP 监听 `/execute`（推荐）<br>• `rcon` → 原生 Minecraft RCON（无需装MC端插件，但需要开rcon） |

---

## 二、身份标签格式（核心概念）

记忆/印象插件里的 `user_id` 标签格式如下：

```
# 非正版验证服（按服务器独立）
user:mcs[<服务器名>].<玩家名>

# 正版验证服（按UUID跨服合并，自动选择）
user:mcs[uuid:<玩家正版UUID>].<玩家名>
```

> 例子：  
> - 离线服 `survival` 的玩家 `Steve` → `user:mcs[survival].Steve`  
> - 正版服玩家 `Notch`（UUID 069a79f4-44e9-4726-a5be-fca90e38aaf5） → `user:mcs[uuid:069a79f4-44e9-4726-a5be-fca90e38aaf5].Notch`  
>   在第二台正版服 `creative` 里他同样以 `user:mcs[uuid:...].Notch` 身份积累记忆。

---

## 三、安装步骤

### 3.1 安装 AstrBot 端插件（本仓库）

1. 把整个 `astrbot_plugin_mc_bridge` 目录拷贝到你的 AstrBot 插件目录下（如 `data/plugins/`）。
2. 重启 AstrBot，或在 AstrBot WebUI 里手动启用本插件。
3. 打开 AstrBot WebUI 插件配置面板，按照 **第四章** 填写 SERVERS JSON 数组与全局配置。
4. 可选：安装依赖插件（推荐）：
   - `astrbot_plugin_memory_companion`：记忆同步（没装不会报错，只是不会同步记忆）
   - `astrbot_plugin_impression`：印象同步（没装不会报错，只是不会写印象）

### 3.2 安装 MC 端 Bukkit 插件（每台服务器都要装）

1. `McAstrbotBridge/target/McAstrbotBridge-1.0.0.jar` 复制到你每台 MC 服务器的 `plugins/` 目录。
2. 重启每台服，首次启动会自动生成 `plugins/McAstrbotBridge/config.yml`。
3. 编辑 `plugins/McAstrbotBridge/config.yml`，和 AstrBot 端 SERVERS 数组里的每一项对应上（详见 4.2 对照表）。
4. 执行 `/plugman reload McAstrbotBridge` 或直接重启服。
5. 启动时控制台看到「握手上报 AstrBot 成功：online_mode=xxx」即表示连接正确。

> **自己编译？** 进入 `McAstrbotBridge/` 执行 `mvn clean package` 即可生成新 jar。

---

## 四、配置说明

### 4.1 AstrBot 端全局配置（_conf_schema.json）

| 配置项 | 类型 | 默认值 | 说明 |
|:-------|:-----|:-------|:-----|
| **SERVERS** | template_list | — | **【核心】** 多服务器可视化表单列表。点击「添加」新增服务器，见下一节详述。 |
| ASTRBOT_LISTEN_HOST | string | `0.0.0.0` | HTTP 接收服务绑定的主机 IP。同机部署可改 `127.0.0.1` 增强安全。 |
| SUPER_ADMIN_IDS | text(逗号分隔) | `""` | 超级管理员ID列表(QQ号/AstrBot平台用户ID)，拥有**全部**MC指令权限。 |
| ADMIN_CAN_QUERY_ONLY | bool | `true` | PermissionType.ADMIN 群管是否只能执行 `查询类 + 插件运维类`；禁用后只有 SUPER_ADMIN 能做任何MC操作。 |
| ENABLE_NATURAL_LANGUAGE_TOOL | bool | `true` | 是否注册 LLM 自然语言工具(管理员用自然语言意图触发MC命令)；关闭后只能用 `/mc` 指令。 |
| CMD_CONFIRMATION_REQUIRED | bool | `false` | 玩家管理类/游戏操作类命令执行前，是否要求管理员再发「确认 / 是 / y / 好」二次确认。 |
| CMD_EXECUTE_TIMEOUT | int | `120` | 二次确认超时时间(秒)，超时自动取消。 |
| ENABLE_CROSS_SERVER_PUNISH | bool | `true` | 正版服之间处罚跨服联动：踢/封禁/白名单时在所有 `online_mode=true` 的服务器同步执行。 |
| ENABLE_LLM_REPLY | bool | `true` | 总开关：是否启用 LLM 回复被触发的玩家消息。 |
| LLM_REPLY_COOLDOWN | int | `3` | LLM 回复同服同一玩家的冷却时间(秒)，0 = 不限。 |
| MAX_HISTORY_PER_SERVER | int | `300` | 每台服务器虚拟会话保留的最大消息条数（滚动）。 |
| LLM_CONTEXT_COUNT | int | `20` | 调用 LLM 时携带的近期消息条数(本服)。 |
| ENABLE_CROSS_SERVER_CONTEXT | bool | `true` | 正版服玩家触发 LLM 回复时，是否补充读取其它正版服近期聊天当作上下文。 |
| ENABLE_SYNC_TO_MEMORY_COMPANION | bool | `true` | 是否将 MC 聊天/回复事件同步到 memory_companion。 |
| MEMORY_COMPANION_SYNC_INTERVAL | int | `5` | 每积累多少条消息批量同步一次到 memory_companion（防止刷屏）。1 = 每条立即同步。 |
| ENABLE_SYNC_TO_IMPRESSION | bool | `true` | 是否为 MC 玩家生成/更新交互印象(impression插件)。 |
| IMPRESSION_TRIGGER_COUNT | int | `8` | MC 玩家累计交互多少次，触发一次印象 LLM 更新。 |

### 4.2 SERVERS —— 可视化表单配置（template_list）

在 AstrBot WebUI 的插件配置页面，SERVERS 是一个 **可视化列表**（`template_list` 类型），无需手写 JSON：

1. 点击 **「添加」** 按钮，新增一台 MC 服务器
2. 在展开的表单里逐项填写配置
3. 需要多台服务器就点多次「添加」

每台服务器的表单字段说明：

| 字段 | 类型 | 默认值 | 说明 |
|:-----|:-----|:-------|:-----|
| 服务器名称 | string | `survival` | 唯一标识。必须与 MC 端 `config.yml` 的 `server_name` 一致。标签格式 `mcs[名称].玩家名`。 |
| MC服务器IP | string | `127.0.0.1` | 本插件向该服回传 tellraw 时连接的 IP。同机部署填 `127.0.0.1`。 |
| AstrBot监听端口 | int | `6188` | 本插件为该服务器独占的 HTTP 监听端口。**每台服必须不同**（如 6188、6189...）。 |
| 鉴权Token | string | `""` | 与 MC 端 `bridge_token` 填一致，为空则不鉴权。生产环境务必设置。 |
| 回传通道 | string(下拉) | `bridge` | `bridge` = 走 MC 端插件 HTTP；`rcon` = 走原生 RCON。 |
| MC端HTTP端口 | int | `25580` | `send_channel=bridge` 时用，对应 MC 端 `bridge_listen_port`。 |
| RCON端口 | int | `25575` | `send_channel=rcon` 时用。 |
| RCON密码 | string | `""` | `send_channel=rcon` 时用。 |
| 机器人显示名 | string | `Kei` | 本服机器人名字，tellraw 模板里 `{BOT_NAME}` 替换为此值。 |
| tellraw消息模板 | string | `§7<{BOT_NAME}> {message}` | 支持 `{BOT_NAME}` 和 `{message}` 占位符，支持 `§` 颜色代码。 |
| 触发关键词 | string | `Kei,机器人` | 玩家消息含任一关键词即触发 LLM 回复。**多个用英文逗号分隔**。 |
| 启用@触发 | bool | `true` | 玩家消息以 `@机器人名` 或 `机器人名` 开头时触发回复。 |
| 正版验证 | bool | `false` | 建议不手动填。MC 端启动握手会自动用 `Bukkit.getOnlineMode()` 覆盖此值。 |
| 玩家白名单 | string | `""` | 非空时只有白名单玩家的消息会被记录。留空=不限制。**逗号分隔**。 |
| 玩家黑名单 | string | `""` | 命中直接忽略。**逗号分隔**。 |
| 消息过滤词 | string | `""` | 消息含任一关键词直接忽略。**逗号分隔**。 |

> **提示**：`trigger_keywords`、`player_whitelist`、`player_blacklist`、`message_filter` 这4个字段在表单里用英文逗号分隔多个值（如 `Kei,机器人,小樱`）。由于 AstrBot 的 `template_list` 不支持嵌套列表，所以采用逗号分隔方式。

### 4.3 MC 端 `plugins/McAstrbotBridge/config.yml`

| 字段 | AstrBot 端对应字段 |
|:-----|:-------------------|
| `server_name` | 服务器名称（必须一致） |
| `astrbot_host` / `astrbot_port` | AstrBot 部署的 IP + AstrBot监听端口 |
| `bridge_token` | 鉴权Token（必须一致） |
| `bridge_listen_port` | MC端HTTP端口（`send_channel=bridge` 时必须一致） |
| `push_chat` | 是否推送聊天（true/false） |

### 4.4 多服务器配置示例

假设你有：
- **Survival 生存服**（正版验证，同机部署，回传走 bridge）
- **Creative 创造服**（离线服，在另一台机器 `192.168.1.20`，回传走 RCON）

**AstrBot 端 WebUI 操作：**

在 SERVERS 配置区点击两次「添加」，分别填写：

**第1台 - Survival：**

| 字段 | 值 |
|:-----|:---|
| 服务器名称 | `survival` |
| MC服务器IP | `127.0.0.1` |
| AstrBot监听端口 | `6188` |
| 鉴权Token | `MySecretToken_123` |
| 回传通道 | `bridge` |
| MC端HTTP端口 | `25580` |
| 机器人显示名 | `Kei` |
| tellraw消息模板 | `§7<{BOT_NAME}> {message}` |
| 触发关键词 | `Kei,机器人` |
| 启用@触发 | ✅ |

**第2台 - Creative：**

| 字段 | 值 |
|:-----|:---|
| 服务器名称 | `creative` |
| MC服务器IP | `192.168.1.20` |
| AstrBot监听端口 | `6189` |
| 鉴权Token | `CreativeToken_456` |
| 回传通道 | `rcon` |
| RCON端口 | `25575` |
| RCON密码 | `MyRconPasswd!!` |
| 机器人显示名 | `Sakura` |
| tellraw消息模板 | `§d<{BOT_NAME}> §f{message}` |
| 触发关键词 | `Sakura,小樱` |
| 启用@触发 | ✅ |

**Survival 服 MC 端 config.yml：**

```yaml
server_name: "survival"
astrbot_host: 127.0.0.1
astrbot_port: 6188
bridge_token: "MySecretToken_123"
bridge_listen_port: 25580
push_chat: true
```

**Creative 服 MC 端 config.yml：**

```yaml
server_name: "creative"
astrbot_host: <AstrBot所在机器的可访问IP>
astrbot_port: 6189
bridge_token: "CreativeToken_456"
bridge_listen_port: 25580    # 远端其实没用到，但仍建议保留默认值
push_chat: true
```

同时 Creative 服的 `server.properties` 需要：
```properties
enable-rcon=true
rcon.port=25575
rcon.password=MyRconPasswd!!
```

---

## 五、管理员指令大全

### 5.1 权限说明

| 身份 | 查询类(query) | 运维类(op) | 玩家管理类(risk) | 游戏操作类(gameop) |
|:-----|:-------------:|:----------:|:----------------:|:------------------:|
| SUPER_ADMIN_IDS | ✅ | ✅ | ✅ | ✅ |
| PermissionType.ADMIN | ✅ (当 ADMIN_CAN_QUERY_ONLY=true) | ✅ (同上) | ❌ | ❌ |
| 普通用户 | ❌ | ❌ | ❌ | ❌ |

> 若开启了 `CMD_CONFIRMATION_REQUIRED=true`，玩家管理类/游戏操作类命令在被**任何管理员**执行前，都需要再发一条消息 `/mc_confirm 确认`（或 "是" "y" "好"）。

### 5.2 `/mc` 文字指令列表

在 AstrBot 接入的任意群聊/私聊里发：

| 指令 | 分类 | 说明 |
|:-----|:----:|:-----|
| `/mc help` | — | 显示帮助 |
| `/mc version` | — | 显示版本与已接入服列表 |
| `/mc list <服务名>` | query | 查询该服在线玩家（执行 MC 的 `/list`） |
| `/mc tps <服务名>` | query | 查询该服 TPS（执行 MC 的 `/tps`，Paper/Spigot 才有） |
| `/mc servers` | op | 查看 AstrBot 已接入的所有服务器详细信息 |
| `/mc resync <服务名\|all>` | op | 重新把近期聊天推一遍到 memory_companion |
| `/mc kick <服务名> <玩家> [原因]` | risk | 踢玩家（正版服会自动跨服踢） |
| `/mc ban <服务名> <玩家> [原因]` | risk | 封禁（正版服会自动跨服封禁） |
| `/mc pardon <服务名> <玩家>` | risk | 解封 |
| `/mc whitelist add\|remove <服务名> <玩家>` | risk | 白名单增删（正版服跨服同步） |
| `/mc gamemode <服务名> <目标> <0..3\|模式名>` | gameop | 切换游戏模式 |
| `/mc tp <服务名> <谁> <传送到谁\|坐标>` | gameop | 传送 |
| `/mc give <服务名> <玩家> <物品ID> [数量]` | gameop | 给物品 |
| `/mc raw <服务名> <任意MC命令>` | gameop | ⚠️ **最高风险**，执行任意 Minecraft 控制台命令 |

### 5.3 自然语言指令（LLM 工具调用）

只要你是 AstrBot 管理员（符合 5.1 权限表），直接用中文自然语言跟机器人说就行，不用记命令。插件向 LLM 注册了 5 个工具：

| 工具名 | 自然语言触发示例 |
|:-------|:----------------|
| `mc_list_servers` | "看看接了哪些MC服？" / "列出MC服务器列表" |
| `mc_list_players` | "生存服现在多少人在线？" / "查 creative 的 TPS" |
| `mc_player_manage` | **(SUPER_ADMIN)** "把 Steve 从生存服踢出去" "封禁 Notch 原因开挂" "把 xx 加到白名单" |
| `mc_game_operation` | **(SUPER_ADMIN)** "把 Alex 改成创造模式" "给小明 64 个钻石块" "传送到 xyz" |
| `mc_plugin_ops` | "查MC桥接插件版本" / "把所有服的记忆重新同步一下" |

> 提示：LLM 在调用 `mc_player_manage` / `mc_game_operation` 前会先查看自己有没有权限；无权时会提示你需要 SUPER_ADMIN。

---

## 六、通信协议说明（对接 / 调试 参考）

### 6.1 MC → AstrBot

每条聊天消息推送到 `POST http://<astrbot_host>:<SERVERS[*].listen_port>/mc_chat`：

```
Content-Type: application/json
Authorization: Bearer <bridge_token>   # 若 token 非空

{
  "player": "Steve",
  "display_name": "§6Steve",
  "message": "Kei 你好呀",
  "timestamp": 1730000000000,
  "player_uuid": "069a79f4-44e9-4726-a5be-fca90e38aaf5",
  "server_name": "survival"
}
```

启动时握手上报 `POST http://.../mc_handshake`：

```json
{
  "online_mode": true,
  "bukkit_version": "git-Paper-310 (MC: 1.20.4)",
  "server_name": "survival",
  "timestamp": 1730000000000
}
```

健康检查 `GET http://.../status` → 返回 `{"ok": true, "server": {...}, "session_count": N}`。

### 6.2 AstrBot → MC

- **bridge 通道**：`POST http://<MC host>:<mc_bridge_port>/execute`  
  请求体 `{"command": "tellraw @a {\"text\":\"hi\",\"color\":\"gray\"}"}`  
  成功响应 `{"ok": true}`。
- **RCON 通道**：直连 `<host>:<mc_rcon_port>`，发送原生命令。

---

## 七、排错指南

| 症状 | 排查 |
|:-----|:-----|
| AstrBot 控制台看到「HTTP 监听失败 (端口占用?)」 | 换一个未被占用的 `listen_port`，每台服必须不同。Windows 下用 `netstat -ano | findstr :6188` 查占用。 |
| AstrBot 收不到 MC 聊天 | 1) MC 端 `config.yml` 的 `astrbot_host` / `astrbot_port` 是否填对；2) `bridge_token` 是否两端一致；3) AstrBot 端机器的防火墙是否开放了 `listen_port` 入站；4) 查看 MC 端控制台有没有「推送聊天到 AstrBot 失败」日志。 |
| MC 端收不到 AstrBot tellraw | 1) 若 `send_channel=bridge`：检查 MC 端控制台是否有「Bridge HTTP 服务已启动」，AstrBot 端 `host` / `mc_bridge_port` 是否对；2) 若 `send_channel=rcon`：确认 MC 的 `server.properties` 是否 `enable-rcon=true`，端口密码与配置一致。 |
| 印象没生成 / 记忆没同步 | 1) 确认 `astrbot_plugin_impression` / `astrbot_plugin_memory_companion` 已安装并启用；2) 调大日志级别观察 warn/debug 输出；3) `/mc resync all` 强制重推。 |
| 处罚跨服联动没生效 | 1) `ENABLE_CROSS_SERVER_PUNISH=true`；2) 源服和目标服都必须 `online_mode=true`（握手成功后 AstrBot 端会自动覆盖配置，用 `/mc servers` 检查）。 |
| 正版服玩家跨服记忆仍是两份 | 1) 启动时握手上报是否成功（看MC端启动日志）；2) 玩家 UUID 是否在所有正版服推送时一致（`/status` 查 session 里存的 `player_uuid`）。 |

---

## 八、安全说明

1. **生产环境务必设置非空 `bridge_token`**。防止任何人能伪造聊天推送到 AstrBot、伪造命令推送到 MC。
2. AstrBot 端 `ASTRBOT_LISTEN_HOST` 同机部署时建议设 `127.0.0.1`；跨机部署时，请只开放信任的 IP 段，**不要把监听端口暴露到公网**（或至少加防火墙白名单 + token）。
3. RCON 协议本身不加密，**请只在内网/可信网络使用 RCON 通道**；公网回传请改用 bridge HTTP + token + HTTPS 反向代理。
4. `/mc raw` 命令等于直接拿 MC 控制台权限，**只交给绝对可信的 SUPER_ADMIN**。建议开启 `CMD_CONFIRMATION_REQUIRED=true` 做二次确认。
5. 白名单和消息过滤只会**忽略聊天记录/触发**，不会实际踢出玩家；需要处罚请使用 `/mc kick/ban`。

---

## 九、文件结构速览

```
astrbot_plugin_mc_bridge/
├── main.py                  # 插件主逻辑（HTTP接收/会话/记忆印象同步/命令注册/LLM工具）
├── metadata.yaml            # 插件元信息（作者uGmTEAM / v2.0.0）
├── _conf_schema.json        # AstrBot WebUI 配置表（SERVERS JSON 数组）
├── README.md                # 本文件
├── McAstrbotBridge/         # MC 端 Bukkit 插件源码
│   ├── pom.xml              # Maven 构建（无第三方依赖）
│   └── src/main/java/io/trae/mcbridge/
│       ├── McAstrbotBridge.java   # 主类：启动握手、配置读写、调度命令
│       ├── ChatListener.java      # 聊天监听：异步推送(含UUID)
│       └── BridgeHttpServer.java  # HTTP /execute 接收
│   └── target/McAstrbotBridge-1.0.0.jar   # 已编译好可直接丢 plugins/
└── data/                    # 运行时自动生成
    ├── mc_session.json      # 虚拟会话持久化（重启后继续）
    └── mc_state.json        # 握手覆盖的 online_mode 真值
```

---

© uGmTEAM · AstrBot 插件生态
