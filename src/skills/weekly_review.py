# -*- coding: utf-8 -*-
"""
Skill: weekly.review
周度开悟报告：基于过去 7 天 daily_report 数据，生成深度总结。

核心逻辑：
1. 数据聚合：汇总 7 天成就按「生存/成长/享受」三维分类 + 趋势对比
2. 模式识别：检索所有认知改写记录，寻找反复纠结的主题，给出破局点
3. 输出格式：Markdown + Emoji + 开悟金句

数据源：
1. 7 天 Daily Note（日报总结 + 打卡）
2. Quick-Notes / 碎碎念 / 归档笔记
3. 情绪评分（state.mood_scores）
4. 上周数据（用于趋势对比）
"""
import sys
import json
from datetime import datetime, timezone, timedelta


BEIJING_TZ = timezone(timedelta(hours=8))


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def execute(params, state, ctx):
    """
    生成周度开悟报告。

    params:
        date: str — 可选，指定周日日期 YYYY-MM-DD，默认最近的周日
    """
    date_str = (params.get("date") or "").strip()
    if not date_str:
        today = datetime.now(BEIJING_TZ).date()
        # 找到本周日（weekday: Mon=0 ... Sun=6）
        days_since_sunday = (today.weekday() + 1) % 7
        sunday = today - timedelta(days=days_since_sunday)
        date_str = sunday.strftime("%Y-%m-%d")

    # 计算本周一到周日
    try:
        end_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return {"success": False, "reply": f"日期格式错误：{date_str}"}

    start_date = end_date - timedelta(days=6)
    period_str = f"{start_date.strftime('%Y-%m-%d')} ~ {end_date.strftime('%Y-%m-%d')}"
    dates = [(start_date + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]

    # 上周日期范围（用于趋势对比）
    last_week_end = start_date - timedelta(days=1)
    last_week_start = last_week_end - timedelta(days=6)
    last_week_dates = [(last_week_start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]

    _log(f"[weekly.review] 生成周度开悟报告: {period_str}")

    # 1. 收集本周 + 上周数据
    this_week_data = _collect_week_data(dates, state, ctx)
    last_week_data = _collect_week_data(last_week_dates, state, ctx)

    if not this_week_data["notes"].strip():
        _log("[weekly.review] 本周没有记录")
        return {"success": True, "reply": f"本周（{period_str}）没有记录，无法生成开悟报告"}

    # 2. 预处理：提取量化对比数据
    comparison = _build_comparison(this_week_data, last_week_data)

    # 3. AI 分析
    from brain import call_deepseek
    analysis = _ai_analyze_enlightenment(this_week_data, comparison, period_str, call_deepseek)

    if not analysis:
        return {"success": False, "reply": "AI 分析失败，开悟报告生成中止"}

    # 补充量化数据
    analysis["activity_summary"] = analysis.get("activity_summary", comparison)

    # 4. 构建 Markdown（文件版）
    review_md = _build_enlightenment_report(period_str, start_date.strftime("%Y-%m-%d"), analysis, this_week_data)

    # 5. 写入文件
    file_path = f"{ctx.daily_notes_dir}/周报-{start_date.strftime('%Y-%m-%d')}.md"
    ok = _write_weekly_review(ctx, file_path, review_md)

    if ok:
        _log(f"[weekly.review] 开悟报告已写入: {file_path}")
        # 6. 构建企微发送版
        reply = _build_enlightenment_for_send(period_str, analysis)
        # 保存到 state
        state["last_weekly_review"] = date_str
        return {"success": True, "reply": reply}
    else:
        return {"success": False, "reply": "开悟报告写入失败"}


# ============================================================
# 数据收集
# ============================================================

def _collect_week_data(dates, state, ctx):
    """并发收集 7 天的所有数据"""
    from concurrent.futures import ThreadPoolExecutor

    files_to_read = {
        "quick_notes": ctx.quick_notes_file,
        "misc": ctx.misc_file,
    }
    for d in dates:
        files_to_read[f"daily_{d}"] = f"{ctx.daily_notes_dir}/{d}.md"
        files_to_read[f"emotion_{d}"] = f"{ctx.emotion_notes_dir}/{d}.md"
        files_to_read[f"fun_{d}"] = f"{ctx.fun_notes_dir}/{d}.md"
        files_to_read[f"work_{d}"] = f"{ctx.work_notes_dir}/{d}.md"

    results = {}
    try:
        from brain import _executor
        executor = _executor
    except ImportError:
        executor = ThreadPoolExecutor(max_workers=6)

    futures = {k: executor.submit(ctx.IO.read_text, v) for k, v in files_to_read.items()}

    for k, fut in futures.items():
        try:
            results[k] = fut.result(timeout=30) or ""
        except Exception:
            results[k] = ""

    # 组装各天的笔记 + 提取日报分析数据
    all_parts = []
    daily_reports = []  # 存放每天的日报结构化数据
    total_notes = 0
    total_done = 0

    for d in dates:
        day_parts = []

        # Quick-Notes 该日条目
        qn_entries = _extract_date_entries(results["quick_notes"], d)
        if qn_entries:
            day_parts.append(qn_entries)
            total_notes += qn_entries.count("\n## ") + (1 if qn_entries.startswith("## ") else 0)

        # 归档笔记
        for key, label in [("emotion", "情感"), ("fun", "趣事"), ("work", "工作")]:
            content = results.get(f"{key}_{d}", "").strip()
            if content:
                day_parts.append(f"[{label}] {content[:500]}")
                total_notes += 1

        # 碎碎念该日条目
        misc_entries = _extract_date_entries(results["misc"], d)
        if misc_entries:
            day_parts.append(f"[碎碎念] {misc_entries[:300]}")

        # Daily Note（日报+打卡）
        daily = results.get(f"daily_{d}", "").strip()
        if daily:
            report_data = _extract_daily_report_data(daily, d)
            if report_data:
                daily_reports.append(report_data)
                total_done += report_data.get("done_count", 0)

            # 提取日报总结
            if "## 📊 今日总结" in daily:
                summary_section = daily.split("## 📊 今日总结")[1]
                end_idx = summary_section.find("\n## 📝 原始记录")
                if end_idx >= 0:
                    summary_section = summary_section[:end_idx]
                elif "\n## " in summary_section:
                    end_idx = summary_section.find("\n## ")
                    summary_section = summary_section[:end_idx]
                day_parts.append(f"[日报] {summary_section.strip()[:600]}")

            # 提取打卡
            if "## 每日复盘" in daily:
                checkin_section = daily.split("## 每日复盘")[1]
                end_idx = checkin_section.find("\n## ")
                if end_idx >= 0:
                    checkin_section = checkin_section[:end_idx]
                day_parts.append(f"[打卡] {checkin_section.strip()[:400]}")

        if day_parts:
            day_text = "\n".join(day_parts)
            all_parts.append(f"=== {d} ===\n{day_text}")

    notes = "\n\n".join(all_parts)

    # 提取情绪评分
    mood_scores = []
    for entry in state.get("mood_scores", []):
        if entry.get("date") in dates:
            mood_scores.append(entry)

    return {
        "notes": notes,
        "daily_reports": daily_reports,
        "mood_scores": mood_scores,
        "total_notes": total_notes,
        "total_done": total_done,
        "dates": dates,
    }


def _extract_date_entries(text, date_str):
    """从 Markdown 文件中提取指定日期的条目"""
    if not text:
        return ""
    entries = []
    sections = text.split("\n## ")
    for section in sections[1:]:
        first_line = section.split("\n")[0].strip()
        if first_line.startswith(date_str):
            entries.append("## " + section.strip())
    return "\n\n".join(entries)


def _extract_daily_report_data(daily_content, date_str):
    """从日报 Markdown 中提取结构化数据（情绪洞察、认知改写、done_list 等）"""
    data = {"date": date_str, "done_count": 0}

    # 提取情绪洞察
    if "🧠 情绪洞察" in daily_content:
        for line in daily_content.split("\n"):
            if "🧠 情绪洞察" in line:
                insight = line.split("🧠 情绪洞察")[1].strip().lstrip(":").lstrip("*:：").strip()
                data["puma_insight"] = insight
                break

    # 提取认知改写
    if "💡 认知改写" in daily_content:
        for line in daily_content.split("\n"):
            if "💡 认知改写" in line:
                rewrite = line.split("💡 认知改写")[1].strip().lstrip(":").lstrip("*:：").strip()
                data["cognitive_rewrite"] = rewrite
                break

    # 统计 done_list（✅ 开头的条目）
    done_count = 0
    done_items = []
    for line in daily_content.split("\n"):
        line = line.strip()
        if "✅" in line and not line.startswith("#"):
            done_count += 1
            # 提取文本内容
            text = line.split("✅")[-1].strip()
            if text:
                done_items.append(text)
    data["done_count"] = done_count
    data["done_items"] = done_items

    # 提取情绪评分
    if "●" in daily_content and "/10" in daily_content:
        import re
        match = re.search(r"(\d+(?:\.\d+)?)/10", daily_content)
        if match:
            try:
                data["mood_score"] = float(match.group(1))
            except ValueError:
                pass

    return data


# ============================================================
# 量化对比
# ============================================================

def _build_comparison(this_week, last_week):
    """构建本周 vs 上周的量化对比"""
    this_notes = this_week.get("total_notes", 0)
    last_notes = last_week.get("total_notes", 0) if last_week["notes"].strip() else None
    this_done = this_week.get("total_done", 0)
    last_done = last_week.get("total_done", 0) if last_week["notes"].strip() else None

    return {
        "this_week_notes": this_notes,
        "last_week_notes": last_notes,
        "this_week_done": this_done,
        "last_week_done": last_done,
    }


# ============================================================
# AI 分析
# ============================================================

def _ai_analyze_enlightenment(data, comparison, period_str, call_deepseek):
    """调用 AI 生成周度开悟报告分析"""
    import prompts

    # 组装本周数据
    parts = []

    # 日报结构化数据（重点：情绪洞察 + 认知改写）
    if data["daily_reports"]:
        parts.append("【本周日报摘要】")
        for report in data["daily_reports"]:
            d = report.get("date", "?")
            score = report.get("mood_score", "?")
            insight = report.get("puma_insight", "无")
            rewrite = report.get("cognitive_rewrite", "无")
            done_items = report.get("done_items", [])
            done_str = "、".join(done_items[:5]) if done_items else "无记录"
            parts.append(f"📅 {d} | 情绪:{score}/10 | 成就:{done_str}")
            if insight != "无":
                parts.append(f"  🧠 情绪洞察: {insight}")
            if rewrite != "无":
                parts.append(f"  💡 认知改写: {rewrite}")

    # 情绪评分
    if data["mood_scores"]:
        parts.append("\n【情绪评分数据】")
        for s in data["mood_scores"]:
            parts.append(f"- {s.get('date', '?')}: {s.get('score', '?')}/10 {s.get('label', '')}")

    # 量化对比
    parts.append(f"\n【量化对比】")
    parts.append(f"- 本周笔记数: {comparison['this_week_notes']}")
    if comparison['last_week_notes'] is not None:
        parts.append(f"- 上周笔记数: {comparison['last_week_notes']}")
    parts.append(f"- 本周完成事项: {comparison['this_week_done']}")
    if comparison['last_week_done'] is not None:
        parts.append(f"- 上周完成事项: {comparison['last_week_done']}")

    # 原始笔记（截断）
    if data["notes"]:
        notes_text = data["notes"][:5000]
        parts.append(f"\n【本周原始记录】\n{notes_text}")

    weekly_data = "\n".join(parts)

    # 额外上下文
    extra_parts = []
    if comparison['last_week_notes'] is not None:
        extra_parts.append(f"- 上周笔记数 {comparison['last_week_notes']}，本周 {comparison['this_week_notes']}")
    if comparison['last_week_done'] is not None:
        extra_parts.append(f"- 上周完成事项 {comparison['last_week_done']}，本周 {comparison['this_week_done']}")
    extra_context = "\n".join(extra_parts) if extra_parts else ""

    user_prompt = prompts.get("WEEKLY_ENLIGHTENMENT_USER",
                              period_str=period_str,
                              weekly_data=weekly_data,
                              extra_context=extra_context)

    response = call_deepseek([
        {"role": "system", "content": prompts.WEEKLY_ENLIGHTENMENT_SYSTEM},
        {"role": "user", "content": user_prompt}
    ], max_tokens=2000, temperature=0.7)

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
    _log(f"[weekly.review] AI 开悟报告 JSON 解析失败: {text[:300]}")
    return None


# ============================================================
# Markdown 构建（文件版 · 写入 01-Daily/周报-{date}.md）
# ============================================================

def _build_enlightenment_report(period_str, start_date_str, analysis, data):
    """构建周度开悟报告 Markdown（文件版）"""
    weekly_summary = analysis.get("weekly_summary", "")
    mood_trend = analysis.get("mood_trend", [])
    mood_avg = analysis.get("mood_avg", "?")
    trend_analysis = analysis.get("trend_analysis", "")
    achievements = analysis.get("achievements", {})
    pattern = analysis.get("pattern_recognition", {})
    breakthrough = analysis.get("breakthrough", {})
    golden_quote = analysis.get("golden_quote", "")
    activity = analysis.get("activity_summary", {})

    lines = [
        "---",
        "type: weekly-enlightenment",
        f"period: {period_str}",
        f"mood_avg: {mood_avg}",
        "generated: true",
        "---",
        "",
        f"# 🧘 周度开悟报告 · {period_str}",
        "",
    ]

    # ─── 本周总结 ───
    if weekly_summary:
        lines.extend([
            "## 📖 本周回顾",
            "",
            weekly_summary,
            "",
        ])

    # ─── 情绪曲线 ───
    lines.extend([
        "## 🌡️ 情绪曲线",
        "",
        "| 日期 | 评分 | 关键词 |",
        "|------|:----:|--------|",
    ])

    for item in mood_trend:
        d = item.get("date", "")
        score = item.get("score")
        keyword = item.get("keyword", "")
        score_str = str(score) if score is not None else "-"
        lines.append(f"| {d} | {score_str} | {keyword} |")

    # 趋势分析
    lines.append("")
    lines.append(f"📊 **平均情绪**: {mood_avg}/10")
    if trend_analysis:
        lines.append(f"📈 {trend_analysis}")
    lines.append("")

    # ─── 三维成就 ───
    lines.extend([
        "## 🏆 本周成就 · 三维视角",
        "",
    ])

    survival = achievements.get("survival", [])
    growth = achievements.get("growth", [])
    enjoyment = achievements.get("enjoyment", [])

    if survival:
        lines.append("### 🛡️ 生存（基本功）")
        for item in survival:
            lines.append(f"- ✅ {item}")
        lines.append("")

    if growth:
        lines.append("### 🌱 成长（突破区）")
        for item in growth:
            lines.append(f"- 🚀 {item}")
        lines.append("")

    if enjoyment:
        lines.append("### 🎉 享受（滋养区）")
        for item in enjoyment:
            lines.append(f"- 🌈 {item}")
        lines.append("")

    # ─── 模式识别 ───
    recurring = pattern.get("recurring_theme", "")
    cognitive_patterns = pattern.get("cognitive_patterns", [])
    deep_insight = pattern.get("deep_insight", "")

    lines.extend([
        "## 🔍 模式识别",
        "",
    ])

    if recurring:
        lines.append(f"**🔄 本周反复主题**: {recurring}")
        lines.append("")

    if cognitive_patterns:
        lines.append("**🧩 认知模式**:")
        for p in cognitive_patterns:
            lines.append(f"- {p}")
        lines.append("")

    if deep_insight:
        lines.append(f"> 💭 {deep_insight}")
        lines.append("")

    # ─── 破局点 ───
    if breakthrough:
        point = breakthrough.get("point", "")
        why = breakthrough.get("why", "")
        how = breakthrough.get("how", "")

        lines.extend([
            "## 🎯 下周破局点",
            "",
            f"**💡 {point}**",
            "",
        ])
        if why:
            lines.append(f"为什么：{why}")
        if how:
            lines.append(f"怎么做：{how}")
        lines.append("")

    # ─── 开悟金句 ───
    if golden_quote:
        lines.extend([
            "## ✨ 本周开悟金句",
            "",
            f"> 🌟 **{golden_quote}**",
            "",
        ])

    # ─── 数据统计 ───
    lines.extend([
        "## 📊 数据统计",
        "",
        f"- 📝 本周笔记: {activity.get('this_week_notes', 0)} 条",
        f"- ✅ 完成事项: {activity.get('this_week_done', 0)} 件",
    ])
    if activity.get("last_week_notes") is not None:
        lines.append(f"- 📈 上周笔记: {activity['last_week_notes']} 条")
    if activity.get("last_week_done") is not None:
        lines.append(f"- 📈 上周完成: {activity['last_week_done']} 件")
    lines.append("")

    # 尾部
    now_str = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")
    lines.extend([
        "---",
        "",
        f"*🧘 周度开悟报告 · 自动生成于 {now_str}*",
    ])

    return "\n".join(lines)


# ============================================================
# 企微发送版（精简，带 emoji 和视觉呼吸感）
# ============================================================

def _build_enlightenment_for_send(period_str, analysis):
    """构建发送到企微的周度开悟报告"""
    weekly_summary = analysis.get("weekly_summary", "")
    mood_avg = analysis.get("mood_avg", "?")
    trend_analysis = analysis.get("trend_analysis", "")
    achievements = analysis.get("achievements", {})
    pattern = analysis.get("pattern_recognition", {})
    breakthrough = analysis.get("breakthrough", {})
    golden_quote = analysis.get("golden_quote", "")
    activity = analysis.get("activity_summary", {})

    lines = [f"🧘 周度开悟报告", f"📅 {period_str}", ""]

    # 本周总结
    if weekly_summary:
        lines.append(weekly_summary)
        lines.append("")

    lines.append("─" * 20)
    lines.append("")

    # 情绪趋势
    lines.append(f"🌡️ 本周情绪均分: {mood_avg}/10")
    if trend_analysis:
        lines.append(f"📈 {trend_analysis}")
    lines.append("")

    lines.append("─" * 20)
    lines.append("")

    # 三维成就
    lines.append("🏆 本周成就")
    lines.append("")

    survival = achievements.get("survival", [])
    growth = achievements.get("growth", [])
    enjoyment = achievements.get("enjoyment", [])

    if survival:
        lines.append("🛡️ 生存（基本功）")
        for item in survival:
            lines.append(f"  · {item}")
    if growth:
        lines.append("🌱 成长（突破区）")
        for item in growth:
            lines.append(f"  · {item}")
    if enjoyment:
        lines.append("🎉 享受（滋养区）")
        for item in enjoyment:
            lines.append(f"  · {item}")
    lines.append("")

    lines.append("─" * 20)
    lines.append("")

    # 模式识别
    recurring = pattern.get("recurring_theme", "")
    deep_insight = pattern.get("deep_insight", "")

    lines.append("🔍 模式识别")
    lines.append("")
    if recurring:
        lines.append(f"🔄 {recurring}")
        lines.append("")
    if deep_insight:
        lines.append(f"💭 {deep_insight}")
        lines.append("")

    lines.append("─" * 20)
    lines.append("")

    # 破局点
    if breakthrough:
        point = breakthrough.get("point", "")
        how = breakthrough.get("how", "")
        lines.append("🎯 下周破局点")
        lines.append("")
        if point:
            lines.append(f"💡 {point}")
        if how:
            lines.append(f"👉 {how}")
        lines.append("")

    lines.append("─" * 20)
    lines.append("")

    # 开悟金句
    if golden_quote:
        lines.append(f"✨ 本周开悟金句")
        lines.append("")
        lines.append(f"🌟 {golden_quote}")
        lines.append("")

    # 数据
    notes_count = activity.get("this_week_notes", 0)
    done_count = activity.get("this_week_done", 0)
    lines.append(f"📊 本周数据: 📝 {notes_count} 条笔记 | ✅ {done_count} 件完成")

    return "\n".join(lines).strip()


# ============================================================
# 文件写入
# ============================================================

def _write_weekly_review(ctx, file_path, content):
    """写入周报文件（覆盖式，每周只生成一份）"""
    return ctx.IO.write_text(file_path, content)


# Skill 热加载注册表
SKILL_REGISTRY = {
    "weekly.review": execute,
}
