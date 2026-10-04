"""
auth_otp.py — phone-number login: send OTP, verify it, capture name.

This backs the onboarding flow (Phone -> OTP+Name -> Hello) added to the
frontend. It supports TWO live OTP providers plus a demo fallback, tried
in this order:

  1. Twilio Verify (recommended — no India DLT registration needed on
     your end; Twilio's Verify product handles that compliance itself)
        https://www.twilio.com/docs/verify/api

     Setup (twilio.com console):
       a. Sign up / log in at https://console.twilio.com
       b. Copy your Account SID and Auth Token from the console home page
       c. Go to Verify -> Services -> create a new Service -> copy its
          Service SID (starts with "VA...")
       d. Set as environment variables before starting uvicorn:
            TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
            TWILIO_AUTH_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
            TWILIO_VERIFY_SID=VAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

  2. MSG91 OTP API (https://docs.msg91.com/otp/otp-api) — requires Indian
     DLT (PE/template) registration to actually deliver SMS to Indian
     numbers, so this is mostly useful once you have a registered
     business. Set MSG91_AUTH_KEY + MSG91_TEMPLATE_ID to use it.

  3. Demo fallback (no env vars set): prints the OTP to the server
     console instead of sending a real SMS, and "0000" always verifies
     successfully — so the app stays fully testable with zero external
     accounts, exactly like DEMO_MODE does for the Gemini chat.
"""
import os
import random
import time

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import storage

router = APIRouter(prefix="/auth", tags=["auth"])

# ---- Twilio Verify (preferred live provider) ----
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_VERIFY_SID = os.environ.get("TWILIO_VERIFY_SID", "").strip()
TWILIO_LIVE = bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_VERIFY_SID)
TWILIO_BASE = f"https://verify.twilio.com/v2/Services/{TWILIO_VERIFY_SID}"

# ---- MSG91 (secondary live provider, needs India DLT registration) ----
MSG91_AUTH_KEY = os.environ.get("MSG91_AUTH_KEY", "").strip()
MSG91_TEMPLATE_ID = os.environ.get("MSG91_TEMPLATE_ID", "").strip()
MSG91_LIVE = bool(MSG91_AUTH_KEY and MSG91_TEMPLATE_ID)

OTP_BASE = "https://control.msg91.com/api/v5/otp"
OTP_TTL_SECONDS = 5 * 60

# What /health reports, and what actually runs below.
OTP_PROVIDER = "twilio" if TWILIO_LIVE else ("msg91" if MSG91_LIVE else "demo")


class SendOtpRequest(BaseModel):
    phone: str  # 10 digits, no country code


class VerifyOtpRequest(BaseModel):
    phone: str
    otp: str
    name: str
    language: str = "en"
    whatsapp_consent: bool = True


def _to_e164_no_plus(phone: str) -> str:
    """MSG91 wants the number as e.g. 919876543210 (country code, no '+')."""
    return f"91{phone}"


def _to_e164(phone: str) -> str:
    """Twilio wants a leading '+', e.g. +919876543210."""
    return f"+91{phone}"


@router.post("/send-otp")
async def send_otp(req: SendOtpRequest):
    phone = req.phone.strip()
    if len(phone) != 10 or not phone.isdigit():
        raise HTTPException(400, "Enter a valid 10-digit mobile number")

    if TWILIO_LIVE:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.post(
                f"{TWILIO_BASE}/Verifications",
                auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
                data={"To": _to_e164(phone), "Channel": "sms"},
            )
            data = res.json()
        if res.status_code >= 400:
            raise HTTPException(502, f"Twilio couldn't send the OTP: {data.get('message', 'unknown error')}")
        return {"status": "sent", "mode": "live", "provider": "twilio", "request_id": data.get("sid")}

    if MSG91_LIVE:
        mobile = _to_e164_no_plus(phone)
        params = {
            "template_id": MSG91_TEMPLATE_ID,
            "mobile": mobile,
            "authkey": MSG91_AUTH_KEY,
            "otp_expiry": 5,  # minutes
        }
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(OTP_BASE, params=params)
            data = res.json()
        if data.get("type") != "success":
            raise HTTPException(502, f"MSG91 couldn't send the OTP: {data.get('message', 'unknown error')}")
        return {"status": "sent", "mode": "live", "provider": "msg91", "request_id": data.get("message")}

    # Demo fallback — see module docstring.
    code = f"{random.randint(0, 9999):04d}"
    otps = storage.get_otps()
    otps[phone] = {"code": code, "expires_at": time.time() + OTP_TTL_SECONDS}
    storage.save_otps(otps)
    print(f"[OTP:demo] would send '{code}' to +91{phone} (no live OTP provider configured — see auth_otp.py)")
    return {"status": "sent", "mode": "demo", "provider": "demo", "expires_in": OTP_TTL_SECONDS}


@router.post("/verify-otp")
async def verify_otp(req: VerifyOtpRequest):
    phone = req.phone.strip()

    if TWILIO_LIVE:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.post(
                f"{TWILIO_BASE}/VerificationCheck",
                auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
                data={"To": _to_e164(phone), "Code": req.otp},
            )
            data = res.json()
        if res.status_code >= 400 or data.get("status") != "approved":
            raise HTTPException(400, data.get("message", "Incorrect or expired code"))

    elif MSG91_LIVE:
        mobile = _to_e164_no_plus(phone)
        params = {"otp": req.otp, "mobile": mobile, "authkey": MSG91_AUTH_KEY}
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(f"{OTP_BASE}/verify", params=params)
            data = res.json()
        if data.get("type") != "success":
            raise HTTPException(400, data.get("message", "Incorrect or expired code"))

    else:
        otps = storage.get_otps()
        record = otps.get(phone)
        if req.otp != "0000":  # dev bypass, see module docstring
            if not record:
                raise HTTPException(400, "No OTP was requested for this number")
            if time.time() > record["expires_at"]:
                raise HTTPException(400, "OTP expired — request a new one")
            if req.otp != record["code"]:
                raise HTTPException(400, "Incorrect code")
        otps.pop(phone, None)
        storage.save_otps(otps)

    storage.save_user(
        phone,
        {
            "phone": phone,
            "name": req.name,
            "language": req.language,
            "whatsapp_consent": req.whatsapp_consent,
            "created_at": time.time(),
            "prefs": {
                "whatsapp": req.whatsapp_consent,
                "sms_fallback": True,
                "severe_weather": True,
                "agro_advisory": True,
                "push": not req.whatsapp_consent,
            },
        },
    )
    return {"status": "verified", "name": req.name}
