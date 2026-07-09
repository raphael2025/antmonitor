#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行扫描器（薄封装，复用 miner_core）。

  python scan_miners.py --lo 100 --hi 160          # 扫描并出排名+CSV
  python scan_miners.py --report miners_xxx.csv     # 只从 CSV 出报告
"""
import sys
import csv
import argparse
import datetime

import miner_core

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

COLS = ["ip", "status", "firmware", "model", "sn", "worker",
        "hr_rt", "hr_avg", "power", "temp", "eff", "uptime",
        "accepted", "rejected", "stale", "note"]
# 兼容旧版 CSV 列名
_ALIAS = {"hashrate_rt_th": "hr_rt", "hashrate_avg_th": "hr_avg",
          "power_w": "power", "chip_temp_max": "temp"}


def fmt_hash(th):
    """算力千进制：1000 TH=1 PH，1000 PH=1 EH。"""
    v = float(th or 0)
    if abs(v) >= 1e6:
        return f"{v/1e6:,.2f} EH"
    if abs(v) >= 1e3:
        return f"{v/1e3:,.2f} PH"
    return f"{v:,.2f} TH"


def report(records, out=None, total=None):
    total = total if total is not None else len(records)
    with_hr, no_hr, offline = miner_core.rank(records)
    online = len(with_hr) + len(no_hr)
    print("\n" + "=" * 60)
    print(f"在线 {online} 台 / 共 {total} 个地址")
    print(f"  可读算力 {len(with_hr)} 台，读不到算力 {len(no_hr)} 台")
    by_fw = {}
    for r in records:
        if r["status"] == "online":
            by_fw[r["firmware"]] = by_fw.get(r["firmware"], 0) + 1
    print(f"  固件分布：{by_fw}")
    if with_hr:
        total_hr = round(sum(r["hr_rt"] for r in with_hr), 2)
        print(f"\n  总算力 {fmt_hash(total_hr)}   平均 {fmt_hash(total_hr/len(with_hr))}/台")
        print("\n  [最快 10 台]")
        for r in with_hr[:10]:
            print(f"    {r['hr_rt']:>8} TH  {r['ip']:16s} {r['firmware']:8s} {r['model']}")
        print("\n  [最慢 10 台]")
        for r in with_hr[-10:][::-1]:
            print(f"    {r['hr_rt']:>8} TH  {r['ip']:16s} {r['firmware']:8s} {r['model']}")
    if no_hr:
        print(f"\n  [在线但读不到算力] {len(no_hr)} 台：")
        for r in no_hr[:30]:
            print(f"    {r['ip']:16s} {r['firmware']:8s} {r.get('note','')}")
    if out:
        print(f"\n  明细 CSV: {out}")


def write_csv(records, out):
    with open(out, "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.DictWriter(fp, fieldnames=COLS)
        w.writeheader()
        for r in records:
            w.writerow({k: r.get(k) for k in COLS})


def load_csv(path):
    res = []
    with open(path, newline="", encoding="utf-8-sig") as fp:
        for row in csv.DictReader(fp):
            for old, new in _ALIAS.items():       # 旧列名 -> 新列名
                if old in row and new not in row:
                    row[new] = row.pop(old)
            for k in ("hr_rt", "hr_avg"):
                row[k] = float(row[k]) if row.get(k) not in (None, "", "None") else None
            res.append(row)
    return res


def main():
    ap = argparse.ArgumentParser(description="矿机算力扫描器")
    ap.add_argument("--report", default="", help="只从已有 CSV 出报告")
    ap.add_argument("--lo", type=int, default=100)
    ap.add_argument("--hi", type=int, default=160)
    ap.add_argument("--base", default="172.16")
    ap.add_argument("--online-timeout", type=float, default=0.5)
    ap.add_argument("--data-timeout", type=float, default=2.0)
    ap.add_argument("--workers", type=int, default=300)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    if args.report:
        report(load_csv(args.report), out=args.report)
        return

    cfg = {"online_timeout": args.online_timeout, "data_timeout": args.data_timeout,
           "workers": args.workers, "passwords": [["root", "root"]]}
    ips = miner_core.gen_ips(args.lo, args.hi, args.base)
    print(f"扫描 {args.base}.{args.lo}.x - {args.base}.{args.hi}.x  共 {len(ips)} 个地址")

    def cb(done, total):
        print(f"  ...进度 {done}/{total}", end="\r")

    records = miner_core.scan(ips, cfg, progress_cb=cb)
    print()
    out = args.out or f"miners_{datetime.datetime.now():%Y%m%d_%H%M%S}.csv"
    write_csv(records, out)
    report(records, out=out, total=len(ips))


if __name__ == "__main__":
    main()
