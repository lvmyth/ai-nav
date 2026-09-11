#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 导航站 · 自动去重检测脚本
============================
扫描 index.html 的 TOOLS 数组，检测工具/模型与术语的重复条目，避免每日
自动任务反复累加重复内容。

检测分级（核心原则：只坚决处理「板上钉钉」的重复，其余仅报告，不擅自删）：

  [A 自动合并] 高置信度，脚本可直接删除多余条目
    - id 完全重复（同一 id 出现多次）
    - id 归一化后相同（仅大小写/连字符/下划线差异，如 gl-a-b 与 gl_a_b）
    - 非术语条目「名称完全相同」且「vendor 相同」（同一产品的重复收录）

  [B 仅报告] 中置信度，需人工判断，默认不删
    - 工具/模型 url 相同（多为同品牌不同产品线，如腾讯混元系列共用官网）
    - 术语 name 与 vendor 交叉相同（中英名互换的重复，如
      gl-align-faking「对齐伪装」与 gl-alignment-faking「Alignment Faking」）
    - 术语 name 完全相同

用法：
    python3 dedup_tools.py            # 只检测并报告（默认，不改文件）
    python3 dedup_tools.py --apply    # 移除 A 类高置信度重复条目并写回
    python3 dedup_tools.py --json     # 以 JSON 输出结果（便于任务解析）

退出码：0 正常；未发现高置信度重复也返回 0（不阻塞自动任务）。
"""
import os
import re
import sys
import json
import collections

ROOT = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(ROOT, "index.html")

# 条目起始锚点：`{ id:'...'` ；结束到该条目自身最后的 icon 字段
ENTRY_START = re.compile(r"\{ id:'([^']*)'")


def read_html():
    with open(HTML, encoding="utf-8") as f:
        return f.read()


def tools_scope(html):
    """取出 const TOOLS 数组的文本范围（到下一个顶层 const 声明为止）。"""
    s = html.index("const TOOLS")
    try:
        e = html.index("const RECENT_CAP", s)
    except ValueError:
        e = len(html)
    return s, e


def parse_entries(html):
    """解析 TOOLS 数组内每个条目，返回 (条目列表, 各条目在 html 中的 [start,end) 区间)。

    条目以 `{ id:'` 开头，到该条目自身结束（以 `},` 或 `}` 结尾）。
    用「下一个 { id:' 之前」作为上界，保证不吞并后续条目。
    """
    s, e = tools_scope(html)
    scope = html[s:e]
    starts = [m.start() for m in ENTRY_START.finditer(scope)]
    entries = []
    for i, st in enumerate(starts):
        en = starts[i + 1] if i + 1 < len(starts) else len(scope)
        seg = scope[st:en]

        def g(key):
            m = re.search(key + r":'((?:[^'\\]|\\.)*)'", seg)
            return m.group(1) if m else ""

        entries.append({
            "id": ENTRY_START.match(seg).group(1),
            "name": g("name"),
            "vendor": g("vendor"),
            "cat": g("cat"),
            "url": g("url"),
            "region": g("region"),
            "subcat": g("subcat"),
            "icon": g("icon"),
            # 在整份 html 中的绝对区间（含条目末尾可能的逗号与换行）
            "abs_start": s + st,
            "abs_end": s + en,
        })
    return entries


def is_glossary(t):
    return t["cat"] == "glossary" or t["id"].startswith("gl-")


def norm_id(x):
    return re.sub(r"[-_\s]", "", x.strip().lower())


def label(t):
    kind = "术语" if is_glossary(t) else "工具"
    return f"[{kind}] {t['id']:26} {t['name']}"


def detect(entries):
    """返回 (auto_items, report)。

    auto_items 为「需删除的具体条目」列表（保留每组第一条，其余标记删除）。
    注意：以条目为单位而非 id，避免同一 id 重复出现多条时只删一条。
    report 为需人工确认的分组（不自动处理）。
    """
    auto_ids = []       # 需删除条目的唯一标识（id + 序号，用于定位具体条目）
    auto_set = set()    # 参与判定的 id（已被标记删除的跳过后续判定）
    auto_reasons = {}   # 唯一标识 -> 原因
    auto_items = []     # 实际要删除的条目对象

    def mark(dup, reason, keeper=None):
        uid = (dup["id"], dup["abs_start"])
        if uid in auto_set:
            return
        auto_set.add(uid)
        auto_ids.append(uid)
        auto_reasons[dup["id"]] = reason
        auto_items.append(dup)

    # ---- A1: id 完全重复（同一 id 多条目，保留首个）----
    by_id = collections.defaultdict(list)
    for t in entries:
        by_id[t["id"]].append(t)
    for k, lst in by_id.items():
        if len(lst) > 1:
            first = lst[0]
            for dup in lst[1:]:
                mark(dup, f"id 完全重复（与第 1 条「{first['name']}」相同）")

    # ---- A2: 归一化 id 相同（大小写/连字符差异）----
    by_norm = collections.defaultdict(list)
    for t in entries:
        uid = (t["id"], t["abs_start"])
        if uid in auto_set:
            continue
        by_norm[norm_id(t["id"])].append(t)
    for k, lst in by_norm.items():
        if len(lst) > 1:
            first = lst[0]
            for dup in lst[1:]:
                mark(dup, f"id 归一化后与「{first['id']}」相同（仅大小写/连字符差异）")

    # ---- A3: 非术语条目 名称+vendor 完全相同（保留首个）----
    by_nv = collections.defaultdict(list)
    for t in entries:
        uid = (t["id"], t["abs_start"])
        if uid in auto_set or is_glossary(t):
            continue
        if t["name"].strip():
            by_nv[(t["name"].strip().lower(), t["vendor"].strip().lower())].append(t)
    for k, lst in by_nv.items():
        if len(lst) > 1:
            first = lst[0]
            for dup in lst[1:]:
                mark(dup, f"名称与厂商均与「{first['id']}」完全相同")

    # ---- B1: 工具/模型 url 相同（仅报告）----
    by_url = collections.defaultdict(list)
    for t in entries:
        if is_glossary(t) or not t["url"]:
            continue
        by_url[t["url"]].append(t)
    url_groups = [(u, lst) for u, lst in by_url.items() if len(lst) > 1]

    # ---- B2: 术语 name/vendor 交叉相同（仅报告）----
    gloss = [t for t in entries if is_glossary(t)]
    seen = collections.defaultdict(list)
    for t in gloss:
        for key in (t["name"].strip().lower(), t["vendor"].strip().lower()):
            if key:
                seen[key].append(t)
    cross_groups = []
    for k, lst in sorted(seen.items()):
        uniq = {id(x) for x in lst}
        if len(uniq) > 1:
            cross_groups.append((k, lst))

    # ---- B3: 术语 name 完全相同（仅报告）----
    by_gn = collections.defaultdict(list)
    for t in gloss:
        if t["name"].strip():
            by_gn[t["name"].strip().lower()].append(t)
    gloss_name_groups = [(k, lst) for k, lst in by_gn.items() if len(lst) > 1]

    report = {
        "url_groups": url_groups,
        "cross_groups": cross_groups,
        "gloss_name_groups": gloss_name_groups,
        "auto_reasons": auto_reasons,
    }
    return auto_items, report


def remove_entries(html, items):
    """删除指定条目对象（items 来自 detect 的 auto 列表，含 abs_start/abs_end）。

    注意：parse_entries 给出的区间上界是「下一个条目起始」，
    因此区间尾部会带上后续条目的缩进前缀。这里显式只删到本条目的
    `},` 结束位置，保留后续条目的缩进，避免破坏下一行格式。
    从后往前删，避免坐标位移。
    """
    ordered = sorted(items, key=lambda t: t["abs_start"], reverse=True)
    removed = 0
    for t in ordered:
        st = t["abs_start"]
        raw = html[st:t["abs_end"]]
        m = re.search(r"\}[ \t]*,[ \t]*\n?", raw)
        if not m:
            m = re.search(r"\}[ \t]*\n?", raw)
        if not m:
            continue
        end = st + m.end()
        lead = st
        while lead > 0 and html[lead - 1] in " \t":
            lead -= 1
        if lead > 0 and html[lead - 1] == "\n":
            lead -= 1
        html = html[:lead] + html[end:]
        removed += 1
    return html, removed


def main():
    apply_mode = "--apply" in sys.argv
    as_json = "--json" in sys.argv

    html = read_html()
    entries = parse_entries(html)
    auto, report = detect(entries)
    n_tools = len([t for t in entries if not is_glossary(t)])
    n_gloss = len([t for t in entries if is_glossary(t)])

    if as_json:
        print(json.dumps({
            "total": len(entries), "tools": n_tools, "glossary": n_gloss,
            "auto_remove_count": len(auto),
            "auto_removed": [t["id"] for t in auto],
            "auto_reasons": report["auto_reasons"],
            "url_dup_groups": [[u, [x["id"] for x in l]] for u, l in report["url_groups"]],
            "glossary_cross_dup": [[k, [x["id"] for x in l]] for k, l in report["cross_groups"]],
            "glossary_name_dup": [[k, [x["id"] for x in l]] for k, l in report["gloss_name_groups"]],
        }, ensure_ascii=False, indent=2))
        return 0

    print(f"[扫描] TOOLS 共 {len(entries)} 条（工具/模型 {n_tools}，术语 {n_gloss}）")

    print(f"\n===== A. 高置信度重复（可自动合并）：{len(auto)} 条 =====")
    if auto:
        for t in auto:
            print(f"  ✂️  {t['id']:26} {report['auto_reasons'].get(t['id'], '')}")
    else:
        print("  无 ✅")

    print(f"\n===== B. 需人工确认的疑似重复（未自动处理）=====")
    print(f"  B1. 工具/模型 url 相同：{len(report['url_groups'])} 组（多为同品牌不同产品线，通常无需合并）")
    for u, lst in report["url_groups"]:
        print(f"      {u}")
        for x in lst:
            print(f"        · {label(x)}")
    print(f"\n  B2. 术语 名称/中文名 交叉相同：{len(report['cross_groups'])} 组（多为中英名互换的真重复，建议合并）")
    for k, lst in report["cross_groups"]:
        print(f"      关键词「{k}」")
        for x in lst:
            print(f"        · {label(x)}")
    print(f"\n  B3. 术语 名称完全相同：{len(report['gloss_name_groups'])} 组")
    for k, lst in report["gloss_name_groups"]:
        for x in lst:
            print(f"        · {label(x)}")

    if not auto:
        print("\n[结果] 未发现高置信度重复，无需自动处理 ✅")
        return 0

    if not apply_mode:
        print(f"\n[dry-run] 发现 {len(auto)} 条高置信度重复，未改动文件。"
              f"加 --apply 执行自动合并。")
        return 0

    html2, removed = remove_entries(html, auto)
    # 安全校验：删除数量与解析结果都必须自洽
    new_entries = parse_entries(html2)
    if removed != len(auto) or len(new_entries) != len(entries) - len(auto):
        print(f"[错误] 删除异常（实际删 {removed}/{len(auto)}，"
              f"条目 {len(entries)} -> {len(new_entries)}），已放弃写回，请人工检查。")
        return 1
    # 校验删除后无 id 重复
    rest = collections.Counter(t["id"] for t in new_entries)
    left = [(k, v) for k, v in rest.items() if v > 1]
    if left:
        print(f"[错误] 删除后仍有 id 重复 {left}，已放弃写回。")
        return 1
    with open(HTML, "w", encoding="utf-8") as f:
        f.write(html2)
    print(f"\n[结果] 已自动合并 {len(auto)} 条重复，条目 {len(entries)} -> {len(new_entries)} ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
