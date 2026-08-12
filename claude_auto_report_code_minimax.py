#!/usr/bin/env python3
"""WorkLog - 单文件自动工作日报

启动后后台采集窗口活动，Ctrl+C 结束时自动调用 LLM 生成日报。

用法：
  python3 claude_auto_report_code_minimax.py            # 启动采集，退出时生成日报
  python3 claude_auto_report_code_minimax.py --report    # 直接生成今日日报
  python3 claude_auto_report_code_minimax.py --report 2026-02-24

macOS 需要：系统设置 → 隐私与安全性 → 辅助功能 → 添加终端
"""

import base64
import glob
import json
import atexit
import os
import platform
import signal
import subprocess
import sqlite3
import shutil
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, DefaultDict

import urllib.error
import urllib.request


# ============================================================
# .env 加载（与脚本同目录的 .env，优先级低于已存在的环境变量）
# ============================================================


def _load_dotenv() -> None:
    """从脚本同目录的 .env 读取配置写入环境变量（不覆盖已存在的变量）。"""
    try:
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    except NameError:
        return
    try:
        with open(env_path, "r", encoding="utf-8-sig") as fp:
            for raw_line in fp:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


_load_dotenv()

# 记忆/配置模块（同目录 worklog_memory.py，纯标准库；缺失时全部功能优雅降级）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import worklog_memory as wlmem
except ImportError:
    wlmem = None


# ============================================================
# 配置（建议通过环境变量或同目录 .env 覆盖）
# ============================================================

LLM_API_URL = (
    os.getenv("LLM_API_URL", "").strip()
    or os.getenv("MINIMAX_API_URL", "").strip()
    or "https://api.minimaxi.com/anthropic/v1/messages"
)
LLM_API_KEY = (
    os.getenv("LLM_API_KEY", "").strip()
    or os.getenv("MINIMAX_API_KEY", "").strip()
)
LLM_MODEL = (
    os.getenv("LLM_MODEL", "").strip()
    or os.getenv("MINIMAX_MODEL", "").strip()
    or "MiniMax-M3"
)
LLM_BEARER_TOKEN_PREFIX = os.getenv("LLM_BEARER_TOKEN_PREFIX", "Bearer").strip()
# 接口格式：openai | anthropic；留空则按 URL 是否含 /anthropic/ 自动判断
LLM_API_FORMAT = os.getenv("LLM_API_FORMAT", "").strip().lower()
if not LLM_API_FORMAT:
    LLM_API_FORMAT = "anthropic" if "/anthropic/" in LLM_API_URL else "openai"
try:
    LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "").strip() or "8000")
except ValueError:
    LLM_MAX_TOKENS = 8000
LLM_ANTHROPIC_VERSION = os.getenv("LLM_ANTHROPIC_VERSION", "2023-06-01").strip()
try:
    # 低温度减少模型「自由发挥」，让报告更贴近素材
    LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "").strip() or "0.2")
except ValueError:
    LLM_TEMPERATURE = 0.2

DB_PATH = os.path.expanduser("~/.worklog/activity.db")
REPORT_DIR = os.path.join(os.getcwd(), "reports")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
SCREENSHOTS_DIR = os.path.join(SCRIPT_DIR, "screenshots")
ANALYSIS_DIR = os.path.join(SCRIPT_DIR, "analysis")
try:
    LLM_MAX_IMAGES = int(os.getenv("LLM_MAX_IMAGES", "").strip() or "8")
except ValueError:
    LLM_MAX_IMAGES = 8
try:
    SCREENSHOT_RETENTION_DAYS = int(os.getenv("SCREENSHOT_RETENTION_DAYS", "").strip() or "7")
except ValueError:
    SCREENSHOT_RETENTION_DAYS = 7
COLLECT_INTERVAL = 5  # 采集间隔（秒）
MIN_DURATION = 5  # 最短记录时长（秒）
WINDOW_STABLE_COUNT = 1  # 窗口稳定次数（默认 1：首次变更即确认）
FLUSH_INTERVAL = 30  # 最多每 30 秒落一次当前窗口
WORKLOG_PROGRESS_BAR = os.getenv("WORKLOG_PROGRESS_BAR", "1").strip().lower() in {"1", "true", "yes", "on"}
WORKLOG_PROGRESS_WIDTH = 26
WORKLOG_VERBOSE = os.getenv("WORKLOG_VERBOSE", "0").strip().lower() in {"1", "true", "yes", "on"}
try:
    WORKLOG_PROGRESS_WIDTH = int(os.getenv("WORKLOG_PROGRESS_WIDTH", "").strip() or WORKLOG_PROGRESS_WIDTH)
except ValueError:
    pass
WORKLOG_PROGRESS_TTY = sys.stdout.isatty()
WORKLOG_PROGRESS_HAS_ANSI = False
if WORKLOG_PROGRESS_TTY:
    WORKLOG_PROGRESS_HAS_ANSI = os.environ.get("TERM", "").lower() not in {"", "dumb"}
WORKLOG_PROGRESS_INTERVAL = 5.0
try:
    WORKLOG_PROGRESS_INTERVAL = float(os.getenv("WORKLOG_PROGRESS_INTERVAL", "5").strip() or 5)
except (TypeError, ValueError):
    WORKLOG_PROGRESS_INTERVAL = 5.0
WORKLOG_PROGRESS_INTERVAL = max(1.0, WORKLOG_PROGRESS_INTERVAL)
WORKLOG_RECENT_RECORDS = 8
try:
    WORKLOG_RECENT_RECORDS = int(os.getenv("WORKLOG_RECENT_RECORDS", "").strip() or WORKLOG_RECENT_RECORDS)
except ValueError:
    pass
WORKLOG_RETENTION_DAYS = 31
try:
    WORKLOG_RETENTION_DAYS = int(
        os.getenv("WORKLOG_RETENTION_DAYS", "").strip() or WORKLOG_RETENTION_DAYS
    )
except ValueError:
    pass
WORKLOG_CLEANUP_INTERVAL_SECONDS = 3600
try:
    WORKLOG_CLEANUP_INTERVAL_SECONDS = int(
        os.getenv("WORKLOG_CLEANUP_INTERVAL_SECONDS", "").strip() or WORKLOG_CLEANUP_INTERVAL_SECONDS
    )
except ValueError:
    pass
if WORKLOG_CLEANUP_INTERVAL_SECONDS < 60:
    WORKLOG_CLEANUP_INTERVAL_SECONDS = 60
ENABLE_ACTIVITY_LOG = os.getenv("WORKLOG_ACTIVITY_LOG", "").strip().lower() in {"1", "true", "yes"}
_LAST_PROGRESS_RENDER_ROWS = 0

DEFAULT_BLACKLIST = [
    "bilibili", "抖音", "淘宝", "京东", "微博",
    "YouTube", "Netflix", "Spotify", "Apple Music",
    "App Store", "Finder", "访达",
    "系统设置", "System Settings", "System Preferences",
]


def get_blacklist() -> List[str]:
    """获取生效的黑名单关键词（blacklist.json 可由 GUI 维护，缺失时用默认值）。"""
    if wlmem is not None:
        return wlmem.load_blacklist(DEFAULT_BLACKLIST)
    return DEFAULT_BLACKLIST

REPORT_STYLE = """请生成一份简洁的工作日报，格式：

## 今日工作
按项目/事项分类，每项一句话描述具体做了什么。

## 进行中
列出素材中明确显示已开始但尚未见到完成迹象的工作（只列有直接证据的，不要脑补）。

## 备注
值得注意的信息（长时间会议、高频切换等）。

【真实性硬规则——违反任何一条都算错误】
1. 只写素材中有直接证据的活动；证据不足的宁可不写，不要为了报告完整而补全。
2. 动词必须准确：只是查看/浏览/阅读的内容，写「查看了…」，严禁升级成「完成/编写/调研/输出」。
3. 屏幕上出现 ≠ 用户做的：聊天中他人发的消息、他人的文档、会议共享画面、AI 生成的内容，都不是用户本人的工作成果，不得写成用户的产出。
4. 不得编造素材中没有的数字、文件名、结论。
5. 拿不准归属或真实性的条目，末尾标注「（待确认）」。
6. 会议时段只写「参加了 XX 会议（讨论主题…）」；会议中他人投屏/演示的幻灯片、文档、数据一律不算用户的产出。

要求：中文、专业简洁、合并相似活动、忽略琐碎操作。"""

MONTH_REPORT_STYLE = """请基于本月活动记录，生成一份月度工作报表，格式：

## 本月成果
按项目/事项分类，按时间顺序归纳本月产出与进展。

## 关键事项
列出本月高频/高投入事项及结论。

## 时间分配
给出本月主力应用与时长占比结论。

要求：中文、专业简洁、结构化输出。"""


# ============================================================
# 窗口采集（跨平台）
# ============================================================


def get_active_window() -> Tuple[Optional[str], Optional[str]]:
    """获取当前前台窗口信息。

    Returns:
        (应用名, 窗口标题)。若采集失败返回 (None, None)。

    Side effects:
        调用系统 API 或 shell 命令，读取当前前台窗口元数据。
    """
    system_name = platform.system()
    if system_name == "Darwin":
        return _get_window_macos()
    if system_name == "Windows":
        return _get_window_windows()
    if system_name == "Linux":
        return _get_window_linux()
    return None, None


def check_runtime_environment() -> bool:
    """检查当前系统采集环境是否就绪。

    Returns:
        采集模式下是否可以继续运行。
    """
    system_name = platform.system()
    if system_name == "Darwin":
        if not shutil.which("osascript"):
            print("[错误] macOS 缺少 osascript，无法采集前台窗口。")
            print("[建议] 请确认系统完整性后再重试。")
            return False
        return True

    if system_name == "Windows":
        # 使用 ctypes 调用 user32/kernel32 采集前台窗口，无需第三方依赖
        return True

    if system_name == "Linux":
        if not shutil.which("xdotool"):
            print("[错误] Linux 缺少 xdotool，无法采集前台窗口。")
            print("[建议] 安装 xdotool（例如 sudo apt install xdotool）。")
            return False
        return True

    print(f"[警告] 未识别系统：{system_name}，采集能力可能受限。")
    return False


def _run_command(command: List[str], timeout: int = 5) -> Optional[str]:
    """执行外部命令并返回标准输出。

    Args:
        command: 要执行的命令列表
        timeout: 超时时间（秒）

    Returns:
        命令标准输出（去除前后空白），失败返回 None。
    """
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None


def _get_window_macos() -> Tuple[Optional[str], Optional[str]]:
    """在 macOS 下读取前台应用与窗口标题。"""
    app = _run_command(
        [
            "osascript",
            "-e",
            'tell application "System Events" to return name of first application process whose frontmost is true',
        ]
    )
    if not app:
        return None, None

    title = _run_command(
        [
            "osascript",
            "-e",
            'tell application "System Events" to tell (first application process whose frontmost is true) to try\nreturn name of front window\non error\nreturn ""\nend try',
        ]
    )
    return app, title or ""


def _get_window_windows() -> Tuple[Optional[str], Optional[str]]:
    """在 Windows 下读取前台应用与窗口标题（基于 ctypes，无需 pywin32/psutil）。"""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # 64 位下句柄是指针宽度，必须显式声明返回/参数类型，否则会被截断
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None, None

        length = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length and length > 0:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

        app: Optional[str] = None
        if pid.value:
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if handle:
                try:
                    size = wintypes.DWORD(32768)
                    name_buffer = ctypes.create_unicode_buffer(size.value)
                    if kernel32.QueryFullProcessImageNameW(handle, 0, name_buffer, ctypes.byref(size)):
                        full_path = name_buffer.value
                        if full_path:
                            app = full_path.rsplit("\\", 1)[-1]
                finally:
                    kernel32.CloseHandle(handle)
        return app, title
    except Exception:
        return None, None


def _get_window_linux() -> Tuple[Optional[str], Optional[str]]:
    """在 Linux 下读取前台应用与窗口标题。"""
    title = _run_command(["xdotool", "getactivewindow", "getwindowname"])
    pid = _run_command(["xdotool", "getactivewindow", "getwindowpid"])
    if not title or not pid or not pid.isdigit():
        return None, None

    try:
        app = Path(f"/proc/{pid}/comm").read_text().strip()
        return app, title
    except OSError:
        return None, title


# ============================================================
# 数据库
# ============================================================


def init_db() -> sqlite3.Connection:
    """初始化数据库并返回可用连接。"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            date TEXT NOT NULL,
            app_name TEXT,
            window_title TEXT,
            duration_seconds INTEGER DEFAULT 0
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_date ON activity(date)")
    conn.commit()
    return conn


def cleanup_old_records(
    conn: sqlite3.Connection,
    retention_days: int = WORKLOG_RETENTION_DAYS,
) -> int:
    """清理过期活动记录。

    Args:
        conn: SQLite 连接对象
        retention_days: 保留天数，默认 30 天

    Returns:
        被删除记录条数。
    """
    if retention_days <= 0:
        return 0

    cutoff_date = (date.today() - timedelta(days=retention_days)).isoformat()
    cursor = conn.execute(
        "DELETE FROM activity WHERE date < ?",
        (cutoff_date,),
    )
    deleted_count = cursor.rowcount if cursor.rowcount is not None else 0
    conn.commit()
    return deleted_count


def is_blacklisted(app: Optional[str], title: Optional[str]) -> bool:
    """判断当前窗口是否属于黑名单。

    Args:
        app: 应用名
        title: 窗口标题

    Returns:
        是否命中黑名单。
    """
    text = f"{app or ''} {title or ''}".lower()
    return any(black.lower() in text for black in get_blacklist())


def save_record(
    conn: sqlite3.Connection,
    app: Optional[str],
    title: Optional[str],
    start: float,
    end: float,
) -> None:
    """保存单条窗口停留记录。

    Args:
        conn: SQLite 连接对象
        app: 应用名
        title: 窗口标题
        start: 起始时间戳
        end: 结束时间戳
    """
    if app is None:
        return

    duration = int(end - start)
    if duration < MIN_DURATION or is_blacklisted(app, title):
        return

    ts = datetime.fromtimestamp(start)
    conn.execute(
        "INSERT INTO activity (timestamp, date, app_name, window_title, duration_seconds) "
        "VALUES (?, ?, ?, ?, ?)",
        (ts.isoformat(), ts.strftime("%Y-%m-%d"), app, title or "", duration),
    )
    conn.commit()
    if ENABLE_ACTIVITY_LOG:
        print(f"  📝 {ts.strftime('%H:%M')} [{app}] {(title or '')[:60]}  ({duration}s)")


# ============================================================
# 日报生成
# ============================================================


def load_today(conn: sqlite3.Connection, target_date: Optional[str] = None) -> List[Dict[str, object]]:
    """读取某一天的活动记录。

    Args:
        conn: SQLite 连接
        target_date: 日期字符串（YYYY-MM-DD），默认今日

    Returns:
        活动记录列表。
    """
    if target_date is None:
        target_date = date.today().isoformat()

    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM activity WHERE date = ? ORDER BY timestamp",
        (target_date,),
    ).fetchall()
    return [dict(item) for item in rows]


def _merge_intervals_seconds(intervals: List[Tuple[float, float]]) -> int:
    """合并重叠时间区间，返回去重后的总秒数。

    用于多个采集进程在同一时段重复记录时，按真实墙钟时间计时而非简单累加
    （不修改 / 不删除任何原始记录，仅在统计时去重）。

    Args:
        intervals: (起始 epoch 秒, 时长秒) 列表

    Returns:
        合并后的总秒数。
    """
    spans = sorted((start, start + dur) for start, dur in intervals if dur and dur > 0)
    if not spans:
        return 0
    total = 0.0
    cur_start, cur_end = spans[0]
    for start, end in spans[1:]:
        if start > cur_end:
            total += cur_end - cur_start
            cur_start, cur_end = start, end
        elif end > cur_end:
            cur_end = end
    total += cur_end - cur_start
    return int(round(total))


def aggregate(activities: List[Dict[str, object]]) -> str:
    """聚合活动记录，生成用于提示词的摘要文本。

    Args:
        activities: 活动字典列表

    Returns:
        已按应用和时间聚合后的文本摘要
    """
    if not activities:
        return "今天没有采集到活动记录。"

    app_intervals: DefaultDict[str, List[Tuple[float, float]]] = defaultdict(list)
    title_intervals: DefaultDict[str, DefaultDict[str, List[Tuple[float, float]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for item in activities:
        app = item["app_name"] if item["app_name"] is not None else "Unknown"
        title = item["window_title"] or "(无标题)"
        duration_seconds = int(item["duration_seconds"])
        try:
            start_epoch = datetime.fromisoformat(str(item["timestamp"])).timestamp()
        except ValueError:
            start_epoch = 0.0
        app_intervals[app].append((start_epoch, float(duration_seconds)))
        title_intervals[app][title].append((start_epoch, float(duration_seconds)))

    # 用区间合并计算真实时长：多个采集进程在同一时段重复记录时不重复累加（不删任何原始数据）
    app_time: Dict[str, int] = {
        app: _merge_intervals_seconds(spans) for app, spans in app_intervals.items()
    }
    app_titles: Dict[str, Dict[str, int]] = {
        app: {title: _merge_intervals_seconds(spans) for title, spans in titles.items()}
        for app, titles in title_intervals.items()
    }
    all_intervals = [span for spans in app_intervals.values() for span in spans]
    total_seconds = _merge_intervals_seconds(all_intervals)

    lines = [f"日期: {activities[0]['date']}", f"总有效时长: {total_seconds / 3600:.1f} 小时", ""]

    max_app_time = max(app_time.values()) if app_time else 0
    for app, total in sorted(app_time.items(), key=lambda item: -item[1]):
        if total <= 0:
            continue
        lines.append(f"【{app}】共 {_format_duration_seconds(total)}")
        for title, total_seconds in sorted(app_titles[app].items(), key=lambda item: -item[1])[:8]:
            if total_seconds <= 0:
                continue
            show_title = f"{title[:80]}..." if len(title) > 80 else title
            lines.append(f"  - {show_title} ({_format_duration_seconds(total_seconds)})")
        lines.append("")

    lines.append("--- 应用时长柱状图 ---")
    lines.append("  (长度按最长应用时长归一化)")
    chart_width = 24
    for app, total in sorted(app_time.items(), key=lambda item: -item[1]):
        if total <= 0:
            continue
        bar = _format_duration_bar(total, max_app_time, chart_width)
        lines.append(f"  {app[:14]:14} {bar} {_format_duration_seconds(total)}")

    lines.append("")
    lines.append("--- 时间线 ---")
    hours: DefaultDict[int, List[Dict[str, object]]] = defaultdict(list)
    for item in activities:
        hour = datetime.fromisoformat(item["timestamp"]).hour
        hours[hour].append(item)

    for hour in sorted(hours):
        apps = sorted({item["app_name"] for item in hours[hour] if item["app_name"] is not None})
        lines.append(f"  {hour:02d}:00  {', '.join(apps)}")

    return "\n".join(lines)


def _format_duration_bar(seconds: int, max_seconds: int, width: int = 24) -> str:
    """将时长映射为文本柱状条。

    Args:
        seconds: 当前值（秒）
        max_seconds: 全部应用中的最大值（秒）
        width: 柱状图总宽度

    Returns:
        形如 [####------] 的占比文本
    """
    if max_seconds <= 0:
        return "[" + "-" * width + "]"

    filled = int(seconds / max_seconds * width)
    if seconds > 0 and filled == 0:
        filled = 1
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def load_recent_records(
    conn: sqlite3.Connection, target_date: Optional[str] = None, limit: int = 8
) -> List[Dict[str, object]]:
    """读取最近活动记录用于终端预览。

    Args:
        conn: SQLite 连接
        target_date: 日期字符串（YYYY-MM-DD），默认今日
        limit: 取最近记录条数

    Returns:
        最近活动记录列表，按时间倒序返回
    """
    if target_date is None:
        target_date = date.today().isoformat()

    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT app_name, window_title, duration_seconds FROM activity WHERE date = ? "
        "ORDER BY id DESC LIMIT ?",
        (target_date, limit),
    ).fetchall()
    return [dict(item) for item in rows]


def build_progress_lines(
    current_line: str,
    recent_records: List[Dict[str, object]],
) -> List[str]:
    """构建终端显示的多行状态内容。

    Args:
        current_line: 当前窗口进度行
        recent_records: 最近记录列表（倒序）

    Returns:
        终端输出行列表
    """
    lines = [current_line, f"最近窗口记录（最近{min(len(recent_records), WORKLOG_RECENT_RECORDS)}条）:"]
    if recent_records:
        for item in reversed(recent_records):
            app = item["app_name"] or "Unknown"
            title = item["window_title"] or "(无标题)"
            duration_seconds = int(item["duration_seconds"])
            lines.append(
                f"  - {app[:12]:12} | {_format_duration_seconds(duration_seconds):>7} | {title[:42]}"
            )
    else:
        lines.append("  - 暂无历史记录")

    return lines


def print_progress_lines(lines: List[str]) -> None:
    """在终端刷新状态文本，避免滚动刷屏。"""
    if not WORKLOG_PROGRESS_BAR:
        return
    if not WORKLOG_PROGRESS_TTY or not WORKLOG_PROGRESS_HAS_ANSI:
        if lines:
            width = shutil.get_terminal_size((100, 20)).columns
            summary_line = lines[0][:width - 1]
            padding = " " * max(0, width - len(summary_line) - 1)
            sys.stdout.write(f"\r{summary_line}{padding}")
            sys.stdout.flush()
        return

    width = shutil.get_terminal_size((100, 20)).columns
    render_lines = lines
    line_count = len(render_lines)

    global _LAST_PROGRESS_RENDER_ROWS

    if _LAST_PROGRESS_RENDER_ROWS > 0:
        sys.stdout.write(f"\033[{_LAST_PROGRESS_RENDER_ROWS}A")
        sys.stdout.write("\r")

    render_row_count = max(_LAST_PROGRESS_RENDER_ROWS, line_count)
    for idx in range(render_row_count):
        sys.stdout.write("\r\033[2K")
        if idx < line_count:
            line = render_lines[idx]
            trimmed = line[: width - 1]
            padding = " " * max(0, width - len(trimmed) - 1)
            sys.stdout.write(f"{trimmed}{padding}")
        sys.stdout.write("\n")

    _LAST_PROGRESS_RENDER_ROWS = render_row_count
    sys.stdout.flush()


def _format_duration_seconds(seconds: int) -> str:
    """将秒数转换为可读中文时长文本。"""
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds / 60:.1f} 分钟"
    return f"{seconds / 3600:.2f} 小时"


def _http_post_json(
    url: str, headers: Dict[str, str], payload: Dict[str, object], timeout: int = 120
) -> Optional[dict]:
    """以标准库发送 JSON POST 并返回解析后的响应字典。

    Args:
        url: 接口地址
        headers: 请求头
        payload: 请求体（将序列化为 JSON）
        timeout: 超时时间（秒）

    Returns:
        解析后的响应 JSON；失败返回 None。
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = error.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        print(f"[API 错误] {error.code}: {detail}")
        return None
    except (urllib.error.URLError, OSError) as error:
        print(f"[网络错误] {error}")
        return None

    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        print(f"[解析错误] 响应不是合法 JSON: {error} | {raw[:200]}")
        return None


def _extract_anthropic_text(data: dict) -> Optional[str]:
    """从 Anthropic Messages 响应中提取正文文本（忽略 thinking 思考块）。"""
    blocks = data.get("content")
    if not isinstance(blocks, list):
        return None
    texts = [
        block.get("text", "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    content = "".join(texts).strip()
    if content:
        return content
    fallback = "".join(
        block.get("text", "")
        for block in blocks
        if isinstance(block, dict) and block.get("text")
    ).strip()
    return fallback or None


def call_minimax(prompt: str) -> Optional[str]:
    """调用 LLM API 生成日报（支持 OpenAI 与 Anthropic 两种接口格式）。

    Args:
        prompt: 模型输入提示词

    Returns:
        模型生成内容；异常时返回 None。
    """
    if not LLM_API_KEY:
        print("[错误] 未配置 LLM_API_KEY（或旧版兼容变量 MINIMAX_API_KEY），请设置环境变量或 .env")
        return None

    system_prompt = "你是一个专业的工作日报生成助手。"

    if LLM_API_FORMAT == "anthropic":
        headers = {
            "x-api-key": LLM_API_KEY,
            "anthropic-version": LLM_ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }
        payload: Dict[str, object] = {
            "model": LLM_MODEL,
            "max_tokens": LLM_MAX_TOKENS,
            "temperature": LLM_TEMPERATURE,
            "system": system_prompt,
            "messages": [{"role": "user", "content": prompt}],
        }
        data = _http_post_json(LLM_API_URL, headers, payload)
        if data is None:
            return None
        content = _extract_anthropic_text(data)
        if content is None:
            print(f"[解析错误] {json.dumps(data, ensure_ascii=False)[:300]}")
        return content

    headers = {
        "Authorization": f"{LLM_BEARER_TOKEN_PREFIX} {LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": LLM_MODEL,
        "max_tokens": LLM_MAX_TOKENS,
        "temperature": LLM_TEMPERATURE,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
    }
    data = _http_post_json(LLM_API_URL, headers, payload)
    if data is None:
        return None
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        print(f"[解析错误] {json.dumps(data, ensure_ascii=False)[:300]}")
        return None


def test_llm_connection() -> bool:
    """轻量连通性测试：只验证地址/Key/模型有效，不等模型完整生成。

    max_tokens 压到极小，接口返回合法响应结构即算成功（思考型模型
    即使没输出正文，HTTP 200 + 正常结构也证明配置可用），几秒内出结果。
    """
    if not LLM_API_KEY:
        print("[错误] 未配置 LLM_API_KEY")
        return False

    if LLM_API_FORMAT == "anthropic":
        headers = {
            "x-api-key": LLM_API_KEY,
            "anthropic-version": LLM_ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }
        payload: Dict[str, object] = {
            "model": LLM_MODEL,
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "OK"}],
        }
        data = _http_post_json(LLM_API_URL, headers, payload, timeout=30)
        return data is not None and isinstance(data.get("content"), list)

    headers = {
        "Authorization": f"{LLM_BEARER_TOKEN_PREFIX} {LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": LLM_MODEL,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "OK"}],
    }
    data = _http_post_json(LLM_API_URL, headers, payload, timeout=30)
    return data is not None and isinstance(data.get("choices"), list)


def parse_report_date(raw: Optional[str]) -> Optional[str]:
    """解析日报日期参数。

    Args:
        raw: 用户输入日期字符串（YYYY-MM-DD）

    Returns:
        规范化后的日期字符串；无效输入返回 None
    """
    if raw is None:
        return date.today().isoformat()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def parse_report_month(raw: Optional[str]) -> Optional[str]:
    """解析月报参数。

    Args:
        raw: 用户输入日期字符串（YYYY-MM）

    Returns:
        规范化后的月份字符串；无效输入返回 None
    """
    if raw is None:
        return date.today().strftime("%Y-%m")
    try:
        return datetime.strptime(raw, "%Y-%m").strftime("%Y-%m")
    except ValueError:
        return None


def load_month(conn: sqlite3.Connection, target_month: Optional[str] = None) -> List[Dict[str, object]]:
    """读取某个月的活动记录。

    Args:
        conn: SQLite 连接
        target_month: 月份字符串（YYYY-MM），默认当月

    Returns:
        活动记录列表。
    """
    if target_month is None:
        target_month = date.today().strftime("%Y-%m")

    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM activity WHERE substr(date,1,7)=? ORDER BY timestamp",
        (target_month,),
    ).fetchall()
    return [dict(item) for item in rows]


def generate_report(conn: sqlite3.Connection, target_date: Optional[str] = None) -> None:
    """聚合活动并写入 Markdown 日报文件。

    Args:
        conn: SQLite 连接
        target_date: 目标日期，默认当日
    """
    if target_date is None:
        target_date = date.today().isoformat()

    activities = load_today(conn, target_date)
    count = len(activities)
    phase_notes = load_phase_notes(target_date)
    if not activities and not phase_notes:
        print(f"日报未生成：{target_date} 无活动记录与截图分析")
        return

    summary = aggregate(activities)
    phase_block = phase_notes if phase_notes else "（今日无截图内容分析）"

    memory_block = ""
    if wlmem is not None:
        memory_block = wlmem.build_memory_block(
            f"{summary}\n{phase_block}",
            exclude_files=[f"daily-report-{target_date}.md"],
        )
    memory_section = f"\n{memory_block}\n" if memory_block else ""

    prompt = f"""以下是从用户电脑自动采集的今日工作素材，包含两部分：
（A）应用 / 窗口标题 / 时长统计；（B）每隔一段时间对屏幕截图做的「分阶段内容小结」。
请综合两部分，整理今天实际做了的工作，生成日报。只归纳素材中有据可查的活动，不要推测和扩写。

{REPORT_STYLE}
{memory_section}
===== A. 今日活动记录（应用/标题/时长） =====
{summary}

===== B. 分阶段截图内容小结 =====
{phase_block}
===== 记录结束 =====

直接输出日报，不要其他解释。"""

    report = call_minimax(prompt)
    if not report:
        print(f"日报未生成：{target_date} LLM 响应失败")
        return

    os.makedirs(REPORT_DIR, exist_ok=True)
    report_path = os.path.join(REPORT_DIR, f"daily-report-{target_date}.md")
    output_lines = [
        f"# 工作日报 {target_date}",
        "",
        f"*自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}*",
        "",
        f"记录数: {count} 条",
        "",
        "## AI 生成日报",
        "",
        report.strip(),
        "",
        "## 分阶段内容小结",
        "",
        phase_block,
        "",
        "## 采集摘要",
        "",
        summary,
    ]
    with open(report_path, "w", encoding="utf-8") as fp:
        fp.write("\n".join(output_lines))

    print(f"日报已写入 Markdown：{report_path}")


def generate_monthly_report(conn: sqlite3.Connection, target_month: Optional[str] = None) -> None:
    """聚合月份活动并写入月报 Markdown 文件。

    Args:
        conn: SQLite 连接
        target_month: 目标月份（YYYY-MM），默认当月
    """
    if target_month is None:
        target_month = date.today().strftime("%Y-%m")

    activities = load_month(conn, target_month)
    if not activities:
        print(f"月报未生成：{target_month} 无活动记录")
        return

    summary_lines: List[str] = []
    app_time: DefaultDict[str, int] = defaultdict(int)
    app_titles: DefaultDict[str, DefaultDict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    day_seconds: DefaultDict[str, int] = defaultdict(int)
    hour_seconds: DefaultDict[int, int] = defaultdict(int)

    for item in activities:
        app = item["app_name"] if item["app_name"] is not None else "Unknown"
        title = item["window_title"] or "(无标题)"
        duration_seconds = int(item["duration_seconds"])
        current_day = str(item["date"])
        current_hour = datetime.fromisoformat(item["timestamp"]).hour
        app_time[app] += duration_seconds
        app_titles[app][title] += duration_seconds
        day_seconds[current_day] += duration_seconds
        hour_seconds[current_hour] += duration_seconds

    total_seconds = sum(app_time.values())
    summary_lines.append(f"月份: {target_month}")
    summary_lines.append(f"总有效时长: {total_seconds / 3600:.1f} 小时")
    summary_lines.append("")
    summary_lines.append("--- 应用时长排行 ---")
    for app, total in sorted(app_time.items(), key=lambda item: -item[1]):
        if total <= 0:
            continue
        ratio = (total / total_seconds * 100) if total_seconds else 0
        summary_lines.append(f"【{app}】{_format_duration_seconds(total)} ({ratio:.1f}%)")
        for title, title_seconds in sorted(app_titles[app].items(), key=lambda item: -item[1])[:8]:
            if title_seconds <= 0:
                continue
            show_title = f"{title[:80]}..." if len(title) > 80 else title
            summary_lines.append(f"  - {show_title} ({_format_duration_seconds(title_seconds)})")
        summary_lines.append("")

    summary_lines.append("--- 日粒度分布 ---")
    for day in sorted(day_seconds):
        summary_lines.append(f"{day}: {_format_duration_seconds(day_seconds[day])}")

    summary_lines.append("")
    summary_lines.append("--- 小时分布 ---")
    for hour in sorted(hour_seconds):
        summary_lines.append(f"{hour:02d}:00: {_format_duration_seconds(hour_seconds[hour])}")

    month_summary_text = _format_activity_summary_for_prompt(activities, target_month)
    memory_block = ""
    if wlmem is not None:
        memory_block = wlmem.build_memory_block(
            month_summary_text,
            exclude_files=[f"monthly-report-{target_month}.md"],
        )
    memory_section = f"\n{memory_block}\n" if memory_block else ""

    prompt = f"""以下是从用户电脑自动采集的本月活动记录（应用名、窗口标题、时长）。
请基于本月数据生成月度工作报表。只归纳记录中有据可查的活动，不要推测和扩写；
只是查看/浏览过的内容不得写成用户完成的工作。

{MONTH_REPORT_STYLE}
{memory_section}
===== 本月活动记录聚合 =====
{month_summary_text}
===== 记录结束 =====

直接输出月报，不要其他解释。"""

    report = call_minimax(prompt)
    if not report:
        print(f"月报未生成：{target_month} LLM 响应失败")
        return

    os.makedirs(REPORT_DIR, exist_ok=True)
    report_path = os.path.join(REPORT_DIR, f"monthly-report-{target_month}.md")
    output_lines = [
        f"# 工作月报 {target_month}",
        "",
        f"*自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}*",
        "",
        f"记录数: {len(activities)} 条",
        "",
        "## AI 生成月报",
        "",
        report.strip(),
        "",
        "## 采集汇总",
        "",
        "\n".join(summary_lines),
    ]
    with open(report_path, "w", encoding="utf-8") as fp:
        fp.write("\n".join(output_lines))

    print(f"月报已写入 Markdown：{report_path}")


def _format_activity_summary_for_prompt(
    activities: List[Dict[str, object]],
    scope_label: str,
) -> str:
    """按 LLM 提示词生成扁平化活动摘要文本。"""
    if not activities:
        return f"{scope_label} 无活动记录"

    grouped: DefaultDict[str, int] = defaultdict(int)
    for item in activities:
        app = item["app_name"] if item["app_name"] is not None else "Unknown"
        title = item["window_title"] or "(无标题)"
        grouped[f"{app} | {title}"] += int(item["duration_seconds"])

    summary_lines = [f"范围: {scope_label}", f"总时长: {sum(grouped.values()) / 3600:.1f} 小时", ""]
    for key, seconds in sorted(grouped.items(), key=lambda item: -item[1]):
        summary_lines.append(f"- {key}: {_format_duration_seconds(seconds)}")
    return "\n".join(summary_lines)


# ============================================================
# 主流程
# ============================================================


# ============================================================
# 采集单实例互斥锁（Windows 命名互斥体，杜绝孤儿/双开重复计时）
# ============================================================

_COLLECT_MUTEX_HANDLE = None


def acquire_collect_mutex() -> bool:
    """获取采集单实例互斥锁。

    Returns:
        True 表示拿到锁（或非 Windows 平台直接放行）；False 表示已有采集进程在运行。
    """
    global _COLLECT_MUTEX_HANDLE
    if platform.system() != "Windows":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, "WorkLog_Collector_Singleton")
        if not handle:
            return True  # 创建失败时不阻断采集
        ERROR_ALREADY_EXISTS = 183
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        _COLLECT_MUTEX_HANDLE = handle  # 持有至进程退出
        return True
    except Exception:
        return True


# ============================================================
# 截图视觉分析 + 分阶段小结 + 周报
# ============================================================


def _encode_png(rgb_bytes: bytes, width: int, height: int) -> bytes:
    """将顶到底的 RGB 像素（每像素3字节）编码为 PNG 字节（纯标准库）。"""
    import struct
    import zlib

    stride = width * 3
    raw = bytearray()
    mv = memoryview(rgb_bytes)
    for row in range(height):
        raw.append(0)  # 每行过滤器字节 0（None）
        raw += mv[row * stride:(row + 1) * stride]
    compressed = zlib.compress(bytes(raw), 6)

    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        out = struct.pack(">I", len(data)) + chunk_type + data
        crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        return out + struct.pack(">I", crc)

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8bit, 真彩色RGB
    return signature + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", compressed) + _chunk(b"IEND", b"")


def _get_blacklisted_screen_rects() -> List[Tuple[int, int, int, int]]:
    """枚举所有可见顶层窗口，返回命中黑名单的窗口矩形（虚拟屏坐标）。

    用于截图打码：黑名单窗口出现在屏幕上时不再跳过整张截图，
    而是只遮蔽对应区域，避免同屏的工作内容一起丢失。
    """
    if platform.system() != "Windows":
        return []
    rects: List[Tuple[int, int, int, int]] = []
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        try:
            dwmapi = ctypes.windll.dwmapi
        except OSError:
            dwmapi = None
        blacklist = [b.lower() for b in get_blacklist()]
        if not blacklist:
            return []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _on_window(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            # 跳过 cloaked 窗口（挂起的 UWP 等：报告矩形但实际不可见，误遮会毁掉截图）
            if dwmapi is not None:
                cloaked = wintypes.DWORD(0)
                DWMWA_CLOAKED = 14
                dwmapi.DwmGetWindowAttribute(
                    hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
                )
                if cloaked.value:
                    return True

            title = ""
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                title = buffer.value

            app = ""
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
                if handle:
                    try:
                        size = wintypes.DWORD(32768)
                        name_buffer = ctypes.create_unicode_buffer(size.value)
                        if kernel32.QueryFullProcessImageNameW(
                            handle, 0, name_buffer, ctypes.byref(size)
                        ):
                            app = name_buffer.value.rsplit("\\", 1)[-1]
                    finally:
                        kernel32.CloseHandle(handle)

            text = f"{app} {title}".lower()
            if not any(black in text for black in blacklist):
                return True

            rect = wintypes.RECT()
            got_rect = False
            if dwmapi is not None:
                DWMWA_EXTENDED_FRAME_BOUNDS = 9
                if dwmapi.DwmGetWindowAttribute(
                    hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)
                ) == 0:
                    got_rect = True
            if not got_rect and not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            if rect.right > rect.left and rect.bottom > rect.top:
                rects.append((rect.left, rect.top, rect.right, rect.bottom))
            return True

        user32.EnumWindows(_on_window, 0)
    except Exception:
        pass
    return rects


def _capture_virtual_screen_png(
    max_width: int = 1600,
    mask_rects: Optional[List[Tuple[int, int, int, int]]] = None,
) -> Optional[bytes]:
    """用 GDI 截取全部显示器（虚拟屏），降采样后返回 PNG 字节（无第三方依赖）。

    Args:
        max_width: 降采样后的最大宽度
        mask_rects: 需遮蔽的矩形（虚拟屏坐标），对应区域涂成深灰色
    """
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass

        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.GetSystemMetrics.restype = ctypes.c_int
        SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
        vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        if vw <= 0 or vh <= 0:
            return None

        if vw > max_width:
            scale = max_width / float(vw)
            tw = max(1, int(vw * scale))
            th = max(1, int(vh * scale))
        else:
            tw, th = vw, vh

        user32.GetDC.argtypes = [wintypes.HWND]
        user32.GetDC.restype = wintypes.HDC
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.ReleaseDC.restype = ctypes.c_int
        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.CreateDIBSection.argtypes = [
            wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
        ]
        gdi32.CreateDIBSection.restype = ctypes.c_void_p
        gdi32.SelectObject.argtypes = [wintypes.HDC, ctypes.c_void_p]
        gdi32.SelectObject.restype = ctypes.c_void_p
        gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
        gdi32.DeleteObject.restype = wintypes.BOOL
        gdi32.DeleteDC.argtypes = [wintypes.HDC]
        gdi32.DeleteDC.restype = wintypes.BOOL
        gdi32.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
        gdi32.SetStretchBltMode.restype = ctypes.c_int
        gdi32.StretchBlt.argtypes = [
            wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
        ]
        gdi32.StretchBlt.restype = wintypes.BOOL

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
            ]

        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = tw
        bmi.biHeight = -th  # 负高 => 顶到底，便于直接编码 PNG
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0  # BI_RGB

        screen_dc = user32.GetDC(None)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        bits_ptr = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(mem_dc, ctypes.byref(bmi), 0, ctypes.byref(bits_ptr), None, 0)
        if not hbmp or not bits_ptr.value:
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(None, screen_dc)
            return None

        old = gdi32.SelectObject(mem_dc, hbmp)
        gdi32.SetStretchBltMode(mem_dc, 4)  # HALFTONE，缩放质量更好
        SRCCOPY = 0x00CC0020
        gdi32.StretchBlt(mem_dc, 0, 0, tw, th, screen_dc, vx, vy, vw, vh, SRCCOPY)
        gdi32.GdiFlush()

        raw = ctypes.string_at(bits_ptr.value, tw * th * 4)  # BGRA，顶到底

        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, screen_dc)

        # BGRA -> RGB（带步长的切片赋值，C 速度）
        rgb = bytearray(tw * th * 3)
        rgb[0::3] = raw[2::4]
        rgb[1::3] = raw[1::4]
        rgb[2::3] = raw[0::4]

        # 黑名单窗口区域打码（深灰填充），坐标从虚拟屏映射到降采样后图像
        if mask_rects:
            scale = tw / float(vw)
            fill = bytes((20, 20, 20))
            for left, top, right, bottom in mask_rects:
                x0 = max(0, min(tw, int((left - vx) * scale)))
                x1 = max(0, min(tw, int((right - vx) * scale) + 1))
                y0 = max(0, min(th, int((top - vy) * scale)))
                y1 = max(0, min(th, int((bottom - vy) * scale) + 1))
                if x1 <= x0 or y1 <= y0:
                    continue
                row_fill = fill * (x1 - x0)
                for y in range(y0, y1):
                    start = (y * tw + x0) * 3
                    rgb[start:start + (x1 - x0) * 3] = row_fill

        return _encode_png(bytes(rgb), tw, th)
    except Exception as error:
        print(f"[截图] 异常：{error}")
        return None


def _cleanup_old_screenshots() -> None:
    """删除超过保留天数（SCREENSHOT_RETENTION_DAYS）的截图日期目录。"""
    if not os.path.isdir(SCREENSHOTS_DIR):
        return
    cutoff = date.today() - timedelta(days=SCREENSHOT_RETENTION_DAYS)
    for name in os.listdir(SCREENSHOTS_DIR):
        full = os.path.join(SCREENSHOTS_DIR, name)
        if not os.path.isdir(full):
            continue
        try:
            folder_date = datetime.strptime(name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if folder_date < cutoff:
            shutil.rmtree(full, ignore_errors=True)


def _take_screenshot_macos(path: str) -> bool:
    """macOS 下用系统自带 screencapture 截主屏并用 sips 降采样。"""
    result = _run_command(["screencapture", "-x", "-t", "png", path], timeout=15)
    if result is None and not os.path.exists(path):
        return False
    _run_command(["sips", "-Z", "1600", path], timeout=15)
    return os.path.exists(path)


def take_screenshot() -> None:
    """截取屏幕并保存为 PNG，随后做保留期清理。

    采集层拦截：
      - 隐私模式开启：完全跳过。
      - 黑名单窗口（Windows）：不跳过整张截图，只把命中窗口的区域打码——
        避免副屏/其他窗口里的工作内容被连带丢失。
      - macOS 无法按窗口打码，保留「前台命中黑名单则跳过」的保守行为。
    """
    if wlmem is not None and wlmem.is_privacy_on():
        print("[截图] 隐私模式开启，跳过")
        return

    today = date.today().isoformat()
    out_dir = os.path.join(SCREENSHOTS_DIR, today)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, datetime.now().strftime("%H%M%S") + ".png")

    system_name = platform.system()
    if system_name == "Windows":
        mask_rects = _get_blacklisted_screen_rects()
        png = _capture_virtual_screen_png(mask_rects=mask_rects)
        if not png:
            print("[截图] 捕获失败")
            return
        try:
            with open(path, "wb") as fp:
                fp.write(png)
        except OSError as error:
            print(f"[截图] 写入失败：{error}")
            return
        size_kb = len(png) // 1024
        if mask_rects:
            print(f"[截图] 已对 {len(mask_rects)} 个黑名单窗口区域打码")
    elif system_name == "Darwin":
        app, title = get_active_window()
        if is_blacklisted(app, title):
            print(f"[截图] 前台窗口命中黑名单（{app} | {(title or '')[:40]}），跳过")
            return
        if not _take_screenshot_macos(path):
            print("[截图] 捕获失败（screencapture）")
            return
        size_kb = os.path.getsize(path) // 1024
    else:
        print(f"[截图] 暂不支持当前系统：{system_name}")
        return

    _cleanup_old_screenshots()
    print(f"[截图] 已保存 {path}（{size_kb} KB）")


def load_phase_notes(target_date: str) -> str:
    """读取某天的分阶段截图内容小结（analysis/<date>.md）。"""
    path = os.path.join(ANALYSIS_DIR, f"{target_date}.md")
    try:
        with open(path, "r", encoding="utf-8") as fp:
            return fp.read().strip()
    except OSError:
        return ""


def _evenly_sample(items: List[str], k: int) -> List[str]:
    """从有序列表中均匀抽取至多 k 个元素（保留首尾、去重保序）。"""
    n = len(items)
    if k <= 0 or n <= k:
        return list(items)
    step = (n - 1) / (k - 1)
    seen = set()
    result: List[str] = []
    for i in range(k):
        item = items[int(round(i * step))]
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _time_label_from_shot(path: str) -> str:
    """从截图文件名 HHMMSS.jpg 推断 HH:MM 标签。"""
    name = os.path.splitext(os.path.basename(path))[0]
    digits = "".join(ch for ch in name if ch.isdigit())
    if len(digits) >= 4:
        return f"{digits[0:2]}:{digits[2:4]}"
    return "??:??"


MEETING_KEYWORDS = [
    "会议", "meeting", "zoom", "teams", "webex", "voov",
    "tblive", "腾讯会议", "钉钉会议", "飞书会议",
]


def _phase_meeting_context(
    conn: sqlite3.Connection, target_date: str, start_label: str, end_label: str
) -> Tuple[int, List[str]]:
    """检测该时段的会议活动。

    Returns:
        (会议累计秒数, 命中的会议应用/窗口名列表)。
    """
    activities = load_today(conn, target_date)
    total_seconds = 0
    names: List[str] = []
    for item in activities:
        try:
            label = datetime.fromisoformat(str(item["timestamp"])).strftime("%H:%M")
        except ValueError:
            continue
        if not (start_label <= label <= end_label):
            continue
        text = f"{item['app_name'] or ''} {item['window_title'] or ''}".lower()
        if any(kw in text for kw in MEETING_KEYWORDS):
            total_seconds += int(item["duration_seconds"])
            name = (item["window_title"] or item["app_name"] or "").strip()[:30]
            if name and name not in names:
                names.append(name)
    return total_seconds, names


def _phase_window_context(
    conn: sqlite3.Connection, target_date: str, start_label: str, end_label: str
) -> str:
    """汇总该时段使用的应用，用于给视觉模型提供上下文。"""
    activities = load_today(conn, target_date)
    app_time: DefaultDict[str, int] = defaultdict(int)
    for item in activities:
        try:
            label = datetime.fromisoformat(str(item["timestamp"])).strftime("%H:%M")
        except ValueError:
            continue
        if start_label <= label <= end_label:
            app = item["app_name"] if item["app_name"] is not None else "Unknown"
            app_time[app] += int(item["duration_seconds"])
    if not app_time:
        return "（该时段无窗口标题记录）"
    parts = [
        f"{app}({_format_duration_seconds(sec)})"
        for app, sec in sorted(app_time.items(), key=lambda kv: -kv[1])
    ]
    return "、".join(parts)


def call_minimax_vision(text_prompt: str, image_paths: List[str]) -> Optional[str]:
    """带截图调用 LLM（视觉），返回文本；异常返回 None。"""
    if not LLM_API_KEY:
        print("[错误] 未配置 LLM_API_KEY，无法做视觉分析")
        return None

    image_blocks: List[Dict[str, object]] = []
    for path in image_paths:
        try:
            with open(path, "rb") as fp:
                raw = fp.read()
        except OSError:
            continue
        b64 = base64.b64encode(raw).decode("ascii")
        media_type = "image/png" if path.lower().endswith(".png") else "image/jpeg"
        image_blocks.append(
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}}
        )

    if not image_blocks:
        return call_minimax(text_prompt)

    system_prompt = "你是专业的工作记录分析助手，擅长从屏幕截图还原用户正在做的具体工作。"

    if LLM_API_FORMAT == "anthropic":
        headers = {
            "x-api-key": LLM_API_KEY,
            "anthropic-version": LLM_ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }
        payload: Dict[str, object] = {
            "model": LLM_MODEL,
            "max_tokens": LLM_MAX_TOKENS,
            "temperature": LLM_TEMPERATURE,
            "system": system_prompt,
            "messages": [
                {"role": "user", "content": image_blocks + [{"type": "text", "text": text_prompt}]}
            ],
        }
        data = _http_post_json(LLM_API_URL, headers, payload)
        return _extract_anthropic_text(data) if data is not None else None

    # OpenAI 兼容视觉格式（image_url + data URL）
    oai_content: List[Dict[str, object]] = []
    for block in image_blocks:
        src = block["source"]
        oai_content.append(
            {"type": "image_url", "image_url": {"url": f"data:{src['media_type']};base64,{src['data']}"}}
        )
    oai_content.append({"type": "text", "text": text_prompt})
    headers = {
        "Authorization": f"{LLM_BEARER_TOKEN_PREFIX} {LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": LLM_MODEL,
        "max_tokens": LLM_MAX_TOKENS,
        "temperature": LLM_TEMPERATURE,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": oai_content},
        ],
    }
    data = _http_post_json(LLM_API_URL, headers, payload)
    if data is None:
        return None
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        print(f"[解析错误] {json.dumps(data, ensure_ascii=False)[:300]}")
        return None


def analyze_phase(conn: sqlite3.Connection, target_date: Optional[str] = None) -> None:
    """分析某天尚未处理的截图，生成一段「阶段小结」并归档截图。

    Args:
        conn: SQLite 连接
        target_date: 目标日期（YYYY-MM-DD），默认今日；凌晨补跑时可指定前一天
    """
    today = target_date or date.today().isoformat()
    shot_dir = os.path.join(SCREENSHOTS_DIR, today)
    if not os.path.isdir(shot_dir):
        print(f"[阶段分析] {today} 暂无截图目录，跳过")
        return

    shots = [p for p in glob.glob(os.path.join(shot_dir, "*.jpg")) if os.path.isfile(p)]
    shots += [p for p in glob.glob(os.path.join(shot_dir, "*.png")) if os.path.isfile(p)]
    shots = sorted(shots, key=lambda p: os.path.basename(p))
    if not shots:
        print("[阶段分析] 无新增截图，跳过")
        return

    start_label = _time_label_from_shot(shots[0])
    end_label = _time_label_from_shot(shots[-1])
    sampled = _evenly_sample(shots, LLM_MAX_IMAGES)
    context = _phase_window_context(conn, today, start_label, end_label)

    profile_block = ""
    if wlmem is not None:
        profile = wlmem.load_profile()
        if profile:
            profile_block = f"\n用户工作档案（用于判断屏幕内容是否属于用户本人的工作）：\n{profile[:1500]}\n"
    blocked_topics = "、".join(get_blacklist())

    meeting_seconds, meeting_names = _phase_meeting_context(conn, today, start_label, end_label)
    meeting_block = ""
    if meeting_seconds >= 300:
        meeting_block = (
            f"\n【会议警告】该时段检测到会议进行中"
            f"（{('、'.join(meeting_names[:3]) or '会议应用')}，累计约 {meeting_seconds // 60} 分钟）。"
            "会议期间屏幕上出现的幻灯片、文档、表格、演示画面极可能是**他人投屏/共享的内容**，"
            "一律不得描述为用户本人的工作或产出；对会议时段只写"
            "「参加了…会议，讨论主题涉及…」这类客观描述。"
            "只有能明确看到用户本人在编辑/输入（非会议窗口）的内容才可写成用户的操作。\n"
        )

    prompt = f"""这是用户在 {start_label}–{end_label} 期间的电脑屏幕截图（从该时段 {len(shots)} 张中均匀抽取 {len(sampled)} 张），以及期间使用的应用：
{context}
{profile_block}{meeting_block}
请用 2–4 条简洁中文，概括这段时间用户本人在做的具体工作：涉及的项目 / 任务 / 文件 / 内容要点。

判别规则（必须遵守）：
1. 截图只能证明「屏幕上显示了什么」，不能证明「是用户做的」。聊天窗口里他人发的消息、他人的文档、会议共享/投屏画面、网页文章、AI 对话中模型生成的内容，一律不得描述为用户的工作产出。
1b. 屏幕上出现会议窗口（钉钉/腾讯/Zoom/Teams 等）时，画面中的演示内容默认按「他人投屏」处理，写「参加了…会议（主题…）」，不要把投屏材料写成用户在编写。
2. 动词要区分：能看到用户在输入/编辑/操作的，才可写「编写/修改/调试」；只是打开着的内容写「查看/浏览」。
3. 没有把握的判断，条目末尾标注「（推测）」；完全无法判断就不写。
4. 不得从屏幕上抄录数字/参数当作用户的工作结论，除非能看出是用户本人在编写这些内容。
5. 以下主题与工作无关，即使出现在截图中也严禁写入小结：{blocked_topics}；此外一切娱乐、购物、私人聊天内容一律忽略。

直接输出要点，不要客套与解释。"""

    summary = call_minimax_vision(prompt, sampled)
    if not summary:
        print("[阶段分析] LLM 无响应，保留截图待下次重试")
        return

    os.makedirs(ANALYSIS_DIR, exist_ok=True)
    apath = os.path.join(ANALYSIS_DIR, f"{today}.md")
    header = f"### {start_label}–{end_label}（{len(shots)} 张截图，抽样 {len(sampled)}）"
    with open(apath, "a", encoding="utf-8") as fp:
        fp.write(f"\n{header}\n\n{summary.strip()}\n")

    # 归档已分析截图，避免重复分析（仍留在磁盘，由保留策略统一清理）
    done_dir = os.path.join(shot_dir, "_analyzed")
    os.makedirs(done_dir, exist_ok=True)
    for shot in shots:
        try:
            shutil.move(shot, os.path.join(done_dir, os.path.basename(shot)))
        except OSError:
            pass

    print(
        f"[阶段分析] 已完成 {start_label}–{end_label}：分析 {len(shots)} 张"
        f"（发送 {len(sampled)} 张），写入 {apath}"
    )


def _weekday_cn(d: date) -> str:
    """返回中文星期标签。"""
    names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    return names[d.weekday()]


def _extract_daily_ai_section(text: str) -> str:
    """从日报 Markdown 中截取「AI 生成日报」正文段，失败则返回全文。"""
    start = text.find("## AI 生成日报")
    if start == -1:
        return text.strip()
    body = text[start + len("## AI 生成日报"):]
    for marker in ("## 分阶段内容小结", "## 采集摘要"):
        idx = body.find(marker)
        if idx != -1:
            body = body[:idx]
    return body.strip()


def generate_weekly_report(conn: sqlite3.Connection, anchor_date: Optional[str] = None) -> None:
    """基于某周（周一~周五）的日报，汇总生成周报。"""
    anchor = date.fromisoformat(anchor_date) if anchor_date else date.today()
    monday = anchor - timedelta(days=anchor.weekday())
    friday = monday + timedelta(days=4)

    sections: List[str] = []
    current = monday
    while current <= friday:
        rp = os.path.join(REPORT_DIR, f"daily-report-{current.isoformat()}.md")
        if os.path.exists(rp):
            try:
                with open(rp, "r", encoding="utf-8") as fp:
                    txt = fp.read()
                sections.append(
                    f"### {current.isoformat()}（{_weekday_cn(current)}）\n{_extract_daily_ai_section(txt)}"
                )
            except OSError:
                pass
        current += timedelta(days=1)

    if not sections:
        print(f"周报未生成：{monday}~{friday} 本周暂无日报")
        return

    joined = chr(10).join(sections)

    memory_block = ""
    if wlmem is not None:
        memory_block = wlmem.build_memory_block(
            joined,
            exclude_files=[f"weekly-report-{monday.isoformat()}_to_{friday.isoformat()}.md"],
        )
    memory_section = f"\n{memory_block}\n" if memory_block else ""

    prompt = f"""以下是本周（{monday} ~ {friday}，周一至周五）每天的工作日报。请汇总成一份**周报**，格式：

## 本周工作与进展
按项目 / 事项归纳本周实际做的工作与进展。

## 关键事项
列出本周重点投入、阶段性里程碑或值得关注的事项。

## 下周计划建议
仅基于日报中明确「进行中」的工作，给出下周可推进的建议。

【真实性硬规则——违反任何一条都算错误】
1. 周报只能汇总、合并、提炼日报中明确记载的事项，严禁新增日报里没有的细节、数字或结论。
2. 日报中写「查看/浏览/参考」的内容，周报中不得升级为「完成/调研/输出」，也不得归入工作成果。
3. 日报中标注「（待确认）」「（推测）」的条目，若要写入周报必须保留该标注。
4. 他人的工作（同事消息、会议内容、评审他人材料）不得写成用户本人的产出。

要求：中文、专业简洁、合并同类项、突出重点，不要逐日流水账。
{memory_section}
===== 本周每日日报 =====
{joined}
===== 结束 =====

直接输出周报，不要其他解释。"""

    report = call_minimax(prompt)
    if not report:
        print(f"周报未生成：{monday}~{friday} LLM 响应失败")
        return

    os.makedirs(REPORT_DIR, exist_ok=True)
    report_path = os.path.join(
        REPORT_DIR, f"weekly-report-{monday.isoformat()}_to_{friday.isoformat()}.md"
    )
    output_lines = [
        f"# 工作周报 {monday.isoformat()} ~ {friday.isoformat()}",
        "",
        f"*自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}*",
        "",
        f"覆盖日报: {len(sections)} 天",
        "",
        "## AI 生成周报",
        "",
        report.strip(),
    ]
    with open(report_path, "w", encoding="utf-8") as fp:
        fp.write("\n".join(output_lines))

    print(f"周报已写入 Markdown：{report_path}")


def main() -> None:
    """程序主入口与参数处理。"""
    conn = init_db()
    cleanup_old_records(conn)
    is_verbose = WORKLOG_VERBOSE

    parsed_args = sys.argv[1:]
    for arg in parsed_args:
        if arg in {"-v", "--verbose"}:
            is_verbose = True
    normalized_args = [arg for arg in parsed_args if arg not in {"-v", "--verbose"}]

    if len(sys.argv) > 1 and {"-h", "--help"} & set(parsed_args):
        print("用法：")
        print("  python3 claude_auto_report_code_minimax.py")
        print("  python3 claude_auto_report_code_minimax.py --verbose        # 开启啰嗦模式，显示实时窗口进度")
        print("  python3 claude_auto_report_code_minimax.py --report [YYYY-MM-DD]")
        print("  python3 claude_auto_report_code_minimax.py --monthly-report [YYYY-MM]")
        print("  python3 claude_auto_report_code_minimax.py --screenshot                # 截取全部显示器存为 PNG")
        print("  python3 claude_auto_report_code_minimax.py --analyze-phase [YYYY-MM-DD] # 分析该日新增截图，生成阶段小结")
        print("  python3 claude_auto_report_code_minimax.py --weekly-report [YYYY-MM-DD] # 生成所在周(周一~周五)周报")
        conn.close()
        return

    if normalized_args and normalized_args[0] == "--report":
        # 不带日期 = 今天（parse_report_date(None) 返回今日）；带了但非法才报错
        target_date = parse_report_date(normalized_args[1]) if len(normalized_args) > 1 else parse_report_date(None)
        if target_date is None:
            print("[错误] 日期参数非法，正确示例: 2026-02-24")
            conn.close()
            sys.exit(2)
        generate_report(conn, target_date)
        conn.close()
        return

    if normalized_args and normalized_args[0] == "--monthly-report":
        # 不带月份 = 当月；带了但非法才报错
        target_month = parse_report_month(normalized_args[1]) if len(normalized_args) > 1 else parse_report_month(None)
        if target_month is None:
            print("[错误] 月份参数非法，正确示例: 2026-02")
            conn.close()
            sys.exit(2)
        generate_monthly_report(conn, target_month)
        conn.close()
        return

    if normalized_args and normalized_args[0] == "--screenshot":
        take_screenshot()
        conn.close()
        return

    if normalized_args and normalized_args[0] == "--analyze-phase":
        target_date = parse_report_date(normalized_args[1]) if len(normalized_args) > 1 else None
        if len(normalized_args) > 1 and target_date is None:
            print("[错误] 日期参数非法，正确示例: 2026-06-09")
            conn.close()
            return
        analyze_phase(conn, target_date)
        conn.close()
        return

    if normalized_args and normalized_args[0] == "--test-llm":
        print(f"接口: {LLM_API_URL}")
        print(f"模型: {LLM_MODEL}（{LLM_API_FORMAT} 格式）")
        ok = test_llm_connection()
        conn.close()
        if ok:
            print("连接成功：地址 / Key / 模型均有效")
            return
        print("[错误] LLM 连接失败，请检查 API 地址 / Key / 模型名")
        sys.exit(2)

    if normalized_args and normalized_args[0] == "--weekly-report":
        anchor = parse_report_date(normalized_args[1]) if len(normalized_args) > 1 else None
        if len(normalized_args) > 1 and anchor is None:
            print("[错误] 日期参数非法，正确示例: 2026-06-12")
            conn.close()
            return
        generate_weekly_report(conn, anchor)
        conn.close()
        return

    if not check_runtime_environment():
        conn.close()
        return

    if not acquire_collect_mutex():
        print("[采集] 已有采集进程在运行（互斥锁占用），本实例退出")
        conn.close()
        return

    if is_verbose:
        print("🟢 WorkLog 已启动，正在采集窗口活动...")
        print("   按 Ctrl+C 结束并自动生成日报\n")

    last_app: Optional[str] = None
    last_title: Optional[str] = None
    last_start: Optional[float] = None
    pending_app: Optional[str] = None
    pending_title: Optional[str] = None
    pending_start: Optional[float] = None
    pending_count = 0
    is_shutting_down = False
    last_progress_line: Optional[str] = None
    last_progress_ts = 0.0
    last_cleanup_ts = 0.0

    def flush_current_segment(now: float) -> None:
        """将当前窗口片段尽快持久化，用于异常退出前兜底。"""
        nonlocal last_app, last_title, last_start
        if last_app and last_start:
            save_record(conn, last_app, last_title, last_start, now)
            last_start = now

    def shutdown(signal_num: int, _frame: Optional[object]) -> None:
        """处理退出信号：保存最后一段记录并输出日报。"""
        nonlocal last_app, last_title, last_start
        nonlocal pending_app, pending_title, pending_start, pending_count, is_shutting_down
        if is_shutting_down:
            return
        is_shutting_down = True
        if WORKLOG_PROGRESS_BAR and is_verbose:
            print()
        print("\n\n🔴 采集结束，正在生成日报...")
        now = time.time()
        if pending_app and pending_start and pending_count >= WINDOW_STABLE_COUNT:
            save_record(conn, pending_app, pending_title, pending_start, now)
        flush_current_segment(now)
        cleanup_old_records(conn)

        pending_app = None
        pending_title = None
        pending_start = None
        pending_count = 0
        last_app = None
        last_title = None
        last_start = None

        generate_report(conn)
        conn.close()
        sys.exit(0)

    def safe_exit() -> None:
        """进程退出时的兜底函数：只持久化当前片段，不强制生成日报。"""
        nonlocal is_shutting_down
        if is_shutting_down:
            return
        is_shutting_down = True
        flush_current_segment(time.time())
        if conn:
            cleanup_old_records(conn)
            conn.close()

    atexit.register(safe_exit)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, shutdown)
    if hasattr(signal, "SIGQUIT"):
        signal.signal(signal.SIGQUIT, shutdown)

    while True:
        try:
            # 21:00 自动收工：到点保存现场并退出（出报由 21:00 计划任务负责），
            # 即使出报任务因关机/睡眠没跑，采集进程也不会熬夜变成孤儿重复计时。
            current_hour = datetime.now().hour
            if current_hour >= 21 or current_hour < 9:
                now = time.time()
                if pending_app and pending_start and pending_count >= WINDOW_STABLE_COUNT:
                    save_record(conn, pending_app, pending_title, pending_start, now)
                flush_current_segment(now)
                is_shutting_down = True
                print("[采集] 已到 21:00 收工时间，保存现场并自动停止")
                break

            # GUI 请求停止采集：保存现场后优雅退出（不出报，出报由 GUI/计划任务触发）
            if wlmem is not None and wlmem.consume_stop_request():
                now = time.time()
                if pending_app and pending_start and pending_count >= WINDOW_STABLE_COUNT:
                    save_record(conn, pending_app, pending_title, pending_start, now)
                flush_current_segment(now)
                is_shutting_down = True
                print("[采集] 收到停止指令，保存现场并退出")
                break

            # 隐私模式：先落盘当前片段，然后暂停记录直到关闭
            if wlmem is not None and wlmem.is_privacy_on():
                now = time.time()
                if last_app and last_start:
                    save_record(conn, last_app, last_title, last_start, now)
                    last_app = None
                    last_title = None
                    last_start = None
                pending_app = None
                pending_title = None
                pending_count = 0
                pending_start = None
                time.sleep(COLLECT_INTERVAL)
                continue

            app, title = get_active_window()
            now = time.time()
            if now - last_cleanup_ts >= WORKLOG_CLEANUP_INTERVAL_SECONDS:
                deleted_count = cleanup_old_records(conn)
                if ENABLE_ACTIVITY_LOG and deleted_count > 0:
                    print(f"🧹 清理历史记录：已删除 {deleted_count} 条（保留 {WORKLOG_RETENTION_DAYS} 天）")
                last_cleanup_ts = now
            if is_verbose:
                status_app = app or last_app or "等待窗口"
                status_title = title or last_title or "(无窗口)"
                status_start = last_start or pending_start or now
                progress_seconds = int(now - status_start)
                progress_line = (
                    f"⏱  当前窗口: {status_app} | {_format_duration_seconds(progress_seconds)} "
                    f"| {status_title[:45]}"
                )
                recent_records = load_recent_records(conn, limit=WORKLOG_RECENT_RECORDS)
                progress_lines = build_progress_lines(progress_line, recent_records)
                progress_content = "\n".join(progress_lines)
                if last_progress_line != progress_content or now - last_progress_ts >= WORKLOG_PROGRESS_INTERVAL:
                    print_progress_lines(progress_lines)
                    last_progress_line = progress_content
                    last_progress_ts = now

            if app is None:
                if last_app and last_start:
                    save_record(conn, last_app, last_title, last_start, now)
                    last_app = None
                    last_title = None
                    last_start = None
                pending_app = None
                pending_title = None
                pending_count = 0
                pending_start = None
                time.sleep(COLLECT_INTERVAL)
                continue

            if app == last_app and title == last_title:
                if last_start is not None and (now - last_start) >= FLUSH_INTERVAL:
                    save_record(conn, last_app, last_title, last_start, now)
                    last_start = now

                pending_app = None
                pending_title = None
                pending_count = 0
                pending_start = None
                time.sleep(COLLECT_INTERVAL)
                continue

            if app != pending_app or title != pending_title:
                pending_app = app
                pending_title = title
                pending_count = 1
                pending_start = now
            else:
                pending_count += 1

            if pending_count >= WINDOW_STABLE_COUNT:
                if last_app and last_start:
                    # 真实切换点是首次检测到窗口变化的时间（pending_start）
                    cut_time = pending_start if pending_start is not None else now
                    save_record(conn, last_app, last_title, last_start, cut_time)

                last_app = pending_app
                last_title = pending_title
                last_start = pending_start
                pending_app = None
                pending_title = None
                pending_count = 0
                pending_start = None

            time.sleep(COLLECT_INTERVAL)
        except Exception as error:
            print(f"[错误] {error}")
            time.sleep(COLLECT_INTERVAL)


if __name__ == "__main__":
    main()
