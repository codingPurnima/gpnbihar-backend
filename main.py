import os
import secrets
import time
from uuid import uuid4
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from livekit import api

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field

load_dotenv()

app = FastAPI(title="GPNBIHAR Recording API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "https://gpnbihar-studio.vercel.app",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

bearer = HTTPBearer()

LIVEKIT_URL = os.environ["LIVEKIT_URL"]
LIVEKIT_API_KEY = os.environ["LIVEKIT_API_KEY"]
LIVEKIT_API_SECRET = os.environ["LIVEKIT_API_SECRET"]

S3_ENDPOINT = os.environ["SUPABASE_S3_ENDPOINT"]
S3_REGION = os.environ["SUPABASE_S3_REGION"]
S3_BUCKET = os.environ["SUPABASE_S3_BUCKET"]
S3_ACCESS_KEY = os.environ["SUPABASE_S3_ACCESS_KEY"]
S3_SECRET_KEY = os.environ["SUPABASE_S3_SECRET_KEY"]

STUDIO_ADMIN_TOKEN = os.environ["STUDIO_ADMIN_TOKEN"]
ANCHOR_ACCESS_CODE = os.environ.get("ANCHOR_ACCESS_CODE", "")
SESSION_TTL_SECONDS = 4 * 60 * 60


class AnchorSessionRequest(BaseModel):
    access_code: str


def encode_part(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def decode_part(value: str) -> bytes:
    return base64.urlsafe_b64decode(
        value + "=" * (-len(value) % 4)
    )


def create_anchor_session() -> str:
    payload = {
        "scope": "anchor",
        "exp": int(time.time()) + SESSION_TTL_SECONDS,
    }

    encoded = encode_part(
        json.dumps(payload, separators=(",", ":")).encode()
    )

    signature = hmac.new(
        STUDIO_ADMIN_TOKEN.encode(),
        encoded.encode(),
        hashlib.sha256,
    ).digest()

    return f"{encoded}.{encode_part(signature)}"


def require_anchor(
    credentials: HTTPAuthorizationCredentials = Depends(bearer),
):
    try:
        encoded, supplied_signature = credentials.credentials.split(".", 1)

        expected_signature = hmac.new(
            STUDIO_ADMIN_TOKEN.encode(),
            encoded.encode(),
            hashlib.sha256,
        ).digest()

        if not hmac.compare_digest(
            expected_signature,
            decode_part(supplied_signature),
        ):
            raise ValueError("Invalid signature")

        payload = json.loads(decode_part(encoded))

        if payload.get("scope") != "anchor":
            raise ValueError("Invalid scope")

        if payload.get("exp", 0) <= int(time.time()):
            raise ValueError("Session expired")

    except Exception as exc:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired anchor session",
        ) from exc

ROOM_NAME = "gpnbihar-poc-room"
active_egress_id: str | None = None




class GuestSessionRequest(BaseModel):
    name: str = Field(default="Browser Guest", max_length=60)


@app.post("/guest/session")
async def create_guest_session(request: GuestSessionRequest):
    guest_name = request.name.strip() or "Browser Guest"

    token = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(f"guest-{uuid4()}")
        .with_name(guest_name)
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=ROOM_NAME,
                can_publish=True,
                can_subscribe=True,
            )
        )
        .with_ttl(timedelta(hours=2))
        .to_jwt()
    )

    client_url = LIVEKIT_URL.replace(
        "https://", "wss://"
    ).replace("http://", "ws://")

    return {
        "server_url": client_url,
        "livekit_token": token,
        "room_name": ROOM_NAME,
    }

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/anchor/session")
async def create_anchor_session_endpoint(
    request: AnchorSessionRequest,
):
    if not ANCHOR_ACCESS_CODE:
        raise HTTPException(
            status_code=503,
            detail="Anchor access is not configured",
        )

    if not secrets.compare_digest(
        request.access_code,
        ANCHOR_ACCESS_CODE,
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid anchor access code",
        )

    livekit_token = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity("gpnbihar-anchor-poc")
        .with_name("GPNBIHAR Anchor")
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=ROOM_NAME,
                can_publish=True,
                can_subscribe=True,
            )
        )
        .with_ttl(timedelta(hours=4))
        .to_jwt()
    )

    client_url = LIVEKIT_URL
    if client_url.startswith("https://"):
        client_url = "wss://" + client_url[len("https://"):]
    elif client_url.startswith("http://"):
        client_url = "ws://" + client_url[len("http://"):]

    return {
        "server_url": client_url,
        "livekit_token": livekit_token,
        "session_token": create_anchor_session(),
        "room_name": ROOM_NAME,
        "identity": "gpnbihar-anchor-poc",
    }

@app.post(
    "/recording/start",
    dependencies=[Depends(require_anchor)],
)
async def start_news_recording(room_name: str):
    if room_name != ROOM_NAME:
        raise HTTPException(
            status_code=403,
            detail="Unauthorized room",
        )

    global active_egress_id

    # Point this to your deployed Vite recorder app on Vercel
    recorder_template_url = "https://gpnbihar-recorder-end.vercel.app/"

    # Ensure S3 endpoint is a valid absolute URI
    s3_endpoint = S3_ENDPOINT.strip()
    if not s3_endpoint.startswith("http://") and not s3_endpoint.startswith("https://"):
        s3_endpoint = f"https://{s3_endpoint}"
    # Remove any trailing slashes
    s3_endpoint = s3_endpoint.rstrip("/")

    egress_request = api.RoomCompositeEgressRequest(
        room_name=room_name,
        layout="grid",
        custom_base_url=recorder_template_url,
        preset=api.EncodingOptionsPreset.H264_1080P_30,
        file=api.EncodedFileOutput(
            file_type=api.EncodedFileType.MP4,
            filepath=f"recordings/{room_name}-{int(time.time())}.mp4",
            s3=api.S3Upload(
                endpoint=s3_endpoint,
                region=S3_REGION or "us-east-1",  # Supabase requires a region string
                bucket=S3_BUCKET,
                access_key=S3_ACCESS_KEY,
                secret=S3_SECRET_KEY,
                force_path_style=True,  # Mandatory for Supabase S3 storage
            ),
        ),
    )

    async with api.LiveKitAPI(
        LIVEKIT_URL,
        api_key=LIVEKIT_API_KEY,
        api_secret=LIVEKIT_API_SECRET,
    ) as lkapi:
        response = await lkapi.egress.start_room_composite_egress(egress_request)

    active_egress_id = response.egress_id

    return {
        "egress_id": response.egress_id,
        "status": str(response.status),
    }

@app.post("/recording/stop", dependencies=[Depends(require_anchor)])
async def stop_recording():
    global active_egress_id

    if not active_egress_id:
        raise HTTPException(
            status_code=409,
            detail="No active recording was started by this API instance.",
        )

    egress_id = active_egress_id

    try:
        async with api.LiveKitAPI(
            LIVEKIT_URL,
            api_key=LIVEKIT_API_KEY,
            api_secret=LIVEKIT_API_SECRET,
        ) as lk:
            print(f"[recording-debug] Stop requested for {egress_id}")
            result = await lk.egress.stop_egress(
                api.StopEgressRequest(egress_id=egress_id)
            )

        active_egress_id = None

        return {
            "status": "stop_requested",
            "egress_id": result.egress_id,
        }

    except Exception as exc:
        print(f"Stop recording failed: {exc}")
        raise HTTPException(
            status_code=502,
            detail="LiveKit could not stop the recording.",
        ) from exc

@app.get(
    "/recording/status/{egress_id}",
    dependencies=[Depends(require_anchor)],
)
async def recording_status(egress_id: str):
    try:
        async with api.LiveKitAPI(
            LIVEKIT_URL,
            api_key=LIVEKIT_API_KEY,
            api_secret=LIVEKIT_API_SECRET,
        ) as lk:
            info = await lk.egress.list_egress(
                api.ListEgressRequest(
                    egress_id=egress_id,
                )
            )
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Egress ID not found or expired: {exc}",
        ) from exc

    if not info.items:
        raise HTTPException(
            status_code=404,
            detail="Egress ID not found",
        )

    item = info.items[0]

    return {
        "egress_id": item.egress_id,
        "status": str(item.status),
        "error": item.error,
        "started_at": item.started_at,
        "ended_at": item.ended_at,
        "file_results": [
            {
                "filename": f.filename,
                "location": f.location,
            }
            for f in item.file_results
        ],
    }


@app.get("/debug/egress/{egress_id}", dependencies=[Depends(require_anchor)])
async def debug_egress(
    egress_id: str,
):
    try:
        async with api.LiveKitAPI(
            LIVEKIT_URL,
            api_key=LIVEKIT_API_KEY,
            api_secret=LIVEKIT_API_SECRET,
        ) as lk:
            response = await lk.egress.list_egress(
                api.ListEgressRequest(egress_id=egress_id)
            )
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Egress not found or expired: {exc}",
        ) from exc

    if not response.items:
        raise HTTPException(
            status_code=404,
            detail="Egress not found",
        )

    e = response.items[0]

    return {
        "egress_id": e.egress_id,
        "status": str(e.status),
        "error": e.error,
        "error_code": e.error_code,
        "details": e.details,
        "file_results": [
            {
                "filename": f.filename,
                "location": f.location,
            }
            for f in e.file_results
        ],
    }