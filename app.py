"""NOC Radio Manager — Manajemen frekuensi radio untuk tim NOC."""
from __future__ import annotations
import asyncio, hashlib, logging, os, sqlite3, subprocess
from datetime import datetime
from pathlib import Path

import yaml
from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer, BadSignature

logging.basicConfig(level=logging.INFO)

CFG        = yaml.safe_load((Path(__file__).parent / "configs" / "noc.yaml").read_text())
APP_NAME   = CFG["app"]["name"]
SECRET_KEY = CFG["app"]["secret_key"]
PORT       = CFG["app"].get("port", 8096)
SA_USER    = CFG["superadmin"]["username"]
SA_PASS    = hashlib.sha256(CFG["superadmin"]["password"].encode()).hexdigest()

DB_PATH = Path(__file__).parent / "noc.db"

app = FastAPI()
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
tpl     = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
signer  = URLSafeTimedSerializer(SECRET_KEY)

VENDOR_LIST = ["MikroTik", "Mimosa", "Ubiquiti AirFiber", "Cambium", "Lainnya"]
BAND_LIST   = ["2.4 GHz", "5 GHz", "5.8 GHz", "11 GHz", "18 GHz", "24 GHz"]
WIDTH_LIST  = ["5 MHz", "10 MHz", "20 MHz", "40 MHz", "80 MHz"]
STATUS_LIST = ["Aktif", "Gangguan", "Maintenance", "Nonaktif"]


# ── DB ────────────────────────────────────────────────────────────────────────

def _db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def _init_db():
    with _db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS radio_links (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            nama_link   TEXT NOT NULL,
            lokasi_a    TEXT,
            lokasi_b    TEXT,
            koordinat   TEXT,
            vendor      TEXT,
            band        TEXT,
            frekuensi   TEXT,
            channel_width TEXT,
            ssid        TEXT,
            ip_mgmt     TEXT,
            sinyal_tx   TEXT,
            sinyal_rx   TEXT,
            jarak_km    TEXT,
            status      TEXT DEFAULT 'Aktif',
            catatan     TEXT,
            created_at  TEXT,
            updated_at  TEXT
        )""")

_init_db()


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _hash(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def _set_session(resp, username: str):
    token = signer.dumps(username)
    resp.set_cookie("session", token, httponly=True, max_age=86400 * 7)

def _get_user(req: Request):
    token = req.cookies.get("session")
    if not token:
        return None
    try:
        return signer.loads(token, max_age=86400 * 7)
    except BadSignature:
        return None

def _require_login(req: Request):
    user = _get_user(req)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _ts(ts) -> str:
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").strftime("%-d %b %Y %H:%M")
    except Exception:
        return str(ts) if ts else "-"

def _ctx(**kw):
    return {"app_name": APP_NAME, **kw}


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(req: Request, error: str = ""):
    if _get_user(req):
        return RedirectResponse("/", 303)
    return tpl.TemplateResponse(req, "login.html", _ctx(error=error))

@app.post("/login")
async def login_post(req: Request, username: str = Form(...), password: str = Form(...)):
    if username == SA_USER and _hash(password) == SA_PASS:
        resp = RedirectResponse("/", 303)
        _set_session(resp, username)
        return resp
    return RedirectResponse("/login?error=Username+atau+password+salah", 303)

@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", 303)
    resp.delete_cookie("session")
    return resp


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(req: Request):
    _require_login(req)
    with _db() as con:
        total   = con.execute("SELECT COUNT(*) FROM radio_links").fetchone()[0]
        aktif   = con.execute("SELECT COUNT(*) FROM radio_links WHERE status='Aktif'").fetchone()[0]
        gangguan= con.execute("SELECT COUNT(*) FROM radio_links WHERE status='Gangguan'").fetchone()[0]
        maintenance = con.execute("SELECT COUNT(*) FROM radio_links WHERE status='Maintenance'").fetchone()[0]
        by_vendor = con.execute("""
            SELECT vendor, COUNT(*) as jml FROM radio_links GROUP BY vendor ORDER BY jml DESC
        """).fetchall()
        by_band = con.execute("""
            SELECT band, COUNT(*) as jml FROM radio_links GROUP BY band ORDER BY jml DESC
        """).fetchall()
        by_frek = con.execute("""
            SELECT frekuensi, COUNT(*) as jml FROM radio_links
            WHERE frekuensi != '' AND frekuensi IS NOT NULL
            GROUP BY frekuensi ORDER BY frekuensi
        """).fetchall()
        recent = con.execute("""
            SELECT * FROM radio_links ORDER BY updated_at DESC LIMIT 5
        """).fetchall()
    return tpl.TemplateResponse(req, "dashboard.html", _ctx(
        total=total, aktif=aktif, gangguan=gangguan, maintenance=maintenance,
        by_vendor=by_vendor, by_band=by_band, by_frek=by_frek, recent=recent, ts=_ts,
        user=_get_user(req)
    ))


# ── Radio Links ───────────────────────────────────────────────────────────────

@app.get("/radio", response_class=HTMLResponse)
async def radio_list(req: Request, q: str = "", status: str = "", vendor: str = "", band: str = ""):
    _require_login(req)
    sql  = "SELECT * FROM radio_links WHERE 1=1"
    args = []
    if q:
        sql += " AND (nama_link LIKE ? OR lokasi_a LIKE ? OR lokasi_b LIKE ? OR ip_mgmt LIKE ? OR ssid LIKE ?)"
        args += [f"%{q}%"] * 5
    if status:
        sql += " AND status=?"
        args.append(status)
    if vendor:
        sql += " AND vendor=?"
        args.append(vendor)
    if band:
        sql += " AND band=?"
        args.append(band)
    sql += " ORDER BY nama_link"
    with _db() as con:
        rows = con.execute(sql, args).fetchall()
    return tpl.TemplateResponse(req, "radio_list.html", _ctx(
        rows=rows, q=q, status=status, vendor=vendor, band=band,
        status_list=STATUS_LIST, vendor_list=VENDOR_LIST, band_list=BAND_LIST, ts=_ts,
        user=_get_user(req)
    ))

@app.get("/radio/tambah", response_class=HTMLResponse)
async def radio_form_new(req: Request):
    _require_login(req)
    return tpl.TemplateResponse(req, "radio_form.html", _ctx(
        row=None, vendor_list=VENDOR_LIST, band_list=BAND_LIST,
        width_list=WIDTH_LIST, status_list=STATUS_LIST, error="",
        user=_get_user(req)
    ))

@app.post("/radio/tambah")
async def radio_form_new_post(req: Request,
    nama_link: str = Form(...), lokasi_a: str = Form(""), lokasi_b: str = Form(""),
    koordinat: str = Form(""), vendor: str = Form(""), band: str = Form(""),
    frekuensi: str = Form(""), channel_width: str = Form(""), ssid: str = Form(""),
    ip_mgmt: str = Form(""), sinyal_tx: str = Form(""), sinyal_rx: str = Form(""),
    jarak_km: str = Form(""), status: str = Form("Aktif"), catatan: str = Form(""),
    snmp_community: str = Form("public")
):
    _require_login(req)
    now = _now()
    with _db() as con:
        con.execute("""
            INSERT INTO radio_links
            (nama_link,lokasi_a,lokasi_b,koordinat,vendor,band,frekuensi,channel_width,
             ssid,ip_mgmt,sinyal_tx,sinyal_rx,jarak_km,status,catatan,snmp_community,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (nama_link,lokasi_a,lokasi_b,koordinat,vendor,band,frekuensi,channel_width,
              ssid,ip_mgmt,sinyal_tx,sinyal_rx,jarak_km,status,catatan,snmp_community,now,now))
    return RedirectResponse("/radio", 303)

@app.get("/radio/{rid}/edit", response_class=HTMLResponse)
async def radio_form_edit(req: Request, rid: int):
    _require_login(req)
    with _db() as con:
        row = con.execute("SELECT * FROM radio_links WHERE id=?", (rid,)).fetchone()
    if not row:
        return RedirectResponse("/radio", 303)
    return tpl.TemplateResponse(req, "radio_form.html", _ctx(
        row=row, vendor_list=VENDOR_LIST, band_list=BAND_LIST,
        width_list=WIDTH_LIST, status_list=STATUS_LIST, error="",
        user=_get_user(req)
    ))

@app.post("/radio/{rid}/edit")
async def radio_form_edit_post(req: Request, rid: int,
    nama_link: str = Form(...), lokasi_a: str = Form(""), lokasi_b: str = Form(""),
    koordinat: str = Form(""), vendor: str = Form(""), band: str = Form(""),
    frekuensi: str = Form(""), channel_width: str = Form(""), ssid: str = Form(""),
    ip_mgmt: str = Form(""), sinyal_tx: str = Form(""), sinyal_rx: str = Form(""),
    jarak_km: str = Form(""), status: str = Form("Aktif"), catatan: str = Form(""),
    snmp_community: str = Form("public")
):
    _require_login(req)
    now = _now()
    with _db() as con:
        con.execute("""
            UPDATE radio_links SET
            nama_link=?,lokasi_a=?,lokasi_b=?,koordinat=?,vendor=?,band=?,frekuensi=?,
            channel_width=?,ssid=?,ip_mgmt=?,sinyal_tx=?,sinyal_rx=?,jarak_km=?,
            status=?,catatan=?,snmp_community=?,updated_at=?
            WHERE id=?
        """, (nama_link,lokasi_a,lokasi_b,koordinat,vendor,band,frekuensi,channel_width,
              ssid,ip_mgmt,sinyal_tx,sinyal_rx,jarak_km,status,catatan,snmp_community,now,rid))
    return RedirectResponse("/radio", 303)

@app.post("/radio/{rid}/hapus")
async def radio_hapus(req: Request, rid: int):
    _require_login(req)
    with _db() as con:
        con.execute("DELETE FROM radio_links WHERE id=?", (rid,))
    return RedirectResponse("/radio", 303)

@app.get("/radio/{rid}/detail", response_class=HTMLResponse)
async def radio_detail(req: Request, rid: int):
    _require_login(req)
    with _db() as con:
        row = con.execute("SELECT * FROM radio_links WHERE id=?", (rid,)).fetchone()
    if not row:
        return RedirectResponse("/radio", 303)
    return tpl.TemplateResponse(req, "radio_detail.html", _ctx(row=row, ts=_ts, user=_get_user(req)))


# ── API JSON (untuk integrasi Zabbix / monitoring) ────────────────────────────

@app.get("/api/radio")
async def api_radio(req: Request, status: str = ""):
    _require_login(req)
    sql  = "SELECT * FROM radio_links"
    args = []
    if status:
        sql += " WHERE status=?"
        args.append(status)
    with _db() as con:
        rows = [dict(r) for r in con.execute(sql, args).fetchall()]
    return JSONResponse(rows)

@app.get("/api/radio/{rid}")
async def api_radio_one(req: Request, rid: int):
    _require_login(req)
    with _db() as con:
        row = con.execute("SELECT * FROM radio_links WHERE id=?", (rid,)).fetchone()
    if not row:
        raise HTTPException(404)
    return JSONResponse(dict(row))


# ── Frekuensi Map (cek tabrakan) ──────────────────────────────────────────────

@app.get("/frekuensi", response_class=HTMLResponse)
async def frekuensi_map(req: Request):
    _require_login(req)
    with _db() as con:
        rows = con.execute("""
            SELECT frekuensi, band, channel_width,
                   GROUP_CONCAT(nama_link, '|') as links,
                   COUNT(*) as jml
            FROM radio_links
            WHERE frekuensi != '' AND frekuensi IS NOT NULL
            GROUP BY frekuensi, band
            ORDER BY band, frekuensi
        """).fetchall()
    return tpl.TemplateResponse(req, "frekuensi.html", _ctx(rows=rows, user=_get_user(req)))


# ── Monitoring Real-time ──────────────────────────────────────────────────────

@app.get("/monitoring", response_class=HTMLResponse)
async def monitoring_page(req: Request):
    _require_login(req)
    with _db() as con:
        links = con.execute("SELECT * FROM radio_links ORDER BY poll_status, nama_link").fetchall()
        counts = {
            "baik"   : con.execute("SELECT COUNT(*) FROM radio_links WHERE poll_status='baik'").fetchone()[0],
            "lemah"  : con.execute("SELECT COUNT(*) FROM radio_links WHERE poll_status='lemah'").fetchone()[0],
            "kritis" : con.execute("SELECT COUNT(*) FROM radio_links WHERE poll_status='kritis'").fetchone()[0],
            "offline": con.execute("SELECT COUNT(*) FROM radio_links WHERE poll_status='offline'").fetchone()[0],
            "unknown": con.execute("SELECT COUNT(*) FROM radio_links WHERE poll_status IS NULL OR poll_status='unknown'").fetchone()[0],
        }
    return tpl.TemplateResponse(req, "monitoring.html", _ctx(
        links=links, counts=counts, ts=_ts, user=_get_user(req)
    ))

@app.post("/monitoring/poll-now")
async def monitoring_poll_now(req: Request):
    _require_login(req)
    poller_path = str(Path(__file__).parent / "poller.py")
    venv_python = str(Path(__file__).parent / ".venv" / "bin" / "python")
    subprocess.Popen([venv_python, poller_path])
    return RedirectResponse("/monitoring?ok=Polling+dimulai+di+background", 303)

@app.get("/monitoring/history/{rid}", response_class=HTMLResponse)
async def monitoring_history(req: Request, rid: int):
    _require_login(req)
    with _db() as con:
        link = con.execute("SELECT * FROM radio_links WHERE id=?", (rid,)).fetchone()
        history = con.execute("""
            SELECT * FROM poll_history WHERE link_id=?
            ORDER BY polled_at DESC LIMIT 100
        """, (rid,)).fetchall()
    if not link:
        return RedirectResponse("/monitoring", 303)
    return tpl.TemplateResponse(req, "poll_history.html", _ctx(
        link=link, history=history, ts=_ts, user=_get_user(req)
    ))


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=PORT, reload=True)
