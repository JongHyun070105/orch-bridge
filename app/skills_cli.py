#!/usr/bin/env python3
from __future__ import annotations
import argparse,json

def main():
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest='cmd'); sub.add_parser('available'); s=sub.add_parser('select'); s.add_argument('--text',default=''); s.add_argument('--limit',type=int,default=2); s.add_argument('--json',action='store_true'); ns=ap.parse_args()
    if ns.cmd=='available': print('Built-in workflow hints: code-review, verify, research'); return 0
    if ns.cmd=='select':
        # V1 keeps skill selection deterministic and local. Rich plugin discovery is a roadmap item.
        out=[]
        print(json.dumps(out,ensure_ascii=False) if ns.json else '')
        return 0
    return 0
if __name__=='__main__': raise SystemExit(main())
