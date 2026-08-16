"""
AstrBot Plugin - MC服务器桥接 3.0.0  (author: uGmTEAM)

核心特性：
  1. 多服务器：SERVERS 可视化列表；每台服独立会话、显示名/关键词/回传通道/Token。
  2. 正版合并：online_mode=true 的服务器，按玩家正版UUID跨服合并 记忆/印象/上下文。
     身份合并（未绑定MC）：正版UUID → user:<UUID>；非正版服 → user:mcs[服名].玩家名。
  3. 记忆同步：改用 memory_companion 1.7.3 的 bridge.record_visible_turn (短期timeline) +
     bridge.search/compose_context (跨平台向量检索注入共享记忆)。不再调用失效的
     submit_emotion_event 或 memory_api.record。
  4. 印象同步：直接调用 impression v3.2.0 插件 _save_summary(key,type,info,data)。
     身份合并（绑定QQ后）：同一份 user:<QQ号> 印象，实现跨MC/QQ互通。
  5. QQ全消息旁路监听：@filter.event_message_type(ALL)，仅旁听不拦截。写入
     memory_companion / impression，记录本地 QQ 会话（群/私聊独立session_id）。
  6. 消息桥：MC→QQ（FORWARD_GROUPS + /mc_forward on|off <服名> 动态订阅）；
     QQ→MC：不转发到tellraw刷屏，但完整进入共享记忆池。
  7. 绑定系统：MC玩家在游戏里 /bind <QQ号>，插件向QQ真人（私聊优先，失败回退群@）
     发起二次确认；同意后 QQ号=MC玩家 身份永久合并，权限跟随QQ号。
     MC里 /unbind 或 QQ里 /unbind 解绑。
  8. 权限映射：SUPER_ADMIN_IDS=全权限；PermissionType.ADMIN=query+op；MEMBER=仅query；
     未绑定=拒绝执行并提示先绑定。
  9. MC自然语言指令：/ai 前缀强制进入指令模式；或正常聊天被LLM识别为指令意图
     时也执行；危险指令（kick/ban/gamemode/give/raw等）由LLM生成拒绝语并提示用
     直接指令。回执/错误用 tellraw @player 私发，普通聊天全服tellraw。
 10. 管理入口：/mc 命令 + AstrBot 侧自然语言 LLM 工具双入口；CMD_CONFIRMATION_REQUIRED
     二次确认、CMD_EXECUTE_TIMEOUT 超时、CMD_NL_COOLDOWN 自然语言独立 15s 冷却。
 11. 处罚联动：ENABLE_CROSS_SERVER_PUNISH 正版服间跨服广播。
 12. 回传：tellraw；bridge(HTTP) / rcon 二选一，每服独立配置；区分
     tellraw_template（全服）与 tellraw_private_template（私信回执）。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import struct
import time
from copy import deepcopy
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request

try:
    from astrbot.api.types import PermissionType  # type: ignore
except Exception:  # 兼容：旧版定义在 filter 里
    try:
        from astrbot.api.event.filter import PermissionType  # type: ignore
    except Exception:
        class PermissionType:  # type: ignore
            ADMIN = "admin"
            ANY = "any"

try:
    from astrbot.api.toolbox import tool, ToolContext, ToolResult  # type: ignore
except Exception:
    try:
        from astrbot.api.provider.entities import tool, ToolContext, ToolResult  # type: ignore
    except Exception:
        tool = None
        ToolContext = Any
        ToolResult = Any


PLUGIN_NAME = "astrbot_plugin_mc_bridge"
PLUGIN_VERSION = "3.0.0"
MEMORY_COMPANION_NAME = "astrbot_plugin_memory_companion"
IMPRESSION_NAME = "astrbot_plugin_impression"

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_PLUGIN_DIR, "data")
SESSION_FILE = os.path.join(DATA_DIR, "mc_session.json")  # key=session_id(MC/QQ) -> entries
STATE_FILE = os.path.join(DATA_DIR, "mc_state.json")
BINDING_FILE = os.path.join(DATA_DIR, "mc_bindings.json")
BINDING_META_FILE = os.path.join(DATA_DIR, "mc_binding_meta.json")  # mid -> {server, player, player_uuid, display}
FORWARD_FILE = os.path.join(DATA_DIR, "mc_forwards.json")

try:
    import aiohttp  # type: ignore
    from aiohttp import web  # type: ignore
except ImportError:
    aiohttp = None
    web = None


# ======================================================================
#  数据结构
# ======================================================================

@dataclass
class ServerCfg:
    """单台服务器配置（SERVERS JSON数组元素）。"""
    name: str
    host: str = "127.0.0.1"
    listen_port: int = 6188
    bridge_token: str = ""
    send_channel: str = "bridge"
    mc_bridge_port: int = 25580
    mc_rcon_port: int = 25575
    mc_rcon_password: str = ""
    bot_name: str = "Kei"
    tellraw_template: str = "§7<{BOT_NAME}> {message}"
    tellraw_private_template: str = "§a<{BOT_NAME}> {message}"
    trigger_keywords: list[str] = field(default_factory=lambda: ["Kei", "机器人"])
    enable_at_trigger: bool = True
    online_mode: bool = False
    player_whitelist: list[str] = field(default_factory=list)
    player_blacklist: list[str] = field(default_factory=list)
    message_filter: list[str] = field(default_factory=list)

    @staticmethod
    def _split_csv(val) -> list[str]:
        """将逗号分隔的字符串解析为列表。兼容旧版已是列表的情况。"""
        if not val:
            return []
        if isinstance(val, list):
            return [str(x).strip() for x in val if str(x).strip()]
        return [x.strip() for x in str(val).split(",") if x.strip()]

    @classmethod
    def from_dict(cls, d: dict) -> "ServerCfg":
        name = str(d.get("server_name", d.get("name", "")) or "").strip()
        return cls(
            name=name,
            host=str(d.get("host", "127.0.0.1") or "127.0.0.1").strip(),
            listen_port=int(d.get("listen_port", 6188)),
            bridge_token=str(d.get("bridge_token", "") or ""),
            send_channel=str(d.get("send_channel", "bridge") or "bridge").strip().lower(),
            mc_bridge_port=int(d.get("mc_bridge_port", 25580)),
            mc_rcon_port=int(d.get("mc_rcon_port", 25575)),
            mc_rcon_password=str(d.get("mc_rcon_password", "") or ""),
            bot_name=str(d.get("bot_name", "Kei") or "Kei").strip() or "Kei",
            tellraw_template=str(d.get("tellraw_template") or "§7<{BOT_NAME}> {message}"),
            tellraw_private_template=str(d.get("tellraw_private_template") or "§a<{BOT_NAME}> {message}"),
            trigger_keywords=cls._split_csv(d.get("trigger_keywords")),
            enable_at_trigger=bool(d.get("enable_at_trigger", True)),
            online_mode=bool(d.get("online_mode", False)),
            player_whitelist=cls._split_csv(d.get("player_whitelist")),
            player_blacklist=cls._split_csv(d.get("player_blacklist")),
            message_filter=cls._split_csv(d.get("message_filter")),
        )


# ======================================================================
#  插件主类
# ======================================================================

@register(
    PLUGIN_NAME,
    "uGmTEAM",
    "MC多服桥接：MC/QQ双向互通共享memory_companion+impression全部记忆；/bind绑定MC玩家↔QQ号（QQ真人二次确认）；MC内/ai前缀或自然语言意图执行MC指令（危险指令由LLM生成拒绝提示用直接指令）；权限跟随绑定QQ号等级；+原多服/正版UUID合并/双tellraw通道/运维指令/LLM工具。",
    PLUGIN_VERSION,
    "",
)
class MCBridgePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        os.makedirs(DATA_DIR, exist_ok=True)

        # 多服务器配置解析
        self._servers: dict[str, ServerCfg] = {}  # key = name
        self._port_to_server: dict[int, ServerCfg] = {}
        self._parse_servers_config()

        # 持久化: 虚拟会话 & 状态
        self._sessions: dict[str, list[dict]] = self._load_json(
            SESSION_FILE, {}
        )  # session_id(MC/QQ) -> entries
        # 握手覆盖的 online_mode 真值（持久化，确保重启后一致）
        self._handshake_state: dict[str, bool] = self._load_json(STATE_FILE, {})
        # 应用 online_mode 覆盖
        for n, om in self._handshake_state.items():
            if n in self._servers:
                self._servers[n].online_mode = bool(om)

        # --------- v3.0 新增持久化 ---------
        # 绑定表: mc_identity_key(如 mcs[survival].Steve 或 uuid:xxx) -> QQ号(str)
        self._bindings: dict[str, str] = self._load_json(BINDING_FILE, {})
        # QQ号反向查绑定的mc_identity_key（1对1）
        self._qq_to_mc: dict[str, str] = {qq: mid for mid, qq in self._bindings.items()}
        # 绑定元数据: mid -> {server, player, player_uuid, display}；用于从QQ号反查MC身份（QQ→MC推送回复）
        self._binding_meta: dict[str, dict] = self._load_json(BINDING_META_FILE, {})
        # 转发: group_id(str) -> set[server_name(str)]；* 号代表订阅所有服
        self._parse_forward_groups()
        # 待确认绑定: token -> {mc_id, qq, server, player, display, timeout_at, platform_id, via_event 等}
        self._pending_binds: dict[str, dict] = {}

        # 交互计数 / 冷却 / 待确认
        self._interaction_count: dict[str, int] = {}  # user_key -> count
        self._mem_sync_acc: dict[str, list[dict]] = {}  # bucket_key(MC/QQ) -> buffered for batch
        self._last_reply_ts: dict[tuple[str, str], float] = {}  # (session_or_sv, user) -> ts（聊天冷却）
        self._last_nl_cmd_ts: dict[tuple[str, str], float] = {}  # (sv, player) -> ts（自然语言指令独立冷却）
        self._pending_confirmations: dict[str, dict] = {}  # token -> {command, category, server, user_id, cross_punish, timeout_at}
        # AstrBot bot 客户端缓存（用于MC→QQ发消息、发绑定确认）
        self._cached_bot: Optional[Any] = None
        self._cached_platform_id: str = ""

        # --------- WebSocket 连接管理（MC端主动连过来，双向实时通讯）---------
        # key = server_name；value = {"ws": WebSocketResponse, "online_mode": bool}
        self._ws_connections: dict[str, dict] = {}
        # 等待 execute_result 的 future（_exec_and_capture_output 用）
        # key = request_id；value = asyncio.Future
        self._exec_futures: dict[str, asyncio.Future] = {}

        self._lock = asyncio.Lock()
        self._rcon_locks: dict[str, asyncio.Lock] = {}
        self._http_runners: list[tuple] = []  # aiohttp AppRunner/TCPSite（WS server 复用）
        self._client_session: Optional[aiohttp.ClientSession] = None
        self._bg_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ 生命周期

    async def initialize(self):
        if aiohttp is None:
            logger.error("[MCBridge] 缺少依赖 aiohttp，无法启动 WebSocket 服务")
            return
        if not self._servers:
            logger.warning("[MCBridge] 未配置任何服务器，请在插件配置 SERVERS 中填写列表")
            return
        await self._start_ws_server()
        for s in self._servers.values():
            logger.info(
                f"[MCBridge] 等待 MC 服务器 [{s.name}] 连接 (listen=:{s.listen_port} "
                f"online_mode={s.online_mode})"
            )
        # 注册 LLM 工具
        if bool(self.config.get("ENABLE_NATURAL_LANGUAGE_TOOL", True)):
            self._register_llm_tools()
        # 迁移：为缺失元数据的旧绑定补充 mid -> {server, player}（仅非正版 mcs[server].player 可解析）
        self._migrate_binding_meta()
        bind_cnt = len(self._bindings)
        fwd_cnt = sum(len(v) for v in self._forwards.values())
        logger.info(
            f"[MCBridge] 初始化: 绑定={bind_cnt}对, 转发订阅={fwd_cnt}条"
        )

    def _migrate_binding_meta(self):
        """为缺失 _binding_meta 的旧绑定补充元数据。
        非正版服 mid=mcs[server].player 可解析；正版服 mid=uuid:xxx 无法解析，需重新绑定。
        """
        changed = False
        for mid in list(self._bindings.keys()):
            if mid in self._binding_meta:
                continue
            m = re.match(r"^mcs\[(.+)\]\.(.+)$", mid)
            if m:
                self._binding_meta[mid] = {
                    "server": m.group(1),
                    "player": m.group(2),
                    "player_uuid": "",
                    "display": m.group(2),
                }
                changed = True
                logger.info(f"[MCBridge] 迁移绑定元数据: {mid}")
            else:
                logger.warning(
                    f"[MCBridge] 绑定 {mid} 缺少元数据且为正版UUID格式，QQ→MC回复推送不可用，建议重新 /bind"
                )
        if changed:
            try:
                self._save_json(BINDING_META_FILE, self._binding_meta)
            except Exception as e:
                logger.warning(f"[MCBridge] 写入迁移后绑定元数据失败: {e}")

    async def on_unload(self):
        for t in list(self._bg_tasks):
            t.cancel()
        self._bg_tasks.clear()
        # 关闭所有 WS 连接
        for name, info in list(self._ws_connections.items()):
            ws = info.get("ws")
            if ws is not None and not ws.closed:
                try:
                    await ws.close()
                except Exception:
                    pass
        self._ws_connections.clear()
        # 取消所有等待 execute_result 的 future
        for fut in self._exec_futures.values():
            if not fut.done():
                fut.cancel()
        self._exec_futures.clear()
        # 清理 aiohttp runner/site
        for runner, site, _port in self._http_runners:
            try:
                await site.stop()
            except Exception:
                pass
            try:
                await runner.cleanup()
            except Exception:
                pass
        self._http_runners.clear()
        if self._client_session and not self._client_session.closed:
            try:
                await self._client_session.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ 配置解析

    def _parse_servers_config(self):
        """解析 SERVERS 配置。template_list 类型直接返回 list[dict]，无需 json.loads。"""
        raw = self.config.get("SERVERS", [])
        if not raw:
            return
        # 兼容：如果用户旧的 text 类型残留了 JSON 字符串，尝试解析
        if isinstance(raw, str):
            raw = raw.strip()
            if not raw:
                return
            try:
                lst = json.loads(raw)
            except Exception as e:
                logger.warning(f"[MCBridge] SERVERS JSON 解析失败: {e}，退化为空")
                return
        else:
            lst = raw
        if not isinstance(lst, list):
            logger.warning("[MCBridge] SERVERS 必须是列表，当前被忽略")
            return
        ports_seen: set[int] = set()
        for item in lst:
            if not isinstance(item, dict):
                continue
            try:
                s = ServerCfg.from_dict(item)
            except Exception as e:
                logger.warning(f"[MCBridge] 服务器配置项解析失败: {e} -> {item}")
                continue
            if not s.name:
                logger.warning(f"[MCBridge] 服务器 name 不能为空，已跳过: {item}")
                continue
            if s.listen_port in ports_seen:
                logger.warning(f"[MCBridge] 服务器 {s.name} listen_port={s.listen_port} 重复，已跳过")
                continue
            ports_seen.add(s.listen_port)
            if s.name in self._servers:
                logger.warning(f"[MCBridge] 服务器名 {s.name} 重复，后定义覆盖前定义")
            self._servers[s.name] = s
            self._port_to_server[s.listen_port] = s

    def _parse_forward_groups(self):
        """解析 FORWARD_GROUPS（template_list）+ FORWARD_FILE 动态转发合并。
        结果: self._forwards = {group_id(str): set[str 服务器名或 '*']}
        """
        result: dict[str, set[str]] = {}
        # 1. 来自 _conf_schema 的静态 FORWARD_GROUPS 列表
        raw = self.config.get("FORWARD_GROUPS", [])
        if isinstance(raw, str):
            raw = raw.strip()
            if raw:
                try:
                    raw = json.loads(raw)
                except Exception:
                    raw = []
            else:
                raw = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                gid = str(item.get("group_id", "") or "").strip()
                srv = str(item.get("server_name", "") or "*").strip() or "*"
                if not gid:
                    continue
                result.setdefault(gid, set()).add(srv)
        # 2. 来自 /mc_forward 动态订阅
        dyn = self._load_json(FORWARD_FILE, {})
        for gid, srvs in (dyn or {}).items():
            if isinstance(srvs, list):
                for s in srvs:
                    if isinstance(s, str) and s:
                        result.setdefault(str(gid), set()).add(s)
        self._forwards: dict[str, set[str]] = result

    def _save_forward_groups(self):
        """只把动态订阅（不含静态FORWARD_GROUPS中读出来的）写回 FORWARD_FILE。
        所以这里不把全部转发dump，而是单独保存一个 runtime_dynamic 的视图。"""
        # 用简单方式：当前 self._forwards 里去掉静态配置就能得到动态部分，但要完全区分太复杂。
        # 简化：直接把 self._forwards 全部 dump 成 list[str]，下次 _parse_forward_groups
        # 合并时会自动去重。
        dump: dict[str, list[str]] = {g: sorted(list(srvs)) for g, srvs in self._forwards.items() if srvs}
        try:
            self._save_json(FORWARD_FILE, dump)
        except Exception as e:
            logger.warning(f"[MCBridge] 写入转发配置失败: {e}")

    def _mc_session_id(self, sv: ServerCfg) -> str:
        """MC服务器对应的虚拟会话ID（未绑定时用）。"""
        return f"mcbridge:GroupMessage:mc_{sv.name}"

    def _unified_session_id(self, sv: ServerCfg, player: str = "", player_uuid: str = "") -> str:
        """绑定后返回QQ私聊session_id（aiocqhttp:FriendMessage:<QQ>），未绑定返回MC session_id。
        这样绑定玩家的MC聊天会写入QQ私聊会话，共享上下文/记忆/印象。"""
        if player:
            mid = self._mc_identity(sv, player, player_uuid)
            qq = self._bindings.get(mid)
            if qq:
                return f"aiocqhttp:FriendMessage:{qq}"
        return self._mc_session_id(sv)

    def _qq_session_id(self, event_or_umo, platform: str = "", group_id: str = "", user_id: str = "", private: bool = False) -> str:
        """从QQ事件构造统一会话ID，兼容memory_companion的 identity.py。"""
        umo = ""
        if isinstance(event_or_umo, AstrMessageEvent):
            umo = str(getattr(event_or_umo, "unified_msg_origin", "") or "")
            if not platform:
                platform = str(event_or_umo.get_platform_id() or event_or_umo.get_platform_name() or "aiocqhttp")
            if event_or_umo.is_private_chat() if hasattr(event_or_umo, "is_private_chat") else private:
                user_id = str(event_or_umo.get_sender_id() or user_id or "")
                return f"{platform}:FriendMessage:{user_id}"
            else:
                group_id = str(event_or_umo.get_group_id() or group_id or "")
                return f"{platform}:GroupMessage:{group_id}"
        else:
            umo = str(event_or_umo or "")
        if umo:
            return umo
        plat = platform or "aiocqhttp"
        if private:
            return f"{plat}:FriendMessage:{user_id}"
        return f"{plat}:GroupMessage:{group_id}"

    # ------------------------------------------------------------------ WebSocket 服务（MC端主动连过来，双向实时通讯；不再有反向HTTP通道和pending队列）

    async def _start_ws_server(self):
        host = str(self.config.get("ASTRBOT_LISTEN_HOST", "0.0.0.0") or "0.0.0.0").strip()
        # 去重：同一个 listen_port 只启动一次 app，避免插件热加载后重复监听同一端口导致 "Address already in use"
        started_sites: dict[tuple[str, int], tuple] = {}
        for sv in self._servers.values():
            port_key = (host, sv.listen_port)
            if port_key in started_sites:
                logger.info(
                    f"[MCBridge] 服务器[{sv.name}] 复用已启动的 WS 监听 {host}:{sv.listen_port}"
                )
                continue
            app = web.Application()
            app.router.add_get("/mc_ws", self._handle_ws)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, host, sv.listen_port)
            try:
                await site.start()
            except Exception as e:
                # 端口启动失败只 logger.error，不再塞 pending（pending已删）
                logger.error(
                    f"[MCBridge] 服务器[{sv.name}] WS 监听 {host}:{sv.listen_port} 失败: {e!r}. "
                    f"**这会导致 MC 端无法连接 AstrBot**！"
                    f"解决：1) 检查端口 :{sv.listen_port} 是否被占用并 kill；"
                    f"2) 在配置 SERVERS 里把 listen_port 改成未占用端口，并同步修改 MC 端连接配置。"
                )
                continue
            self._http_runners.append((runner, site, sv.listen_port))
            started_sites[port_key] = (app, runner, site)

    async def _handle_ws(self, request):
        # 从 URL query 获取 token 和 server_name
        token = request.query.get("token", "").strip()
        server_name = request.query.get("server_name", "").strip()
        # 验证 token（匹配任一已配置的 ServerCfg.bridge_token）
        matched_sv: Optional[ServerCfg] = None
        for sv in self._servers.values():
            t = (sv.bridge_token or "").strip()
            if t and t == token:
                matched_sv = sv
                break
        if matched_sv is None:
            # 兼容：token 为空时也接受（未配置 token 的服）
            for sv in self._servers.values():
                if not (sv.bridge_token or "").strip():
                    matched_sv = sv
                    break
        if matched_sv is None:
            logger.warning(f"[MCBridge] WS连接拒绝: token不匹配 (server_name={server_name})")
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        sv = matched_sv
        # 根据 server_name 找到 ServerCfg（匹配不到用 matched_sv）
        if server_name and server_name in self._servers:
            sv = self._servers[server_name]
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws_connections[sv.name] = {"ws": ws, "sv": sv}
        logger.info(
            f"[MCBridge] WS连接建立: server={sv.name} (上报server_name={server_name or sv.name}) "
            f"listen_port={sv.listen_port}"
        )
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except Exception as e:
                        logger.debug(f"[MCBridge][{sv.name}] WS收到非JSON消息: {e}")
                        continue
                    mtype = str(data.get("type", "") or "").strip()
                    if mtype == "handshake":
                        om_reported = data.get("online_mode")
                        reported_name = str(data.get("server_name", "") or "").strip()
                        if isinstance(om_reported, bool):
                            sv.online_mode = om_reported
                            self._handshake_state[sv.name] = om_reported
                            try:
                                self._save_json(STATE_FILE, self._handshake_state)
                            except Exception:
                                pass
                            logger.info(
                                f"[MCBridge] 服务器[{sv.name}]握手: online_mode={om_reported}"
                                f" (上报server_name={reported_name or sv.name})"
                            )
                    elif mtype in ("chat", "command"):
                        player = str(data.get("player", "") or "").strip()
                        display = str(data.get("display_name", "") or player).strip()
                        message = str(data.get("message", "") or "").strip()
                        ts = data.get("timestamp") or int(time.time() * 1000)
                        player_uuid = str(data.get("player_uuid", "") or "").strip()
                        reported_srv = str(data.get("server_name", "") or sv.name).strip()
                        is_command = bool(data.get("is_command", False)) or mtype == "command"
                        if not player or not message:
                            continue
                        # 命令和聊天都走 _spawn 后台执行（WS 不需要返回响应体）
                        self._spawn(
                            self._mc_message_safe_wrapper(
                                sv, player, display, message, int(ts), player_uuid, reported_srv, is_command
                            )
                        )
                    elif mtype == "execute_result":
                        request_id = str(data.get("request_id", "") or "").strip()
                        fut = self._exec_futures.get(request_id)
                        if fut is not None and not fut.done():
                            try:
                                fut.set_result({
                                    "success": bool(data.get("success", False)),
                                    "output": str(data.get("output", "") or ""),
                                })
                            except Exception:
                                pass
                elif msg.type == aiohttp.WSMsgType.CLOSE:
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.warning(f"[MCBridge][{sv.name}] WS连接异常: {ws.exception()!r}")
                    break
        finally:
            # 连接关闭后从 _ws_connections 移除（仅当存的还是本 ws 时才移除，避免误删新连接）
            if self._ws_connections.get(sv.name, {}).get("ws") is ws:
                self._ws_connections.pop(sv.name, None)
            logger.info(f"[MCBridge] WS连接断开: server={sv.name}")
        return ws

    async def _mc_message_safe_wrapper(
        self,
        sv: ServerCfg,
        player: str,
        display: str,
        message: str,
        ts: int,
        player_uuid: str,
        reported_srv: str,
        is_command: bool,
    ):
        """_on_mc_message 的顶层异常兜底：任何异常都给玩家一条明确的 tellraw 回执，避免"没下文"。"""
        try:
            await self._on_mc_message(sv, player, display, message, ts, player_uuid, reported_srv)
        except Exception as e:
            logger.exception(f"[MCBridge][{sv.name}] 处理MC消息异常: player={player} msg={message!r}")
            # 尽量用 tellraw 私聊给玩家一条提示（失败也不要紧，至少日志有了）
            try:
                if is_command or message.startswith("/"):
                    await self._tellraw_private(
                        sv, player,
                        f"[Bridge 内部错误：{type(e).__name__}] 请管理员查看 AstrBot 日志"
                    )
            except Exception:
                pass

    # ------------------------------------------------------------------ 身份合并 / 会话工具 / 消息桥发送

    def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self._bg_tasks.add(t)
        t.add_done_callback(self._bg_tasks.discard)
        return t

    # ---------- 身份合并（绑定QQ后用QQ号作为 impression/memory 的统一 user_id） ----------

    def _mc_identity(self, sv: ServerCfg, player: str, player_uuid: str) -> str:
        """MC侧身份主键。正版服按UUID；非正版按 mcs[server_name].player。绑定系统key也是它。"""
        if sv.online_mode and player_uuid and len(player_uuid) > 10:
            return f"uuid:{player_uuid}"
        return f"mcs[{sv.name}].{player}"

    def _qq_to_user_key(self, qq: str) -> str:
        """QQ侧身份 = user:<QQ号>。与 impression 插件 _get_key() 完全一致，实现跨平台共用印象。"""
        return f"user:{qq}"

    def _identity_user_key(self, sv: ServerCfg, player: str, player_uuid: str) -> tuple[str, str]:
        """返回 (user_key, display_label)。优先用绑定的QQ号合并；否则沿用 MC 内部格式。"""
        mid = self._mc_identity(sv, player, player_uuid)
        qq = self._bindings.get(mid)
        if qq:
            return self._qq_to_user_key(qq), f"QQ{qq}"
        # 未绑定：保持老标签兼容
        if sv.online_mode and player_uuid and len(player_uuid) > 10:
            return f"user:mcs[uuid:{player_uuid}].{player}", f"mcs[uuid:{player_uuid}]"
        return f"user:mcs[{sv.name}].{player}", f"mcs[{sv.name}]"

    def _impression_key_from_qq(self, qq: str) -> str:
        """impression 插件的 key 格式是 `user:<uid>`。直接复用QQ号，绑定后MC印象也写到这里。"""
        return f"user:{qq}"

    def _binding_for_qq(self, qq: str) -> Optional[str]:
        """由QQ号反查 mc_identity。"""
        return self._qq_to_mc.get(str(qq))

    def _save_bindings(self):
        """绑定表写盘。"""
        try:
            self._save_json(BINDING_FILE, self._bindings)
            # 同步刷新反查
            self._qq_to_mc = {qq: mid for mid, qq in self._bindings.items()}
            # 清理已解绑的元数据，保持一致
            self._binding_meta = {k: v for k, v in self._binding_meta.items() if k in self._bindings}
            self._save_json(BINDING_META_FILE, self._binding_meta)
        except Exception as e:
            logger.warning(f"[MCBridge] 写入绑定表失败: {e}")

    # ---------- MC/QQ 消息发送（tellraw + QQ 群/私聊） ----------

    def _render_tellraw(self, template: str, bot_name: str, message: str) -> str:
        text_json = (
            template.replace("{BOT_NAME}", bot_name).replace("{message}", message)
            .replace("\\", "\\\\").replace('"', '\\"')
        )
        return f'tellraw @a {{"text":"{text_json}"}}'

    def _render_tellraw_to(self, template: str, bot_name: str, player: str, message: str) -> str:
        """用 tellraw 私信给某玩家：tellraw [player_name] <[botname]> {message}
        按用户指定格式：tellraw {玩家名} 消息文本。
        """
        # template 里的 "{BOT_NAME}" 和 "{message}" 先组成实际消息内容
        rendered_content = (
            template.replace("{BOT_NAME}", bot_name).replace("{message}", message)
        )
        # 用户指定格式 = tellraw [player_name] <[botname]> {message}；其中 <[botname]>{message}
        # 已经包含在 rendered_content 里；所以直接用告诉玩家名 + 内容
        text_escaped = rendered_content.replace("\\", "\\\\").replace('"', '\\"')
        safe = player.replace("\\", "\\\\").replace('"', '\\"').strip() or "@p"
        return f'tellraw {safe} {{"text":"{text_escaped}"}}'

    async def _tellraw_broadcast(self, sv: ServerCfg, message: str):
        """全服 tellraw（普通聊天/系统公告）。通过 WS 发完整 tellraw 命令，MC端直接 dispatchCommand。"""
        cmd = self._render_tellraw(sv.tellraw_template, sv.bot_name, message)
        await self._ws_send(sv, {"type": "execute", "command": cmd})

    async def _tellraw_private(self, sv: ServerCfg, player: str, message: str, *, with_diagnose: bool = True) -> bool:
        """单独 tellraw 给某玩家（指令回执/错误/绑定提示等）。

        返回：是否发送成功。签名与返回值保持不变，调用方无需改动。
        """
        cmd = self._render_tellraw_to(sv.tellraw_private_template, sv.bot_name, player, message)
        ok = await self._ws_send(sv, {"type": "execute", "command": cmd})
        if ok:
            return True
        # 原模板可能太复杂导致发送失败，尝试退化成最简单的 tellraw
        if with_diagnose:
            fallback_cmd = f'tellraw {player} {{"text":"{message}"}}'
            ok2 = await self._ws_send(sv, {"type": "execute", "command": fallback_cmd})
            if ok2:
                return True
            logger.warning(
                f"[MCBridge][{sv.name}] tellraw_private(玩家={player}) WS发送失败，"
                f"MC端插件是否启动？WS连接是否正常？"
            )
        return False

    async def _ws_send(self, sv: ServerCfg, msg: dict) -> bool:
        """通过 WS 连接发送 JSON 消息给 MC 端。返回是否成功。"""
        conn = self._ws_connections.get(sv.name)
        if conn is None:
            # 尝试匹配上报的 server_name（握手时可能用了不同的名字）
            for name, info in self._ws_connections.items():
                if info.get("sv") is sv:
                    conn = info
                    break
        if conn is None:
            logger.warning(f"[MCBridge][{sv.name}] WS连接不存在，无法发送: {msg.get('type')}")
            return False
        ws = conn.get("ws")
        if ws is None or ws.closed:
            logger.warning(f"[MCBridge][{sv.name}] WS已关闭，无法发送: {msg.get('type')}")
            return False
        try:
            await ws.send_json(msg)
            return True
        except Exception as e:
            logger.warning(f"[MCBridge][{sv.name}] WS发送异常: {e}")
            return False

    async def _send_via(self, sv: ServerCfg, command: str):
        """统一 bridge/rcon 发送MC命令。bridge 模式走 WS（不再有反向HTTP通道）。"""
        if sv.send_channel == "rcon":
            try:
                await self._rcon_command(
                    sv.host, sv.mc_rcon_port, sv.mc_rcon_password, command
                )
                return True
            except Exception as e:
                logger.debug(f"[MCBridge][{sv.name}] rcon发送失败: {e}")
                return False
        return await self._ws_send(sv, {"type": "execute", "command": command})

    async def _cache_bot_from_event(self, event: AstrMessageEvent):
        """旁路缓存 bot 引用（后续MC→QQ消息发送、绑定私聊确认复用）。"""
        bot = getattr(event, "bot", None)
        if bot is not None:
            self._cached_bot = bot
            self._cached_platform_id = str(
                getattr(event, "get_platform_id", lambda: "")()
                or getattr(event, "unified_msg_origin", "")
                or ""
            ) or self._cached_platform_id

    async def _qq_send_to_group(self, group_id: str, text: str) -> bool:
        """向QQ群发消息，优先最近缓存的bot。"""
        gid = str(group_id).strip()
        if not gid or not text:
            return False
        bot = self._cached_bot
        if bot is None:
            return False
        api = getattr(bot, "api", None) or bot
        call_action = getattr(api, "call_action", None)
        if not callable(call_action):
            return False
        attempts = [int(gid)] if gid.isdigit() else [gid]
        if not isinstance(attempts[0], int):
            attempts.insert(0, int(gid)) if gid.isdigit() else None
        for g in attempts:
            try:
                await call_action("send_group_msg", group_id=g, message=str(text))
                return True
            except Exception as e:
                logger.debug(f"[MCBridge] 群{gid} send_group_msg失败: {e}")
        return False

    async def _qq_send_private(self, qq_number: str, text: str) -> bool:
        """优先发私聊。失败返回False，调用方可以回退群@。"""
        qq = str(qq_number).strip()
        if not qq.isdigit() or not text:
            return False
        bot = self._cached_bot
        if bot is None:
            return False
        api = getattr(bot, "api", None) or bot
        call_action = getattr(api, "call_action", None)
        if not callable(call_action):
            return False
        for v in (int(qq), qq):
            try:
                await call_action("send_private_msg", user_id=v, message=str(text))
                return True
            except Exception as e:
                logger.debug(f"[MCBridge] QQ{qq} 私聊失败: {e}")
        return False

    async def _qq_groups_of_user(self, qq_number: str) -> list[str]:
        """查找目标QQ号和机器人的共同群列表（用于私聊失败回退群@确认）。"""
        bot = self._cached_bot
        if bot is None:
            return []
        api = getattr(bot, "api", None) or bot
        call = getattr(api, "call_action", None)
        if not callable(call):
            return []
        try:
            groups = await call("get_group_list")
        except Exception:
            return []
        result: list[str] = []
        if not isinstance(groups, list):
            return []
        for g in groups:
            if not isinstance(g, dict):
                continue
            gid = str(g.get("group_id", "") or "")
            if not gid.isdigit():
                continue
            try:
                members = await call("get_group_member_list", group_id=int(gid))
            except Exception:
                continue
            if not isinstance(members, list):
                continue
            for m in members:
                if isinstance(m, dict) and str(m.get("user_id", "")) == str(qq_number):
                    result.append(gid)
                    break
        return result

    def _passes_filters(self, sv: ServerCfg, player: str, message: str) -> bool:
        if sv.player_whitelist and player not in sv.player_whitelist:
            return False
        if player in sv.player_blacklist:
            return False
        for kw in sv.message_filter:
            if kw and kw in message:
                return False
        return True

    # ---------- MC→QQ 消息桥转发 ----------
    def _should_forward_mc_to_qq(self, sv: ServerCfg) -> list[str]:
        """返回需要转发的QQ群号列表（订阅此服务器的群号 + 订阅*的群号 去重）。"""
        result: list[str] = []
        seen: set[str] = set()
        for gid, srvs in self._forwards.items():
            if sv.name in srvs or "*" in srvs:
                if gid not in seen:
                    seen.add(gid)
                    result.append(gid)
        return result

    async def _forward_mc_chat_to_qq(self, sv: ServerCfg, entry: dict):
        fmt = str(self.config.get("FORWARD_FMT_MC_TO_QQ", "[MC|{server}] <{display}> {message}") or "[MC|{server}] <{display}> {message}")
        try:
            text = fmt.format(
                server=entry.get("server", sv.name),
                player=entry.get("player", ""),
                display=entry.get("name", entry.get("player", "")),
                message=entry.get("message", ""),
                time=entry.get("time", datetime.now().strftime("%H:%M:%S")),
            )
        except Exception:
            text = f"[MC|{entry.get('server',sv.name)}] <{entry.get('name','')}> {entry.get('message','')}"
        groups = self._should_forward_mc_to_qq(sv)
        for gid in groups:
            await asyncio.sleep(0)
            ok = await self._qq_send_to_group(gid, text)
            if not ok:
                logger.debug(f"[MCBridge][{sv.name}] 群{gid} 转发失败（未缓存bot/无权限）")

    # ---------- MC端自然语言指令 ----------
    def _check_nl_cooldown(self, sv: ServerCfg, player: str) -> bool:
        cd = int(self.config.get("CMD_NL_COOLDOWN", 15))
        if cd <= 0:
            return True
        k = (sv.name, player)
        now = time.time()
        last = self._last_nl_cmd_ts.get(k, 0.0)
        if now - last < cd:
            return False
        self._last_nl_cmd_ts[k] = now
        return True

    def _permission_for(self, sv: ServerCfg, player: str, player_uuid: str) -> str:
        """绑定玩家权限 = 跟随绑定QQ号；否则返回 'unbound'。"""
        mid = self._mc_identity(sv, player, player_uuid)
        qq = self._bindings.get(mid)
        if not qq:
            return "unbound"
        try:
            super_admins = self.config.get("SUPER_ADMIN_IDS", [])
            if isinstance(super_admins, str):
                super_admins = [x.strip() for x in super_admins.split(",") if x.strip()]
            if str(qq) in [str(x) for x in (super_admins or [])]:
                return "superadmin"
        except Exception:
            pass
        # 粗略：没有 AstrBot PermissionType 枚举时回退为 member。调用方需自行再用 check_permission。
        return "member"

    async def _handle_mc_command(self, sv: ServerCfg, player: str, display: str, message: str, ts: int, player_uuid: str):
        """MC端输入的 指令消息（/开头）。目前只处理 /bind /unbind /ai 前缀。

        /bind <QQ号>：游戏内绑定，会在QQ端向 <QQ号> 真人发二次确认
        /unbind：解绑当前MC玩家
        /ai <自然语言>：显式进入"自然语言指令"模式，独立冷却15s。
        其它命令：AstrBot端不介入，原样忽略。
        """
        stripped = message.strip()
        if not stripped.startswith("/"):
            return False

        # ----- /unbind -----
        if re.match(r"^/unbind\b", stripped):
            mid = self._mc_identity(sv, player, player_uuid)
            if mid in self._bindings:
                qq = self._bindings.pop(mid)
                self._qq_to_mc = {v: k for k, v in self._bindings.items()}
                self._save_bindings()
                ok = await self._tellraw_private(sv, player, f"已与QQ号 {qq} 解除绑定。")
                if not ok:
                    logger.warning(f"[MCBridge][{sv.name}] /unbind 回执发送失败 (bridge不通)，player={player}")
            else:
                ok = await self._tellraw_private(sv, player, "你还没有绑定任何QQ号。")
                if not ok:
                    logger.warning(f"[MCBridge][{sv.name}] /unbind 回执发送失败 (bridge不通)，player={player}")
            return True

        # ----- /bind <QQ号> -----
        m = re.match(r"^/bind\s+([0-9]{5,14})\s*$", stripped)
        if m:
            qq = m.group(1)
            mid = self._mc_identity(sv, player, player_uuid)
            if mid in self._bindings:
                await self._tellraw_private(sv, player, f"当前已绑定QQ {self._bindings[mid]}，如需换绑请先 /unbind。")
                return True
            if qq in self._qq_to_mc:
                await self._tellraw_private(sv, player, f"该QQ号已被另一名玩家绑定。")
                return True
            if not self._cached_bot:
                await self._tellraw_private(
                    sv, player,
                    "绑定失败：机器人暂未缓存。请先在QQ侧给机器人随便发一条消息（私聊或任意群里都可），再重试。"
                )
                return True
            token = f"bind_{int(time.time()*1000)}_{abs(hash((mid,qq))) % 1000000:06d}"
            timeout_at = time.time() + int(self.config.get("BIND_CONFIRM_TIMEOUT", 300))
            self._pending_binds[token] = {
                "mc_id": mid, "qq": qq, "server": sv.name,
                "player": player, "display": display or player,
                "player_uuid": player_uuid, "timeout_at": timeout_at,
            }
            # 1) 优先私聊确认
            ask_prompt = (
                f"【身份绑定二次确认】\n"
                f"玩家 [{sv.name}] {display or player} 尝试把MC身份与你的QQ号绑定。\n"
                f"确认请回复：同意 / 是 / y / 确认\n"
                f"拒绝请回复：拒绝 / 否 / n / cancel"
            )
            ok_private = False
            private_err = ""
            try:
                # 私聊超时从 6.0s 缩到 2.0s：保证 /bind 整体在 HTTP 3.8s wait_for 内得出结果
                ok_private = await asyncio.wait_for(self._qq_send_private(qq, ask_prompt), timeout=2.0)
            except Exception as e:
                ok_private = False
                private_err = f"{type(e).__name__}: {e}"
            if ok_private:
                await self._tellraw_private(
                    sv, player,
                    f"✅ [私聊通道] 已向 QQ {qq} 发送绑定确认，请对方私聊回复「同意」。"
                    f"({int(self.config.get('BIND_CONFIRM_TIMEOUT', 300))}秒内有效)"
                )
            else:
                # 2) 失败则找共同群，群@确认
                groups = await self._qq_groups_of_user(qq)
                sent = False
                sent_group = ""
                group_err_list: list[str] = []
                for idx, g in enumerate(groups):
                    # 每个群单条发送也设 1.5s 超时；超过 3 个群就 break（避免整体超时）
                    if idx >= 3:
                        break
                    txt = (
                        f"[CQ:at,qq={qq}] "
                        f"【MC绑定二次确认】{display or player} [{sv.name}] 请求与你绑定。\n"
                        f"同意/是/y 或 拒绝/否/n 以完成。"
                    )
                    try:
                        if await asyncio.wait_for(self._qq_send_to_group(g, txt), timeout=1.5):
                            self._pending_binds[token]["group_id"] = g
                            sent = True
                            sent_group = g
                            break
                    except Exception as e2:
                        group_err_list.append(f"群{g}:{type(e2).__name__}")
                if sent:
                    await self._tellraw_private(
                        sv, player,
                        f"✅ [共同群通道] 已在群 {sent_group} @QQ {qq}，请对方回复「同意/是/y」确认。"
                    )
                else:
                    self._pending_binds.pop(token, None)
                    # 给玩家一个完整诊断：私聊失败原因 + 共同群数量 + 建议
                    diag_parts = [f"❌ 无法联系到 QQ {qq}。"]
                    if private_err:
                        diag_parts.append(f"私聊失败：{private_err}")
                    diag_parts.append(
                        f"找到共同群 {len(groups)} 个（{len(group_err_list)} 个群发送出错）。"
                    )
                    diag_parts.append(
                        "解决方法：① 先把机器人加成 QQ 好友，再重试；② 或把机器人拉到你所在的一个 QQ 群里，再重试。"
                    )
                    diag_parts.append("注：机器人必须先收到过一条 QQ 消息（私聊/群里都可）才能发送，请确认。")
                    await self._tellraw_private(sv, player, "  ".join(diag_parts))
                    logger.warning(
                        f"[MCBridge][{sv.name}] /bind 联系QQ{qq} 完全失败："
                        f"私聊err={private_err or 'ok_private=False/API静默失败'} "
                        f"共同群数量={len(groups)} 群发送错误={group_err_list!r}"
                    )
            return True

        # ----- /ai <自然语言> -----
        m2 = re.match(r"^/ai\s*(.*)$", stripped, re.DOTALL)
        if m2:
            content = m2.group(1).strip()
            if not bool(self.config.get("ENABLE_MC_NL_COMMAND", True)):
                await self._tellraw_private(sv, player, "管理员关闭了MC自然语言指令通道。")
                return True
            if not content:
                await self._tellraw_private(sv, player, "用法：/ai <用自然语言说你想让机器人在MC里做什么>")
                return True
            if not self._check_nl_cooldown(sv, player):
                await self._tellraw_private(sv, player, f"指令冷却中（{int(self.config.get('CMD_NL_COOLDOWN',15))}秒），请稍后再试。")
                return True
            await self._dispatch_mc_natural_command(sv, player, display, player_uuid, content, forced=True)
            return True
        return False

    async def _dispatch_mc_natural_command(self, sv: ServerCfg, player: str, display: str, player_uuid: str, content: str, forced: bool):
        """MC侧自然语言指令：让LLM判断意图=执行安全指令 / 危险指令拒绝 / 不是指令当普通聊天。
        回执一律 tellraw @player 私发（按 tellraw_private_template）。
        """
        logger.info(f"[MCBridge][{sv.name}] 自然语言指令: player={player} content={content!r} forced={forced}")
        perm = self._permission_for(sv, player, player_uuid)
        if perm == "unbound":
            logger.info(f"[MCBridge][{sv.name}] 自然语言指令跳过: player={player} 未绑定QQ")
            await self._tellraw_private(sv, player, "你还未绑定QQ号，仅QQ绑定玩家可用自然语言执行指令。请先发送 /bind <QQ号>。")
            return
        # 先调用LLM判别+生成安全指令或拒绝语
        plan = await self._mc_nl_route_with_llm(sv, player, display, content, perm, forced)
        action = plan.get("action")
        logger.info(f"[MCBridge][{sv.name}] LLM路由结果: action={action} command={plan.get('command','')} message={plan.get('message','')[:80]}")
        if action == "deny":
            msg = plan.get("message") or "这是一条危险指令，请直接使用服务器命令（非自然语言）。"
            await self._tellraw_private(sv, player, msg)
            return
        if action == "not_command":
            # 不是指令：若为 /ai 强制模式，也提示；否则不做动作交给上层当聊天
            if forced:
                msg = plan.get("message") or "无法从你的话里识别出要执行什么MC指令。"
                await self._tellraw_private(sv, player, msg)
            return
        if action == "execute_safe":
            # 安全指令：tellraw @player 回传"指令+输出"
            cmd = plan.get("command", "").strip()
            if not cmd.startswith("/"):
                cmd = "/" + cmd
            await self._tellraw_private(sv, player, f"识别为指令：{cmd}，开始执行。")
            # 执行：如果是普通 say/me/list/help/tps 等查询，通过bridge/rcon发
            if cmd.startswith("/mc_"):
                # AstrBot自己的/mc系列，游戏内不便解析，拒绝给提示
                await self._tellraw_private(sv, player, f"游戏内不支持 {cmd}，请在 AstrBot/QQ 侧执行。")
                return
            # 这里直接发给MC服务器执行，把输出也tellraw回玩家（输出过长会被自动截断）
            out = await self._exec_and_capture_output(sv, cmd)
            out_text = out if out else "（无返回结果）"
            # tellraw 私发：命令结果
            lines = [l for l in out_text.splitlines() if l.strip()][:6]
            for line in lines:
                await self._tellraw_private(sv, player, line[:200])
            return
        # unknown
        await self._tellraw_private(sv, player, "自然语言指令解析失败。")

    async def _exec_and_capture_output(self, sv: ServerCfg, cmd: str) -> str:
        """执行MC命令并捕获输出。bridge模式通过 WS 发 execute 等待 MC 端回 execute_result；RCON直接返回。"""
        real_cmd = cmd[1:] if cmd.startswith("/") else cmd
        if sv.send_channel == "rcon":
            try:
                res = await self._rcon_command(sv.host, sv.mc_rcon_port, sv.mc_rcon_password, real_cmd)
                return (res or "").strip()
            except Exception as e:
                return f"[RCON 错误] {e}"
        # bridge 模式：通过 WS 发 execute，等待 execute_result
        import uuid as _uuid
        request_id = str(_uuid.uuid4())
        fut = asyncio.get_event_loop().create_future()
        self._exec_futures[request_id] = fut
        ok = await self._ws_send(sv, {"type": "execute", "command": real_cmd, "request_id": request_id})
        if not ok:
            self._exec_futures.pop(request_id, None)
            return "[Bridge WS发送失败]"
        try:
            result = await asyncio.wait_for(fut, timeout=10)
            self._exec_futures.pop(request_id, None)
            success = result.get("success", False)
            output = result.get("output", "")
            if not success:
                return f"[执行失败] {output}" if output else "[执行失败]"
            return output or ""
        except asyncio.TimeoutError:
            self._exec_futures.pop(request_id, None)
            return "[Bridge 执行超时(10s)]"

    async def _mc_nl_route_with_llm(self, sv: ServerCfg, player: str, display: str, content: str, perm: str, forced: bool) -> dict:
        """LLM路由：返回 {action: 'deny'|'execute_safe'|'not_command', ...}。

        危险指令关键词清单（仅阻断服务器安全级操作）：
          服管：stop / restart / shutdown
          封禁/权限：ban / ban-ip / pardon / op / deop / whitelist
        give/gamemode/tp/summon/setblock/fill/clone/effect/clear 等不阻断（由LLM根据权限等级决定）。

        【关键设计】：该调用走 _llm_generate_clean(skip_persona=True)，
        不叠加 AstrBot 默认 persona、印象、记忆等钩子，避免模型输出"我不是仓库管理员"
        这类与指令路由任务完全无关的、来自聊天人设立场的拒绝。
        """
        dangerous = r"\b(stop|restart|shutdown|ban|ban-ip|pardon|op|deop|whitelist)\b"
        MUST_HAVE_HINT = "请直接使用游戏内命令（非自然语言）"
        ROLE_PROMPT = (
            "你是 Minecraft 服务器的「指令路由器」，不是聊天助手，也不是玩家的朋友或管家。"
            "你没有任何身份、性格或人设。你的唯一职责就是严格输出一段 JSON。"
            "禁止输出任何 JSON 代码块之外的文字，禁止用自然语言解释、道歉、拒绝、寒暄。"
        )
        lines = [
            "任务：把玩家的自然语言请求，判定为以下 3 种动作之一，并用 JSON 输出。",
            "动作1：execute_safe = 可执行指令（time/weather/help/list/me/say/tp/give/gamemode/summon/setblock/fill/clone/effect/clear 等常规游戏指令），必须输出一条具体的 Minecraft 命令。",
            f"动作2：deny = 危险指令（仅 stop/restart/shutdown/ban/ban-ip/pardon/op/deop/whitelist 等服务器安全级操作），用自然语言写一段礼貌的拒绝说明，并且 message 结尾必须包含「{MUST_HAVE_HINT}」。",
            '动作3：not_command = 这不是指令意图，只是普通闲聊（如"今天天气不错"/"你好"），或你无法识别为可执行 MC 指令的请求，输出 message 字段简短解释为什么不是指令。',
            f"当前玩家权限等级={perm}（仅 superadmin/admin 能做更重操作，member 只能做查询/自操作，unbound 不允许执行）。",
            f"玩家={display or player}, 服务器={sv.name}",
            "输出格式必须是纯 JSON，不要加 markdown、不要加 ```json、不要加注释。格式：",
            "{\"action\":\"execute_safe|deny|not_command\", \"command\":\"/xxx 仅当action=execute_safe时填\", \"message\":\"解释或拒绝语\"}",
            "",
            "玩家原话：" + content,
        ]
        prompt = "\n".join(lines)
        raw = await self._llm_generate_clean(prompt, ROLE_PROMPT, skip_persona=True)

        def _ensure_hint(msg: str) -> str:
            m = (msg or "").strip()
            if not m:
                return f"这条指令会影响服务器或其他玩家，{MUST_HAVE_HINT}。"
            if MUST_HAVE_HINT in m:
                return m
            return f"{m}（{MUST_HAVE_HINT}）"

        # ---- 兜底1：LLM 根本没返回，或返回里出现"人设式拒绝"（即使 skip_persona 也防一层） ----
        persona_refuse_re = r"(我不是|我不能|我无法|我只是|做不到|没有这个能力|没有权限|无法帮助|不能帮助|不能替你|不负责|我是一个)(仓库|管理|OP|服主|管理员|游戏内|服务器|物品|搬|取|箱子|红石|指令|命令)"
        if not raw:
            return {"action": "not_command", "message": "指令识别失败：LLM未返回结果。"}
        raw_stripped = raw.strip()
        if re.search(persona_refuse_re, raw_stripped, re.I):
            # 说明 model / hook 仍强行注入了"聊天人设"拒绝：把它改写成玩家可理解的解释 + 强制不是指令
            # 如果内容里含有危险词，则当作危险拒绝处理
            if re.search(dangerous, content, re.I):
                return {"action": "deny", "message": _ensure_hint("")}
            return {
                "action": "not_command",
                "message": "没有从你的话里识别出可执行的MC指令。如果要做服务器安全级操作（关闭/封禁/OP等），请直接使用游戏内命令。",
            }
        # ---- 剥离 ```json 包裹 ----
        if raw_stripped.startswith("```"):
            raw_stripped = re.sub(r"^```(?:json)?\s*", "", raw_stripped)
            raw_stripped = re.sub(r"\s*```$", "", raw_stripped)
        data = {}
        try:
            data = json.loads(raw_stripped)
        except Exception:
            # 从文本里尽量取 JSON 子串
            m = re.search(r"\{[\s\S]*\}", raw_stripped)
            if m:
                try:
                    data = json.loads(m.group(0))
                except Exception:
                    data = {}
        if not isinstance(data, dict):
            data = {}
        action = str(data.get("action", "")).strip().lower()
        proposed_cmd = str(data.get("command", "")).strip()

        # ---- 最终危险兜底：即使 LLM 把危险词放行成 execute_safe，也强制改回 deny ----
        if proposed_cmd and re.search(dangerous, proposed_cmd, re.I):
            return {"action": "deny", "message": _ensure_hint(data.get("message") or "")}
        # 用户原话里含危险词，但 LLM 误判成 not_command/execute_safe 且没给出 command：按 deny 算
        if (not proposed_cmd) and re.search(dangerous, content, re.I) and action != "deny":
            return {"action": "deny", "message": _ensure_hint(data.get("message") or "")}

        if action == "execute_safe":
            if not proposed_cmd:
                return {"action": "not_command", "message": "没有识别出具体可执行指令。你可以直接用游戏内命令。"}
            return {"action": "execute_safe", "command": proposed_cmd}
        if action == "deny":
            return {"action": "deny", "message": _ensure_hint(data.get("message") or "")}
        return {"action": "not_command", "message": str(data.get("message") or "没有识别出指令意图。")}

    # ---------- 主入口：_on_mc_message ------------
    async def _on_mc_message(
        self,
        sv: ServerCfg,
        player: str,
        display: str,
        message: str,
        ts: int,
        player_uuid: str,
        reported_srv: str,
    ):
        logger.info(f"[MCBridge][{sv.name}] 收到消息: player={player} msg={message!r} uuid={player_uuid[:8] if player_uuid else 'N/A'}")
        if not self._passes_filters(sv, player, message):
            logger.debug(f"[MCBridge][{sv.name}] 消息被过滤器拦截: player={player} msg={message!r}")
            return
        user_key, _label = self._identity_user_key(sv, player, player_uuid)
        entry = {
            "player": player,
            "name": display or player,
            "player_uuid": player_uuid,
            "server": sv.name,
            "message": message,
            "timestamp": ts,
            "is_bot": False,
            "time": datetime.now().strftime("%H:%M:%S"),
        }
        # ---- 1) MC端命令优先处理（/bind /unbind /ai） ----
        try:
            cmd_handled = await self._handle_mc_command(sv, player, display, message, ts, player_uuid)
        except Exception as e:
            logger.warning(f"[MCBridge][{sv.name}] _handle_mc_command 异常: {e}")
            cmd_handled = False
        # 绑定后用QQ私聊session_id，未绑定用MC session_id
        sid = self._unified_session_id(sv, player, player_uuid)
        if cmd_handled:
            # 命令事件也写入统一会话
            async with self._lock:
                ses = self._sessions.setdefault(sid, [])
                ses.append({**entry, "event": "mc_cmd"})
                max_h = int(self.config.get("MAX_HISTORY_PER_SERVER", 300))
                if len(ses) > max_h:
                    ses[:] = ses[-max_h:]
                self._save_json(SESSION_FILE, self._sessions)
            return

        # ---- 2) 记录会话（绑定后写入QQ私聊会话） ----
        async with self._lock:
            ses = self._sessions.setdefault(sid, [])
            ses.append(entry)
            max_h = int(self.config.get("MAX_HISTORY_PER_SERVER", 300))
            if len(ses) > max_h:
                ses[:] = ses[-max_h:]
            self._save_json(SESSION_FILE, self._sessions)

        # 交互计数（用户级，用于印象触发；正版合并时跨服同一个user_key）
        self._interaction_count[user_key] = self._interaction_count.get(user_key, 0) + 1

        # ---- 3) MC→QQ 消息桥转发 ----
        if bool(self.config.get("ENABLE_FORWARD_MC_TO_QQ", True)):
            self._spawn(self._forward_mc_chat_to_qq(sv, entry))

        # ---- 4) 同步到 memory_companion（异步、批处理） ----
        if bool(self.config.get("ENABLE_SYNC_TO_MEMORY_COMPANION", True)):
            self._spawn(
                self._buffer_and_sync_memory(
                    sv, user_key, player, display, message, "chat", player_uuid=player_uuid,
                    session_id=sid,
                )
            )

        # ---- 5) 自然语言指令意图识别（未触发关键词也会跑，但独立冷却） ----
        triggered = bool(self.config.get("ENABLE_LLM_REPLY", True)) and self._should_trigger(sv, message)
        nl_ran = False
        enable_nl = bool(self.config.get("ENABLE_MC_NL_COMMAND", True))
        enable_nl_auto = bool(self.config.get("ENABLE_NL_COMMAND_AUTO_DETECT", True))
        if enable_nl and enable_nl_auto and self._check_nl_cooldown(sv, player):
            # 非绑定玩家不浪费LLM，但仍允许普通聊天回复
            if self._permission_for(sv, player, player_uuid) != "unbound":
                self._last_nl_cmd_ts[(sv.name, player)] = self._last_nl_cmd_ts.get((sv.name, player), 0.0)
                self._spawn(self._dispatch_mc_natural_command(sv, player, display, player_uuid, message, forced=False))
                nl_ran = True

        # ---- 6) 触发 LLM 聊天回复？（关键词/@bot 或 未识别指令时） ----
        if triggered:
            async with self._reply_cooldown(sv, player):
                if not await self._can_reply_now(sv, player):
                    return
                reply = await self._generate_reply(sv, player, display, message, player_uuid, user_key, sid)
            if reply:
                # 回复发到MC tellraw + 如果绑定了QQ也推到QQ私聊
                await self._tellraw_broadcast(sv, reply)
                await self._push_reply_to_qq_if_bound(sv, player, player_uuid, reply)
                async with self._lock:
                    bot_entry = {
                        "player": sv.bot_name,
                        "name": sv.bot_name,
                        "player_uuid": "",
                        "server": sv.name,
                        "message": reply,
                        "timestamp": int(time.time() * 1000),
                        "is_bot": True,
                        "time": datetime.now().strftime("%H:%M:%S"),
                    }
                    ses = self._sessions.setdefault(sid, [])
                    ses.append(bot_entry)
                    max_h = int(self.config.get("MAX_HISTORY_PER_SERVER", 300))
                    if len(ses) > max_h:
                        ses[:] = ses[-max_h:]
                    self._save_json(SESSION_FILE, self._sessions)
                if bool(self.config.get("ENABLE_SYNC_TO_MEMORY_COMPANION", True)):
                    self._spawn(
                        self._buffer_and_sync_memory(
                            sv, user_key, sv.bot_name, sv.bot_name, reply, "llm_reply",
                            player_uuid="", session_id=sid,
                        )
                    )
                if bool(self.config.get("ENABLE_SYNC_TO_IMPRESSION", True)):
                    self._spawn(
                        self._try_update_impression(
                            user_key, player, display, sv, player_uuid, triggered=True
                        )
                    )
        else:
            # 未触发聊天回复，但仍可能达到交互次数 -> 更印象
            if bool(self.config.get("ENABLE_SYNC_TO_IMPRESSION", True)):
                self._spawn(
                    self._try_update_impression(
                        user_key, player, display, sv, player_uuid, triggered=False
                    )
                )

    def _should_trigger(self, sv: ServerCfg, message: str) -> bool:
        bot_name = sv.bot_name.strip()
        for kw in sv.trigger_keywords:
            if kw and str(kw) in message:
                return True
        if sv.enable_at_trigger and bot_name:
            if (
                message.startswith(bot_name)
                or message.startswith(f"@{bot_name}")
                or f"@{bot_name}" in message
                or message.lower().startswith(bot_name.lower())
            ):
                return True
        return False

    def _reply_cooldown(self, sv: ServerCfg, player: str):
        return _DummyContext()  # 已通过 _can_reply_now 检查，这里仅占位语法糖

    async def _can_reply_now(self, sv: ServerCfg, player: str) -> bool:
        cd = int(self.config.get("LLM_REPLY_COOLDOWN", 3))
        if cd <= 0:
            return True
        k = (sv.name, player)
        now = time.time()
        last = self._last_reply_ts.get(k, 0.0)
        if now - last < cd:
            return False
        self._last_reply_ts[k] = now
        return True

    # ------------------------------------------------------------------ LLM 回复生成（含跨服上下文）

    async def _get_system_prompt(self) -> str:
        pm = getattr(self.context, "persona_manager", None)
        if pm is None:
            return ""
        persona = None
        try:
            persona = await pm.get_default_persona_v3(umo=None)
        except Exception:
            try:
                persona = pm.get_default_persona_v3(umo=None)
            except Exception as e:
                logger.debug(f"[MCBridge] get_default_persona_v3 失败: {e}")
                return ""
        if not persona:
            return ""
        sp = getattr(persona, "prompt", None)
        if not sp and isinstance(persona, dict):
            sp = persona.get("prompt", "")
        return str(sp or "")

    def _get_provider_id(self) -> Optional[str]:
        try:
            provider = self.context.get_using_provider()
            if provider is not None:
                meta = provider.meta() if callable(getattr(provider, "meta", None)) else None
                pid = getattr(meta, "id", None)
                if pid:
                    return pid
        except Exception as e:
            logger.debug(f"[MCBridge] get_using_provider 失败: {e}")
        try:
            return self.context.get_current_chat_provider_id(None)
        except Exception:
            return None

    async def _llm_generate_text(self, prompt: str, system_prompt: str) -> Optional[str]:
        """普通聊天式 LLM 调用：叠加默认 persona（_get_system_prompt），走 AstrBot 原生 llm_generate 钩子链。
        适用：MC/QQ 侧普通闲聊回复、印象生成。"""
        return await self._llm_generate_clean(prompt, system_prompt, skip_persona=False)

    async def _llm_generate_clean(self, prompt: str, system_prompt: str, *, skip_persona: bool = False) -> Optional[str]:
        """底层 LLM 调用。
        - skip_persona=False（默认）：system_prompt 会叠加 AstrBot 默认人设 prompt，适合普通聊天。
        - skip_persona=True：**完全不叠加**默认人设/印象/记忆等任何 AstrBot 钩子注入的 system 内容，
          只使用调用方显式传入的 system_prompt 作为角色约束。适用于：
          * MC 自然语言指令路由（必须严格输出 JSON，不能冒出"我不是仓库管理员"这类人设拒绝）
          * 其它要求 LLM 输出严格结构化 JSON 的任务。
        """
        provider_id = self._get_provider_id()
        if not provider_id:
            logger.warning("[MCBridge] 未找到可用的 LLM provider，跳过回复")
            return None
        kwargs = {"chat_provider_id": provider_id, "prompt": prompt}
        if skip_persona:
            # 关键：显式传我们自带的干净 system_prompt，并且不走 _get_system_prompt 叠加。
            # llm_generate 收到非空的 system_prompt 参数时，通常会以"传入者优先"覆盖默认人设。
            # 为了保险同时避免印象插件在 pre_invoke 钩子内改写 system_prompt，这里用一个"明确覆盖"的策略：
            # 如果最终文本里含"我不是仓库管理员"等不相关拒绝，说明钩子链仍被注入——解析层会兜底处理。
            if system_prompt:
                kwargs["system_prompt"] = system_prompt
        else:
            # 默认模式：合并默认 persona + 调用方传入的额外角色约束（如果有）
            default_sp = await self._get_system_prompt()
            merged = ""
            if default_sp:
                merged = default_sp
            if system_prompt:
                merged = (merged + "\n\n" + system_prompt) if merged else system_prompt
            if merged:
                kwargs["system_prompt"] = merged
        try:
            resp = await self.context.llm_generate(**kwargs)
            text = resp.completion_text if resp else None
            return text.strip() if text else None
        except Exception as e:
            logger.warning(f"[MCBridge] llm_generate 失败: {e}")
            return None

    # --- 共享记忆检索：调用 memory_companion.bridge.search / compose_context 注入 LLM ---
    async def _compose_shared_memory(self, user_key: str, session_id: str, query: str, limit: int = 12) -> list[str]:
        """调用 memory_companion 的 bridge.search（若存在），按 query 检索共享长期记忆。
        返回若干行文本（可直接拼到 prompt 里）。QQ与MC两侧共用。
        """
        result: list[str] = []
        companion = self._get_memory_companion()
        if not companion:
            return result
        bridge = (
            getattr(companion, "bridge", None)
            or getattr(companion, "_bridge", None)
            or getattr(companion, "memory_companion", None)
        )
        if not bridge:
            return result
        search = callable(getattr(bridge, "search", None))
        compose = callable(getattr(bridge, "compose_context", None))
        hits: list[str] = []
        # 1) 优先 compose_context
        if compose:
            try:
                import inspect
                params = inspect.signature(bridge.compose_context).parameters
                kw = {"user_id": user_key, "session_id": session_id,
                      "query": query, "k": limit, "limit": limit}
                kw = {k: v for k, v in kw.items() if k in set(params.keys())} if params else {
                    "query": query, "session_id": session_id
                }
                out = await bridge.compose_context(**kw)
                if isinstance(out, str) and out.strip():
                    hits.append(out.strip())
                elif isinstance(out, list):
                    hits.extend(str(x).strip() for x in out if str(x).strip())
            except Exception as e:
                logger.debug(f"[MCBridge] compose_context 失败: {e}")
        # 2) 其次 search
        if not hits and search:
            try:
                q = query.strip() or "最近发生了什么"
                out = await bridge.search(q, user_id=user_key, session_id=session_id, limit=limit)
                if isinstance(out, list):
                    hits.extend(str(x).strip() for x in out if str(x).strip())
                elif isinstance(out, str) and out.strip():
                    hits.append(out.strip())
            except Exception as e:
                logger.debug(f"[MCBridge] bridge.search 失败: {e}")
        # 整理成行
        if hits:
            result.append("【共享记忆（QQ/MC通用）】")
            for i, h in enumerate(hits[:limit]):
                for line in str(h).splitlines()[:4]:
                    if line.strip():
                        result.append(f"- {line.strip()}")
        return result

    async def _generate_reply(
        self,
        sv: ServerCfg,
        player: str,
        display: str,
        message: str,
        player_uuid: str,
        user_key: str,
        sid: str = "",
    ) -> Optional[str]:
        """生成LLM回复。绑定后sid=QQ私聊session_id，上下文/记忆自动与QQ私聊共享。"""
        if not sid:
            sid = self._unified_session_id(sv, player, player_uuid)
        ctx_count = int(self.config.get("LLM_CONTEXT_COUNT", 20))
        history = list(self._sessions.get(sid, []))
        recent = history[-ctx_count:] if ctx_count > 0 else history

        # 正版服：若 ENABLE_CROSS_SERVER_CONTEXT，补充其他正版服同名 UUID 用户的近期消息
        cross_parts = []
        if (
            sv.online_mode
            and player_uuid
            and bool(self.config.get("ENABLE_CROSS_SERVER_CONTEXT", True))
        ):
            extra = []
            for srv_name, srv_ses in self._sessions.items():
                if srv_name == sid:
                    continue
                srv_obj = None
                for sn, obj in self._servers.items():
                    if self._mc_session_id(obj) == srv_name:
                        srv_obj = obj
                        break
                if srv_obj is None or not srv_obj.online_mode:
                    continue
                for e in srv_ses[-ctx_count:]:
                    if str(e.get("player_uuid", "")) == player_uuid:
                        extra.append((srv_name, e))
            if extra:
                lines = [f"【其它正版服上下文】"]
                for srv_name, e in extra:
                    who = (
                        sv.bot_name
                        if e.get("is_bot")
                        else e.get("name", e.get("player", ""))
                    )
                    lines.append(
                        f"[{srv_name}|{e.get('time','')}] <{who}> {e.get('message','')}"
                    )
                cross_parts.append("\n".join(lines))

        # 共享记忆检索（QQ/MC合并）
        mem_lines = await self._compose_shared_memory(
            user_key=user_key,
            session_id=sid,
            query=message,
        )
        if mem_lines:
            cross_parts.insert(0, "\n".join(mem_lines))

        lines = [f"【服务器: {sv.name} 最近聊天】"]
        for e in recent:
            who = sv.bot_name if e.get("is_bot") else e.get("name", e.get("player", ""))
            lines.append(f"[{e.get('time','')}] <{who}> {e.get('message','')}")
        lines.append("")
        lines.append(f"刚刚玩家 <{display or player}> ({user_key}) 提到了你。")
        lines.append(
            f"请你用 {sv.bot_name} 的身份，结合聊天上下文用一句简短自然的口语回复。"
            f"不要加名字前缀，直接输出回复内容即可；允许使用换行符。"
        )
        prompt_parts = ["\n".join(lines)]
        if cross_parts:
            prompt_parts.extend(cross_parts)
        prompt = "\n\n".join(prompt_parts)
        sp = await self._get_system_prompt()
        reply = await self._llm_generate_text(prompt, sp)
        if not reply:
            return None
        return reply.strip().strip('"').strip("'") or None

    async def _push_reply_to_qq_if_bound(self, sv: ServerCfg, player: str, player_uuid: str, reply: str):
        """如果MC玩家绑定了QQ，把bot回复也推到QQ私聊（双向同步）。"""
        mid = self._mc_identity(sv, player, player_uuid)
        qq = self._bindings.get(mid)
        if not qq:
            return
        bot = self._cached_bot
        if bot is None:
            logger.debug(f"[MCBridge] bot未缓存，跳过推送到QQ私聊")
            return
        try:
            await bot.api.send_private_msg(user_id=int(qq), message=reply)
            logger.info(f"[MCBridge] MC回复已推送到QQ私聊: qq={qq}")
        except Exception as e:
            logger.warning(f"[MCBridge] 推送回复到QQ私聊失败: {e}")

    async def _send_via_rcon(self, sv: ServerCfg, command: str) -> bool:
        if not sv.mc_rcon_password:
            logger.warning(f"[MCBridge][{sv.name}] RCON 模式但未设置密码，跳过")
            return False
        key = sv.name
        if key not in self._rcon_locks:
            self._rcon_locks[key] = asyncio.Lock()
        async with self._rcon_locks[key]:
            try:
                await asyncio.wait_for(
                    self._rcon_command(sv.host, sv.mc_rcon_port, sv.mc_rcon_password, command),
                    timeout=10,
                )
                return True
            except Exception as e:
                logger.warning(f"[MCBridge][{sv.name}] RCON执行失败: {e}")
                return False

    async def _rcon_command(self, host, port, password, command) -> str:
        reader, writer = await asyncio.open_connection(host, port)
        try:
            await self._rcon_send(writer, 1, 3, password)
            rid, _rt, _pl = await self._rcon_recv(reader)
            if rid == -1:
                raise Exception("RCON认证失败(密码错误/rcon未开启)")
            await self._rcon_send(writer, 2, 2, command)
            _rid2, _rt2, payload = await self._rcon_recv(reader)
            return payload.decode("utf-8", "ignore")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _rcon_send(self, writer, req_id, ptype, payload):
        p = payload.encode("utf-8")
        length = 4 + 4 + len(p) + 2
        packet = struct.pack("<iii", length, req_id, ptype) + p + b"\x00\x00"
        writer.write(packet)
        await writer.drain()

    async def _rcon_recv(self, reader):
        header = await reader.readexactly(4)
        length = struct.unpack("<i", header)[0]
        if not (10 <= length <= 8192):
            raise Exception(f"RCON非法长度: {length}")
        body = await reader.readexactly(length)
        req_id = struct.unpack("<i", body[0:4])[0]
        ptype = struct.unpack("<i", body[4:8])[0]
        payload = body[8:-2]
        return req_id, ptype, payload

    # ------------------------------------------------------------------ memory_companion 同步（BiliCompanion 风格异步批处理）

    def _get_memory_companion(self):
        try:
            return self.context.get_registered_star(MEMORY_COMPANION_NAME)
        except Exception:
            return None

    async def _buffer_and_sync_memory(
        self,
        sv: Optional[ServerCfg],
        user_key: str,
        user_id: str,
        username: str,
        text: str,
        event_type: str,
        *,
        player_uuid: str = "",
        session_id: str = "",
        platform: str = "minecraft",
        group_id: str = "",
        group_name: str = "",
        qq_number: str = "",
    ):
        """把 MC/QQ 两侧的聊天事件按批写入 memory_companion v1.7.3+ 的短期timeline，
        让它内部自动提取长期记忆。

        优先 bridge.record_visible_turn(兼容新老 signature)。不存在就回退老的两个方法。
        """
        interval = max(1, int(self.config.get("MEMORY_COMPANION_SYNC_INTERVAL", 5)))
        # 缓冲key：MC用 mc_server_name/QQ用 qq_session_id 分桶
        bucket_key = session_id or (sv.name if sv else "global")
        buf = self._mem_sync_acc.setdefault(bucket_key, [])
        packed_text = f"[{sv.name}] <{username}> {text}" if sv else text
        buf.append(
            {
                "t": int(time.time() * 1000),
                "user_id": user_id or (f"mcs[{sv.name}]" if sv else ""),
                "username": username,
                "text": packed_text,
                "raw_text": text,
                "event_type": event_type,
                "server": sv.name if sv else "",
                "user_key": user_key,
                "session_id": session_id,
                "platform": platform,
                "group_id": group_id,
                "group_name": group_name,
                "qq_number": qq_number,
            }
        )
        if len(buf) < interval:
            return
        batch = list(buf)
        buf.clear()
        companion = self._get_memory_companion()
        if not companion:
            logger.warning(f"[MCBridge] memory_companion 未注册，记忆同步跳过（batch={len(batch)}条）")
            return
        bridge = (
            getattr(companion, "bridge", None)
            or getattr(companion, "_bridge", None)
            or getattr(companion, "memory_companion", None)
        )
        logger.info(f"[MCBridge] 记忆同步: batch={len(batch)}条 user_key={user_key} companion={'有bridge' if bridge else '无bridge'}")

        # ---- 1) 首选 bridge.record_visible_turn（短期 timeline） ----
        if bridge and callable(getattr(bridge, "record_visible_turn", None)):
            import inspect
            sig_ok = False
            try:
                params = inspect.signature(bridge.record_visible_turn).parameters
                # 预期 (session_id, role, content, user_id, username, platform, group_id, time_ms)
                # 但可能只有 (role, content, session_id) 之类的；宽松传参。
                accepted = set(params.keys())
                sig_ok = True
            except Exception:
                accepted = set()
            for rec in batch:
                try:
                    role = "assistant" if rec["event_type"] in ("llm_reply", "bot_reply") else "user"
                    kwargs = {
                        "session_id": rec["session_id"] or (
                            f"mc:{rec['server']}" if rec["server"] else f"{rec['platform']}:unknown"
                        ),
                        "role": role,
                        "content": rec["raw_text"],
                        "user_id": rec["user_key"],
                        "username": rec["username"],
                        "platform": rec["platform"],
                        "group_id": rec["group_id"] or rec["server"] or "",
                        "time_ms": rec["t"],
                    }
                    # 去掉参数不支持的键
                    if accepted:
                        kwargs = {k: v for k, v in kwargs.items() if k in accepted}
                    await bridge.record_visible_turn(**kwargs)
                except TypeError as e:
                    # 签名不一致，降级
                    logger.debug(f"[MCBridge] record_visible_turn 参数不兼容: {e}")
                    break
                except Exception as e:
                    logger.debug(f"[MCBridge] record_visible_turn 失败(单条跳过): {e}")
            else:
                # 全部写入成功就不再fallback
                logger.info(f"[MCBridge] 记忆同步成功: record_visible_turn 写入{len(batch)}条")
                return

        # ---- 2) 回退 bridge.submit_emotion_event ----
        if bridge and callable(getattr(bridge, "submit_emotion_event", None)):
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            for rec in batch:
                try:
                    srv = rec["server"] or rec["group_id"] or rec["platform"]
                    event = {
                        "producer_plugin": PLUGIN_NAME,
                        "origin_kind": "interaction",
                        "platform": rec["platform"],
                        "bot_id": (sv.bot_name if sv else ""),
                        "scope": "group" if rec["group_id"] or rec["server"] else "private",
                        "session_id": rec["session_id"] or f"mc:{srv}",
                        "actor_ref": {
                            "kind": "user",
                            "id": rec["user_key"],
                            "role": "user",
                        },
                        "target_ref": {"kind": "bot", "id": (sv.bot_name if sv else "bot"), "role": "bot"},
                        "event_type": "chat" if rec["event_type"] == "chat" else "reply",
                        "intensity": 50.0,
                        "confidence": 0.8,
                        "occurred_at": now,
                        "status": "observed",
                        "dedupe_key": f"mcbridge:{rec['t']}:{srv}:{rec['username']}",
                        "payload": {
                            "text": rec["raw_text"],
                            "username": rec["username"],
                            "extra": {"server": rec["server"], "qq_group": rec["group_id"]},
                        },
                    }
                    await bridge.submit_emotion_event(event)
                except Exception as e:
                    logger.debug(f"[MCBridge] submit_emotion_event 失败(单条跳过): {e}")
            return

        # ---- 3) 回退 memory_api.record ----
        memory_api = getattr(companion, "memory_api", None) or getattr(
            companion, "_memory_api", None
        )
        if memory_api and callable(getattr(memory_api, "record", None)):
            for rec in batch:
                try:
                    await memory_api.record(
                        rec["raw_text"],
                        user_id=rec["user_key"],
                        username=rec["username"],
                        source=rec["platform"],
                        memory_type="chat",
                        level="today",
                        importance=6,
                        extra={"server": rec["server"], "qq_group": rec["group_id"]},
                    )
                except Exception as e:
                    logger.debug(f"[MCBridge] memory_api.record 失败: {e}")

    # ------------------------------------------------------------------ 印象同步：直接调用 impression 实例 _save_summary

    def _get_impression_plugin(self):
        try:
            return self.context.get_registered_star(IMPRESSION_NAME)
        except Exception:
            return None

    async def _try_update_impression(
        self,
        user_key: str,
        player: str,
        display: str,
        sv: ServerCfg,
        player_uuid: str,
        triggered: bool,
        *,
        session_id: str = "",
        qq_only: bool = False,
    ):
        """印象同步：按 user_key 身份写入 impression 插件。
        兼容 QQ 侧传入 sv=None 的调用（qq_only=True，从全部 session 里收集该 user 的近期发言）。
        """
        trigger_count = max(0, int(self.config.get("IMPRESSION_TRIGGER_COUNT", 8)))
        cnt = self._interaction_count.get(user_key, 0)
        need = triggered or (trigger_count > 0 and cnt % trigger_count == 0 and cnt > 0)
        if not need:
            return
        imp = self._get_impression_plugin()
        if imp is None:
            return
        try:
            ctx_count = int(self.config.get("LLM_CONTEXT_COUNT", 20))
            recent_msgs = []
            target_sessions = [session_id] if session_id and not qq_only else list(self._sessions.keys())
            async with self._lock:
                for sess_id in target_sessions:
                    for e in self._sessions.get(sess_id, [])[-ctx_count:]:
                        # user_key 命中：QQ 则看 user_key 直接比对；MC 则按 UUID / player 名匹配
                        if qq_only:
                            # QQ 侧暂时没有记录每条 user_key，靠 caller 传入 session_id 隔离即可
                            recent_msgs.append(e)
                        else:
                            ok = False
                            if player_uuid and sv and sv.online_mode and str(e.get("player_uuid", "")) == player_uuid:
                                ok = True
                            elif e.get("player") == player:
                                ok = True
                            if ok:
                                recent_msgs.append(e)
            if not recent_msgs:
                return
            conv = "\n".join(
                f"[{x.get('time','')}] <{x.get('name')}> {x.get('message')}"
                for x in recent_msgs
            )
            # 生成印象 JSON （QQ侧没有sv对象，构造一个最小化fake参数）
            fake_sv = sv
            if fake_sv is None:
                fake_sv = ServerCfg(name="QQ", bot_name=display or "QQ用户")
            new_data = await self._generate_impression_json(fake_sv, player, display, conv, imp)
            if new_data and new_data.get("summary"):
                group_id = (f"mc:{sv.name}" if sv else (session_id or "qq:unknown"))
                info = {"user_id": user_key, "user_name": display or player,
                        "group_id": group_id, "group_name": (sv.name if sv else (session_id or group_id))}
                save = getattr(imp, "_save_summary", None)
                if callable(save):
                    save(user_key, "mc_server_chat", info, new_data)
                    save_now = getattr(imp, "_save_now", None)
                    if callable(save_now):
                        try:
                            save_now()
                        except Exception:
                            pass
                    logger.info(
                        f"[MCBridge] 已同步印象: {user_key} "
                        f"({len(new_data['summary'])}字) session={group_id}"
                    )
        except Exception as e:
            logger.debug(f"[MCBridge] 同步印象失败: {e}")

    async def _generate_impression_json(
        self, sv: ServerCfg, player: str, display: str, conv: str, imp_inst
    ):
        # 提取印象插件默认人设(如果有)；失败就用 AstrBot 默认
        system_prompt = await self._get_system_prompt()
        bot_name = sv.bot_name
        prompt = (
            f"下面是 Minecraft 服务器[{sv.name}] 里玩家 <{display or player}> 最近的聊天记录：\n"
            f"{conv}\n\n"
            f"请你以 {bot_name} 的第一人称视角，输出一段「我对这个人的印象」JSON。"
            f"严格按此格式输出，不要写 markdown 代码块，纯JSON：\n"
            f"{{\"summary\": \"印象第一人称自述(200字以内，不要含自我身份介绍)\", "
            f"\"topics\": [\"兴趣点1\", \"兴趣点2\"], "
            f"\"key_facts\": [\"关键事实1\", \"关键事实2\"], "
            f"\"sentiment\": \"positive|neutral|negative\"}}\n"
            f"禁止出现「Agnès/Sapiens/AINES 等身份词」，你就是 {bot_name}。"
        )
        text = await self._llm_generate_text(prompt, system_prompt)
        if not text:
            return None
        try:
            # 取 JSON 子串
            match = re.search(r"\{[\s\S]*\}", text)
            if not match:
                return None
            obj = json.loads(match.group(0))
            if not isinstance(obj, dict):
                return None
            obj.setdefault("summary", "")
            obj["topics"] = [str(x) for x in (obj.get("topics") or []) if x]
            obj["key_facts"] = [str(x) for x in (obj.get("key_facts") or []) if x]
            obj["sentiment"] = str(obj.get("sentiment") or "neutral") or "neutral"
            return obj
        except Exception:
            return None

    # ------------------------------------------------------------------ 命令通道：/mc 指令 + LLM 工具（统一调用 dispatch_mc_command）

    # 命令执行安全分级：risk=玩家管理类；operation=游戏操作类（更危险）；query=查询只读；op=运维插件
    CMD_CATEGORY_QUERY = "query"
    CMD_CATEGORY_OP = "op"
    CMD_CATEGORY_RISK = "risk"       # 踢/禁言/白名单
    CMD_CATEGORY_OPERATION = "gameop"  # tp/gamemode/give 等

    def _is_super_admin(self, user_id: str) -> bool:
        ids_raw = str(self.config.get("SUPER_ADMIN_IDS", "") or "")
        ids = {x.strip() for x in ids_raw.replace("，", ",").split(",") if x.strip()}
        if not ids:
            return False
        return str(user_id or "").strip() in ids

    def _is_admin(self, event_or_id) -> bool:
        if isinstance(event_or_id, AstrMessageEvent):
            uid = event_or_id.get_sender_id() if hasattr(event_or_id, "get_sender_id") else ""
            try:
                if event_or_id.get_permission() >= PermissionType.ADMIN:
                    return True
            except Exception:
                pass
            try:
                if hasattr(event_or_id, "is_admin") and bool(event_or_id.is_admin):
                    return True
            except Exception:
                pass
            return self._is_super_admin(str(uid))
        # 字符串 id
        return self._is_super_admin(str(event_or_id))

    def _can_execute(self, uid: str, category: str, event=None) -> tuple[bool, str]:
        """返回 (ok, reason)。
        - SUPER_ADMIN：所有类别全开；
        - PermissionType.ADMIN：当 ADMIN_CAN_QUERY_ONLY=true 时 仅 query + op（运维）；
        - 否则都拒绝。
        """
        if self._is_super_admin(uid):
            return True, ""
        if event is not None:
            try:
                perm = event.get_permission()
                is_adm = (perm >= PermissionType.ADMIN) if hasattr(PermissionType, "__ge__") else (
                    str(perm) == str(PermissionType.ADMIN)
                )
            except Exception:
                is_adm = False
            if not is_adm:
                try:
                    is_adm = bool(getattr(event, "is_admin", False))
                except Exception:
                    is_adm = False
            if is_adm:
                if bool(self.config.get("ADMIN_CAN_QUERY_ONLY", True)):
                    if category in (self.CMD_CATEGORY_QUERY, self.CMD_CATEGORY_OP):
                        return True, ""
                    return False, "仅SUPER_ADMIN可执行玩家管理/游戏操作类命令"
                return True, ""
        return False, "无权限（需要SUPER_ADMIN或PermissionType.ADMIN）"

    # ---- 执行 Minecraft 命令（桥接 HTTP 或 RCON） ----
    async def dispatch_mc_command(
        self,
        *,
        server_name: str,
        command: str,
        category: str,
        user_id: str,
        event=None,
        cross_punish: bool = False,
    ) -> tuple[bool, str]:
        """执行 MC 命令。category 用于权限校验。"""
        ok, reason = self._can_execute(user_id, category, event=event)
        if not ok:
            return False, f"权限不足: {reason}"
        sv = self._servers.get(server_name)
        if sv is None:
            return False, f"服务器 {server_name} 不存在，可用服: {list(self._servers.keys())}"
        # 处罚联动
        need_cross = (
            cross_punish
            and bool(self.config.get("ENABLE_CROSS_SERVER_PUNISH", True))
            and category == self.CMD_CATEGORY_RISK
            and sv.online_mode
        )
        ok1, msg1 = await self._do_command(sv, command)
        main_msg = f"[{sv.name}] {msg1}"
        if need_cross and ok1:
            extras = []
            for other in self._servers.values():
                if other.name == sv.name:
                    continue
                if not other.online_mode:
                    continue
                o, m = await self._do_command(other, command)
                extras.append(f"[{other.name}] {'OK' if o else 'FAIL'}: {m}")
            if extras:
                main_msg += "（跨服联动: " + "; ".join(extras) + "）"
        return ok1, main_msg

    async def _do_command(self, sv: ServerCfg, command: str) -> tuple[bool, str]:
        """把 Minecraft 指令发到指定服务器，返回 (success, 描述)。"""
        command = command.strip()
        if not command:
            return False, "空命令"
        if sv.send_channel == "rcon":
            try:
                ret = await self._rcon_command(
                    sv.host, sv.mc_rcon_port, sv.mc_rcon_password, command
                )
                return True, f"RCON执行成功: {(ret or '').strip()[:300]}"
            except Exception as e:
                return False, f"RCON失败: {e}"
        else:
            try:
                ok = await self._send_via(sv, command)
                if ok:
                    return True, "bridge执行成功(无返回)"
                return False, "bridge执行失败(见日志)"
            except Exception as e:
                return False, f"bridge异常: {e}"

    # ---- 命令语义：把高级动作 -> Minecraft 命令串 ----
    def action_kick(self, server: str, player: str, reason: str = "") -> tuple[str, str]:
        cmd = f"kick {player} {reason}".strip()
        return cmd, self.CMD_CATEGORY_RISK

    def action_ban(self, server: str, player: str, reason: str = "") -> tuple[str, str]:
        cmd = f"ban {player} {reason}".strip()
        return cmd, self.CMD_CATEGORY_RISK

    def action_pardon(self, server: str, player: str) -> tuple[str, str]:
        return f"pardon {player}", self.CMD_CATEGORY_RISK

    def action_whitelist_add(self, server: str, player: str) -> tuple[str, str]:
        return f"whitelist add {player}", self.CMD_CATEGORY_RISK

    def action_whitelist_remove(self, server: str, player: str) -> tuple[str, str]:
        return f"whitelist remove {player}", self.CMD_CATEGORY_RISK

    def action_gamemode(self, server: str, target: str, mode: str) -> tuple[str, str]:
        return f"gamemode {mode} {target}", self.CMD_CATEGORY_OPERATION

    def action_tp(self, server: str, who: str, to: str) -> tuple[str, str]:
        return f"tp {who} {to}", self.CMD_CATEGORY_OPERATION

    def action_give(self, server: str, who: str, item: str, count: int = 1) -> tuple[str, str]:
        return f"give {who} {item} {max(1, int(count))}", self.CMD_CATEGORY_OPERATION

    def action_raw(self, server: str, command: str) -> tuple[str, str]:
        # 任何未归类的MC命令都当作最高风险 operation
        return command.strip(), self.CMD_CATEGORY_OPERATION

    def action_list(self, server: str) -> tuple[str, str]:
        return "list", self.CMD_CATEGORY_QUERY

    def action_tps(self, server: str) -> tuple[str, str]:
        # paper/spigot 有 /tps 命令；若不存在会返回未知命令，不会崩
        return "tps", self.CMD_CATEGORY_QUERY

    # ---- AstrBot 命令入口：/mc help / /mc list / /mc kick ... ----
    @filter.command("mc", alias={"mc_bridge"})
    async def cmd_mc_root(self, event: AstrMessageEvent, *args, **kwargs):
        sender_id = str(event.get_sender_id() or "")
        text = (getattr(event, "message_str", "") or "").strip()
        parts = text.split()
        # parts[0] 是 /mc，parts[1..] 是子命令与参数
        sub = parts[1].lower() if len(parts) >= 2 else "help"
        params = parts[2:]

        def reply(s: str):
            return event.plain_result(s)

        if sub in ("help", "帮助"):
            yield reply(
                "【MC桥接 /mc 指令列表】\n"
                "/mc list [服务名]               — 查询在线玩家列表(query)\n"
                "/mc tps [服务名]                — 查询TPS(query)\n"
                "/mc kick <服务名> <玩家> [原因]  — 踢玩家(SUPER_ADMIN)\n"
                "/mc ban  <服务名> <玩家> [原因]  — 封禁(SUPER_ADMIN)\n"
                "/mc pardon <服务名> <玩家>       — 解封(SUPER_ADMIN)\n"
                "/mc whitelist add|remove <服务名> <玩家>\n"
                "/mc gamemode <服务名> <目标> <0..3/survival|creative|...>\n"
                "/mc tp <服务名> <谁> <传送到谁>\n"
                "/mc give <服务名> <玩家> <物品ID> [数量]\n"
                "/mc raw <服务名> <命令>         — 执行任意MC命令(最危险)\n"
                "/mc servers                    — 查看已接入服务器列表(op)\n"
                "/mc resync <服务名|all>        — 强制同步玩家记忆/印象(op)\n"
                "/mc version                    — 插件版本"
            )
            return

        if sub in ("version",):
            yield reply(f"[MCBridge] v{PLUGIN_VERSION}  author: uGmTEAM  服务器={list(self._servers.keys())}")
            return

        if sub in ("servers",):
            ok, reason = self._can_execute(sender_id, self.CMD_CATEGORY_OP, event)
            if not ok:
                yield reply("权限不足: " + reason)
                return
            lines = ["【服务器列表】"]
            for s in self._servers.values():
                lines.append(
                    f"- {s.name}  host={s.host}:{s.listen_port}"
                    f"  channel={s.send_channel}(bridge:{s.mc_bridge_port}/rcon:{s.mc_rcon_port})"
                    f"  online_mode={s.online_mode}  bot={s.bot_name}"
                )
            yield reply("\n".join(lines))
            return

        if sub in ("resync",):
            ok, reason = self._can_execute(sender_id, self.CMD_CATEGORY_OP, event)
            if not ok:
                yield reply("权限不足: " + reason)
                return
            target = params[0] if params else "all"
            done = 0
            for s in self._servers.values():
                if target not in ("all", s.name):
                    continue
                for entry in list(self._sessions.get(self._mc_session_id(s), []))[-50:]:
                    self._spawn(
                        self._buffer_and_sync_memory(
                            s,
                            entry.get("player") or "",
                            entry.get("player") or "",
                            entry.get("name") or "",
                            entry.get("message") or "",
                            "chat",
                            player_uuid=entry.get("player_uuid") or "",
                            session_id=self._mc_session_id(s),
                        )
                    )
                    done += 1
            yield reply(f"已提交 {done} 条消息到记忆同步队列（印象会随交互次数自动更新）。")
            return

        # 以下子命令都至少需要 服务名 参数
        if not params:
            yield reply("参数缺失: 需要 <服务名>；/mc help 查看用法")
            return
        server = params[0]
        if server not in self._servers:
            yield reply(
                f"服务器 '{server}' 不存在，已接入: {list(self._servers.keys())}"
            )
            return

        if sub in ("list",):
            ok, reason = self._can_execute(sender_id, self.CMD_CATEGORY_QUERY, event)
            if not ok:
                yield reply("权限不足: " + reason)
                return
            cmd, cat = self.action_list(server)
            o, m = await self.dispatch_mc_command(
                server_name=server, command=cmd, category=cat,
                user_id=sender_id, event=event,
            )
            yield reply(("✅ " if o else "❌ ") + m)
            return

        if sub in ("tps",):
            ok, reason = self._can_execute(sender_id, self.CMD_CATEGORY_QUERY, event)
            if not ok:
                yield reply("权限不足: " + reason)
                return
            cmd, cat = self.action_tps(server)
            o, m = await self.dispatch_mc_command(
                server_name=server, command=cmd, category=cat,
                user_id=sender_id, event=event,
            )
            yield reply(("✅ " if o else "❌ ") + m)
            return

        if sub == "kick":
            if len(params) < 2:
                yield reply("用法: /mc kick <服务名> <玩家> [原因]")
                return
            player = params[1]
            reason = " ".join(params[2:]) if len(params) > 2 else ""
            await self._exec_with_maybe_confirm(
                event, sender_id, server, *self.action_kick(server, player, reason),
                cross_punish=True,
            )
            return

        if sub == "ban":
            if len(params) < 2:
                yield reply("用法: /mc ban <服务名> <玩家> [原因]")
                return
            player = params[1]
            reason = " ".join(params[2:]) if len(params) > 2 else ""
            await self._exec_with_maybe_confirm(
                event, sender_id, server, *self.action_ban(server, player, reason),
                cross_punish=True,
            )
            return

        if sub == "pardon":
            if len(params) < 2:
                yield reply("用法: /mc pardon <服务名> <玩家>")
                return
            await self._exec_with_maybe_confirm(
                event, sender_id, server, *self.action_pardon(server, params[1]),
                cross_punish=True,
            )
            return

        if sub == "whitelist":
            op = params[1] if len(params) >= 2 else ""
            if op not in ("add", "remove"):
                yield reply("用法: /mc whitelist add|remove <服务名> <玩家>")
                return
            if len(params) < 3:
                yield reply("缺少玩家名")
                return
            if op == "add":
                await self._exec_with_maybe_confirm(
                    event, sender_id, server,
                    *self.action_whitelist_add(server, params[2]),
                    cross_punish=True,
                )
            else:
                await self._exec_with_maybe_confirm(
                    event, sender_id, server,
                    *self.action_whitelist_remove(server, params[2]),
                    cross_punish=True,
                )
            return

        if sub == "gamemode":
            if len(params) < 3:
                yield reply("用法: /mc gamemode <服务名> <玩家> <模式>")
                return
            await self._exec_with_maybe_confirm(
                event, sender_id, server,
                *self.action_gamemode(server, params[1], params[2]),
            )
            return

        if sub == "tp":
            if len(params) < 3:
                yield reply("用法: /mc tp <服务名> <谁> <传送到谁>")
                return
            await self._exec_with_maybe_confirm(
                event, sender_id, server,
                *self.action_tp(server, params[1], params[2]),
            )
            return

        if sub == "give":
            if len(params) < 3:
                yield reply("用法: /mc give <服务名> <玩家> <物品ID> [数量]")
                return
            count = 1
            if len(params) >= 4:
                try:
                    count = int(params[3])
                except Exception:
                    count = 1
            await self._exec_with_maybe_confirm(
                event, sender_id, server,
                *self.action_give(server, params[1], params[2], count),
            )
            return

        if sub == "raw":
            if len(params) < 2:
                yield reply("用法: /mc raw <服务名> <命令> (最危险，需SUPER_ADMIN)")
                return
            cmd = " ".join(params[1:])
            await self._exec_with_maybe_confirm(
                event, sender_id, server,
                *self.action_raw(server, cmd),
            )
            return

        yield reply(f"未知子命令: {sub}；请 /mc help 查看用法")

    async def _exec_with_maybe_confirm(
        self,
        event: AstrMessageEvent,
        user_id: str,
        server_name: str,
        command: str,
        category: str,
        cross_punish: bool = False,
    ):
        def quick_reply(s: str):
            return event.plain_result(s)

        # 预校验权限
        ok, reason = self._can_execute(user_id, category, event)
        if not ok:
            yield quick_reply("❌ 权限不足: " + reason)
            return

        need_confirm = bool(self.config.get("CMD_CONFIRMATION_REQUIRED", False)) and (
            category in (self.CMD_CATEGORY_RISK, self.CMD_CATEGORY_OPERATION)
        )
        if not need_confirm:
            o, m = await self.dispatch_mc_command(
                server_name=server_name,
                command=command,
                category=category,
                user_id=user_id,
                event=event,
                cross_punish=cross_punish,
            )
            yield quick_reply(("✅ " if o else "❌ ") + m)
            return

        # 二次确认流程：生成 token 放 pending，等待管理员下一条消息里发确认词
        token = f"mc_confirm_{int(time.time()*1000)}_{user_id[-4:]}"
        timeout = max(10, int(self.config.get("CMD_EXECUTE_TIMEOUT", 120)))
        self._pending_confirmations[token] = {
            "command": command,
            "category": category,
            "server": server_name,
            "user_id": user_id,
            "cross_punish": cross_punish,
            "timeout_at": time.time() + timeout,
        }
        yield quick_reply(
            f"⚠️ 请在 {timeout}s 内发送「确认/是/y/好」之一来执行，超时自动取消。\n"
            f"服务={server_name} 分类={category}\n命令: {command}\n"
            f"（确认码={token}）"
        )
        # 注册一次性监听器：下一条来自同一管理员的消息包含确认词就执行
        # 用 AstrBot filter.on_message 太重量级，这里用简单的全局轮询：
        # 实际做法：挂一个 filter 监听器（下一条消息钩子）；但无法动态注册。
        # 折中：启动后台任务 wait_for_confirmation_within_timeout 等待管理员在 IM 里回复确认。
        # 这里通过 AstrBot 无此API，因此改为「管理员下一次发送 /mc_confirm <确认码|确认词」触发。
        # 同时超时自动清理。
        self._spawn(self._auto_cleanup_pending(token, timeout))

    async def _auto_cleanup_pending(self, token: str, timeout: int):
        await asyncio.sleep(timeout + 5)
        self._pending_confirmations.pop(token, None)

    @filter.command("mc_confirm")
    async def cmd_mc_confirm(self, event: AstrMessageEvent, *args, **kwargs):
        """管理员确认执行二次确认的命令。
        用法：/mc_confirm 确认|是|y|好    （或 /mc_confirm <确认码>）
        """
        uid = str(event.get_sender_id() or "")
        text = (getattr(event, "message_str", "") or "").strip()
        parts = text.split(maxsplit=1)
        arg = (parts[1] if len(parts) > 1 else "").strip().lower()
        confirm_words = {"确认", "是", "y", "好", "ok", "yes"}
        # 匹配：要么 arg 是确认词匹配最近同 uid 的 pending；要么 arg 是 token 直接匹配
        matched = None
        if arg.startswith("mc_confirm_") and arg in self._pending_confirmations:
            matched = arg
        else:
            for tok, info in list(self._pending_confirmations.items()):
                if info["user_id"] != uid:
                    continue
                if time.time() > info["timeout_at"]:
                    self._pending_confirmations.pop(tok, None)
                    continue
                if arg in confirm_words or arg == "":
                    matched = tok
                    break
        if not matched:
            yield event.plain_result("未找到待确认的MC命令（已超时/不存在）。")
            return
        info = self._pending_confirmations.pop(matched)
        o, m = await self.dispatch_mc_command(
            server_name=info["server"],
            command=info["command"],
            category=info["category"],
            user_id=uid,
            event=event,
            cross_punish=info.get("cross_punish", False),
        )
        yield event.plain_result(("✅ " if o else "❌ ") + m)

    # ------------------------------------------------------------------ LLM 工具注册（自然语言调用 MC 指令）

    def _register_llm_tools(self):
        if tool is None:
            logger.warning("[MCBridge] 当前 AstrBot 版本无 toolbox.tool 装饰器，LLM工具注册跳过")
            return

        host_self = self

        # --------- 工具1：MC命令查询类（安全只读） ---------
        @tool(
            name="mc_list_players",
            description=(
                "查询 Minecraft 服务器在线玩家列表或TPS(服务器性能)。"
                "仅管理员可调用。需要传 server_name(必选) 以及 mode('list'|'tps', 默认list)。"
                "不要用于执行任何会改变游戏状态的操作。"
                "已接入服务器列表请先调用 mc_list_servers 工具。"
            ),
        )
        async def t_mc_list(
            tctx: ToolContext, server_name: str, mode: str = "list"
        ) -> ToolResult:
            uid = str(getattr(tctx, "user_id", "") or "")
            try:
                ev = getattr(tctx, "event", None)
            except Exception:
                ev = None
            ok, reason = self._can_execute(uid, self.CMD_CATEGORY_QUERY, ev)
            if not ok:
                return ToolResult(error="权限不足: " + reason)
            sv = host_self._servers.get(server_name)
            if sv is None:
                return ToolResult(
                    error=f"服务器 {server_name} 不存在, 可用={list(host_self._servers.keys())}"
                )
            cmd, cat = (
                host_self.action_list(server_name)
                if mode != "tps"
                else host_self.action_tps(server_name)
            )
            _o, m = await host_self.dispatch_mc_command(
                server_name=server_name, command=cmd, category=cat,
                user_id=uid, event=getattr(tctx, "event", None),
            )
            return ToolResult(content=m)

        # --------- 工具2：列出已接入服务器（运维类，ADMIN 可） ---------
        @tool(
            name="mc_list_servers",
            description="列出 AstrBot 当前已接入的所有MC服务器名称、在线模式、连接方式等基本信息。",
        )
        async def t_mc_servers(tctx: ToolContext) -> ToolResult:
            uid = str(getattr(tctx, "user_id", "") or "")
            ok, reason = self._can_execute(uid, self.CMD_CATEGORY_OP, getattr(tctx, "event", None))
            if not ok:
                return ToolResult(error="权限不足: " + reason)
            lines = []
            for s in host_self._servers.values():
                lines.append(
                    f"name={s.name},host={s.host},listen={s.listen_port},"
                    f"send={s.send_channel},online_mode={s.online_mode},bot={s.bot_name}"
                )
            return ToolResult(content="\n".join(lines) or "无任何服务器接入")

        # --------- 工具3：玩家管理（踢/封禁/解封/白名单增删）——SUPER_ADMIN才可用 ---------
        @tool(
            name="mc_player_manage",
            description=(
                "对MC玩家执行管理动作（踢、封禁、解封、白名单增删）。"
                "参数：server_name(必填), action(kick|ban|pardon|whitelist_add|whitelist_remove), "
                "player(必填玩家名), reason(可选, ban/kick时的原因)。"
                "⚠️ 仅SUPER_ADMIN可调用；若 ENABLE_CROSS_SERVER_PUNISH=true 且服务器正版验证开启，会自动跨正版服同步处罚。"
            ),
        )
        async def t_mc_manage(
            tctx: ToolContext,
            server_name: str,
            action: str,
            player: str,
            reason: str = "",
        ) -> ToolResult:
            uid = str(getattr(tctx, "user_id", "") or "")
            ok, reason_msg = self._can_execute(uid, self.CMD_CATEGORY_RISK, getattr(tctx, "event", None))
            if not ok:
                return ToolResult(error="权限不足: " + reason_msg)
            sv = host_self._servers.get(server_name)
            if sv is None:
                return ToolResult(error=f"服务器不存在, 可用={list(host_self._servers.keys())}")
            action = (action or "").strip().lower()
            if action == "kick":
                cmd, cat = host_self.action_kick(server_name, player, reason or "")
            elif action == "ban":
                cmd, cat = host_self.action_ban(server_name, player, reason or "")
            elif action == "pardon":
                cmd, cat = host_self.action_pardon(server_name, player)
            elif action in ("whitelist_add", "wl_add", "add"):
                cmd, cat = host_self.action_whitelist_add(server_name, player)
            elif action in ("whitelist_remove", "wl_remove", "remove"):
                cmd, cat = host_self.action_whitelist_remove(server_name, player)
            else:
                return ToolResult(error=f"未知 action: {action}")
            o, m = await host_self.dispatch_mc_command(
                server_name=server_name, command=cmd, category=cat,
                user_id=uid, event=getattr(tctx, "event", None), cross_punish=True,
            )
            return ToolResult(content=("成功: " if o else "失败: ") + m)

        # --------- 工具4：游戏操作（gamemode/tp/give/raw 任意命令）——SUPER_ADMIN ---------
        @tool(
            name="mc_game_operation",
            description=(
                "执行MC游戏操作：切换模式/传送/给物品/任意命令。"
                "参数：server_name(必填), type(gamemode|tp|give|raw), params(对应子参数字典)："
                "gamemode={target, mode(0|1|2|3|survival|creative|...)}；"
                "tp={who, to}；give={who, item, count=1}；raw={command}。"
                "⚠️ 仅SUPER_ADMIN可调用，风险极高，会直接改变游戏状态。"
            ),
        )
        async def t_mc_gameop(
            tctx: ToolContext, server_name: str, type: str, params: dict
        ) -> ToolResult:
            uid = str(getattr(tctx, "user_id", "") or "")
            ok, reason = self._can_execute(uid, self.CMD_CATEGORY_OPERATION, getattr(tctx, "event", None))
            if not ok:
                return ToolResult(error="权限不足: " + reason)
            sv = host_self._servers.get(server_name)
            if sv is None:
                return ToolResult(error=f"服务器不存在: {list(host_self._servers.keys())}")
            try:
                params = params or {}
                t = (type or "").strip().lower()
                if t == "gamemode":
                    cmd, cat = host_self.action_gamemode(
                        server_name, str(params["target"]), str(params["mode"])
                    )
                elif t == "tp":
                    cmd, cat = host_self.action_tp(
                        server_name, str(params["who"]), str(params["to"])
                    )
                elif t == "give":
                    cmd, cat = host_self.action_give(
                        server_name,
                        str(params["who"]),
                        str(params["item"]),
                        int(params.get("count", 1) or 1),
                    )
                elif t == "raw":
                    cmd, cat = host_self.action_raw(server_name, str(params["command"]))
                else:
                    return ToolResult(error=f"未知 type={type}")
                o, m = await host_self.dispatch_mc_command(
                    server_name=server_name, command=cmd, category=cat,
                    user_id=uid, event=getattr(tctx, "event", None),
                )
                return ToolResult(content=("成功: " if o else "失败: ") + m)
            except KeyError as e:
                return ToolResult(error=f"缺少参数: {e}")
            except Exception as e:
                return ToolResult(error=f"执行异常: {e}")

        # --------- 工具5：运维（resync/version/status）——ADMIN或SUPER ---------
        @tool(
            name="mc_plugin_ops",
            description=(
                "MC桥接插件运维操作：查询版本、强制触发记忆/印象重新同步。"
                "参数 operation: 'version' | 'resync'。resync 需要 server_name 参数(或 'all')。"
            ),
        )
        async def t_mc_ops(
            tctx: ToolContext, operation: str, server_name: str = "all"
        ) -> ToolResult:
            uid = str(getattr(tctx, "user_id", "") or "")
            ok, reason = self._can_execute(uid, self.CMD_CATEGORY_OP, getattr(tctx, "event", None))
            if not ok:
                return ToolResult(error="权限不足: " + reason)
            operation = (operation or "").strip().lower()
            if operation == "version":
                return ToolResult(
                    content=f"MCBridge v{PLUGIN_VERSION}  author uGmTEAM  接入服务器: {list(host_self._servers.keys())}"
                )
            if operation == "resync":
                done = 0
                for s in host_self._servers.values():
                    if server_name not in ("all", s.name):
                        continue
                    for entry in list(host_self._sessions.get(host_self._mc_session_id(s), []))[-100:]:
                        host_self._spawn(
                            host_self._buffer_and_sync_memory(
                                s,
                                entry.get("player") or "",
                                entry.get("player") or "",
                                entry.get("name") or "",
                                entry.get("message") or "",
                                "chat",
                                player_uuid=entry.get("player_uuid") or "",
                                session_id=host_self._mc_session_id(s),
                            )
                        )
                        done += 1
                return ToolResult(content=f"已提交 {done} 条消息到记忆同步队列（印象随交互次数自动更新）")
            return ToolResult(error=f"未知 operation: {operation}")

    # ======================================================================
    #   v3.0 新增: QQ 全消息旁路监听器 + /mc_forward 动态转发 + /unbind
    # ======================================================================

    # ------ 工具方法：从 AstrMessageEvent 里拿纯文本（去掉 CQ 码/引用后内容） ------
    @staticmethod
    def _strip_cq(text: str) -> str:
        """最简单的 CQ 码剥离，得到可读纯文本。"""
        if not text:
            return ""
        # 先去掉 [CQ:reply,id=...]（引用回复块）
        t = re.sub(r"\[CQ:reply[^\]]*\]", "", text)
        # 去掉所有 [CQ:xxx,...]
        t = re.sub(r"\[CQ:[^\]]*\]", "", t)
        return t.strip()

    def _event_plain_text(self, event: AstrMessageEvent) -> str:
        base = getattr(event, "message_str", None)
        if isinstance(base, str) and base:
            return self._strip_cq(base)
        # 备用：遍历 message 段（若存在）
        msg_obj = getattr(event, "message", None)
        if msg_obj is None:
            return ""
        out = []
        try:
            for seg in msg_obj:
                t = getattr(seg, "type", None)
                if t in ("text", "plain"):
                    d = getattr(seg, "data", None) or getattr(seg, "__dict__", {})
                    out.append(str(d.get("text") if isinstance(d, dict) else d or ""))
                elif t == "at":
                    d = getattr(seg, "data", None) or getattr(seg, "__dict__", {})
                    qq = d.get("qq") if isinstance(d, dict) else None
                    if qq:
                        out.append(f"@{qq}")
        except Exception:
            pass
        return " ".join(x for x in out if x).strip()

    def _event_sender_name(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_name() or "")
        except Exception:
            return str(getattr(event, "sender_name", None) or "") or str(event.get_sender_id() or "")

    def _event_group_name(self, event: AstrMessageEvent) -> str:
        try:
            return str(getattr(event, "get_group_name", lambda: "")() or "")
        except Exception:
            return str(getattr(event, "group_name", None) or "")

    def _event_group_id(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_group_id() or "")
        except Exception:
            return ""

    # ------ /mc_forward on|off <服名>      群里ADMIN动态订阅 / 取消订阅 ------
    @filter.command("mc_forward")
    async def cmd_mc_forward(self, event: AstrMessageEvent, *args, **kwargs):
        uid = str(event.get_sender_id() or "")
        try:
            is_admin = bool(
                filter.check_permission(event.get_permission(), PermissionType.ADMIN)
            )
        except Exception:
            is_admin = False
        try:
            super_ids = self.config.get("SUPER_ADMIN_IDS", [])
            if isinstance(super_ids, str):
                super_ids = [x.strip() for x in super_ids.split(",") if x.strip()]
            if str(uid) in [str(x) for x in (super_ids or [])]:
                is_admin = True
        except Exception:
            pass
        if not is_admin:
            yield event.plain_result("权限不足：仅 ADMIN 及以上可在群里动态订阅/取消MC聊天转发。")
            return
        # 只允许在群里使用（便于拿到 group_id）
        group_id = self._event_group_id(event)
        if not group_id:
            yield event.plain_result("请在 QQ 群里使用本命令。")
            return
        text = self._event_plain_text(event)
        parts = text.split()
        # parts[0] = /mc_forward, [1]=on|off, [2]=server_name
        if len(parts) < 2:
            yield event.plain_result(
                "用法：\n"
                f"/mc_forward on  <服名或 *>    —— 本群订阅该服务器聊天(* = 所有服)\n"
                f"/mc_forward off <服名或 *>    —— 本群取消订阅该服务器\n"
                f"当前已接入服务器: {list(self._servers.keys())}"
            )
            return
        op = parts[1].lower()
        srv = (parts[2].strip() if len(parts) >= 3 else "*").strip() or "*"
        if srv != "*" and srv not in self._servers:
            yield event.plain_result(
                f"服务器 '{srv}' 不存在。已接入服务器: {list(self._servers.keys())}"
            )
            return
        srvs_set = self._forwards.setdefault(str(group_id), set())
        if op in ("on", "enable", "add", "开", "订阅"):
            if srv in srvs_set:
                yield event.plain_result(f"本群已订阅 {srv}，无需重复添加。")
                return
            srvs_set.add(srv)
            self._save_forward_groups()
            yield event.plain_result(f"✅ 本群已订阅 MC服务器={srv} 的聊天。")
            return
        if op in ("off", "disable", "remove", "关", "取消"):
            if srv not in srvs_set:
                yield event.plain_result(f"本群没有订阅 {srv}。")
                return
            srvs_set.discard(srv)
            if not srvs_set:
                self._forwards.pop(group_id, None)
            self._save_forward_groups()
            yield event.plain_result(f"✅ 本群已取消订阅 MC服务器={srv}。")
            return
        yield event.plain_result("参数错误: 仅支持 on / off 两种操作。")

    # ------ 绑定确认的回复词匹配 ------
    CONFIRM_YES = {"同意", "确认", "是", "y", "好", "ok", "yes", "绑定"}
    CONFIRM_NO = {"拒绝", "否", "n", "no", "cancel", "取消"}

    def _resolve_bind_confirmation(self, event: AstrMessageEvent, text: str):
        """检查这条消息是否是某条待确认绑定的 同意/拒绝。命中返回 (token, is_accept)，否则返回 None."""
        qq = str(event.get_sender_id() or "")
        if not qq:
            return None
        lower = text.strip().lower()
        if not (lower in self.CONFIRM_YES or lower in self.CONFIRM_NO or
                text.strip() in self.CONFIRM_YES or text.strip() in self.CONFIRM_NO):
            return None
        is_accept = (lower in self.CONFIRM_YES) or (text.strip() in self.CONFIRM_YES)
        # 找该 QQ 最老一条未超时 pending
        now = time.time()
        best = None
        for tok, info in list(self._pending_binds.items()):
            if info["qq"] != qq:
                continue
            if now > info.get("timeout_at", 0):
                self._pending_binds.pop(tok, None)
                continue
            if best is None or info["timeout_at"] < self._pending_binds[best]["timeout_at"]:
                best = tok
        if best is None:
            return None
        return best, is_accept

    # ------ QQ侧解绑命令：/unbind （只有绑定玩家本人能发） ------
    @filter.command("unbind")
    async def cmd_qq_unbind(self, event: AstrMessageEvent, *args, **kwargs):
        qq = str(event.get_sender_id() or "")
        if not qq:
            return
        mid = self._qq_to_mc.get(qq)
        if not mid:
            yield event.plain_result("你还没有绑定MC身份。")
            return
        self._bindings.pop(mid, None)
        self._save_bindings()
        yield event.plain_result(f"✅ 已解除 MC身份 {mid} ↔ QQ {qq} 的绑定。")

    # ------ /mc_bindings   (ADMIN) 查看绑定表 ------
    @filter.command("mc_bindings")
    async def cmd_mc_bindings(self, event: AstrMessageEvent, *args, **kwargs):
        uid = str(event.get_sender_id() or "")
        try:
            is_admin = bool(filter.check_permission(event.get_permission(), PermissionType.ADMIN))
        except Exception:
            is_admin = False
        try:
            super_ids = self.config.get("SUPER_ADMIN_IDS", [])
            if isinstance(super_ids, str):
                super_ids = [x.strip() for x in super_ids.split(",") if x.strip()]
            if str(uid) in [str(x) for x in (super_ids or [])]:
                is_admin = True
        except Exception:
            pass
        if not is_admin:
            yield event.plain_result("权限不足：仅 ADMIN 及以上可查看绑定表。")
            return
        if not self._bindings:
            yield event.plain_result("目前没有任何绑定。")
            return
        lines = ["【MC ↔ QQ 绑定表】"]
        for i, (mid, qq) in enumerate(list(self._bindings.items())[:50], 1):
            lines.append(f"{i}. {mid}  ↔  QQ {qq}")
        yield event.plain_result("\n".join(lines))

    # ---- 最核心：QQ 全消息旁路监听（仅旁听、不拦截） @filter.event_message_type(ALL) ----
    @filter.event_message_type(filter.EventMessageType.ALL, priority=30)
    async def on_qq_all_messages(self, event: AstrMessageEvent, *args, **kwargs):
        # 1) 缓存 bot 引用（旁路缓存，不影响正常处理）
        try:
            await self._cache_bot_from_event(event)
        except Exception:
            pass

        plain = self._event_plain_text(event)
        sender_id = str(event.get_sender_id() or "")
        sender_name = self._event_sender_name(event)
        group_id = self._event_group_id(event)
        group_name = self._event_group_name(event)
        is_private = False
        try:
            if hasattr(event, "is_private_chat"):
                is_private = bool(event.is_private_chat())
        except Exception:
            is_private = not bool(group_id)

        # 2) 二次确认绑定（优先处理：若命中就直接消耗掉，不记录为记忆；但仍会通知MC结果）
        try:
            bind_res = self._resolve_bind_confirmation(event, plain or "")
        except Exception:
            bind_res = None
        if bind_res is not None:
            tok, is_accept = bind_res
            info = self._pending_binds.pop(tok, None)
            if info is not None:
                sv = self._servers.get(info.get("server", ""))
                player = info.get("player", "")
                if is_accept:
                    mc_id = info.get("mc_id", "")
                    qq = info.get("qq", "")
                    # 再校验一次防重复
                    if mc_id in self._bindings:
                        reply_txt = "绑定失败：该MC身份已被其他人先绑定。"
                    elif qq in self._qq_to_mc:
                        reply_txt = "绑定失败：你的QQ号已绑定过另一个MC身份，请先 /unbind。"
                    else:
                        self._bindings[mc_id] = qq
                        # 写入绑定元数据：mid -> {server, player, player_uuid, display}；供QQ→MC回复推送反查
                        self._binding_meta[mc_id] = {
                            "server": info.get("server", ""),
                            "player": info.get("player", ""),
                            "player_uuid": info.get("player_uuid", ""),
                            "display": info.get("display", ""),
                        }
                        self._save_bindings()
                        reply_txt = (
                            f"✅ 绑定成功：MC[{info.get('server','')}] {info.get('display','')}"
                            f"  ↔  QQ {qq}。记忆、印象、指令权限已互通。"
                        )
                        # 写一条 memory_companion 系统事件
                        if bool(self.config.get("ENABLE_SYNC_TO_MEMORY_COMPANION", True)):
                            self._spawn(
                                self._buffer_and_sync_memory(
                                    None,
                                    self._qq_to_user_key(qq),
                                    qq,
                                    sender_name or info.get("display", ""),
                                    f"[系统] MC身份 {mc_id} 与本QQ号绑定完成（权限/记忆互通）",
                                    "system",
                                    platform="qq",
                                    qq_number=qq,
                                    session_id=self._qq_session_id(event),
                                    group_id=group_id, group_name=group_name,
                                )
                            )
                else:
                    reply_txt = f"已拒绝绑定请求：MC玩家 {info.get('display','')}。"
                # 回执给MC玩家 tellraw 私发
                if sv and player:
                    await self._tellraw_private(
                        sv, player,
                        reply_txt if is_accept else "QQ方拒绝了绑定请求。"
                    )
                yield event.plain_result(reply_txt)
                return

        # 若是 AstrBot 系统命令（以 / 开头），不写入记忆（但 /mc_forward /unbind /mc_bindings 被上层 command 处理，这里不拦）
        # 注意：command 装饰器优先于 event_message_type(ALL) 执行，所以这里只拿到执行后的旁路事件。
        # 但是部分版本也可能会经过 all 监听器再进 command，因此用"如果是 /mc 系列 /mc_forward /unbind /mc_bindings /mc_confirm 就跳过记忆"
        if re.match(r"^\s*/(mc|mc_bridge|mc_confirm|mc_forward|mc_bindings|unbind)(\s|$)", plain or ""):
            return

        # 3) 记录 QQ 会话 + 写入 memory_companion / impression
        if plain and sender_id:
            session_id = self._qq_session_id(event, group_id=group_id, user_id=sender_id, private=is_private)
            user_key = self._qq_to_user_key(sender_id)
            now_time = datetime.now().strftime("%H:%M:%S")
            # 引用回复检测：[CQ:reply,id=xxx]
            raw_msg_str = getattr(event, "message_str", "") or ""
            reply_to = None
            rm = re.search(r"\[CQ:reply,id=(-?\d+)[^\]]*\]", raw_msg_str or "")
            if rm:
                reply_to = rm.group(1)
            entry = {
                "player": sender_id,
                "name": sender_name or sender_id,
                "player_uuid": "",
                "server": "",
                "qq_number": sender_id,
                "group_id": group_id,
                "group_name": group_name,
                "is_private": is_private,
                "reply_to_id": reply_to,
                "message": plain,
                "timestamp": int(time.time() * 1000),
                "is_bot": False,
                "time": now_time,
            }
            async with self._lock:
                ses = self._sessions.setdefault(session_id, [])
                ses.append(entry)
                max_h = int(self.config.get("MAX_HISTORY_PER_SERVER", 300))
                if len(ses) > max_h:
                    ses[:] = ses[-max_h:]
                self._save_json(SESSION_FILE, self._sessions)
            # 交互计数 + 印象
            self._interaction_count[user_key] = self._interaction_count.get(user_key, 0) + 1
            if bool(self.config.get("ENABLE_SYNC_TO_MEMORY_COMPANION", True)):
                fmt = str(self.config.get("FORWARD_FMT_QQ_TO_MC",
                                           "[QQ群{group}] <{sender}> {message}")
                          or "[QQ群{group}] <{sender}> {message}")
                try:
                    text_for_mem = fmt.format(
                        group=group_id or "私聊",
                        group_name=group_name or "",
                        sender=sender_name or sender_id,
                        qq=sender_id,
                        message=plain,
                    )
                except Exception:
                    text_for_mem = f"[QQ{group_id or '私聊'}] <{sender_name or sender_id}> {plain}"
                self._spawn(
                    self._buffer_and_sync_memory(
                        None,
                        user_key,
                        sender_id,
                        sender_name or sender_id,
                        text_for_mem,
                        "chat",
                        platform="qq",
                        session_id=session_id,
                        group_id=group_id,
                        group_name=group_name,
                        qq_number=sender_id,
                    )
                )
            if bool(self.config.get("ENABLE_SYNC_TO_IMPRESSION", True)):
                self._spawn(
                    self._try_update_impression(
                        user_key, sender_id, sender_name or sender_id, None, "",
                        triggered=False, session_id=session_id, qq_only=True,
                    )
                )

    # ---- QQ侧bot回复同步推送MC（on_decorating_result 钩子） ----
    # 绑定后视为QQ私聊会话：QQ用户私聊bot时，bot回复不仅发到QQ，也 tellraw 私发给绑定的MC玩家，
    # 实现双向互通（MC→QQ 已由 _push_reply_to_qq_if_bound 实现；QQ→MC 由本钩子实现）。
    # 群消息不推送，避免干扰。MC自身产生的回复不会触发本钩子（MC消息不走AstrBot event管道）。
    @filter.on_decorating_result()
    async def _on_qq_reply_decorating(self, event: AstrMessageEvent, *args, **kwargs):
        if not bool(self.config.get("ENABLE_PUSH_QQ_REPLY_TO_MC", True)):
            return
        try:
            platform = str(event.get_platform_name() or event.get_platform_id() or "")
        except Exception:
            platform = ""
        if platform.lower() != "aiocqhttp":
            return
        # 仅私聊场景推送（群消息不推MC，避免刷屏）
        try:
            is_private = bool(event.is_private_chat()) if hasattr(event, "is_private_chat") else False
        except Exception:
            is_private = False
        if not is_private:
            return
        qq = str(event.get_sender_id() or "")
        if not qq:
            return
        mid = self._qq_to_mc.get(qq)
        if not mid:
            return
        meta = self._binding_meta.get(mid)
        if not meta:
            return
        sv_name = meta.get("server", "")
        sv = self._servers.get(sv_name)
        if sv is None:
            return
        player = meta.get("player", "")
        if not player:
            return
        # 取回复纯文本
        try:
            result = event.get_result()
        except Exception:
            result = None
        if result is None:
            return
        try:
            text = result.get_plain_text() if hasattr(result, "get_plain_text") else ""
        except Exception:
            text = ""
        text = (text or "").strip()
        if not text:
            return
        # tellraw 私发给绑定的MC玩家
        self._spawn(self._tellraw_private(sv, player, text))
        logger.info(f"[MCBridge] QQ私聊回复推送MC: qq={qq} player={player} server={sv_name}")

    # ------------------------------------------------------------------ 杂项工具

    def _load_json(self, path, default):
        try:
            if not os.path.exists(path):
                return deepcopy(default)
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"[MCBridge] 读取 {os.path.basename(path)} 失败: {e}")
            return deepcopy(default)

    def _save_json(self, path, data):
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            logger.warning(f"[MCBridge] 写入 {os.path.basename(path)} 失败: {e}")


class _DummyContext:
    """极简 async context manager 占位。"""
    async def __aenter__(self):
        return None
    async def __aexit__(self, exc_type, exc, tb):
        return False
