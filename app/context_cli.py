#!/usr/bin/env python3
from __future__ import annotations
import argparse,json,os,subprocess
from pathlib import Path

def main():
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest='cmd'); b=sub.add_parser('build'); b.add_argument('--job'); ns=ap.parse_args()
    if ns.cmd!='build': return 0
    repo=Path(os.environ.get('AI_ORCH_REPO',os.getcwd())).resolve()
    parts=[]
    try:
        p=subprocess.run(['git','-C',str(repo),'status','--short','--branch'],capture_output=True,text=True,timeout=3)
        if p.returncode==0 and p.stdout.strip(): parts.append('GIT STATE:\n'+p.stdout.strip())
    except Exception: pass
    if ns.job: parts.append(f'JOB: {ns.job}')
    print('\n\n'.join(parts)); return 0
if __name__=='__main__': raise SystemExit(main())
