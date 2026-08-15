"""
AstrBot Plugin - MC服务器桥接 2.0.0  (author: uGmTEAM)

核心特性：
  1. 多服务器：SERVERS JSON数组配置；每台服务器独立会话、独立显示名/关键词/回传通道/Token。
  2. 正版合并：online_mode=true 的服务器，按玩家正版UUID跨服合并 记忆/印象/上下文。
  3. 记忆同步：异步 fire-and-forget 同步到 memory_companion（bridge.submit_emotion_event + memory_api.record 双回退）。
  4. 印象同步：直接调用 impression 插件实例的 _save_summary 更新/写入印象。
  5. 管理入口：PermissionType.ADMIN 下的 /mc 系列命令；同时注册 LLM 工具自然语言触发。
     权限分级：SUPER_ADMIN_IDS 全权限；PermissionType.ADMIN 仅查询+运维（风险类禁行）。
     二次确认：若 CMD_CONFIRMATION_REQUIRED=true，玩家管理/游戏操作类需管理员再发 确认/是/y/好。
  6. 处罚联动：ENABLE_CROSS_SERVER_PUNISH=true 且目标服务器 online_mode=true 时，踢/禁言/白名单跨服广播。
  7. 回传：tellraw；通道二选一(bridge HTTP / RCON)，可每服独立配置。

协议：
  MC → AstrBot（每台MC服配置自己的推送端口）:
    POST http://<astrbot-host>:<listen_port>/mc_chat         聊天消息
    POST http://<astrbot-host>:<listen_port>/mc_handshake    启动握手元数据(online_mode/版本等)
    Header: Authorization: Bearer <bridge_token>    (token 为空则不校验)
  AstrBot → MC:
    bridge:  POST http://<mc_host>:<mc_bridge_port>/execute  Body {"command":"..."}
    rcon :   直连 <mc_host>:<mc_rcon_port> 发送原始命令

用户身份 key（标签）:
  非正版服(独立):  mcs[<服务器名>].<玩家名>
  正版服  (合并):  mcs[uuid:<正版UUID>].<玩家名>   （跨多服共用）
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
MEMORY_COMPANION_NAME = "astrbot_plugin_memory_companion"
IMPRESSION_NAME = "astrbot_plugin_impression"

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_PLUGIN_DIR, "data")
SESSION_FILE = os.path.join(DATA_DIR, "mc_session.json")  # key=server_name -> entries
STATE_FILE = os.path.join(DATA_DIR, "mc_state.json")

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
        return cls(
            name=str(d.get("name", "")).strip(),
            host=str(d.get("host", "127.0.0.1") or "127.0.0.1").strip(),
            listen_port=int(d.get("listen_port", 6188)),
            bridge_token=str(d.get("bridge_token", "") or ""),
            send_channel=str(d.get("send_channel", "bridge") or "bridge").strip().lower(),
            mc_bridge_port=int(d.get("mc_bridge_port", 25580)),
            mc_rcon_port=int(d.get("mc_rcon_port", 25575)),
            mc_rcon_password=str(d.get("mc_rcon_password", "") or ""),
            bot_name=str(d.get("bot_name", "Kei") or "Kei").strip() or "Kei",
            tellraw_template=str(d.get("tellraw_template") or "§7<{BOT_NAME}> {message}"),
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
    "MC多服桥接：虚拟会话记录 + LLM tellraw 回复 + 同步记忆(memory_companion)/印象(impression) + 管理员自然语言工具与/mc指令。",
    "2.0.0",
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
        )  # server_name -> entries
        # 握手覆盖的 online_mode 真值（持久化，确保重启后一致）
        self._handshake_state: dict[str, bool] = self._load_json(STATE_FILE, {})
        # 应用 online_mode 覆盖
        for n, om in self._handshake_state.items():
            if n in self._servers:
                self._servers[n].online_mode = bool(om)

        # 交互计数 / 冷却 / 待确认
        self._interaction_count: dict[str, int] = {}  # user_key -> count
        self._mem_sync_acc: dict[str, list[dict]] = {}  # server_name -> buffered for batch
        self._last_reply_ts: dict[tuple[str, str], float] = {}  # (server, user) -> ts
        self._pending_confirmations: dict[str, dict] = {}  # token -> {func, timeout_at, user_id, server, cmd}

        self._lock = asyncio.Lock()
        self._rcon_locks: dict[str, asyncio.Lock] = {}
        self._http_runners: list[tuple] = []  # (runner, site, port)
        self._client_session: Optional[aiohttp.ClientSession] = None
        self._bg_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ 生命周期

    async def initialize(self):
        if aiohttp is None:
            logger.error("[MCBridge] 缺少依赖 aiohttp，无法启动 HTTP 接收/回传通道")
            return
        if not self._servers:
            logger.warning("[MCBridge] 未配置任何服务器，请在插件配置 SERVERS 中填写 JSON 数组")
            return
        await self._start_http_servers()
        for s in self._servers.values():
            logger.info(
                f"[MCBridge] 已接入服务器 [{s.name}] (host={s.host} listen=:{s.listen_port} "
                f"channel={s.send_channel} online_mode={s.online_mode})"
            )
        # 注册 LLM 工具
        if bool(self.config.get("ENABLE_NATURAL_LANGUAGE_TOOL", True)):
            self._register_llm_tools()

    async def on_unload(self):
        for t in list(self._bg_tasks):
            t.cancel()
        self._bg_tasks.clear()
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

    # ------------------------------------------------------------------ HTTP 接收服务（每台服务器一个 aiohttp Site，按 listen_port 区分来源）

    async def _start_http_servers(self):
        host = str(self.config.get("ASTRBOT_LISTEN_HOST", "0.0.0.0") or "0.0.0.0").strip()
        for sv in self._servers.values():
            app = web.Application()
            # 捕获 listen_port -> 找到对应服务器配置，传参给 handler
            sv_ref = sv
            app.router.add_post(
                "/mc_chat", lambda r, s=sv_ref: self._handle_mc_chat(r, s)
            )
            app.router.add_post(
                "/mc_handshake", lambda r, s=sv_ref: self._handle_mc_handshake(r, s)
            )
            app.router.add_get(
                "/status", lambda r, s=sv_ref: self._handle_status(r, s)
            )
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, host, sv.listen_port)
            try:
                await site.start()
            except Exception as e:
                logger.error(
                    f"[MCBridge] 服务器[{sv.name}] HTTP 监听 :{sv.listen_port} 失败 (端口占用?): {e}"
                )
                continue
            self._http_runners.append((runner, site, sv.listen_port))

    async def _check_token(self, request, sv: ServerCfg) -> bool:
        token = (sv.bridge_token or "").strip()
        if not token:
            return True
        auth = request.headers.get("Authorization", "")
        if auth == f"Bearer {token}" or request.headers.get("X-Bridge-Token", "") == token:
            return True
        return False

    async def _handle_mc_chat(self, request, sv: ServerCfg):
        if not await self._check_token(request, sv):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad json"}, status=400)
        player = str(data.get("player", "") or "").strip()
        display = str(data.get("display_name", "") or player).strip()
        message = str(data.get("message", "") or "").strip()
        ts = data.get("timestamp") or int(time.time() * 1000)
        player_uuid = str(data.get("player_uuid", "") or "").strip()  # 正版UUID
        server_name_report = str(data.get("server_name", "") or sv.name).strip()
        if not player or not message:
            return web.json_response({"ok": False, "error": "missing player/message"}, status=400)
        self._spawn(
            self._on_mc_message(
                sv, player, display, message, int(ts), player_uuid, server_name_report
            )
        )
        return web.json_response({"ok": True})

    async def _handle_mc_handshake(self, request, sv: ServerCfg):
        if not await self._check_token(request, sv):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad json"}, status=400)
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
        return web.json_response(
            {
                "ok": True,
                "server": sv.name,
                "online_mode": sv.online_mode,
                "timestamp": int(time.time() * 1000),
            }
        )

    async def _handle_status(self, request, sv: ServerCfg):
        return web.json_response(
            {
                "ok": True,
                "server": {
                    "name": sv.name,
                    "host": sv.host,
                    "listen_port": sv.listen_port,
                    "send_channel": sv.send_channel,
                    "online_mode": sv.online_mode,
                    "bot_name": sv.bot_name,
                },
                "session_count": len(self._sessions.get(sv.name, [])),
            }
        )

    # ------------------------------------------------------------------ 消息记录 + 触发

    def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self._bg_tasks.add(t)
        t.add_done_callback(self._bg_tasks.discard)
        return t

    def _identity_user_key(self, sv: ServerCfg, player: str, player_uuid: str) -> tuple[str, str]:
        """返回 (user_key, display_label)。
        - 正版服(online_mode=true)且有UUID：按 UUID 全局合并 -> user:mcs[uuid:xxx].player
        - 非正版服或缺失UUID：按服务器独立 -> user:mcs[server_name].player
        """
        if sv.online_mode and player_uuid and len(player_uuid) > 10:
            key = f"user:mcs[uuid:{player_uuid}].{player}"
            return key, f"mcs[uuid:{player_uuid}]"
        key = f"user:mcs[{sv.name}].{player}"
        return key, f"mcs[{sv.name}]"

    def _passes_filters(self, sv: ServerCfg, player: str, message: str) -> bool:
        if sv.player_whitelist and player not in sv.player_whitelist:
            return False
        if player in sv.player_blacklist:
            return False
        for kw in sv.message_filter:
            if kw and kw in message:
                return False
        return True

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
        if not self._passes_filters(sv, player, message):
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
        async with self._lock:
            ses = self._sessions.setdefault(sv.name, [])
            ses.append(entry)
            max_h = int(self.config.get("MAX_HISTORY_PER_SERVER", 300))
            if len(ses) > max_h:
                self._sessions[sv.name] = ses[-max_h:]
            self._save_json(SESSION_FILE, self._sessions)
        # 交互计数（用户级，用于印象触发；正版合并时跨服同一个user_key）
        self._interaction_count[user_key] = self._interaction_count.get(user_key, 0) + 1
        # 同步到 memory_companion（异步、批处理）
        if bool(self.config.get("ENABLE_SYNC_TO_MEMORY_COMPANION", True)):
            self._spawn(self._buffer_and_sync_memory(sv, user_key, player, display, message, "chat"))
        # 触发 LLM 回复？
        if bool(self.config.get("ENABLE_LLM_REPLY", True)) and self._should_trigger(sv, message):
            async with self._reply_cooldown(sv, player):
                if not await self._can_reply_now(sv, player):
                    return
                reply = await self._generate_reply(sv, player, display, message, player_uuid, user_key)
            if reply:
                await self._send_tellraw(sv, reply)
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
                    ses = self._sessions.setdefault(sv.name, [])
                    ses.append(bot_entry)
                    max_h = int(self.config.get("MAX_HISTORY_PER_SERVER", 300))
                    if len(ses) > max_h:
                        self._sessions[sv.name] = ses[-max_h:]
                    self._save_json(SESSION_FILE, self._sessions)
                if bool(self.config.get("ENABLE_SYNC_TO_MEMORY_COMPANION", True)):
                    self._spawn(
                        self._buffer_and_sync_memory(
                            sv, user_key, sv.bot_name, sv.bot_name, reply, "llm_reply"
                        )
                    )
                if bool(self.config.get("ENABLE_SYNC_TO_IMPRESSION", True)):
                    self._spawn(
                        self._try_update_impression(
                            user_key, player, display, sv, player_uuid, triggered=True
                        )
                    )
        else:
            # 未触发回复，但仍可能达到交互次数 -> 更印象
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
        provider_id = self._get_provider_id()
        if not provider_id:
            logger.warning("[MCBridge] 未找到可用的 LLM provider，跳过回复")
            return None
        kwargs = {"chat_provider_id": provider_id, "prompt": prompt}
        if system_prompt:
            kwargs["system_prompt"] = system_prompt
        try:
            resp = await self.context.llm_generate(**kwargs)
            text = resp.completion_text if resp else None
            return text.strip() if text else None
        except Exception as e:
            logger.warning(f"[MCBridge] llm_generate 失败: {e}")
            return None

    async def _generate_reply(
        self,
        sv: ServerCfg,
        player: str,
        display: str,
        message: str,
        player_uuid: str,
        user_key: str,
    ) -> Optional[str]:
        ctx_count = int(self.config.get("LLM_CONTEXT_COUNT", 20))
        history = list(self._sessions.get(sv.name, []))
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
                if srv_name == sv.name:
                    continue
                srv_obj = self._servers.get(srv_name)
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

    # ------------------------------------------------------------------ tellraw 回传（bridge 或 RCON）

    def _build_tellraw_command(self, sv: ServerCfg, message: str) -> str:
        rendered = sv.tellraw_template.replace("{BOT_NAME}", sv.bot_name).replace(
            "{message}", message
        )
        text_json = rendered.replace("\\", "\\\\").replace('"', '\\"')
        return f'tellraw @a {{"text":"{text_json}"}}'

    async def _send_tellraw(self, sv: ServerCfg, message: str):
        cmd = self._build_tellraw_command(sv, message)
        if sv.send_channel == "rcon":
            await self._send_via_rcon(sv, cmd)
        else:
            await self._send_via_bridge(sv, cmd)

    async def _send_via_bridge(self, sv: ServerCfg, command: str) -> bool:
        url = f"http://{sv.host}:{sv.mc_bridge_port}/execute"
        if self._client_session is None or self._client_session.closed:
            self._client_session = aiohttp.ClientSession()
        headers = {"Content-Type": "application/json"}
        if sv.bridge_token.strip():
            headers["Authorization"] = f"Bearer {sv.bridge_token.strip()}"
        try:
            async with self._client_session.post(
                url,
                json={"command": command},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(
                        f"[MCBridge][{sv.name}] bridge执行失败 status={resp.status} body={body[:200]}"
                    )
                    return False
                return True
        except Exception as e:
            logger.warning(
                f"[MCBridge][{sv.name}] bridge请求失败 (确认MC端插件启动、端口/token一致): {e}"
            )
            return False

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
        sv: ServerCfg,
        user_key: str,
        user_id: str,
        username: str,
        text: str,
        event_type: str,
    ):
        interval = max(1, int(self.config.get("MEMORY_COMPANION_SYNC_INTERVAL", 5)))
        buf = self._mem_sync_acc.setdefault(sv.name, [])
        buf.append(
            {
                "t": int(time.time() * 1000),
                "user_id": user_id or f"mcs[{sv.name}]",
                "username": username,
                "text": f"[{sv.name}] <{username}> {text}",
                "event_type": event_type,
                "server": sv.name,
                "user_key": user_key,
            }
        )
        if len(buf) < interval:
            return
        batch = list(buf)
        buf.clear()
        companion = self._get_memory_companion()
        if not companion:
            return
        bridge = (
            getattr(companion, "bridge", None)
            or getattr(companion, "_bridge", None)
            or getattr(companion, "memory_companion", None)
        )
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        # 优先 bridge.submit_emotion_event
        if bridge and hasattr(bridge, "submit_emotion_event"):
            for rec in batch:
                try:
                    event = {
                        "producer_plugin": PLUGIN_NAME,
                        "origin_kind": "interaction",
                        "platform": "minecraft",
                        "bot_id": sv.bot_name,
                        "scope": "group",
                        "session_id": f"mc:{sv.name}:{rec['server']}",
                        "actor_ref": {
                            "kind": "mc_player",
                            "id": rec["user_key"],
                            "role": "user",
                        },
                        "target_ref": {"kind": "bot", "id": sv.bot_name, "role": "bot"},
                        "event_type": "chat" if rec["event_type"] == "chat" else "reply",
                        "intensity": 50.0,
                        "confidence": 0.8,
                        "occurred_at": now,
                        "status": "observed",
                        "dedupe_key": f"mc:{rec['t']}:{rec['server']}:{rec['username']}",
                        "payload": {
                            "text": rec["text"],
                            "username": rec["username"],
                            "extra": {"server": rec["server"]},
                        },
                    }
                    await bridge.submit_emotion_event(event)
                except Exception as e:
                    logger.debug(f"[MCBridge] submit_emotion_event 失败(单条跳过): {e}")
            return
        # 回退 memory_api.record
        memory_api = getattr(companion, "memory_api", None) or getattr(
            companion, "_memory_api", None
        )
        if memory_api and hasattr(memory_api, "record"):
            for rec in batch:
                try:
                    await memory_api.record(
                        rec["text"],
                        user_id=rec["user_key"],
                        username=rec["username"],
                        source="minecraft",
                        memory_type="chat",
                        level="today",
                        importance=6,
                        extra={"server": rec["server"]},
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
    ):
        trigger_count = max(0, int(self.config.get("IMPRESSION_TRIGGER_COUNT", 8)))
        cnt = self._interaction_count.get(user_key, 0)
        need = triggered or (trigger_count > 0 and cnt % trigger_count == 0 and cnt > 0)
        if not need:
            return
        imp = self._get_impression_plugin()
        if imp is None:
            return
        # 最小化模拟：如果 _update_summary 不需要真实 event，就直接用 _save_summary。
        # 但生成新印象必须走 LLM -> 简化做法：攒一段最近聊天文本当 prompt，调 LLM 生成 JSON。
        try:
            ctx_count = int(self.config.get("LLM_CONTEXT_COUNT", 20))
            # 此用户最近聊天 + 该服务器全部会话中近期
            recent_msgs = []
            async with self._lock:
                for e in self._sessions.get(sv.name, [])[-ctx_count:]:
                    if (
                        player_uuid
                        and sv.online_mode
                        and str(e.get("player_uuid", "")) == player_uuid
                    ):
                        recent_msgs.append(e)
                    elif e.get("player") == player:
                        recent_msgs.append(e)
            if not recent_msgs:
                return
            conv = "\n".join(
                f"[{x.get('time','')}] <{x.get('name')}> {x.get('message')}"
                for x in recent_msgs
            )
            new_data = await self._generate_impression_json(sv, player, display, conv, imp)
            if new_data and new_data.get("summary"):
                info = {"user_id": user_key, "user_name": display or player,
                        "group_id": f"mc:{sv.name}", "group_name": sv.name}
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
                        f"({len(new_data['summary'])}字) server={sv.name}"
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
                ok = await self._send_via_bridge(sv, command)
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
            yield reply(f"[MCBridge] v2.0.0  author: uGmTEAM  服务器={list(self._servers.keys())}")
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
                for entry in list(self._sessions.get(s.name, []))[-50:]:
                    self._spawn(
                        self._buffer_and_sync_memory(
                            s,
                            entry.get("player") or "",
                            entry.get("player") or "",
                            entry.get("name") or "",
                            entry.get("message") or "",
                            "chat",
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
                    content=f"MCBridge v2.0.0  author uGmTEAM  接入服务器: {list(host_self._servers.keys())}"
                )
            if operation == "resync":
                done = 0
                for s in host_self._servers.values():
                    if server_name not in ("all", s.name):
                        continue
                    for entry in list(host_self._sessions.get(s.name, []))[-100:]:
                        host_self._spawn(
                            host_self._buffer_and_sync_memory(
                                s,
                                entry.get("player") or "",
                                entry.get("player") or "",
                                entry.get("name") or "",
                                entry.get("message") or "",
                                "chat",
                            )
                        )
                        done += 1
                return ToolResult(content=f"已提交 {done} 条消息到记忆同步队列（印象随交互次数自动更新）")
            return ToolResult(error=f"未知 operation: {operation}")

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
