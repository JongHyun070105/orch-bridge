#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, os, pty, re, select, signal, struct, subprocess, termios, fcntl, time
from pathlib import Path
from typing import Any
HOME=Path.home(); CACHE=HOME/".cache/orchbridge"; CACHE.mkdir(parents=True,exist_ok=True)
ANSI_RE=re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
CTRL_RE=re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
def clean(s:str)->str:
    s=ANSI_RE.sub("",s).replace("\r","\n"); s=CTRL_RE.sub("",s); return re.sub(r"\n{3,}","\n\n",s)
def cache_path(provider:str)->Path: return CACHE/("cmd-usage.json" if provider=="cmd" else "codex-usage.json")
def load(p:Path)->dict[str,Any]:
    try:
        x=json.loads(p.read_text()); return x if isinstance(x,dict) else {}
    except Exception:return {}
def save(provider:str,data:dict[str,Any])->Path:
    p=cache_path(provider); x=dict(data); x["captured_at"]=time.time(); x["_captured_at_unix"]=x["captured_at"]
    t=p.with_suffix('.tmp'); t.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n'); os.replace(t,p); return p
def kill(pid:int)->None:
    for sig in (signal.SIGTERM,signal.SIGKILL):
        try: os.kill(pid,sig)
        except Exception:return
        time.sleep(.15)
def pty_run(argv:list[str],send:bytes,timeout:float,settle:float)->str:
    pid,fd=pty.fork()
    if pid==0: os.execvp(argv[0],argv)
    try:
        try: fcntl.ioctl(fd,termios.TIOCSWINSZ,struct.pack('HHHH',50,180,0,0))
        except Exception: pass
        chunks=[]; start=time.monotonic(); sent=False; last=start
        while time.monotonic()-start<timeout:
            now=time.monotonic()
            if not sent and now-start>=settle:
                try: os.write(fd,send); sent=True
                except OSError: break
            r,_,_=select.select([fd],[],[],0.2)
            if not r:
                if sent and now-last>3.0: break
                continue
            try:data=os.read(fd,65536)
            except OSError:break
            if not data:break
            chunks.append(data); last=time.monotonic()
        try: os.write(fd,b"\x1b\x03")
        except Exception:pass
        return clean(b''.join(chunks).decode(errors='replace'))
    finally:
        kill(pid)
        try:os.close(fd)
        except Exception:pass
def nums_near(text:str,label:str)->list[float]:
    pats=[rf"(?is){label}.{{0,220}}?(\d{{1,3}}(?:\.\d+)?)\s*%",rf"(?is)(\d{{1,3}}(?:\.\d+)?)\s*%.{{0,140}}?{label}"]
    vals=[]
    for pat in pats:
        for m in re.finditer(pat,text):
            try: vals.append(float(m.group(1)))
            except Exception: pass
    return vals
def to_left(text_segment:str,val:float)->float:
    lo=text_segment.lower()
    if 'used' in lo and 'left' not in lo and 'remaining' not in lo: return max(0,100-val)
    return max(0,min(100,val))
def parse_window(text:str,label:str)->float|None:
    pats=[rf"(?is)({label}).{{0,180}}?(\d{{1,3}}(?:\.\d+)?)\s*%\s*(left|remaining|used)?",rf"(?is)(\d{{1,3}}(?:\.\d+)?)\s*%\s*(left|remaining|used)?.{{0,100}}?({label})"]
    for pat in pats:
        m=re.search(pat,text)
        if not m: continue
        groups=m.groups(); num=next((g for g in groups if g and re.fullmatch(r"\d{1,3}(?:\.\d+)?",g)),None); marker=next((g for g in groups if g and g.lower() in {'left','remaining','used'}),None)
        if num is None: continue
        v=float(num); return max(0,100-v) if marker=='used' else max(0,min(100,v))
    return None
def parse_cmd(text:str)->dict[str,Any]:
    d={"raw_text_tail":text[-8000:]}
    # monthly usage block
    m=re.search(r"(?is)(?:USAGE|PLAN).{0,300}?(\d{1,3}(?:\.\d+)?)\s*%\s*used",text)
    if m: d['monthly_used_pct']=float(m.group(1)); d['monthly_remaining_pct']=max(0,100-float(m.group(1)))
    m=re.search(r"(?i)([\d,]+)\s+requests?\s+this\s+month",text)
    if m:d['monthly_requests']=int(m.group(1).replace(',',''))
    m=re.search(r"(?i)(\d+)\s+days?\s+to\s+renew",text)
    if m:d['monthly_renewal_days']=int(m.group(1))
    for key,label in [('five_hour_remaining_pct',r'5\s*[- ]?\s*hour|5h'),('weekly_remaining_pct',r'weekly|7\s*[- ]?\s*day')]:
        v=parse_window(text,label)
        if v is not None:d[key]=v
    if not any(k.endswith('_remaining_pct') for k in d): raise ValueError('Command Code /usage meters not recognized')
    return d
def parse_codex(text:str)->dict[str,Any]:
    d={"raw_text_tail":text[-8000:]}
    for key,label in [('five_hour_remaining_pct',r'5\s*[- ]?\s*hour|5h'),('weekly_remaining_pct',r'weekly|7\s*[- ]?\s*day|week')]:
        v=parse_window(text,label)
        if v is not None:d[key]=v
    if not any(k.endswith('_remaining_pct') for k in d): raise ValueError('Codex /status quota meters not recognized')
    return d
def probe(provider:str)->dict[str,Any]:
    p=cache_path(provider); old=load(p)
    try:
        if provider=='cmd': text=pty_run(['cmd','--skip-onboarding'],b'/usage\r',14,1.7); data=parse_cmd(text)
        else: text=pty_run(['codex'],b'/status\r',14,2.0); data=parse_codex(text)
        data['_probe']='ephemeral-pty'; save(provider,data); return {'provider':provider,'ok':True,'fresh':True,**data}
    except Exception as e:
        if old:
            return {'provider':provider,'ok':True,'fresh':False,'stale':True,'reason':str(e),**old}
        return {'provider':provider,'ok':False,'fresh':False,'reason':str(e),'captured_at':time.time()}
def main()->int:
    ap=argparse.ArgumentParser(); ap.add_argument('provider',choices=['cmd','codex']); ap.add_argument('--json',action='store_true'); ns=ap.parse_args(); data=probe(ns.provider)
    print(json.dumps(data,ensure_ascii=False) if ns.json else json.dumps(data,ensure_ascii=False,indent=2)); return 0 if data.get('ok') else 2
if __name__=='__main__': raise SystemExit(main())
