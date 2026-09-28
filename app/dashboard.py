#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, os, subprocess, time
from pathlib import Path

HOME=Path.home(); BASE=HOME/'.local/share/orchbridge'; CACHE=HOME/'.cache/orchbridge'

def load(p):
    try:
        x=json.loads(Path(p).read_text()); return x if isinstance(x,dict) else {}
    except Exception:return {}

def git(repo,*args):
    try:
        p=subprocess.run(['git','-C',str(repo),*args],capture_output=True,text=True,timeout=3)
        return p.stdout.strip() if p.returncode==0 else '?'
    except Exception:return '?'

def render(repo:Path, project_base:Path|None=None)->str:
    project_base=project_base or Path(os.environ.get('AI_ORCH_PROJECT_BASE',str(BASE)))
    jobs=project_base/'jobs'
    rows=[]
    if jobs.exists():
        for d in sorted(jobs.glob('job-*'),key=lambda x:x.stat().st_mtime,reverse=True)[:1]:
            j=load(d/'job.json'); rows.append(j)
    j=rows[0] if rows else {}
    lines=[
      'ORCHBRIDGE · OPS', '='*78,
      f'repo     {repo}',
      f'git      {git(repo,"branch","--show-current")} @ {git(repo,"rev-parse","--short","HEAD")}',
      f'job      {j.get("id","-")} · {j.get("status","IDLE")}',
      '', 'PROVIDERS'
    ]
    for name,exe in [('codex','codex'),('claude','claude'),('agy','agy'),('commandcode','cmd')]:
        from shutil import which
        lines.append(f'  {name:<12} {which(exe) or "not installed"}')
    return '\n'.join(lines)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--repo',default=os.getcwd()); ap.add_argument('--project-base'); ap.add_argument('--once',action='store_true'); ns=ap.parse_args()
    repo=Path(ns.repo).expanduser().resolve(); pb=Path(ns.project_base).expanduser().resolve() if ns.project_base else None
    if ns.once: print(render(repo,pb)); return 0
    try:
        while True:
            print('\033[2J\033[H'+render(repo,pb),flush=True); time.sleep(2)
    except KeyboardInterrupt:return 0
if __name__=='__main__': raise SystemExit(main())
