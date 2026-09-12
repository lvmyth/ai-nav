#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 导航站 · TOOLS 自动去重脚本
==============================
扫描 index.html 的 TOOLS 数组，自动合并【高置信度重复】，仅报告【中置信度疑似重复】。

高置信度（直接删除，保留先出现者）：
  A1  id 完全重复
  A2  id 归一化后相同（仅大小写 / 连字符差异）
  A3  非术语条目（cat != 'glossary' 且 id 不以 gl- 开头）名称(name)与厂商(vendor)完全相同

安全校验：删除后条目数与 id 唯一性必须成立，异常则放弃写回（不破坏文件）。

中置信度（仅报告，不删除）：
  B1  工具/模型 url 相同（多为同品牌不同产品线，如腾讯混元系列共用官网，通常无需合并）
  B2  术语 name 与 vendor 交叉相同（中英名互换的真重复，建议人工合并）
  B3  术语名称(name)完全相同（不同 id）

特性：
  - 幂等可重复运行：已去重的文件再次运行不会改动
  - 退出码恒为 0，不阻塞后续推送流程
  - 纯本地文件操作，不触碰 git / GitHub

用法：
  python3 dedup_tools.py            # 扫描并报告（dry-run，默认不改文件）
  python3 dedup_tools.py --apply    # 执行合并写回
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(ROOT, "index.html")


def parse_entries(html):
    """返回 TOOLS 数组内的全部条目：[{start,end,block,id,name,vendor,url,cat,glossary}]。"""
    s = html.index("const TOOLS")
    try:
        e = html.index("const RECENT_CAP", s)
    except ValueError:
        e = len(html)
    scope = html[s:e]
    entries = []
    i = 0
    while True:
        m = re.search(r"\{ id:'", scope[i:])
        if not m:
            break
        start = i + m.start()
        # 按花括号深度找到匹配的右括号（条目内无嵌套 {}）
        depth = 0
        j = start
        end = None
        while j < len(scope):
            c = scope[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
            j += 1
        if end is None:
            break
        block = scope[start:end + 1]
        fid = re.search(r"id:'([^']*)'", block)
        name = re.search(r"name:'([^']*)'", block)
        vendor = re.search(r"vendor:'([^']*)'", block)
        url = re.search(r"url:'([^']*)'", block)
        cat = re.search(r"cat:'([^']*)'", block)
        fid = fid.group(1) if fid else ""
        name = name.group(1) if name else ""
        vendor = vendor.group(1) if vendor else ""
        url = url.group(1) if url else ""
        cat = cat.group(1) if cat else ""
        gl = (cat == "glossary") or fid.startswith("gl-")
        entries.append({
            "start": start, "end": end, "block": block,
            "id": fid, "name": name, "vendor": vendor,
            "url": url, "cat": cat, "glossary": gl,
        })
        i = end + 1
    return scope, entries


def norm_id(s):
    return s.lower().replace("-", "").replace("_", "")


def analyze(entries):
    """返回 (keep_flags, high_dups, reports)。"""
    n = len(entries)
    keep = [True] * n
    high = []  # 高置信度被删条目: (idx, reason, kept_idx)

    # A1 + A2：按归一化 id
    seen_norm = {}
    for idx, en in enumerate(entries):
        key = norm_id(en["id"])
        if key in seen_norm:
            keep[idx] = False
            high.append((idx, "A1/A2 id重复(归一化)", seen_norm[key]))
        else:
            seen_norm[key] = idx

    # A3：非术语 名称+厂商 完全相同
    seen_nv = {}
    for idx, en in enumerate(entries):
        if not keep[idx]:
            continue
        if en["glossary"]:
            continue
        if not en["name"] or not en["vendor"]:
            continue
        key = (en["name"].strip().lower(), en["vendor"].strip().lower())
        if key in seen_nv:
            keep[idx] = False
            high.append((idx, "A3 名称+厂商重复", seen_nv[key]))
        else:
            seen_nv[key] = idx

    # 中置信度报告
    b1, b2, b3 = [], [], []

    # B1：工具/模型 url 相同
    url_groups = {}
    for idx, en in enumerate(entries):
        if en["glossary"]:
            continue
        if not en["url"]:
            continue
        url_groups.setdefault(en["url"], []).append(idx)
    for u, grp in url_groups.items():
        if len(grp) > 1:
            b1.append((u, grp))

    # B2：术语 name<->vendor 交叉相同
    gl_idx = [i for i, en in enumerate(entries) if en["glossary"]]
    for a in range(len(gl_idx)):
        for b in range(a + 1, len(gl_idx)):
            ia, ib = gl_idx[a], gl_idx[b]
            ea, eb = entries[ia], entries[ib]
            if not ea["name"] or not eb["name"]:
                continue
            if not ea["vendor"] or not eb["vendor"]:
                continue
            if ea["name"] == eb["vendor"] and ea["vendor"] == eb["name"]:
                b2.append((ia, ib))

    # B3：术语 name 完全相同
    name_groups = {}
    for i in gl_idx:
        en = entries[i]
        if not en["name"]:
            continue
        name_groups.setdefault(en["name"], []).append(i)
    for nm, grp in name_groups.items():
        if len(grp) > 1:
            b3.append((nm, grp))

    return keep, high, (b1, b2, b3)


def main():
    apply = "--apply" in sys.argv
    html = open(HTML, encoding="utf-8").read()
    scope, entries = parse_entries(html)
    n0 = len(entries)
    keep, high, (b1, b2, b3) = analyze(entries)

    print(f"[扫描] TOOLS 共 {n0} 条（术语 {sum(1 for e in entries if e['glossary'])} / 工具 {sum(1 for e in entries if not e['glossary'])}）")

    # 高置信度
    merged = [i for i, k in enumerate(keep) if not k]
    if high:
        print(f"\n[高置信度·将合并 {len(merged)} 条] 每组保留先出现者：")
        for idx, reason, kept in high:
            en = entries[idx]
            ke = entries[kept]
            print(f"  ❌ 删除 {en['id']:28} 原因={reason}  保留={ke['id']}")
    else:
        print("[高置信度] 未发现可合并的重复 ✅")

    # 中置信度
    if b1:
        print(f"\n[B1 中置信·工具/模型 url 相同 {len(b1)} 组]（仅报告，不合并）：")
        for u, grp in b1:
            ids = " / ".join(entries[i]["id"] for i in grp)
            print(f"  · {ids}  <= {u}")
    if b2:
        print(f"\n[B2 中置信·术语 name/vendor 交叉相同 {len(b2)} 组]（建议人工合并）：")
        for ia, ib in b2:
            print(f"  · {entries[ia]['id']}「{entries[ia]['name']}」<-> {entries[ib]['id']}「{entries[ib]['name']}」")
    if b3:
        print(f"\n[B3 中置信·术语名称相同 {len(b3)} 组]（建议人工合并）：")
        for nm, grp in b3:
            ids = " / ".join(entries[i]["id"] for i in grp)
            print(f"  · 「{nm}」: {ids}")
    if not (b1 or b2 or b3):
        print("[中置信度] 未发现疑似重复 ✅")

    # 汇总
    print(f"\n[汇总] 本次自动合并 {len(merged)} 条；剩余疑似重复组 B1={len(b1)} B2={len(b2)} B3={len(b3)}")

    if not apply:
        print("[dry-run] 未改动文件，退出。")
        return 0

    if not merged:
        print("[apply] 无需合并，文件未改动 ✅")
        return 0

    # 重建 TOOLS 数组（仅保留 keep 的条目原样）
    kept_blocks = [entries[i]["block"] for i in range(n0) if keep[i]]
    # 安全校验：条目数 + id 唯一性
    if len(kept_blocks) != n0 - len(merged):
        print(f"[安全校验失败] 删除后条目数异常 ({len(kept_blocks)} != {n0 - len(merged)})，放弃写回，不破坏文件！")
        return 0
    kept_ids = [entries[i]["id"] for i in range(n0) if keep[i]]
    if len(set(kept_ids)) != len(kept_ids):
        print("[安全校验失败] 删除后存在 id 冲突，放弃写回，不破坏文件！")
        return 0

    # 定位并替换整个 TOOLS 数组
    s_idx = html.index("const TOOLS")
    open_b = html.index("[", s_idx)
    e_idx = html.index("const RECENT_CAP", s_idx)
    close = html.rindex("];", open_b, e_idx)
    new_html = html[:open_b + 1] + "\n" + "\n".join(kept_blocks) + "\n" + html[close:]
    open(HTML, "w", encoding="utf-8").write(new_html)
    print(f"[apply] 已合并写回，删除 {len(merged)} 条，剩余 {len(kept_blocks)} 条 ✅")
    return 0


if __name__ == "__main__":
    # 退出码恒为 0，不阻塞推送
    try:
        sys.exit(main())
    except Exception as ex:
        print(f"[异常] dedup_tools 运行出错（不影响后续）: {ex}")
        sys.exit(0)
