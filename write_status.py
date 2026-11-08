#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 导航站 · 运行状态落库脚本
============================
每日自动化任务结束时调用，从 index.html 自动统计真实数据并生成 status.json，
供页脚「站点健康」面板展示，也可被外部监控直接消费。

用法:
  python3 write_status.py --status ok     --message "每日更新成功" --added 6
  python3 write_status.py --status failed --failed-step "4. 推送部署" --message "GitHub 网络不可达"

设计原则:
  - 条目数 / 字段覆盖 / 日期一律从 index.html 解析，不依赖调用方手填，避免错数
  - 任何异常都尽力写出一个 status.json（失败也要可观测），不抛异常阻塞任务
  - 退出码恒为 0，绝不成为自动化任务的新失败源
"""
import argparse
import datetime
import json
import os
import re

BASE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(BASE, "index.html")
OUT = os.path.join(BASE, "status.json")


def parse_stats(html):
    m = re.search(r"const TOOLS\s*=\s*\[(.*?)\n\];", html, re.S)
    block = m.group(1) if m else ""
    ids = re.findall(r"\{\s*id:'([^']+)'", block)
    dm = re.search(r"NAV_UPDATE_DATE\s*=\s*'([^']*)'", html)
    return {
        "date": dm.group(1) if dm else "",
        "tools": len(ids),
        "duplicateIds": len(ids) - len(set(ids)),
        "fields": {
            "release": block.count("release:"),
            "pricing": block.count("pricing:"),
            "cn": block.count("cn:"),
            "api": block.count("api:"),
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", default="ok", choices=["ok", "failed", "partial"])
    ap.add_argument("--message", default="")
    ap.add_argument("--added", type=int, default=0)
    ap.add_argument("--failed-step", default="")
    args = ap.parse_args()

    status = {
        "lastRun": datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=8))
        ).isoformat(timespec="seconds"),
        "status": args.status,
        "message": args.message,
        "added": args.added,
        "failedStep": args.failed_step,
    }
    try:
        with open(INDEX, "r", encoding="utf-8") as f:
            status.update(parse_stats(f.read()))
    except Exception as e:
        status["statsError"] = str(e)[:200]
    try:
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
        print("[status] 已写入 status.json: %s / %s 条 / %s" % (
            status["status"], status.get("tools", "?"), status.get("date", "?")))
    except Exception as e:
        print("[status] 写入失败(不影响任务): %s" % e)
    return 0


if __name__ == "__main__":
    main()
