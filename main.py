"""AstrBot YouTube 订阅提醒插件。

订阅 YouTube 频道，直播上播/下播与新投稿支持图片或文字通知推送。
订阅按会话（unified_msg_origin）隔离。

指令：
    /yt订阅 <channel_id>          订阅频道
    /yt取消订阅 <channel_id>      取消订阅
    /yt批量订阅 <目标> <目标>...   批量订阅（空格分隔）
    /yt批量取消订阅 <目标>...      批量取消订阅（空格分隔）
    /yt列表                       查看本会话订阅
    /yt直播测试 <目标>            抓目标直播并推送一条测试通知
    /yt视频测试 <目标>            抓目标最新视频并推送一条测试通知
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import NamedTuple, Optional

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .renderer import NotificationRenderer
from .services.cleanup import ImageCleaner
from .services.data_api import (
    ApiKeyMissingError,
    ChannelNotFoundError,
    InvalidApiKeyError,
    QuotaExceededError,
    YouTubeDataAPI,
    parse_channel_input,
)
from .services.feed import LegacyFeedClient
from .services.livebroadcasts import LiveBroadcastsClient
from .services.models import (
    ChannelMeta,
    FeedResult,
    Notification,
    TYPE_LIVE_END,
    TYPE_LIVE_START,
    TYPE_NEW_VIDEO,
)
from .services.notifier import NotificationService
from .services.oauth import OAuthManager
from .services.page_json import ChannelPageClient, parse_target
from .services.poller import PollScheduler
from .services.state_machine import seed_channel_from_feed
from .services.store import SubscriptionStore
from .services.websub import WebSubManager
from .services.websub_server import WebSubCallbackServer
from .utils import format_time_zh, register_font_dirs

PLUGIN_NAME = "astrbot_plugin_youtube_notifier"
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)

# 批量命令单条消息最多处理的目标数。每个**新**频道要花 1 单位 channels.list
# （解析 handle）+ 2 单位快照（playlistItems + videos.list），还要一次网络往返；
# 不设上限的话，一条粘进来的长列表会连打几十个请求，把消息处理卡到超时、
# 并且吃掉一天的配额。超出的部分会明确告知用户，不静默丢弃。
BATCH_MAX_TARGETS = 20

# 批量命令的完整命令名（含别名）。用于从原始消息里剥离命令名，见
# `_split_after_command()`。改命令名/别名时这里必须同步改。
_BATCH_SUBSCRIBE_NAMES = frozenset(
    {"yt批量订阅", "youtube批量订阅", "yt_batch_subscribe"}
)
_BATCH_UNSUBSCRIBE_NAMES = frozenset(
    {"yt批量取消订阅", "youtube批量取消订阅", "yt_batch_unsubscribe"}
)

_SUBSCRIBE_USAGE = (
    "用法: /yt订阅 <@handle 或 频道ID 或 频道URL>\n"
    "例如:\n"
    "  /yt订阅 @ukaisaki\n"
    "  /yt订阅 https://www.youtube.com/@ukaisaki\n"
    "  /yt订阅 UCxxxxxxxxxxxxxxxxxxxxxx"
)

_BATCH_SUBSCRIBE_USAGE = (
    "用法: /yt批量订阅 <目标1> <目标2> ...\n"
    "目标之间用空格分隔，每个目标可以是 @handle / 频道ID / 频道URL\n"
    "例如:\n"
    "  /yt批量订阅 @ukaisaki @NASA UCxxxxxxxxxxxxxxxxxxxxxx\n"
    f"说明: 单条消息最多处理 {BATCH_MAX_TARGETS} 个目标；"
    "频道名里有空格时请改用 @handle 或频道ID。"
)

_BATCH_UNSUBSCRIBE_USAGE = (
    "用法: /yt批量取消订阅 <目标1> <目标2> ...\n"
    "目标之间用空格分隔，可以是 @handle / 频道ID / 频道名\n"
    "例如:\n"
    "  /yt批量取消订阅 @ukaisaki @NASA\n"
    f"说明: 单条消息最多处理 {BATCH_MAX_TARGETS} 个目标；"
    "频道名里有空格时请改用 @handle 或频道ID。"
)


class SubscribeOutcome(NamedTuple):
    """单次订阅的结果。`/yt订阅` 与 `/yt批量订阅` 共用同一份实现。"""

    status: str  # added / exists / failed
    channel_id: str = ""
    name: str = ""  # 展示用频道名（拿不到时回退成 channel_id）
    handle: str = ""  # @handle，用于回复里区分同名频道
    reason: str = ""  # failed 时的原因代码，见 _SUBSCRIBE_FAIL_*


class UnsubscribeOutcome(NamedTuple):
    """单次取消订阅的结果。"""

    status: str  # removed / missing
    channel_id: str = ""
    name: str = ""


# 订阅失败的原因代码 → 单条订阅的完整回复（与重构前的文案保持一致）
_SUBSCRIBE_FAIL_TEXT = {
    "empty": "频道标识为空，请检查输入",
    "not_found": "未找到频道: {raw}\n请确认 handle 或频道 ID 是否正确",
    "quota": "YouTube API 配额已耗尽，请稍后再试",
    "bad_key": "YouTube API Key 无效，请检查插件配置",
    "error": "解析频道失败，请稍后重试",
    "no_id": "未能解析出频道 ID: {raw}",
}

# 同一个原因代码 → 批量汇总里的短说明（不含输入回显，回显由行首的 raw 负责）
_SUBSCRIBE_FAIL_SHORT = {
    "empty": "输入无法识别",
    "not_found": "未找到频道",
    "quota": "API 配额已耗尽",
    "bad_key": "API Key 无效",
    "error": "解析失败",
    "no_id": "未能解析出频道 ID",
}


@register(
    PLUGIN_NAME,
    "yuiasami",
    "订阅 YouTube 频道，直播上/下播与新投稿支持图片或文字推送。",
    "v1.1.0",
)
class YouTubeNotifierPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        # AstrBotConfig 是 dict 子类；这里只做只读的 .get 取值，
        # 若框架未传配置则退化为空 dict（不构造 AstrBotConfig，避免误当作文件路径）。
        self.config = config if config is not None else {}
        self.data_dir: Path = StarTools.get_data_dir(PLUGIN_NAME)

        self._session: Optional[aiohttp.ClientSession] = None
        self._proxy: str = ""
        self.store: Optional[SubscriptionStore] = None
        self.data_api: Optional[YouTubeDataAPI] = None
        self.page_json: Optional[ChannelPageClient] = None
        self.renderer: Optional[NotificationRenderer] = None
        self.notifier: Optional[NotificationService] = None
        self.poller: Optional[PollScheduler] = None
        self.cleaner: Optional[ImageCleaner] = None
        self.websub: Optional[WebSubManager] = None
        self.websub_server: Optional[WebSubCallbackServer] = None
        # 运行期降级原因（如「API Key 无效」）。配置检查发现不了这类问题 ——
        # 配了 Key 不等于 Key 能用，必须靠真实调用暴露出来。
        self._runtime_degraded: str = ""

    # ------------------------------------------------------------ 生命周期

    async def initialize(self):
        cfg = self.config
        basic = cfg.get("basic", {}) or {}
        oauth_cfg = cfg.get("oauth", {}) or {}
        websub_cfg = cfg.get("websub", {}) or {}
        notify_cfg = cfg.get("notify", {}) or {}
        render_cfg = cfg.get("render", {}) or {}
        cleanup_cfg = cfg.get("cleanup", {}) or {}

        logger.info(f"[YT] 正在初始化 {PLUGIN_NAME} ...")

        proxy = str(basic.get("proxy", "") or "")
        self._proxy = proxy
        self._session = aiohttp.ClientSession(
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": "astrbot-youtube-notifier/1.0"},
        )
        oauth = OAuthManager(
            self._session,
            client_id=str(oauth_cfg.get("client_id", "") or ""),
            client_secret=str(oauth_cfg.get("client_secret", "") or ""),
            refresh_token=str(oauth_cfg.get("refresh_token", "") or ""),
        )
        self.data_api = YouTubeDataAPI(
            self._session, api_key=str(basic.get("api_key", "") or ""), proxy=proxy
        )
        legacy_feed = LegacyFeedClient(self._session, proxy=proxy)
        live_broadcasts = LiveBroadcastsClient(self._session, oauth, proxy=proxy)
        self.page_json = ChannelPageClient(
            self._session,
            proxy=proxy,
            min_interval=float(
                basic.get("page_fallback_min_interval_seconds", 60) or 60
            ),
        )

        self.store = SubscriptionStore(self.data_dir)
        self.store.load()

        # 登记 <data_dir>/fonts/ 为字体搜索目录。data 目录在 Docker 部署里
        # 通常被挂载到宿主机，所以这是「容器内可见 + 重启不丢」的放字体位置
        # —— 用户把 ttf 丢进去即可，无需挂载宿主机字库、也无需配 font_path。
        register_font_dirs([self.data_dir / "fonts"])

        notify_style = str(
            notify_cfg.get("style", "image") or "image"
        ).strip().lower()
        if notify_style == "text":
            # 文字模式不初始化 PIL/字体链，确保「无需字体」不仅是发送阶段跳过渲染，
            # 启动时也不会因缺少中文字体产生无关报错。
            self.renderer = None
        else:
            self.renderer = NotificationRenderer(
                image_width=int(render_cfg.get("image_width", 800) or 800),
                font_path=str(render_cfg.get("font_path", "") or ""),
                output_dir=self.data_dir / "images" / "notifications",
            )
        renderer = self.renderer
        mode = str(basic.get("live_detect_mode", "data_api") or "data_api")
        self.notifier = NotificationService(
            self.context,
            self.store,
            renderer,
            data_api=self.data_api,
            legacy_feed=legacy_feed,
            live_broadcasts=live_broadcasts,
            page_json=self.page_json,
            live_detect_mode=mode,
            page_fallback_enabled=bool(basic.get("page_fallback_enabled", True)),
            cover_download=bool(basic.get("cover_download", True)),
            max_results=int(basic.get("max_results", 5) or 5),
            image_dir=self.data_dir / "images" / "covers",
            notify_style=notify_style,
            enabled={
                TYPE_LIVE_START: bool(notify_cfg.get("live_start_enabled", True)),
                TYPE_LIVE_END: bool(notify_cfg.get("live_end_enabled", True)),
                TYPE_NEW_VIDEO: bool(notify_cfg.get("new_video_enabled", True)),
            },
        )

        self.poller = PollScheduler(
            self.store,
            self.notifier,
            interval_seconds=int(basic.get("poll_interval_seconds", 300) or 300),
        )
        self.poller.start()

        # 通知图与封面都是「用完即弃」的临时文件，必须定期清理，否则磁盘单调增长
        # （每张 300–700KB，5 分钟轮询 + 几个频道就能一天写满小 VPS）
        self.cleaner = ImageCleaner(
            [
                self.data_dir / "images" / "notifications",
                self.data_dir / "images" / "covers",
            ],
            retention_days=_cfg_int(cleanup_cfg, "retention_days", 7),
            max_total_mb=_cfg_int(cleanup_cfg, "max_total_mb", 500),
            hour=_cfg_int(cleanup_cfg, "hour", 4),
            run_on_startup=bool(cleanup_cfg.get("run_on_startup", True)),
        )
        if bool(cleanup_cfg.get("enabled", True)):
            self.cleaner.start()
        else:
            logger.info("[YT] 图片自动清理已关闭（cleanup.enabled=false）")

        await self._start_websub(websub_cfg)

        api_state = "已配置" if self.data_api.configured else "未配置（走网页兜底）"
        fallback_state = "开" if self.notifier.page_fallback_enabled else "关"
        logger.info(
            f"[YT] 初始化完成: 模式={mode} API Key={api_state} "
            f"网页兜底={fallback_state} 频道数={len(self.store.all_channel_ids())}"
        )

    async def _start_websub(self, websub_cfg: dict) -> None:
        if not bool(websub_cfg.get("enabled", False)):
            logger.info("[YT] WebSub 未启用，新投稿由轮询检测")
            return
        callback_url = str(websub_cfg.get("callback_url", "") or "")
        verify_token = str(websub_cfg.get("verify_token", "") or "")
        if not callback_url or not verify_token:
            logger.warning(
                "[YT] WebSub 已启用但缺少 callback_url/verify_token，已跳过"
            )
            return

        self.websub = WebSubManager(
            self._session, self.notifier, callback_url, verify_token
        )
        self.websub_server = WebSubCallbackServer(
            port=int(websub_cfg.get("callback_port", 8477) or 8477),
            on_verify=self.websub.handle_verification,
            on_push=self.websub.handle_push,
        )
        try:
            await self.websub_server.start()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[YT] WebSub 回调服务启动失败: {exc!r}")
            self.websub_server = None
            return

        # 为已有订阅补订阅
        for channel_id in self.store.all_channel_ids():
            await self.websub.subscribe(channel_id)

    async def terminate(self):
        logger.info(f"[YT] 正在停止 {PLUGIN_NAME} ...")
        if self.poller is not None:
            await self.poller.stop()
        if self.cleaner is not None:
            await self.cleaner.stop()
        if self.websub_server is not None:
            await self.websub_server.stop()
        if self.store is not None:
            await self.store.save()
        if self._session is not None and not self._session.closed:
            await self._session.close()
        logger.info(f"[YT] {PLUGIN_NAME} 已停止")

    # ------------------------------------------------------------ 指令

    @filter.command("yt订阅", alias={"yt_subscribe", "youtube订阅"})
    async def subscribe(self, event: AstrMessageEvent, channel_id: str = ""):
        """订阅 YouTube 频道，格式: /yt订阅 <@handle 或 频道ID 或 频道URL>"""
        raw = (channel_id or "").strip()
        if not raw:
            yield event.plain_result(_SUBSCRIBE_USAGE)
            return

        outcome = await self._subscribe_one(event.unified_msg_origin, raw)

        if outcome.status == "failed":
            yield event.plain_result(
                _SUBSCRIBE_FAIL_TEXT.get(outcome.reason, "订阅失败").format(raw=raw)
            )
            return

        if outcome.status == "exists":
            yield event.plain_result(f"本会话已订阅该频道: {outcome.name}")
            return

        # 数据源不可用时必须显式告知：否则订阅「成功」了但监控完全不工作，
        # 用户只会看到含糊的「暂无记录」，误以为一切正常。
        if not self._monitoring_ready():
            logger.error(
                f"[YT] 订阅已保存但监控不会工作：{self._monitoring_blocker()}"
            )
            yield event.plain_result(
                f"⚠️ 已保存订阅: {outcome.name}\n"
                f"频道ID: {outcome.channel_id}\n\n"
                f"但**监控尚未生效** —— {self._monitoring_blocker()}\n"
                "修好后即可正常推送，已保存的订阅会自动开始工作。"
            )
            return

        notice = self._degraded_notice()
        if notice:
            yield event.plain_result(
                f"已订阅频道: {outcome.name}\n"
                f"频道ID: {outcome.channel_id}\n"
                f"直播上/下播与新投稿将以{self._notification_style_text()}推送。\n\n"
                f"⚠️ {notice}"
            )
            return

        yield event.plain_result(
            f"已订阅频道: {outcome.name}\n"
            f"频道ID: {outcome.channel_id}\n"
            f"直播上/下播与新投稿将以{self._notification_style_text()}推送。"
        )

    @filter.command("yt批量订阅", alias={"yt_batch_subscribe", "youtube批量订阅"})
    async def batch_subscribe(self, event: AstrMessageEvent, first_target: str = ""):
        """批量订阅 YouTube 频道，格式: /yt批量订阅 <目标1> <目标2> ...（空格分隔）"""
        targets = _split_after_command(event, _BATCH_SUBSCRIBE_NAMES, first_target)
        if not targets:
            yield event.plain_result(_BATCH_SUBSCRIBE_USAGE)
            return

        targets, deduped = _dedup_targets(targets)
        skipped = targets[BATCH_MAX_TARGETS:]
        targets = targets[:BATCH_MAX_TARGETS]

        session_id = event.unified_msg_origin
        results: list[tuple[str, SubscribeOutcome]] = []
        for raw in targets:
            results.append((raw, await self._subscribe_one(session_id, raw)))

        added = sum(1 for _, o in results if o.status == "added")
        # 订阅整体「成功」但监控废掉的情况必须提示，且只提示一次
        # （每个频道都提一遍会把批量回复淹掉）。判据与 /yt订阅 完全一致。
        if added and not self._monitoring_ready():
            logger.error(
                f"[YT] 批量订阅已保存但监控不会工作：{self._monitoring_blocker()}"
            )
            notice = (
                f"订阅已保存，但**监控尚未生效** —— {self._monitoring_blocker()}\n"
                "修好后即可正常推送，已保存的订阅会自动开始工作。"
            )
        else:
            notice = self._degraded_notice()

        logger.info(
            f"[YT] 批量订阅 session={session_id} 处理={len(results)} "
            f"去重={deduped} 超限未处理={len(skipped)} "
            f"新增={added} 已订阅={sum(1 for _, o in results if o.status == 'exists')} "
            f"失败={sum(1 for _, o in results if o.status == 'failed')}"
        )

        yield event.plain_result(
            "\n".join(
                _batch_subscribe_lines(
                    results, deduped=deduped, skipped=skipped, notice=notice
                )
            )
        )

    @filter.command("yt取消订阅", alias={"yt_unsubscribe", "youtube取消订阅"})
    async def unsubscribe(self, event: AstrMessageEvent, channel_id: str = ""):
        """取消订阅 YouTube 频道，格式: /yt取消订阅 <@handle 或 频道ID>"""
        raw = (channel_id or "").strip()
        if not raw:
            yield event.plain_result("用法: /yt取消订阅 <@handle 或 频道ID>")
            return

        outcome = await self._unsubscribe_one(event.unified_msg_origin, raw)
        if outcome.status == "missing":
            yield event.plain_result(f"本会话未订阅该频道: {raw}")
            return
        yield event.plain_result(f"已取消订阅: {outcome.channel_id}")

    @filter.command("yt批量取消订阅", alias={"yt_batch_unsubscribe", "youtube批量取消订阅"})
    async def batch_unsubscribe(self, event: AstrMessageEvent, first_target: str = ""):
        """批量取消订阅，格式: /yt批量取消订阅 <目标1> <目标2> ...（空格分隔）"""
        targets = _split_after_command(event, _BATCH_UNSUBSCRIBE_NAMES, first_target)
        if not targets:
            yield event.plain_result(_BATCH_UNSUBSCRIBE_USAGE)
            return

        targets, deduped = _dedup_targets(targets)
        skipped = targets[BATCH_MAX_TARGETS:]
        targets = targets[:BATCH_MAX_TARGETS]

        session_id = event.unified_msg_origin
        results: list[tuple[str, UnsubscribeOutcome]] = []
        for raw in targets:
            results.append((raw, await self._unsubscribe_one(session_id, raw)))

        logger.info(
            f"[YT] 批量取消订阅 session={session_id} 处理={len(results)} "
            f"去重={deduped} 超限未处理={len(skipped)} "
            f"已取消={sum(1 for _, o in results if o.status == 'removed')} "
            f"未订阅={sum(1 for _, o in results if o.status == 'missing')}"
        )

        yield event.plain_result(
            "\n".join(
                _batch_unsubscribe_lines(results, deduped=deduped, skipped=skipped)
            )
        )

    @filter.command("yt列表", alias={"yt_list", "youtube列表"})
    async def list_subscriptions(self, event: AstrMessageEvent):
        """查看本会话已订阅的频道"""
        session_id = event.unified_msg_origin
        channels = self.store.get_session_channels(session_id)
        if not channels:
            yield event.plain_result("本会话暂无订阅。\n用 /yt订阅 @handle 添加。")
            return

        lines = [f"本会话已订阅 {len(channels)} 个频道:"]
        for idx, (channel_id, meta) in enumerate(channels.items(), 1):
            state = self.store.get_channel_state(channel_id)
            name = (state.channel_name if state else "") or meta.get("channel_name") or ""
            handle = (state.channel_handle if state else "") or ""
            status = _status_text(state)
            lines.append(f"{idx}. {name or channel_id} {handle}".rstrip())
            lines.append(f"    ID: {channel_id}｜{status}")
            # 降级必须可见（否则用户不知道数据来自网页兜底）
            degraded = self.notifier.degraded_reason(channel_id) if self.notifier else ""
            if degraded:
                lines.append(f"    ⚠️ 数据源已降级: {degraded}")
            elif self._runtime_degraded:
                lines.append(f"    ⚠️ 数据源已降级: {self._runtime_degraded}")

        notice = self._degraded_notice()
        if notice:
            lines.append("")
            lines.append(f"⚠️ {notice}")

        font_notice = self._font_notice()
        if font_notice:
            lines.append("")
            lines.append(f"⚠️ {font_notice}")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------ 测试指令

    @filter.command("yt直播测试", alias={"yt_live_test", "youtube直播测试"})
    async def live_test(self, event: AstrMessageEvent, target: str = ""):
        """抓取目标当前直播并推送一条测试通知（测试生成与推送链路）

        用法: /yt直播测试 <@handle 或 频道ID 或 频道URL 或 视频URL>
        """
        yield event.plain_result(
            await self._run_test(event, target, want_live=True)
        )

    @filter.command("yt视频测试", alias={"yt_video_test", "youtube视频测试"})
    async def video_test(self, event: AstrMessageEvent, target: str = ""):
        """抓取目标最新视频并推送一条测试通知（测试生成与推送链路）

        用法: /yt视频测试 <@handle 或 频道ID 或 频道URL 或 视频URL>
        """
        yield event.plain_result(
            await self._run_test(event, target, want_live=False)
        )

    _TEST_USAGE = (
        "用法: {cmd} <@handle 或 频道ID 或 频道URL 或 视频URL>\n"
        "例如:\n"
        "  {cmd} @NASA\n"
        "  {cmd} https://www.youtube.com/@MrBeast\n"
        "  {cmd} https://www.youtube.com/watch?v=gTKS8SAwUzE\n"
        "说明: 测试命令会真实抓取数据并按当前通知样式推送一条通知"
        "（带「🧪 测试」标记），用于验证内容生成与推送链路是否正常。"
    )

    async def _run_test(self, event: AstrMessageEvent, target: str, *, want_live: bool) -> str:
        """测试命令公共实现，返回给用户的文字说明。"""
        cmd = "/yt直播测试" if want_live else "/yt视频测试"
        raw = (target or "").strip()
        if not raw:
            return self._TEST_USAGE.format(cmd=cmd)

        if self.notifier is None or self.page_json is None:
            return "插件尚未初始化完成（或初始化失败），请稍后重试或查看日志。"

        kind, value = parse_target(raw)
        if not value:
            return f"无法识别的目标: {raw}\n\n" + self._TEST_USAGE.format(cmd=cmd)

        session = event.unified_msg_origin
        if kind == "video":
            return await self._test_from_video(session, value, want_live=want_live)
        return await self._test_from_channel(session, raw, want_live=want_live)

    async def _test_from_video(self, session: str, video_id: str, *, want_live: bool) -> str:
        """按视频 URL 生成测试通知（走观看页 ytInitialData）。"""
        data = await self.page_json.fetch_video(video_id, respect_throttle=False)
        if data is None:
            return (
                f"抓取视频失败: {video_id}\n"
                "可能原因：网络/代理不通、视频不可访问，或页面结构变化。请查看日志。"
            )

        entry = data.entry
        note = ""
        if want_live and not data.is_live_now:
            note = (
                "\n⚠️ 注意：该视频**当前不在直播**，这条通知是借用它的信息生成的样例"
                "（真实推送只会在检测到直播时发出）。"
            )
        elif not want_live and data.is_live_now:
            note = "\n⚠️ 注意：该视频**正在直播**，新投稿通知仅作样式效果展示。"

        notification = Notification(
            type=TYPE_LIVE_START if want_live else TYPE_NEW_VIDEO,
            title=entry.title,
            channel_id=entry.channel_id,
            channel_name=entry.channel_name,
            thumbnail_url=entry.thumbnail_url,
            start_time=entry.actual_start_time if want_live else entry.published_at,
            url=entry.url,
            video_id=entry.video_id,
        )
        return await self._push_test(
            session, notification, source=f"视频 {video_id}", note=note
        )

    async def _test_from_channel(self, session: str, raw: str, *, want_live: bool) -> str:
        """按频道标识生成测试通知（自动挑选直播或最新投稿）。"""
        try:
            meta, _ = await self._resolve_channel(raw)
        except ChannelNotFoundError:
            return f"未找到频道: {raw}\n请确认 handle / 频道ID / 链接是否正确"
        except QuotaExceededError:
            return "YouTube API 配额已耗尽，且网页兜底也没取到数据，请稍后重试"
        except InvalidApiKeyError:
            return "YouTube API Key 无效，请检查插件配置 basic.api_key"
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[YT] 测试命令解析频道失败 {raw}: {exc!r}")
            return "解析频道失败，请稍后重试或查看日志"

        snapshot = await self._fetch_test_snapshot(meta)
        if snapshot is None or not snapshot.entries:
            return (
                f"未取到频道 {meta.title or raw} 的视频数据。\n"
                "可能原因：网络/代理不通、频道无公开视频，或页面结构变化。请查看日志。"
            )

        entries = snapshot.entries
        channel_name = snapshot.channel_name or meta.title
        note = ""

        if want_live:
            chosen = snapshot.find_live()
            if chosen is None:
                # 没在直播：也没法可靠找到「最近一场直播」——
                # 网页数据里 /streams 只保留直播，往期直播存档与普通投稿
                # 无法区分（角标一样，见 services/page_json.py）。
                # 如实说明：这条通知只是借用最新投稿验证通知链路。
                chosen = entries[0]
                note = (
                    "\n⚠️ 该频道**当前没有直播**。网页数据无法可靠找出往期直播，"
                    f"这条通知借用最新投稿（{chosen.title[:30]}）的信息生成，"
                    "仅用于验证通知效果。"
                )
            start_time = chosen.actual_start_time or chosen.published_at
        else:
            regular = [e for e in entries if not e.live_state] or entries
            chosen = regular[0]
            start_time = chosen.published_at
            if chosen.was_live:
                note = "\n⚠️ 该频道最近只有直播内容，这条通知借用直播存档生成。"

        notification = Notification(
            type=TYPE_LIVE_START if want_live else TYPE_NEW_VIDEO,
            title=chosen.title,
            channel_id=meta.channel_id,
            channel_name=chosen.channel_name or channel_name,
            thumbnail_url=chosen.thumbnail_url,
            start_time=start_time,
            url=chosen.url,
            video_id=chosen.video_id,
        )
        return await self._push_test(
            session, notification, source=f"频道 {channel_name or meta.channel_id}", note=note
        )

    async def _fetch_test_snapshot(self, meta: ChannelMeta) -> Optional[FeedResult]:
        """测试命令取快照：Data API 优先（数据准确），失败/无 Key 时用网页兜底。

        网页兜底在这里很划算 —— 测试命令不消耗 API 配额，没配 Key 也能用。
        """
        max_results = max(10, (self.notifier.max_results if self.notifier else 5))
        if self.data_api is not None and self.data_api.configured:
            try:
                return await self.data_api.fetch_snapshot(
                    meta.channel_id,
                    meta.uploads_playlist_id,
                    max_results=max_results,
                    channel_name=meta.title,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[YT] 测试命令 Data API 取快照失败，改用网页兜底: {exc!r}")
        if self.page_json is not None:
            return await self.page_json.fetch_snapshot(
                meta.channel_id,
                meta.handle or meta.channel_id,
                max_results=max_results,
                channel_name=meta.title,
                respect_throttle=False,  # 交互式命令：要当前真实数据
            )
        return None

    async def _push_test(
        self, session: str, notification: Notification, *, source: str, note: str = ""
    ) -> str:
        """生成并推送测试通知，返回给用户的文字说明。"""
        outcome = await self.notifier.dispatch_test(session, notification)
        style = self._notification_style_text()

        header = (
            f"🧪 测试通知已推送（{source}）\n"
            f"标题: {notification.title or '（无标题）'}\n"
            f"频道: {notification.channel_name or notification.channel_id or '未知'}\n"
            f"链接: {notification.url}\n"
            f"内容生成与真实推送完全一致，测试通知额外带有测试标记。"
            f"当前样式：{style}。{note}"
        )

        if outcome.delivered:
            return header

        if outcome.uncertain:
            # 适配器超时但消息通常已送达（NapCat 的已知现象）。
            # 别让用户以为失败了 —— 他明明收到了图。
            return (
                f"🧪 测试通知已发送（{source}），但适配器上报了超时：\n"
                f"  {outcome.error}\n"
                "这通常是 NapCat/QQ 适配器的已知现象：消息**实际已经送达**，"
                "只是适配器等「消息列表更新」事件超时。请先确认上方是否已收到通知；\n"
                "插件不会因此重试（重试会导致重复推送）。\n\n"
                + header.split("\n", 1)[1]
            )

        if outcome.rendered:
            return (
                f"通知内容已生成成功，但推送到本会话失败（{source}）。\n"
                f"原因: {outcome.error}\n"
                "内容生成链路是好的，问题在会话/适配器侧（机器人是否在线、"
                "会话是否有效、是否有发言权限）。"
            )
        if self.notifier.notify_style == "image":
            return (
                f"通知图片生成失败（{source}）。\n"
                f"原因: {outcome.error}\n"
                "图片生成失败通常是字体缺失或配置问题，可运行 "
                "`python scripts/diagnose.py --check-fonts` 自查。"
            )
        return (
            f"文字通知生成失败（{source}）。\n"
            f"原因: {outcome.error}\n"
            "请检查日志中的文字格式化异常。"
        )

    def _notification_style_text(self) -> str:
        """当前通知样式的用户可见名称。"""
        return "纯文字通知" if self.notifier.notify_style == "text" else "图片通知"

    # ------------------------------------------------------------ 订阅公共实现

    async def _subscribe_one(self, session_id: str, raw: str) -> SubscribeOutcome:
        """订阅单个频道：解析 → 落库 → 静默播种 →（可选）通知 hub。

        `/yt订阅` 与 `/yt批量订阅` 共用。这里只负责「一个频道」这件事，
        数据源就绪/降级的提示由调用方统一渲染 —— 批量时那些话每个频道都一样，
        逐条重复只会把回复淹掉。
        """
        kind, value = parse_channel_input(raw)
        if not value:
            return SubscribeOutcome("failed", reason="empty")

        # 输入本身就是频道 ID 且本会话已订阅 → 不必查 API。
        # 批量重发同一份 ID 列表时，这一条能省下每个频道 1 单位配额
        # （原有实现在这种情况下也是先花配额、再回一句「已订阅」）。
        if kind == "id" and self.store.has_subscription(session_id, value):
            state = self.store.get_channel_state(value)
            return SubscribeOutcome(
                "exists",
                value,
                (state.channel_name if state else "") or value,
                (state.channel_handle if state else ""),
            )

        try:
            meta, from_api = await self._resolve_channel(raw)
        except ChannelNotFoundError:
            return SubscribeOutcome("failed", reason="not_found")
        except QuotaExceededError:
            return SubscribeOutcome("failed", reason="quota")
        except InvalidApiKeyError:
            return SubscribeOutcome("failed", reason="bad_key")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[YT] 订阅时解析频道失败 {raw}: {exc!r}")
            return SubscribeOutcome("failed", reason="error")

        resolved_id = meta.channel_id
        if not resolved_id:
            return SubscribeOutcome("failed", reason="no_id")

        display_name = meta.title or resolved_id
        if self.store.has_subscription(session_id, resolved_id):
            state = self.store.get_channel_state(resolved_id)
            return SubscribeOutcome(
                "exists",
                resolved_id,
                display_name,
                (state.channel_handle if state else "") or meta.handle,
            )

        self.store.add_subscription(session_id, resolved_id, meta.title)
        state = self.store.get_channel_state(resolved_id)
        if state is not None:
            if meta.uploads_playlist_id:
                state.uploads_playlist_id = meta.uploads_playlist_id
            if meta.handle:
                state.channel_handle = meta.handle
            elif kind not in ("id",):
                state.channel_handle = f"@{value}"
            # 记录名字来源：抓页面拿到的可能是其它语言，之后要用 API 更正一次
            state.name_from_api = from_api

        # 静默播种：避免刚订阅就把历史视频/正在进行的直播刷给用户
        snapshot = await self._fetch_snapshot_quiet(resolved_id, meta)
        if state is not None and snapshot is not None:
            seed_channel_from_feed(state, snapshot)
        await self.store.save()

        if self.websub is not None:
            await self.websub.subscribe(resolved_id)

        logger.info(
            f"[YT] 新订阅 session={session_id} channel={resolved_id} "
            f"name={meta.title} input={raw}"
        )
        return SubscribeOutcome(
            "added",
            resolved_id,
            display_name,
            (state.channel_handle if state else "") or meta.handle,
        )

    async def _unsubscribe_one(self, session_id: str, raw: str) -> UnsubscribeOutcome:
        """取消本会话对单个频道的订阅。

        先在本地订阅里匹配（不花配额），匹配不到才查一次 API 把 handle
        换成频道 ID —— 与重构前 `/yt取消订阅` 的行为一致。批量取消时本地
        匹配是主力：40 个目标逐个查 API 就是 40 单位配额。
        """
        # 本会话一个订阅都没有时直接判定「未订阅」：结果与走 API 一致
        # （没有订阅就不可能订阅着这个频道），但省掉每个目标 1 单位配额。
        if not self.store.get_session_channels(session_id):
            return UnsubscribeOutcome("missing")

        target = self._match_subscription(session_id, raw)
        if target is None:
            try:
                meta = await self._resolve_channel(raw)
                if meta.channel_id and self.store.has_subscription(
                    session_id, meta.channel_id
                ):
                    target = meta.channel_id
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[YT] 取消订阅时解析频道失败 {raw}: {exc!r}")

        if target is None:
            return UnsubscribeOutcome("missing")

        state = self.store.get_channel_state(target)
        name = (state.channel_name if state else "") or ""
        self.store.remove_subscription(session_id, target)
        await self.store.save()

        # 若已无任何会话订阅该频道，向 hub 退订
        if self.websub is not None and not self.store.sessions_for_channel(target):
            await self.websub.unsubscribe(target)

        logger.info(f"[YT] 取消订阅 session={session_id} channel={target} input={raw}")
        return UnsubscribeOutcome("removed", target, name)

    def _match_subscription(self, session_id: str, raw: str) -> Optional[str]:
        """在本会话的订阅里找出 raw 指向的频道 id（没有则 None）。

        匹配优先级：频道 ID → @handle → 频道名。
        - 频道 ID 大小写敏感（真实 ID 形如 UC + 22 位 base64 字符串），精确比；
        - handle 在 YouTube 上大小写不敏感，归一化后比（`/yt取消订阅 @NASA`
          订阅时若存的是 `@nasa` 也要能取消掉）；
        - 频道名只在完全相等时才算命中 —— 用户常常直接抄名字过来。
        """
        channels = self.store.get_session_channels(session_id)
        if not channels:
            return None

        candidates = {raw}
        _, value = parse_channel_input(raw)
        if value:
            candidates.add(value)

        for cid in channels:
            if cid in candidates:
                return cid

        wanted_handle = _norm_handle(value or raw)
        if wanted_handle:
            for cid in channels:
                state = self.store.get_channel_state(cid)
                if state and _norm_handle(state.channel_handle) == wanted_handle:
                    return cid

        wanted_name = raw.strip().casefold()
        for cid in channels:
            state = self.store.get_channel_state(cid)
            if state and state.channel_name.strip().casefold() == wanted_name:
                return cid
        return None

    # ------------------------------------------------------------ 数据源就绪检查

    def _monitoring_ready(self) -> bool:
        """当前配置下监控是否真的能工作。"""
        return self._monitoring_blocker() == ""

    def _page_fallback_ready(self) -> bool:
        """网页兜底是否可用（决定没配 Key 时监控还能不能工作）。"""
        if self.page_json is None or self.notifier is None:
            return False
        return bool(self.notifier.page_fallback_enabled)

    def _monitoring_blocker(self) -> str:
        """返回阻碍监控的原因；一切就绪（含可用降级）返回空串。

        语义约定（别轻易改，测试锁定了这套矩阵）：

          livebroadcasts  需要 API Key + OAuth，缺一即阻塞
          data_api        用户**显式要求官方 API**：没 Key 且网页兜底也关了
                          → 阻塞；网页兜底可用 → 不算阻塞，但由
                          `_degraded_notice()` 提示正在降级运行
          auto / feed     语义就是「尽力而为」，永不阻塞；实际走哪条链路
                          由 `_degraded_notice()` 如实说明

        「不阻塞」不等于「不提示」：能工作但降级时必须让用户知道，
        否则就是 CLAUDE.md 禁止的静默降级。
        """
        mode = self.notifier.live_detect_mode if self.notifier else "data_api"
        api_ok = bool(self.data_api and self.data_api.configured)

        if mode == "livebroadcasts":
            if not api_ok:
                return "livebroadcasts 模式需要 Data API Key 用于拉取投稿（basic.api_key 为空）"
            if not self._oauth_configured():
                return "livebroadcasts 模式需要配置 OAuth（oauth.client_id/secret/refresh_token）"
            return ""

        if mode in ("auto", "feed"):
            return ""

        # mode == data_api
        if api_ok or self._page_fallback_ready():
            return ""
        return (
            "未配置 YouTube Data API Key（配置项 basic.api_key），"
            "且网页兜底已被关闭（basic.page_fallback_enabled=false）。"
            "请到 Google Cloud 免费申请 API Key 后填入并重载插件"
        )

    def _degraded_notice(self) -> str:
        """降级运行说明；未降级返回空串。

        必须与 notifier 的实际降级链一致，否则就成了骗人的提示。
        运行期降级（如 Key 无效）优先于配置层面的判断 ——
        「配了 Key」不等于「Key 能用」。
        """
        if self.notifier is None:
            return ""
        if self._runtime_degraded:
            return (
                f"数据源已降级：{self._runtime_degraded}。"
                "已自动改用网页 JSON 兜底继续监控（数据可用但不够精确）。"
                "请检查 basic.api_key 配置"
            )
        mode = self.notifier.live_detect_mode
        if mode == "feed":
            return "当前使用 legacy Atom feed（该端点已不可靠，建议改用 Data API）"
        if mode == "livebroadcasts":
            return ""
        if self.data_api is not None and self.data_api.configured:
            return ""
        if self._page_fallback_ready():
            return (
                "当前**未配置 Data API Key**，监控走网页 JSON 兜底：实测可用，"
                "但比官方 API 脆弱 —— 没有精确时间戳、直播时长不准确，"
                "且每次检查抓 2 个页面（约 2.4MB）。建议配置 basic.api_key"
            )
        return (
            "当前**未配置 Data API Key**，且网页兜底已关闭，监控将回退到"
            "自 2025 年底起大面积 404 的 legacy Atom feed —— 大概率不会工作。"
            "请配置 basic.api_key，或打开 basic.page_fallback_enabled"
        )

    def _font_notice(self) -> str:
        """中文渲染能力异常时给出提示；正常返回空串。

        必须能出现在聊天里（不只日志）：缺中文字体的表现是「图里全是方框」，
        用户看不出原因，只会以为插件坏了。
        """
        if (
            self.notifier is not None
            and self.notifier.notify_style == "text"
        ) or self.renderer is None or getattr(self.renderer, "cjk_ok", True):
            return ""
        fonts_dir = self.data_dir / "fonts"
        return (
            "服务器（或容器）里缺少中文字体，通知图中的中文会显示为方框。任选一种：\n"
            "  ① Debian/Ubuntu: apt-get install -y fonts-noto-cjk\n"
            "  ② 把任意中文字体文件放进这个目录，然后重载插件（Docker 下同样有效，"
            f"该目录已被挂载）：{fonts_dir}\n"
            "  ③ 在配置项 render.font_path 填入字体文件的绝对路径"
            "（注意：Docker 部署要填**容器内**的路径，宿主机路径容器看不到）"
        )

    def _oauth_configured(self) -> bool:
        oauth_cfg = (self.config.get("oauth", {}) or {})
        return all(
            str(oauth_cfg.get(k, "") or "").strip()
            for k in ("client_id", "client_secret", "refresh_token")
        )

    # ------------------------------------------------------------ 频道解析辅助

    async def _resolve_channel(self, raw: str):
        """解析频道标识：Data API 优先，失败/无 Key 时抓频道页兜底。

        Returns:
            (ChannelMeta, from_api) —— from_api 表示名称是否来自官方 API。
        """
        if self.data_api is not None and self.data_api.configured:
            try:
                meta = await self.data_api.resolve_channel(raw)
                self._clear_runtime_degraded()
                return meta, True
            except ChannelNotFoundError:
                # 官方明确回答「没有这个频道」，不必再抓页面
                raise
            except QuotaExceededError as exc:
                logger.warning(f"[YT] Data API 配额耗尽，改用网页兜底: {exc}")
                self._note_runtime_degraded(_degrade_reason(exc))
            except InvalidApiKeyError as exc:
                logger.error(f"[YT] Data API Key 无效，改用网页兜底: {exc}")
                self._note_runtime_degraded(_degrade_reason(exc))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[YT] Data API 请求失败，改用网页兜底: {exc!r}")
                self._note_runtime_degraded(_degrade_reason(exc))

        meta = await self._resolve_channel_via_page(raw)
        if meta is None:
            raise ChannelNotFoundError(f"无法解析频道: {raw}")
        return meta, False

    # ------------------------------------------------------------ 运行期降级痕迹

    def _note_runtime_degraded(self, reason: str) -> None:
        """记录「真实调用暴露出的」降级原因，供订阅回复与 /yt列表 展示。"""
        if self._runtime_degraded != reason:
            logger.warning(f"[YT] 数据源运行期降级: {reason}")
        self._runtime_degraded = reason

    def _clear_runtime_degraded(self) -> None:
        if self._runtime_degraded:
            logger.info("[YT] Data API 已恢复可用，清除降级状态")
        self._runtime_degraded = ""

    async def _resolve_channel_via_page(self, raw: str):
        """抓频道页解析频道标识（网页 JSON 客户端优先，其自带节流）。"""
        if self.page_json is not None:
            logger.warning(
                f"[YT] 使用网页抓取解析频道（非官方 API，脆弱）: {raw}"
            )
            # 交互式命令不参与节流：否则连跑两次同一频道会被误报「未找到频道」
            return await self.page_json.resolve_channel(
                raw, respect_throttle=False
            )

        from .services.scrape import resolve_channel_via_html

        logger.warning(
            f"[YT] 使用网页抓取解析频道（非官方 API，脆弱）: {raw}"
        )
        return await resolve_channel_via_html(
            self._session, raw, proxy=self._proxy or None
        )

    async def _fetch_snapshot_quiet(self, channel_id: str, meta):
        """订阅时静默拉一次快照用于播种，失败不影响订阅本身。

        播种同样要有降级链：否则配额耗尽时订阅「成功」却播种不到状态，
        频道会以「首次接入」的静默语义进入监控 —— 第一条视频被吞掉。
        """
        if self.data_api is not None and self.data_api.configured and meta.uploads_playlist_id:
            try:
                return await self.data_api.fetch_snapshot(
                    channel_id,
                    meta.uploads_playlist_id,
                    max_results=self.notifier.max_results,
                    channel_name=meta.title,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"[YT] 订阅播种 Data API 失败，改用网页兜底 channel={channel_id}: {exc!r}"
                )
                if not isinstance(exc, ChannelNotFoundError):
                    self._note_runtime_degraded(_degrade_reason(exc))
        if self.page_json is not None:
            try:
                return await self.page_json.fetch_snapshot(
                    channel_id,
                    meta.handle or channel_id,
                    max_results=self.notifier.max_results,
                    channel_name=meta.title,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[YT] 订阅播种网页兜底失败 channel={channel_id}: {exc!r}")
        return None


def _split_after_command(
    event: AstrMessageEvent, names: frozenset[str], fallback: str = ""
) -> list[str]:
    """取出「命令名之后」的全部参数，按空白切分（批量命令用）。

    为什么不能靠类型注解拿参数：AstrBot 的指令参数是逐 token 绑定的 ——
    `core/star/filter/command.py` 把消息按空格 split 后**按位置**赋给形参，
    所以 `x: str = ""` 这样的注解只能拿到第一个 token，第二个之后全被丢掉。
    批量命令必须自己从原始消息里取。框架在 waking_check 阶段已经把唤醒前缀
    从 message_str 上剥掉了，所以这里有 `message_str == "<命令名> <参数...>"`。

    为什么不用框架的 `GreedyStr`：它是 `astrbot.core.star.filter.command` 里的
    内部类型，插件一旦 import 它，就等于把「整个插件能不能加载」押在框架内部
    结构不变上 —— 这里手动剥一个 token 的代价小得多。

    首个 token 不是本命令的任何名字时**不猜**：宁可回一条用法说明，
    也不能把用户敲的 @b 当成参数静默订阅下去（那是不可逆的错误订阅）。
    """
    text = (event.get_message_str() or "").strip()
    # 用 split(None, 1) 而不是 partition(" ")：框架是用 `re.sub(r"\s+", " ")`
    # 归一化之后才判定命令匹配的，所以制表符、全角空格、换行都可能出现在
    # 命令名后面（那是 str 的 Unicode 空白，None 分隔符一并处理）。
    parts = text.split(None, 1)
    head = parts[0] if parts else ""
    if head not in names:
        logger.warning(
            f"[YT] 批量命令参数解析异常：消息首 token {head!r} 不在预期命令名内，"
            f"已退回类型注解解析（可能只处理第一个目标）"
        )
        return [fallback] if fallback else []
    return parts[1].split() if len(parts) > 1 else []


def _dedup_targets(targets: list[str]) -> tuple[list[str], int]:
    """按字面去重（保持顺序），返回 (去重后的列表, 去掉的个数)。

    只做字面去重：@handle 在 YouTube 上大小写不敏感，但频道 ID 是大小写
    敏感的，统一转小写去重会把两个合法但不同的频道 ID 误并成一个。
    真正的重复（本会话已订阅）由 `_subscribe_one` 里的 has_subscription 兜住。
    """
    seen: set[str] = set()
    unique: list[str] = []
    for target in targets:
        if target in seen:
            continue
        seen.add(target)
        unique.append(target)
    return unique, len(targets) - len(unique)


def _norm_handle(value: str) -> str:
    """归一化 handle / 频道名用于匹配：去 @ 前缀与空白，统一小写。"""
    return (value or "").strip().lstrip("@").casefold()


def _channel_label(name: str, handle: str = "") -> str:
    """拼「名字 (@handle)」；两者本来就是同一个词时不重复显示。"""
    name = (name or "").strip()
    handle = (handle or "").strip()
    if name and handle and _norm_handle(handle) != _norm_handle(name):
        return f"{name} ({handle})"
    return name or handle


def _batch_notes(deduped: int, skipped: Optional[list[str]]) -> list[str]:
    """批量回复末尾的说明行：去重提示 + 超出上限未处理的提示。

    超限的部分必须逐条说明（这里最多列出 10 个，剩下的给个数），
    不能让用户以为「命令说新增 3 个」=「我贴的 30 个都办好了」。
    """
    notes: list[str] = []
    if deduped:
        notes.append(f"（输入里有 {deduped} 个完全相同的目标，已自动去重）")
    if skipped:
        shown = " ".join(skipped[:10])
        more = f" …（其余 {len(skipped) - 10} 个未列出）" if len(skipped) > 10 else ""
        notes.append(
            f"⚠️ 单条消息最多处理 {BATCH_MAX_TARGETS} 个目标，"
            f"以下 {len(skipped)} 个本次未处理: {shown}{more}"
        )
    return notes


def _batch_subscribe_lines(
    results: list[tuple[str, SubscribeOutcome]],
    *,
    deduped: int = 0,
    skipped: Optional[list[str]] = None,
    notice: str = "",
) -> list[str]:
    """渲染 /yt批量订阅 的回复（纯函数，便于离线单测）。"""
    added = [o for _, o in results if o.status == "added"]
    exists = [o for _, o in results if o.status == "exists"]
    failed = [(raw, o) for raw, o in results if o.status == "failed"]

    lines = [
        f"批量订阅完成：新增 {len(added)} 个 / 已订阅 {len(exists)} 个 / "
        f"失败 {len(failed)} 个"
    ]
    for o in added:
        lines.append(f"✅ {_channel_label(o.name, o.handle)} — {o.channel_id}")
    for o in exists:
        lines.append(f"⏭️ 已订阅: {_channel_label(o.name, o.handle)}")
    for raw, o in failed:
        reason = _SUBSCRIBE_FAIL_SHORT.get(o.reason, "订阅失败")
        lines.append(f"❌ {raw} — {reason}")
    lines.extend(_batch_notes(deduped, skipped))
    if notice:
        lines.append("")
        lines.append(f"⚠️ {notice}")
    return lines


def _batch_unsubscribe_lines(
    results: list[tuple[str, UnsubscribeOutcome]],
    *,
    deduped: int = 0,
    skipped: Optional[list[str]] = None,
) -> list[str]:
    """渲染 /yt批量取消订阅 的回复（纯函数，便于离线单测）。"""
    removed = [o for _, o in results if o.status == "removed"]
    missing = [raw for raw, o in results if o.status == "missing"]

    lines = [
        f"批量取消订阅完成：已取消 {len(removed)} 个 / 未订阅 {len(missing)} 个"
    ]
    for o in removed:
        label = _channel_label(o.name)
        prefix = f"{label} — " if label else ""
        lines.append(f"✅ 已取消订阅: {prefix}{o.channel_id}")
    for raw in missing:
        lines.append(f"❌ 未订阅: {raw}")
    lines.extend(_batch_notes(deduped, skipped))
    return lines


def _cfg_int(cfg: dict, key: str, default: int) -> int:
    """读整数配置项；只在「缺失 / 空」时回退默认值。

    ⚠️ 不能用 `cfg.get(key, default) or default`：`0 or default` 会得到
    default，于是**配置里的 0 被静默吞掉**。而 0 在本项目里是有意义的
    （cleanup.retention_days=0 表示不按天数删、max_total_mb=0 表示不限制、
    hour=0 表示午夜清理），实测踩过：填 0 结果按 7 天执行。
    """
    value = cfg.get(key)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning(f"[YT] 配置项 {key}={value!r} 不是整数，回退默认值 {default}")
        return default


def _degrade_reason(exc: BaseException) -> str:
    """把 Data API 异常映射为**给用户看**的简短降级原因。

    不要把异常 repr 拼进提示：`InvalidApiKeyError('API Key 无效: API key
    not valid. Please pass a valid API key.')` 这种文案对用户毫无价值。
    细节留在日志里。
    """
    if isinstance(exc, QuotaExceededError):
        return "Data API 配额耗尽"
    if isinstance(exc, InvalidApiKeyError):
        return "Data API Key 无效"
    if isinstance(exc, ApiKeyMissingError):
        return "未配置 Data API Key"
    return "Data API 不可用"


def _status_text(state) -> str:
    """频道当前状态的简短描述。

    注意区分「尚未检测」与「已同步但确实没有投稿/直播」：
    主播型频道的上传列表全是直播存档，没有普通投稿，
    若笼统显示「暂无记录」会让人误以为插件没在工作。
    """
    if state is None:
        return "状态未知"
    if state.last_status == "live":
        return "🔴 直播中"
    if state.last_status == "ended":
        when = format_time_zh(state.last_live_end_at)
        title = state.last_live_title
        if len(title) > 18:
            title = title[:18] + "…"
        suffix = f"（{when}）" if when else ""
        return f"⚫ 上次直播已结束{suffix}" + (f": {title}" if title else "")
    if state.latest_video_title:
        title = state.latest_video_title
        if len(title) > 20:
            title = title[:20] + "…"
        return f"最近投稿: {title}"
    if state.video_seeded:
        return "✅ 已同步（暂无投稿或直播）"
    return "⏳ 尚未完成首次检测"
