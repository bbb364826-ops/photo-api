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
from urllib.parse import quote

import httpx
from PIL import Image
from bs4 import BeautifulSoup
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, Response
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
            # No photo — send proxy link with all voter data embedded
            log.info(f"No photo ({result.get('error')}), sending proxy link")
            gvari_geo_display = lat_to_geo(req.gvari.strip())
            msg_text = (
                f"🪪 {piadi}\n"
                f"👤 {gvari_geo_display}\n\n"
                f"📸 ფოტოს სანახავად დააჭირე ღილაკს:"
            )
            import base64 as _b64
            caption_b64 = _b64.b64encode(caption.encode("utf-8")).decode("ascii")
            proxy_url = (
                f"https://photo-api.fly.dev/cec-proxy"
                f"?piadi={quote(piadi, safe='')}"
                f"&gvari={quote(gvari_geo_display, safe='')}"
                f"&d={quote(caption_b64, safe='')}"
            )
            tg_r = await client.post(
                f"{tg_base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": msg_text,
                    "reply_markup": {
                        "inline_keyboard": [[
                            {"text": "📸 ფოტო და მონაცემები", "url": proxy_url}
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


# ── Endpoint: GET /cec-proxy ─────────────────────────────────────────────────
@app.get("/cec-proxy", response_class=HTMLResponse)
async def cec_proxy(piadi: str = "", gvari: str = "", d: str = ""):
    """
    Fetches CEC result page, extracts photo + voter data,
    returns a self-contained HTML page with photo embedded as base64.
    Falls back to a data card with copy buttons if CEC blocks the server.
    d = base64-encoded voter caption (from Supabase, passed by n8n).
    """
    if not piadi:
        return HTMLResponse("<p>Missing piadi</p>", status_code=400)

    gvari_geo = gvari.strip()

    import base64 as _b64
    voter_caption = ""
    if d:
        try:
            voter_caption = _b64.b64decode(d).decode("utf-8")
        except Exception:
            voter_caption = ""

    try:
        async with httpx.AsyncClient(
            headers=HEADERS,
            follow_redirects=True,
            timeout=25,
        ) as client:
            # Step 1: GET homepage → CSRF token
            r1 = await client.get(CEC_URL)
            r1.raise_for_status()
            soup1 = BeautifulSoup(r1.text, "html.parser")
            csrf_input = soup1.find("input", {"name": "__RequestVerificationToken"})
            csrf_token = csrf_input["value"] if csrf_input else ""

            # Step 2: POST search form
            r2 = await client.post(
                CEC_URL,
                data={
                    "__RequestVerificationToken": csrf_token,
                    "PersonalId": piadi,
                    "Surname":    gvari_geo,
                },
                headers={
                    **HEADERS,
                    "Referer":      CEC_URL,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Cookie": "; ".join(f"{k}={v}" for k, v in r1.cookies.items()),
                },
            )
            r2.raise_for_status()

            soup2 = BeautifulSoup(r2.text, "html.parser")

            # ── Find photo src ──────────────────────────────────────────────
            photo_src = None
            for sel in [
                "img[src*='GetPhoto']", "img[src*='Photo']",
                "img[src*='photo']",    "img[src*='Handler']",
                "img[src*='Image']",    ".voter-photo img",
                ".photo img",           ".result img",
            ]:
                el = soup2.select_one(sel)
                if el and el.get("src"):
                    photo_src = el["src"]
                    break
            if not photo_src:
                for img_tag in soup2.find_all("img"):
                    src = img_tag.get("src", "")
                    if (src and not src.endswith((".svg", ".ico"))
                            and "logo" not in src.lower()
                            and "icon" not in src.lower()
                            and len(src) > 5):
                        photo_src = src
                        break

            # ── Download photo → base64 ─────────────────────────────────────
            import base64 as _b64
            photo_data_uri = None
            if photo_src:
                if photo_src.startswith("data:"):
                    photo_data_uri = photo_src
                else:
                    if photo_src.startswith("/"):
                        photo_src = CEC_URL.rstrip("/") + photo_src
                    r3 = await client.get(
                        photo_src, headers={**HEADERS, "Referer": CEC_URL}
                    )
                    if r3.status_code == 200:
                        try:
                            from PIL import ImageFilter
                            img_obj = Image.open(io.BytesIO(r3.content)).convert("RGB")
                            w, h = img_obj.size
                            scale = 800 / max(w, h)
                            if scale > 1:
                                nw, nh = int(w * scale), int(h * scale)
                                img_obj = img_obj.resize((nw, nh), Image.LANCZOS)
                                img_obj = img_obj.filter(
                                    ImageFilter.UnsharpMask(radius=1.5, percent=130, threshold=2)
                                )
                            buf = io.BytesIO()
                            img_obj.save(buf, format="JPEG", quality=93)
                            photo_data_uri = (
                                "data:image/jpeg;base64,"
                                + _b64.b64encode(buf.getvalue()).decode()
                            )
                        except Exception:
                            photo_data_uri = (
                                "data:image/jpeg;base64,"
                                + _b64.b64encode(r3.content).decode()
                            )

            # ── Extract voter info rows from tables ─────────────────────────
            rows: list[tuple[str, str]] = []
            for table in soup2.find_all("table"):
                for tr in table.find_all("tr"):
                    cells = [td.get_text(" ", strip=True)
                             for td in tr.find_all(["td", "th"])]
                    if len(cells) >= 2 and cells[0] and cells[1]:
                        rows.append((cells[0], cells[1]))

            # Fallback: generic text blocks if no table
            if not rows:
                for div in soup2.select(
                    ".voter-info,.info-row,.result-item,"
                    "[class*='voter'],[class*='result'],[class*='info']"
                ):
                    text = div.get_text(" ", strip=True)
                    if text and len(text) > 3:
                        rows.append(("", text))

            return HTMLResponse(content=_cec_result_page(
                piadi, gvari_geo, photo_data_uri, rows, voter_caption
            ))

    except Exception as e:
        log.warning(f"cec_proxy error for {piadi}: {e}")
        return HTMLResponse(content=_cec_fallback_page(piadi, gvari_geo, voter_caption))


def _cec_result_page(piadi: str, gvari_geo: str,
                     photo_uri: Optional[str],
                     rows: list,
                     voter_caption: str = "") -> str:
    photo_html = (
        f'<img src="{photo_uri}" alt="ფოტო" '
        f'style="max-width:220px;max-height:280px;border-radius:10px;'
        f'box-shadow:0 4px 16px rgba(0,0,0,.25);margin-bottom:18px">'
        if photo_uri else
        '<p style="color:#999;margin-bottom:18px">📷 ფოტო ვერ მოიძებნა</p>'
    )
    # Prefer voter_caption over raw table rows (already nicely formatted)
    if voter_caption:
        data_html = (
            '<pre style="font-family:inherit;font-size:14px;line-height:1.7;'
            'white-space:pre-wrap;text-align:left;margin:0">'
            + voter_caption.replace("<", "&lt;").replace(">", "&gt;")
            + '</pre>'
        )
    elif rows:
        data_html = "<table>" + "".join(
            f'<tr><td style="color:#888;padding:5px 10px 5px 0;white-space:nowrap">{k}</td>'
            f'<td style="font-weight:600;padding:5px 0">{v}</td></tr>'
            for k, v in rows
        ) + "</table>"
    else:
        data_html = '<p style="color:#bbb">მონაცემები ვერ მოიძებნა</p>'

    return f"""<!DOCTYPE html>
<html lang="ka">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>ამომრჩეველი {piadi}</title>
  <style>
    body{{margin:0;font-family:sans-serif;background:#f0f0f0;
         display:flex;align-items:flex-start;justify-content:center;
         padding:20px 12px;min-height:100vh}}
    .card{{background:#fff;border-radius:16px;padding:24px 20px;
           max-width:440px;width:100%;
           box-shadow:0 4px 24px rgba(0,0,0,.13)}}
    .hdr{{background:#c0392b;color:#fff;border-radius:10px;
          padding:10px 14px;margin-bottom:20px;font-size:14px;
          display:flex;gap:16px;flex-wrap:wrap}}
    .hdr span{{font-weight:700}}
  </style>
</head>
<body>
  <div class="card">
    <div class="hdr">
      <span>🪪 {piadi}</span>
      <span>👤 {gvari_geo}</span>
    </div>
    <div style="text-align:center">{photo_html}</div>
    <div>{data_html}</div>
  </div>
</body>
</html>"""


def _cec_fallback_page(piadi: str, gvari_geo: str, voter_caption: str = "") -> str:
    """
    Shown when CEC is unreachable from our server.
    Shows all voter data from caption + copy buttons + CEC link.
    NO form auto-submit.
    """
    if voter_caption:
        caption_html = (
            '<div style="background:#f8f8f8;border-radius:10px;padding:14px 16px;'
            'margin-bottom:14px;font-size:14px;line-height:1.8;text-align:left">'
            '<pre style="font-family:inherit;white-space:pre-wrap;margin:0">'
            + voter_caption.replace("<", "&lt;").replace(">", "&gt;")
            + '</pre></div>'
        )
    else:
        caption_html = ""

    return f"""<!DOCTYPE html>
<html lang="ka">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>ამომრჩეველი {piadi}</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:sans-serif;background:#f4f4f4;
         display:flex;align-items:flex-start;justify-content:center;
         min-height:100vh;padding:16px}}
    .card{{background:#fff;border-radius:14px;padding:24px 20px;
           max-width:400px;width:100%;
           box-shadow:0 4px 20px rgba(0,0,0,.12)}}
    .hdr{{background:#c0392b;color:#fff;border-radius:10px;
          padding:10px 14px;margin-bottom:16px;font-size:14px;
          display:flex;gap:14px;flex-wrap:wrap}}
    .hdr span{{font-weight:700}}
    .row{{display:flex;align-items:center;gap:8px;background:#f8f8f8;
          border-radius:10px;padding:11px 13px;margin-bottom:10px}}
    .lbl{{color:#888;font-size:13px;width:56px;flex-shrink:0}}
    .val{{font-weight:700;font-size:14px;flex:1;word-break:break-all}}
    .cp{{background:#fff;border:1px solid #ddd;border-radius:6px;
         padding:4px 9px;cursor:pointer;font-size:12px;color:#555;flex-shrink:0}}
    .cp:active{{background:#eee}}
    .btn{{display:block;width:100%;margin-top:14px;padding:13px;
          background:#c0392b;color:#fff;border-radius:10px;
          text-decoration:none;font-size:15px;font-weight:700;text-align:center}}
    .note{{font-size:12px;color:#999;margin-top:10px;text-align:center;line-height:1.5}}
  </style>
</head>
<body>
  <div class="card">
    <div class="hdr">
      <span>🪪 {piadi}</span>
      <span>👤 {gvari_geo}</span>
    </div>
    {caption_html}
    <div class="row">
      <span class="lbl">🪪 №</span>
      <span class="val" id="v_piadi">{piadi}</span>
      <button class="cp" onclick="cp('v_piadi',this)">კოპირება</button>
    </div>
    <div class="row">
      <span class="lbl">👤 გვარი</span>
      <span class="val" id="v_gvari">{gvari_geo}</span>
      <button class="cp" onclick="cp('v_gvari',this)">კოპირება</button>
    </div>
    <a class="btn" href="https://ems-voters.cec.gov.ge/" target="_blank">
      📸 CEC-ზე ფოტო →
    </a>
    <p class="note">CEC-ის საიტზე ჩაწერე პირადი № და გვარი</p>
  </div>
  <script>
    function cp(id, btn) {{
      navigator.clipboard.writeText(document.getElementById(id).textContent)
        .then(function() {{
          btn.textContent = '✓';
          setTimeout(function() {{ btn.textContent = 'კოპირება'; }}, 1500);
        }});
    }}
  </script>
</body>
</html>"""


# ── Health check ─────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "version": "3.6.0-caption",
            "max_concurrent": MAX_CONC}
