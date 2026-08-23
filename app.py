import os
import re
import secrets
import string
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import psycopg2
import psycopg2.extras
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
BANK_VERIFY_URL = os.environ.get("BANK_VERIFY_URL", "").strip()
BANK_VERIFY_TOKEN = os.environ.get("BANK_VERIFY_TOKEN", "").strip()

BANK = {
    "bankCode": os.environ.get("BANK_CODE", "970423"),
    "stk": os.environ.get("BANK_STK", "45626072009"),
    "accountName": os.environ.get("BANK_ACCOUNT_NAME", "LO VAN HIEP"),
    "bankName": os.environ.get("BANK_NAME", "TPBank"),
}

PLANS = {
    "90m-1": {"label": "90MIN-1TB", "price": 33000, "ms": 5400000, "devices": 1},
    "90m-2": {"label": "90MIN-2TB", "price": 41000, "ms": 5400000, "devices": 2},
    "90m-3": {"label": "90MIN-3TB", "price": 53000, "ms": 5400000, "devices": 3},
}

def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL chưa được cấu hình.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS payment_orders (
                id TEXT PRIMARY KEY,
                plan_key TEXT NOT NULL,
                plan_label TEXT NOT NULL,
                price INTEGER NOT NULL,
                pay_code TEXT UNIQUE NOT NULL,
                device TEXT NOT NULL,
                account_id TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                proof_text TEXT DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                reviewed_at TIMESTAMPTZ,
                issued_key TEXT
            );
            CREATE TABLE IF NOT EXISTS issued_keys (
                key TEXT PRIMARY KEY,
                plan_key TEXT NOT NULL,
                plan_label TEXT NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                max_devices INTEGER NOT NULL DEFAULT 1,
                devices_used INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS inbox_keys (
                id BIGSERIAL PRIMARY KEY,
                device TEXT NOT NULL,
                account_id TEXT DEFAULT '',
                key TEXT NOT NULL,
                note TEXT DEFAULT '',
                order_id TEXT,
                expires_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                delivered BOOLEAN NOT NULL DEFAULT FALSE
            );
            """)
        conn.commit()

def clean(s):
    return str(s or "").strip()

def norm(s):
    s = clean(s).upper()
    # Keep letters/numbers only; this matches the frontend OCR normalizer.
    return re.sub(r"[^A-Z0-9]", "", s)

def money_values(text):
    t = clean(text).upper().replace(".", "").replace(",", "")
    vals = []
    for m in re.finditer(r"(?<!\d)(\d{4,9})(?:\s*(?:VND|VNĐ|D|DONG))?(?!\d)", t):
        try:
            vals.append(int(m.group(1)))
        except Exception:
            pass
    return vals

def new_id(prefix="ORD"):
    alphabet = string.ascii_uppercase + string.digits
    return prefix + datetime.now(timezone.utc).strftime("%y%m%d%H%M%S") + "".join(secrets.choice(alphabet) for _ in range(8))

def new_pay_code():
    return "MUKEY-" + secrets.token_hex(4).upper()

def new_key(plan_label):
    # Example: SHADOW-90MIN-AB12CD34
    suffix = secrets.token_hex(5).upper()
    return f"SHADOW-{plan_label.split('-')[0]}-{suffix}"

def issue_key(conn, order):
    plan = PLANS[order["plan_key"]]
    key = new_key(order["plan_label"])
    expires = datetime.now(timezone.utc) + timedelta(milliseconds=plan["ms"])
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO issued_keys(key, plan_key, plan_label, expires_at, max_devices)
            VALUES(%s,%s,%s,%s,%s)
        """, (key, order["plan_key"], order["plan_label"], expires, plan["devices"]))
        cur.execute("""
            INSERT INTO inbox_keys(device, account_id, key, note, order_id, expires_at)
            VALUES(%s,%s,%s,%s,%s,%s)
        """, (
            order["device"], order["account_id"], key,
            f"Mua {order['plan_label']} - {order['price']:,}đ",
            order["id"], expires
        ))
        cur.execute("""
            UPDATE payment_orders
            SET status='completed', issued_key=%s, reviewed_at=NOW()
            WHERE id=%s
        """, (key, order["id"]))
    return key


@app.get("/")
def home():
    return jsonify(
        ok=True,
        service="TXAl Payment Backend",
        status="online"
    )

@app.get("/health")
def health():
    try:
        init_db()
        return jsonify(ok=True, service="payment-proof")
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

@app.get("/bank-config")
def bank_config():
    return jsonify(BANK)

@app.post("/create-order")
def create_order():
    data = request.get_json(silent=True) or {}
    plan_key = clean(data.get("planKey"))
    device = clean(data.get("device"))
    account_id = clean(data.get("accountId"))
    plan = PLANS.get(plan_key)
    if not plan:
        return jsonify(error="Gói key không hợp lệ."), 400
    if not device:
        return jsonify(error="Thiếu device."), 400

    order_id = new_id()
    pay_code = new_pay_code()
    with db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO payment_orders
                (id,plan_key,plan_label,price,pay_code,device,account_id)
                VALUES(%s,%s,%s,%s,%s,%s,%s)
                RETURNING id,plan_key,plan_label,price,pay_code,device,account_id,
                          status,extract(epoch from created_at)*1000 AS created_ms
            """, (
                order_id, plan_key, plan["label"], plan["price"], pay_code,
                device, account_id
            ))
            row = cur.fetchone()
        conn.commit()

    return jsonify(order={
        "id": row["id"],
        "plan": row["plan_label"],
        "price": int(row["price"]),
        "content": row["pay_code"],
        "payCode": row["pay_code"],
        "device": row["device"],
        "accountId": row["account_id"] or "",
        "createdAt": int(row["created_ms"] or 0),
        "status": row["status"],
    })

@app.post("/payment-proof")
def payment_proof():
    data = request.get_json(silent=True) or {}
    order_id = clean(data.get("orderId"))
    device = clean(data.get("device"))
    account_id = clean(data.get("accountId"))
    proof_text = clean(data.get("proofText"))

    if not order_id or not device or not proof_text:
        return jsonify(valid=False, error="Thiếu orderId/device/proofText."), 400
    if len(proof_text) > 12000:
        proof_text = proof_text[:12000]

    with db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM payment_orders WHERE id=%s FOR UPDATE", (order_id,))
            order = cur.fetchone()
            if not order:
                return jsonify(valid=False, error="Đơn không tồn tại."), 404
            if order["device"] != device:
                return jsonify(valid=False, error="Đơn không thuộc thiết bị này."), 403
            if order["status"] == "completed":
                return jsonify(valid=True, completed=True, key=order["issued_key"])
            if order["status"] == "reviewing":
                return jsonify(valid=True, reviewing=True)
            if order["status"] == "rejected":
                return jsonify(valid=False, error="Đơn đã bị từ chối."), 409

            expected_code = norm(order["pay_code"])
            text_norm = norm(proof_text)
            code_ok = expected_code in text_norm
            amount_ok = int(order["price"]) in money_values(proof_text)

            if not code_ok or not amount_ok:
                cur.execute("""
                    UPDATE payment_orders SET status='rejected', proof_text=%s, reviewed_at=NOW()
                    WHERE id=%s
                """, (proof_text, order_id))
                conn.commit()
                return jsonify(
                    valid=False,
                    error="Bill không khớp số tiền hoặc nội dung chuyển khoản."
                ), 422

            # Optional second layer: verify the transaction against a real bank API.
            # The provider must accept {"amount":..., "content":..., "orderId":...}
            # and return {"valid": true}. Without this env var, the endpoint
            # performs server-side order validation + OCR text matching only.
            if BANK_VERIFY_URL:
                headers = {"Content-Type": "application/json"}
                if BANK_VERIFY_TOKEN:
                    headers["Authorization"] = "Bearer " + BANK_VERIFY_TOKEN
                try:
                    br = requests.post(
                        BANK_VERIFY_URL,
                        json={
                            "amount": int(order["price"]),
                            "content": order["pay_code"],
                            "orderId": order["id"],
                        },
                        headers=headers,
                        timeout=12,
                    )
                    bj = br.json() if br.content else {}
                    if br.status_code >= 300 or not bj.get("valid"):
                        cur.execute("""
                            UPDATE payment_orders
                            SET status='rejected', proof_text=%s, reviewed_at=NOW()
                            WHERE id=%s
                        """, (proof_text, order_id))
                        conn.commit()
                        return jsonify(valid=False, error="Ngân hàng chưa xác nhận giao dịch."), 422
                except Exception:
                    return jsonify(valid=False, error="Không kiểm tra được giao dịch ngân hàng."), 502

            cur.execute("""
                UPDATE payment_orders
                SET status='reviewing', proof_text=%s
                WHERE id=%s
            """, (proof_text, order_id))
            order = dict(order)
            order["account_id"] = account_id or order["account_id"] or ""
            key = issue_key(conn, order)
        conn.commit()

    return jsonify(valid=True, completed=True, key=key)

@app.get("/inbox")
def inbox():
    device = clean(request.args.get("device"))
    if not device:
        return jsonify(keys=[], messages=[])
    with db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id,key,note,order_id,
                       extract(epoch from expires_at)*1000 AS exp,
                       extract(epoch from created_at)*1000 AS received_at
                FROM inbox_keys
                WHERE device=%s AND delivered=FALSE
                  AND (expires_at IS NULL OR expires_at > NOW())
                ORDER BY id DESC
            """, (device,))
            rows = cur.fetchall()
            # Mark delivered only after returning them. The client stores them locally.
            ids = [r["id"] for r in rows]
            if ids:
                cur.execute("UPDATE inbox_keys SET delivered=TRUE WHERE id = ANY(%s)", (ids,))
        conn.commit()

    keys = [{
        "id": str(r["id"]),
        "key": r["key"],
        "note": r["note"] or "",
        "orderId": r["order_id"] or "",
        "exp": int((r["exp"] or 0)),
        "receivedAt": int((r["received_at"] or 0)),
    } for r in rows]
    return jsonify(keys=keys, messages=[])

@app.get("/keys")
def keys():
    with db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT key, plan_label, expires_at, max_devices, devices_used
                FROM issued_keys ORDER BY created_at DESC LIMIT 500
            """)
            rows = cur.fetchall()
    return jsonify(keys=[{
        "key": r["key"],
        "plan": r["plan_label"],
        "exp": r["expires_at"].isoformat() if r["expires_at"] else None,
        "maxDevices": r["max_devices"],
        "devicesUsed": r["devices_used"],
    } for r in rows])

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
