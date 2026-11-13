![:name](https://count.getloli.com/@astrbot-forward-media-saver?name=astrbot-forward-media-saver&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

# 合并转发媒体保存（astrbot_plugin_forward_media_saver）

**⚠️暂时没有实现文件和语音的保存**，图片和视频可以正常使用

自动保存 QQ 群聊 / 私聊里**别人发送的合并转发**中的图片、视频，可选保存语音和文件附件。

插件监听 aiocqhttp（OneBot v11）消息，发现合并转发后通过 `get_forward_msg`
取回节点内容，递归解析嵌套转发，把媒体下载到 AstrBot 的 `data/plugin_data`
目录，并提供 SQLite 索引、`/fms` 管理命令和 WebUI 统计页。

## 功能

- 自动监听，不需要指令；只处理合并转发，不碰普通聊天里的媒体。
- 跳过机器人自己发送的合并转发。
- 群聊、私聊、图片、视频、语音、文件分别开关。
- 支持嵌套合并转发，默认展开 3 层。
- 兼容 go-cqhttp / Lagrange / NapCat / SnowLuma 等常见返回结构：
  `messages`、`message`、`node.data.content`、外层 `data` 包装、CQ 码字符串。
- 识别 `url` 里的 HTTP 地址和协议端本地路径；本地路径存在时直接复制。
- URL/路径失败时，用节点 `message_id` 调 `get_msg` 刷新，再尝试
  `get_file`（图片还会尝试 `get_image`），最后按文件名搜索 `search_dirs`。
- 媒体类型按文件头识别；音频识别 MP3/WAV/AMR/OGG/FLAC 等常见头。
- 可选跳过动画表情/商城表情/GIF 表情包。
- SQLite 媒体索引 + `manifest.jsonl` 清单，支持统计、最近记录、导出和清理。
- 失败/中断任务持久化，`/fms retry` 可重试；任务有最大尝试次数。
- 可配置目录模板和文件名模板。
- 可选 URL 域名白名单和内网地址拦截。
- 耗时操作在后台任务中执行，不阻塞 AstrBot 的 LLM 回复。
- 可选在来源会话回复保存摘要（默认关闭）。
- WebUI 可视化页面：统计、按类型汇总、最近记录、清理。
- 插件卸载/重载时取消任务、关闭 HTTP 会话和数据库连接。

## 兼容性

| 项目 | 说明 |
| --- | --- |
| 平台适配器 | 仅 `aiocqhttp`（OneBot v11） |
| 协议端 | NapCat、SnowLuma、Lagrange、go-cqhttp 等支持 `get_forward_msg` 的实现 |
| QQ 官方机器人 | 不支持（官方 API 收不到合并转发节点） |
| 私聊合并转发 | 取决于协议端是否支持 |
| AstrBot | `>=4.5,<5`；WebUI 页面建议 4.13+ |

## 安装

1. 把整个目录放到 `AstrBot/data/plugins/astrbot_plugin_forward_media_saver/`。
2. WebUI → 插件 → 重载插件。
3. 按需修改配置；默认即可使用。
4. 让别人发一条带媒体的合并转发，查看日志和保存目录。

插件只依赖 AstrBot 自带的 `aiohttp`，不需要额外安装依赖。

## 管理命令

需要 AstrBot 管理员权限：

```text
/fms stats [天数]   保存统计（默认全部）
/fms last [条数]    当前会话最近保存记录
/fms path           媒体目录、索引、清单路径
/fms clean [天数]   清理旧文件（默认 retention_days）
/fms retry          重试当前会话失败/中断的任务
/fms export         导出 manifest.jsonl
/fms help           帮助
```

## WebUI 页面

插件目录下的 `views/dashboard/` 提供统计页，可查看：

- 记录数、总大小、任务状态；
- 图片/视频/语音/文件分别的数量和空间；
- 最近保存记录；
- 按天数清理旧文件。

页面通过 AstrBot plugin bridge 调用插件后端 API；不支持插件页面的旧版本会自动跳过注册。

## 配置项

配置文件在 `data/config/astrbot_plugin_forward_media_saver_config.json`。

| 配置 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | 总开关 |
| `save_images` | bool | `true` | 保存图片 |
| `save_videos` | bool | `true` | 保存视频 |
| `save_audio` | bool | `false` | 保存语音 |
| `save_files` | bool | `false` | 保存文件附件 |
| `skip_emoji` | bool | `false` | 跳过动画表情/GIF 表情 |
| `save_group` | bool | `true` | 群聊开关 |
| `save_private` | bool | `true` | 私聊开关 |
| `group_whitelist` | list | `[]` | 群号白名单 |
| `group_blacklist` | list | `[]` | 群号黑名单 |
| `user_blacklist` | list | `[]` | 发送者黑名单 |
| `max_depth` | int | `3` | 嵌套转发展开层数 |
| `max_media_per_forward` | int | `100` | 单条转发媒体上限 |
| `max_file_mb` | int | `200` | 单文件大小上限 |
| `min_video_seconds` | int | `0` | 视频最短时长（秒），0 不过滤 |
| `skip_unknown_video_duration` | bool | `false` | 无法识别时长时是否跳过 |
| `video_probe_timeout` | int | `10` | ffprobe 探测时长超时 |
| `min_free_mb` | int | `200` | 磁盘最低剩余空间 |
| `api_timeout` | int | `30` | 普通协议端接口超时 |
| `file_api_timeout` | int | `300` | `get_file` 超时 |
| `download_timeout` | int | `60` | 媒体下载超时 |
| `concurrency` | int | `2` | 同时处理的任务数 |
| `retries` | int | `2` | 下载重试次数 |
| `dedupe` | bool | `false` | SHA-256 跨消息去重 |
| `folder_layout` | string | `date_scope` | 旧版目录结构 |
| `custom_dir` | string | `""` | 自定义保存根目录 |
| `folder_template` | string | `""` | 目录模板，留空用 `folder_layout` |
| `filename_template` | string | `""` | 文件名模板，留空用默认命名 |
| `timezone` | string | `Asia/Shanghai` | 日期/时间模板与旧版目录使用的时区 |
| `search_dirs` | list | `[]` | 协议端缓存搜索目录 |
| `save_manifest` | bool | `true` | 写入 `manifest.jsonl` |
| `retention_days` | int | `0` | 默认保留天数，0 不自动清理 |
| `max_total_mb` | int | `0` | 保存目录总量上限，0 不限制 |
| `max_attempts` | int | `3` | 失败任务最大尝试次数 |
| `block_private_urls` | bool | `false` | 阻止内网/回环 URL |
| `allowed_media_hosts` | list | `[]` | 媒体域名白名单 |
| `notify` | bool | `false` | 回复保存摘要 |
| `notify_empty` | bool | `false` | 没有媒体也回复摘要 |

### 目录结构

默认：

```text
data/plugin_data/astrbot_plugin_forward_media_saver/
├── media/
│   └── 2026-10-23/group_123456/
├── index.db
├── manifest.jsonl
└── export_manifest.jsonl
```

`folder_layout`：

| 值 | 示例 |
| --- | --- |
| `date_scope`（默认） | `2026-10-23/group_123456/` |
| `date` | `2026-10-23/` |
| `scope` | `group_123456/` |
| `flat` | `media/` 根目录 |

### 模板

`folder_template` / `filename_template` 可用：

```text
{date} {year} {month} {day} {time} {datetime} {timestamp}
{media_date} {media_year} {media_month} {media_day}
{media_time} {media_datetime} {media_timestamp}
{scope} {scope_id} {scope_name} {kind}
{sender} {sender_name} {seq} {hash} {orig}
```

- `{date}`/`{time}` 等：**保存时间**，按 `timezone` 计算；
- `{media_date}`/`{media_time}` 等：**转发节点原始时间**，缺失时回退保存时间；
- `timezone` 默认 `Asia/Shanghai`；留空使用容器本地时区。Docker 默认 UTC 时
  不设置它会出现“日期差一天”；
- `{scope_name}`：群名；私聊或群名缺失时回退为群号/QQ 号；
- `{seq}`：本条合并转发内媒体序号，两位数字；
- `{hash}`：文件 SHA-256 前 8 位；
- `{orig}`：原始文件名（不含扩展名）；
- 扩展名由插件识别后自动追加，模板里不要写 `.jpg`/`.mp4`。

例如：

```json
"folder_template": "{year}/{month}/{scope_name}",
"filename_template": "{kind}_{sender}_{seq}_{hash}"
```

结果类似：

```text
media/2026/10/测试群/video_10001_03_ab12cd34.mp4
```

如果想按媒体原始日期归档：

```json
"folder_template": "{media_year}/{media_month}/{scope_name}"
```

中文群名、中文发送者名会保留；路径分隔符、`..`、控制字符会被清理，单个片段有长度和 UTF-8 字节上限。

## 媒体类型

- `image`：JPEG/PNG/GIF/WebP/BMP/TIFF/HEIC/AVIF 等按文件头识别。
- `video`：MP4/MOV/WebM/MKV/AVI/FLV 等按文件头识别。
- `audio`：MP3/WAV/AMR/OGG/FLAC/SILK 等，按文件头 + 文件名/Content-Type。
- `file`：文件附件，按文件名扩展名保存；默认关闭。

图片/视频会拒绝 HTML 错误页和空文件；文件附件不做媒体嗅探，但仍受大小/空间限制。

## 视频时长过滤

只想保存“有点内容”的视频时：

```json
"min_video_seconds": 3
```

处理顺序：

1. 视频段自身带 `duration`/`video_time` 等字段时，先判断，不满足直接跳过，不下载；
2. 没有时长字段时，下载到临时文件后用 `ffprobe` 探测；
3. 没有 `ffprobe` 时，对 MP4/MOV/M4V 用内置 `mvhd` 解析；
4. 仍无法识别时：
   - `skip_unknown_video_duration=false`（默认）：保存；
   - `true`：跳过。

探测超时由 `video_probe_timeout` 控制，默认 10 秒。短于阈值的视频不会写入索引/清单。

## 索引、清单与任务

- `index.db`：SQLite，保存媒体记录、去重 hash、任务状态。
- `manifest.jsonl`：每次保存追加一条 JSONL，便于外部处理。
- `/fms export`：生成 `export_manifest.jsonl`。
- 旧版 `seen_hashes.txt` 会在首次初始化时迁移到 SQLite。
- 任务状态：`pending` / `running` / `done` / `failed`。
- 插件重载后，`running` 会重置为 `pending`；在对应会话执行 `/fms retry` 继续。

## 安全

- 文件名/目录模板清理危险字符，清理操作只删除保存根目录内的文件。
- 管理命令需要管理员权限。
- 可选 `block_private_urls`：拒绝 localhost、内网、回环、链路本地地址。
- 可选 `allowed_media_hosts`：只允许指定域名后缀的 HTTP 媒体 URL。
- 本地路径只读取协议端给出的路径；如果协议端不可信，请限制 `search_dirs` 和挂载范围。

## NapCat / SnowLuma 说明

- SnowLuma 复用 OneBot v11，插件无需改代码；但它的数据目录与 NapCat 不同。
  SnowLuma Docker 常用卷是 `/app/data`、`/app/.config`、`/app/.local/share`；
  插件会默认尝试这些目录，找不到时请把对应卷挂给 AstrBot 并配置 `search_dirs`。
- NapCat 的 `get_file` 对合并转发内视频可能在 `downloadRichMedia` 阶段超时，
  这是 NapCat 上游问题；优先使用 SnowLuma，或把 NapCat 的 QQ 缓存目录挂载给
  AstrBot。
- NapCat/SnowLuma 与 AstrBot 分容器时，修改 compose 后要
  `docker compose up -d --force-recreate astrbot`，只 `restart` 不会更新挂载。

## 常见问题

**没有保存？**

检查 `save_group`/`save_private`、媒体类型开关、群/用户黑白名单、`max_file_mb`、
`max_media_per_forward` 和 `min_free_mb`。

**视频失败？**

查看日志里的 `get_file` / `downloadRichMedia` 和本地路径诊断：

- `exists=False`：AstrBot 看不到协议端缓存，检查挂载/`search_dirs`；
- `readable=False`：检查 UID/GID 和文件权限；
- `downloadRichMedia Timeout`：协议端上游问题，升级协议端或换 SnowLuma。

**怎么清理？**

```text
/fms clean 30
```

或在 WebUI 统计页清理；清理会同时删除 SQLite 记录和对应 hash。

**WebUI 页面打不开？**

AstrBot 版本需要支持插件 `views/` 页面；旧版本仍可使用 `/fms` 命令和自动保存。

## 开发与测试

```bash
pip install aiohttp pytest pyyaml ruff

python -m pytest tests
python -m unittest discover -s tests

ruff check .
ruff format --check .

# 对照真实 AstrBot 源码核验 API（路径为仓库根目录）
python tests/check_api_surface.py /path/to/AstrBot
```

测试覆盖：协议端返回结构、嵌套转发、CQ 码、本地路径、音频/文件、模板、
SQLite 索引/任务、URL 安全策略、Web API、命令、去重、清理等。

## 参考

- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin.html)
- [AstrBot 处理消息事件](https://docs.astrbot.app/dev/star/guides/listen-message-event.html)
- [AstrBot 插件页面](https://docs.astrbot.app/dev/star/guides/plugin-pages.html)
- [astrbot_plugin_media_saver](https://github.com/bsiyu/astrbot_plugin_media_saver)
- [astrbot_plugin_eat_history](https://github.com/logicat-123/astrbot_plugin_eat_history)

`path_utils.py` 的跨平台路径规范化思路参考了 `astrbot_plugin_media_saver`，
其余代码为本项目独立实现。

## License

[MIT](LICENSE)
