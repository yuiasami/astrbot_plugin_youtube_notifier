# astrbot_plugin_youtube_notifier

AstrBot 的 YouTube 订阅提醒插件：订阅频道后，**直播上播 / 下播**与**新投稿**会按配置以
**图片通知**（Pillow 渲染的深色卡片，默认）或**纯文字通知**推送到会话，订阅按会话隔离。

直接给 `@handle` 就能订阅，例如 `/yt订阅 @ukaisaki`。

> 文档：[实现计划](PLAN.md)｜[项目约束](CLAUDE.md)｜[API 接入指南](API_GUIDE.md)｜[更新日志](CHANGELOG.md)

## 功能

- 🔴 **上播提醒**：检测到频道开播即推送（标题 + 封面 + 开始时间）
- ⚫ **下播提醒**：直播结束后推送（含**直播时长**，取 YouTube 的实际起止时间）
- 📺 **新投稿提醒**：新视频发布推送（标题 + 封面）
- 🔒 **会话隔离**：每个会话（群/私聊）的订阅互不影响
- 🖼️ **通知样式可切换**：默认渲染为图片，也可切换为纯文字通知（无需字体与封面下载）

## 安装

1. 把本目录放入 AstrBot 的 `data/plugins/`，重启或在 WebUI 重载插件
2. **配置 `api_key`**（见下，必填）
3. `/yt订阅 @handle` 开始使用

依赖 `aiohttp`（`requirements.txt` 声明，AstrBot 自动安装；Pillow 已随 AstrBot 提供）。

## 必须先配 API Key

① 到 [Google Cloud Console](https://console.cloud.google.com/) 免费申请：

- 新建项目 → **API 和服务 → 库** → 启用 **YouTube Data API v3**
- **凭据 → 创建凭据 → API 密钥** → 复制 `AIza...`
- 无需 OAuth、无需绑卡

② 填进插件配置的 **`api_key`**，或先验证：

```bash
python scripts/diagnose.py @ukaisaki --api-key AIza...
```

> **为什么必须配？** YouTube 的 Atom feed（`feeds/videos.xml`）自 2025 年底起对自动化
> 请求间歇性返回 404/500，实测 YouTube 官方频道也会 404，已无法作为主数据源。
> 官方 Data API 稳定且只需一个免费 API Key。详见 [API_GUIDE.md](API_GUIDE.md)。

## 指令

| 指令 | 说明 |
|---|---|
| `/yt订阅 @handle` | 订阅频道，也接受频道ID / 频道URL |
| `/yt取消订阅 @handle` | 取消本会话的订阅（同样接受 ID / URL）|
| `/yt批量订阅 <目标> <目标>...` | 一次订阅多个频道，目标之间用**空格**分隔 |
| `/yt批量取消订阅 <目标>...` | 一次取消多个订阅，目标之间用**空格**分隔 |
| `/yt列表` | 查看本会话订阅与频道状态 |
| `/yt直播测试 <目标>` | 抓目标当前直播，按当前样式推送一条**测试通知** |
| `/yt视频测试 <目标>` | 抓目标最新视频，按当前样式推送一条**测试通知** |

支持的输入形式：`@ukaisaki`、`ukaisaki`、`https://www.youtube.com/@ukaisaki`、
`https://www.youtube.com/channel/UC...`、`UCxxxxxxxxxxxxxxxxxxxxxx`。

### 批量指令

```bash
/yt批量订阅 @ukaisaki @NASA UCxxxxxxxxxxxxxxxxxxxxxx
/yt批量取消订阅 @ukaisaki @NASA
```

- 目标之间用空格分隔；`/yt批量取消订阅` 额外接受**频道名**（如 `NASA`）。
- 单条消息最多处理 20 个目标；超出的部分会在回复里**逐个列出**说未处理，
  不会静默丢弃。想订阅更多就分几条发。
- 回复按结果分组：✅ 新增 / ⏭️ 已订阅 / ❌ 失败（附原因），
  失败原因会精确到是「未找到频道」还是「API 配额耗尽」。
- 取消订阅优先在**本地已订阅列表**里匹配（频道ID → @handle → 频道名），
  匹配不到才会查一次 API，因此批量取消基本不消耗配额。
- 频道名里含空格时（例如 `Rurudo Lion`）请改用 `@handle` 或频道ID ——
  空格是分隔符。

### 测试指令

`/yt直播测试` 与 `/yt视频测试` 走的是与真实推送**完全相同**的抓取 → 内容生成 →
推送链路；图片模式在图上标记「🧪 测试」，文字模式在首行标记 `🧪 [测试]`，且均不触碰任何去重状态。

```bash
/yt直播测试 @NASA                                      # 该频道当前直播
/yt视频测试 @MrBeast                                   # 该频道最新视频
/yt视频测试 https://www.youtube.com/watch?v=gTKS8SAwUzE  # 指定视频
```

用途：确认当前样式的内容生成与推送是否正常；图片模式还会覆盖字体、封面下载与适配器发图。
目标当前没有直播时，回复会**明确说明**测试通知借用的是哪条内容，不会假装是直播。

## 配额与轮询间隔

免费额度 **10,000 单位/天**，每频道每轮约消耗 **2 单位**（`channels.list` 只在订阅时消耗一次并缓存）。

| 轮询间隔 | 可支撑频道数 |
|---|---|
| 60 秒 | ≈ 3 |
| 3 分钟 | ≈ 10 |
| **5 分钟（默认）** | **≈ 17** |
| 10 分钟 | ≈ 34 |

## 配置项

| 配置 | 默认 | 说明 |
|---|---|---|
| `api_key` | — | **强烈建议填**，YouTube Data API Key（不填会走网页兜底） |
| `live_detect_mode` | `data_api` | `data_api` / `livebroadcasts` / `feed` / `auto` |
| `page_fallback_enabled` | `true` | **配额耗尽/请求失败时自动改用网页 JSON** |
| `page_fallback_min_interval_seconds` | `60` | 网页兜底同频道最小抓取间隔 |
| `poll_interval_seconds` | `300` | 轮询间隔，见上表 |
| `max_results` | `5` | 每轮拉取最近多少条视频（1-50） |
| `proxy` | — | 代理，如 `http://127.0.0.1:7890`（大陆网络通常需要） |
| `cover_download` | `true` | 图片模式下是否下载封面到通知图；文字模式不生效 |
| `notify.style` | `image` | `image`=图片通知（默认）；`text`=纯文字通知，不下载封面、不渲染图片 |
| `cleanup.enabled` | `true` | **每天定时清理通知图**（见下） |
| `cleanup.retention_days` | `7` | 删除超过该天数的图片（`0` = 不按天数删） |
| `cleanup.max_total_mb` | `500` | 图片总量上限，超出则从最旧开始删（`0` = 不限） |
| `cleanup.hour` | `4` | 每天几点清理（本地时间，0-23） |
| `cleanup.run_on_startup` | `true` | 启动后也补清理一次 |
| `render.image_width` | `800` | 通知图宽度（像素） |
| `render.font_path` | 自动 | 中文字体路径。**建议留空**自动探测；Docker 下填宿主机路径无效，见「常见问题」 |
| `notify.*_enabled` | 全开 | 分别开关上播/下播/新投稿通知 |
| `oauth.*` | — | 仅 `livebroadcasts` 模式需要 |
| `websub.*` | 关闭 | ⚠️ 依赖已不可靠的 feed，不建议启用 |

### 图片会自动清理

每张通知图约 **300–700KB**（含封面），每次推送都新建一个文件、从不复用。
按 5 分钟轮询 + 几个频道估算，不清理的话磁盘只会单调增长直到写满——
所以默认**每天凌晨 4 点**清理一次，两条策略同时生效：

- **按年龄**：删除超过 `retention_days`（默认 7 天）的图；
- **按总量**：清理后若仍超过 `max_total_mb`（默认 500MB），从最旧的继续删到限额内。

无论哪条策略，都**不会删除最近 1 小时内的文件**（可能还在发送队列里），
也**只删通知图与封面目录里的图片**，不碰 `state.json` 等其它文件。

> bot 若每天重启，可能永远赶不上凌晨 4 点，因此启动约 30 秒后也会补清理一次
> （`run_on_startup`，默认开）。不想自动清理就设 `cleanup.enabled=false`，
> 但请自行确保磁盘不会写满。

### 自动降级（配额用完也不会断）

```
Data API 配额耗尽 / 请求失败 / API Key 无效 / 未配置 Key
    → 网页 JSON（抓频道页 ytInitialData）⭐ 实测可用
    → legacy Atom feed（仅当网页也失败）
```

降级**不是静默的**：`/yt列表` 会显示 `⚠️ 数据源已降级: …`，订阅回复也会提示。

网页兜底的代价（已实测，心里有数即可）：

- 直播只在 `/streams` 标签页有 → 每次检查抓 **2 个页面**，约 **2.4MB**
- **没有精确时间戳**，时间由 "6 days ago" 这类相对文案换算，仅够排序展示
- 直播的**时长不准确**（网页不给实际开始/结束时间）
- 未配置 Key 时自动使用，所以**不配 Key 也能用**（只是不如官方 API 稳）

## 开发

```bash
python tests/test_imports.py        # 包导入冒烟 + 配置 schema 校验 + 就绪语义矩阵
python tests/test_state_machine.py  # 状态机 + feed 解析（含真实 feed 回归）
python tests/test_store.py          # 会话隔离 + 持久化
python tests/test_data_api.py       # Data API 解析与快照组装（mock HTTP）
python tests/test_page_json.py      # 网页 JSON 解析 + 降级链（含真实页面回归）
python tests/test_cleanup.py        # 图片清理：年龄/总量策略 + 误删防护
python tests/test_notifier_send.py  # 推送结果分类（适配器超时 ≠ 推送失败）
python tests/test_batch_commands.py # 批量订阅/取消订阅：参数切分 + 上限 + 本地匹配
```

测试全部离线，不依赖 AstrBot 运行时与网络。

### 排查

```bash
python scripts/diagnose.py @handle --api-key AIza...   # 完整数据源诊断
python scripts/diagnose.py --check-fonts                # 只体检中文字体（无需网络）
python scripts/diagnose.py --font-path /path/to/x.ttf   # 验证配置的 font_path 在本环境是否可用
python scripts/diagnose.py @handle --check-page         # 只体检网页兜底（无需 Key）
python scripts/diagnose.py @handle --check-feed         # 顺带体检 legacy feed
python scripts/diagnose.py --file feed.xml              # 离线解析本地 XML
```

> Docker 部署时，这些命令要在**容器内**执行才会看到容器真实情况：
> `docker exec -it astrbot python /AstrBot/data/plugins/astrbot_plugin_youtube_notifier/scripts/diagnose.py --check-fonts`

> 诊断脚本需在**能访问 YouTube** 的机器上运行（通常是 VPS）。

## 架构

```
main.py                 # Star 插件：指令 / 生命周期 / 装配
renderer.py             # Pillow 文生图通知（三模板）
utils.py                # 重试退避 / 时间 / 字体 / emoji / 换行
services/
  data_api.py           # ★ 主数据源：Data API v3（API Key）+ @handle 解析 + 配额计数
  page_json.py          # ★ 网页 JSON 兜底：频道页 ytInitialData 抓取与解析
  feed.py               # legacy Atom feed（⚠️ 端点已不可靠，仅最后兜底）
  livebroadcasts.py     # LiveBroadcasts API（OAuth，仅自己的频道）
  scrape.py             # 频道页 HTML 正则（handle 解析兜底）
  oauth.py              # OAuth token 管理与 device flow
  models.py             # 数据模型
  store.py              # 订阅存储（会话隔离 + JSON 持久化）
  state_machine.py      # 直播状态机（纯逻辑）
  poller.py             # asyncio 后台轮询
  notifier.py           # 检测 → 图片渲染/文字格式化 → 推送 + 降级链
  cleanup.py            # 通知图定时清理（按年龄 + 按总量）
  websub*.py            # WebSub 推送（默认关闭）
```

数据保存在插件数据目录 `data/`（已 gitignore）：`state.json` 与 `images/`。

## 常见问题

### 日志报「推送适配器上报超时」/ 测试命令说推送失败，但通知其实收到了

**这是适配器的「假失败」，不是本插件的问题。** NapCat（QQ NT）适配器在发送后会等待
`onMsgInfoListUpdate` 事件，该事件超时就会抛：

```
ActionFailed retcode=1200
Timeout: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg
ListenerName:NodeIKernelMsgListener/onMsgInfoListUpdate
```

而消息**实际已经送达**。插件对此的处理是：

- **不重试** —— 重试会让用户收到两条一样的通知；
- 如实报告「适配器上报超时、消息可能已送达」，而不是断言失败；
- 真实推送里这类超时**只告警一次**，之后降为 debug，不会每条通知刷一条 WARN。

所以看到这条日志时：先确认是不是真的收到了通知。收到了就无需处理；
若频繁出现且影响使用，可从 NapCat 侧排查（例如降低发送频率、升级 NapCat 版本）。


### 图片模式下，VPS 上图里中文全是方框（豆腐块）

**原因**：插件找不到中文字体。Linux 最小化安装通常自带 DejaVuSans（纯拉丁、
不含任何中文字形），拿它渲染就会把所有中文变成 `□`。

**修复**（任选其一，装完重载插件）：

```bash
# Debian / Ubuntu
apt-get install -y fonts-noto-cjk

# CentOS / RHEL / Fedora
dnf install -y google-noto-sans-cjk-fonts

# Alpine
apk add font-noto-cjk

# Arch
pacman -S noto-fonts-cjk
```

或者把任意中文字体文件传到服务器，将配置项 `render.font_path` 设为它的
绝对路径（例如 `/opt/fonts/msyh.ttc`）。

**确认是否修好**：

```bash
fc-list :lang=zh | head          # 应列出中文字体
python scripts/diagnose.py --check-fonts   # 会打印判定结果并生成一张测试图
```

> 插件启动时会自己检测中文字体：缺失时日志里会打出带安装命令的 ERROR，
> `/yt列表` 也会在聊天里提示 —— 不会让你对着方框猜原因。

### Docker 部署：配了 `font_path` 却提示「不存在」

**这不是路径写错，而是容器看不到宿主机的文件。** 插件跑在容器里，
`font_path` 的检查是在**容器自己的文件系统**内做的；你在宿主机上
`apt install` 的字体，容器里默认没有。

> 官方镜像 `soulter/astrbot:latest` 的 Dockerfile 里**已经装了
> `fonts-noto-cjk`**，所以最简单的是**直接清空 `render.font_path`**，
> 插件会自动找到容器内的 Noto CJK —— 通常根本不用手动指定字体。

想用自己装的字体（比如 Maple Mono NF CN），从下面任选一种：

**① 先确认容器里到底有什么**（在宿主机上执行）：

```bash
docker exec -it astrbot fc-list :lang=zh | head
docker exec -it astrbot ls /usr/share/fonts/opentype/noto/
docker exec -it astrbot python /AstrBot/data/plugins/astrbot_plugin_youtube_notifier/scripts/diagnose.py --check-fonts
```

最后一条会直接告诉你容器内的判定结果，并生成一张测试图。

**② 把宿主机字库挂载进容器** —— 在 `docker-compose.yml` 里加一行：

```yaml
    volumes:
      - ./data:/AstrBot/data
      - /usr/share/fonts:/usr/share/fonts:ro      # ← 新增：让容器看见宿主机字库
```

然后 `docker compose up -d` 重建容器，原来那个路径就生效了。

**③ 把字体丢进插件 data 目录**（推荐，无需改 compose、重启也不丢）：

AstrBot 的 `data/` 已被挂载到宿主机，所以把字体文件放到：

```
<宿主机 AstrBot 目录>/data/plugin_data/astrbot_plugin_youtube_notifier/fonts/
```

容器内对应 `/AstrBot/data/plugin_data/astrbot_plugin_youtube_notifier/fonts/`。
放进这个目录的字体**优先于系统字体**被采用，`render.font_path` 留空即可。

## License

见 [LICENSE](LICENSE)。
