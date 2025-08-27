import os
import time
import hmac
import base64
import hashlib
from typing import Optional
from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel
import jwt

"""
Orchestrator Service
Responsibilities:
- Issue LiveKit access tokens (JWT) for participants (agent, load test, PSTN via Twilio).
- Twilio Voice webhook -> returns TwiML that dials LiveKit SIP ingress with room + token.
Environment Variables (see .env.example):
  LIVEKIT_API_KEY
  LIVEKIT_API_SECRET
  LIVEKIT_HOST
  ORCH_JWT_TTL_SECONDS
  TWILIO_SIP_INGRESS_HOST
  TWILIO_VOICE_WEBHOOK_SECRET (optional HMAC validation)
"""

LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")
JWT_TTL = int(os.getenv("ORCH_JWT_TTL_SECONDS", "60"))
SIP_INGRESS_HOST = os.getenv("TWILIO_SIP_INGRESS_HOST", "sip.example.com")
VOICE_WEBHOOK_SECRET = os.getenv("TWILIO_VOICE_WEBHOOK_SECRET")
ISSUER = os.getenv("JWT_ISSUER", "telephony-demo")
AUDIENCE = os.getenv("JWT_AUDIENCE", "livekit")

if not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
    raise RuntimeError("LIVEKIT_API_KEY / LIVEKIT_API_SECRET must be set")

app = FastAPI(title="Orchestrator", version="0.1.0")


class TokenRequest(BaseModel):
    room: str
    identity: str
    publish: bool = True
    subscribe: bool = True
    metadata: Optional[str] = None
    ttl_seconds: Optional[int] = None


def build_livekit_token(room: str, identity: str, can_publish: bool = True, can_subscribe: bool = True,
                        metadata: Optional[str] = None, ttl_seconds: Optional[int] = None) -> str:
    now = int(time.time())
    exp = now + (ttl_seconds or JWT_TTL)
    # LiveKit video grant schema
    video_grant = {
        "roomJoin": True,
        "room": room,
        "canPublish": can_publish,
        "canSubscribe": can_subscribe,
        "canPublishData": True,
    }
    payload = {
        "iss": LIVEKIT_API_KEY,
        "sub": identity,
        "nbf": now - 1,
        "exp": exp,
        "video": video_grant,
        "metadata": metadata or ""
    }
    token = jwt.encode(payload, LIVEKIT_API_SECRET, algorithm="HS256")
    # pyjwt >= 2 returns str
    return token


@app.get("/health")
async def health():
    return {"status": "ok", "time": int(time.time())}


@app.post("/token")
async def token(req: TokenRequest):
    t = build_livekit_token(
        room=req.room,
        identity=req.identity,
        can_publish=req.publish,
        can_subscribe=req.subscribe,
        metadata=req.metadata,
        ttl_seconds=req.ttl_seconds
    )
    return {"token": t, "ttl": req.ttl_seconds or JWT_TTL}


def verify_twilio_signature(request: Request, body: bytes):
    """
    Optional simple HMAC validation (not Twilio's exact scheme).
    If VOICE_WEBHOOK_SECRET set, compute HMAC-SHA256(secret, raw_body) hex == X-Twilio-Signature header.
    """
    if not VOICE_WEBHOOK_SECRET:
        return True
    header = request.headers.get("X-Twilio-Signature")
    if not header:
        return False
    mac = hmac.new(VOICE_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, header)


@app.post("/twilio/voice")
async def twilio_voice(request: Request,
                       CallSid: str = Form(...),
                       From: str = Form("anonymous"),
                       To: str = Form(""),
                       Caller: str = Form(""),
                       AccountSid: str = Form("")):
    # Basic (optional) signature verification
    body = await request.body()
    if not verify_twilio_signature(request, body):
        raise HTTPException(status_code=403, detail="Invalid signature")

    # Derive room name per call (isolated)
    room = f"call-{CallSid}"
    # Identity for PSTN participant
    suffix = From[-4:].replace("+", "") if From else CallSid[-4:]
    identity = f"pstn-{suffix}"

    lk_token = build_livekit_token(room=room, identity=identity, can_publish=True, can_subscribe=True)

    # Twilio SIP URI referencing LiveKit SIP ingress domain; token passed as query parameter
    sip_uri = f"sip:room-{room}@{SIP_INGRESS_HOST}?token={lk_token}"

    # Minimal TwiML
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Dial>
    <Sip>{sip_uri}</Sip>
  </Dial>
</Response>"""
    return PlainTextResponse(content=twiml, media_type="application/xml")


# Simple root
@app.get("/")
async def root():
    return {"service": "orchestrator", "endpoints": ["/token", "/twilio/voice", "/health"]}


# Run via: uvicorn app:app --host 0.0.0.0 --port 8000
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
