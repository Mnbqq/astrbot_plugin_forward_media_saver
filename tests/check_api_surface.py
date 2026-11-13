"""对照真实 AstrBot 源码核验插件依赖的 API 面。

用法：
    python tests/check_api_surface.py /path/to/AstrBot

只做静态检查（AST），不需要安装 AstrBot 的运行依赖。
退出码：0 全部通过；1 有缺失；2 参数错误。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

# (说明, 相对 AstrBot 根目录的文件, 类/对象名, 必须存在的成员)
CLASS_MEMBERS = (
    (
        "Star 生命周期",
        "astrbot/core/star/base.py",
        "Star",
        ("initialize", "terminate", "__init__"),
    ),
    (
        "Context 主动发送消息",
        "astrbot/core/star/context.py",
        "Context",
        ("send_message", "register_web_api"),
    ),
    (
        "StarTools 数据目录",
        "astrbot/core/star/star_tools.py",
        "StarTools",
        ("get_data_dir",),
    ),
    (
        "AstrMessageEvent 事件读取",
        "astrbot/core/platform/astr_message_event.py",
        "AstrMessageEvent",
        (
            "get_sender_id",
            "get_sender_name",
            "get_group_id",
            "get_self_id",
            "get_messages",
        ),
    ),
    (
        "AiocqhttpMessageEvent.bot",
        "astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py",
        "AiocqhttpMessageEvent",
        ("__init__", "send"),
    ),
    (
        "Forward 组件",
        "astrbot/core/message/components.py",
        "Forward",
        ("id",),
    ),
    (
        "Node 组件",
        "astrbot/core/message/components.py",
        "Node",
        ("content", "name", "uin"),
    ),
    (
        "Nodes 组件",
        "astrbot/core/message/components.py",
        "Nodes",
        ("nodes",),
    ),
    (
        "PlatformAdapterType 枚举",
        "astrbot/core/star/filter/platform_adapter_type.py",
        "PlatformAdapterType",
        ("AIOCQHTTP",),
    ),
    (
        "PermissionType 枚举",
        "astrbot/core/star/filter/permission.py",
        "PermissionType",
        ("ADMIN",),
    ),
    (
        "EventMessageType 枚举",
        "astrbot/core/star/filter/event_message_type.py",
        "EventMessageType",
        ("ALL",),
    ),
)

# 模块级名字
MODULE_NAMES = (
    ("astrbot.api.logger", "astrbot/api/__init__.py", "logger"),
    ("astrbot.api.AstrBotConfig", "astrbot/api/__init__.py", "AstrBotConfig"),
    (
        "astrbot.api.event.AstrMessageEvent",
        "astrbot/api/event/__init__.py",
        "AstrMessageEvent",
    ),
    (
        "astrbot.api.event.MessageChain",
        "astrbot/api/event/__init__.py",
        "MessageChain",
    ),
    ("astrbot.api.star.Context", "astrbot/api/star/__init__.py", "Context"),
    ("astrbot.api.star.Star", "astrbot/api/star/__init__.py", "Star"),
    ("astrbot.api.star.StarTools", "astrbot/api/star/__init__.py", "StarTools"),
    ("astrbot.api.web.json_response", "astrbot/api/web.py", "json_response"),
    ("astrbot.api.web.error_response", "astrbot/api/web.py", "error_response"),
    ("astrbot.api.web.request", "astrbot/api/web.py", "request"),
)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _class_node(tree: ast.Module, name: str) -> ast.ClassDef | None:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def _class_members(node: ast.ClassDef) -> set[str]:
    members: set[str] = set()
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            members.add(item.name)
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            members.add(item.target.id)
        elif isinstance(item, ast.Assign):
            for target in item.targets:
                if isinstance(target, ast.Name):
                    members.add(target.id)
    return members


def _module_exports(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[-1])
    return names


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    root = Path(sys.argv[1])
    if not root.is_dir():
        print(f"AstrBot 路径不存在: {root}")
        return 2

    failures: list[str] = []

    for label, rel_path, class_name, members in CLASS_MEMBERS:
        path = root / rel_path
        if not path.is_file():
            failures.append(f"{label}: 文件不存在 {rel_path}")
            continue
        tree = _parse(path)
        cls = _class_node(tree, class_name)
        if cls is None:
            failures.append(f"{label}: 找不到类 {class_name} ({rel_path})")
            continue
        available = _class_members(cls)
        missing = [name for name in members if name not in available]
        if missing:
            failures.append(f"{label}: {class_name} 缺少 {missing}")

    for label, rel_path, name in MODULE_NAMES:
        path = root / rel_path
        if not path.is_file():
            failures.append(f"{label}: 文件不存在 {rel_path}")
            continue
        if name not in _module_exports(_parse(path)):
            failures.append(f"{label}: 模块未导出 {name}")

    # ComponentTypes 必须能把 forward/node 映射到组件类。
    components_path = root / "astrbot/core/message/components.py"
    if components_path.is_file():
        tree = _parse(components_path)
        mapping: dict[str, str] = {}
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(target, ast.Name) and target.id == "ComponentTypes"
                for target in node.targets
            ):
                continue
            if not isinstance(node.value, ast.Dict):
                continue
            for key, value in zip(node.value.keys, node.value.values, strict=False):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    mapping[key.value] = (
                        value.id if isinstance(value, ast.Name) else ast.dump(value)
                    )
        for key, expected in (
            ("forward", "Forward"),
            ("node", "Node"),
            ("nodes", "Nodes"),
        ):
            if mapping.get(key) != expected:
                failures.append(
                    f"ComponentTypes['{key}'] 应为 {expected}，实际为 {mapping.get(key)}"
                )
    else:
        failures.append("找不到 astrbot/core/message/components.py")

    # aiocqhttp 适配器必须把 bot 挂到事件上，插件才能 call_action。
    adapter_path = (
        root / "astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py"
    )
    if adapter_path.is_file():
        tree = _parse(adapter_path)
        cls = _class_node(tree, "AiocqhttpMessageEvent")
        has_bot_assign = False
        if cls is not None:
            for item in ast.walk(cls):
                if isinstance(item, ast.Assign):
                    for target in item.targets:
                        if (
                            isinstance(target, ast.Attribute)
                            and target.attr == "bot"
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "self"
                        ):
                            has_bot_assign = True
        if not has_bot_assign:
            failures.append("AiocqhttpMessageEvent 没有 self.bot 赋值")

    # 插件自身的 import 必须存在（防止改名后静默失效）。
    required_imports = {
        "astrbot.api": {"logger"},
        "astrbot.api.event": {"AstrMessageEvent", "MessageChain"},
        "astrbot.api.message_components": {"Plain"},
        "astrbot.api.star": {"Context", "Star", "StarTools"},
    }
    for module, names in required_imports.items():
        path = root / (
            module.replace(".", "/")
            + ("/__init__.py" if module.endswith(("api", "event", "star")) else ".py")
        )
        if not path.is_file():
            failures.append(f"插件 import 的模块不存在: {module}")
            continue
        if module == "astrbot.api.event":
            filter_path = root / "astrbot/api/event/filter/__init__.py"
            if not filter_path.is_file():
                failures.append("astrbot.api.event.filter 包不存在")
            else:
                filter_exports = _module_exports(_parse(filter_path))
                for name in ("platform_adapter_type", "event_message_type"):
                    if name not in filter_exports:
                        failures.append(f"astrbot.api.event.filter 缺少 {name}")
        if module == "astrbot.api.message_components":
            # 该文件是 `from astrbot.core.message.components import *`，
            # AST 看不到具体名字，改为检查被 re-export 的源模块。
            source_path = root / "astrbot/core/message/components.py"
            if not source_path.is_file() or "Plain" not in _module_exports(
                _parse(source_path)
            ):
                failures.append("astrbot.core.message.components 未定义 Plain")
            continue
        exports = _module_exports(_parse(path))
        missing = [name for name in names if name not in exports]
        if missing:
            failures.append(f"{module} 缺少导出: {missing}")

    if failures:
        print("API 面核验失败：")
        for item in failures:
            print(" -", item)
        return 1

    print("API 面核验通过：插件使用的 AstrBot 接口均存在。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
