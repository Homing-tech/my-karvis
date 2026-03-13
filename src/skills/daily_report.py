# -*- coding: utf-8 -*-
"""
Skill: daily.generate
V2 — 全新每日总结：能量状态 / 高光时刻 / 进化足迹
"""
import sys
import re
import json
from datetime import datetime, timezone, timedelta
from collections import Counter

BEIJING_TZ = timezone(timedelta(hours=8))

# 感恩关键词
GRATITUDE_KEYWORDS = [
    "谢谢", "感谢", "幸运", "挺好", "真好", "太好了", "开心", "感恩",
    "幸好", "还好", "庆幸", "值得", "温暖", "贴心", "暖心", "好棒",
    "厉害", "不错", "满足", "知足", "幸福", "快乐", "美好", "享受",
]

# 勋章定义
BADGES = {
    "notes_5":   {"icon": "📝", "name": "日记达人",   "desc": "今日记录 ≥ 5 条"},
    "notes_10":  {"icon": "🔥", "name": "笔耕不辍",   "desc": "今日记录 ≥ 10 条"},
    "done_5":    {"icon": "⚡", "name": "执行力爆表",  "desc": "今日完成 ≥ 5 件事"},
    "done_10":   {"icon": "🏆", "name": "超级战士",    "desc": "今日完成 ≥ 10 件事"},
    "gratitude": {"icon": "💛", "name": "感恩之心",    "desc": "发现 ≥ 3 个感恩瞬间"},
    "mood_high": {"icon": "🌈", "name": "阳光满格",    "desc": "情绪评分 ≥ 8"},
    "streak":    {"icon": "🎯", "name": "连续进化",    "desc": "成就数 ≥ 昨日"},
}


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def execute(params, state, ctx):
    """
    生成今日日报（V2 全新版）。

    params:
        date: str — 可选，指定日期 YYYY-MM-DD，默认今天
    """
    date_str = (params.get("date") or "").strip()
    if not date_str:
        date_str = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")

    _log(f"[daily.generate] 开始生成 {date_str} 日报 (V2)")

    # 1. 收集当天所有笔记内容
    notes = _collect_today_notes(date_str, ctx)

    if not notes.strip():
        _log("[daily.generate] 今天没有笔记内容")
        return {"success": True, "reply": f"今天（{date_str}）还没有记录，无法生成日报"}

    # 2. 本地预处理：提取量化数据
    note_entries = _count_note_entries(notes)
    gratitude_moments = _extract_gratitude(notes)
    keywords = _extract_keywords(notes)

    # 3. 获取昨日 done_count（用于对比波动）
    yesterday_done = _get_yesterday_done(date_str, state)

    # 4. 调用 AI 分析（新版 prompt：Done List + PUMA 情绪 + 感恩）
    from brain import call_deepseek
    analysis = _ai_analyze_v2(notes, date_str, call_deepseek, gratitude_moments)

    if not analysis:
        return {"success": False, "reply": "AI 分析失败，日报生成中止"}

    # 5. 补充量化数据到 analysis
    done_count = len(analysis.get("done_list", []))
    analysis["note_count"] = note_entries
    analysis["done_count"] = done_count
    analysis["yesterday_done"] = yesterday_done
    analysis["done_delta"] = done_count - yesterday_done if yesterday_done >= 0 else None
    analysis["gratitude_moments"] = analysis.get("gratitude_moments", gratitude_moments[:3])
    analysis["keywords"] = keywords[:8]

    # 6. 计算勋章
    analysis["badges"] = _compute_badges(analysis)

    # 7. 保存 done_count 到 state（供明天对比）
    state["last_done_count"] = done_count
    state["last_daily_report"] = date_str

    # 8. 构建日报 Markdown（三区块格式）
    daily_md = _build_daily_report_v2(date_str, analysis, notes)

    # 9. 写入 Daily Note
    file_path = f"{ctx.daily_notes_dir}/{date_str}.md"
    ok = _write_daily_note(ctx, file_path, date_str, daily_md)

    if ok:
        _log(f"[daily.generate] V2 日报已写入: {file_path}")
        # 构建发送到企微的三区块日报（不含原始记录）
        reply = _build_daily_report_for_send(date_str, analysis)
        return {"success": True, "reply": reply}
    else:
        return {"success": False, "reply": "日报写入失败"}


# ============================================================
# 数据收集
# ============================================================

def _collect_today_notes(date_str, ctx):
    """收集当天所有笔记内容（并发读取所有文件）"""
    from concurrent.futures import ThreadPoolExecutor

    files_to_read = {
        "quick_notes": ctx.quick_notes_file,
        "work": f"{ctx.work_notes_dir}/{date_str}.md",
        "emotion": f"{ctx.emotion_notes_dir}/{date_str}.md",
        "fun": f"{ctx.fun_notes_dir}/{date_str}.md",
        "misc": ctx.misc_file,
    }

    results = {}
    try:
        from brain import _executor
        futures = {key: _executor.submit(ctx.IO.read_text, path) for key, path in files_to_read.items()}
    except ImportError:
        _pool = ThreadPoolExecutor(max_workers=5)
        futures = {key: _pool.submit(ctx.IO.read_text, path) for key, path in files_to_read.items()}

    for key, fut in futures.items():
        try:
            results[key] = fut.result(timeout=30) or ""
        except Exception:
            results[key] = ""

    parts = []

    # Quick-Notes
    today_entries = _extract_date_entries(results["quick_notes"], date_str)
    if today_entries:
        parts.extend(["【快速笔记】", today_entries])

    # 分类归档
    for key, label in [("work", "工作笔记"), ("emotion", "情感日记"), ("fun", "生活趣事")]:
        content = results[key].strip()
        if content:
            parts.extend([f"【{label}】", content])

    # 碎碎念
    misc_entries = _extract_date_entries(results["misc"], date_str)
    if misc_entries:
        parts.extend(["【碎碎念】", misc_entries])

    return "\n\n".join(parts)


def _extract_date_entries(text, date_str):
    """从 Markdown 文件中提取指定日期的条目"""
    entries = []
    sections = text.split("\n## ")
    for section in sections[1:]:
        first_line = section.split("\n")[0].strip()
        if first_line.startswith(date_str):
            entries.append("## " + section.strip())
    return "\n\n".join(entries)


# ============================================================
# 本地量化分析（不依赖 AI）
# ============================================================

def _count_note_entries(notes):
    """统计笔记条目数（以 ## 段落为单位）"""
    return max(len(re.findall(r"^##\s", notes, re.MULTILINE)), 1)


def _extract_gratitude(notes):
    """从笔记中提取包含感恩关键词的句子"""
    moments = []
    # 按句子拆分
    sentences = re.split(r"[。！？\n]", notes)
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence or len(sentence) < 4:
            continue
        for kw in GRATITUDE_KEYWORDS:
            if kw in sentence:
                # 截取合理长度
                text = sentence[:80] if len(sentence) > 80 else sentence
                moments.append(text)
                break
    # 去重
    seen = set()
    unique = []
    for m in moments:
        key = m[:20]
        if key not in seen:
            seen.add(key)
            unique.append(m)
    return unique[:5]


def _extract_keywords(notes):
    """提取高频关键词（简单的中文分词 + 词频统计）"""
    # 移除 Markdown 语法
    clean = re.sub(r"[#*\-\[\]()（）「」【】]", " ", notes)
    clean = re.sub(r"https?://\S+", "", clean)

    # 简单的中文关键词提取：2-4 字的连续中文
    words = re.findall(r"[\u4e00-\u9fff]{2,4}", clean)

    # 过滤停用词
    stopwords = {
        "今天", "明天", "昨天", "时候", "可以", "但是", "因为", "所以",
        "一个", "就是", "这个", "那个", "什么", "怎么", "没有", "已经",
        "还是", "自己", "知道", "觉得", "感觉", "现在", "然后", "不是",
        "还有", "或者", "如果", "虽然", "不过", "其实", "应该", "可能",
        "快速笔记", "工作笔记", "情感日记", "生活趣事", "碎碎念",
    }
    filtered = [w for w in words if w not in stopwords]

    counter = Counter(filtered)
    return [word for word, _ in counter.most_common(10)]


def _get_yesterday_done(today_str, state):
    """从 state 获取昨日 done_count，如果不存在返回 -1"""
    return state.get("last_done_count", -1)


def _compute_badges(analysis):
    """根据分析结果计算获得的勋章"""
    earned = []
    note_count = analysis.get("note_count", 0)
    done_count = analysis.get("done_count", 0)
    gratitude_count = len(analysis.get("gratitude_moments", []))
    mood_score = analysis.get("mood_score", 5)
    done_delta = analysis.get("done_delta")

    if note_count >= 10:
        earned.append(BADGES["notes_10"])
    elif note_count >= 5:
        earned.append(BADGES["notes_5"])

    if done_count >= 10:
        earned.append(BADGES["done_10"])
    elif done_count >= 5:
        earned.append(BADGES["done_5"])

    if gratitude_count >= 3:
        earned.append(BADGES["gratitude"])

    if isinstance(mood_score, (int, float)) and mood_score >= 8:
        earned.append(BADGES["mood_high"])

    if done_delta is not None and done_delta >= 0 and analysis.get("yesterday_done", -1) >= 0:
        earned.append(BADGES["streak"])

    return earned


# ============================================================
# AI 分析（V2 增强版 prompt）
# ============================================================

def _ai_analyze_v2(notes, date_str, call_deepseek, gratitude_hints):
    """调用 AI 做增强分析：Done List + PUMA 情绪洞察 + 感恩识别"""
    import prompts

    # 将本地提取的感恩线索传给 AI 作为参考
    gratitude_hint = ""
    if gratitude_hints:
        gratitude_hint = "\n已初步识别的感恩瞬间（供参考，你可以调整）：\n" + "\n".join(f"- {g}" for g in gratitude_hints[:5])

    system_prompt = prompts.DAILY_SYSTEM_V2
    user_prompt = prompts.get("DAILY_USER_V2",
                              date_str=date_str,
                              notes=notes[:4000],
                              gratitude_hint=gratitude_hint)

    response = call_deepseek([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ], max_tokens=1200, temperature=0.7)

    if not response:
        return None

    # 解析 JSON
    text = response.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except Exception:
                pass
    _log(f"[daily.generate] V2 AI 分析 JSON 解析失败: {text[:300]}")
    return None


# ============================================================
# 日报 Markdown 构建（三区块格式）
# ============================================================

def _build_daily_report_v2(date_str, analysis, notes):
    """构建 V2 日报 Markdown：能量状态 / 高光时刻 / 进化足迹"""
    mood = analysis.get("mood", "📝")
    summary = analysis.get("summary", "")
    mood_score = analysis.get("mood_score", "")
    inner_voice = analysis.get("inner_voice", "")
    done_list = analysis.get("done_list", [])
    done_count = analysis.get("done_count", 0)
    done_delta = analysis.get("done_delta")
    gratitude = analysis.get("gratitude_moments", [])
    tags = analysis.get("tags", [])
    keywords = analysis.get("keywords", [])
    note_count = analysis.get("note_count", 0)
    badges = analysis.get("badges", [])

    lines = [f"## 📊 今日总结", ""]

    # ─── 区块一：能量状态 ───
    lines.append("### 🔋 能量状态")
    lines.append("")

    # 情绪 + 评分
    score_bar = ""
    if isinstance(mood_score, (int, float)):
        filled = int(mood_score)
        empty = 10 - filled
        score_bar = f"  {'●' * filled}{'○' * empty} {mood_score}/10"
    lines.append(f"{mood} {summary}")
    if score_bar:
        lines.append(score_bar)
    lines.append("")

    # 智者之声
    if inner_voice:
        lines.append(f"> 🌙 {inner_voice}")
        lines.append("")

    # ─── 区块二：高光时刻 ───
    lines.append("### ✨ 高光时刻")
    lines.append("")

    # Done List 战果
    if done_list:
        delta_str = ""
        if done_delta is not None:
            if done_delta > 0:
                delta_str = f" ↑{done_delta}"
            elif done_delta < 0:
                delta_str = f" ↓{abs(done_delta)}"
            else:
                delta_str = " 持平"
        lines.append(f"**🏅 今日战果** × {done_count}{delta_str}")
        lines.append("")
        for i, item in enumerate(done_list[:10], 1):
            if isinstance(item, dict):
                text = item.get("text", item.get("content", str(item)))
            else:
                text = str(item)
            lines.append(f"  {i}. ✅ {text}")
        lines.append("")

    # 感恩 Top 3
    if gratitude:
        lines.append("**💛 感恩瞬间**")
        lines.append("")
        for i, moment in enumerate(gratitude[:3], 1):
            if isinstance(moment, dict):
                text = moment.get("text", str(moment))
            else:
                text = str(moment)
            lines.append(f"  {i}. 🙏 {text}")
        lines.append("")

    # ─── 区块三：进化足迹 ───
    lines.append("### 📈 进化足迹")
    lines.append("")

    # 标签云
    all_tags = tags + [k for k in keywords if k not in tags]
    if all_tags:
        tag_str = " ".join(f"`{t}`" for t in all_tags[:8])
        lines.append(f"**🏷️ 标签云**: {tag_str}")
        lines.append("")

    # 量化数据
    lines.append("**📊 今日数据**")
    lines.append(f"  - 📝 笔记条数: **{note_count}**")
    lines.append(f"  - ✅ 完成事项: **{done_count}**")
    if done_delta is not None and analysis.get("yesterday_done", -1) >= 0:
        yesterday = analysis["yesterday_done"]
        lines.append(f"  - 📊 昨日对比: {yesterday} → {done_count}")
    lines.append("")

    # 勋章
    if badges:
        badge_strs = [f"{b['icon']} **{b['name']}**" for b in badges]
        lines.append(f"**🎖️ 今日勋章**: {' | '.join(badge_strs)}")
        lines.append("")

    # ─── 原始记录 ───
    lines.extend(["---", "", "## 📝 原始记录", ""])
    if len(notes) > 2000:
        lines.append(notes[:2000] + "\n\n...(更多内容见各分类笔记)")
    else:
        lines.append(notes)

    lines.extend(["", f"*🤖 AI 生成于 {datetime.now(BEIJING_TZ).strftime('%Y-%m-%d %H:%M')}*", ""])

    return "\n".join(lines)



# ============================================================
# 企微发送版日报（三区块，不含原始记录）
# ============================================================

def _build_daily_report_for_send(date_str, analysis):
    """构建发送到企微的精简版三区块日报"""
    mood = analysis.get("mood", "📝")
    summary = analysis.get("summary", "")
    mood_score = analysis.get("mood_score", "")
    inner_voice = analysis.get("inner_voice", "")
    done_list = analysis.get("done_list", [])
    done_count = analysis.get("done_count", 0)
    done_delta = analysis.get("done_delta")
    gratitude = analysis.get("gratitude_moments", [])
    note_count = analysis.get("note_count", 0)

    lines = [f"📊 今天的日报来啦！", ""]

    # ─── 区块一：能量状态 ───
    lines.append("🔋 能量状态")
    lines.append("")

    # 情绪 + 评分
    if isinstance(mood_score, (int, float)):
        filled = int(mood_score)
        empty = 10 - filled
        lines.append(f"{mood} {'●' * filled}{'○' * empty} {mood_score}/10")
    else:
        lines.append(f"{mood}")
    lines.append("")
    lines.append(summary)
    lines.append("")

    # 智者之声
    if inner_voice:
        lines.append(f"🌙 {inner_voice}")
        lines.append("")

    lines.append("─" * 20)
    lines.append("")

    # ─── 区块二：高光时刻 ───
    lines.append("✨ 高光时刻")
    lines.append("")

    # Done List 战果
    if done_list:
        delta_str = ""
        if done_delta is not None:
            if done_delta > 0:
                delta_str = f" ↑{done_delta}"
            elif done_delta < 0:
                delta_str = f" ↓{abs(done_delta)}"
            else:
                delta_str = " 持平"
        lines.append(f"🏅 今日战果 × {done_count}{delta_str}")
        for i, item in enumerate(done_list[:8], 1):
            text = item.get("text", str(item)) if isinstance(item, dict) else str(item)
            lines.append(f"  {i}. ✅ {text}")
        lines.append("")

    # 感恩 Top 3
    if gratitude:
        lines.append("💛 感恩瞬间")
        for i, moment in enumerate(gratitude[:3], 1):
            text = moment.get("text", str(moment)) if isinstance(moment, dict) else str(moment)
            lines.append(f"  {i}. 🙏 {text}")
        lines.append("")

    lines.append("─" * 20)
    lines.append("")

    # ─── 区块三：进化足迹 ───
    lines.append("📈 进化足迹")
    lines.append("")

    # 量化数据
    lines.append(f"📝 笔记 {note_count} 条 | ✅ 完成 {done_count} 件")
    if done_delta is not None and analysis.get("yesterday_done", -1) >= 0:
        yesterday = analysis["yesterday_done"]
        lines.append(f"📊 昨日 {yesterday} → 今日 {done_count}")
    lines.append("")

    return "\n".join(lines).strip()


# ============================================================
# 文件写入（与 V1 相同）
# ============================================================

def _write_daily_note(ctx, file_path, date_str, daily_content):
    """写入 Daily Note，与打卡内容合并"""
    existing = ctx.IO.read_text(file_path)
    if existing is None:
        existing = ""

    if not existing.strip():
        new_content = f"# {date_str}\n\n{daily_content}"
    elif "## 📊 今日总结" in existing:
        parts = existing.split("## 📊 今日总结")
        before = parts[0]
        after_text = parts[1]
        checkin_idx = after_text.find("## 每日复盘")
        if checkin_idx >= 0:
            after = after_text[checkin_idx:]
            new_content = before + daily_content + "\n\n" + after
        else:
            new_content = before + daily_content
    else:
        if "## 每日复盘" in existing:
            parts = existing.split("## 每日复盘")
            new_content = parts[0].rstrip() + "\n\n" + daily_content + "\n\n## 每日复盘" + parts[1]
        else:
            new_content = existing.rstrip() + "\n\n" + daily_content

    return ctx.IO.write_text(file_path, new_content)


# Skill 热加载注册表（O-010）
SKILL_REGISTRY = {
    "daily.generate": execute,
}
