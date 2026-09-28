#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, os, subprocess, time
from pathlib import Path
from typing import Any

def run(repo: Path, *args: str, check: bool=False) -> subprocess.CompletedProcess[str]:
    p=subprocess.run(["git","-C",str(repo),*args],capture_output=True,text=True,timeout=30)
    if check and p.returncode!=0:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip() or f"git {' '.join(args)} exit={p.returncode}")
    return p

def root(path: str|None) -> Path:
    p=Path(path or os.environ.get("AI_ORCH_REPO") or os.getcwd()).expanduser().resolve()
    q=run(p,"rev-parse","--show-toplevel")
    if q.returncode!=0: raise SystemExit("not a Git repository")
    return Path(q.stdout.strip()).resolve()

def branch(repo:Path)->str:
    p=run(repo,"branch","--show-current"); return p.stdout.strip() if p.returncode==0 else ""
def head(repo:Path)->str:
    p=run(repo,"rev-parse","HEAD"); return p.stdout.strip() if p.returncode==0 else ""
def dirty(repo:Path)->list[str]:
    p=run(repo,"status","--porcelain=v1","--untracked-files=all",check=True); return [x for x in p.stdout.splitlines() if x.strip()]
def oid(repo:Path,ref:str)->str:
    p=run(repo,"rev-parse","--verify",f"{ref}^{{commit}}"); return p.stdout.strip() if p.returncode==0 else ""
def valid(repo:Path,name:str)->bool: return run(repo,"check-ref-format","--branch",name).returncode==0
def local(repo:Path,name:str)->bool: return run(repo,"show-ref","--verify","--quiet",f"refs/heads/{name}").returncode==0
def remote(repo:Path,name:str)->bool: return run(repo,"show-ref","--verify","--quiet",f"refs/remotes/origin/{name}").returncode==0

def project_base()->Path:
    return Path(os.environ.get("AI_ORCH_PROJECT_BASE",str(Path.home()/".local/share/orchbridge"))).expanduser().resolve()
def audit(repo:Path,action:str,**extra:Any)->None:
    p=project_base()/"branch-events.jsonl"; p.parent.mkdir(parents=True,exist_ok=True)
    row={"ts":time.time(),"action":action,"repo":str(repo),"job_id":os.environ.get("AI_ORCH_JOB_ID"),**extra}
    with p.open("a") as f: f.write(json.dumps(row,ensure_ascii=False)+"\n")
def ensure_main()->None:
    if str(os.environ.get("AI_ORCH_DELEGATION_DEPTH","0")) not in {"","0"}:
        raise SystemExit("delegated workers may not mutate the authoritative branch; MAIN only")

def status(repo:Path)->dict[str,Any]:
    return {"repo":str(repo),"branch":branch(repo),"head":head(repo),"dirty_count":len(dirty(repo)),"job_id":os.environ.get("AI_ORCH_JOB_ID")}

def switch(repo:Path,name:str)->dict[str,Any]:
    ensure_main()
    if not valid(repo,name): raise RuntimeError(f"invalid branch name: {name}")
    before=status(repo)
    if before["branch"]==name: return before
    changes=dirty(repo)
    if changes: raise RuntimeError(f"working tree is not clean ({len(changes)} changes); commit/stash or create a new branch from current HEAD instead")
    if local(repo,name): args=("switch",name)
    elif remote(repo,name): args=("switch","--track","-c",name,f"origin/{name}")
    else: raise RuntimeError(f"branch not found locally or on origin: {name}")
    run(repo,*args,check=True)
    after=status(repo); audit(repo,"switch",before=before,after=after)
    return after

def new(repo:Path,name:str,from_ref:str)->dict[str,Any]:
    ensure_main()
    if not valid(repo,name): raise RuntimeError(f"invalid branch name: {name}")
    if local(repo,name): raise RuntimeError(f"local branch already exists: {name}")
    base=oid(repo,from_ref); cur=head(repo)
    if not base: raise RuntimeError(f"base ref not found: {from_ref}")
    changes=dirty(repo)
    if changes and base!=cur: raise RuntimeError(f"working tree is dirty ({len(changes)} changes); creating from a different ref would rewrite files")
    before=status(repo); run(repo,"switch","-c",name,from_ref,check=True); after=status(repo); audit(repo,"new",from_ref=from_ref,before=before,after=after)
    return after

def back(repo:Path)->dict[str,Any]:
    ensure_main(); jid=os.environ.get("AI_ORCH_JOB_ID","").strip(); pb=project_base()
    if not jid: raise RuntimeError("AI_ORCH_JOB_ID is unavailable")
    jp=pb/"jobs"/jid/"job.json"
    if not jp.exists(): raise RuntimeError(f"job metadata not found: {jp}")
    data=json.loads(jp.read_text()); target=str(data.get("branch_at_start") or "").strip()
    if not target: raise RuntimeError("job has no branch_at_start")
    return switch(repo,target)

def main()->int:
    ap=argparse.ArgumentParser(description="Safe MAIN-only branch lifecycle helper")
    ap.add_argument("--repo")
    ap.add_argument("--json",action="store_true")
    sub=ap.add_subparsers(dest="command",required=True)
    sub.add_parser("status"); sub.add_parser("list")
    s=sub.add_parser("switch"); s.add_argument("name")
    n=sub.add_parser("new"); n.add_argument("name"); n.add_argument("--from",dest="from_ref",default="HEAD")
    sub.add_parser("back")
    ns=ap.parse_args(); repo=root(ns.repo)
    try:
        if ns.command=="status": out=status(repo)
        elif ns.command=="list":
            p=run(repo,"branch","--format=%(HEAD) %(refname:short)",check=True); out={"repo":str(repo),"branches":p.stdout.splitlines()}
        elif ns.command=="switch": out=switch(repo,ns.name)
        elif ns.command=="new": out=new(repo,ns.name,ns.from_ref)
        else: out=back(repo)
    except Exception as e:
        if ns.json: print(json.dumps({"ok":False,"error":str(e)},ensure_ascii=False))
        else: print(f"ERROR: {e}")
        return 2
    payload={"ok":True,**out}; print(json.dumps(payload,ensure_ascii=False) if ns.json else json.dumps(payload,ensure_ascii=False,indent=2)); return 0
if __name__=="__main__": raise SystemExit(main())
