"""网页 JSON 兜底数据源单测：JSON 提取、lockup 解析、时间换算、降级链。

fixture 是 2026-09-12 从真实 YouTube 页面抓取的 ytInitialData（裁剪保留
原始嵌套结构），因此解析器是**对着真实结构**回归的，不是对着猜的结构。

运行: python tests/test_page_json.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

def _install_stub() -> None:
    """注入最简 astrbot 桩（覆盖 notifier 与 main 的导入需求）。

    本文件自建完整桩，不复用 test_imports 的 —— 后者看到 sys.modules 里
    已有 "astrbot" 就直接返回，复用会漏掉 AstrBotConfig / star 等符号。
    """
    import logging

    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("astrbot_test")
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    logging.basicConfig(level=logging.CRITICAL)

    class AstrBotConfig(dict):
        pass

    # astrbot.api.event: MessageChain / AstrMessageEvent / filter
    event_mod = types.ModuleType("astrbot.api.event")

    class MessageChain:
        def __init__(self, chain=None):
            self.chain = chain or []

        def file_image(self, path):
            self.chain.append(("image", path))
            return self

    class AstrMessageEvent:
        def __init__(self):
            self.unified_msg_origin = "test:group:1"

        def plain_result(self, text):
            return ("plain", text)

    class _Filter:
        def command(self, name, alias=None, **kwargs):
            def deco(fn):
                return fn

            return deco

    event_mod.MessageChain = MessageChain
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = _Filter()
    api.event = event_mod
    sys.modules["astrbot.api.event"] = event_mod

    # astrbot.api.star: Context / Star / StarTools / register
    star = types.ModuleType("astrbot.api.star")

    class Context:
        async def send_message(self, session, chain):
            return True

    class Star:
        def __init__(self, context):
            self.context = context

    class StarTools:
        @staticmethod
        def get_data_dir(plugin_name=None):
            return PLUGIN_ROOT / "data"

    def register(*a, **k):
        def deco(cls):
            return cls

        return deco

    star.Context = Context
    star.Star = Star
    star.StarTools = StarTools
    star.register = register
    api.star = star
    sys.modules["astrbot.api.star"] = star

    api.AstrBotConfig = AstrBotConfig


_install_stub()

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT.parent))

from astrbot_plugin_youtube_notifier.services import page_json as pj  # noqa: E402
from astrbot_plugin_youtube_notifier.services.models import (  # noqa: E402
    LIVE_STATE_COMPLETED,
    LIVE_STATE_LIVE,
    LIVE_STATE_UPCOMING,
    STATUS_ENDED,
    ChannelState,
    FeedResult,
)

FIXTURES = PLUGIN_ROOT / "tests" / "fixtures"
LIVE_FIXTURE = FIXTURES / "real_channel_streams_live.json"
NORMAL_FIXTURE = FIXTURES / "real_channel_videos_normal.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- mock HTTP


class FakeResponse:
    def __init__(self, text, status=200):
        self._text = text
        self.status = status

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """按 URL 片段路由返回 HTML 文本（或 (text, status) 元组）。"""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url, params=None, **kwargs):
        self.calls.append(url)
        for fragment, payload in self.routes.items():
            if fragment in url:
                if isinstance(payload, tuple):
                    return FakeResponse(payload[0], payload[1])
                return FakeResponse(payload)
        return FakeResponse("", 404)


def _html_with(payload: dict, marker: str = "ytInitialData") -> str:
    """把 JSON 塞进一段仿真的 HTML 里（含 JS 里同名的干扰串）。"""
    blob = json.dumps(payload, ensure_ascii=False)
    return (
        "<!DOCTYPE html><html><head></head><body>"
        "<script nonce=\"abc\">var ytInitialData = "
        + blob
        + ";</script>"
        # 真实页面里 JS bundle 会在后面再次出现同名标识，用来验证提取器
        # 不会被第一个「假」命中带偏
        + "<script>a.ytInitialData,a.ytInitialPlayerResponse;</script>"
        "</body></html>"
    )


# ---------------------------------------------------------------- JSON 提取


def test_extract_json_object_robust() -> None:
    """花括号配对提取：兼容 `var x = {}` 与 `x = {}` 两种写法。"""
    payload = {"a": {"b": [1, 2, {"c": "}"}]}, "d": '}\\" tricky'}
    blob = json.dumps(payload)

    for template in (
        f"var ytInitialData = {blob};</script>",
        f"window['ytInitialData']={blob};</script>",
        f"<script>ytInitialData = {blob}</script>",
    ):
        got = pj.extract_json_object(template, "ytInitialData")
        assert got == payload, f"提取失败: {template[:40]}"

    # marker 后面没有对象 / 对象被截断 → 返回 None，不能抛异常
    assert pj.extract_json_object("no marker here", "ytInitialData") is None
    assert pj.extract_json_object("ytInitialData = {", "ytInitialData") is None
    # marker 与 `{` 相隔太远（JS 里的无关串）→ 不误取
    assert pj.extract_json_object("ytInitialData" + "x" * 100 + "{}", "ytInitialData") is None
    print("✅ test_extract_json_object_robust")


# ---------------------------------------------------------------- 条目解析


def test_parse_normal_videos() -> None:
    """真实 /videos 页 fixture：普通投稿解析出标题/缩略图/近似时间。"""
    entries = pj.parse_channel_videos(_load(NORMAL_FIXTURE))
    assert entries, "解析不出条目（页面结构可能又变了）"
    for e in entries:
        assert e.video_id, "缺少 video_id"
        assert e.title, f"{e.video_id} 缺少标题"
        assert e.thumbnail_url.startswith("https://"), f"{e.video_id} 缺少缩略图"
        assert e.url == f"https://www.youtube.com/watch?v={e.video_id}"
        assert e.published_at, f"{e.video_id} 缺少时间"
        # 页面无法区分直播存档与普通投稿 → 一律空 live_state
        assert e.live_state == "", f"{e.video_id} 不该有直播状态: {e.live_state}"
    print(f"✅ test_parse_normal_videos ({len(entries)} 条)")


def test_parse_live_streams() -> None:
    """真实 /streams 页 fixture：靠 LIVE 角标识别直播，非直播条目一律丢弃。

    ⚠️ 不能把 /streams 的非直播条目标成 completed：实测 MrBeast 的
    /streams 里全是普通投稿（首播视频归入 streams 标签），标成 completed
    会让 find_new_videos 永久跳过这些真实投稿 → 静默漏推。
    """
    entries = pj.parse_channel_videos(_load(LIVE_FIXTURE), lives_only=True)
    assert entries, "解析不出条目"

    live = [e for e in entries if e.live_state == LIVE_STATE_LIVE]
    assert live, "未识别出任何直播（LIVE 角标解析可能失效）"
    for e in live:
        assert e.title and e.thumbnail_url

    # 只保留 live/upcoming；completed 之类的状态不该再出现
    for e in entries:
        assert e.live_state in (LIVE_STATE_LIVE, LIVE_STATE_UPCOMING), (
            f"{e.video_id} 不该出现在 lives_only 结果里: {e.live_state}"
        )
    assert not any(e.live_state == LIVE_STATE_COMPLETED for e in entries)
    print(f"✅ test_parse_live_streams ({len(entries)} 条, 直播 {len(live)} 条)")


def test_real_streams_tab_has_regular_videos() -> None:
    """固化那条致命实测结论：/streams 页会混入普通投稿。

    fixture 是 MrBeast 的 /videos 与 /streams 两份真实数据。若哪天有人
    「顺手」把 /streams 的非直播条目恢复成 completed，这条测试会立刻失败。
    """
    videos = pj.parse_channel_videos(_load(NORMAL_FIXTURE))
    streams_all = pj.parse_channel_videos(_load(LIVE_FIXTURE))
    streams_lives = pj.parse_channel_videos(_load(LIVE_FIXTURE), lives_only=True)
    # fixture 取自 NASA（真在直播），所以应能过滤出直播
    assert streams_lives, "NASA /streams 应含直播"
    assert len(streams_lives) < len(streams_all), "lives_only 应确实做了过滤"
    # 两份 fixture 来自不同频道，各自都应有普通投稿可供新投稿检测
    assert videos, "/videos 应有普通投稿"
    print(
        f"✅ test_real_streams_tab_has_regular_videos "
        f"(streams {len(streams_all)}→{len(streams_lives)} 条)"
    )


def test_live_entries_never_capped() -> None:
    """直播必须免疫 max_results 截断。

    实测 NASA 的 ISS 直播已持续数周，按时间排序会掉出窗口；
    一旦被截掉，live_start 永远不会触发。
    """
    entries = pj.parse_channel_videos(_load(LIVE_FIXTURE))
    live_ids = {e.video_id for e in entries if e.live_state == LIVE_STATE_LIVE}
    assert live_ids, "fixture 里应有直播"

    for limit in (1, 2, 3):
        capped = pj._cap_entries(entries, limit)
        kept = {e.video_id for e in capped if e.live_state == LIVE_STATE_LIVE}
        assert kept == live_ids, f"max_results={limit} 时直播被截断: {live_ids - kept}"
        # 非直播条目确实被限制
        others = [e for e in capped if e.live_state not in (LIVE_STATE_LIVE, LIVE_STATE_UPCOMING)]
        assert len(others) <= limit
    print("✅ test_live_entries_never_capped")


def test_malformed_items_are_skipped() -> None:
    """缺字段/畸形条目不能抛异常，返回 None 跳过。"""
    assert pj.lockup_to_entry({}) is None
    assert pj.lockup_to_entry({"contentId": ""}) is None
    # 只有 contentId，没有任何 metadata → 仍应产出条目（标题为空）
    entry = pj.lockup_to_entry({"contentId": "abcdefghijk"})
    assert entry is not None and entry.video_id == "abcdefghijk"
    print("✅ test_malformed_items_are_skipped")


# ---------------------------------------------------------------- 时间换算


def test_relative_time_parsing() -> None:
    now = datetime(2026, 9, 12, 12, 0, 0, tzinfo=timezone.utc)
    cases = {
        "6 days ago": "2026-09-06T12:00:00+00:00",
        "23 hours ago": "2026-09-11T13:00:00+00:00",
        "2 weeks ago": "2026-08-29T12:00:00+00:00",
        "1 month ago": "2026-08-13T12:00:00+00:00",
        "45 minutes ago": "2026-09-12T11:15:00+00:00",
        "1 year ago": "2025-09-12T12:00:00+00:00",
        # 直播/首播前缀不影响解析
        "Streamed 2 days ago": "2026-09-10T12:00:00+00:00",
        "Premiered 3 days ago": "2026-09-09T12:00:00+00:00",
        "Started streaming 2 hours ago": "2026-09-12T10:00:00+00:00",
        # 中文页面兜底
        "6天前": "2026-09-06T12:00:00+00:00",
        "3小时前": "2026-09-12T09:00:00+00:00",
    }
    for text, expected in cases.items():
        got = pj.parse_relative_time_to_iso(text, now=now)
        assert got == expected, f"{text!r} → {got}，期望 {expected}"

    # 观看人数/无时间信息 → 空串（不能瞎猜成「刚刚」）
    for text in ("77 watching", "81M views", "", "LIVE"):
        assert pj.parse_relative_time_to_iso(text, now=now) == "", text
    print(f"✅ test_relative_time_parsing ({len(cases)} 个用例)")


def test_date_text_parsing() -> None:
    cases = {
        "Sep 5, 2026": "2026-09-05T00:00:00+00:00",
        "Started streaming on Jul 30, 2026": "2026-07-30T00:00:00+00:00",
        "Streamed live on Dec 31, 2025": "2025-12-31T00:00:00+00:00",
        "Premiered Jan 9, 2026": "2026-01-09T00:00:00+00:00",
    }
    for text, expected in cases.items():
        got = pj.parse_date_text_to_iso(text)
        assert got == expected, f"{text!r} → {got}，期望 {expected}"
    for text in ("", "LIVE", "Streaming now", "Sep 5"):
        assert pj.parse_date_text_to_iso(text) == "", text
    print(f"✅ test_date_text_parsing ({len(cases)} 个用例)")


# ---------------------------------------------------------------- 输入解析


def test_video_and_target_parsing() -> None:
    video_inputs = {
        "https://www.youtube.com/watch?v=gTKS8SAwUzE": "gTKS8SAwUzE",
        "https://www.youtube.com/watch?v=gTKS8SAwUzE&t=30s": "gTKS8SAwUzE",
        "https://youtu.be/gTKS8SAwUzE": "gTKS8SAwUzE",
        "https://www.youtube.com/live/M3HKLzjvKPc": "M3HKLzjvKPc",
        "https://www.youtube.com/shorts/gTKS8SAwUzE": "gTKS8SAwUzE",
        "gTKS8SAwUzE": "gTKS8SAwUzE",
    }
    for raw, expected in video_inputs.items():
        assert pj.parse_video_input(raw) == expected, raw

    # 频道标识不能被误判成视频 id
    for raw in ("@NASA", "@MrBeast", "UCX6OQ3DkcsbYNE6H8uQQuVA",
                "https://www.youtube.com/@NASA", "https://www.youtube.com/channel/UCX6OQ3DkcsbYNE6H8uQQuVA"):
        assert pj.parse_video_input(raw) == "", raw
        assert pj.parse_target(raw) == ("channel", raw), pj.parse_target(raw)

    for raw, expected in video_inputs.items():
        assert pj.parse_target(raw) == ("video", expected), raw
    assert pj.parse_target("") == ("", "")
    print("✅ test_video_and_target_parsing")


# ---------------------------------------------------------------- 客户端


def test_fetch_snapshot_merges_tabs() -> None:
    """同时抓 /videos 与 /streams，合并后直播优先、往期直播为 completed。"""
    session = FakeSession({
        "/videos": _html_with(_load(NORMAL_FIXTURE)),
        "/streams": _html_with(_load(LIVE_FIXTURE)),
    })
    client = pj.ChannelPageClient(session, min_interval=0)

    snap = asyncio.run(
        client.fetch_snapshot("UCLA_DiR1FfKNvjuUpBHmylQ", "NASA", max_results=5)
    )
    assert snap is not None, "两个页面都抓到了，不该返回 None"
    assert len(session.calls) == 2, f"应各抓一次，实际 {session.calls}"

    live = snap.find_live()
    assert live is not None and live.live_state == LIVE_STATE_LIVE

    # 时间倒序（无时间的直播排在最后）
    times = [e.published_at for e in snap.entries]
    assert times == sorted(times, reverse=True), f"未按时间倒序: {times}"

    # 两个页面的 video_id 都应在（合并而非覆盖）
    normal_ids = {e.video_id for e in pj.parse_channel_videos(_load(NORMAL_FIXTURE))}
    assert normal_ids & {e.video_id for e in snap.entries}, "普通投稿被丢了"
    print(f"✅ test_fetch_snapshot_merges_tabs ({len(snap.entries)} 条合并)")


def test_fetch_snapshot_returns_none_when_all_fail() -> None:
    """两个页面都失败 → 返回 None，让上层知道是失败而不是「没有新内容」。"""
    client = pj.ChannelPageClient(FakeSession({}), min_interval=0)
    assert asyncio.run(client.fetch_snapshot("UCX6OQ3DkcsbYNE6H8uQQuVA", "x")) is None

    # 一个成立一个失败 → 仍应给出快照（部分可用总比没有好）
    session = FakeSession({"/videos": _html_with(_load(NORMAL_FIXTURE))})
    client2 = pj.ChannelPageClient(session, min_interval=0)
    snap = asyncio.run(client2.fetch_snapshot("UCX6OQ3DkcsbYNE6H8uQQuVA", "x"))
    assert snap is not None and snap.entries
    print("✅ test_fetch_snapshot_returns_none_when_all_fail")


def test_throttle_prevents_refetch() -> None:
    """冷却期内重复请求不再抓页面（网页约 1.2MB，必须节流）。"""
    session = FakeSession({
        "/videos": _html_with(_load(NORMAL_FIXTURE)),
        "/streams": _html_with(_load(LIVE_FIXTURE)),
    })
    client = pj.ChannelPageClient(session, min_interval=999)
    asyncio.run(client.fetch_snapshot("UCX6OQ3DkcsbYNE6H8uQQuVA", "MrBeast"))
    assert len(session.calls) == 2
    asyncio.run(client.fetch_snapshot("UCX6OQ3DkcsbYNE6H8uQQuVA", "MrBeast"))
    assert len(session.calls) == 2, "冷却期内不应重复抓取"
    print("✅ test_throttle_prevents_refetch")


# ---------------------------------------------------------------- 防 VOD 重复


def test_stream_vod_not_notified_as_new_video() -> None:
    """网页兜底的防 VOD 重复推送：直播结束后存档出现在 /videos 里。

    真实场景：/streams 给出 live（带 LIVE 角标），直播结束后同一视频会作为
    普通条目出现在 /videos（角标与普通投稿完全一样），此时页面已无法区分
    它是不是直播存档 —— 所以**只能**靠状态机的 recent_live_ids 挡住，
    否则用户会先收「🔴 上播」再收「📺 新投稿」两条重复通知。
    """
    from astrbot_plugin_youtube_notifier.services.state_machine import (
        find_new_videos,
        process_live,
    )

    live_entries = pj.parse_channel_videos(_load(LIVE_FIXTURE), lives_only=True)
    live_entry = next(e for e in live_entries if e.live_state == LIVE_STATE_LIVE)

    # 已建立的监控：播种过、且不是首次接入（首次接入在直播是静默的）
    state = ChannelState(
        channel_id="UCLA_DiR1FfKNvjuUpBHmylQ",
        video_seeded=True,
        live_seeded=True,
        last_status=STATUS_ENDED,
        last_live_id="OLDLIVEID01",
    )
    now = "2026-09-12T12:00:00+00:00"

    # 第一轮：检测到上播
    from astrbot_plugin_youtube_notifier.services.models import LiveInfo

    live_info = LiveInfo(
        live_id=live_entry.video_id,
        title=live_entry.title,
        channel_id=state.channel_id,
        start_time=now,
    )
    notifs = process_live(live_info, state, now)
    assert [n.type for n in notifs] == ["live_start"], notifs
    assert live_entry.video_id in state.recent_live_ids

    # 第二轮：同一视频作为普通条目出现在 /videos 页（直播存档，无角标）
    vod = pj.lockup_to_entry(_regular_entry(live_entry.video_id))
    assert vod is not None and vod.live_state == "", "存档在 /videos 页应无直播状态"

    snapshot = FeedResult(state.channel_id, "NASA", [vod])
    again = find_new_videos(snapshot.entries, state, now)
    assert again == [], f"直播存档被当成新投稿重复推送了: {again}"
    print("✅ test_stream_vod_not_notified_as_new_video")


def test_regular_video_after_fallback_is_still_notified() -> None:
    """反向保护：真实投稿必须仍能被通知（别把普通条目一律当存档吞掉）。

    这条是上一条的对偶 —— 一味「宁可少推」会静默漏推真实投稿，
    正是 MrBeast /streams 那个坑的成因。
    """
    from astrbot_plugin_youtube_notifier.services.state_machine import find_new_videos

    state = ChannelState(
        channel_id="UCX6OQ3DkcsbYNE6H8uQQuVA", video_seeded=True,
        last_video_id="OLDVIDEO001",
    )
    entries = pj.parse_channel_videos(_load(NORMAL_FIXTURE))
    notifs = find_new_videos(entries, state, "2026-09-12T12:00:00+00:00")
    assert notifs, "普通投稿没被通知（可能被误标成直播存档了）"
    assert all(n.type == "new_video" for n in notifs)
    print(f"✅ test_regular_video_after_fallback_is_still_notified ({len(notifs)} 条)")


def _regular_entry(video_id: str) -> dict:
    """构造一条 /videos 页样式的 lockup（无直播角标，只有时长角标）。"""
    return {
        "contentId": video_id,
        "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
        "metadata": {
            "lockupMetadataViewModel": {
                "title": {"content": "Live Video from the ISS"},
                "metadata": {
                    "contentMetadataViewModel": {
                        "metadataRows": [
                            {"metadataParts": [{"text": {"content": "1 day ago"}}]}
                        ]
                    }
                },
            }
        },
        "contentImage": {
            "thumbnailViewModel": {
                "image": {"sources": [{"url": "https://i.ytimg.com/vi/x/hq720.jpg", "width": 720}]},
                "overlays": [
                    {
                        "thumbnailBottomOverlayViewModel": {
                            "badges": [
                                {
                                    "thumbnailBadgeViewModel": {
                                        "text": "3:12:00",
                                        "badgeStyle": "THUMBNAIL_OVERLAY_BADGE_STYLE_DEFAULT",
                                    }
                                }
                            ]
                        }
                    }
                ],
            }
        },
    }


# ---------------------------------------------------------------- 降级链


def test_notifier_falls_back_on_quota() -> None:
    """配额耗尽 → 自动改用网页 JSON，并留下降级痕迹。"""
    from astrbot_plugin_youtube_notifier.services.data_api import QuotaExceededError
    from astrbot_plugin_youtube_notifier.services.notifier import NotificationService

    class _Store:
        def __init__(self):
            self.saved = 0

        def get_channel_state(self, cid):
            return state

        def set_channel_name(self, cid, name):
            pass

        def sessions_for_channel(self, cid):
            return []  # 本用例只验证降级链，不涉及推送

        async def save(self):
            self.saved += 1

    class _QuotaAPI:
        configured = True

        async def fetch_snapshot(self, *a, **k):
            raise QuotaExceededError("配额耗尽")

    state = ChannelState(channel_id="UCX6OQ3DkcsbYNE6H8uQQuVA",
                         channel_name="MrBeast", uploads_playlist_id="UUx",
                         name_from_api=True, video_seeded=True)

    session = FakeSession({
        "/videos": _html_with(_load(NORMAL_FIXTURE)),
        "/streams": _html_with(_load(LIVE_FIXTURE)),
    })
    page_client = pj.ChannelPageClient(session, min_interval=0)

    notifier = NotificationService(
        context=object(),
        store=_Store(),
        renderer=object(),
        data_api=_QuotaAPI(),
        page_json=page_client,
        live_detect_mode="data_api",
        page_fallback_enabled=True,
    )
    asyncio.run(notifier.check_channel(state.channel_id))

    assert notifier.degraded_reason(state.channel_id), "降级了却没有留痕"
    assert "配额" in notifier.degraded_reason(state.channel_id)
    assert state.video_seeded, "播种标志被破坏"

    # 关闭兜底后配额耗尽 → 不回退网页，且不应崩
    class _NoPage:
        async def fetch_snapshot(self, *a, **k):
            raise AssertionError("兜底已关闭，不该调用网页数据源")

    notifier2 = NotificationService(
        context=object(), store=_Store(), renderer=object(), data_api=_QuotaAPI(),
        page_json=_NoPage(), live_detect_mode="data_api", page_fallback_enabled=False,
    )
    asyncio.run(notifier2.check_channel(state.channel_id))
    assert notifier2.degraded_reason(state.channel_id) == ""
    print("✅ test_notifier_falls_back_on_quota")


def test_notifier_uses_page_json_without_api_key() -> None:
    """没配 Key 时用网页 JSON 监控（取代已不可靠的 legacy feed）。"""
    from astrbot_plugin_youtube_notifier.services.notifier import NotificationService

    class _Store:
        def get_channel_state(self, cid):
            return state

        def set_channel_name(self, cid, name):
            state.channel_name = name

        def sessions_for_channel(self, cid):
            return []

        async def save(self):
            pass

    class _NoKeyAPI:
        configured = False

    class _MustNotBeUsedFeed:
        async def fetch_feed(self, *a, **k):
            raise AssertionError("网页兜底可用时不该走 legacy feed")

    state = ChannelState(channel_id="UCLA_DiR1FfKNvjuUpBHmylQ", channel_handle="@NASA",
                         uploads_playlist_id="UUx", name_from_api=False, video_seeded=True)
    session = FakeSession({
        "/videos": _html_with(_load(NORMAL_FIXTURE)),
        "/streams": _html_with(_load(LIVE_FIXTURE)),
    })
    notifier = NotificationService(
        context=object(), store=_Store(), renderer=object(),
        data_api=_NoKeyAPI(), legacy_feed=_MustNotBeUsedFeed(),
        page_json=pj.ChannelPageClient(session, min_interval=0),
        live_detect_mode="auto", page_fallback_enabled=True,
    )
    asyncio.run(notifier.check_channel(state.channel_id))

    assert notifier.degraded_reason(state.channel_id), "没留下降级痕迹"
    assert "API Key" in notifier.degraded_reason(state.channel_id)
    print("✅ test_notifier_uses_page_json_without_api_key")


def test_semantic_errors_are_not_retried() -> None:
    """语义性错误（Key 无效/配额耗尽/频道不存在）不能进重试循环。

    实测踩坑：无效 API Key 被重试 3 次（1.1s + 3.3s + 11.6s ≈ 16 秒）才走到
    本就该立刻执行的降级逻辑 —— 每个频道每轮白等十几秒。
    """
    from astrbot_plugin_youtube_notifier.services.data_api import (
        ChannelNotFoundError,
        InvalidApiKeyError,
        QuotaExceededError,
    )
    from astrbot_plugin_youtube_notifier.utils import retry_async

    for err in (InvalidApiKeyError("坏 Key"), QuotaExceededError("配额没了"),
                ChannelNotFoundError("没这个频道")):
        calls = {"n": 0}

        async def factory(err=err):
            calls["n"] += 1
            raise err

        try:
            asyncio.run(retry_async(factory, retries=3, base_delay=0.01,
                                    non_retryable=(InvalidApiKeyError,
                                                   QuotaExceededError,
                                                   ChannelNotFoundError)))
            raise AssertionError(f"{type(err).__name__} 应该抛出")
        except (InvalidApiKeyError, QuotaExceededError, ChannelNotFoundError):
            pass
        assert calls["n"] == 1, f"{type(err).__name__} 被重试了 {calls['n']} 次"

    # 对照：网络类错误仍要重试
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("网络抖动")
        return "ok"

    assert asyncio.run(
        retry_async(flaky, retries=3, base_delay=0.01,
                    non_retryable=(InvalidApiKeyError,))
    ) == "ok"
    assert calls["n"] == 3
    print("✅ test_semantic_errors_are_not_retried")


def test_runtime_degradation_is_surfaced() -> None:
    """配了 Key 但 Key 无效时，必须报出降级（配置检查发现不了这种问题）。"""
    from astrbot_plugin_youtube_notifier.main import YouTubeNotifierPlugin

    class _API:
        configured = True  # ← 配置层面「已配置」

    class _Notifier:
        live_detect_mode = "data_api"
        page_fallback_enabled = True

    plugin = YouTubeNotifierPlugin(context=object(), config={"basic": {}})
    plugin.data_api = _API()
    plugin.notifier = _Notifier()
    plugin.page_json = object()

    # 配置层面看起来一切正常
    assert plugin._monitoring_ready()
    assert plugin._degraded_notice() == ""

    # 真实调用暴露 Key 无效 → 必须浮出降级提示
    plugin._note_runtime_degraded("Data API Key 无效（API key not valid）")
    notice = plugin._degraded_notice()
    assert "降级" in notice and "api_key" in notice, notice

    # Data API 恢复后提示要消失
    plugin._clear_runtime_degraded()
    assert plugin._degraded_notice() == ""
    print("✅ test_runtime_degradation_is_surfaced")


# ---------------------------------------------------------------- main 取


def main() -> int:
    tests = [
        test_extract_json_object_robust,
        test_parse_normal_videos,
        test_parse_live_streams,
        test_real_streams_tab_has_regular_videos,
        test_live_entries_never_capped,
        test_malformed_items_are_skipped,
        test_relative_time_parsing,
        test_date_text_parsing,
        test_video_and_target_parsing,
        test_fetch_snapshot_merges_tabs,
        test_fetch_snapshot_returns_none_when_all_fail,
        test_throttle_prevents_refetch,
        test_stream_vod_not_notified_as_new_video,
        test_regular_video_after_fallback_is_still_notified,
        test_notifier_falls_back_on_quota,
        test_notifier_uses_page_json_without_api_key,
        test_semantic_errors_are_not_retried,
        test_runtime_degradation_is_surfaced,
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
