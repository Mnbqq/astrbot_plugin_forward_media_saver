# Changelog

## 2.0.3

- 新增 `min_video_seconds`：过滤过短的视频。
- 支持消息自带时长、ffprobe、内置 MP4 `mvhd` 解析三级探测。
- 新增 `skip_unknown_video_duration` 和 `video_probe_timeout`。

## 2.0.2

- 新增 `timezone` 配置，日期/时间模板和旧版目录不再依赖容器时区。
- 修复 Docker 默认 UTC 环境下 `{date}` 比本地日期差一天的问题。
- `/fms last` 也使用同一时区显示时间。

## 2.0.1

- 模板文件名/目录保留中文等 Unicode 字符，不再把中文群名变成 unknown。
- 新增 `{media_date}` / `{media_time}` / `{media_datetime}` 等原始时间占位符。
- 增加 Windows 设备名和 UTF-8 字节长度处理。

## 2.0.0

- 新增 SQLite 媒体索引（`index.db`）和 `manifest.jsonl` 清单。
- 新增 `/fms stats|last|path|clean|retry|export|help` 管理命令（管理员）。
- 新增 WebUI 可视化页面：统计、最近记录、按类型汇总、清理。
- 支持语音（`record`）和文件附件（`file`）保存，可单独开关。
- 新增可配置目录/文件名模板。
- 新增按节点 `message_id` 后台刷新、失败任务持久化与 `/fms retry`。
- 新增 URL 白名单/内网地址拦截、路径规范化和安全清理。
- 新增 `views/`、i18n、Logo、CI、测试夹具和工程文档。

## 1.0.7

- 移植参考插件的跨平台路径规范化（Windows/Unix/file://）。
- 兼容 NapCat 本地视频路径与缓存搜索。

## 1.0.6

- 本地路径诊断（exists/readable/UID/GID）。
- 合并转发视频本地缓存不存在时跳过 2 分钟 `get_file` 超时。

## 1.0.5

- 明确提示 NapCat 本地路径挂载/权限问题。

## 1.0.4

- 保留 NapCat `url` 字段中的本地路径。
- 用节点 `message_id` 调 `get_msg` 刷新媒体地址。
- 增加 `search_dirs` 缓存搜索。

## 1.0.3

- 新增 `file_api_timeout` 和协议端版本探测。

## 1.0.2

- `get_file` 优先 HTTP URL / base64。
- 新增本地路径/缓存搜索。

## 1.0.1

- 视频 `get_file` 兜底与详细失败日志。

## 1.0.0

- 首个版本：合并转发图片/视频自动保存。
