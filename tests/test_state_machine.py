"""状态机 / feed 解析器离线单测（不依赖 AstrBot 运行时与网络）。

运行: python tests/test_state_machine.py
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

# Windows GBK 控制台下允许输出 ✅/❌
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

# ---- 为独立运行注入 astrbot.api.logger 桩
if "astrbot" not in sys.modules:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    import logging

    api.logger = logging.getLogger("astrbot_test")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api

# ---- 以包形式导入插件模块
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT.parent))

from astrbot_plugin_youtube_notifier.services.feed import parse_feed  # noqa: E402
from astrbot_plugin_youtube_notifier.services.models import (  # noqa: E402
    ChannelState,
    FeedEntry,
    FeedResult,
    LiveInfo,
    STATUS_ENDED,
    STATUS_LIVE,
    STATUS_NONE,
    TYPE_LIVE_END,
    TYPE_LIVE_START,
    TYPE_NEW_VIDEO,
)
from astrbot_plugin_youtube_notifier.services.state_machine import (  # noqa: E402
    find_new_videos,
    process_live,
    seed_channel_from_feed,
)

NOW = "2026-09-12T12:00:00+00:00"
LATER = "2026-09-12T14:00:00+00:00"

FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom">
  <title>Videos - 测试频道</title>
  <entry>
    <id>yt:video:LIVE001</id>
    <yt:videoId>LIVE001</yt:videoId>
    <yt:channelId>UC_TEST</yt:channelId>
    <title>正在进行的直播</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=LIVE001"/>
    <published>2026-09-12T11:00:00+00:00</published>
    <updated>2026-09-12T11:30:00+00:00</updated>
    <yt:liveBroadcastContent>live</yt:liveBroadcastContent>
    <media:group>
      <media:title>正在进行的直播</media:title>
      <media:thumbnail url="https://i.ytimg.com/vi/LIVE001/hqdefault.jpg"/>
    </media:group>
  </entry>
  <entry>
    <id>yt:video:VID002</id>
    <yt:videoId>VID002</yt:videoId>
    <yt:channelId>UC_TEST</yt:channelId>
    <title>普通投稿视频</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=VID002"/>
    <published>2026-09-11T08:00:00+00:00</published>
    <updated>2026-09-11T08:00:00+00:00</updated>
    <media:group>
      <media:title>普通投稿视频</media:title>
      <media:thumbnail url="https://i.ytimg.com/vi/VID002/hqdefault.jpg"/>
    </media:group>
  </entry>
</feed>
"""

# media:status 变体（另一种已知的直播信号写法）
FEED_XML_MEDIA_STATUS = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom">
  <title>Videos - 变体频道</title>
  <entry>
    <id>yt:video:LIVE009</id>
    <yt:videoId>LIVE009</yt:videoId>
    <yt:channelId>UC_ALT</yt:channelId>
    <title>media:status 变体直播</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=LIVE009"/>
    <published>2026-09-12T10:00:00+00:00</published>
    <media:group>
      <media:thumbnail url="https://i.ytimg.com/vi/LIVE009/hqdefault.jpg"/>
      <media:status state="live"/>
    </media:group>
  </entry>
</feed>
"""


def test_parse_feed() -> None:
    result = parse_feed(FEED_XML, "UC_TEST")
    assert result is not None, "feed 应解析成功"
    assert result.channel_name == "测试频道", f"频道名解析错误: {result.channel_name!r}"
    assert len(result.entries) == 2

    live_entry = result.find_live()
    assert live_entry is not None, "应识别出直播 entry"
    assert live_entry.video_id == "LIVE001"
    assert live_entry.is_live
    assert live_entry.thumbnail_url.endswith("hqdefault.jpg")
    assert live_entry.url == "https://www.youtube.com/watch?v=LIVE001"

    video = [e for e in result.entries if not e.live_state]
    assert len(video) == 1 and video[0].video_id == "VID002"
    print("✅ test_parse_feed")


def test_parse_feed_media_status_variant() -> None:
    result = parse_feed(FEED_XML_MEDIA_STATUS, "UC_ALT")
    assert result is not None
    live = result.find_live()
    assert live is not None and live.video_id == "LIVE009", "应识别 media:status=live 变体"
    print("✅ test_parse_feed_media_status_variant")


def test_parse_real_youtube_feed() -> None:
    """真实 YouTube feed 回归（2026-09 抓取，YouTube 官方频道 15 条）。

    注意：真实 feed 的普通投稿 entry 只含
    [author, channelId, group, id, link, published, title, updated, videoId]，
    不含 liveBroadcastContent / media:status —— 这两个字段只在直播时出现。
    因此本用例只断言「能稳定解析出条目」，不断言直播（无直播样本）。
    """
    fixture = Path(__file__).resolve().parent / "fixtures" / "real_feed_youtube.xml"
    result = parse_feed(fixture.read_text(encoding="utf-8"))
    assert result is not None, "真实 feed 应能解析"
    assert result.channel_name == "YouTube", result.channel_name
    assert len(result.entries) == 15, len(result.entries)
    first = result.entries[0]
    assert first.video_id, "应解析出 video_id"
    assert first.title, "应解析出标题"
    # 注意：真实 feed 的 alternate 链接可能是 /shorts/ 而非 /watch?v=
    # （解析器保留 feed 原始链接，不强行改写，这是刻意行为）
    assert first.url.startswith("https://www.youtube.com/"), first.url
    assert first.video_id in first.url, "链接应包含 video id"
    assert first.thumbnail_url.startswith("http"), first.thumbnail_url
    assert first.published_at, "应解析出发布时间"
    # 普通投稿不应被识别为直播
    assert all(not e.is_live for e in result.entries)
    assert result.find_live() is None
    print(f"✅ test_parse_real_youtube_feed ({len(result.entries)} 条真实数据)")


def test_real_feed_channel_id_has_uc_prefix() -> None:
    """回归：真实 feed 根的 yt:channelId 不含 UC 前缀，必须补回。

    真实数据：根 <yt:channelId>BR8-60-B28hp2BmDPdntcQ</yt:channelId>
              entry <yt:channelId>UCBR8-60-B28hp2BmDPdntcQ</yt:channelId>
    """
    fixture = Path(__file__).resolve().parent / "fixtures" / "real_feed_youtube.xml"
    raw = fixture.read_text(encoding="utf-8")
    assert "<yt:channelId>BR8-60-B28hp2BmDPdntcQ</yt:channelId>" in raw, (
        "fixture 应保留根元素无 UC 前缀的真实现象"
    )
    result = parse_feed(raw)
    assert result.channel_id == "UCBR8-60-B28hp2BmDPdntcQ", result.channel_id
    # entry 级 channelId 本就是完整值
    assert all(e.channel_id == "UCBR8-60-B28hp2BmDPdntcQ" for e in result.entries)
    print("✅ test_real_feed_channel_id_has_uc_prefix")


def test_real_feed_shorts_link_preserved() -> None:
    """真实 feed 中 shorts 条目的链接应被原样保留。"""
    fixture = Path(__file__).resolve().parent / "fixtures" / "real_feed_youtube.xml"
    result = parse_feed(fixture.read_text(encoding="utf-8"))
    shorts = [e for e in result.entries if "/shorts/" in e.url]
    assert shorts, "真实数据中应含 shorts 条目（用于固化该行为）"
    for e in shorts:
        assert e.video_id in e.url, "shorts 链接应指向对应 video id"
    print(f"✅ test_real_feed_shorts_link_preserved ({len(shorts)} 条 shorts)")


def test_real_feed_thumbnail_selection() -> None:
    """真实 feed 的 media:thumbnail 应能取到 URL。"""
    fixture = Path(__file__).resolve().parent / "fixtures" / "real_feed_youtube.xml"
    result = parse_feed(fixture.read_text(encoding="utf-8"))
    with_thumb = [e for e in result.entries if e.thumbnail_url]
    assert len(with_thumb) == len(result.entries), "所有真实 entry 都应有封面"
    print("✅ test_real_feed_thumbnail_selection")


def test_seed_silent() -> None:
    """订阅播种：标记最新投稿与当前直播，且不产生通知。"""
    state = ChannelState(channel_id="UC_TEST")
    feed = parse_feed(FEED_XML, "UC_TEST")
    seed_channel_from_feed(state, feed)
    assert state.live_seeded is True
    assert state.last_video_id == "VID002", state.last_video_id
    assert state.last_status == STATUS_LIVE, state.last_status
    assert state.last_live_id == "LIVE001"
    print("✅ test_seed_silent")


def test_live_none_to_live_is_silent() -> None:
    """未经 seed_channel_from_feed 播种的裸状态机首次观测直播时静默。"""
    state = ChannelState(channel_id="UC_X")
    live = LiveInfo(live_id="L1", title="直播一", start_time=NOW, url="u")
    notes = process_live(live, state, NOW)
    assert notes == [], f"首次接入不应推送: {notes}"
    assert state.live_seeded is True
    assert state.last_status == STATUS_LIVE
    assert state.last_live_id == "L1"
    print("✅ test_live_none_to_live_is_silent")


def test_live_after_seed_is_notified() -> None:
    """订阅时未在播，之后第一次真正开播必须通知。"""
    state = ChannelState(channel_id="UC_X")
    seed_channel_from_feed(state, FeedResult("UC_X", "测试频道", []))
    assert state.live_seeded is True
    assert state.last_status == STATUS_NONE

    live = LiveInfo(live_id="L1", title="直播一", start_time=NOW, url="u")
    notes = process_live(live, state, NOW)
    assert len(notes) == 1 and notes[0].type == TYPE_LIVE_START, notes
    assert notes[0].video_id == "L1"
    print("✅ test_live_after_seed_is_notified")


def test_live_seed_self_heals_after_failed_seed() -> None:
    """订阅播种失败后，首轮无直播观测应置位，之后开播必须通知。"""
    state = ChannelState(channel_id="UC_X")
    assert process_live(None, state, NOW) == []
    assert state.live_seeded is True

    live = LiveInfo(live_id="L1", title="直播一", start_time=NOW, url="u")
    notes = process_live(live, state, NOW)
    assert len(notes) == 1 and notes[0].type == TYPE_LIVE_START, notes
    print("✅ test_live_seed_self_heals_after_failed_seed")


def test_live_dedup_same_id() -> None:
    """同一直播重复检测不重复推送。"""
    state = ChannelState(channel_id="UC_X")
    live = LiveInfo(live_id="L1", title="直播一", start_time=NOW, url="u")
    process_live(live, state, NOW)  # 首次静默
    notes = process_live(live, state, NOW)
    assert notes == [], f"同一直播不应重复推送: {notes}"
    print("✅ test_live_dedup_same_id")


def test_live_start_after_ended() -> None:
    """ended 状态下新直播 → 推送上播。"""
    state = ChannelState(channel_id="UC_X")
    process_live(LiveInfo(live_id="L1", title="直播一", start_time=NOW, url="u"), state, NOW)
    state.last_status = STATUS_ENDED  # 模拟上一场已结束
    notes = process_live(
        LiveInfo(live_id="L2", title="直播二", start_time=NOW, url="u2"), state, NOW
    )
    assert len(notes) == 1 and notes[0].type == TYPE_LIVE_START, notes
    assert notes[0].video_id == "L2"
    assert state.last_live_id == "L2"
    print("✅ test_live_start_after_ended")


def test_live_end_with_duration() -> None:
    """live → none → 下播通知，计算时长。"""
    state = ChannelState(channel_id="UC_X")
    process_live(
        LiveInfo(live_id="L1", title="直播一", start_time=NOW, url="u"), state, NOW
    )
    notes = process_live(None, state, LATER)
    assert len(notes) == 1 and notes[0].type == TYPE_LIVE_END, notes
    assert notes[0].duration_seconds == 7200, notes[0].duration_seconds
    assert notes[0].title == "直播一"
    assert state.last_status == STATUS_ENDED
    print("✅ test_live_end_with_duration")


def test_live_switch_emits_both() -> None:
    """直播中换成另一个直播流 → 先下播再上播。"""
    state = ChannelState(channel_id="UC_X")
    process_live(LiveInfo(live_id="L1", title="一", start_time=NOW, url="u"), state, NOW)
    notes = process_live(
        LiveInfo(live_id="L2", title="二", start_time=NOW, url="u2"), state, NOW
    )
    types = [n.type for n in notes]
    assert types == [TYPE_LIVE_END, TYPE_LIVE_START], types
    print("✅ test_live_switch_emits_both")


def test_new_video_detection() -> None:
    """首次静默播种，之后新视频推送一次，不重复。"""
    feed = parse_feed(FEED_XML, "UC_TEST")
    state = ChannelState(channel_id="UC_TEST")

    # 首次：静默播种
    assert find_new_videos(feed.entries, state) == []
    assert state.last_video_id == "VID002"

    # 无新视频
    assert find_new_videos(feed.entries, state) == []

    # 出现新视频（插入到 feed 最前）
    newer = parse_feed(
        FEED_XML.replace(
            "<title>普通投稿视频</title>",
            "<title>普通投稿视频</title>",
        ).replace(
            'yt:video:VID002</id>',
            'yt:video:VID003</id>',
        ),
        "UC_TEST",
    )
    # 构造一条 genuinely new 的投稿
    from astrbot_plugin_youtube_notifier.services.models import FeedEntry

    entries = [
        FeedEntry(
            video_id="VID003",
            title="全新视频",
            channel_id="UC_TEST",
            channel_name="测试频道",
            published_at="2026-09-12T09:00:00+00:00",
            url="https://www.youtube.com/watch?v=VID003",
            thumbnail_url="t",
        ),
        *feed.entries,
    ]
    notes = find_new_videos(entries, state)
    assert len(notes) == 1 and notes[0].type == TYPE_NEW_VIDEO, notes
    assert notes[0].video_id == "VID003"
    assert state.last_video_id == "VID003"
    # 幂等
    assert find_new_videos(entries, state) == []
    assert newer is not None
    print("✅ test_new_video_detection")


def test_live_entry_not_treated_as_video() -> None:
    """直播 entry 不应被当作新投稿推送。"""
    feed = parse_feed(FEED_XML, "UC_TEST")
    state = ChannelState(channel_id="UC_TEST")
    state.last_video_id = "VID002"  # 已见过普通视频
    notes = find_new_videos(feed.entries, state)
    assert all(n.type != TYPE_NEW_VIDEO for n in notes), notes
    print("✅ test_live_entry_not_treated_as_video")


def test_vod_after_live_not_renotified_as_video() -> None:
    """直播结束后，同一 video id 变成普通 VOD 时不应再当新投稿推一次。"""
    state = ChannelState(channel_id="UC_TEST")
    # 先按直播通知（会写入 recent_live_ids）
    live = LiveInfo(live_id="LIVE001", title="直播", start_time=NOW, url="u")
    state.last_status = STATUS_ENDED  # 非首次接入，确保会推送
    process_live(live, state, NOW)
    assert "LIVE001" in state.recent_live_ids

    # 直播结束后该视频以普通投稿形态出现在列表里（live_state 已清空）
    entry = FeedEntry(
        video_id="LIVE001",
        title="直播存档",
        published_at="2026-09-12T11:00:00+00:00",
        live_state="",
    )
    notes = find_new_videos([entry], state)
    assert notes == [], f"直播存档不应重复推送为新投稿: {notes}"
    print("✅ test_vod_after_live_not_renotified_as_video")


def test_live_end_uses_actual_end_time() -> None:
    """提供 actualEndTime 时，时长应基于真实结束时间而非当前时间。"""
    state = ChannelState(channel_id="UC_TEST")
    state.last_status = STATUS_ENDED
    process_live(
        LiveInfo(live_id="L1", title="直播", start_time="2026-09-12T12:00:00+00:00", url="u"),
        state,
        NOW,
    )
    notes = process_live(
        None,
        state,
        "2026-09-12T23:59:00+00:00",  # now 很晚
        ended_at_iso="2026-09-12T13:30:00+00:00",  # 实际 1.5 小时后结束
    )
    assert len(notes) == 1 and notes[0].type == TYPE_LIVE_END
    assert notes[0].duration_seconds == 5400, notes[0].duration_seconds
    print("✅ test_live_end_uses_actual_end_time")


def test_streamer_channel_first_upload_is_notified() -> None:
    """真实场景回归：主播型频道（上传列表全是直播存档）。

    @ukaisaki 的真实上传列表 5 条全是 completed 直播。播种时找不到普通投稿，
    last_video_id 会一直为空。修复前，该频道之后发的第一个普通视频会被
    误判为「首次接入」而静默吞掉 —— 本用例固化修复后的正确行为。
    """
    state = ChannelState(channel_id="UC_STREAMER")

    # 播种：只有已结束的直播，没有普通投稿
    stream_only = FeedResult("UC_STREAMER", "主播频道", [
        FeedEntry(video_id="S1", title="直播1", published_at="2026-09-09T11:00:00+00:00",
                  live_state="completed", actual_start_time="2026-09-09T11:03:14+00:00",
                  actual_end_time="2026-09-09T18:03:57+00:00"),
        FeedEntry(video_id="S2", title="直播2", published_at="2026-08-29T11:00:00+00:00",
                  live_state="completed"),
    ])
    seed_channel_from_feed(state, stream_only)

    assert state.last_video_id == "", "没有普通投稿，last_video_id 应为空"
    assert state.video_seeded is True, "但必须标记为已播种"

    # 该频道终于发了一个普通视频 → 应该通知（修复前这里是漏推的）
    after = [
        FeedEntry(video_id="V_NEW", title="第一个普通视频",
                  published_at="2026-09-12T09:00:00+00:00", live_state=""),
        *stream_only.entries,
    ]
    notes = find_new_videos(after, state)
    assert len(notes) == 1 and notes[0].video_id == "V_NEW", (
        f"主播型频道的首个普通视频必须通知: {notes}"
    )
    # 幂等
    assert find_new_videos(after, state) == []
    print("✅ test_streamer_channel_first_upload_is_notified")


def test_empty_snapshot_does_not_mark_seeded() -> None:
    """失败的空拉取不应标记已播种，否则会把历史视频误报成新投稿。"""
    state = ChannelState(channel_id="UC_X")
    assert find_new_videos([], state) == []
    assert state.video_seeded is False, "空 entries 不应标记播种"

    # 下一轮成功拉取 → 静默播种，不推送历史视频
    entries = [FeedEntry(video_id="OLD", title="历史视频",
                         published_at="2026-09-01T00:00:00+00:00", live_state="")]
    assert find_new_videos(entries, state) == [], "首次成功拉取应静默播种"
    assert state.video_seeded is True
    assert state.last_video_id == "OLD"
    print("✅ test_empty_snapshot_does_not_mark_seeded")


def test_legacy_state_migration() -> None:
    """旧状态文件缺少播种字段时按各自语义迁移。"""
    old = {"channel_id": "UC_X", "last_video_id": "VID1", "last_status": "ended"}
    st = ChannelState.from_dict(old)
    assert st.video_seeded is True, "有 last_video_id 的旧状态应迁移为投稿已播种"
    assert st.live_seeded is True, "旧状态应无条件迁移为直播已播种"

    old2 = {"channel_id": "UC_Y"}  # 从未播种过投稿，但仍是既有订阅
    st2 = ChannelState.from_dict(old2)
    assert st2.video_seeded is False
    assert st2.live_seeded is True, "直播播种迁移不能依赖 last_video_id"
    print("✅ test_legacy_state_migration")


def test_actual_start_time_preferred_for_duration() -> None:
    """直播开始时间应优先取 actual_start_time（比发布时间准）。"""
    state = ChannelState(channel_id="UC_TEST")
    state.last_status = STATUS_ENDED
    live_entry = FeedEntry(
        video_id="L2",
        title="直播",
        published_at="2026-09-12T11:00:00+00:00",   # 发布时间（含预热）
        actual_start_time="2026-09-12T12:00:00+00:00",  # 真实开播
        live_state="live",
    )
    live = LiveInfo(
        live_id=live_entry.video_id,
        title=live_entry.title,
        start_time=live_entry.actual_start_time or live_entry.published_at,
        url="u",
    )
    process_live(live, state, NOW)
    assert state.last_live_start_at == "2026-09-12T12:00:00+00:00"
    print("✅ test_actual_start_time_preferred_for_duration")


def main() -> int:
    tests = [
        test_parse_feed,
        test_parse_feed_media_status_variant,
        test_parse_real_youtube_feed,
        test_real_feed_channel_id_has_uc_prefix,
        test_real_feed_shorts_link_preserved,
        test_real_feed_thumbnail_selection,
        test_seed_silent,
        test_live_none_to_live_is_silent,
        test_live_after_seed_is_notified,
        test_live_seed_self_heals_after_failed_seed,
        test_live_dedup_same_id,
        test_live_start_after_ended,
        test_live_end_with_duration,
        test_live_switch_emits_both,
        test_new_video_detection,
        test_live_entry_not_treated_as_video,
        test_vod_after_live_not_renotified_as_video,
        test_live_end_uses_actual_end_time,
        test_streamer_channel_first_upload_is_notified,
        test_empty_snapshot_does_not_mark_seeded,
        test_legacy_state_migration,
        test_actual_start_time_preferred_for_duration,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f"❌ {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"❌ {fn.__name__} 异常: {exc!r}")
    print()
    print(f"{'❌' if failed else '🎉'} {len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
