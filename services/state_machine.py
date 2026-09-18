"""直播状态机与新投稿检测（纯逻辑，无 IO，可独立单测）。

状态转移（ChannelState.last_status: none / live / ended）：

    上播: 当前有 live 且 (last_status != live 或 last_live_id 变化) → live_start
          例外: 尚未完成直播播种且从未见过直播状态时，首次观测到直播只记状态不推送
    下播: last_status==live 且当前无 live → live_end（记录 end_at，计算时长）
    防重复: 同一 live_id 已在 live 状态 → 不重复发上播
"""

from __future__ import annotations

from typing import Optional

from astrbot.api import logger

from .models import (
    ChannelState,
    FeedEntry,
    FeedResult,
    LiveInfo,
    Notification,
    STATUS_ENDED,
    STATUS_LIVE,
    STATUS_NONE,
    TYPE_LIVE_END,
    TYPE_LIVE_START,
    TYPE_NEW_VIDEO,
)
from ..utils import seconds_between

# 单轮最多推送的新投稿数量（feed 窗口自然上限 ~15）
MAX_NEW_VIDEOS_PER_ROUND = 15


def process_live(
    live: Optional[LiveInfo],
    state: ChannelState,
    now_iso: str,
    *,
    silent_initial: bool = True,
    ended_at_iso: str = "",
) -> list[Notification]:
    """根据当前直播状态驱动状态机，返回需要发送的通知。

    Args:
        live: 当前处于直播的流，None 表示无直播。
        state: 频道状态（会被原地修改）。
        now_iso: 当前时间 ISO 字符串。
        silent_initial: 未播种时首次观测到直播是否静默记录不推送。
        ended_at_iso: 已知的直播实际结束时间（Data API 的 actualEndTime），
                      用于计算更准确的时长；为空则用 now_iso。
    """
    notifications: list[Notification] = []
    # 每次成功观测都算完成直播播种；必须早于所有早退分支，才能让
    # 「订阅时播种失败、首轮轮询无直播」的场景自行恢复。
    was_seeded = state.live_seeded
    state.live_seeded = True

    if live is None:
        # 无直播：上一次在直播 → 下播
        if state.last_status == STATUS_LIVE:
            end_iso = ended_at_iso or now_iso
            state.last_live_end_at = end_iso
            state.last_status = STATUS_ENDED
            notifications.append(_make_live_end(state, end_iso))
        return notifications

    # 有直播
    if state.last_status == STATUS_LIVE and state.last_live_id == live.live_id:
        # 同一直播，防重复：仅刷新元数据
        _refresh_live_meta(state, live)
        return notifications

    prev_status = state.last_status
    if state.last_status == STATUS_LIVE:
        # 上一个直播结束了，换成了新直播 → 先补一条下播，再上播
        end_iso = ended_at_iso or now_iso
        state.last_live_end_at = end_iso
        notifications.append(_make_live_end(state, end_iso))

    _start_live(state, live, now_iso)
    # 记为「已作为直播通知过」，防止下播后该 VOD 又被当成新投稿推送
    state.mark_live_notified(live.live_id)

    if (not was_seeded) and prev_status == STATUS_NONE and silent_initial:
        # 未完成播种且首次观测时已经在直播：静默记录，不打扰用户
        logger.info(
            f"[YT] channel={state.channel_id} 首次接入已在直播 id={state.last_live_id}，"
            "静默记录不推送"
        )
    else:
        notifications.append(_make_live_start(state, live))
    return notifications


def find_new_videos(
    entries: list[FeedEntry],
    state: ChannelState,
    now_iso: Optional[str] = None,
) -> list[Notification]:
    """从 entry 中找出比 last_video_id 更新的普通投稿（排除直播）。

    排除两类：
      1. 本身处于直播状态（live/upcoming/completed）的条目；
      2. 已作为直播通知过的 video id（recent_live_ids），避免同一条
         直播结束后作为 VOD 再次被当成「新投稿」重复推送。

    Returns:
        按时间正序的通知列表；同时推进 state.last_video_id。
    """
    recent_live = set(state.recent_live_ids)
    regular = [
        e for e in entries if not e.live_state and e.video_id not in recent_live
    ]
    if not regular:
        # 拿到条目但一条普通投稿都没有（纯直播频道）→ 视为完成播种，
        # 之后出现的第一个普通视频仍会正常通知。
        # 若 entries 本身为空（可能是一次失败的拉取），则不动播种标志，
        # 留给下一轮成功拉取再播种，避免把历史视频误报成新投稿。
        if entries:
            state.video_seeded = True
        return []

    regular.sort(key=lambda e: e.published_at or "", reverse=True)  # 新的在前
    newest = regular[0]

    if not state.video_seeded:
        # 首次接入：静默标记最新投稿，不推送历史视频。
        # 注意用 video_seeded 而非 last_video_id 判空 —— 主播型频道
        # 上传列表全是直播存档，播种时 last_video_id 仍为空，若以判空为准
        # 会把该频道之后发的第一个普通视频也当「首次接入」吞掉。
        state.video_seeded = True
        state.last_video_id = newest.video_id
        state.latest_video_title = newest.title
        state.latest_video_published_at = newest.published_at
        logger.info(
            f"[YT] channel={state.channel_id} 首次接入，静默标记最新投稿 "
            f"id={newest.video_id}"
        )
        return []

    new_entries: list[FeedEntry] = []
    for e in regular:
        if e.video_id == state.last_video_id:
            break
        new_entries.append(e)
    new_entries = new_entries[:MAX_NEW_VIDEOS_PER_ROUND]
    new_entries.reverse()  # 时间正序，先发的先推

    if not new_entries:
        return []

    notifications = []
    for e in new_entries:
        notifications.append(
            Notification(
                type=TYPE_NEW_VIDEO,
                title=e.title,
                channel_id=state.channel_id,
                channel_name=e.channel_name or state.channel_name,
                thumbnail_url=e.thumbnail_url,
                start_time=e.published_at,
                url=e.url,
                video_id=e.video_id,
            )
        )
    # 推进去重标记
    state.last_video_id = new_entries[-1].video_id
    state.latest_video_title = new_entries[-1].title
    state.latest_video_published_at = new_entries[-1].published_at
    return notifications


def seed_channel_from_feed(state: ChannelState, feed: FeedResult) -> None:
    """订阅新频道时静默播种状态：标记最新投稿与当前直播，避免装好即刷屏。"""
    state.live_seeded = True
    state.channel_name = feed.channel_name or state.channel_name
    if feed.entries:
        # 无论有没有普通投稿都标记已播种：主播型频道（上传列表全是直播存档）
        # 播不到普通投稿，若因此不标记，之后第一个普通视频会被静默吞掉。
        state.video_seeded = True
        regular = [e for e in feed.entries if not e.live_state]
        if regular:
            newest = max(regular, key=lambda e: e.published_at or "")
            state.last_video_id = newest.video_id
            state.latest_video_title = newest.title
            state.latest_video_published_at = newest.published_at
    live_entry = feed.find_live()
    if live_entry is not None and state.last_status != STATUS_LIVE:
        state.last_live_id = live_entry.video_id
        state.last_live_start_at = (
            live_entry.actual_start_time or live_entry.published_at
        )
        state.last_live_title = live_entry.title
        state.last_live_thumbnail_url = live_entry.thumbnail_url
        state.last_status = STATUS_LIVE
        state.mark_live_notified(live_entry.video_id)


# ---------------------------------------------------------------- 内部工具


def _start_live(state: ChannelState, live: LiveInfo, now_iso: str) -> None:
    state.last_live_id = live.live_id
    state.last_live_start_at = live.start_time or now_iso
    state.last_live_end_at = ""
    state.last_status = STATUS_LIVE
    _refresh_live_meta(state, live)


def _refresh_live_meta(state: ChannelState, live: LiveInfo) -> None:
    if live.title:
        state.last_live_title = live.title
    if live.thumbnail_url:
        state.last_live_thumbnail_url = live.thumbnail_url
    if live.channel_name:
        state.channel_name = live.channel_name


def _make_live_start(state: ChannelState, live: LiveInfo) -> Notification:
    return Notification(
        type=TYPE_LIVE_START,
        title=live.title or state.last_live_title,
        channel_id=state.channel_id,
        channel_name=live.channel_name or state.channel_name,
        thumbnail_url=live.thumbnail_url or state.last_live_thumbnail_url,
        start_time=state.last_live_start_at,
        url=live.url or f"https://www.youtube.com/watch?v={live.live_id}",
        video_id=live.live_id,
    )


def _make_live_end(state: ChannelState, now_iso: str) -> Notification:
    duration = seconds_between(state.last_live_start_at, now_iso)
    logger.info(
        f"[YT] channel={state.channel_id} 直播结束 "
        f"id={state.last_live_id} duration={duration}s"
    )
    return Notification(
        type=TYPE_LIVE_END,
        title=state.last_live_title,
        channel_id=state.channel_id,
        channel_name=state.channel_name,
        thumbnail_url=state.last_live_thumbnail_url,
        start_time=state.last_live_start_at,
        end_time=now_iso,
        duration_seconds=duration,
        url=f"https://www.youtube.com/watch?v={state.last_live_id}",
        video_id=state.last_live_id,
    )
