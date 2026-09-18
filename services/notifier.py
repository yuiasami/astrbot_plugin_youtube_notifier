"""通知服务：单频道检查 → 状态机/新投稿检测 → 渲染图片 → 推送给所有订阅会话。

数据源优先级（live_detect_mode）：
  data_api      默认。官方 Data API v3（仅需 API Key），稳定，支持 @handle。
  livebroadcasts  LiveBroadcasts API（OAuth）查直播 + Data API 查投稿；仅自己的频道。
  feed           legacy Atom feed（⚠️ 端点已不可靠，见 services/feed.py）。
  auto           优先 data_api，未配置 Key 或失败时回退网页 JSON / feed。

降级链（page_fallback_enabled=true 时生效）：
  Data API 配额耗尽 / 请求失败 / 未配置 Key
      → 网页 JSON（services/page_json.py，实测可用）⭐
      → legacy Atom feed（仅在网页也失败时，端点已大面积 404）

降级不是静默的：每次降级都打日志，并记在 `degraded_reason()` 里供 /yt列表 展示。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import MessageChain

from .data_api import (
    ApiKeyMissingError,
    InvalidApiKeyError,
    QuotaExceededError,
    YouTubeDataAPI,
)
from .feed import LegacyFeedClient
from .livebroadcasts import LiveBroadcastsClient
from .models import (
    ChannelState,
    FeedResult,
    LiveInfo,
    Notification,
    TYPE_LIVE_END,
    TYPE_LIVE_START,
    TYPE_NEW_VIDEO,
)
from .page_json import ChannelPageClient
from .state_machine import find_new_videos, process_live
from ..utils import format_duration, format_time_zh, utc_now_iso

_DEFAULT_ENABLED = {
    TYPE_LIVE_START: True,
    TYPE_LIVE_END: True,
    TYPE_NEW_VIDEO: True,
}

# 适配器「上报超时、但消息其实已经送达」的特征。
#
# 实测（NapCat / QQ NT，2026-09）：sendMsg 会抛
#   ActionFailed(status='failed', retcode=1200,
#                message='Timeout: NTEvent serviceAndMethod:NodeIKernelMsgService/
#                         sendMsg ListenerName:NodeIKernelMsgListener/
#                         onMsgInfoListUpdate ...')
# 也就是「等消息列表更新事件超时」，而图片**已经发出去了**。
# 此时若报「推送失败」并重试，用户会收到重复消息 —— 所以必须识别出来，
# 如实说「适配器超时、可能已送达」，既不假装成功也不误报失败。
#
# 用字符串特征判断而不是 import aiocqhttp.exceptions.ActionFailed：
# 插件不该依赖具体适配器的实现，其它适配器的同类超时也应被同样对待。
_UNCERTAIN_SEND_HINTS = (
    "timeout",
    "timed out",
    "ntevent",
    "onmsginfolistupdate",
)


@dataclass
class SendOutcome:
    """一次通知推送的结果。

    区分三态而不是「成功/失败」二元：
      delivered=True             确认送达
      uncertain=True             适配器报错但极可能已送达（超时类，不应重试）
      delivered=uncertain=False  确实失败
    """

    image_path: Optional[str] = None
    rendered: bool = False
    delivered: bool = False
    uncertain: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        """对用户而言算不算成功（不确定时按成功对待，避免误导去排查）。"""
        return self.delivered or self.uncertain

    def describe(self) -> str:
        if self.delivered:
            return "已送达"
        if self.uncertain:
            return f"适配器上报超时、消息可能已送达（{self.error}）"
        return f"推送失败：{self.error or '未知原因'}"


def classify_send_error(exc: BaseException) -> tuple[bool, str]:
    """把推送异常分类为 (是否可能已送达, 给用户看的简短原因)。

    Returns:
        (True, 原因)  —— 超时类，消息很可能已经发出，**不要重试**
        (False, 原因) —— 确认失败（如机器人离线、会话不存在、权限不足）
    """
    text = f"{exc}".strip()
    lowered = text.lower()
    reason = _short_error(text)

    if any(hint in lowered for hint in _UNCERTAIN_SEND_HINTS):
        return True, reason
    return False, reason


def _short_error(text: str, limit: int = 160) -> str:
    """把适配器异常压成一行可读文本（原始内容很长且含换行）。"""
    flat = " ".join(text.split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


class NotificationService:
    def __init__(
        self,
        context,
        store,
        renderer,
        *,
        data_api: Optional[YouTubeDataAPI] = None,
        legacy_feed: Optional[LegacyFeedClient] = None,
        live_broadcasts: Optional[LiveBroadcastsClient] = None,
        page_json: Optional[ChannelPageClient] = None,
        live_detect_mode: str = "data_api",
        page_fallback_enabled: bool = True,
        cover_download: bool = True,
        max_results: int = 5,
        image_dir: Path = Path("data/images"),
        enabled: Optional[dict] = None,
        notify_style: str = "image",
    ):
        self.context = context
        self.store = store
        self.renderer = renderer
        self.data_api = data_api
        self.legacy_feed = legacy_feed
        self.live_broadcasts = live_broadcasts
        self.page_json = page_json
        self.live_detect_mode = live_detect_mode
        self.page_fallback_enabled = bool(page_fallback_enabled)
        self.cover_download = cover_download
        self.max_results = max(1, min(50, int(max_results)))
        self.image_dir = Path(image_dir)
        if notify_style not in ("image", "text"):
            logger.warning(
                f"[YT] 未知通知样式 {notify_style!r}，已回落为 image"
            )
            notify_style = "image"
        self.notify_style = notify_style
        self._enabled = dict(_DEFAULT_ENABLED)
        if enabled:
            self._enabled.update(enabled)
        self._channel_locks: dict[str, asyncio.Lock] = {}
        self._api_key_warned = False
        # 适配器「超时但其实已送达」只告警一次，之后降 debug（见 dispatch）
        self._send_timeout_warned = False
        # 降级原因（channel_id → 原因），供 /yt列表 与订阅回复展示
        self._degraded: dict[str, str] = {}
        if not (data_api and data_api.configured):
            if self.page_fallback_enabled and page_json is not None:
                logger.warning(
                    "[YT] 未配置 YouTube Data API Key —— 监控将走网页 JSON 兜底"
                    "（可用但比官方 API 脆弱、无精确时间戳）。建议填写 api_key"
                )
            else:
                logger.warning(
                    "[YT] 未配置 YouTube Data API Key —— 将退化为不稳定的 legacy feed "
                    "数据源，强烈建议在配置中填写 api_key"
                )

    # ------------------------------------------------------------ 降级痕迹

    def degraded_reason(self, channel_id: str) -> str:
        """该频道当前是否在用降级数据源；正常返回空串。

        降级必须留痕（见 CLAUDE.md「不允许静默降级」）：用户要能知道
        现在看到的推送来自哪条链路。
        """
        return self._degraded.get(str(channel_id), "")

    def _mark_degraded(self, channel_id: str, reason: str) -> None:
        cid = str(channel_id)
        if self._degraded.get(cid) != reason:
            logger.info(f"[YT] channel={cid} 降级为网页数据源: {reason}")
        self._degraded[cid] = reason

    def _clear_degraded(self, channel_id: str) -> None:
        cid = str(channel_id)
        if self._degraded.pop(cid, None) is not None:
            logger.info(f"[YT] channel={cid} 已恢复使用 Data API")

    # ------------------------------------------------------------ 主入口

    async def check_channel(self, channel_id: str) -> None:
        """单频道单轮检查：直播状态机 + 新投稿检测 → 渲染并推送。

        单频道串行化，避免 poller 与 WebSub 回调并发修改同一状态。
        """
        lock = self._channel_locks.setdefault(channel_id, asyncio.Lock())
        async with lock:
            state = self.store.get_channel_state(channel_id)
            if state is None:
                return
            try:
                await self._check_channel_locked(state)
            finally:
                await self.store.save()

    async def process_feed_result(self, channel_id: str, feed: FeedResult) -> None:
        """对一份已获取的快照做状态机 + 新投稿检测 + 推送（供 WebSub 推送复用）。"""
        lock = self._channel_locks.setdefault(channel_id, asyncio.Lock())
        async with lock:
            state = self.store.get_channel_state(channel_id)
            if state is None:
                logger.debug(f"[YT] 收到未订阅频道 {channel_id} 的推送，忽略")
                return
            notifications = self._apply_snapshot(state, feed)
            try:
                await self._dispatch_filtered(channel_id, notifications)
            finally:
                await self.store.save()

    # ------------------------------------------------------------ 检查流程

    async def _check_channel_locked(self, state: ChannelState) -> None:
        use_api = self.live_detect_mode in ("data_api", "auto") and bool(
            self.data_api and self.data_api.configured
        )

        if self.live_detect_mode == "livebroadcasts":
            notifications = await self._check_via_livebroadcasts(state)
        elif self.live_detect_mode == "feed":
            # 显式 feed 模式：用户明确要求用 legacy feed，不参与降级链
            snapshot = await self._fetch_legacy_feed(state)
            notifications = self._apply_snapshot(state, snapshot) if snapshot else []
        elif use_api:
            snapshot = await self._fetch_snapshot(state)
            notifications = self._apply_snapshot(state, snapshot) if snapshot else []
        else:
            # data_api / auto 且未配置 Key → 直接走降级链
            if self.live_detect_mode == "auto":
                logger.info(
                    f"[YT] channel={state.channel_id} 未配置 API Key，回退网页数据源"
                )
            snapshot = await self._fetch_degraded_snapshot(
                state, "未配置 Data API Key"
            )
            notifications = self._apply_snapshot(state, snapshot) if snapshot else []

        await self._dispatch_filtered(state.channel_id, notifications)

    async def _fetch_snapshot(self, state: ChannelState) -> Optional[FeedResult]:
        """用 Data API 拉取频道快照；失败/配额耗尽时自动降级到网页 JSON。"""
        try:
            # 需要解析的情况：① 缺 uploads 播放列表；② 频道名来自抓页面
            # （可能是英文 og:title），拿到 API Key 后更正一次官方名称。
            if not state.uploads_playlist_id or not state.name_from_api:
                meta = await self.data_api.resolve_channel(state.channel_id)
                if meta.uploads_playlist_id:
                    state.uploads_playlist_id = meta.uploads_playlist_id
                if meta.title:
                    self.store.set_channel_name(state.channel_id, meta.title)
                    state.channel_name = meta.title
                state.name_from_api = True
                if not state.uploads_playlist_id:
                    logger.warning(
                        f"[YT] channel={state.channel_id} 未取到 uploads 播放列表"
                    )
                    return None
            snapshot = await self.data_api.fetch_snapshot(
                state.channel_id,
                state.uploads_playlist_id,
                max_results=self.max_results,
                channel_name=state.channel_name,
            )
            self._clear_degraded(state.channel_id)
            return snapshot
        except QuotaExceededError as exc:
            logger.error(
                f"[YT] Data API 配额耗尽 channel={state.channel_id}: {exc}"
            )
            return await self._fetch_degraded_snapshot(state, "Data API 配额耗尽")
        except InvalidApiKeyError as exc:
            logger.error(f"[YT] API Key 无效，请检查配置: {exc}")
            return await self._fetch_degraded_snapshot(state, "API Key 无效")
        except ApiKeyMissingError as exc:
            # 每轮都会走到这里，只报一次免得刷屏
            if not self._api_key_warned:
                self._api_key_warned = True
                logger.error(f"[YT] {exc} —— 改用网页兜底数据源")
            else:
                logger.debug(f"[YT] 仍未配置 API Key，跳过 channel={state.channel_id}")
            return await self._fetch_degraded_snapshot(state, "未配置 Data API Key")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[YT] channel={state.channel_id} Data API 拉取失败: {exc!r}"
            )
            return await self._fetch_degraded_snapshot(state, "Data API 请求失败")

    async def _fetch_degraded_snapshot(
        self, state: ChannelState, reason: str
    ) -> Optional[FeedResult]:
        """降级链：网页 JSON（优先，实测可用）→ legacy feed（最后手段）。

        网页 JSON 取代 legacy feed 作为首选兜底：feed 端点自 2025 年底起
        大面积 404（见 services/feed.py 与 CLAUDE.md），而网页 JSON 实测可用。
        """
        if not self.page_fallback_enabled:
            logger.debug(
                f"[YT] channel={state.channel_id} 网页兜底已关闭（{reason}）"
            )
            return await self._fetch_legacy_feed(state)

        snapshot = None
        if self.page_json is not None:
            snapshot = await self.page_json.fetch_snapshot(
                state.channel_id,
                state.channel_handle,
                max_results=self.max_results,
                channel_name=state.channel_name,
            )

        if snapshot is not None:
            self._mark_degraded(state.channel_id, f"{reason} → 网页兜底")
            # 网页给的频道名可能是官方语言，比抓页面兜底的名字可信
            if snapshot.channel_name:
                self.store.set_channel_name(state.channel_id, snapshot.channel_name)
                state.channel_name = snapshot.channel_name
            return snapshot

        logger.warning(
            f"[YT] channel={state.channel_id} 网页兜底失败（{reason}），"
            "继续回退 legacy feed"
        )
        feed = await self._fetch_legacy_feed(state)
        if feed is not None:
            self._mark_degraded(state.channel_id, f"{reason} → legacy feed（不可靠）")
        return feed

    async def _fetch_legacy_feed(self, state: ChannelState) -> Optional[FeedResult]:
        if self.legacy_feed is None:
            return None
        feed = await self.legacy_feed.fetch_feed(state.channel_id)
        if feed is not None and feed.channel_name:
            self.store.set_channel_name(state.channel_id, feed.channel_name)
            state.channel_name = feed.channel_name
        return feed

    async def _check_via_livebroadcasts(self, state: ChannelState) -> list[Notification]:
        """LiveBroadcasts(OAuth) 查直播 + Data API 查投稿（仅自己的频道）。"""
        now_iso = utc_now_iso()
        notifications: list[Notification] = []
        snapshot: Optional[FeedResult] = None

        if self.live_broadcasts is not None:
            try:
                lives = await self.live_broadcasts.fetch_live_broadcasts()
                mine = [
                    lv
                    for lv in lives
                    if not state.channel_id or lv.channel_id == state.channel_id
                ]
                live = mine[0] if mine else None
                if live:
                    live.channel_name = state.channel_name
                notifications.extend(process_live(live, state, now_iso))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"[YT] channel={state.channel_id} LiveBroadcasts 检测失败: {exc!r}"
                )

        # 投稿始终走 Data API（若可用），否则降级链
        if self.data_api and self.data_api.configured:
            snapshot = await self._fetch_snapshot(state)
        else:
            snapshot = await self._fetch_degraded_snapshot(state, "未配置 Data API Key")
        if snapshot is not None:
            notifications.extend(find_new_videos(snapshot.entries, state, now_iso))
        return notifications

    def _apply_snapshot(self, state: ChannelState, snapshot: FeedResult) -> list[Notification]:
        """对一份快照运行状态机与新投稿检测，返回通知列表。"""
        now_iso = utc_now_iso()
        if snapshot.channel_name:
            self.store.set_channel_name(state.channel_id, snapshot.channel_name)
            state.channel_name = snapshot.channel_name

        notifications: list[Notification] = []
        live_entry = snapshot.find_live()
        live: Optional[LiveInfo] = None
        if live_entry is not None:
            live = LiveInfo(
                live_id=live_entry.video_id,
                title=live_entry.title,
                channel_id=state.channel_id,
                channel_name=snapshot.channel_name or state.channel_name,
                thumbnail_url=live_entry.thumbnail_url,
                # 直播实际开始时间优先于发布时间
                start_time=live_entry.actual_start_time or live_entry.published_at,
                url=live_entry.url,
            )
        # 若上一次在直播、本轮已不在直播，尝试取该场的实际结束时间，算出准确时长
        ended_iso = ""
        if live is None and state.last_live_id:
            prev = next(
                (e for e in snapshot.entries if e.video_id == state.last_live_id), None
            )
            if prev is not None and prev.actual_end_time:
                ended_iso = prev.actual_end_time

        notifications.extend(
            process_live(live, state, now_iso, ended_at_iso=ended_iso)
        )
        notifications.extend(find_new_videos(snapshot.entries, state, now_iso))
        return notifications

    async def _dispatch_filtered(
        self, channel_id: str, notifications: list[Notification]
    ) -> None:
        if not notifications:
            return
        filtered = [n for n in notifications if self._enabled.get(n.type, True)]
        if not filtered:
            return
        await self.dispatch(channel_id, filtered)

    # ------------------------------------------------------------ 推送

    async def dispatch(self, channel_id: str, notifications: list[Notification]) -> None:
        sessions = self.store.sessions_for_channel(channel_id)
        if not sessions:
            logger.debug(f"[YT] channel={channel_id} 无订阅会话，跳过推送")
            return
        for n in notifications:
            if self.notify_style == "text":
                payload, generate_err = self._format_text_safe(n)
            else:
                payload, generate_err = await self._render_safe(n)
            if not payload:
                logger.warning(
                    f"[YT] 通知内容生成失败 type={n.type} video={n.video_id}: "
                    f"{generate_err}"
                )
                continue
            for session in sessions:
                if self.notify_style == "text":
                    outcome = await self._send_text(session, payload)
                else:
                    outcome = await self._send_image(session, payload)
                self._log_send_outcome(channel_id, session, n, outcome)

    def _log_send_outcome(
        self, channel_id: str, session: str, n: Notification, outcome: SendOutcome
    ) -> None:
        """统一记录图片/文字通知的三态发送结果。"""
        if outcome.delivered:
            logger.info(
                f"[YT] 已推送 {n.type} → session={session} "
                f"channel={channel_id} video={n.video_id or '-'}"
            )
        elif outcome.uncertain:
            # 常见于 NapCat：适配器超时但消息已送达。不重试（会重复推送），
            # 只首次告警，之后降为 debug —— 否则每条通知都刷一条 WARN。
            msg = (
                f"[YT] {n.type} 推送适配器上报超时，消息可能已送达，"
                f"已跳过重试 session={session} video={n.video_id or '-'}: "
                f"{outcome.error}"
            )
            if not self._send_timeout_warned:
                self._send_timeout_warned = True
                logger.warning(msg)
            else:
                logger.debug(msg)
        else:
            logger.warning(
                f"[YT] 推送失败 session={session} type={n.type}: {outcome.error}"
            )

    async def _render_safe(
        self, n: Notification, *, test: bool = False
    ) -> tuple[Optional[str], str]:
        """渲染通知图；失败返回 (None, 原因)。**绝不抛异常**。

        渲染异常必须在这里挡住：一旦冒泡出去会中断整个 dispatch 循环
        （同一通知的其余会话收不到），还会被上层记成「频道检查失败」，
        把真正的原因（字体/图片写入）掩盖掉。
        """
        try:
            path = await self._render_notification(n, test=test)
        except Exception as exc:  # noqa: BLE001 - 渲染失败只影响这一条通知
            logger.error(
                f"[YT] 渲染通知图异常 type={n.type} video={n.video_id}: {exc!r}"
            )
            return None, _short_error(f"{type(exc).__name__}: {exc}")
        if not path:
            return None, "渲染器未返回图片路径"
        return path, ""

    def _format_text(self, n: Notification) -> str:
        """把通知格式化为纯文字；标签与图片模板保持一致。"""
        channel = (n.channel_name or n.channel_id or "未知频道").strip()
        title = (n.title or "（无标题）").strip()
        if len(title) > 100:
            title = title[:100] + "…"
        start_time = format_time_zh(n.start_time)
        end_time = format_time_zh(n.end_time)
        url = (n.url or "").strip()

        if n.type == TYPE_LIVE_START:
            lines = [f"🔴 {channel} 开播了", f"标题: {title}"]
            if start_time:
                lines.append(f"开始: {start_time}")
        elif n.type == TYPE_LIVE_END:
            lines = [f"⚫ {channel} 下播了", f"标题: {title}"]
            if start_time:
                lines.append(f"开始: {start_time}")
            if end_time:
                lines.append(f"结束: {end_time}")
            if n.duration_seconds:
                lines.append(f"时长: {format_duration(n.duration_seconds)}")
        else:
            lines = [f"📺 {channel} 发布了新视频", f"标题: {title}"]
            if start_time:
                lines.append(f"开始: {start_time}")
        if url:
            lines.append(f"链接: {url}")
        return "\n".join(lines)

    def _format_text_safe(
        self, n: Notification, *, test: bool = False
    ) -> tuple[Optional[str], str]:
        """生成文字通知；失败返回 (None, 原因)，绝不向上抛异常。"""
        try:
            text = self._format_text(n)
        except Exception as exc:  # noqa: BLE001 - 单条格式化失败不影响其它通知
            logger.error(
                f"[YT] 格式化文字通知异常 type={n.type} video={n.video_id}: {exc!r}"
            )
            return None, _short_error(f"{type(exc).__name__}: {exc}")
        if not text.strip():
            return None, "文字通知内容为空"
        if test:
            text = f"🧪 [测试]\n{text}"
        return text, ""

    async def _send_text(self, session: str, text: str) -> SendOutcome:
        """发送纯文字通知并分类结果。"""
        try:
            await self.context.send_message(session, MessageChain().message(text))
        except Exception as exc:  # noqa: BLE001 - 适配器异常五花八门
            uncertain, reason = classify_send_error(exc)
            return SendOutcome(
                rendered=True,
                delivered=False,
                uncertain=uncertain,
                error=reason,
            )
        return SendOutcome(rendered=True, delivered=True)

    async def _send_image(self, session: str, image_path: str) -> SendOutcome:
        """发送一张图并分类结果（确认成功 / 超时但可能已送达 / 确认失败）。"""
        try:
            await self.context.send_message(
                session, MessageChain().file_image(image_path)
            )
        except Exception as exc:  # noqa: BLE001 - 适配器异常五花八门
            uncertain, reason = classify_send_error(exc)
            return SendOutcome(
                image_path=image_path,
                rendered=True,
                delivered=False,
                uncertain=uncertain,
                error=reason,
            )
        return SendOutcome(image_path=image_path, rendered=True, delivered=True)

    async def _render_notification(
        self, n: Notification, *, test: bool = False
    ) -> Optional[str]:
        data = n.to_dict()
        data["thumbnail_path"] = ""
        data["test"] = bool(test)
        if self.cover_download and n.thumbnail_url and self.data_api is not None:
            thumb_path = self.image_dir / f"{n.video_id or 'thumb'}.jpg"
            ok = await self.data_api.download_image(n.thumbnail_url, thumb_path)
            if ok and thumb_path.exists():
                data["thumbnail_path"] = str(thumb_path)
        return await self.renderer.render(data)

    # ------------------------------------------------------------ 测试通知

    async def dispatch_test(
        self, session: str, notification: Notification
    ) -> SendOutcome:
        """生成并推送一条测试通知，返回 SendOutcome。

        供 /yt直播测试 /yt视频测试 使用：走的是与真实推送**完全相同**的
        内容生成 + 发送链路，但额外标记为测试，且不触碰任何去重状态。
        """
        if self.notify_style == "text":
            payload, generate_err = self._format_text_safe(notification, test=True)
        else:
            payload, generate_err = await self._render_safe(notification, test=True)
        if not payload:
            logger.warning(
                f"[YT] 测试通知内容生成失败 type={notification.type} "
                f"video={notification.video_id}: {generate_err}"
            )
            return SendOutcome(error=f"通知内容生成失败：{generate_err}")

        if self.notify_style == "text":
            outcome = await self._send_text(session, payload)
        else:
            outcome = await self._send_image(session, payload)
        if outcome.delivered:
            logger.info(
                f"[YT] 已推送测试通知 {notification.type} → session={session} "
                f"video={notification.video_id or '-'}"
            )
        elif outcome.uncertain:
            logger.warning(
                f"[YT] 测试通知推送适配器上报超时（消息可能已送达）"
                f" session={session}: {outcome.error}"
            )
        else:
            logger.warning(
                f"[YT] 测试通知推送失败 session={session}: {outcome.error}"
            )
        return outcome
