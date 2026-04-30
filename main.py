#!/usr/bin/env python3
"""
CEC Photo API Server — lightweight version (no browser, uses httpx)
Fetches voter photos from ems-voters.cec.gov.ge via HTTP + HTML parsing.

Run: uvicorn main:app --host 0.0.0.0 --port 8000
Env: BOT_TOKEN, API_KEY, MAX_CONCURRENT
"""

import asyncio
import io
import logging
import os
import re
from typing import Optional

import httpx
from PIL import Image
from bs4 import BeautifulSoup
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "8789371314:AAHZ4E2x7k-qB08D2l3EWUqgZ-8il0aU1wA")
API_KEY   = os.getenv("API_KEY", "")
MAX_CONC  = int(os.getenv("MAX_CONCURRENT", "5"))

_sem = asyncio.Semaphore(MAX_CONC)

CEC_URL    = "https://ems-voters.cec.gov.ge/"
HEADERS    = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "ka-GE,ka;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

app = FastAPI(title="CEC Photo API", version="3.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# ── Georgian Latin→Geo ──────────────────────────────────────────────────────
_L2G = {
    'a':'ა','b':'ბ','g':'გ','d':'დ','e':'ე','v':'ვ','z':'ზ','T':'თ',
    'i':'ი','k':'კ','l':'ლ','m':'მ','n':'ნ','o':'ო','p':'პ','J':'ჟ',
    'r':'რ','s':'ს','t':'ტ','u':'უ','f':'ფ','q':'ქ','R':'ღ','y':'ყ',
    'S':'შ','C':'ჩ','c':'ც','Z':'ძ','w':'წ','W':'ჭ','x':'ხ','j':'ჯ','h':'ჰ'
}
def lat_to_geo(s: str) -> str:
    return ''.join(_L2G.get(c, c) for c in s)

def _check_key(x_api_key: str = Header(default="")):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ── Models ──────────────────────────────────────────────────────────────────
class SendPhotoRequest(BaseModel):
    piadi:   str
    gvari:   str       # DB Latin, e.g. "kvaracxelia"
    chat_id: str
    caption: str


# ── Core: HTTP-based CEC photo fetch (no browser) ──────────────────────────
async def fetch_cec_photo(piadi: str, gvari_geo: str) -> dict:
    """
    1. GET CEC homepage → extract CSRF token
    2. POST form with piadi + gvari
    3. Parse result HTML → find photo img src
    4. Download photo bytes
    Returns: {success, photo_bytes, mime, error}
    """
    async with _sem:
        try:
            async with httpx.AsyncClient(
                headers=HEADERS,
                follow_redirects=True,
                timeout=20,
            ) as client:

                # ── Step 1: GET homepage, grab CSRF token ───────────────
                r1 = await client.get(CEC_URL)
                r1.raise_for_status()

                soup1 = BeautifulSoup(r1.text, "html.parser")

                # ASP.NET CSRF token in hidden input
                csrf_input = soup1.find(
                    "input", {"name": "__RequestVerificationToken"}
                )
                csrf_token = csrf_input["value"] if csrf_input else ""
                if not csrf_token:
                    log.warning("No CSRF token found on CEC page")

                # ── Step 2: POST the search form ────────────────────────
                form_data = {
                    "__RequestVerificationToken": csrf_token,
                    "PersonalId": piadi,
                    "Surname":    gvari_geo,
                }

                r2 = await client.post(
                    CEC_URL,
                    data=form_data,
                    headers={
                        **HEADERS,
                        "Referer":      CEC_URL,
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Cookie":       "; ".join(
                            f"{k}={v}" for k, v in r1.cookies.items()
                        ),
                    },
                )
                r2.raise_for_status()

                soup2 = BeautifulSoup(r2.text, "html.parser")

                # ── Step 3: Check for error text ────────────────────────
                for err_sel in [
                    ".validation-summary-errors",
                    ".field-validation-error",
                    ".alert-danger",
                    ".error",
                ]:
                    err_el = soup2.select_one(err_sel)
                    if err_el and err_el.get_text(strip=True):
                        msg = err_el.get_text(strip=True)
                        log.info(f"CEC error [{piadi}]: {msg}")
                        return {"success": False, "error": f"cec: {msg}"}

                # ── Step 4: Find photo <img> ─────────────────────────────
                photo_src = None

                # Try specific selectors first (most reliable)
                for sel in [
                    "img[src*='GetPhoto']",
                    "img[src*='Photo']",
                    "img[src*='photo']",
                    "img[src*='Handler']",
                    "img[src*='Image']",
                    ".voter-photo img",
                    ".photo img",
                    ".result img",
                ]:
                    el = soup2.select_one(sel)
                    if el and el.get("src"):
                        photo_src = el["src"]
                        log.info(f"Photo via '{sel}': {photo_src[:80]}")
                        break

                # Fallback: any img with reasonable src
                if not photo_src:
                    for img in soup2.find_all("img"):
                        src = img.get("src", "")
                        # Skip tiny icons, flags, logos
                        if (src and
                                not src.endswith(".svg") and
                                not src.endswith(".ico") and
                                "logo" not in src.lower() and
                                "icon" not in src.lower() and
                                len(src) > 5):
                            photo_src = src
                            log.info(f"Photo via fallback: {src[:80]}")
                            break

                if not photo_src:
                    # Check if there's voter data (found but no photo)
                    body_text = r2.text
                    found = (
                        piadi in body_text or
                        "ამომრჩეველი" in body_text or
                        "voter" in body_text.lower()
                    )
                    err = "voter_found_no_photo" if found else "not_found"
                    return {"success": found,
                            "photo_bytes": None, "mime": None, "error": err}

                # ── Step 5: Download photo ──────────────────────────────
                if photo_src.startswith("data:"):
                    m = re.match(r"data:([^;]+);base64,(.+)", photo_src, re.S)
                    if m:
                        import base64
                        return {
                            "success": True,
                            "photo_bytes": base64.b64decode(m.group(2)),
                            "mime": m.group(1)
                        }

                if photo_src.startswith("/"):
                    photo_src = CEC_URL.rstrip("/") + photo_src

                r3 = await client.get(
                    photo_src,
                    headers={**HEADERS, "Referer": CEC_URL},
                )
                if r3.status_code != 200:
                    return {"success": False,
                            "error": f"photo_download_{r3.status_code}"}

                mime = r3.headers.get(
                    "content-type", "image/jpeg"
                ).split(";")[0].strip()

                return {
                    "success":     True,
                    "photo_bytes": r3.content,
                    "mime":        mime
                }

        except httpx.TimeoutException:
            log.warning(f"Timeout for piadi={piadi}")
            return {"success": False, "error": "timeout"}
        except Exception as e:
            log.exception(f"Error for piadi={piadi}: {e}")
            return {"success": False, "error": str(e)[:300]}


# ── Endpoint: POST /send-photo ───────────────────────────────────────────────
@app.post("/send-photo")
async def send_photo(
    req: SendPhotoRequest,
    x_api_key: str = Header(default=""),
):
    _check_key(x_api_key)

    piadi     = req.piadi.strip()
    gvari_geo = lat_to_geo(req.gvari.strip())
    chat_id   = req.chat_id.strip()
    caption   = req.caption[:1024]   # Telegram caption limit

    log.info(f"/send-photo piadi={piadi} geo={gvari_geo} chat={chat_id}")

    result  = await fetch_cec_photo(piadi, gvari_geo)
    tg_base = f"https://api.telegram.org/bot{BOT_TOKEN}"

    async with httpx.AsyncClient(timeout=30) as client:
        if result["success"] and result.get("photo_bytes"):
            mime = result.get("mime", "image/jpeg")
            ext  = "jpg" if "jpeg" in mime else mime.split("/")[-1]

            # ── Upscale + sharpen, then send as Document (no Telegram compression) ──
            photo_bytes = result["photo_bytes"]
            try:
                from PIL import ImageFilter, ImageEnhance
                img = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
                w, h = img.size
                TARGET = 1200
                scale = TARGET / max(w, h)
                if scale > 1:
                    new_w, new_h = int(w * scale), int(h * scale)
                    img = img.resize((new_w, new_h), Image.LANCZOS)
                    img = img.filter(ImageFilter.UnsharpMask(radius=1.5, percent=120, threshold=2))
                    log.info(f"Upscaled {w}x{h} → {new_w}x{new_h}")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=95)
                photo_bytes = buf.getvalue()
                mime = "image/jpeg"
                ext  = "jpg"
            except Exception as e:
                log.warning(f"Upscale failed, sending original: {e}")

            # sendDocument — Telegram does NOT compress, tap opens full image
            tg_r = await client.post(
                f"{tg_base}/sendDocument",
                data={"chat_id": chat_id, "caption": caption},
                files={"document": (f"photo.{ext}", photo_bytes, mime)},
            )
            tg_j = tg_r.json()
            log.info(f"sendDocument ok={tg_j.get('ok')} "
                     f"err={tg_j.get('description', '')}")
            return {
                "sent":        tg_j.get("ok", False),
                "has_photo":   True,
                "original_size": f"{w}x{h}",
                "final_size":    f"{img.width}x{img.height}",
                "tg_error":    tg_j.get("description"),
            }
        else:
            # No photo — send CEC link so user can view it in browser
            log.info(f"No photo ({result.get('error')}), sending CEC link")
            gvari_geo_display = lat_to_geo(req.gvari.strip())
            msg_text = (
                f"🪪 {piadi}\n"
                f"👤 {gvari_geo_display}\n\n"
                f"📸 ფოტოს სანახავად გახსენი CEC-ის საიტი:\n"
                f"პირადი №: {piadi}\nგვარი: {gvari_geo_display}"
            )
            cec_link = "https://ems-voters.cec.gov.ge/"
            tg_r = await client.post(
                f"{tg_base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": msg_text,
                    "reply_markup": {
                        "inline_keyboard": [[
                            {"text": "📸 ფოტო CEC-ზე", "url": cec_link}
                        ]]
                    }
                },
            )
            tg_j = tg_r.json()
            return {
                "sent":      tg_j.get("ok", False),
                "has_photo": False,
                "cec_error": result.get("error"),
                "tg_error":  tg_j.get("description"),
            }


# ── Health check ─────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "version": "3.4.0-headers",
            "max_concurrent": MAX_CONC}
