"""推送结果分类与「假失败」处理。

背景（真实日志，2026-09，NapCat/QQ NT）：图片**已经发出去了**，但适配器抛
    ActionFailed(retcode=1200, message='Timeout: NTEvent
        serviceAndMethod:NodeIKernelMsgService/sendMsg ListenerName:
        NodeIKernelMsgListener/onMsgInfoListUpdate ...')
插件原本把它当推送失败 → 日志报错 + 给用户回「渲染或推送失败」，用户明明
收到了图，被误导去排查会话/适配器。这里锁住「不能误报失败」和「也不能
假装成功」的边界。

运行: python tests/test_notifier_send.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
import types
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _install_stub() -> None:
    """完整 astrbot 桩（notifier 需要 astrbot.api.event.MessageChain）。"""
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("astrbot_test")
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    logging.basicConfig(level=logging.CRITICAL)

    event_mod = types.ModuleType("astrbot.api.event")

    class MessageChain:
        def __init__(self, chain=None):
            self.chain = chain or []

        def file_image(self, path):
            self.chain.append(("image", path))
            return self

        def message(self, text):
            self.chain.append(("text", text))
            return self

    event_mod.MessageChain = MessageChain
    api.event = event_mod
    sys.modules["astrbot.api.event"] = event_mod


_install_stub()
sys.path.insert(0, str(PLUGIN_ROOT.parent))

from astrbot_plugin_youtube_notifier.services.notifier import (  # noqa: E402
    NotificationService,
    SendOutcome,
    classify_send_error,
)
from astrbot_plugin_youtube_notifier.services.models import (  # noqa: E402
    TYPE_LIVE_END,
    TYPE_LIVE_START,
    TYPE_NEW_VIDEO,
    Notification,
)


# 用户日志里的真实异常文本（换行、超长都保留了）
NAPCAT_TIMEOUT_TEXT = (
    "Timeout: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg "
    "ListenerName:NodeIKernelMsgListener/onMsgInfoListUpdate EventRet:\n{}\n"
)


class ActionFailed(Exception):
    """冒充 aiocqhttp.exceptions.ActionFailed（结构相同：带 retcode）。"""

    def __init__(self, message: str, retcode: int = 1200):
        super().__init__(message)
        self.retcode = retcode


class FakeRenderer:
    def __init__(self, fail: bool = False):
        self.fail = fail

    async def render(self, data: dict) -> str:
        if self.fail:
            raise RuntimeError("字体炸了")
        return "C:/tmp/fake_notif.png"


class FakeStore:
    def __init__(self, sessions=("sess:1",)):
        self._sessions = list(sessions)

    def sessions_for_channel(self, cid):
        return self._sessions

    def set_channel_name(self, cid, name):
        pass

    async def save(self):
        pass


class FakeContext:
    """send_message 按预设行为抛异常或成功。"""

    def __init__(self, exc: Exception | None = None):
        self.exc = exc
        self.sent: list = []

    async def send_message(self, session, chain):
        if self.exc is not None:
            raise self.exc
        self.sent.append((session, chain))
        return True


def _notifier(context, renderer, store=None, **kwargs):
    kwargs.setdefault("cover_download", False)
    return NotificationService(
        context=context,
        store=store or FakeStore(),
        renderer=renderer,
        **kwargs,
    )


def _notification() -> Notification:
    return Notification(
        type=TYPE_NEW_VIDEO,
        title="测试标题",
        channel_id="UC1",
        channel_name="测试频道",
        url="https://www.youtube.com/watch?v=x",
        video_id="x",
    )


class _LogCapture:
    """捕获指定 logger 的 WARNING 记录数。"""

    def __init__(self, logger_name: str = "astrbot_test"):
        self.records: list[logging.LogRecord] = []
        self._logger = logging.getLogger(logger_name)
        self._handler = logging.Handler()
        self._handler.emit = self.records.append

    def __enter__(self):
        self._logger.addHandler(self._handler)
        self._logger.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        self._logger.removeHandler(self._handler)
        return False

    @property
    def warnings(self) -> list[logging.LogRecord]:
        return [r for r in self.records if r.levelno >= logging.WARNING]


# ---------------------------------------------------------------- 分类


def test_napcat_timeout_is_uncertain_not_failure() -> None:
    """NapCat 的 sendMsg 超时：消息实际已送达，不能判为失败。"""
    uncertain, reason = classify_send_error(ActionFailed(NAPCAT_TIMEOUT_TEXT))
    assert uncertain is True, "NapCat 超时应判为「可能已送达」"
    # 原因要压成一行（原始文本含换行，直接进日志/聊天会很难看）
    assert "\n" not in reason, f"原因未压平: {reason!r}"
    assert "Timeout" in reason
    print("✅ test_napcat_timeout_is_uncertain_not_failure")


def test_hard_failures_are_not_marked_uncertain() -> None:
    """确认失败不能被当成「可能已送达」，否则会掩盖真问题。"""
    cases = [
        ActionFailed("Retcode 100: 机器人已离线"),
        ActionFailed("not found: 群不存在", retcode=102),
        ConnectionRefusedError("[Errno 111] Connection refused"),
        RuntimeError("群 12345 不存在或机器人不在群内"),
        PermissionError("no permission to speak"),
        ValueError("不合法的 session 字符串"),
    ]
    for exc in cases:
        uncertain, reason = classify_send_error(exc)
        assert uncertain is False, f"{type(exc).__name__} 被误判为可能已送达"
        assert reason, "必须给出原因"
    print(f"✅ test_hard_failures_are_not_marked_uncertain ({len(cases)} 个用例)")


def test_long_error_is_truncated() -> None:
    _, reason = classify_send_error(RuntimeError("x" * 500))
    assert len(reason) <= 161, f"未截断: {len(reason)}"
    assert reason.endswith("…")
    print("✅ test_long_error_is_truncated")


def test_send_outcome_states() -> None:
    ok = SendOutcome(image_path="p", delivered=True)
    assert ok.ok and ok.describe() == "已送达"

    un = SendOutcome(image_path="p", uncertain=True, error="Timeout…")
    assert un.ok, "不确定时对用户算成功（避免误导去排查）"
    assert "超时" in un.describe() and "可能已送达" in un.describe()

    bad = SendOutcome(error="连接被拒绝")
    assert not bad.ok
    assert "失败" in bad.describe()

    # 内容生成失败：未进入发送阶段
    failed = SendOutcome(error="通知内容生成失败")
    assert failed.image_path is None and failed.rendered is False
    print("✅ test_send_outcome_states")


# ---------------------------------------------------------------- 端到端


def test_dispatch_test_reports_uncertain() -> None:
    """测试命令遇到 NapCat 超时：要报「可能已送达」，不能报失败。"""
    ctx = FakeContext(ActionFailed(NAPCAT_TIMEOUT_TEXT))
    notifier = _notifier(ctx, FakeRenderer())
    outcome = asyncio.run(notifier.dispatch_test("sess:1", _notification()))

    assert outcome.image_path, "渲染是成功的，应保留图片路径"
    assert outcome.uncertain is True
    assert outcome.delivered is False
    assert outcome.ok is True, "ok 必须为 True —— 用户其实收到了图"
    print("✅ test_dispatch_test_reports_uncertain")


def test_dispatch_test_reports_hard_failure() -> None:
    ctx = FakeContext(ConnectionRefusedError("[Errno 111] Connection refused"))
    outcome = asyncio.run(
        _notifier(ctx, FakeRenderer()).dispatch_test("sess:1", _notification())
    )
    assert outcome.image_path, "渲染成功过，路径仍在"
    assert outcome.uncertain is False and outcome.delivered is False
    assert outcome.ok is False, "确认失败必须是失败"
    print("✅ test_dispatch_test_reports_hard_failure")


def test_dispatch_test_render_failure_has_no_path() -> None:
    outcome = asyncio.run(
        _notifier(FakeContext(), FakeRenderer(fail=True)).dispatch_test(
            "sess:1", _notification()
        )
    )
    assert outcome.image_path is None
    assert outcome.ok is False
    assert outcome.error, "渲染失败要有原因"
    print("✅ test_dispatch_test_render_failure_has_no_path")


def test_text_notification_format_and_send() -> None:
    """文字模式应跳过图片渲染，按配置直接发送 Plain 文本。"""
    ctx = FakeContext()
    notifier = _notifier(ctx, FakeRenderer(fail=True), notify_style="text")
    notification = _notification()
    notification.start_time = "2026-09-18T15:03:00+00:00"
    asyncio.run(notifier.dispatch("UC1", [notification]))

    assert len(ctx.sent) == 1
    chain = ctx.sent[0][1].chain
    assert chain[0][0] == "text"
    text = chain[0][1]
    assert "📺 测试频道 发布了新视频" in text
    assert "标题: 测试标题" in text
    assert "开始: " in text
    assert "链接: https://www.youtube.com/watch?v=x" in text
    print("✅ test_text_notification_format_and_send")


def test_text_formats_all_notification_types() -> None:
    notifier = _notifier(FakeContext(), FakeRenderer(), notify_style="text")
    start = Notification(
        type=TYPE_LIVE_START, channel_name="白上フブキ", title="【歌】",
        start_time="2026-09-18T15:03:00+00:00", url="https://youtu.be/start",
    )
    end = Notification(
        type=TYPE_LIVE_END, channel_name="白上フブキ", title="【歌】",
        start_time="2026-09-18T15:03:00+00:00",
        end_time="2026-09-18T16:03:00+00:00", duration_seconds=3600,
        url="https://youtu.be/end",
    )
    assert notifier._format_text(start).splitlines()[0] == "🔴 白上フブキ 开播了"
    end_text = notifier._format_text(end)
    assert end_text.splitlines()[0] == "⚫ 白上フブキ 下播了"
    assert "开始: " in end_text and "结束: " in end_text and "时长: 1:00:00" in end_text

    no_time = _notification()
    no_time.start_time = "not-an-iso-time"
    assert not any(line == "开始: " for line in notifier._format_text(no_time).splitlines())
    print("✅ test_text_formats_all_notification_types")


def test_text_test_notification_has_prefix() -> None:
    ctx = FakeContext()
    outcome = asyncio.run(
        _notifier(ctx, FakeRenderer(fail=True), notify_style="text").dispatch_test(
            "sess:1", _notification()
        )
    )
    assert outcome.delivered and outcome.rendered
    assert outcome.image_path is None
    assert ctx.sent[0][1].chain[0][1].startswith("🧪 [测试]\n")
    print("✅ test_text_test_notification_has_prefix")


def test_text_title_is_truncated() -> None:
    notifier = _notifier(FakeContext(), FakeRenderer(), notify_style="text")
    notification = _notification()
    notification.title = "长" * 101
    text = notifier._format_text(notification)
    title_line = next(line for line in text.splitlines() if line.startswith("标题: "))
    assert title_line == "标题: " + "长" * 100 + "…"
    print("✅ test_text_title_is_truncated")


def test_dispatch_timeout_warns_once() -> None:
    """真实推送里的超时只告警一次，之后降 debug —— 否则每条通知都刷 WARN。"""
    ctx = FakeContext(ActionFailed(NAPCAT_TIMEOUT_TEXT))
    notifier = _notifier(ctx, FakeRenderer())
    notif = _notification()

    with _LogCapture() as cap:
        for _ in range(4):
            asyncio.run(notifier.dispatch("UC1", [notif]))
        assert len(cap.warnings) == 1, (
            f"应只告警 1 次，实际 {len(cap.warnings)} 次（会刷屏）"
        )
    print("✅ test_dispatch_timeout_warns_once")


def test_dispatch_hard_failure_always_warns() -> None:
    """确认失败每次都该告警（那是真问题，不能被折叠掉）。"""
    ctx = FakeContext(RuntimeError("群不存在"))
    notifier = _notifier(ctx, FakeRenderer())
    notif = _notification()
    with _LogCapture() as cap:
        for _ in range(3):
            asyncio.run(notifier.dispatch("UC1", [notif]))
        assert len(cap.warnings) == 3, f"实际 {len(cap.warnings)} 次"
    print("✅ test_dispatch_hard_failure_always_warns")


def test_dispatch_success_does_not_warn() -> None:
    ctx = FakeContext()
    notifier = _notifier(ctx, FakeRenderer())
    with _LogCapture() as cap:
        asyncio.run(notifier.dispatch("UC1", [_notification()]))
        assert cap.warnings == [], "成功时不该有告警"
    assert len(ctx.sent) == 1, "消息应已发出"
    print("✅ test_dispatch_success_does_not_warn")


def test_no_retry_on_uncertain_send() -> None:
    """图片/文字通知超时后都绝不能重试，且重复超时只首次 WARN。"""
    for style in ("image", "text"):
        ctx = FakeContext(ActionFailed(NAPCAT_TIMEOUT_TEXT))
        notifier = _notifier(ctx, FakeRenderer(), notify_style=style)
        calls = {"n": 0}
        original = ctx.send_message

        async def counting(session, chain):
            calls["n"] += 1
            return await original(session, chain)

        ctx.send_message = counting
        with _LogCapture() as cap:
            for _ in range(2):
                asyncio.run(notifier.dispatch("UC1", [_notification()]))
        assert calls["n"] == 2, (
            f"style={style} 单次通知发生重试！2 条通知共调用 {calls['n']} 次"
        )
        assert len(cap.warnings) == 1, (
            f"style={style} 超时应只首次 WARN，实际 {len(cap.warnings)} 次"
        )
    print("✅ test_no_retry_on_uncertain_send (image + text)")


def main() -> int:
    tests = [
        test_napcat_timeout_is_uncertain_not_failure,
        test_hard_failures_are_not_marked_uncertain,
        test_long_error_is_truncated,
        test_send_outcome_states,
        test_dispatch_test_reports_uncertain,
        test_dispatch_test_reports_hard_failure,
        test_dispatch_test_render_failure_has_no_path,
        test_text_notification_format_and_send,
        test_text_formats_all_notification_types,
        test_text_test_notification_has_prefix,
        test_text_title_is_truncated,
        test_dispatch_timeout_warns_once,
        test_dispatch_hard_failure_always_warns,
        test_dispatch_success_does_not_warn,
        test_no_retry_on_uncertain_send,
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
