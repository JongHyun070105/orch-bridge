#!/usr/bin/env python3
from __future__ import annotations
import argparse,json,shutil,subprocess
PROVIDERS={'codex':'codex','claude':'claude','agy':'agy','commandcode':'cmd'}
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('provider',nargs='?'); ap.add_argument('--json',action='store_true'); ns=ap.parse_args()
    rows=[{'provider':k,'binary':v,'path':shutil.which(v)} for k,v in PROVIDERS.items() if not ns.provider or k==ns.provider]
    print(json.dumps(rows,ensure_ascii=False,indent=2) if ns.json else '\n'.join(f"{r['provider']:<12} {r['path'] or 'not installed'}" for r in rows)); return 0
if __name__=='__main__': raise SystemExit(main())
