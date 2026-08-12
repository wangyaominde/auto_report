#!/usr/bin/env python3
"""WorkLog 记忆 / 配置模块（纯标准库）。

职责：
  1. 个人工作档案 memory/profile.md —— 告诉模型哪些项目/职责属于用户本人
  2. 修正库 memory/corrections.jsonl —— 用户在 GUI 勾掉的「不是我做的」条目（负面样本）
  3. 历史报告检索 —— 生成周报/月报时检索过去报告相关段落，保持项目名与进展表述一致
  4. 黑名单配置 blacklist.json —— 采集层拦截关键词（GUI 可维护）
  5. 隐私模式 / 停止采集标志文件 —— GUI 与采集进程之间的跨进程开关

被主引擎（claude_auto_report_code_minimax.py）与 GUI（worklog_gui.py）共同引用。
"""

import json
import os
import re
import time
from datetime import datetime
from typing import Dict, List, Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# WORKLOG_DATA_DIR：打包版（PyInstaller）的可写数据目录，由 GUI 设置；未设置时用脚本目录
DATA_DIR = os.getenv("WORKLOG_DATA_DIR", "").strip() or SCRIPT_DIR
MEMORY_DIR = os.path.join(DATA_DIR, "memory")
PROFILE_PATH = os.path.join(MEMORY_DIR, "profile.md")
CORRECTIONS_PATH = os.path.join(MEMORY_DIR, "corrections.jsonl")
BLACKLIST_PATH = os.path.join(DATA_DIR, "blacklist.json")
REPORTS_DIR = os.path.join(DATA_DIR, "reports")

_WORKLOG_HOME = os.path.expanduser("~/.worklog")
PRIVACY_FLAG = os.path.join(_WORKLOG_HOME, "privacy.flag")
STOP_FLAG = os.path.join(_WORKLOG_HOME, "stop.flag")

_blacklist_cache: Optional[List[str]] = None
_blacklist_cache_mtime: float = -1.0


# ============================================================
# 黑名单（采集层拦截关键词）
# ============================================================


def load_blacklist(defaults: Optional[List[str]] = None) -> List[str]:
    """读取黑名单关键词（blacklist.json 优先，缺失时落回默认列表）。

    带 mtime 缓存：采集循环每 5 秒调用一次也不会反复读盘。
    """
    global _blacklist_cache, _blacklist_cache_mtime
    try:
        mtime = os.path.getmtime(BLACKLIST_PATH)
    except OSError:
        return list(defaults or [])

    if _blacklist_cache is not None and mtime == _blacklist_cache_mtime:
        return _blacklist_cache

    try:
        with open(BLACKLIST_PATH, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        keywords = [str(k).strip() for k in data.get("keywords", []) if str(k).strip()]
    except (OSError, json.JSONDecodeError, AttributeError):
        return list(defaults or [])

    _blacklist_cache = keywords
    _blacklist_cache_mtime = mtime
    return keywords


def save_blacklist(keywords: List[str]) -> None:
    """保存黑名单关键词到 blacklist.json（去重保序）。"""
    seen = set()
    cleaned: List[str] = []
    for keyword in keywords:
        text = str(keyword).strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            cleaned.append(text)
    with open(BLACKLIST_PATH, "w", encoding="utf-8") as fp:
        json.dump({"keywords": cleaned}, fp, ensure_ascii=False, indent=2)


# ============================================================
# 隐私模式 / 停止采集（跨进程标志文件）
# ============================================================


def _touch(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        fp.write(datetime.now().isoformat())


def is_privacy_on() -> bool:
    """隐私模式开启时：不记录窗口、不截图。"""
    return os.path.exists(PRIVACY_FLAG)


def set_privacy(enabled: bool) -> None:
    if enabled:
        _touch(PRIVACY_FLAG)
    else:
        try:
            os.remove(PRIVACY_FLAG)
        except OSError:
            pass


def request_collector_stop() -> None:
    """GUI 请求采集进程优雅退出。"""
    _touch(STOP_FLAG)


def consume_stop_request() -> bool:
    """采集循环调用：若存在停止标志则消费掉并返回 True。"""
    if os.path.exists(STOP_FLAG):
        try:
            os.remove(STOP_FLAG)
        except OSError:
            pass
        return True
    return False


# ============================================================
# 个人工作档案
# ============================================================


def load_profile() -> str:
    try:
        with open(PROFILE_PATH, "r", encoding="utf-8") as fp:
            return fp.read().strip()
    except OSError:
        return ""


def save_profile(text: str) -> None:
    os.makedirs(MEMORY_DIR, exist_ok=True)
    with open(PROFILE_PATH, "w", encoding="utf-8") as fp:
        fp.write(text.rstrip() + "\n")


# ============================================================
# 修正库（「不是我做的」负面样本）
# ============================================================


def load_corrections(limit: Optional[int] = None) -> List[Dict[str, object]]:
    """读取修正记录（新→旧）。"""
    entries: List[Dict[str, object]] = []
    try:
        with open(CORRECTIONS_PATH, "r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    entries.reverse()
    return entries[:limit] if limit else entries


def add_correction(text: str, report: str = "", reason: str = "not_mine") -> Dict[str, object]:
    """追加一条修正记录。

    Args:
        text: 被勾掉的报告条目原文
        report: 来源报告文件名
        reason: not_mine（不是我做的）| wrong（内容有误）| private（不想出现）
    """
    entry = {
        "id": f"c{int(time.time() * 1000)}",
        "text": text.strip(),
        "report": report,
        "reason": reason,
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    os.makedirs(MEMORY_DIR, exist_ok=True)
    with open(CORRECTIONS_PATH, "a", encoding="utf-8") as fp:
        fp.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def delete_correction(entry_id: str) -> bool:
    """按 id 删除修正记录，返回是否删除成功。"""
    entries = load_corrections()
    kept = [e for e in entries if e.get("id") != entry_id]
    if len(kept) == len(entries):
        return False
    kept.reverse()  # 恢复旧→新写盘顺序
    with open(CORRECTIONS_PATH, "w", encoding="utf-8") as fp:
        for entry in kept:
            fp.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return True


# ============================================================
# 历史报告检索（轻量关键词匹配，无第三方依赖）
# ============================================================


def _tokenize(text: str) -> List[str]:
    """英文/数字按词、中文按二字组切分。"""
    tokens = [t.lower() for t in re.findall(r"[A-Za-z0-9_\-]{2,}", text)]
    cjk = re.findall(r"[一-鿿]", text)
    tokens += ["".join(pair) for pair in zip(cjk, cjk[1:])]
    return tokens


def _split_sections(markdown: str) -> List[str]:
    """按二级/三级标题把报告拆成段落块。"""
    parts = re.split(r"\n(?=#{2,3} )", markdown)
    return [p.strip() for p in parts if p.strip()]


def retrieve_history(query_text: str, k: int = 5, exclude_files: Optional[List[str]] = None) -> List[Dict[str, str]]:
    """从历史报告中检索与本次素材最相关的段落。

    Args:
        query_text: 本次报告的输入素材（活动摘要/日报合集）
        k: 返回段落数
        exclude_files: 排除的报告文件名（如正在重新生成的那份）

    Returns:
        [{"source": 文件名, "text": 段落}]，按相关度降序。
    """
    query_tokens = set(_tokenize(query_text))
    if not query_tokens:
        return []
    excluded = set(exclude_files or [])

    scored: List[tuple] = []
    try:
        names = sorted(os.listdir(REPORTS_DIR), reverse=True)
    except OSError:
        return []

    for name in names[:120]:  # 最近 ~120 份报告足够
        if not name.endswith(".md") or name in excluded:
            continue
        path = os.path.join(REPORTS_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as fp:
                content = fp.read()
        except OSError:
            continue
        # 只取 AI 正文，不检索原始采集摘要（避免噪声）
        cut = content.find("## 采集摘要")
        if cut != -1:
            content = content[:cut]
        for section in _split_sections(content):
            tokens = _tokenize(section)
            if not tokens:
                continue
            overlap = sum(1 for t in tokens if t in query_tokens)
            score = overlap / (len(tokens) ** 0.5)
            if score > 1.0:
                scored.append((score, name, section))

    scored.sort(key=lambda item: -item[0])
    results: List[Dict[str, str]] = []
    for score, name, section in scored[:k]:
        snippet = section if len(section) <= 800 else section[:800] + "…"
        results.append({"source": name, "text": snippet})
    return results


# ============================================================
# 记忆块组装（注入报告生成提示词）
# ============================================================


def build_memory_block(query_text: str, exclude_files: Optional[List[str]] = None) -> str:
    """组装注入提示词的记忆上下文，无内容时返回空字符串。"""
    sections: List[str] = []

    profile = load_profile()
    if profile:
        sections.append(
            "===== 用户工作档案（判断工作归属的依据） =====\n"
            + profile[:2500]
        )

    corrections = load_corrections(limit=40)
    if corrections:
        lines = []
        for entry in corrections:
            reason = {
                "not_mine": "非本人工作",
                "wrong": "内容有误",
                "private": "不应出现",
            }.get(str(entry.get("reason")), "已否决")
            lines.append(f"- [{reason}] {entry.get('text', '')}")
        sections.append(
            "===== 用户此前否决过的报告条目（严禁把同类内容再写成用户的工作） =====\n"
            + "\n".join(lines)
        )

    history = retrieve_history(query_text, k=5, exclude_files=exclude_files)
    if history:
        lines = []
        for item in history:
            lines.append(f"【{item['source']}】\n{item['text']}")
        sections.append(
            "===== 相关历史报告摘录（仅用于对齐项目名称与进展表述；不得把历史内容当作本期新工作） =====\n"
            + "\n\n".join(lines)
        )

    return "\n\n".join(sections)
