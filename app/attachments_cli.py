#!/usr/bin/env python3
from __future__ import annotations
import argparse,json,re
from pathlib import Path

def main():
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest='cmd');
    p=sub.add_parser('pick'); p.add_argument('--json',action='store_true')
    d=sub.add_parser('detect'); d.add_argument('--text',default=''); d.add_argument('--json',action='store_true')
    l=sub.add_parser('list'); l.add_argument('--json',action='store_true')
    ns=ap.parse_args(); out=[]
    if ns.cmd=='detect':
        tokens=re.findall(r'(?<!\w)(?:~?/|\./|\.\./)[^\s]+',ns.text)
        for t in tokens:
            p=Path(t.rstrip('.,;:)')).expanduser()
            if p.exists(): out.append(str(p.resolve()))
    if getattr(ns,'json',False): print(json.dumps(out,ensure_ascii=False))
    else:
        for x in out: print(x)
    return 0
if __name__=='__main__': raise SystemExit(main())
