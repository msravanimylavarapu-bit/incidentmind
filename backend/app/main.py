import asyncio, json, logging, time
from datetime import datetime, timezone
from typing import Any
import httpx
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from .config import settings
from .db import init_db, SessionLocal, MemoryRecord, ChatMessage, Setting
from .hindsight import memory
from .seed_data import ALL, INCIDENTS

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("incidentmind")
app = FastAPI(title="IncidentMind API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

class IncidentIn(BaseModel):
    title: str = Field(min_length=2, max_length=250)
    description: str = Field(min_length=8)
    service: str = "unknown-service"
    severity: str = "SEV-2"
    environment: str = "production"

class ChatIn(BaseModel):
    incident_id: str
    message: str = Field(min_length=1)

class ResolveIn(BaseModel):
    incident_id: str
    root_cause: str
    fix_applied: str
    steps_worked: str = ""
    steps_failed: str = ""
    lessons: str = ""

class KnowledgeIn(BaseModel):
    kind: str
    title: str
    content: str
    service: str = "Platform"
    severity: str = "-"
    category: str = "General"

def row_dict(r):
    return {"id":r.id,"kind":r.kind,"title":r.title,"service":r.service,"severity":r.severity,"category":r.category,"date":r.date,"content":r.content,"root_cause":r.root_cause,"resolution":r.resolution,"duration_minutes":r.duration_minutes}

def record_memory(item, seeded=False):
    with SessionLocal() as db:
        existing=db.get(MemoryRecord, item["id"])
        if existing: return
        db.add(MemoryRecord(id=item["id"],kind=item.get("kind","incident"),title=item.get("title","Untitled"),service=item.get("service","Platform"),severity=item.get("severity","-"),category=item.get("category","General"),date=item.get("date",datetime.now(timezone.utc).date().isoformat()),content=item["content"],root_cause=item.get("root_cause",""),resolution=item.get("resolution",""),duration_minutes=item.get("duration_minutes",0),seeded=seeded))
        db.commit()

def retain_item(item, context="IncidentMind engineering operations memory"):
    meta={k:item.get(k) for k in ("service","severity","category","date","id","kind","title")}
    meta["tags"]=[item.get("category","incident"),item.get("service","Platform")]
    _, ms=memory.retain(item["content"],meta,context)
    record_memory(item, seeded=item.get("seeded",False))
    return ms

@app.on_event("startup")
async def startup():
    init_db()
    try:
        await asyncio.to_thread(memory.ensure_bank)
        with SessionLocal() as db:
            seeded=db.get(Setting,"seed_v1")
            if settings.seed_on_start and not seeded:
                for original in ALL:
                    with SessionLocal() as check_db:
                        if check_db.get(MemoryRecord, original["id"]): continue
                    item={**original,"seeded":True}
                    try: await asyncio.to_thread(retain_item,item,"Seeded engineering incident, runbook or post-mortem")
                    except Exception as exc:
                        log.warning("Seed retain failed for %s: %s",item["id"],exc)
                        break
                else:
                    db.add(Setting(key="seed_v1",value="complete")); db.commit()
                    log.info("Seeded %s Hindsight memories",len(ALL))
    except Exception as exc: log.warning("Hindsight startup unavailable; API remains online: %s",exc)

@app.get("/api/health")
async def health():
    connected=False; error=memory.error
    try:
        if memory.client:
            await asyncio.to_thread(memory.client.recall, bank_id=settings.hindsight_bank_id, query="IncidentMind health check", budget="low")
            connected=True; memory.error=""; error=""
    except Exception as exc: error=str(exc); memory.error=error
    with SessionLocal() as db:
        local_total=db.scalar(select(func.count()).select_from(MemoryRecord)) or 0
        resolved=db.scalar(select(func.count()).select_from(MemoryRecord).where(MemoryRecord.kind=="resolved_incident")) or 0
    total=local_total
    if connected:
        try:
            listing=await asyncio.to_thread(memory.client.list_memories,bank_id=settings.hindsight_bank_id,limit=1)
            total=listing.total
        except Exception: pass
    return {"hindsight_connected":connected,"hindsight_error":error,"bank_id":settings.hindsight_bank_id,"total_memories":total,"incidents_resolved":resolved,"llm_configured":bool(settings.llm_api_key)}

@app.get("/api/memories")
async def memories(q: str="", service: str="", severity: str="", category: str="", kind: str="", limit: int=100, offset: int=0):
    with SessionLocal() as db:
        stmt=select(MemoryRecord).order_by(MemoryRecord.date.desc())
        if service: stmt=stmt.where(MemoryRecord.service==service)
        if severity: stmt=stmt.where(MemoryRecord.severity==severity)
        if category: stmt=stmt.where(MemoryRecord.category==category)
        if kind: stmt=stmt.where(MemoryRecord.kind==kind)
        local=[row_dict(x) for x in db.scalars(stmt).all()]
    search_count=0; search_ms=0
    if q:
        try:
            recalled,search_ms=await asyncio.to_thread(memory.recall,q)
            search_count=len(recalled)
            ids={str(x.get("metadata",{}).get("id","")) for x in recalled}
            if ids: local=[x for x in local if x["id"] in ids or q.lower() in (x["title"]+x["content"]).lower()]
            else: local=[x for x in local if q.lower() in (x["title"]+x["content"]).lower()]
        except Exception: local=[x for x in local if q.lower() in (x["title"]+x["content"]).lower()]
    return {"items":local[offset:offset+limit],"total":len(local),"search_count":search_count,"search_duration_ms":search_ms}

@app.get("/api/memories/search")
async def search_memories(q: str):
    try:
        rows,ms=await asyncio.to_thread(memory.recall,q)
        return {"items":rows,"count":len(rows),"duration_ms":ms}
    except Exception as exc: raise HTTPException(503,f"Hindsight recall failed: {exc}")

@app.post("/api/incidents")
async def create_incident(body: IncidentIn):
    iid=f"LIVE-{datetime.now(timezone.utc).strftime('%y%m%d%H%M%S%f')}"
    item={"id":iid,"kind":"live_incident","title":body.title,"service":body.service,"severity":body.severity,"category":"Investigation","date":datetime.now(timezone.utc).date().isoformat(),"content":f"Incident {iid}: {body.title}. Service: {body.service}. Severity: {body.severity}. Environment: {body.environment}. Description: {body.description}","root_cause":"","resolution":"","duration_minutes":0}
    with SessionLocal() as db:
        db.add(MemoryRecord(id=iid,kind=item["kind"],title=body.title,service=body.service,severity=body.severity,category="Investigation",date=item["date"],content=item["content"])); db.commit()
    retain_ms=0; retain_success=False
    try:
        _, retain_ms=await asyncio.to_thread(memory.retain,item["content"],{"id":iid,"service":body.service,"severity":body.severity,"category":"Investigation","date":item["date"],"tags":[body.service,body.severity,"live-incident"]},"Key facts from a live incident investigation")
        retain_success=True
        log.info("Hindsight retained initial facts for %s in %dms",iid,retain_ms)
    except Exception as exc: log.warning("Initial live incident retain failed: %s",exc)
    try:
        related,ms=await asyncio.to_thread(memory.recall,item["content"])
        log.info("Hindsight recall for incident %s returned %d memories in %dms",iid,len(related),ms)
    except Exception as exc: related=[]; log.warning("Incident recall failed: %s",exc)
    return {"incident":item,"memories":related,"memory_count":len(related),"retain_duration_ms":retain_ms,"retain_success":retain_success}

async def llm_stream(messages, recalled, incident_id, user_text, incident_context=""):
    references="\n\nRECALLED HINDSIGHT MEMORIES (cite incident/runbook IDs when using them):\n"+"\n".join(f"[{m.get('metadata',{}).get('id','memory')}] {m['text']}" for m in recalled)
    system="You are IncidentMind, an experienced production incident response partner. Be concise, practical, and ordered. Ground recommendations in the current incident and recalled memories; distinguish evidence from hypotheses. Cite source IDs in square brackets. Never invent details. When recalled evidence is absent, say so. Suggest the next checks with commands or metrics when useful.\n\nCURRENT INCIDENT CONTEXT:\n"+incident_context
    msgs=[{"role":"system","content":system+references}]+messages[-12:]
    if settings.llm_api_key:
        try:
            async with httpx.AsyncClient(timeout=90) as client:
                if settings.llm_provider.lower()=="anthropic":
                    payload={"model":settings.llm_model,"max_tokens":700,"system":msgs[0]["content"],"messages":[m for m in msgs[1:] if m["role"] in ("user","assistant")],"stream":True}
                    async with client.stream("POST","https://api.anthropic.com/v1/messages",headers={"x-api-key":settings.llm_api_key,"anthropic-version":"2023-06-01","content-type":"application/json"},json=payload) as resp:
                        resp.raise_for_status()
                        async for line in resp.aiter_lines():
                            if line.startswith("data: "):
                                try:
                                    obj=json.loads(line[6:]); part=obj.get("delta",{}).get("text","")
                                    if part: yield part
                                except Exception: pass
                else:
                    payload={"model":settings.llm_model,"messages":msgs,"stream":True,"temperature":0.25}
                    async with client.stream("POST","https://api.openai.com/v1/chat/completions",headers={"Authorization":f"Bearer {settings.llm_api_key}"},json=payload) as resp:
                        resp.raise_for_status()
                        async for line in resp.aiter_lines():
                            if line.startswith("data: ") and line[6:]!="[DONE]":
                                try:
                                    part=json.loads(line[6:])["choices"][0]["delta"].get("content","")
                                    if part: yield part
                                except Exception: pass
            return
        except Exception as exc: log.warning("LLM stream failed; using local memory-grounded response: %s",exc)
    if recalled:
        top=recalled[:3]
        words="Based on the recalled incident memory, start by checking the signals that distinguished those events.\n\n"
        words+="1. Compare current symptoms with the closest prior cases: "+"; ".join(f"[{x.get('metadata',{}).get('id','memory')}] {x['text'][:260]}" for x in top)+"\n\n"
        words+="2. Validate the suspected dependency and its saturation/error metrics before changing capacity.\n3. Apply the smallest reversible mitigation, then verify the error rate and latency return to baseline.\n\nThese are memory-based leads, not a confirmed diagnosis. Share recent changes and key metrics to narrow the next step."
    else: words="I couldn't retrieve matching incident memories, so I can't ground a historical comparison yet. Check recent deployments, dependency health, saturation signals, and request traces. Share the key error and latency metrics and I can help narrow the next step."
    for token in words.split(" "): yield token+" "; await asyncio.sleep(.012)

@app.post("/api/chat/stream")
async def chat_stream(body: ChatIn):
    with SessionLocal() as db:
        live=db.get(MemoryRecord,body.incident_id)
        hist=db.scalars(select(ChatMessage).where(ChatMessage.incident_id==body.incident_id).order_by(ChatMessage.id)).all()
        history=[{"role":x.role,"content":x.content} for x in hist]
        db.add(ChatMessage(incident_id=body.incident_id,role="user",content=body.message)); db.commit()
    incident_context=live.content if live else f"Incident ID: {body.incident_id}"
    recall_query=f"{incident_context}\nCurrent investigation question: {body.message}"
    try: recalled,recall_ms=await asyncio.to_thread(memory.recall,recall_query)
    except Exception as exc: recalled=[]; recall_ms=0; log.warning("Chat recall failed: %s",exc)
    async def events():
        answer=""
        yield "event: memories\ndata: "+json.dumps({"items":recalled,"count":len(recalled),"duration_ms":recall_ms})+"\n\n"
        try:
            async for chunk in llm_stream(history+[{"role":"user","content":body.message}],recalled,body.incident_id,body.message,incident_context):
                answer+=chunk; yield "event: token\ndata: "+json.dumps({"text":chunk})+"\n\n"
            if answer.strip():
                with SessionLocal() as db:
                    db.add(ChatMessage(incident_id=body.incident_id,role="assistant",content=answer)); db.commit()
                with SessionLocal() as db:
                    live=db.get(MemoryRecord,body.incident_id)
                learned=f"Live investigation {body.incident_id}: Engineer asked: {body.message}. Assistant guidance: {answer}"
                try:
                    _,ms=await asyncio.to_thread(memory.retain,learned,{"id":body.incident_id,"service":live.service if live else "unknown-service","severity":live.severity if live else "-","category":"Live investigation","date":datetime.now(timezone.utc).date().isoformat(),"tags":["investigation","chat"]},"Key facts and conclusions from a live incident investigation")
                    note_id=f"NOTE-{body.incident_id}-{datetime.now(timezone.utc).strftime('%H%M%S%f')}"
                    record_memory({"id":note_id,"kind":"investigation_note","title":f"Investigation update: {body.message[:90]}","service":live.service if live else "unknown-service","severity":live.severity if live else "-","category":"Live investigation","date":datetime.now(timezone.utc).date().isoformat(),"content":learned,"root_cause":"","resolution":"","duration_minutes":0})
                    yield "event: activity\ndata: "+json.dumps({"operation":"retain","count":1,"duration_ms":ms})+"\n\n"
                except Exception as exc: log.warning("Chat learning retain failed: %s",exc)
            yield "event: done\ndata: {}\n\n"
        except Exception as exc: yield "event: error\ndata: "+json.dumps({"message":str(exc)})+"\n\n"
    return StreamingResponse(events(),media_type="text/event-stream",headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.post("/api/incidents/resolve")
async def resolve(body: ResolveIn):
    with SessionLocal() as db:
        original=db.get(MemoryRecord,body.incident_id)
        if not original: raise HTTPException(404,"Incident not found")
        messages=db.scalars(select(ChatMessage).where(ChatMessage.incident_id==body.incident_id).order_by(ChatMessage.id)).all()
        transcript="\n".join(f"{m.role}: {m.content}" for m in messages)
        iid=f"RES-{body.incident_id}"
        content=f"Resolved incident {body.incident_id}: {original.title}. Service: {original.service}. Severity: {original.severity}. Environment: production. Original symptoms: {original.content}. Root cause: {body.root_cause}. Fix applied: {body.fix_applied}. Steps that worked: {body.steps_worked}. Steps that failed: {body.steps_failed}. Lessons learned: {body.lessons}. Investigation transcript: {transcript}"
        item={"id":iid,"kind":"resolved_incident","title":original.title,"service":original.service,"severity":original.severity,"category":"Resolved","date":datetime.now(timezone.utc).date().isoformat(),"content":content,"root_cause":body.root_cause,"resolution":body.fix_applied,"duration_minutes":0}
    try:
        ms=await asyncio.to_thread(retain_item,item)
        return {"item":item,"retained":True,"duration_ms":ms}
    except Exception as exc: raise HTTPException(503,f"Could not retain resolved incident in Hindsight: {exc}")

@app.post("/api/knowledge")
async def add_knowledge(body: KnowledgeIn):
    if body.kind not in ("incident","runbook","postmortem"): raise HTTPException(400,"Kind must be incident, runbook or postmortem")
    iid=f"{body.kind[:3].upper()}-{datetime.now(timezone.utc).strftime('%y%m%d%H%M%S%f')}"
    item={"id":iid,"kind":body.kind,"title":body.title,"service":body.service,"severity":body.severity,"category":body.category,"date":datetime.now(timezone.utc).date().isoformat(),"content":f"{body.kind.title()} {iid}: {body.title}. Service: {body.service}. Severity: {body.severity}. Category: {body.category}. {body.content}","root_cause":"","resolution":"","duration_minutes":0}
    try: ms=await asyncio.to_thread(retain_item,item,f"User-added {body.kind} knowledge")
    except Exception as exc: raise HTTPException(503,f"Could not retain knowledge in Hindsight: {exc}")
    return {"item":item,"duration_ms":ms}

@app.post("/api/knowledge/upload")
async def upload_knowledge(file: UploadFile=File(...), kind: str=Form("runbook")):
    if not file.filename.lower().endswith((".txt",".md",".markdown")): raise HTTPException(400,"Upload a .txt or Markdown file")
    raw=(await file.read()).decode("utf-8",errors="replace")
    if not raw.strip(): raise HTTPException(400,"File is empty")
    title=file.filename.rsplit(".",1)[0]
    return await add_knowledge(KnowledgeIn(kind=kind,title=title,content=raw))

@app.get("/api/insights")
async def insights():
    with SessionLocal() as db: rows=[row_dict(x) for x in db.scalars(select(MemoryRecord)).all()]
    incidents=[x for x in rows if x["kind"] in ("incident","resolved_incident")]
    roots={}
    services={}
    for x in incidents:
        if x["root_cause"]: roots[x["root_cause"]]=roots.get(x["root_cause"],0)+1
        services[x["service"]]=services.get(x["service"],0)+1
    try: reflection,ms=await asyncio.to_thread(memory.reflect,"Analyze recurring production incident root causes, repeat-offender services, effective fixes, and lessons learned. Cite memory IDs.")
    except Exception as exc: reflection={"text":f"Hindsight reflect unavailable: {exc}","based_on":[]}; ms=0
    trend={}
    for x in incidents:
        month=x.get("date","")[:7]
        if month: trend[month]=trend.get(month,0)+x.get("duration_minutes",0)
    monthly=[{"month":k,"mean_minutes":round(sum(x.get("duration_minutes",0) for x in incidents if x.get("date","").startswith(k))/max(1,sum(1 for x in incidents if x.get("date","").startswith(k))))} for k in sorted(trend)]
    return {"top_root_causes":[{"name":k,"count":v} for k,v in sorted(roots.items(),key=lambda x:-x[1])[:6]],"top_services":[{"name":k,"count":v} for k,v in sorted(services.items(),key=lambda x:-x[1])[:6]],"mttr":monthly,"reflection":reflection,"reflect_duration_ms":ms}

@app.get("/api/activity")
async def activity(): return {"items":[]}
