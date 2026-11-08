"""SNMP Poller — poll tiap radio link, simpan hasil ke DB."""
from __future__ import annotations
import asyncio, logging, sqlite3
from datetime import datetime
from pathlib import Path

from pysnmp.hlapi.v3arch.asyncio import (
    SnmpEngine, CommunityData, UdpTransportTarget, ContextData,
    ObjectType, ObjectIdentity, get_cmd
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("poller")

DB_PATH = Path(__file__).parent / "noc.db"

# ── OID MikroTik Wireless ─────────────────────────────────────────────────────
# Signal ke AP (RX di sisi radio)
OID_SIGNAL     = "1.3.6.1.4.1.14988.1.1.1.1.1.4.1"  # mtxrWlApClientSignal (per client index 1)
OID_SIGNAL_TX  = "1.3.6.1.4.1.14988.1.1.1.1.1.3.1"  # mtxrWlApClientTxRate
OID_CCQ        = "1.3.6.1.4.1.14988.1.1.1.3.1.9.1"  # mtxrWlCMOverallTxCCQ
# Standard sysDescr untuk verifikasi
OID_SYSDESCR   = "1.3.6.1.2.1.1.1.0"
OID_UPTIME     = "1.3.6.1.2.1.1.3.0"
# Wireless signal standard (IEEE 802.11 MIB) — untuk fallback
OID_IF_IN      = "1.3.6.1.2.1.2.2.1.10.1"
OID_IF_OUT     = "1.3.6.1.2.1.2.2.1.16.1"

# Ubiquiti AirFiber OIDs
OID_UB_RX_SIGNAL = "1.3.6.1.4.1.41112.1.4.4.1.3.1"  # ubntAirFiber RX signal
OID_UB_TX_SIGNAL = "1.3.6.1.4.1.41112.1.4.4.1.2.1"  # ubntAirFiber TX signal

# Mimosa — pakai SNMP community standard, signal via wireless MIB
OID_MIMOSA_SIGNAL = "1.3.6.1.4.1.43356.2.1.2.1.1.1.1.4"


def _db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _init_poll_tables():
    with _db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS poll_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            link_id     INTEGER NOT NULL,
            polled_at   TEXT NOT NULL,
            sinyal_rx   INTEGER,
            sinyal_tx   INTEGER,
            ccq         INTEGER,
            uptime_sec  INTEGER,
            status      TEXT,
            raw_info    TEXT
        )""")
        # Tambah kolom ke radio_links kalau belum ada
        cols = [r[1] for r in con.execute("PRAGMA table_info(radio_links)").fetchall()]
        if "last_polled" not in cols:
            con.execute("ALTER TABLE radio_links ADD COLUMN last_polled TEXT")
        if "poll_signal_rx" not in cols:
            con.execute("ALTER TABLE radio_links ADD COLUMN poll_signal_rx INTEGER")
        if "poll_signal_tx" not in cols:
            con.execute("ALTER TABLE radio_links ADD COLUMN poll_signal_tx INTEGER")
        if "poll_ccq" not in cols:
            con.execute("ALTER TABLE radio_links ADD COLUMN poll_ccq INTEGER")
        if "poll_status" not in cols:
            con.execute("ALTER TABLE radio_links ADD COLUMN poll_status TEXT DEFAULT 'unknown'")
        if "snmp_community" not in cols:
            con.execute("ALTER TABLE radio_links ADD COLUMN snmp_community TEXT DEFAULT 'public'")


async def _snmp_get(ip: str, oid: str, community: str = "public", port: int = 161, timeout: int = 5):
    """Ambil satu OID via SNMP v2c. Return (value, error_str)."""
    try:
        transport = await UdpTransportTarget.create((ip, port), timeout=timeout, retries=1)
        errorIndication, errorStatus, _, varBinds = await get_cmd(
            SnmpEngine(),
            CommunityData(community, mpModel=1),
            transport,
            ContextData(),
            ObjectType(ObjectIdentity(oid))
        )
        if errorIndication:
            return None, str(errorIndication)
        if errorStatus:
            return None, errorStatus.prettyPrint()
        val = varBinds[0][1]
        return int(val), None
    except Exception as e:
        return None, str(e)


async def _poll_mikrotik(ip: str, community: str):
    """Poll MikroTik device."""
    sig_rx, err_rx = await _snmp_get(ip, OID_SIGNAL, community)
    sig_tx, err_tx = await _snmp_get(ip, OID_SIGNAL_TX, community)
    ccq, _         = await _snmp_get(ip, OID_CCQ, community)
    uptime, _      = await _snmp_get(ip, OID_UPTIME, community)

    if sig_rx is None and sig_tx is None:
        return None, None, None, None, f"SNMP gagal: {err_rx}"
    return sig_rx, sig_tx, ccq, uptime, None


async def _poll_airfiber(ip: str, community: str):
    """Poll Ubiquiti AirFiber."""
    sig_rx, err_rx = await _snmp_get(ip, OID_UB_RX_SIGNAL, community)
    sig_tx, _      = await _snmp_get(ip, OID_UB_TX_SIGNAL, community)
    uptime, _      = await _snmp_get(ip, OID_UPTIME, community)
    if sig_rx is None:
        return None, None, None, None, f"SNMP gagal: {err_rx}"
    return sig_rx, sig_tx, None, uptime, None


async def _poll_generic(ip: str, community: str):
    """Generic SNMP poll — coba sysDescr dulu, lalu wireless OID."""
    sig, err = await _snmp_get(ip, OID_SIGNAL, community)
    uptime, _ = await _snmp_get(ip, OID_UPTIME, community)
    return sig, None, None, uptime, err


def _signal_status(sig_rx) -> str:
    if sig_rx is None:
        return "offline"
    if sig_rx > -70:
        return "baik"
    elif sig_rx > -80:
        return "lemah"
    else:
        return "kritis"


async def poll_link(link: sqlite3.Row):
    ip        = link["ip_mgmt"]
    vendor    = (link["vendor"] or "").lower()
    community = link["snmp_community"] or "public"
    link_id   = link["id"]

    if not ip or ip.strip() == "":
        log.warning(f"[{link['nama_link']}] IP mgmt kosong, skip")
        return

    log.info(f"Polling [{link['nama_link']}] {ip} vendor={vendor}")

    try:
        if "airfiber" in vendor or "ubiquiti" in vendor:
            sig_rx, sig_tx, ccq, uptime, err = await _poll_airfiber(ip, community)
        elif "mikrotik" in vendor:
            sig_rx, sig_tx, ccq, uptime, err = await _poll_mikrotik(ip, community)
        else:
            sig_rx, sig_tx, ccq, uptime, err = await _poll_generic(ip, community)
    except Exception as e:
        sig_rx, sig_tx, ccq, uptime, err = None, None, None, None, str(e)

    status = _signal_status(sig_rx)
    now    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with _db() as con:
        con.execute("""
            INSERT INTO poll_history (link_id, polled_at, sinyal_rx, sinyal_tx, ccq, uptime_sec, status, raw_info)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (link_id, now, sig_rx, sig_tx, ccq, uptime, status, err or ""))

        con.execute("""
            UPDATE radio_links SET
                last_polled=?, poll_signal_rx=?, poll_signal_tx=?, poll_ccq=?, poll_status=?
            WHERE id=?
        """, (now, sig_rx, sig_tx, ccq, status, link_id))

    if err:
        log.warning(f"  [{link['nama_link']}] error: {err}")
    else:
        log.info(f"  [{link['nama_link']}] RX={sig_rx} TX={sig_tx} CCQ={ccq} status={status}")


async def poll_all():
    with _db() as con:
        links = con.execute(
            "SELECT * FROM radio_links WHERE ip_mgmt IS NOT NULL AND ip_mgmt != '' AND status != 'Nonaktif'"
        ).fetchall()

    if not links:
        log.info("Tidak ada link dengan IP management untuk di-poll")
        return

    log.info(f"Mulai polling {len(links)} link...")
    tasks = [poll_link(link) for link in links]
    await asyncio.gather(*tasks)
    log.info("Polling selesai.")


if __name__ == "__main__":
    _init_poll_tables()
    asyncio.run(poll_all())
