#!/usr/bin/env python3
from __future__ import annotations
import argparse,shutil,subprocess

def main():
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest='cmd'); sub.add_parser('list'); ns=ap.parse_args()
    if ns.cmd!='list': return 0
    for exe,args in [('codex',['mcp','list']),('agy',['mcp','list']),('cmd',['mcp','list'])]:
        path=shutil.which(exe)
        if not path: continue
        print(f'[{exe}]')
        try:
            p=subprocess.run([path,*args],capture_output=True,text=True,timeout=10); print((p.stdout or p.stderr).strip())
        except Exception as e: print(e)
    return 0
if __name__=='__main__': raise SystemExit(main())
