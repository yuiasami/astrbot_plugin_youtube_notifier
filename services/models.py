"""数据模型定义。

- ChannelState: 单个频道的监控状态（全局共享，与订阅解耦）
- FeedEntry: 一条视频/投稿的归一化结果（Data API 与 legacy feed 共用）
- LiveInfo: 当前直播的信息（供状态机判定）
- Notification: 一次待发送的通知事件
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Optional

# 直播状态
STATUS_NONE = "none"      # 未接入 / 无直播记录
STATUS_LIVE = "live"      # 直播中
STATUS_ENDED = "ended"    # 已结束

# 通知类型
TYPE_LIVE_START = "live_start"
TYPE_LIVE_END = "live_end"
TYPE_NEW_VIDEO = "new_video"

# 直播状态取值（snippet.liveBroadcastContent 与 feed 信号共用）
LIVE_STATE_LIVE = "live"
LIVE_STATE_UPCOMING = "upcoming"
LIVE_STATE_COMPLETED = "completed"
LIVE_STATE_NONE = "none"

# 已作为直播通知过的 video id 保留数量（防止下播后的 VOD 被误判为新投稿）
RECENT_LIVE_IDS_MAX = 10


@dataclass
class ChannelState:
    """单频道的监控状态。字段缺失时 from_dict 用默认值兜底。"""

    channel_id: str = ""
    channel_name: str = ""
    # 用户输入形式（@handle 或 URL），便于展示
    channel_handle: str = ""
    # channels.list 拿到的上传播放列表 ID，缓存避免重复消耗配额
    uploads_playlist_id: str = ""
    # channel_name 是否来自官方 API。抓频道页得到的名字可能是其它语言
    # （如英文 og:title），本字段用于在拿到 API Key 后触发一次更正。
    name_from_api: bool = False

    # 投稿去重
    last_video_id: str = ""
    latest_video_title: str = ""
    latest_video_published_at: str = ""
    # 是否已完成「首次接入播种」。
    # 不能拿 last_video_id 是否为空来判断：主播型频道上传列表全是直播存档，
    # 播种时找不到普通投稿，last_video_id 会一直为空 —— 那样该频道发的第一个
    # 普通视频会被误判成「首次接入」而静默吞掉，永不通知。
    video_seeded: bool = False

    # 直播状态机
    # 是否已完成过一次直播状态观测/播种；不是「是否正在直播」。
    live_seeded: bool = False
    last_live_id: str = ""
    last_status: str = STATUS_NONE
    last_live_start_at: str = ""  # ISO8601
    last_live_end_at: str = ""
    last_live_title: str = ""
    last_live_thumbnail_url: str = ""
    # 已作为直播通知过的 video id，避免下播后 VOD 再被当成新投稿推送
    recent_live_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ChannelState":
        valid = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in (data or {}).items() if k in valid}
        # recent_live_ids 容错
        ids = kwargs.get("recent_live_ids")
        if not isinstance(ids, list):
            kwargs["recent_live_ids"] = []
        else:
            kwargs["recent_live_ids"] = [str(i) for i in ids if isinstance(i, str)]
        # 兼容旧状态文件：没有 video_seeded 字段时，若已有 last_video_id
        # 说明此前已播种过，避免升级后被当成首次接入而漏推。
        if "video_seeded" not in kwargs and kwargs.get("last_video_id"):
            kwargs["video_seeded"] = True
        # 老订阅都已经走过订阅时的直播播种流程，只是当时没有这个字段。
        # 升级后视为已播种，确保下一次开播会正常通知。
        if "live_seeded" not in kwargs:
            kwargs["live_seeded"] = True
        return cls(**kwargs)

    def mark_live_notified(self, video_id: str) -> None:
        """记录某 video id 已作为直播通知过（有界列表）。"""
        if not video_id:
            return
        ids = [i for i in self.recent_live_ids if i != video_id]
        ids.insert(0, video_id)
        self.recent_live_ids = ids[:RECENT_LIVE_IDS_MAX]


@dataclass
class FeedEntry:
    """一条视频/投稿的归一化结果（Data API 与 legacy feed 共用）。"""

    video_id: str = ""
    title: str = ""
    channel_id: str = ""
    channel_name: str = ""
    published_at: str = ""  # ISO8601
    updated_at: str = ""
    url: str = ""
    thumbnail_url: str = ""
    # 直播状态: "" / "none" / "upcoming" / "live" / "completed"
    live_state: str = ""
    # 直播实际起止（liveStreamingDetails），用于计算时长
    actual_start_time: str = ""
    actual_end_time: str = ""

    @property
    def is_live(self) -> bool:
        return self.live_state == LIVE_STATE_LIVE

    @property
    def was_live(self) -> bool:
        """曾是直播（含已结束），用于把它与普通投稿区分开。"""
        return self.live_state in (
            LIVE_STATE_LIVE,
            LIVE_STATE_UPCOMING,
            LIVE_STATE_COMPLETED,
        )


@dataclass
class LiveInfo:
    """当前处于直播的流的信息（Data API / feed 归一化后的结果）。"""

    live_id: str = ""
    title: str = ""
    channel_id: str = ""
    channel_name: str = ""
    thumbnail_url: str = ""
    start_time: str = ""  # ISO8601
    url: str = ""


class FeedResult:
    """一次频道拉取的归一化结果（Data API 快照或 legacy feed 解析结果）。"""

    __slots__ = ("channel_id", "channel_name", "entries")

    def __init__(self, channel_id: str, channel_name: str, entries: list[FeedEntry]):
        self.channel_id = channel_id
        self.channel_name = channel_name
        self.entries = entries

    def find_live(self) -> Optional[FeedEntry]:
        """取当前处于直播的 entry（按 published 最新优先）。"""
        lives = [e for e in self.entries if e.is_live]
        if not lives:
            return None
        lives.sort(key=lambda e: e.published_at or "", reverse=True)
        return lives[0]


@dataclass
class ChannelMeta:
    """channels.list 解析出的频道元数据。"""

    channel_id: str = ""
    title: str = ""
    handle: str = ""
    uploads_playlist_id: str = ""


@dataclass
class Notification:
    """一次待发送的通知。"""

    type: str = ""  # live_start / live_end / new_video
    title: str = ""
    channel_id: str = ""
    channel_name: str = ""
    thumbnail_url: str = ""
    start_time: str = ""
    end_time: str = ""
    duration_seconds: int = 0
    url: str = ""
    video_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)
