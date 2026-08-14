"""
AstrBot Plugin - MC服务器桥接 1.0.0
连接本地 Minecraft(Paper/Spigot) 服务器：
  1. 接收 MC 端 Bukkit 插件推送的玩家聊天，记录为虚拟群聊会话(本地 JSON)。
  2. 当玩家 @机器人 或消息含关键词时，调用 AstrBot 内置 LLM provider(并注入默认人设)
     基于会话上下文生成回复。
  3. 通过 tellraw 把回复发回 MC 服务器(桥接 HTTP / RCON 双通道，配置选择)。

通信协议(与 MC 端 McAstrbotBridge 插件约定一致)：
  - MC→AstrBot: POST http://<本机>:<LISTEN_PORT>/mc_chat
      Header: Authorization: Bearer <BRIDGE_TOKEN>   (token 为空则不校验)
      Body  : {"player":"...","display_name":"...","message":"...","timestamp":<毫秒>}
  - AstrBot→MC(桥接): POST http://<MC_HOST>:<MC_BRIDGE_PORT>/execute
      Header: Authorization: Bearer <BRIDGE_TOKEN>
      Body  : {"command":"tellraw @a ..."}
  - AstrBot→MC(RCON): 直接连 <MC_HOST>:<MC_RCON_PORT> 发送 tellraw 命令
"""
import os
import json
import time
import struct
import asyncio
from datetime import datetime

from astrbot.api import logger, AstrBotConfig
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_mc_bridge"
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_PLUGIN_DIR, "data")
SESSION_FILE = os.path.join(DATA_DIR, "mc_session.json")

try:
    import aiohttp
    from aiohttp import web
except ImportError:  # pragma: no cover
    aiohttp = None
    web = None


@register(
    PLUGIN_NAME,
    "trae",
    "MC服务器桥接 — 记录玩家聊天为虚拟会话，LLM回复@/关键词触发并通过tellraw发回MC",
    "1.0.0",
    "",
)
class MCBridgePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        os.makedirs(DATA_DIR, exist_ok=True)
        self._session = self._load_json(SESSION_FILE, [])
        # 会话写入锁
        self._lock = asyncio.Lock()
        # 回复串行锁：避免并发触发导致回复乱序/重复
        self._reply_lock = asyncio.Lock()
        # RCON 串行锁
        self._rcon_lock = asyncio.Lock()
        # aiohttp 接收端
        self._http_runner = None
        self._http_site = None
        # aiohttp 客户端(发往MC桥接)
        self._client_session = None
        # 后台任务引用集合(防止被GC)
        self._bg_tasks = set()

    # ======================================================================
    # 生命周期
    # ======================================================================
    async def initialize(self):
        if aiohttp is None:
            logger.error("[MCBridge] 缺少 aiohttp 依赖，HTTP 接收/桥接不可用，请安装 aiohttp")
            return
        try:
            await self._start_http_server()
            port = int(self.config.get("LISTEN_PORT", 6188))
            logger.info(f"[MCBridge] HTTP 接收服务已启动 监听 0.0.0.0:{port} (路径 /mc_chat)")
            logger.info(
                f"[MCBridge] 回传通道={self.config.get('SEND_CHANNEL', 'bridge')} "
                f"BOT_NAME={self.config.get('BOT_NAME', 'Kei')}"
            )
        except Exception as e:
            logger.error(f"[MCBridge] HTTP 接收服务启动失败: {e}", exc_info=True)

    async def on_unload(self):
        for t in list(self._bg_tasks):
            t.cancel()
        self._bg_tasks.clear()
        try:
            if self._http_site is not None:
                await self._http_site.stop()
        except Exception:
            pass
        try:
            if self._http_runner is not None:
                await self._http_runner.cleanup()
        except Exception:
            pass
        try:
            if self._client_session is not None and not self._client_session.closed:
                await self._client_session.close()
        except Exception:
            pass
        logger.info("[MCBridge] 已停用")

    # ======================================================================
    # HTTP 接收服务
    # ======================================================================
    async def _start_http_server(self):
        app = web.Application()
        app.router.add_post("/mc_chat", self._handle_mc_chat)
        app.router.add_get("/status", self._handle_status)
        port = int(self.config.get("LISTEN_PORT", 6188))
        self._http_runner = web.AppRunner(app)
        await self._http_runner.setup()
        # 监听 0.0.0.0 以便局域网内的MC服务器可访问；靠 BRIDGE_TOKEN 鉴权
        self._http_site = web.TCPSite(self._http_runner, "0.0.0.0", port)
        await self._http_site.start()

    async def _check_token(self, request) -> bool:
        token = str(self.config.get("BRIDGE_TOKEN", "") or "").strip()
        if not token:
            return True
        auth = request.headers.get("Authorization", "")
        if auth == f"Bearer {token}":
            return True
        if request.headers.get("X-Bridge-Token", "") == token:
            return True
        return False

    async def _handle_mc_chat(self, request):
        if not await self._check_token(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad json"}, status=400)
        player = str(data.get("player", "") or "").strip()
        display = str(data.get("display_name", "") or player).strip()
        message = str(data.get("message", "") or "").strip()
        ts = data.get("timestamp") or int(time.time() * 1000)
        if not player or not message:
            return web.json_response({"ok": False, "error": "missing player/message"}, status=400)
        # 立即返回 200，后台处理记录与触发(LLM 可能较慢)
        self._spawn(self._on_mc_message(player, display, message, int(ts)))
        return web.json_response({"ok": True})

    async def _handle_status(self, request):
        return web.json_response(
            {
                "ok": True,
                "data": {
                    "session_count": len(self._session),
                    "bot_name": self.config.get("BOT_NAME", "Kei"),
                    "send_channel": self.config.get("SEND_CHANNEL", "bridge"),
                    "llm_reply_enabled": bool(self.config.get("LLM_REPLY_ENABLED", True)),
                },
            }
        )

    def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self._bg_tasks.add(t)
        t.add_done_callback(self._bg_tasks.discard)
        return t

    # ======================================================================
    # 消息记录与触发
    # ======================================================================
    def _passes_filters(self, player: str, message: str) -> bool:
        wl = self.config.get("PLAYER_WHITELIST", []) or []
        if wl and player not in wl:
            return False
        bl = self.config.get("PLAYER_BLACKLIST", []) or []
        if player in bl:
            return False
        for kw in self.config.get("MESSAGE_FILTER_KEYWORDS", []) or []:
            if kw and str(kw) in message:
                return False
        return True

    def _mapped_name(self, player: str) -> str:
        raw = str(self.config.get("PLAYER_NAME_MAP", "") or "").strip()
        if raw:
            try:
                m = json.loads(raw)
                if isinstance(m, dict) and player in m:
                    return str(m[player])
            except Exception:
                pass
        return player

    async def _on_mc_message(self, player: str, display: str, message: str, ts: int):
        if not self._passes_filters(player, message):
            return
        name = self._mapped_name(player) or display or player
        entry = {
            "player": player,
            "name": name,
            "message": message,
            "timestamp": ts,
            "is_bot": False,
            "time": datetime.now().strftime("%H:%M:%S"),
        }
        async with self._lock:
            self._session.append(entry)
            max_h = int(self.config.get("MAX_HISTORY", 200))
            if len(self._session) > max_h:
                self._session = self._session[-max_h:]
            self._save_json(SESSION_FILE, self._session)

        if bool(self.config.get("LLM_REPLY_ENABLED", True)) and self._should_trigger(message):
            async with self._reply_lock:
                try:
                    await self._generate_and_send_reply(name, message)
                except Exception as e:
                    logger.warning(f"[MCBridge] LLM 回复失败: {e}", exc_info=True)

    def _should_trigger(self, message: str) -> bool:
        bot_name = str(self.config.get("BOT_NAME", "") or "").strip()
        for kw in self.config.get("TRIGGER_KEYWORDS", []) or []:
            if kw and str(kw) in message:
                return True
        if bool(self.config.get("ENABLE_AT_TRIGGER", True)) and bot_name:
            if message.startswith(bot_name) or message.startswith(f"@{bot_name}"):
                return True
            if f"@{bot_name}" in message:
                return True
            if message.lower().startswith(bot_name.lower()):
                return True
        return False

    # ======================================================================
    # LLM 调用
    # ======================================================================
    async def _get_system_prompt(self) -> str:
        """取 AstrBot 默认人设 prompt。llm_generate 不会自动注入人设，需手动传入。"""
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
                logger.debug(f"[MCBridge] 获取人设失败: {e}")
                return ""
        if not persona:
            return ""
        sp = getattr(persona, "prompt", None)
        if not sp and isinstance(persona, dict):
            sp = persona.get("prompt", "")
        return str(sp or "")

    def _get_provider_id(self):
        # 优先用会话默认 provider
        try:
            provider = self.context.get_using_provider()
            if provider is not None:
                pid = getattr(provider.meta(), "id", None) if callable(getattr(provider, "meta", None)) else None
                if pid:
                    return pid
        except Exception as e:
            logger.debug(f"[MCBridge] get_using_provider 失败: {e}")
        try:
            return self.context.get_current_chat_provider_id(None)
        except Exception:
            return None

    async def _llm_reply(self, prompt: str, system_prompt: str):
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

    async def _generate_and_send_reply(self, trigger_name: str, trigger_message: str):
        bot_name = str(self.config.get("BOT_NAME", "") or "AI").strip()
        ctx_count = int(self.config.get("LLM_CONTEXT_COUNT", 20))
        recent = self._session[-ctx_count:] if ctx_count > 0 else self._session
        lines = []
        for e in recent:
            who = bot_name if e.get("is_bot") else e.get("name", e.get("player", ""))
            lines.append(f"[{e.get('time', '')}] <{who}> {e.get('message', '')}")
        history = "\n".join(lines)
        prompt = (
            f"以下是 Minecraft 服务器里的群聊会话记录，你({bot_name})也是其中一员。\n"
            f"刚刚 <{trigger_name}> 提到了你。请结合聊天上下文，以 {bot_name} 的身份用一句简短自然的口语回复。"
            f"不要加 <名字> 前缀，不要解释你是 AI，不要复述别人说过的话，直接输出回复内容：\n\n"
            f"{history}\n\n你的回复："
        )
        system_prompt = await self._get_system_prompt()
        reply = await self._llm_reply(prompt, system_prompt)
        if not reply:
            return
        reply = reply.strip().strip('"').strip("'").strip()
        if not reply:
            return
        # 记录机器人回复到会话
        async with self._lock:
            self._session.append(
                {
                    "player": bot_name,
                    "name": bot_name,
                    "message": reply,
                    "timestamp": int(time.time() * 1000),
                    "is_bot": True,
                    "time": datetime.now().strftime("%H:%M:%S"),
                }
            )
            max_h = int(self.config.get("MAX_HISTORY", 200))
            if len(self._session) > max_h:
                self._session = self._session[-max_h:]
            self._save_json(SESSION_FILE, self._session)
        # tellraw 发回 MC
        await self._send_tellraw(reply)

    # ======================================================================
    # tellraw 回传
    # ======================================================================
    def _build_tellraw_command(self, message: str) -> str:
        bot_name = str(self.config.get("BOT_NAME", "") or "AI").strip()
        template = (
            str(self.config.get("TELLRAW_TEMPLATE", "") or "§7<{BOT_NAME}> {message}")
        )
        rendered = template.replace("{BOT_NAME}", bot_name).replace("{message}", message)
        # 转义 JSON 字符串内的特殊字符
        text_json = rendered.replace("\\", "\\\\").replace('"', '\\"')
        return f'tellraw @a {{"text":"{text_json}"}}'

    async def _send_tellraw(self, message: str):
        command = self._build_tellraw_command(message)
        channel = str(self.config.get("SEND_CHANNEL", "bridge") or "bridge").strip().lower()
        if channel == "rcon":
            await self._send_via_rcon(command)
        else:
            await self._send_via_bridge(command)

    async def _send_via_bridge(self, command: str):
        host = str(self.config.get("MC_HOST", "127.0.0.1"))
        port = int(self.config.get("MC_BRIDGE_PORT", 25580))
        token = str(self.config.get("BRIDGE_TOKEN", "") or "").strip()
        url = f"http://{host}:{port}/execute"
        if self._client_session is None or self._client_session.closed:
            self._client_session = aiohttp.ClientSession()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
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
                        f"[MCBridge] 桥接执行失败 status={resp.status} body={body[:200]}"
                    )
        except Exception as e:
            logger.warning(f"[MCBridge] 桥接请求失败(确认MC端插件已运行且端口/token一致): {e}")

    async def _send_via_rcon(self, command: str):
        host = str(self.config.get("MC_HOST", "127.0.0.1"))
        port = int(self.config.get("MC_RCON_PORT", 25575))
        password = str(self.config.get("MC_RCON_PASSWORD", "") or "")
        if not password:
            logger.warning("[MCBridge] RCON 模式但未配置 MC_RCON_PASSWORD，跳过")
            return
        async with self._rcon_lock:
            try:
                await asyncio.wait_for(
                    self._rcon_command(host, port, password, command), timeout=10
                )
            except Exception as e:
                logger.warning(f"[MCBridge] RCON 执行失败: {e}")

    # ---- 简易异步 Source RCON 客户端 ----
    async def _rcon_command(self, host: str, port: int, password: str, command: str) -> str:
        reader, writer = await asyncio.open_connection(host, port)
        try:
            await self._rcon_send(writer, 1, 3, password)
            rid, _rtype, _payload = await self._rcon_recv(reader)
            if rid == -1:
                raise Exception("RCON 认证失败(密码错误或 rcon 未开启)")
            await self._rcon_send(writer, 2, 2, command)
            _rid2, _rtype2, payload = await self._rcon_recv(reader)
            return payload.decode("utf-8", "ignore")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _rcon_send(self, writer, req_id: int, ptype: int, payload: str):
        p = payload.encode("utf-8")
        length = 4 + 4 + len(p) + 2  # id + type + payload + 两个 \x00
        packet = struct.pack("<iii", length, req_id, ptype) + p + b"\x00\x00"
        writer.write(packet)
        await writer.drain()

    async def _rcon_recv(self, reader):
        header = await reader.readexactly(4)
        length = struct.unpack("<i", header)[0]
        if length < 10 or length > 4096:
            raise Exception(f"RCON 非法包长度: {length}")
        body = await reader.readexactly(length)
        req_id = struct.unpack("<i", body[0:4])[0]
        ptype = struct.unpack("<i", body[4:8])[0]
        payload = body[8:-2]  # 去掉末尾两个 \x00
        return req_id, ptype, payload

    # ======================================================================
    # 持久化工具
    # ======================================================================
    def _load_json(self, path, default):
        try:
            if not os.path.exists(path):
                return default
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"[MCBridge] 读取 {path} 失败: {e}")
            return default

    def _save_json(self, path, data):
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            logger.warning(f"[MCBridge] 写入 {path} 失败: {e}")
