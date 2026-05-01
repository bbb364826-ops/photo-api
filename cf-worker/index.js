/**
 * Cloudflare Worker — CEC photo proxy
 * Fetches voter photo from ems-voters.cec.gov.ge
 * GET /?piadi=55001028218&gvari=კვარაცხელია
 * Returns photo bytes (image/jpeg) or JSON error
 */

const CEC = 'https://ems-voters.cec.gov.ge/';

const HEADERS = {
  'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
  'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
  'Accept-Language': 'ka-GE,ka;q=0.9,en-US;q=0.8,en;q=0.7',
  'Accept-Encoding': 'gzip, deflate, br',
  'Connection': 'keep-alive',
  'Upgrade-Insecure-Requests': '1',
  'Cache-Control': 'max-age=0',
};

function err(msg, extra = {}) {
  return Response.json({ success: false, error: msg, ...extra },
    { headers: { 'Access-Control-Allow-Origin': '*' } });
}

function parseCookies(response) {
  const cookies = [];
  // CF Workers: headers.entries() yields each Set-Cookie separately
  for (const [k, v] of response.headers.entries()) {
    if (k.toLowerCase() === 'set-cookie') {
      const kv = v.split(';')[0].trim();
      if (kv) cookies.push(kv);
    }
  }
  // Also try getSetCookie (available in newer CF Workers runtime)
  if (typeof response.headers.getSetCookie === 'function') {
    for (const c of response.headers.getSetCookie()) {
      const kv = c.split(';')[0].trim();
      if (kv && !cookies.includes(kv)) cookies.push(kv);
    }
  }
  return cookies.join('; ');
}

export default {
  async fetch(request) {
    const url = new URL(request.url);

    // Health check
    if (url.pathname === '/health') {
      return Response.json({ ok: true, version: '1.0.0' });
    }

    const piadi = url.searchParams.get('piadi') || '';
    const gvari = url.searchParams.get('gvari') || '';
    if (!piadi || !gvari) {
      return err('missing piadi or gvari');
    }

    try {
      // ── Step 1: GET CEC homepage → CSRF token + cookies ──────────────
      const r1 = await fetch(CEC, { headers: HEADERS });
      if (!r1.ok) return err(`cec_get_${r1.status}`);

      const html1 = await r1.text();
      const csrfMatch = html1.match(
        /name="__RequestVerificationToken"[^>]*value="([^"]+)"/
      );
      const csrf = csrfMatch ? csrfMatch[1] : '';
      const cookieStr = parseCookies(r1);

      // ── Step 2: POST search form ──────────────────────────────────────
      const body = new URLSearchParams({
        '__RequestVerificationToken': csrf,
        'PersonalId': piadi,
        'Surname': gvari,
      });

      const r2 = await fetch(CEC, {
        method: 'POST',
        headers: {
          ...HEADERS,
          'Content-Type': 'application/x-www-form-urlencoded',
          'Referer': CEC,
          'Cookie': cookieStr,
        },
        body: body.toString(),
      });

      const html2 = await r2.text();

      // ── Step 3: Find photo <img> src ──────────────────────────────────
      const photoMatch = html2.match(
        /src=["']([^"']*(?:GetPhoto|getphoto|Photo|Handler)[^"']*)["']/i
      );

      if (!photoMatch) {
        const found = html2.includes(piadi) || html2.includes('ამომრჩეველი');
        return err(found ? 'voter_found_no_photo' : 'not_found', {
          snippet: html2.substring(0, 400),
        });
      }

      let photoUrl = photoMatch[1];
      if (photoUrl.startsWith('/')) photoUrl = CEC.slice(0, -1) + photoUrl;

      // ── Step 4: Download photo ────────────────────────────────────────
      const r3 = await fetch(photoUrl, {
        headers: { ...HEADERS, 'Referer': CEC, 'Cookie': cookieStr },
      });

      if (!r3.ok) return err(`photo_download_${r3.status}`);

      const mime = r3.headers.get('content-type') || 'image/jpeg';
      const buf  = await r3.arrayBuffer();

      return new Response(buf, {
        headers: {
          'Content-Type': mime,
          'Access-Control-Allow-Origin': '*',
          'Cache-Control': 'max-age=1800',
        },
      });

    } catch (e) {
      return err(e.message || String(e));
    }
  },
};
