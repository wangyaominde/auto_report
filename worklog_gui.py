#!/usr/bin/env python3
"""WorkLog GUI —— 原生窗口 + 系统托盘（Windows / macOS）。

技术栈：pywebview（原生窗口，Win=WebView2 / Mac=WKWebView）+ pystray + Pillow（托盘与图标）。
引擎仍是纯标准库的 claude_auto_report_code_minimax.py，本 GUI 只是调用方：
  - 报告生成 / 截图 / 阶段分析：subprocess 调引擎 CLI（与计划任务同一入口）
  - 采集启停：subprocess 启动 + 停止标志文件（worklog_memory）
  - 记忆库 / 黑名单 / 隐私模式：直接读写 worklog_memory

启动：python worklog_gui.py（Windows 建议用 start-gui.ps1 / pythonw 免控制台）
"""

import os
import platform
import re
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 冻结（PyInstaller 打包）支持：
#   RESOURCE_DIR —— 只读资源（ui/、引擎模块），打包后位于解包目录
#   DATA_DIR     —— 可写数据（.env/报告/截图/记忆/黑名单），打包后固定在 ~/WorkLog
# 通过 WORKLOG_DATA_DIR 环境变量传给引擎与记忆模块（子进程继承）
IS_FROZEN = bool(getattr(sys, "frozen", False))
RESOURCE_DIR = getattr(sys, "_MEIPASS", SCRIPT_DIR) if IS_FROZEN else SCRIPT_DIR
DATA_DIR = os.path.join(os.path.expanduser("~"), "WorkLog") if IS_FROZEN else SCRIPT_DIR
os.makedirs(DATA_DIR, exist_ok=True)
os.environ.setdefault("WORKLOG_DATA_DIR", DATA_DIR)
sys.path.insert(0, SCRIPT_DIR)

# pythonw 下 sys.stdout/stderr 为 None：引擎导入时会调 sys.stdout.isatty()，
# 且任何 print 都会炸——先垫上空设备流
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")
# 非中文 locale 的管道默认 cp1252 等编码，打印中文会 UnicodeEncodeError（打包版必现）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

import worklog_memory as wlmem  # noqa: E402
import claude_auto_report_code_minimax as engine  # noqa: E402

try:
    import webview
except ImportError:
    print("缺少依赖：pip install pywebview pystray pillow")
    sys.exit(1)

ENGINE_PATH = os.path.join(RESOURCE_DIR, "claude_auto_report_code_minimax.py")
ENV_PATH = os.path.join(DATA_DIR, ".env")
UI_INDEX = os.path.join(RESOURCE_DIR, "ui", "index.html")
ASSETS_DIR = os.path.join(DATA_DIR, "assets")
REPORTS_DIR = os.path.join(DATA_DIR, "reports")
IS_WINDOWS = platform.system() == "Windows"
IS_MACOS = platform.system() == "Darwin"

_window = None
_tray = None
_quitting = False

_REPORT_PATTERNS = {
    "daily": re.compile(r"^daily-report-(\d{4}-\d{2}-\d{2})\.md$"),
    "weekly": re.compile(r"^weekly-report-(\d{4}-\d{2}-\d{2})_to_(\d{4}-\d{2}-\d{2})\.md$"),
    "monthly": re.compile(r"^monthly-report-(\d{4}-\d{2})\.md$"),
}


def _python_exe() -> str:
    """优先用当前解释器同目录的 python.exe（pythonw 启动时子进程也能正常跑）。"""
    exe = sys.executable
    if IS_WINDOWS and exe.lower().endswith("pythonw.exe"):
        candidate = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(candidate):
            return candidate
    return exe


def _child_env():
    """子进程环境：剔除 GUI 启动时从旧 .env 继承的 LLM 配置，让子进程重新读 .env。

    （引擎的 _load_dotenv 不覆盖已存在的环境变量，若不剔除，GUI 保存的新
    Key/模型要等 GUI 重启才生效。）
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LLM_", "MINIMAX_"))}
    env["PYTHONIOENCODING"] = "utf-8"  # 子进程中文输出统一 UTF-8，防乱码
    return env


def _engine_cmd(args):
    """构造引擎子进程命令：打包版用自身 exe + --engine 分发，开发版直接跑脚本。"""
    if IS_FROZEN:
        return [sys.executable, "--engine"] + args
    return [_python_exe(), ENGINE_PATH] + args


def _run_engine(args, timeout=600):
    """调引擎 CLI，返回 (成功, 输出末尾)。"""
    creationflags = 0x08000000 if IS_WINDOWS else 0  # CREATE_NO_WINDOW
    env = _child_env()
    try:
        result = subprocess.run(
            _engine_cmd(args),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=DATA_DIR,
            timeout=timeout,
            creationflags=creationflags,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return False, "执行超时"
    except OSError as error:
        return False, str(error)
    output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
    # 引擎部分错误路径 print 后仍以退出码 0 返回，必须按输出内容判定失败
    failure_markers = ("[错误]", "[API 错误]", "[网络错误]", "[解析错误]", "未生成")
    ok = result.returncode == 0 and not any(m in output for m in failure_markers)
    return ok, output[-800:]


class Api:
    """暴露给前端 JS 的桥接接口（window.pywebview.api.*）。"""

    # ---------- 状态 ----------

    def status(self):
        today = date.today().isoformat()
        records = 0
        last_ts = None
        try:
            conn = sqlite3.connect(engine.DB_PATH)
            row = conn.execute(
                "SELECT COUNT(*), MAX(timestamp) FROM activity WHERE date = ?", (today,)
            ).fetchone()
            conn.close()
            records = row[0] or 0
            last_ts = row[1]
        except sqlite3.Error:
            pass

        collecting = False
        if last_ts:
            try:
                age = (datetime.now() - datetime.fromisoformat(last_ts)).total_seconds()
                collecting = age < 90  # 采集进程活跃时至少每 ~35 秒落一条
            except ValueError:
                pass

        shots = 0
        shot_dir = os.path.join(engine.SCREENSHOTS_DIR, today)
        if os.path.isdir(shot_dir):
            for root, _dirs, files in os.walk(shot_dir):
                shots += sum(1 for f in files if f.lower().endswith((".png", ".jpg")))

        return {
            "date": today,
            "collecting": collecting,
            "privacy": wlmem.is_privacy_on(),
            "records": records,
            "screenshots": shots,
            "lastRecord": last_ts or "",
        }

    # ---------- 报告 ----------

    def list_reports(self, kind):
        pattern = _REPORT_PATTERNS.get(kind)
        if pattern is None:
            return []
        items = []
        try:
            names = os.listdir(REPORTS_DIR)
        except OSError:
            return []
        for name in names:
            if pattern.match(name):
                items.append(name)
        items.sort(reverse=True)
        return items

    def read_report(self, name):
        if not self._valid_report_name(name):
            return {"ok": False, "error": "非法文件名"}
        path = os.path.join(REPORTS_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as fp:
                return {"ok": True, "content": fp.read()}
        except OSError as error:
            return {"ok": False, "error": str(error)}

    def regenerate(self, name):
        """按报告文件名重新生成（携带最新记忆/修正）。"""
        for kind, pattern in _REPORT_PATTERNS.items():
            match = pattern.match(name or "")
            if not match:
                continue
            args = {
                "daily": ["--report", match.group(1)],
                "weekly": ["--weekly-report", match.group(1)],
                "monthly": ["--monthly-report", match.group(1)],
            }[kind]
            ok, output = _run_engine(args)
            return {"ok": ok, "output": output}
        return {"ok": False, "output": "无法识别的报告文件名"}

    def generate_today(self):
        ok, output = _run_engine(["--report", date.today().isoformat()])
        return {"ok": ok, "output": output}

    def screenshot_now(self):
        ok, output = _run_engine(["--screenshot"], timeout=60)
        return {"ok": ok, "output": output}

    def analyze_now(self):
        ok, output = _run_engine(["--analyze-phase"])
        return {"ok": ok, "output": output}

    # ---------- 采集控制 ----------

    def collector_start(self):
        wlmem.consume_stop_request()  # 清掉可能残留的停止标志
        creationflags = 0x08000008 if IS_WINDOWS else 0  # CREATE_NO_WINDOW | DETACHED_PROCESS
        kwargs = {"creationflags": creationflags} if IS_WINDOWS else {"start_new_session": True}
        try:
            # --collect 为采集进程标记：引擎按无参路径处理，便于按命令行识别采集进程
            subprocess.Popen(
                _engine_cmd(["--collect"]),
                cwd=DATA_DIR,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=_child_env(),
                **kwargs,
            )
            return {"ok": True}
        except OSError as error:
            return {"ok": False, "output": str(error)}

    def collector_stop(self):
        wlmem.request_collector_stop()
        return {"ok": True}

    # ---------- 隐私 / 黑名单 / 记忆 ----------

    def privacy_set(self, enabled):
        wlmem.set_privacy(bool(enabled))
        _update_tray_menu()
        return {"ok": True, "privacy": wlmem.is_privacy_on()}

    def blacklist_get(self):
        return wlmem.load_blacklist(engine.DEFAULT_BLACKLIST)

    def blacklist_save(self, keywords):
        if not isinstance(keywords, list):
            return {"ok": False}
        wlmem.save_blacklist([str(k) for k in keywords])
        return {"ok": True}

    def profile_get(self):
        return wlmem.load_profile()

    def profile_save(self, text):
        wlmem.save_profile(str(text or ""))
        return {"ok": True}

    def corrections_list(self):
        return wlmem.load_corrections()

    def corrections_add(self, text, report="", reason="not_mine"):
        text = (text or "").strip()
        if not text:
            return {"ok": False}
        entry = wlmem.add_correction(text, report=report or "", reason=reason or "not_mine")
        return {"ok": True, "entry": entry}

    def corrections_delete(self, entry_id):
        return {"ok": wlmem.delete_correction(str(entry_id))}

    # ---------- 大模型设置（读写 .env，引擎子进程即时生效） ----------

    LLM_ENV_KEYS = (
        "LLM_API_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_API_FORMAT",
        "LLM_MAX_TOKENS", "LLM_TEMPERATURE", "LLM_MAX_IMAGES",
        "SCREENSHOT_RETENTION_DAYS", "SCREENSHOT_DELETE_AFTER_ANALYSIS",
    )

    def settings_get(self):
        values = self._read_env()[1]
        return {
            "url": values.get("LLM_API_URL", engine.LLM_API_URL),
            "key": values.get("LLM_API_KEY", ""),
            "model": values.get("LLM_MODEL", engine.LLM_MODEL),
            "format": values.get("LLM_API_FORMAT", engine.LLM_API_FORMAT),
            "maxTokens": values.get("LLM_MAX_TOKENS", str(engine.LLM_MAX_TOKENS)),
            "temperature": values.get("LLM_TEMPERATURE", str(engine.LLM_TEMPERATURE)),
            "maxImages": values.get("LLM_MAX_IMAGES", str(engine.LLM_MAX_IMAGES)),
            "shotRetentionDays": values.get(
                "SCREENSHOT_RETENTION_DAYS", str(engine.SCREENSHOT_RETENTION_DAYS)
            ),
            "shotDeleteAfterAnalysis": values.get(
                "SCREENSHOT_DELETE_AFTER_ANALYSIS", ""
            ).strip().lower() in {"1", "true", "yes", "on"},
        }

    def settings_save(self, cfg):
        if not isinstance(cfg, dict):
            return {"ok": False, "output": "参数错误"}
        mapping = {
            "LLM_API_URL": str(cfg.get("url", "")).strip(),
            "LLM_API_KEY": str(cfg.get("key", "")).strip(),
            "LLM_MODEL": str(cfg.get("model", "")).strip(),
            "LLM_API_FORMAT": str(cfg.get("format", "")).strip().lower(),
            "LLM_MAX_TOKENS": str(cfg.get("maxTokens", "")).strip(),
            "LLM_TEMPERATURE": str(cfg.get("temperature", "")).strip(),
            "LLM_MAX_IMAGES": str(cfg.get("maxImages", "")).strip(),
            "SCREENSHOT_RETENTION_DAYS": str(cfg.get("shotRetentionDays", "")).strip(),
            "SCREENSHOT_DELETE_AFTER_ANALYSIS": (
                "1" if cfg.get("shotDeleteAfterAnalysis") else "0"
            ),
        }
        if not mapping["LLM_API_URL"] or not mapping["LLM_API_KEY"]:
            return {"ok": False, "output": "API 地址与 Key 不能为空"}

        lines, _values = self._read_env()
        seen = set()
        new_lines = []
        for line in lines:
            stripped = line.strip()
            key = stripped.partition("=")[0].strip() if "=" in stripped and not stripped.startswith("#") else None
            if key in mapping:
                if mapping[key]:
                    new_lines.append(f"{key}={mapping[key]}")
                seen.add(key)
            else:
                new_lines.append(line)
        for key, value in mapping.items():
            if key not in seen and value:
                new_lines.append(f"{key}={value}")

        try:
            with open(ENV_PATH, "w", encoding="utf-8") as fp:
                fp.write("\n".join(new_lines).rstrip() + "\n")
        except OSError as error:
            return {"ok": False, "output": str(error)}
        return {"ok": True}

    def settings_test(self):
        ok, output = _run_engine(["--test-llm"], timeout=180)
        return {"ok": ok, "output": output}

    @staticmethod
    def _read_env():
        """读 .env，返回 (原始行列表, {key: value})。"""
        lines = []
        values = {}
        try:
            with open(ENV_PATH, "r", encoding="utf-8-sig") as fp:
                for raw in fp:
                    line = raw.rstrip("\n")
                    lines.append(line)
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#") or "=" not in stripped:
                        continue
                    key, _, value = stripped.partition("=")
                    values[key.strip()] = value.strip().strip('"').strip("'")
        except OSError:
            pass
        return lines, values

    # ---------- 杂项 ----------

    def open_folder(self, kind):
        target = {
            "reports": REPORTS_DIR,
            "screenshots": engine.SCREENSHOTS_DIR,
            "analysis": engine.ANALYSIS_DIR,
            "memory": wlmem.MEMORY_DIR,
        }.get(kind, SCRIPT_DIR)
        os.makedirs(target, exist_ok=True)
        try:
            if IS_WINDOWS:
                os.startfile(target)  # noqa: S606
            elif IS_MACOS:
                subprocess.Popen(["open", target])
            else:
                subprocess.Popen(["xdg-open", target])
            return {"ok": True}
        except OSError as error:
            return {"ok": False, "output": str(error)}

    @staticmethod
    def _valid_report_name(name):
        return bool(name) and any(p.match(name) for p in _REPORT_PATTERNS.values())


# ============================================================
# 内置调度器 —— 接管原计划任务的全部职能（GUI 常驻托盘时生效）
#   采集守护(09-21) / 截图(5分钟) / 阶段分析(11-19点每2小时) /
#   日报(21:00) / 周报(周五21:05) / 启动补漏(最近7天)
# ============================================================

SHOT_INTERVAL_SECONDS = 300
ANALYZE_HOURS = (11, 13, 15, 17, 19)


def _daily_report_exists(day_str):
    return os.path.exists(os.path.join(REPORTS_DIR, f"daily-report-{day_str}.md"))


def _weekly_report_exists(friday):
    monday = friday - timedelta(days=4)
    return os.path.exists(os.path.join(
        REPORTS_DIR, f"weekly-report-{monday.isoformat()}_to_{friday.isoformat()}.md"
    ))


def _day_has_data(day_str):
    """该日是否有活动记录或截图分析（空数据日不出报）。"""
    count = 0
    try:
        conn = sqlite3.connect(engine.DB_PATH)
        count = conn.execute(
            "SELECT COUNT(*) FROM activity WHERE date=?", (day_str,)
        ).fetchone()[0]
        conn.close()
    except sqlite3.Error:
        pass
    return count > 0 or os.path.exists(os.path.join(engine.ANALYSIS_DIR, f"{day_str}.md"))


def _last_record_age_seconds():
    try:
        conn = sqlite3.connect(engine.DB_PATH)
        row = conn.execute(
            "SELECT MAX(timestamp) FROM activity WHERE date=?", (date.today().isoformat(),)
        ).fetchone()
        conn.close()
        if row and row[0]:
            return (datetime.now() - datetime.fromisoformat(row[0])).total_seconds()
    except (sqlite3.Error, ValueError):
        pass
    return None


def _last_finished_friday(now):
    """最近一个已过 21:05 的周五（周报目标）。"""
    today = now.date()
    weekday = today.weekday()
    if weekday == 4 and (now.hour, now.minute) >= (21, 5):
        return today
    days_back = (weekday - 4) % 7
    if days_back == 0:
        days_back = 7
    return today - timedelta(days=days_back)


def _catchup_reports():
    """启动补漏：最近 7 天缺失的日报 + 上一个已结束周五的周报。"""
    today = date.today()
    for offset in range(1, 8):
        day = (today - timedelta(days=offset)).isoformat()
        if _daily_report_exists(day) or not _day_has_data(day):
            continue
        _run_engine(["--analyze-phase", day])
        _run_engine(["--report", day])

    friday = _last_finished_friday(datetime.now())
    if not _weekly_report_exists(friday):
        monday = friday - timedelta(days=4)
        has_dailies = any(
            _daily_report_exists((monday + timedelta(days=i)).isoformat()) for i in range(5)
        )
        if has_dailies:
            _run_engine(["--weekly-report", friday.isoformat()])


def _scheduler_loop():
    """30 秒一拍的调度循环。所有出报动作按「目标文件是否存在」天然幂等。"""
    api = Api()
    last_shot_ts = 0.0
    analyzed_slots = set()  # (date_str, hour)

    try:
        _catchup_reports()
    except Exception:
        pass

    while not _quitting:
        try:
            now = datetime.now()
            today = now.date().isoformat()
            in_window = 9 <= now.hour < 21
            privacy = wlmem.is_privacy_on()

            # ① 采集守护：窗口期内若采集进程不活跃则拉起（引擎互斥锁防双开）
            if in_window and not privacy:
                age = _last_record_age_seconds()
                if age is None or age > 120:
                    api.collector_start()

            # ② 截图：每 5 分钟（隐私/黑名单在引擎侧还会再拦一层）
            if in_window and not privacy and time.time() - last_shot_ts >= SHOT_INTERVAL_SECONDS:
                last_shot_ts = time.time()
                _run_engine(["--screenshot"], timeout=60)

            # ③ 阶段分析：11/13/15/17/19 点各一次
            if now.hour in ANALYZE_HOURS and (today, now.hour) not in analyzed_slots:
                analyzed_slots.add((today, now.hour))
                _run_engine(["--analyze-phase", today])

            # ④ 日报：21:00 后当天日报缺失则生成（末段先分析）
            if now.hour >= 21 and not _daily_report_exists(today) and _day_has_data(today):
                _run_engine(["--analyze-phase", today])
                _run_engine(["--report", today])

            # ⑤ 周报：周五 21:05 后本周周报缺失则生成
            if now.weekday() == 4 and (now.hour, now.minute) >= (21, 5):
                friday = now.date()
                if not _weekly_report_exists(friday):
                    _run_engine(["--weekly-report", friday.isoformat()])
        except Exception:
            pass
        time.sleep(30)


# ============================================================
# 图标与托盘
# ============================================================


def _build_icon_image():
    """用 Pillow 画应用图标：靛蓝渐变圆角方块 + 白色 W 折线。"""
    from PIL import Image, ImageDraw

    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 竖向渐变（靛蓝 -> 紫），画在圆角矩形蒙版内
    top, bottom = (79, 70, 229), (147, 51, 234)
    gradient = Image.new("RGBA", (size, size))
    gdraw = ImageDraw.Draw(gradient)
    for y in range(size):
        t = y / (size - 1)
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,)
        gdraw.line([(0, y), (size, y)], fill=color)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([8, 8, size - 8, size - 8], radius=56, fill=255)
    img.paste(gradient, (0, 0), mask)

    # 白色 W 折线
    w = [(58, 88), (92, 180), (128, 112), (164, 180), (198, 88)]
    draw = ImageDraw.Draw(img)
    draw.line(w, fill=(255, 255, 255, 255), width=22, joint="curve")
    for point in (w[0], w[-1]):
        draw.ellipse([point[0] - 11, point[1] - 11, point[0] + 11, point[1] + 11],
                     fill=(255, 255, 255, 255))
    return img


def _ensure_icons():
    """生成 assets/icon.png 与 icon.ico（存在则跳过），返回 PIL Image。"""
    from PIL import Image

    os.makedirs(ASSETS_DIR, exist_ok=True)
    png_path = os.path.join(ASSETS_DIR, "icon.png")
    ico_path = os.path.join(ASSETS_DIR, "icon.ico")
    if os.path.exists(png_path):
        img = Image.open(png_path).convert("RGBA")
    else:
        img = _build_icon_image()
        img.save(png_path)
    if IS_WINDOWS and not os.path.exists(ico_path):
        img.save(ico_path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
    return img


_GUI_MUTEX_HANDLE = None
WINDOW_TITLE = "WorkLog 工作日报"


def _acquire_gui_singleton():
    """GUI 单实例：已有实例时把它的窗口带到前台并返回 False。"""
    global _GUI_MUTEX_HANDLE
    if not IS_WINDOWS:
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, "WorkLog_GUI_Singleton")
        if handle and kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            hwnd = user32.FindWindowW(None, WINDOW_TITLE)
            if hwnd:
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
            return False
        _GUI_MUTEX_HANDLE = handle
        return True
    except Exception:
        return True


def _show_window():
    if _window is not None:
        _window.show()
        _window.restore()


def _quit_app():
    global _quitting
    _quitting = True
    if _tray is not None:
        try:
            _tray.stop()
        except Exception:
            pass
    if _window is not None:
        _window.destroy()


def _toggle_privacy():
    wlmem.set_privacy(not wlmem.is_privacy_on())
    _update_tray_menu()


def _update_tray_menu():
    if _tray is not None:
        try:
            _tray.update_menu()
        except Exception:
            pass


def _tray_generate_report():
    threading.Thread(
        target=lambda: _run_engine(["--report", date.today().isoformat()]), daemon=True
    ).start()


def _build_tray(image):
    import pystray

    menu = pystray.Menu(
        pystray.MenuItem("打开 WorkLog 面板", lambda: _show_window(), default=True),
        pystray.MenuItem(
            "隐私模式（暂停采集与截图）",
            lambda: _toggle_privacy(),
            checked=lambda item: wlmem.is_privacy_on(),
        ),
        pystray.MenuItem("立即生成今日日报", lambda: _tray_generate_report()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出（停止自动采集与出报）", lambda: _quit_app()),
    )
    return pystray.Icon("WorkLog", image, "WorkLog 工作日报", menu)


def _start_tray():
    """webview 启动后：挂托盘 + 启动内置调度器（接管原计划任务职能）。"""
    global _tray
    try:
        image = _ensure_icons()
        _tray = _build_tray(image)
        if IS_MACOS:
            _tray.run_detached()
        else:
            threading.Thread(target=_tray.run, daemon=True).start()
    except Exception as error:
        print(f"[托盘] 启动失败（窗口仍可用）：{error}")
    threading.Thread(target=_scheduler_loop, daemon=True).start()


def _on_closing():
    """点关闭按钮 = 隐藏到托盘；只有托盘「退出」才真正退出。"""
    if _quitting or _tray is None:
        return True
    _window.hide()
    return False


def main():
    global _window
    if not _acquire_gui_singleton():
        return
    _window = webview.create_window(
        WINDOW_TITLE,
        UI_INDEX,
        js_api=Api(),
        width=1180,
        height=780,
        min_size=(940, 620),
        background_color="#f5f6fa",
    )
    _window.events.closing += _on_closing
    webview.start(_start_tray, debug=False)


def _log_crash() -> str:
    """崩溃写入 DATA_DIR/logs/gui-crash.log，返回日志路径。"""
    import traceback

    log_dir = os.path.join(DATA_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, "gui-crash.log")
    with open(path, "a", encoding="utf-8") as fp:
        fp.write(f"\n===== {datetime.now().isoformat()} argv={sys.argv[1:]} =====\n")
        fp.write(traceback.format_exc())
    return path


if __name__ == "__main__":
    # 打包版引擎分发：WorkLog.exe --engine <引擎参数...>（采集/截图/分析/出报子进程都走这里）
    if len(sys.argv) > 1 and sys.argv[1] == "--engine":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        try:
            engine.main()
        except SystemExit:
            raise
        except Exception:
            _log_crash()
            sys.exit(1)
        sys.exit(0)
    try:
        main()
    except Exception:
        # 注意：windowed 打包版里 raise 会弹阻塞式错误对话框（无人值守环境=永久卡死），
        # 统一改为写日志 + 非零退出；日志在 DATA_DIR/logs/gui-crash.log
        _log_crash()
        sys.exit(1)
